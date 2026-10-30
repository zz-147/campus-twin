# -*- coding: utf-8 -*-
"""Packet Tracer Network Controller (NC) 采集驱动。

数据来源：PT 9.x NC 的 Real World Access，即 Cisco DNA Center 风格的 REST API
（默认 ``http://127.0.0.1:58000/api/v1``，仅 localhost 可访问）。

关键设计
========
1. **ticket 认证 + 自动续期**
   ``POST /ticket`` 换 ``serviceTicket``，后续请求带 ``x-auth-token``。
   NC 返回 ``idleTimeout``（默认 900s）与 ``sessionTimeout``（默认 3600s），
   这里在 idle 的 80% 或 session 到期前主动重登；遇到 401/403 也会自动重登并
   重试一次，避免长跑采集过程中 token 过期导致数据中断。
2. **优雅降级，绝不阻塞服务**
   NC 可能没开（PT 未启动/Real World Access 未启用）。连接失败时驱动：
   - 记录一次 warning 日志（之后按 60s 节流，避免刷屏）；
   - 指数退避（15s 起步、上限 120s）后再尝试，不会 5s 一次空转重试；
   - 本轮返回空数据集（``CollectResult.unavailable``），pipeline 继续跑。
   单端点 4xx/5xx 只影响该端点（记入 ``errors``），不影响其它数据。
3. **空响应容错**
   0 设备时 NC 对很多端点返回 ``{"version": "1.0"}``（没有 response 字段），
   统一在 models.extract_list/from_netcon 里变成空列表。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.collectors.base import CollectResult
from app.config import Settings
from app.models import (
    Device,
    HealthIssue,
    Host,
    NetworkHealth,
    Topology,
    extract_list,
)

logger = logging.getLogger("campus_twin.netcon")


def _text_or_none(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)


def _preview_dict(doc: Any, limit: int = 300) -> str:
    try:
        import json as _json

        text = _json.dumps(doc, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(doc)
    return text[:limit]


class NetconError(Exception):
    """NC 采集异常基类。"""


class NetconUnavailable(NetconError):
    """整个数据源不可用（连不上 / 认证失败 / 退避中）。"""


class NetconEndpointError(NetconError):
    """单个端点的错误（4xx/5xx/非法 JSON），不影响其它端点。"""


class NetconDriver:
    """NC REST 采集驱动。"""

    name = "netcon"

    # token 提前续期余量（秒）：避免卡在边界上刚好过期
    TICKET_REFRESH_MARGIN = 120.0
    # 失败日志节流间隔（秒）
    FAILURE_LOG_INTERVAL = 60.0
    # 防缓存响应头：PT NC 作为内嵌 HTTP 服务可能缓存 /host 响应（故障注入实验
    # 中后端 ping_status 曾滞后于 NC 实际值），这里显式要求每轮重新计算。
    NO_CACHE_HEADERS = {
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
    }

    def __init__(
        self,
        settings: Settings,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.settings = settings
        self._base = settings.nc_base_url.rstrip("/")
        timeout = httpx.Timeout(
            timeout=settings.nc_timeout,
            connect=min(settings.nc_timeout, 3.0),
        )
        # trust_env=False：本机可能有 HTTP_PROXY 环境变量，localhost 请求不能走代理。
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept": "application/json"},
            trust_env=False,
            transport=transport,
        )
        self._ticket: Optional[str] = None
        self._ticket_at: float = 0.0
        self._idle_timeout: float = 900.0
        self._session_timeout: float = 3600.0

        # 失败退避状态
        self._failures: int = 0
        self._next_attempt_at: float = 0.0
        self._last_error: Optional[str] = None
        self._last_failure_log_at: float = 0.0

        # 供 /api/health 观测
        self.nc_reachable: bool = False
        self.last_ok_at: Optional[float] = None
        self.login_count: int = 0
        self.endpoint_errors: Dict[str, str] = {}
        # host 新鲜度可观测性（W3-A）：每次成功采集 /host 都刷新
        self.hosts_observed_at: Optional[float] = None
        self.host_fetch_count: int = 0
        self.hosts_last_count: int = 0

    # ------------------------------------------------------------------ #
    # 认证
    # ------------------------------------------------------------------ #
    def _ticket_valid(self) -> bool:
        if not self._ticket:
            return False
        age = time.time() - self._ticket_at
        idle_budget = max(self._idle_timeout - self.TICKET_REFRESH_MARGIN, 60.0)
        session_budget = max(self._session_timeout - self.TICKET_REFRESH_MARGIN, 60.0)
        return age < min(idle_budget, session_budget)

    async def _login(self) -> bool:
        """获取 serviceTicket；失败时记录退避状态并返回 False（不抛异常）。"""
        url = self._url("ticket")
        try:
            response = await self._client.post(
                url, json={"username": self.settings.nc_username, "password": self.settings.nc_password}
            )
        except httpx.HTTPError as exc:
            self._record_failure("连接 NC 失败（POST /ticket）：{}".format(exc))
            return False

        if response.status_code not in (200, 201):
            self._record_failure(
                "认证失败（POST /ticket -> HTTP {}）：{}".format(
                    response.status_code, self._body_preview(response)
                )
            )
            return False

        try:
            doc = response.json()
        except ValueError:
            self._record_failure("认证响应不是合法 JSON：{}".format(self._body_preview(response)))
            return False

        payload = doc.get("response") if isinstance(doc, dict) else None
        ticket = payload.get("serviceTicket") if isinstance(payload, dict) else None
        if not ticket:
            self._record_failure(
                "认证响应缺少 serviceTicket：{}".format(self._body_preview(response))
            )
            return False

        self._ticket = str(ticket)
        self._ticket_at = time.time()
        self._idle_timeout = float(payload.get("idleTimeout") or 900.0)
        self._session_timeout = float(payload.get("sessionTimeout") or 3600.0)
        self.login_count += 1
        self._record_success()
        logger.info(
            "已获取 NC ticket  serviceTicket=%s  idleTimeout=%.0fs  sessionTimeout=%.0fs",
            self._ticket,
            self._idle_timeout,
            self._session_timeout,
        )
        return True

    async def _ensure_ticket(self) -> bool:
        if self._ticket and self._ticket_valid():
            return True
        # 处于退避窗口内：直接跳过本轮，不产生新的网络请求。
        if self._failures and time.time() < self._next_attempt_at:
            return False
        return await self._login()

    # ------------------------------------------------------------------ #
    # 失败/恢复状态机
    # ------------------------------------------------------------------ #
    def _record_failure(self, message: str) -> None:
        self._failures += 1
        self._last_error = message
        self.nc_reachable = False
        backoff = min(
            self.settings.nc_retry_interval * (2 ** (self._failures - 1)),
            self.settings.nc_max_retry_interval,
        )
        self._next_attempt_at = time.time() + backoff
        now = time.time()
        # 首次失败立即 warning；持续失败时每 60s 提醒一次，避免日志刷屏。
        if self._failures == 1 or now - self._last_failure_log_at >= self.FAILURE_LOG_INTERVAL:
            self._last_failure_log_at = now
            logger.warning(
                "%s（第 %d 次失败，%.0fs 后重试；NC 离线时服务将以空数据集继续运行）",
                message,
                self._failures,
                backoff,
            )

    def _record_success(self) -> None:
        if self._failures:
            logger.info("NC 连接恢复（此前连续失败 %d 次）", self._failures)
        self._failures = 0
        self._next_attempt_at = 0.0
        self._last_error = None
        self.nc_reachable = True
        self.last_ok_at = time.time()

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _url(self, path: str) -> str:
        return "{}/{}".format(self._base, path.lstrip("/"))

    @staticmethod
    def _body_preview(response: httpx.Response, limit: int = 300) -> str:
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - 预览日志不需要精确
            return "<unreadable>"
        text = " ".join(text.split())
        return text[:limit] + ("..." if len(text) > limit else "")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        retry_auth: bool = True,
    ) -> Dict[str, Any]:
        """发一个带认证的请求，返回解析后的 JSON dict。

        异常约定：
        - :class:`NetconUnavailable`：数据源整体不可用（连接/超时/认证失败）
        - :class:`NetconEndpointError`：单端点错误（4xx/5xx/非 JSON）
        """
        if self._failures and time.time() < self._next_attempt_at:
            raise NetconUnavailable(
                "NC 退避中（{:.0f}s 后重试）：{}".format(
                    self._next_attempt_at - time.time(), self._last_error or "unknown"
                )
            )
        if not await self._ensure_ticket():
            raise NetconUnavailable(self._last_error or "NC 不可用（ticket 获取失败）")

        headers = {"x-auth-token": self._ticket or ""}
        headers.update(self.NO_CACHE_HEADERS)
        try:
            response = await self._client.request(
                method,
                self._url(path),
                json=json_body,
                params=params,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            self._record_failure("请求 NC 端点 /{} 失败：{}".format(path, exc))
            raise NetconUnavailable("请求 /{} 失败：{}".format(path, exc)) from exc

        if response.status_code in (401, 403) and retry_auth:
            logger.info("NC ticket 失效（HTTP %d），重新认证后重试 /%s", response.status_code, path)
            self._ticket = None
            if await self._login():
                return await self._request(
                    method,
                    path,
                    json_body=json_body,
                    params=params,
                    retry_auth=False,
                )
            raise NetconUnavailable("ticket 失效且重新认证失败")

        if response.status_code >= 400:
            raise NetconEndpointError(
                "/{} -> HTTP {}: {}".format(path, response.status_code, self._body_preview(response))
            )

        try:
            doc = response.json()
        except ValueError as exc:
            raise NetconEndpointError("/{} 响应不是合法 JSON".format(path)) from exc
        if not isinstance(doc, dict):
            # 少数端点可能返回裸数组，统一包一层，调用方按 response 语义处理。
            doc = {"response": doc}
        self._record_success()
        return doc

    # ------------------------------------------------------------------ #
    # 驱动契约
    # ------------------------------------------------------------------ #
    async def probe(self) -> bool:
        """轻量探测：GET /network-device/count，失败返回 False（不抛异常）。"""
        try:
            await self._request("GET", "network-device/count")
            logger.info("NC 探测成功：%s", self._base)
            return True
        except NetconError as exc:
            logger.info("NC 探测失败：%s", exc)
            return False

    async def collect(self) -> CollectResult:
        """并发拉取设备/拓扑/主机/健康问题/健康总览并归一化。

        NC 不可用时返回 ``ok=False`` 的空结果；单端点失败时其余数据照常返回。
        """
        endpoints: List[Tuple[str, str]] = [
            ("devices", "network-device"),
            ("topology", "topology/physical-topology"),
            ("hosts", "host"),
            ("issues", "assurance/health-issues"),
            ("network_health", "network-health"),
        ]
        now = time.time()
        # 每轮给所有 GET 加时间戳 cache-buster：确保拿到的是 NC 当前状态，
        # 而不是内嵌 HTTP 服务/中间层缓存的旧响应（W3-A host 新鲜度修复）。
        cache_bust = {"_ts": str(int(now * 1000))}

        async def fetch(key: str, path: str) -> Tuple[str, Optional[dict], Optional[str], bool]:
            """返回 (key, doc, error, unavailable)。"""
            try:
                doc = await self._request("GET", path, params=dict(cache_bust))
                return key, doc, None, False
            except NetconEndpointError as exc:
                # 单端点失败（如 PT 不支持某端点/暂时 500）：记录后继续其它端点。
                self.endpoint_errors[key] = str(exc)
                logger.warning("NC 端点 %s 采集失败：%s", path, exc)
                return key, None, str(exc), False
            except NetconUnavailable as exc:
                return key, None, str(exc), True

        results = await asyncio.gather(*(fetch(key, path) for key, path in endpoints))
        fetched = {key: doc for key, doc, _error, _unavailable in results}
        errors = {key: error for key, _doc, error, _unavailable in results if error}
        unavailable_count = sum(1 for _key, _doc, _error, unavailable in results if unavailable)

        if unavailable_count == len(results):
            # 全部端点不可用：数据源离线，返回空结果让上层优雅降级。
            self.nc_reachable = False
            message = self._last_error or "NC 全部端点不可用"
            return CollectResult.unavailable("netcon", message)

        if unavailable_count:
            # 部分端点因连接问题失败（非退避/非认证）：说明网络抖动，逐端点容错。
            logger.warning("NC 本轮有 %d/%d 个端点不可用", unavailable_count, len(results))

        devices: List[Device] = []
        for raw in extract_list(fetched.get("devices")):
            try:
                devices.append(Device.from_netcon(raw, seen_at=now))
            except Exception as exc:  # noqa: BLE001 - 脏数据不允许打断整轮采集
                logger.warning("跳过无法归一化的设备记录：%s（原始数据：%.120s）", exc, raw)

        topology = Topology.from_netcon(fetched.get("topology"), captured_at=now)

        hosts: List[Host] = []
        for raw in extract_list(fetched.get("hosts")):
            try:
                # 每轮都用 seen_at=now 重建 host 模型（含 ping_status）——不做增量
                # 合并/缓存，保证 /api/hosts 与 NC 当前状态一致（W3-A）。
                hosts.append(Host.from_netcon(raw, seen_at=now))
            except Exception as exc:  # noqa: BLE001
                logger.warning("跳过无法归一化的主机记录：%s", exc)
        hosts_updated = "hosts" not in errors and fetched.get("hosts") is not None
        if hosts_updated:
            self.hosts_observed_at = now
            self.host_fetch_count += 1
            self.hosts_last_count = len(hosts)

        issues: List[HealthIssue] = []
        for raw in extract_list(fetched.get("issues")):
            try:
                issues.append(HealthIssue.from_netcon(raw, seen_at=now))
            except Exception as exc:  # noqa: BLE001
                logger.warning("跳过无法归一化的健康问题记录：%s", exc)

        # W3.1：NC 的 /network-health 汇总字段口径不可靠（只有百分比、没有
        # healthyDevices 计数），因此以设备列表自算为权威口径，NC 原始值放 nc_*。
        nc_health = NetworkHealth.from_netcon(fetched.get("network_health"))
        health = NetworkHealth.compute_from_devices(
            devices, source="netcon", nc_health=nc_health
        )

        self.nc_reachable = True
        self.endpoint_errors = errors
        return CollectResult(
            devices=devices,
            topology=topology,
            hosts=hosts,
            issues=issues,
            metrics=[],  # NC 不提供计数器，指标由 simulator 生成并标 source=simulator
            network_health=health,
            source="netcon",
            ok=True,
            error=None if not errors else "; ".join(sorted(errors.values()))[:500],
            errors=errors,
            collected_at=now,
            hosts_observed_at=self.hosts_observed_at,
        )

    # ------------------------------------------------------------------ #
    # W10 路径追踪（NC flow-analysis）
    # ------------------------------------------------------------------ #
    async def flow_analysis(
        self,
        source_ip: str,
        dest_ip: str,
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> Dict[str, Any]:
        """调用 NC ``POST /flow-analysis`` 创建追踪并轮询结果，返回统一字典。

        契约：``{"ok": bool, "flow_analysis_id": str|None, "raw": dict|None,
        "error": str|None}``；NC 不可用/不支持/超时都返回 ``ok=False`` + 中文 error，
        不抛异常（API 层可直接回给前端）。实测 PT 9.0 响应：

        - POST → ``{"response": {"flowAnalysisId": "...", "url": ...}, "version": "1"}``
        - GET  → ``response.request.status = "COMPLETED"`` + ``response.networkElementsInfo[]``
          （元素含 name/id/ip/ingressInterface.physicalInterface.name/egressInterface...）
        """
        total_timeout = float(
            timeout if timeout is not None else self.settings.path_trace_timeout
        )
        interval = float(
            poll_interval
            if poll_interval is not None
            else self.settings.path_trace_poll_interval
        )
        started = time.monotonic()
        try:
            created = await self._request(
                "POST",
                "flow-analysis",
                json_body={"sourceIP": source_ip, "destIP": dest_ip},
            )
        except NetconUnavailable as exc:
            return {
                "ok": False,
                "flow_analysis_id": None,
                "raw": None,
                "error": "NC 不可用，无法执行路径追踪：{}".format(exc),
            }
        except NetconEndpointError as exc:
            return {
                "ok": False,
                "flow_analysis_id": None,
                "raw": None,
                "error": "NC 不支持路径追踪（flow-analysis）：{}".format(exc),
            }

        payload = created.get("response") if isinstance(created, dict) else None
        # 少数实现 POST 直接返回完整结果（兼容处理）
        if isinstance(payload, dict) and payload.get("networkElementsInfo"):
            return {
                "ok": True,
                "flow_analysis_id": payload.get("flowAnalysisId"),
                "raw": created,
                "error": None,
            }
        flow_id: Optional[str] = None
        if isinstance(payload, dict):
            flow_id = _text_or_none(
                payload.get("flowAnalysisId") or payload.get("id")
            )
        elif isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, dict):
                flow_id = _text_or_none(
                    first.get("flowAnalysisId") or first.get("id")
                )
        if not flow_id:
            return {
                "ok": False,
                "flow_analysis_id": None,
                "raw": created,
                "error": "NC 未返回 flowAnalysisId（响应：{}）".format(
                    _preview_dict(created)
                ),
            }

        last_raw: Optional[Dict[str, Any]] = None
        last_status = ""
        while time.monotonic() - started < total_timeout:
            await asyncio.sleep(interval)
            try:
                result = await self._request("GET", "flow-analysis/{}".format(flow_id))
            except NetconUnavailable as exc:
                return {
                    "ok": False,
                    "flow_analysis_id": flow_id,
                    "raw": last_raw,
                    "error": "轮询 NC 路径追踪失败：{}".format(exc),
                }
            except NetconEndpointError as exc:
                # 结果尚未就绪时 NC 可能短暂 4xx/5xx：继续等到超时
                logger.debug("路径追踪轮询暂时失败（继续重试）：%s", exc)
                continue
            last_raw = result
            response = result.get("response") if isinstance(result, dict) else None
            if isinstance(response, list) and response:
                # 少数实现直接返回 hop 列表
                return {
                    "ok": True,
                    "flow_analysis_id": flow_id,
                    "raw": result,
                    "error": None,
                }
            if not isinstance(response, dict):
                continue
            request_meta = response.get("request")
            status = ""
            if isinstance(request_meta, dict):
                status = str(request_meta.get("status") or "").upper()
            status = status or str(response.get("status") or "").upper()
            last_status = status
            elements = response.get("networkElementsInfo") or response.get(
                "networkElements"
            )
            if elements and status in ("", "COMPLETED", "SUCCESS", "DONE", "IN_PROGRESS"):
                if status in ("", "COMPLETED", "SUCCESS", "DONE"):
                    return {
                        "ok": True,
                        "flow_analysis_id": flow_id,
                        "raw": result,
                        "error": None,
                    }
            if status in ("FAILED", "ERROR", "TIMEOUT"):
                return {
                    "ok": False,
                    "flow_analysis_id": flow_id,
                    "raw": result,
                    "error": "NC 路径追踪失败（status={}）".format(status),
                }
        return {
            "ok": False,
            "flow_analysis_id": flow_id,
            "raw": last_raw,
            "error": "NC 路径追踪超时（>{:.0f}s，最后状态：{}）".format(
                total_timeout, last_status or "unknown"
            ),
        }

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # 观测信息
    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        return {
            "driver": self.name,
            "base_url": self._base,
            "reachable": self.nc_reachable,
            "failures": self._failures,
            "last_error": self._last_error,
            "last_ok_at": self.last_ok_at,
            "login_count": self.login_count,
            "next_retry_at": self._next_attempt_at or None,
            "hosts_observed_at": self.hosts_observed_at,
            "host_fetch_count": self.host_fetch_count,
            "hosts_last_count": self.hosts_last_count,
        }
