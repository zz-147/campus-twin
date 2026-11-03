# -*- coding: utf-8 -*-
"""W10/W15 Webhook 告警通知（后台发送、失败重试、去重、不阻塞采集）。

功能演进
========
- **W10**：``generic`` / ``dingtalk`` / ``feishu`` / ``wecom`` 四种 Webhook；
  新 active 告警 severity 达标才推送；同 ``dedup_key`` 5 分钟去重；
  失败重试 2 次（1s/3s 退避）；记录 last_sent_at / last_error / sent_count；
- **W15 本版**：
  1. **飞书 interactive 富卡片**：header 颜色按 severity 映射
     （critical=red / warning=orange / info=blue / test=green），字段区展示
     级别/设备/规则/时间/详情，固定备注行“Campus Twin 数字孪生运维平台 ·
     仅通知，请以平台数据为准”，可选 ``NOTIFY_LINK_URL`` 按钮「打开 3D 大屏」；
  2. **恢复通知**：``NOTIFY_ON_RECOVERY``（默认 true）时，pipeline 把告警
     cleared 事件投递为**绿色恢复卡片**（设备/恢复时间/恢复原因 clear_reason），
     走同一套去重/重试机制；单轮恢复过多时自动合并成一张“批量恢复”卡片；
  3. **运行时配置**：``apply_config(...)`` 在不重启后端的情况下切换
     enabled / webhook_url / webhook_type / min_severity / notify_on_recovery
     （必要时重建 httpx client 与后台 worker）；``status()`` 增加
     ``on_recovery`` / ``last_payload_kind``（alert/recovery/test）等字段。
- **回归约束**：``generic`` / ``dingtalk`` / ``wecom`` 的告警 payload 结构保持
  W10 原样（字段名、markdown 文本格式不变），仅 feishu 从纯文本升级为卡片。

契约（docs/W10-路径追踪告警聚合设计.md §三 + docs/飞书接入说明.md）
==================================================================
- 环境变量：``NOTIFY_ENABLED`` / ``NOTIFY_WEBHOOK_URL`` /
  ``NOTIFY_WEBHOOK_TYPE=generic|dingtalk|feishu|wecom`` / ``NOTIFY_MIN_SEVERITY`` /
  ``NOTIFY_ON_RECOVERY`` / ``NOTIFY_LINK_URL``；
- pipeline 每轮**新产生的 active 告警**里 severity 达标的投递到后台队列发送；
  cleared/info 不按告警格式发送（cleared 走恢复通知通道）；
- 同一 ``dedup_key`` 默认 5 分钟内只发一次；恢复通知用 ``recovery:<dedup_key>``
  独立命名空间去重（故障告警与恢复通知互不吞并）；
- 队列 + worker 在 lifespan 中启动/停止，绝不阻塞 5s 采集循环；
- ``GET /api/notify/status`` / ``POST /api/notify/test``（接口在 main.py；运行时
  配置接口由 Lead 按本文件 ``apply_config`` 契约统一添加）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

import httpx

from app.config import Settings
from app.models import Alert

logger = logging.getLogger("campus_twin.notify")

#: severity -> 比较权重（数值越大越严重）；test 仅用于测试消息
SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1, "test": 0}

#: 支持的 Webhook 类型
WEBHOOK_TYPES = ("generic", "dingtalk", "feishu", "wecom")

#: 飞书卡片 header 颜色（按 severity）
FEISHU_SEVERITY_TEMPLATE = {
    "critical": "red",
    "warning": "orange",
    "info": "blue",
    "test": "green",
}
#: 恢复卡片固定绿色
FEISHU_RECOVERY_TEMPLATE = "green"

#: payload 种类（status().last_payload_kind 的取值）
PAYLOAD_ALERT = "alert"
PAYLOAD_RECOVERY = "recovery"
PAYLOAD_TEST = "test"

#: 卡片备注行（固定话术：只通知、不替代平台数据）
FEISHU_NOTE = "Campus Twin 数字孪生运维平台 · 仅通知，请以平台数据为准"
#: 可选按钮文案（NOTIFY_LINK_URL 配置后出现）
FEISHU_LINK_BUTTON = "打开 3D 大屏"

#: 单轮恢复事件超过该数量时合并成一张“批量恢复”卡片，避免集中恢复刷屏
MAX_RECOVERY_CARDS_PER_BATCH = 5

BEIJING_TZ = timezone(timedelta(hours=8))


class NotifyConfigError(ValueError):
    """运行时配置非法（API 层应映射为 HTTP 400）。"""


def _iso(ts: Optional[float]) -> Optional[str]:
    """epoch -> UTC ISO8601（generic/dingtalk/wecom 沿用 W10 时间格式）。"""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _beijing_time(ts: Optional[float]) -> str:
    """epoch -> 北京时间字符串（飞书卡片展示用；PT 仿真时钟与宿主机差约 10 分钟，
    告警一律使用宿主机 ``observed_at``，不用 NC 的 ``source_ts``）。"""
    if not ts:
        return "-"
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return "-"
    return datetime.fromtimestamp(value, tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _as_dict(alert: Any) -> Dict[str, Any]:
    """把 Alert / DB row(dict) 统一成 dict（兼容 pipeline 传来的两种形态）。"""
    if isinstance(alert, dict):
        return dict(alert)
    dump = getattr(alert, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return dict(getattr(alert, "__dict__", {}) or {})


def _mask_url(url: str) -> str:
    """Webhook URL 可能含 token：状态接口只显示 host/path 前缀，避免泄露密钥。"""
    if not url:
        return ""
    if len(url) <= 40:
        return url
    return url[:40] + "..."


def _first_present(*values: Any) -> Any:
    """返回第一个“非空”的值（0/False 视为有效，与 or 链区分）。"""
    for value in values:
        if value is None or value == "":
            continue
        return value
    return None


class NotifyService:
    """Webhook 通知服务（单例，由 app lifespan 启动/停止）。"""

    def __init__(
        self,
        settings: Settings,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.settings = settings
        self._transport = transport

        # ---- 环境变量初始配置（apply_config 运行时覆盖；此处保留快照供 UI 恢复默认）----
        self._default_enabled_requested = bool(settings.notify_enabled)
        self._default_webhook_url = str(settings.notify_webhook_url or "").strip()
        default_type = str(settings.notify_webhook_type or "generic").strip().lower()
        if default_type not in WEBHOOK_TYPES:  # 防御：Settings 被手工构造时也不能崩
            logger.warning("未知 NOTIFY_WEBHOOK_TYPE=%r，回退 generic", default_type)
            default_type = "generic"
        self._default_webhook_type = default_type
        self._default_min_severity = str(settings.notify_min_severity or "critical").lower()
        if self._default_min_severity not in SEVERITY_RANK:
            self._default_min_severity = "critical"
        self._default_notify_on_recovery = bool(settings.notify_on_recovery)
        self._default_link_url = str(getattr(settings, "notify_link_url", "") or "").strip()

        # ---- 当前生效配置 ----
        self.enabled_requested = self._default_enabled_requested
        self.webhook_url = self._default_webhook_url
        self.webhook_type = self._default_webhook_type
        self.min_severity = self._default_min_severity
        self.notify_on_recovery = self._default_notify_on_recovery
        self.link_url = self._default_link_url
        self.config_source = "env"
        # enabled 是“请求开启 + 已配置 URL”的合取（UI 只开开关没填 URL 时不发）
        self.enabled = bool(self.enabled_requested and self.webhook_url)

        self.dedup_seconds = float(settings.notify_dedup_seconds)
        self.timeout = float(settings.notify_timeout)
        self.retry_delays = tuple(float(delay) for delay in settings.notify_retry_delays)

        self._client: Optional[httpx.AsyncClient] = None
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        # 同 dedup_key 上次发送时间（告警用原始 dedup_key，恢复通知用 recovery:<key>）
        self._last_sent_keys: Dict[str, float] = {}
        self.last_sent_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.sent_count: int = 0
        self.failed_count: int = 0
        self.last_payload: Optional[Dict[str, Any]] = None
        #: 最近一次构造的 payload 种类：alert / recovery / test（W15）
        self.last_payload_kind: Optional[str] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if not self.enabled:
            logger.info(
                "Webhook 通知未启用（NOTIFY_ENABLED=%s / URL 已配置=%s）",
                self.enabled_requested,
                bool(self.webhook_url),
            )
            return
        if self._worker is not None:  # 幂等：重复 start 不重建
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 2.0)),
            trust_env=False,  # localhost 接收器不能走系统代理
            transport=self._transport,
        )
        self._queue = asyncio.Queue(maxsize=200)
        self._worker = asyncio.create_task(self._worker_loop(), name="campus-twin-notify")
        logger.info(
            "Webhook 通知已启用：type=%s url=%s min_severity=%s on_recovery=%s link=%s",
            self.webhook_type,
            _mask_url(self.webhook_url),
            self.min_severity,
            self.notify_on_recovery,
            bool(self.link_url),
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

    async def restart(self) -> None:
        """按当前内存配置重建 client/worker（apply_config 内部使用）。"""
        await self.stop()
        await self.start()

    # ------------------------------------------------------------------ #
    # 运行时配置（供后续 API/UI 调用，不重启后端）
    # ------------------------------------------------------------------ #
    async def apply_config(
        self,
        enabled: Optional[bool] = None,
        webhook_url: Optional[str] = None,
        webhook_type: Optional[str] = None,
        min_severity: Optional[str] = None,
        notify_on_recovery: Optional[bool] = None,
        link_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """运行时切换通知配置；返回最新的 ``status()``。

        - 参数为 ``None`` 表示“保持当前值”，支持 UI 只提交修改项；
        - ``webhook_type`` 校验失败 / ``min_severity`` 非法时抛
          :class:`NotifyConfigError`（API 层映射 HTTP 400），**不会**改动任何状态；
        - 连接类配置（enabled / url / type）变化时重建 httpx client 与后台 worker，
          让新配置立即生效且无需重启后端；
        - 环境变量初始配置始终保留在 ``status().env_defaults``，可据此恢复默认。
        """
        # ---- 1) 先全部校验，校验失败不改状态（避免半配置）----
        new_type = self.webhook_type
        if webhook_type is not None:
            candidate = str(webhook_type).strip().lower()
            if candidate not in WEBHOOK_TYPES:
                raise NotifyConfigError(
                    "NOTIFY_WEBHOOK_TYPE 必须是 {} 之一，收到：{!r}".format(
                        "|".join(WEBHOOK_TYPES), webhook_type
                    )
                )
            new_type = candidate
        new_min_severity = self.min_severity
        if min_severity is not None:
            candidate = str(min_severity).strip().lower()
            if candidate not in SEVERITY_RANK:
                raise NotifyConfigError(
                    "NOTIFY_MIN_SEVERITY 必须是 critical|warning|info 之一，收到：{!r}".format(
                        min_severity
                    )
                )
            new_min_severity = candidate
        new_url = self.webhook_url if webhook_url is None else str(webhook_url or "").strip()
        new_enabled_requested = (
            self.enabled_requested if enabled is None else bool(enabled)
        )
        new_on_recovery = (
            self.notify_on_recovery
            if notify_on_recovery is None
            else bool(notify_on_recovery)
        )
        new_link_url = self.link_url if link_url is None else str(link_url or "").strip()

        connection_changed = (
            new_url != self.webhook_url
            or new_type != self.webhook_type
            or new_enabled_requested != self.enabled_requested
        )
        any_changed = connection_changed or (
            new_min_severity != self.min_severity
            or new_on_recovery != self.notify_on_recovery
            or new_link_url != self.link_url
        )

        # ---- 2) 连接类变化：停掉旧 worker/client，避免旧配置继续发消息 ----
        if connection_changed and (self._worker is not None or self._client is not None):
            await self.stop()

        self.enabled_requested = new_enabled_requested
        self.webhook_url = new_url
        self.webhook_type = new_type
        self.min_severity = new_min_severity
        self.notify_on_recovery = new_on_recovery
        self.link_url = new_link_url
        self.enabled = bool(self.enabled_requested and self.webhook_url)
        if any_changed:
            self.config_source = "runtime"

        # ---- 3) 需要时重建 client + worker（enabled 为真才会真正启动）----
        if connection_changed or (self.enabled and self._worker is None):
            await self.start()

        logger.info(
            "运行时通知配置已更新：enabled=%s type=%s url=%s min_severity=%s "
            "on_recovery=%s link=%s",
            self.enabled,
            self.webhook_type,
            _mask_url(self.webhook_url),
            self.min_severity,
            self.notify_on_recovery,
            bool(self.link_url),
        )
        return self.status()

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
        if not self._dedup_allow(dedup_key):
            return False
        return self._put({"kind": PAYLOAD_ALERT, "data": alert.model_dump(mode="json")}, dedup_key)

    def enqueue_recovery(self, alert: Any) -> bool:
        """把一条告警恢复事件投递为恢复通知；返回是否真的入队。

        ``alert`` 可以是 ``Alert`` 对象、``ClearedEvent`` 转成的 dict，或
        ``store.resolve_alert()`` 返回的 DB row（含 ``clear_reason`` / ``resolved_at``）。
        """
        if not self.enabled or self._queue is None:
            return False
        if not self.notify_on_recovery:
            logger.debug("NOTIFY_ON_RECOVERY=false，跳过恢复通知")
            return False
        data = _as_dict(alert)
        if data.get("deleted_at"):
            return False
        dedup_key = "recovery:{}".format(
            _first_present(data.get("dedup_key"), data.get("id"), data.get("title"), "unknown")
        )
        if not self._dedup_allow(dedup_key):
            return False
        return self._put({"kind": PAYLOAD_RECOVERY, "data": data}, dedup_key)

    def notify_recoveries(self, alerts: Sequence[Any]) -> int:
        """批量投递本轮恢复事件；单轮过多时合并为一张“批量恢复”卡片。

        返回成功入队的卡片数。集中恢复（如依赖了 3 分钟的 NC issue 一次性清除）
        不应把群刷屏，因此超过 :data:`MAX_RECOVERY_CARDS_PER_BATCH` 条时聚合。
        """
        rows = [_as_dict(item) for item in alerts]
        rows = [row for row in rows if not row.get("deleted_at")]
        if not rows:
            return 0
        if len(rows) <= MAX_RECOVERY_CARDS_PER_BATCH:
            return sum(1 for row in rows if self.enqueue_recovery(row))
        aggregate = self._aggregate_recovery(rows)
        return 1 if self.enqueue_recovery(aggregate) else 0

    @staticmethod
    def _aggregate_recovery(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """把一轮集中恢复聚合成一张批量恢复卡片的数据。"""
        count = len(rows)
        types: Dict[str, int] = {}
        for row in rows:
            key = str(row.get("alert_type") or "unknown")
            types[key] = types.get(key, 0) + 1
        type_text = "、".join("{}×{}".format(name, num) for name, num in sorted(types.items()))
        stale_count = sum(1 for row in rows if row.get("stale_source"))
        titles = "、".join(
            str(_first_present(row.get("recovery_title"), row.get("title"), ""))
            for row in rows[:5]
        )
        reason = "本轮共 {} 条告警恢复（{}）".format(count, type_text)
        if stale_count:
            reason += "；其中 {} 条为 NC 记录滞后、按数据源实际状态判定恢复".format(
                stale_count
            )
        resolved_at = max(
            float(row.get("resolved_at") or row.get("observed_at") or time.time())
            for row in rows
        )
        return {
            "id": None,
            "alert_type": "batch_recovery",
            "severity": "info",
            "title": "批量恢复：本轮 {} 条告警已恢复".format(count),
            "detail": "{}\n示例：{}".format(reason, titles),
            "clear_reason": reason,
            "device_id": "{} 个告警对象".format(count),
            "dedup_key": "batch_recovery",
            "resolved_at": resolved_at,
            "observed_at": resolved_at,
            "stale_source": stale_count > 0,
            "state": "cleared",
        }

    # ------------------------------------------------------------------ #
    # 去重 / 入队辅助
    # ------------------------------------------------------------------ #
    def _dedup_allow(self, dedup_key: str) -> bool:
        now = time.time()
        last_sent = self._last_sent_keys.get(dedup_key)
        if last_sent is not None and now - last_sent < self.dedup_seconds:
            logger.debug(
                "Webhook 去重命中：%s（%.0fs 内已发送）", dedup_key, self.dedup_seconds
            )
            return False
        return True

    def _put(self, item: Dict[str, Any], dedup_key: str) -> bool:
        """真正入队并记录去重时间；队列满时丢弃并返回 False。"""
        assert self._queue is not None
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("Webhook 队列已满，丢弃消息：%s", dedup_key)
            return False
        self._last_sent_keys[dedup_key] = time.time()
        # 防止 _last_sent_keys 无限增长：超过 500 条时清理最旧的 100 条
        if len(self._last_sent_keys) > 500:
            oldest = sorted(self._last_sent_keys.items(), key=lambda pair: pair[1])[:100]
            for key, _ in oldest:
                self._last_sent_keys.pop(key, None)
        return True

    async def _worker_loop(self) -> None:
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            try:
                await self._deliver_with_retries(item)
            except Exception:  # noqa: BLE001 - worker 不能因单条失败退出
                logger.exception("Webhook 通知后台任务异常")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ #
    # payload 构造
    # ------------------------------------------------------------------ #
    @staticmethod
    def _alert_view(alert: Dict[str, Any]) -> Dict[str, Any]:
        """抽取告警展示字段（保持 W10 generic/dingtalk/wecom 行为不变）。"""
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
        return {
            "title": title,
            "severity": severity,
            "device": device,
            "time_text": time_text,
            "beijing_time": _beijing_time(observed_at),
            "detail": str(alert.get("detail") or ""),
            "rule": str(_first_present(alert.get("alert_type"), alert.get("rule"), "-")),
            "alert_id": alert.get("id"),
        }

    @staticmethod
    def _recovery_view(alert: Dict[str, Any]) -> Dict[str, Any]:
        """抽取恢复事件展示字段（时间优先用 resolved_at=恢复时间）。

        优先使用 pipeline 附加的 ``recovery_title``（来自 ClearedEvent，如
        “主机恢复：PC-JX1”），DB row 自身的 title 是故障标题（“主机离线：PC-JX1”）。
        """
        title = str(
            _first_present(alert.get("recovery_title"), alert.get("title"), "告警已恢复")
        )
        resolved_at = _first_present(alert.get("resolved_at"), alert.get("observed_at"))
        detail = str(
            _first_present(alert.get("recovery_detail"), alert.get("detail"), "")
        )
        clear_reason = str(
            _first_present(alert.get("clear_reason"), detail, "数据源状态已恢复")
        )
        return {
            "title": title,
            "severity": "recovered",
            "device": str(
                _first_present(
                    alert.get("recovery_device"),
                    alert.get("device_id"),
                    alert.get("device_name"),
                    "未知设备",
                )
            ),
            "rule": str(_first_present(alert.get("alert_type"), alert.get("rule"), "-")),
            "time_text": _iso(float(resolved_at)) if resolved_at else _iso(time.time()),
            "beijing_time": _beijing_time(resolved_at),
            "detail": detail,
            "clear_reason": clear_reason,
            "alert_id": alert.get("id"),
            "stale_source": bool(alert.get("stale_source")),
        }

    def _payload_for_alert(self, alert: Dict[str, Any], kind: str = PAYLOAD_ALERT) -> Dict[str, Any]:
        """按 ``webhook_type`` 构造告警 payload（feishu 为 interactive 卡片）。"""
        view = self._alert_view(alert)
        if self.webhook_type == "feishu":
            return self._feishu_card(
                kind=PAYLOAD_TEST if kind == PAYLOAD_TEST else PAYLOAD_ALERT,
                title=view["title"],
                severity=view["severity"],
                device=str(view["device"]),
                rule=view["rule"],
                time_text=view["beijing_time"],
                detail=view["detail"],
            )
        if self.webhook_type == "dingtalk":
            return {
                "msgtype": "markdown",
                "markdown": {
                    "title": view["title"],
                    "text": self._markdown_text(
                        view["title"],
                        view["severity"],
                        view["device"],
                        view["time_text"],
                        view["detail"],
                    ),
                },
            }
        if self.webhook_type == "wecom":
            return {
                "msgtype": "markdown",
                "markdown": {
                    "content": self._markdown_text(
                        view["title"],
                        view["severity"],
                        view["device"],
                        view["time_text"],
                        view["detail"],
                    )
                },
            }
        return {
            "title": view["title"],
            "severity": view["severity"],
            "device": view["device"],
            "time": view["time_text"],
            "detail": view["detail"],
            "alert_id": view["alert_id"],
        }

    def _payload_for_recovery(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """按 ``webhook_type`` 构造恢复通知 payload（feishu 为绿色恢复卡片）。"""
        view = self._recovery_view(alert)
        if self.webhook_type == "feishu":
            return self._feishu_card(
                kind=PAYLOAD_RECOVERY,
                title=view["title"],
                severity="info",
                device=str(view["device"]),
                rule=view["rule"],
                time_text=view["beijing_time"],
                detail=view["clear_reason"],
                stale_source=view["stale_source"],
            )
        if self.webhook_type == "dingtalk":
            return {
                "msgtype": "markdown",
                "markdown": {
                    "title": "【恢复】{}".format(view["title"]),
                    "text": self._recovery_markdown(view),
                },
            }
        if self.webhook_type == "wecom":
            return {
                "msgtype": "markdown",
                "markdown": {"content": self._recovery_markdown(view)},
            }
        return {
            "kind": PAYLOAD_RECOVERY,
            "title": view["title"],
            "severity": view["severity"],
            "device": view["device"],
            "rule": view["rule"],
            "time": view["time_text"],
            "detail": view["detail"],
            "clear_reason": view["clear_reason"],
            "alert_id": view["alert_id"],
            "stale_source": view["stale_source"],
            "state": "cleared",
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

    @staticmethod
    def _recovery_markdown(view: Dict[str, Any]) -> str:
        return (
            "### 【恢复】{}\n"
            "- **级别**: 已恢复\n"
            "- **设备**: {}\n"
            "- **规则**: {}\n"
            "- **恢复时间**: {}\n"
            "- **恢复原因**: {}\n"
            "> {}".format(
                view["title"],
                view["device"],
                view["rule"],
                view["time_text"] or "-",
                view["clear_reason"] or "-",
                FEISHU_NOTE,
            )
        )

    # ---- 飞书 interactive 卡片 ---------------------------------------- #
    @staticmethod
    def _header_title(kind: str, title: str) -> str:
        """卡片 header 标题：告警【告警】/ 恢复【恢复】/ 测试【测试】。"""
        if kind == PAYLOAD_RECOVERY:
            return "【恢复】{}".format(title)
        if kind == PAYLOAD_TEST:
            if title.startswith("【测试】"):
                return title
            return "【测试】{}".format(title)
        return "【告警】{}".format(title)

    @staticmethod
    def _card_fields(
        kind: str, severity: str, device: str, rule: str, time_text: str, detail: str
    ) -> List[Dict[str, Any]]:
        """卡片字段区（告警：级别/设备/规则/时间/详情；恢复：…恢复原因）。"""
        if kind == PAYLOAD_RECOVERY:
            pairs = [
                ("级别", "已恢复"),
                ("设备", device),
                ("规则", rule),
                ("恢复时间", time_text),
                ("恢复原因", detail),
            ]
        else:
            pairs = [
                ("级别", severity or "-"),
                ("设备", device),
                ("规则", rule),
                ("时间", time_text),
                ("详情", detail),
            ]
        fields: List[Dict[str, Any]] = []
        for index, (label, value) in enumerate(pairs):
            content = str(value if value not in (None, "") else "-")
            fields.append(
                {
                    # 前 4 项短字段两列排版；最后一项（详情/恢复原因）独占整行
                    "is_short": index < 4,
                    "text": {"tag": "lark_md", "content": "**{}**\n{}".format(label, content)},
                }
            )
        return fields

    def _feishu_card(
        self,
        *,
        kind: str,
        title: str,
        severity: str,
        device: str,
        rule: str,
        time_text: str,
        detail: str,
        stale_source: bool = False,
    ) -> Dict[str, Any]:
        """构造飞书 interactive 卡片封装（webhook 自定义机器人消息体）。"""
        if kind == PAYLOAD_RECOVERY:
            template = FEISHU_RECOVERY_TEMPLATE
        else:
            template = FEISHU_SEVERITY_TEMPLATE.get(
                str(severity or "").lower(), "blue"
            )
        elements: List[Dict[str, Any]] = [
            {"tag": "div", "fields": self._card_fields(kind, severity, device, rule, time_text, detail)}
        ]
        if kind == PAYLOAD_RECOVERY and stale_source:
            elements.append(
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "NC 问题记录滞后，已按数据源实际状态判定恢复",
                        }
                    ],
                }
            )
        elements.append({"tag": "hr"})
        elements.append(
            {"tag": "note", "elements": [{"tag": "plain_text", "content": FEISHU_NOTE}]}
        )
        if self.link_url:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": FEISHU_LINK_BUTTON},
                            "type": "primary",
                            "url": self.link_url,
                        }
                    ],
                }
            )
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": self._header_title(kind, title),
                    },
                    "template": template,
                },
                "elements": elements,
            },
        }

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    async def _post(self, payload: Dict[str, Any]) -> None:
        assert self._client is not None
        response = await self._client.post(self.webhook_url, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(
                "Webhook 返回 HTTP {}: {}".format(
                    response.status_code, response.text[:200]
                )
            )
        # 飞书/钉钉/企微在 HTTP 200 时业务码仍可能失败（如飞书 19021 签名错误），
        # 必须解析 body 才能如实反馈；generic 自定义接收器不假设响应结构（保持 W10 行为）。
        if self.webhook_type in ("feishu", "dingtalk", "wecom"):
            code = _response_code(response)
            if code not in (None, 0):
                raise RuntimeError(
                    "Webhook 业务码错误 code={} msg={}".format(
                        code, _response_message(response)
                    )
                )

    def _payload_for_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(item.get("kind") or PAYLOAD_ALERT)
        data = item.get("data") or {}
        if kind == PAYLOAD_RECOVERY:
            return self._payload_for_recovery(data)
        return self._payload_for_alert(data, kind=kind)

    async def _deliver_with_retries(self, item: Dict[str, Any]) -> bool:
        payload = self._payload_for_item(item)
        kind = str(item.get("kind") or PAYLOAD_ALERT)
        self.last_payload = payload
        self.last_payload_kind = kind
        attempts = 1 + len(self.retry_delays)  # 初次 + 2 次重试
        last_error: Optional[str] = None
        label = str((item.get("data") or {}).get("title") or kind)
        for attempt in range(attempts):
            try:
                await self._post(payload)
                self.sent_count += 1
                self.last_sent_at = time.time()
                self.last_error = None
                logger.info(
                    "Webhook 已发送（kind=%s）：%s（第 %d 次尝试）",
                    kind,
                    label,
                    attempt + 1,
                )
                return True
            except Exception as exc:  # noqa: BLE001 - 失败重试，不抛出到采集链路
                last_error = "{}: {}".format(type(exc).__name__, exc)
                self.failed_count += 1
                self.last_error = last_error
                logger.warning(
                    "Webhook 发送失败（kind=%s，%d/%d）：%s",
                    kind,
                    attempt + 1,
                    attempts,
                    last_error,
                )
                if attempt < len(self.retry_delays):
                    await asyncio.sleep(self.retry_delays[attempt])
        logger.error("Webhook 最终发送失败（kind=%s）：%s（%s）", kind, label, last_error)
        return False

    # ------------------------------------------------------------------ #
    # 测试 / 状态
    # ------------------------------------------------------------------ #
    async def send_test(self, kind: str = PAYLOAD_TEST) -> Dict[str, Any]:
        """立即发送一条测试消息（供 POST /api/notify/test）。

        ``kind="test"`` 发告警测试卡片（绿色 header）；``kind="recovery"`` 发恢复
        测试卡片（绿色 header，演示 recovery 样式），便于前端做“发送测试通知”。
        """
        if not self.enabled or self._client is None:
            return {
                "ok": False,
                "sent": False,
                "kind": kind,
                "error": "Webhook 未启用（需设置 NOTIFY_ENABLED=true 且配置 NOTIFY_WEBHOOK_URL）",
            }
        if str(kind).lower() == PAYLOAD_RECOVERY:
            payload = self._payload_for_recovery(
                {
                    "id": None,
                    "alert_type": "notify_test",
                    "title": "测试告警已恢复",
                    "device_id": "campus-twin",
                    "resolved_at": time.time(),
                    "observed_at": time.time(),
                    "clear_reason": "这是一条来自 /api/notify/test 的恢复测试通知",
                    "detail": "这是一条来自 /api/notify/test 的恢复测试通知",
                    "stale_source": False,
                }
            )
            payload_kind = PAYLOAD_RECOVERY
        else:
            payload = self._payload_for_alert(
                {
                    "id": None,
                    "alert_type": "notify_test",
                    "title": "【测试】Campus Twin 告警通知",
                    "severity": "test",
                    "device": "campus-twin",
                    "observed_at": time.time(),
                    "detail": "这是一条来自 /api/notify/test 的测试通知",
                },
                kind=PAYLOAD_TEST,
            )
            payload_kind = PAYLOAD_TEST
        self.last_payload = payload
        self.last_payload_kind = payload_kind
        try:
            await self._post(payload)
            self.sent_count += 1
            self.last_sent_at = time.time()
            self.last_error = None
            return {"ok": True, "sent": True, "kind": payload_kind, "error": None}
        except Exception as exc:  # noqa: BLE001
            self.failed_count += 1
            self.last_error = "{}: {}".format(type(exc).__name__, exc)
            return {"ok": False, "sent": False, "kind": payload_kind, "error": self.last_error}

    def env_defaults(self) -> Dict[str, Any]:
        """环境变量初始配置快照（运行时覆盖不修改它，供 UI「恢复默认」）。"""
        return {
            "enabled": self._default_enabled_requested,
            "webhook_url": _mask_url(self._default_webhook_url),
            "webhook_type": self._default_webhook_type,
            "min_severity": self._default_min_severity,
            "notify_on_recovery": self._default_notify_on_recovery,
            "link_url": self._default_link_url,
        }

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "enabled_requested": bool(self.enabled_requested),
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
            # ---- W15：恢复通知 / 卡片 / 运行时配置 ----
            "on_recovery": bool(self.notify_on_recovery),
            "last_payload_kind": self.last_payload_kind,
            "link_url": self.link_url,
            "link_button": bool(self.link_url),
            "card_type": "feishu_interactive" if self.webhook_type == "feishu" else None,
            "config_source": self.config_source,
            "available_types": list(WEBHOOK_TYPES),
            "env_defaults": self.env_defaults(),
            "max_recovery_cards_per_batch": MAX_RECOVERY_CARDS_PER_BATCH,
        }


# --------------------------------------------------------------------------- #
# Webhook 响应解析（飞书/钉钉/企微 HTTP 200 时业务码仍可能失败）
# --------------------------------------------------------------------------- #
def _response_json(response: httpx.Response) -> Optional[Dict[str, Any]]:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - 非 JSON 响应（如本地接收器返回 {}）不算错误
        return None
    return payload if isinstance(payload, dict) else None


def _response_code(response: httpx.Response) -> Optional[int]:
    """提取业务码：飞书 ``code`` / 钉钉 ``errcode`` / 企微 ``errcode``。"""
    payload = _response_json(response)
    if not payload:
        return None
    for key in ("code", "errcode", "StatusCode", "status_code"):
        if key in payload:
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                return None
    return None


def _response_message(response: httpx.Response) -> str:
    payload = _response_json(response) or {}
    for key in ("msg", "errmsg", "message", "StatusMessage"):
        if payload.get(key):
            return str(payload[key])[:200]
    return response.text[:200]
