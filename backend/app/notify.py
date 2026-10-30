# -*- coding: utf-8 -*-
"""W10 Webhook 告警通知（后台发送、失败重试、去重、不阻塞采集）。

契约（docs/W10-路径追踪告警聚合设计.md §三）：

- 环境变量：``NOTIFY_ENABLED`` / ``NOTIFY_WEBHOOK_URL`` /
  ``NOTIFY_WEBHOOK_TYPE=generic|dingtalk|feishu|wecom`` / ``NOTIFY_MIN_SEVERITY``；
- pipeline 每轮新产生的 **active** 告警里 severity 达标的，投递到后台队列发送；
  cleared/info 不发；同一 ``dedup_key`` 默认 5 分钟内只发一次；
- 失败重试 2 次（1s/3s 指数退避），记录 last_sent_at / last_error / sent_count；
- 队列 + worker 在 lifespan 中启动/停止，绝不阻塞 5s 采集循环；
- ``GET /api/notify/status`` / ``POST /api/notify/test``。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from app.config import Settings
from app.models import Alert

logger = logging.getLogger("campus_twin.notify")

SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class NotifyService:
    """Webhook 通知服务（单例，由 app lifespan 启动/停止）。"""

    def __init__(
        self,
        settings: Settings,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.settings = settings
        self.enabled = bool(settings.notify_enabled and settings.notify_webhook_url)
        self.webhook_url = str(settings.notify_webhook_url or "").strip()
        self.webhook_type = str(settings.notify_webhook_type or "generic").lower()
        self.min_severity = str(settings.notify_min_severity or "critical").lower()
        self.dedup_seconds = float(settings.notify_dedup_seconds)
        self.timeout = float(settings.notify_timeout)
        self.retry_delays = tuple(float(delay) for delay in settings.notify_retry_delays)
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        # 同 dedup_key 上次发送时间
        self._last_sent_keys: Dict[str, float] = {}
        self.last_sent_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.sent_count: int = 0
        self.failed_count: int = 0
        self.last_payload: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if not self.enabled:
            logger.info("Webhook 通知未启用（NOTIFY_ENABLED/NOTIFY_WEBHOOK_URL 未配置）")
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 2.0)),
            trust_env=False,  # localhost 接收器不能走系统代理
            transport=self._transport,
        )
        self._queue = asyncio.Queue(maxsize=200)
        self._worker = asyncio.create_task(self._worker_loop(), name="campus-twin-notify")
        logger.info(
            "Webhook 通知已启用：type=%s url=%s min_severity=%s",
            self.webhook_type,
            self.webhook_url,
            self.min_severity,
        )

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._queue = None
        logger.info(
            "Webhook 通知已停止（sent=%d failed=%d）",
            self.sent_count,
            self.failed_count,
        )

    # ------------------------------------------------------------------ #
    # 入队（同步，非阻塞；由 pipeline 每轮调用）
    # ------------------------------------------------------------------ #
    def should_notify(self, alert: Alert) -> bool:
        if not self.enabled or self._queue is None:
            return False
        if alert.state != "active" or alert.deleted_at is not None:
            return False
        if alert.status == "silenced":
            return False
        alert_rank = SEVERITY_RANK.get(str(alert.severity or "").lower(), 0)
        min_rank = SEVERITY_RANK.get(self.min_severity, SEVERITY_RANK["critical"])
        return alert_rank >= min_rank

    def enqueue(self, alert: Alert) -> bool:
        """把新 active 告警投递到后台发送队列；返回是否真的入队。"""
        if not self.should_notify(alert):
            return False
        dedup_key = alert.dedup_key or "alert:{}".format(alert.id or alert.title)
        now = time.time()
        last_sent = self._last_sent_keys.get(dedup_key)
        if last_sent is not None and now - last_sent < self.dedup_seconds:
            logger.debug("Webhook 去重命中：%s（%.0fs 内已发送）", dedup_key, self.dedup_seconds)
            return False
        assert self._queue is not None
        try:
            self._queue.put_nowait(alert.model_dump(mode="json"))
        except asyncio.QueueFull:
            logger.warning("Webhook 队列已满，丢弃告警：%s", alert.title)
            return False
        self._last_sent_keys[dedup_key] = now
        # 防止 _last_sent_keys 无限增长：超过 500 条时清理最旧的
        if len(self._last_sent_keys) > 500:
            oldest = sorted(self._last_sent_keys.items(), key=lambda item: item[1])[:100]
            for key, _ in oldest:
                self._last_sent_keys.pop(key, None)
        return True

    async def _worker_loop(self) -> None:
        assert self._queue is not None
        while True:
            alert = await self._queue.get()
            try:
                await self._deliver_with_retries(alert)
            except Exception:  # noqa: BLE001 - worker 不能因单条失败退出
                logger.exception("Webhook 通知后台任务异常")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    def _payload_for_alert(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        title = str(alert.get("title") or "校园网告警")
        severity = str(alert.get("severity") or "")
        device = (
            alert.get("device_id")
            or alert.get("device_name")
            or alert.get("detail")
            or "未知设备"
        )
        observed_at = alert.get("observed_at")
        time_text = _iso(float(observed_at)) if observed_at else _iso(time.time())
        detail = str(alert.get("detail") or "")
        alert_id = alert.get("id")
        if self.webhook_type == "dingtalk":
            return {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": self._markdown_text(title, severity, device, time_text, detail),
                },
            }
        if self.webhook_type == "feishu":
            return {
                "msg_type": "text",
                "content": {
                    "text": self._plain_text(title, severity, device, time_text, detail)
                },
            }
        if self.webhook_type == "wecom":
            return {
                "msgtype": "markdown",
                "markdown": {
                    "content": self._markdown_text(
                        title, severity, device, time_text, detail
                    )
                },
            }
        return {
            "title": title,
            "severity": severity,
            "device": device,
            "time": time_text,
            "detail": detail,
            "alert_id": alert_id,
        }

    @staticmethod
    def _plain_text(
        title: str, severity: str, device: Any, time_text: Optional[str], detail: str
    ) -> str:
        return "【校园网告警】{}\n级别: {}\n设备: {}\n时间: {}\n详情: {}".format(
            title, severity, device, time_text or "-", detail or "-"
        )

    @staticmethod
    def _markdown_text(
        title: str, severity: str, device: Any, time_text: Optional[str], detail: str
    ) -> str:
        return "### {}\n- **级别**: {}\n- **设备**: {}\n- **时间**: {}\n- **详情**: {}".format(
            title, severity, device, time_text or "-", detail or "-"
        )

    async def _post(self, payload: Dict[str, Any]) -> None:
        assert self._client is not None
        response = await self._client.post(self.webhook_url, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(
                "Webhook 返回 HTTP {}: {}".format(
                    response.status_code, response.text[:200]
                )
            )

    async def _deliver_with_retries(self, alert: Dict[str, Any]) -> bool:
        payload = self._payload_for_alert(alert)
        self.last_payload = payload
        attempts = 1 + len(self.retry_delays)  # 初次 + 2 次重试
        last_error: Optional[str] = None
        for attempt in range(attempts):
            try:
                await self._post(payload)
                self.sent_count += 1
                self.last_sent_at = time.time()
                self.last_error = None
                logger.info(
                    "Webhook 已发送：%s（第 %d 次尝试）", alert.get("title"), attempt + 1
                )
                return True
            except Exception as exc:  # noqa: BLE001 - 失败重试，不抛出到采集链路
                last_error = "{}: {}".format(type(exc).__name__, exc)
                self.failed_count += 1
                self.last_error = last_error
                logger.warning(
                    "Webhook 发送失败（%d/%d）：%s", attempt + 1, attempts, last_error
                )
                if attempt < len(self.retry_delays):
                    await asyncio.sleep(self.retry_delays[attempt])
        logger.error("Webhook 最终发送失败：%s（%s）", alert.get("title"), last_error)
        return False

    # ------------------------------------------------------------------ #
    # 测试 / 状态
    # ------------------------------------------------------------------ #
    async def send_test(self) -> Dict[str, Any]:
        """立即发送一条测试消息（供 POST /api/notify/test）。"""
        if not self.enabled or self._client is None:
            return {
                "ok": False,
                "sent": False,
                "error": "Webhook 未启用（需设置 NOTIFY_ENABLED=true 且配置 NOTIFY_WEBHOOK_URL）",
            }
        payload = self._payload_for_alert(
            {
                "id": None,
                "title": "【测试】Campus Twin 告警通知",
                "severity": "test",
                "device": "campus-twin",
                "observed_at": time.time(),
                "detail": "这是一条来自 /api/notify/test 的测试通知",
            }
        )
        self.last_payload = payload
        try:
            await self._post(payload)
            self.sent_count += 1
            self.last_sent_at = time.time()
            self.last_error = None
            return {"ok": True, "sent": True, "error": None}
        except Exception as exc:  # noqa: BLE001
            self.failed_count += 1
            self.last_error = "{}: {}".format(type(exc).__name__, exc)
            return {"ok": False, "sent": False, "error": self.last_error}

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "configured": bool(self.webhook_url),
            "type": self.webhook_type,
            "min_severity": self.min_severity,
            "webhook_url": _mask_url(self.webhook_url),
            "dedup_seconds": self.dedup_seconds,
            "retry_delays": list(self.retry_delays),
            "last_sent_at": _iso(self.last_sent_at),
            "last_sent_at_epoch": self.last_sent_at,
            "last_error": self.last_error,
            "sent_count": self.sent_count,
            "failed_count": self.failed_count,
            "pending": (self._queue.qsize() if self._queue is not None else 0),
        }


def _mask_url(url: str) -> str:
    """Webhook URL 可能含 token：状态接口只显示 host/path 前缀。"""
    if not url:
        return ""
    if len(url) <= 40:
        return url
    return url[:40] + "..."
