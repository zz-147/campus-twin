# -*- coding: utf-8 -*-
"""后端数据访问层：HTTP 真实后端 / fixture 离线快照，只读 + W9 写接口。

Agent 的所有工具都通过这一层读取 campus-twin 后端 REST API，
不直接访问 Packet Tracer / Network Controller。

W9 新增：
- 写方法 ``post`` / ``delete``（所有写请求带 ``X-Operator`` 头，由后端审计）；
- fixture 模式内置 W9 场景配置/告警管理/审计的内存状态，
  使离线单测与评测在真实后端就绪前也能覆盖完整 action 确认执行链路。
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from urllib.parse import unquote

import httpx

#: 后端 W9 写接口统一操作者标识（Agent 身份；前端按钮操作会传 web-ui）
AGENT_OPERATOR = "agent"


class BackendError(RuntimeError):
    """后端不可用/返回异常/写接口拒绝。消息可安全展示（不含密钥）。"""


@runtime_checkable
class BackendClient(Protocol):
    """后端 client 协议（只读 + W9 写接口）。"""

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ...

    def post(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        operator: Optional[str] = AGENT_OPERATOR,
    ) -> Dict[str, Any]:
        ...

    def delete(self, path: str, operator: str = AGENT_OPERATOR) -> Dict[str, Any]:
        ...


class HttpBackendClient:
    """真实后端 client（httpx）：http://127.0.0.1:8000。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout_s: float = 5.0) -> None:
        self._base_url = (base_url or "http://127.0.0.1:8000").rstrip("/")
        self._timeout = max(0.5, float(timeout_s))
        self._client: Optional[httpx.Client] = None
        self._w9_cache: Optional[Dict[str, Any]] = None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def mode(self) -> str:
        return "http"

    def _client_instance(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self._timeout),
                headers={"Accept": "application/json"},
            )
        return self._client

    # ------------------------------------------------------------------ #
    # 通用请求
    # ------------------------------------------------------------------ #
    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        operator: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = self._base_url + path
        method = method.upper()
        headers: Dict[str, str] = {}
        if method != "GET" and operator:
            # 所有写操作必须带 X-Operator（后端审计）；只读 POST（如 /api/path-trace）传 operator=None
            headers["X-Operator"] = operator
        try:
            response = self._client_instance().request(
                method, url, params=params or None, json=body, headers=headers or None
            )
        except httpx.HTTPError as exc:  # 连接失败/超时等
            raise BackendError(
                "后端不可达（%s）：%s: %s" % (self._base_url, type(exc).__name__, exc)
            ) from None
        if response.status_code >= 400:
            detail = _response_detail(response)
            raise BackendError(
                "后端返回 HTTP %d（%s）%s"
                % (response.status_code, path, ("：" + detail) if detail else "")
            )
        if response.status_code == 204 or not response.content:
            return {"ok": True, "status_code": response.status_code}
        try:
            payload = response.json()
        except ValueError:
            raise BackendError("后端响应不是合法 JSON（%s）" % path) from None
        if not isinstance(payload, dict):
            raise BackendError("后端响应格式异常（%s）：期望 JSON 对象" % path)
        return payload

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        operator: Optional[str] = AGENT_OPERATOR,
    ) -> Dict[str, Any]:
        return self.request("POST", path, body=body or {}, operator=operator)

    def delete(self, path: str, operator: str = AGENT_OPERATOR) -> Dict[str, Any]:
        return self.request("DELETE", path, operator=operator)

    def probe_w9(self, timeout_s: float = 1.5, ttl_s: float = 20.0) -> Dict[str, Any]:
        """探测后端 W9 接口是否就绪（/api/scene），带短缓存避免拖慢 /api/health。"""
        now = time.time()
        if self._w9_cache and now - float(self._w9_cache.get("checked_at") or 0) < ttl_s:
            return dict(self._w9_cache)
        result: Dict[str, Any] = {
            "available": False,
            "mode": "http",
            "endpoint": "/api/scene",
            "checked_at": now,
            "error": None,
        }
        try:
            response = httpx.get(
                self._base_url + "/api/scene",
                timeout=httpx.Timeout(max(0.5, timeout_s)),
                headers={"Accept": "application/json"},
            )
            if response.status_code < 400:
                result["available"] = True
            else:
                result["error"] = "HTTP %d" % response.status_code
        except httpx.HTTPError as exc:
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
        self._w9_cache = result
        return dict(result)

    def probe_w10(self, timeout_s: float = 1.5, ttl_s: float = 20.0) -> Dict[str, Any]:
        """探测后端 W10 接口是否就绪（/api/notify/status、/api/alerts/groups）。"""
        now = time.time()
        cache = getattr(self, "_w10_cache", None)
        if cache and now - float(cache.get("checked_at") or 0) < ttl_s:
            return dict(cache)
        result: Dict[str, Any] = {
            "available": False,
            "mode": "http",
            "endpoints": ["/api/path-trace", "/api/alerts/groups", "/api/notify/status"],
            "checked_at": now,
            "error": None,
        }
        checks = {
            "/api/notify/status": "GET",
            "/api/alerts/groups": "GET",
        }
        missing: List[str] = []
        for endpoint, method in checks.items():
            try:
                response = httpx.request(
                    method,
                    self._base_url + endpoint + ("?state=active" if "groups" in endpoint else ""),
                    timeout=httpx.Timeout(max(0.5, timeout_s)),
                    headers={"Accept": "application/json"},
                )
                if response.status_code >= 400:
                    missing.append("%s(HTTP %d)" % (endpoint, response.status_code))
            except httpx.HTTPError as exc:
                result["error"] = "%s: %s" % (type(exc).__name__, exc)
                missing.append(endpoint)
        result["available"] = not missing
        if missing:
            result["error"] = "接口未就绪：%s" % "、".join(missing)
        self._w10_cache = result
        return dict(result)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:300]
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if detail is not None:
            return str(detail)[:500]
        return json.dumps(payload, ensure_ascii=False)[:500]
    return str(payload)[:500]


class FixtureBackendClient:
    """离线 fixture client：从快照返回只读数据，并在内存中模拟 W9 写接口。

    写接口语义与 docs/W9-Agent扩展设计.md 的后端契约保持一致（含 active 告警不可删除、
    软删除、X-Operator 审计），用于 Agent 离线单测/评测，不依赖真实后端。
    """

    def __init__(self, snapshot: Dict[str, Any], w10_available: bool = True) -> None:
        if not isinstance(snapshot, dict):
            raise BackendError("fixture 快照必须是 dict")
        self._snapshot = snapshot
        self._w10_available = bool(w10_available)
        # ---- W9 可变状态（每个 client 实例独立） ----
        self._scene: Dict[str, Any] = copy.deepcopy(
            snapshot.get("scene") if isinstance(snapshot.get("scene"), dict) else _default_scene()
        )
        self._scene_initial: Dict[str, Any] = copy.deepcopy(self._scene)
        alerts_block = snapshot.get("alerts") or {}
        self._alerts: List[Dict[str, Any]] = copy.deepcopy(list(alerts_block.get("alerts") or []))
        audit_block = snapshot.get("audit") or {}
        self._audit: List[Dict[str, Any]] = copy.deepcopy(list(audit_block.get("entries") or []))
        self._audit_seq = len(self._audit)
        self._alert_seq = len(self._alerts)
        self._notify: Dict[str, Any] = copy.deepcopy(
            snapshot.get("notify") if isinstance(snapshot.get("notify"), dict) else _default_notify()
        )

    @property
    def mode(self) -> str:
        return "fixture"

    @property
    def snapshot(self) -> Dict[str, Any]:
        return self._snapshot

    def probe_w9(self, timeout_s: float = 1.5, ttl_s: float = 20.0) -> Dict[str, Any]:
        return {
            "available": True,
            "mode": "fixture",
            "endpoint": "/api/scene",
            "checked_at": time.time(),
            "error": None,
        }

    def probe_w10(self, timeout_s: float = 1.5, ttl_s: float = 20.0) -> Dict[str, Any]:
        return {
            "available": self._w10_available,
            "mode": "fixture",
            "endpoints": ["/api/path-trace", "/api/alerts/groups", "/api/notify/status"],
            "checked_at": time.time(),
            "error": None if self._w10_available else "fixture 已关闭 W10 接口（模拟后端未就绪）",
        }

    def _require_w10(self) -> None:
        if not self._w10_available:
            raise BackendError(
                "后端返回 HTTP 404（W10 接口尚未就绪：/api/path-trace、/api/alerts/groups、/api/notify/status）"
            )

    # ------------------------------------------------------------------ #
    # 只读接口
    # ------------------------------------------------------------------ #
    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        clean = str(path or "").split("?", 1)[0].rstrip("/")
        # ---- 基础采集数据 ----
        if clean == "/api/health":
            return copy.deepcopy(self._snapshot.get("health") or _default_health())
        if clean == "/api/devices":
            return copy.deepcopy(self._endpoint("devices"))
        if clean == "/api/topology":
            return copy.deepcopy(self._endpoint("topology"))
        if clean == "/api/hosts":
            return copy.deepcopy(self._endpoint("hosts"))
        if clean == "/api/issues":
            payload = copy.deepcopy(self._endpoint("issues"))
            issues = list(payload.get("issues") or [])
            limit = _to_int(params.get("limit"), 200)
            payload["issues"] = issues[: max(1, limit)]
            payload["count"] = len(payload["issues"])
            return payload
        # ---- 告警（W9 状态化：软删除/静默/已恢复） ----
        if clean == "/api/alerts":
            return self._alerts_payload(params)
        if clean == "/api/alerts/summary":
            return {"count": len(self._visible_alerts()), "summary": self._alert_summary()}
        if clean == "/api/network-health":
            return copy.deepcopy(self._endpoint("network_health"))
        if clean.startswith("/api/metrics/"):
            device_id = unquote(clean[len("/api/metrics/") :])
            metrics = (self._snapshot.get("metrics") or {}).get(device_id)
            if metrics is None:
                return {
                    "device_id": device_id,
                    "known_device": False,
                    "source": "simulator",
                    "count": 0,
                    "metrics": [],
                }
            payload = copy.deepcopy(metrics)
            points = list(payload.get("metrics") or [])
            metric = params.get("metric")
            if metric:
                points = [item for item in points if str(item.get("metric")) == str(metric)]
            limit = _to_int(params.get("limit"), 240)
            payload["metrics"] = points[-max(1, limit) :]
            payload["count"] = len(payload["metrics"])
            return payload
        # ---- W9：场景配置 / 审计 ----
        if clean == "/api/scene":
            return copy.deepcopy(self._scene)
        if clean == "/api/scene/pending":
            pending = [
                self._pending_device_entry(building, device)
                for building in self._scene.get("buildings") or []
                for device in building.get("devices") or []
                if str(device.get("status") or "confirmed") == "pending"
            ]
            return {
                "count": len(pending),
                "devices": pending,
                "scene_version": self._scene.get("version"),
                "updated_at": time.time(),
                "note": "fixture pending 队列（含管理 IP/型号/归属楼栋）",
            }
        if clean == "/api/audit":
            limit = max(1, _to_int(params.get("limit"), 50))
            items = list(reversed(self._audit))[:limit]
            return {"count": len(items), "items": items}
        # ---- W10：告警根因分组 / 通知状态 ----
        if clean == "/api/alerts/groups":
            self._require_w10()
            return self._alert_groups_payload(params)
        if clean == "/api/notify/status":
            self._require_w10()
            return copy.deepcopy(self._notify)
        raise BackendError("fixture 未实现的接口：%s" % clean)

    # ------------------------------------------------------------------ #
    # 写接口（W9）
    # ------------------------------------------------------------------ #
    def post(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        operator: Optional[str] = AGENT_OPERATOR,
    ) -> Dict[str, Any]:
        body = body if isinstance(body, dict) else {}
        clean = str(path or "").split("?", 1)[0].rstrip("/")
        # ---- W10：路径追踪（只读 POST，不需要 X-Operator） ----
        if clean == "/api/path-trace":
            self._require_w10()
            return self._path_trace(body)
        # ---- 告警管理 ----
        if clean == "/api/alerts/clear":
            return self._alerts_clear(body, operator or AGENT_OPERATOR)
        if clean.startswith("/api/alerts/") and clean.endswith("/silence"):
            alert_id = unquote(clean[len("/api/alerts/") : -len("/silence")])
            return self._alert_silence(alert_id, operator)
        if clean.startswith("/api/alerts/") and clean.endswith("/restore"):
            alert_id = unquote(clean[len("/api/alerts/") : -len("/restore")])
            return self._alert_restore(alert_id, operator)
        # ---- 场景配置 ----
        if clean == "/api/scene/buildings":
            return self._scene_add_building(body, operator)
        if clean.startswith("/api/scene/buildings/") and clean.endswith("/devices"):
            building_id = unquote(clean[len("/api/scene/buildings/") : -len("/devices")])
            return self._scene_upsert_device(building_id, body, operator)
        if clean.startswith("/api/scene/devices/") and clean.endswith("/confirm"):
            name = unquote(clean[len("/api/scene/devices/") : -len("/confirm")])
            return self._scene_confirm_device(name, operator)
        if clean == "/api/scene/reset":
            self._scene = copy.deepcopy(self._scene_initial)
            self._scene["version"] = int(self._scene.get("version") or 1) + 1
            self._record_audit("scene_reset", "scene", "重置为默认场景", operator)
            return {"ok": True, "version": self._scene["version"]}
        raise BackendError("fixture 未实现的写接口：POST %s" % clean)

    def delete(self, path: str, operator: str = AGENT_OPERATOR) -> Dict[str, Any]:
        clean = str(path or "").split("?", 1)[0].rstrip("/")
        if clean.startswith("/api/alerts/"):
            alert_id = unquote(clean[len("/api/alerts/") :])
            return self._alert_delete(alert_id, operator)
        if clean.startswith("/api/scene/devices/"):
            name = unquote(clean[len("/api/scene/devices/") :])
            return self._scene_remove_device(name, operator)
        raise BackendError("fixture 未实现的写接口：DELETE %s" % clean)

    # ------------------------------------------------------------------ #
    # 内部：快照
    # ------------------------------------------------------------------ #
    def _endpoint(self, key: str) -> Dict[str, Any]:
        value = self._snapshot.get(key)
        if value is None:
            raise BackendError("fixture 快照缺少接口数据：%s" % key)
        if not isinstance(value, dict):
            raise BackendError("fixture 快照接口数据格式异常：%s" % key)
        return value

    # ------------------------------------------------------------------ #
    # 内部：告警状态
    # ------------------------------------------------------------------ #
    def _visible_alerts(self, include_deleted: bool = False) -> List[Dict[str, Any]]:
        if include_deleted:
            return list(self._alerts)
        return [alert for alert in self._alerts if not alert.get("deleted_at")]

    def _alerts_payload(self, params: Dict[str, Any]) -> Dict[str, Any]:
        include_deleted = bool(params.get("include_deleted"))
        alerts = copy.deepcopy(self._visible_alerts(include_deleted))
        severity = params.get("severity")
        if severity:
            alerts = [item for item in alerts if str(item.get("severity")) == str(severity)]
        limit = _to_int(params.get("limit"), 100)
        alerts = alerts[: max(1, limit)]
        summary = self._alert_summary()
        return {
            "count": len(alerts),
            "state": params.get("state") or "all",
            "include_deleted": include_deleted,
            "summary": summary,
            "alerts": alerts,
        }

    def _alert_summary(self) -> Dict[str, int]:
        summary = {"active": 0, "cleared": 0, "info": 0, "silenced": 0, "total": 0}
        for alert in self._visible_alerts():
            status = _alert_status(alert)
            if status == "active":
                summary["active"] += 1
            elif status == "cleared":
                summary["cleared"] += 1
            elif status == "silenced":
                summary["silenced"] += 1
            if str(alert.get("severity") or "") == "info":
                summary["info"] += 1
            summary["total"] += 1
        return summary

    def _find_alert(self, alert_id: str) -> Optional[Dict[str, Any]]:
        for alert in self._alerts:
            if str(alert.get("id") or alert.get("alert_id") or "") == str(alert_id):
                return alert
        return None

    def _alert_delete(self, alert_id: str, operator: str) -> Dict[str, Any]:
        alert = self._find_alert(alert_id)
        if alert is None:
            raise BackendError("HTTP 404：告警不存在（%s）" % alert_id)
        if alert.get("deleted_at"):
            raise BackendError("HTTP 409：告警已被删除（%s）" % alert_id)
        if _alert_status(alert) == "active":
            # 设计不变式：active 告警不可删除，只能静默
            raise BackendError(
                "HTTP 409：active 告警不可删除，只能静默（alert_id=%s）" % alert_id
            )
        alert["deleted_at"] = time.time()
        self._record_audit("alert_delete", "alert:%s" % alert_id, "软删除告警", operator)
        return {"ok": True, "deleted": 1, "alert_id": alert_id}

    def _alerts_clear(self, body: Dict[str, Any], operator: str) -> Dict[str, Any]:
        scope = str(body.get("scope") or "cleared")
        before = body.get("before")
        deleted = 0
        for alert in self._alerts:
            if alert.get("deleted_at"):
                continue
            status = _alert_status(alert)
            if status == "active":
                continue
            if scope == "cleared" and status != "cleared":
                continue
            if before is not None:
                try:
                    if float(alert.get("created_at") or 0) > float(before):
                        continue
                except (TypeError, ValueError):
                    continue
            alert["deleted_at"] = time.time()
            deleted += 1
        self._record_audit(
            "alerts_clear",
            "alerts",
            "清空已恢复告警 scope=%s deleted=%d" % (scope, deleted),
            operator,
        )
        return {"ok": True, "deleted": deleted, "scope": scope}

    def _alert_silence(self, alert_id: str, operator: str) -> Dict[str, Any]:
        alert = self._find_alert(alert_id)
        if alert is None or alert.get("deleted_at"):
            raise BackendError("HTTP 404：告警不存在（%s）" % alert_id)
        alert["previous_state"] = alert.get("state")
        alert["state"] = "silenced"
        alert["status"] = "silenced"
        alert["silenced_at"] = time.time()
        alert["silenced_by"] = operator
        self._record_audit("alert_silence", "alert:%s" % alert_id, "静默活跃告警", operator)
        return {"ok": True, "alert": copy.deepcopy(alert)}

    def _alert_restore(self, alert_id: str, operator: str) -> Dict[str, Any]:
        alert = self._find_alert(alert_id)
        if alert is None:
            raise BackendError("HTTP 404：告警不存在（%s）" % alert_id)
        alert.pop("deleted_at", None)
        self._record_audit("alert_restore", "alert:%s" % alert_id, "撤销软删除", operator)
        return {"ok": True, "alert": copy.deepcopy(alert)}

    # ------------------------------------------------------------------ #
    # 内部：W10 路径追踪 / 告警根因分组 / 通知状态（fixture 模拟后端契约）
    # ------------------------------------------------------------------ #
    def _path_trace(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """模拟后端 POST /api/path-trace：基于 topology 图求路径，识别断点。"""
        source_ip = str(body.get("source_ip") or "").strip()
        dest_ip = str(body.get("dest_ip") or "").strip()
        started = time.perf_counter()

        def elapsed() -> int:
            return max(1, int((time.perf_counter() - started) * 1000))

        if not source_ip or not dest_ip:
            return {
                "ok": False,
                "source_ip": source_ip,
                "dest_ip": dest_ip,
                "trace_status": "failed",
                "hops": [],
                "links": [],
                "elapsed_ms": elapsed(),
                "error": "缺少 source_ip / dest_ip",
            }
        hosts = list(self._endpoint("hosts").get("hosts") or [])
        devices = list(self._endpoint("devices").get("devices") or [])
        topology = self._endpoint("topology")
        links = list(topology.get("links") or [])
        host_by_ip = {str(host.get("ip")): host for host in hosts if host.get("ip")}
        source_host = host_by_ip.get(source_ip)
        dest_host = host_by_ip.get(dest_ip)
        device_by_id = {str(device.get("id")): device for device in devices}
        device_name = lambda device_id: str(
            (device_by_id.get(str(device_id)) or {}).get("hostname") or device_id
        )
        for label, host in (("源", source_host), ("目的", dest_host)):
            if host is None:
                return {
                    "ok": False,
                    "source_ip": source_ip,
                    "dest_ip": dest_ip,
                    "trace_status": "failed",
                    "hops": [],
                    "links": [],
                    "elapsed_ms": elapsed(),
                    "error": "未找到%s IP 为 %s 的主机（可在 /api/hosts 中确认）"
                    % (label, source_ip if label == "源" else dest_ip),
                }
        source_device_id = str(source_host.get("connected_device_id") or "")
        dest_device_id = str(dest_host.get("connected_device_id") or "")
        if source_device_id not in device_by_id or dest_device_id not in device_by_id:
            return {
                "ok": False,
                "source_ip": source_ip,
                "dest_ip": dest_ip,
                "trace_status": "failed",
                "hops": [],
                "links": [],
                "elapsed_ms": elapsed(),
                "error": "主机未关联到已知网络设备，无法追踪路径",
            }

        # BFS 设备级最短路径（保留 down 链路，便于定位断点）
        adjacency: Dict[str, List[tuple]] = {}
        for link in links:
            source = str(link.get("source") or "")
            target = str(link.get("target") or "")
            adjacency.setdefault(source, []).append((target, link))
            adjacency.setdefault(target, []).append((source, link))
        from collections import deque

        queue = deque([(source_device_id, [])])
        visited = {source_device_id}
        path_edges: Optional[List[Dict[str, Any]]] = None
        while queue:
            current, edges = queue.popleft()
            if current == dest_device_id:
                path_edges = edges
                break
            for neighbor, link in adjacency.get(current, []):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                queue.append(
                    (neighbor, edges + [{"from": current, "to": neighbor, "link": link}])
                )
        source_hop = self._host_hop(source_host, role="source")
        if path_edges is None:
            return {
                "ok": True,
                "source_ip": source_ip,
                "dest_ip": dest_ip,
                "trace_status": "failed",
                "hops": [source_hop],
                "links": [],
                "elapsed_ms": elapsed(),
                "error": "拓扑中没有从 %s 到 %s 的可达路径"
                % (source_host.get("hostname"), dest_host.get("hostname")),
            }

        node_sequence = [source_device_id] + [edge["to"] for edge in path_edges]
        down_index: Optional[int] = None
        for edge_index, edge in enumerate(path_edges):
            if str(edge["link"].get("link_status") or "").lower() in (
                "down",
                "err-disabled",
                "disabled",
            ):
                down_index = edge_index
                break

        hops: List[Dict[str, Any]] = [source_hop]
        complete = down_index is None
        visible_nodes = node_sequence if complete else node_sequence[: down_index + 1]
        for node_index, node_id in enumerate(visible_nodes):
            ingress = (
                self._link_port(path_edges[node_index - 1]["link"], node_id, "ingress")
                if node_index > 0
                else None
            )
            egress = None
            if node_index < len(path_edges) and (complete or node_index < (down_index or 0)):
                egress = self._link_port(path_edges[node_index]["link"], node_id, "egress")
            hops.append(
                {
                    "index": len(hops),
                    "device_name": device_name(node_id),
                    "device_id": node_id,
                    "ingress_port": ingress,
                    "egress_port": egress,
                    "role": "transit",
                }
            )
        if complete:
            hops.append(self._host_hop(dest_host, role="destination", index=len(hops)))
            status = "complete"
            error = None
            traversed = [
                {"source": edge["from"], "target": edge["to"], "link_id": edge["link"].get("id")}
                for edge in path_edges
            ]
        else:
            traversed = [
                {"source": edge["from"], "target": edge["to"], "link_id": edge["link"].get("id")}
                for edge in path_edges[: down_index or 0]
            ]
            broken_edge = path_edges[down_index]
            broken_link = broken_edge["link"]
            current_port = self._link_port(broken_link, broken_edge["from"], "egress")
            next_port = self._link_port(broken_link, broken_edge["to"], "ingress")
            error = "链路 %s %s ↔ %s %s 状态为 down，路径在此中断" % (
                device_name(broken_edge["from"]),
                current_port,
                device_name(broken_edge["to"]),
                next_port,
            )
            # 断点前仍有可达 hop（至少源设备），统一标记 partial 更符合语义
            status = "partial"
        return {
            "ok": True,
            "source_ip": source_ip,
            "dest_ip": dest_ip,
            "trace_status": status,
            "hops": hops,
            "links": traversed,
            "elapsed_ms": elapsed(),
            "error": error,
        }

    @staticmethod
    def _host_hop(host: Dict[str, Any], role: str, index: int = 0) -> Dict[str, Any]:
        return {
            "index": index,
            "device_name": host.get("hostname"),
            "device_id": host.get("id"),
            "ingress_port": None if role == "source" else host.get("connected_interface"),
            "egress_port": host.get("connected_interface") if role == "source" else None,
            "role": role,
        }

    @staticmethod
    def _link_port(link: Dict[str, Any], device_id: str, direction: str) -> Optional[str]:
        if not isinstance(link, dict):
            return None
        if direction == "egress":
            if str(link.get("source") or "") == str(device_id):
                return link.get("start_port_name")
            if str(link.get("target") or "") == str(device_id):
                return link.get("end_port_name")
        else:
            if str(link.get("target") or "") == str(device_id):
                return link.get("end_port_name")
            if str(link.get("source") or "") == str(device_id):
                return link.get("start_port_name")
        return None

    def _alert_groups_payload(self, params: Dict[str, Any]) -> Dict[str, Any]:
        state = str(params.get("state") or "active")
        groups = self._compute_alert_groups(state)
        return {"count": len(groups), "state": state, "groups": groups}

    def _compute_alert_groups(self, state: str = "active") -> List[Dict[str, Any]]:
        devices = {str(item.get("id")): item for item in self._endpoint("devices").get("devices") or []}
        hosts = {str(item.get("id")): item for item in self._endpoint("hosts").get("hosts") or []}
        topology_links = {str(item.get("id")): item for item in (self._snapshot.get("topology") or {}).get("links") or []}
        if state == "active":
            alerts = [alert for alert in self._visible_alerts() if _alert_status(alert) == "active"]
        elif state == "cleared":
            alerts = [alert for alert in self._visible_alerts() if _alert_status(alert) == "cleared"]
        else:
            alerts = self._visible_alerts()
        if not alerts:
            return []

        def device_label(device_id: Any) -> str:
            device = devices.get(str(device_id))
            return str((device or {}).get("hostname") or device_id or "未知设备")

        def host_label(host_id: Any) -> Optional[str]:
            host = hosts.get(str(host_id))
            return str(host.get("hostname")) if host else None

        max_priority = {"link_down": 4, "device_unreachable": 3, "device_offline": 2}
        groups: List[Dict[str, Any]] = []
        consumed: set = set()

        def downstream_ids(alert: Dict[str, Any]) -> set:
            downstream: set = set()
            alert_type = str(alert.get("alert_type") or "")
            if alert_type == "link_down":
                link = topology_links.get(str(alert.get("link_id") or ""))
                if link:
                    target_id = str(link.get("target") or "")
                    downstream.add(target_id)
                    for host in hosts.values():
                        if str(host.get("connected_device_id") or "") == target_id:
                            downstream.add(str(host.get("id")))
                            downstream.add(str(host.get("hostname")))
                    for other in topology_links.values():
                        if str(other.get("source") or "") == target_id:
                            downstream.add(str(other.get("target") or ""))
                        if str(other.get("target") or "") == target_id:
                            downstream.add(str(other.get("source") or ""))
            if alert_type in ("device_unreachable", "device_offline"):
                device_id = str(alert.get("device_id") or "")
                downstream.add(device_id)
                for host in hosts.values():
                    if str(host.get("connected_device_id") or "") == device_id:
                        downstream.add(str(host.get("id")))
                        downstream.add(str(host.get("hostname")))
            return downstream

        root_alerts = [alert for alert in alerts if str(alert.get("alert_type")) in max_priority]
        root_alerts.sort(
            key=lambda item: (
                -max_priority.get(str(item.get("alert_type")), 0),
                float(item.get("created_at") or 0),
            )
        )
        group_index = 0
        for root in root_alerts:
            key = (str(root.get("alert_type")), str(root.get("link_id") or root.get("device_id") or ""))
            if key in consumed:
                continue
            consumed.add(key)
            members = [
                alert
                for alert in alerts
                if str(alert.get("alert_type")) == str(root.get("alert_type"))
                and str(alert.get("link_id") or alert.get("device_id") or "")
                == str(root.get("link_id") or root.get("device_id") or "")
            ]
            downstream = downstream_ids(root)
            group_index += 1
            group_id = "g_%02d" % group_index
            member_ids = {str(alert.get("id") or alert.get("alert_id")) for alert in members}
            suppressed_alerts = []
            for alert in alerts:
                if alert in members:
                    continue
                alert_id = str(alert.get("id") or alert.get("alert_id"))
                if alert_id in member_ids:
                    continue
                device_ids = {
                    str(alert.get("device_id") or ""),
                    host_label(alert.get("device_id")) or "",
                }
                if any(item and item in downstream for item in device_ids):
                    suppressed_alerts.append(alert)
            suppressed_ids = {id(alert) for alert in suppressed_alerts}
            all_members = members + suppressed_alerts
            consumed.update(
                (str(alert.get("alert_type")), str(alert.get("link_id") or alert.get("device_id") or ""))
                for alert in all_members
            )
            group_index_holder = group_id
            root_cause = str(root.get("alert_type"))
            root_target = str(root.get("link_id") or root.get("device_id") or "")
            if root_cause == "link_down":
                link = topology_links.get(root_target)
                if link:
                    title = "%s ↔ %s 链路中断" % (
                        device_label(link.get("source")),
                        device_label(link.get("target")),
                    )
                    root_device_id = str(link.get("source") or "")
                    root_device_name = device_label(root_device_id)
                else:
                    title = "链路中断"
                    root_device_id = str(root.get("device_id") or "")
                    root_device_name = device_label(root_device_id)
            else:
                root_device_id = str(root.get("device_id") or "")
                root_device_name = device_label(root_device_id)
                title = "%s %s" % (
                    root_device_name,
                    "设备不可达" if root_cause == "device_unreachable" else "设备离线",
                )
            severities = [str(item.get("severity") or "warning") for item in all_members]
            severity = "critical" if "critical" in severities else (severities[0] if severities else "warning")
            times = [float(item.get("created_at") or 0) for item in all_members]
            members_payload = []
            for alert in all_members:
                alert_id = str(alert.get("id") or alert.get("alert_id"))
                suppressed_by = group_index_holder if id(alert) in suppressed_ids else None
                members_payload.append(
                    {
                        "id": alert.get("id") or alert.get("alert_id"),
                        "alert_type": alert.get("alert_type"),
                        "severity": alert.get("severity"),
                        "device_name": host_label(alert.get("device_id"))
                        or device_label(alert.get("device_id")),
                        "detail": alert.get("detail"),
                        "suppressed_by": suppressed_by,
                    }
                )
            groups.append(
                {
                    "group_id": group_index_holder,
                    "root_cause": root_cause,
                    "root_device_id": root_device_id,
                    "root_device_name": root_device_name,
                    "severity": severity,
                    "title": title,
                    "alert_ids": [alert.get("id") or alert.get("alert_id") for alert in all_members],
                    "member_count": len(all_members),
                    "suppressed_count": len(suppressed_alerts),
                    "first_seen": min(times) if times else 0.0,
                    "last_seen": max(times) if times else 0.0,
                    "members": members_payload,
                }
            )

        # 独立告警单独成组
        for alert in alerts:
            alert_id = str(alert.get("id") or alert.get("alert_id"))
            already = any(
                str(alert_id) in {str(item) for item in group["alert_ids"]} for group in groups
            )
            if already:
                continue
            group_index += 1
            group_id = "g_%02d" % group_index
            root_device_id = str(alert.get("device_id") or "")
            root_device_name = host_label(root_device_id) or device_label(root_device_id)
            groups.append(
                {
                    "group_id": group_id,
                    "root_cause": str(alert.get("alert_type") or "unknown"),
                    "root_device_id": root_device_id,
                    "root_device_name": root_device_name,
                    "severity": alert.get("severity") or "warning",
                    "title": "%s：%s" % (root_device_name, alert.get("title")),
                    "alert_ids": [alert.get("id") or alert.get("alert_id")],
                    "member_count": 1,
                    "suppressed_count": 0,
                    "first_seen": float(alert.get("created_at") or 0),
                    "last_seen": float(alert.get("created_at") or 0),
                    "members": [
                        {
                            "id": alert.get("id") or alert.get("alert_id"),
                            "alert_type": alert.get("alert_type"),
                            "severity": alert.get("severity"),
                            "device_name": root_device_name,
                            "detail": alert.get("detail"),
                            "suppressed_by": None,
                        }
                    ],
                }
            )
        return groups

    # ------------------------------------------------------------------ #
    # 内部：场景配置
    # ------------------------------------------------------------------ #
    def _scene_version_up(self) -> int:
        self._scene["version"] = int(self._scene.get("version") or 1) + 1
        return int(self._scene["version"])

    def _find_building(self, building_id: str) -> Optional[Dict[str, Any]]:
        for building in self._scene.get("buildings") or []:
            if str(building.get("id") or "") == str(building_id):
                return building
        return None

    def _pending_device_entry(
        self, building: Dict[str, Any], device: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {
            "name": device.get("name"),
            "type": device.get("type"),
            "model": device.get("model"),
            "mgmt_ip": device.get("mgmt_ip"),
            "position": device.get("position"),
            "status": device.get("status") or "pending",
            "source": device.get("source"),
            "building_id": building.get("id"),
            "building_name": building.get("name"),
        }

    def _scene_add_building(self, body: Dict[str, Any], operator: str) -> Dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise BackendError("HTTP 422：新增楼栋缺少 name")
        existing = {
            str(building.get("name") or "") for building in self._scene.get("buildings") or []
        }
        if name in existing:
            raise BackendError("HTTP 409：楼栋已存在（%s）" % name)
        index = len(self._scene.get("buildings") or [])
        position = body.get("position")
        if not isinstance(position, dict):
            position = {"x": 240.0 + 170.0 * max(0, index - 4), "z": -140.0}
        size = body.get("size")
        if not isinstance(size, dict):
            size = {"width": 64.0, "depth": 60.0, "height": 24.0}
        style = body.get("style")
        building = {
            "id": str(body.get("id") or ("bld-%d" % (index + 1))),
            "name": name,
            "position": dict(position),
            "size": dict(size),
            "style": dict(style) if isinstance(style, dict) else {},
            "status": str(body.get("status") or "pending"),
            "source": "agent",
            "devices": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._scene.setdefault("buildings", []).append(building)
        version = self._scene_version_up()
        self._record_audit("scene_add_building", "building:%s" % building["id"], name, operator)
        return {"ok": True, "building": copy.deepcopy(building), "version": version}

    def _scene_upsert_device(
        self, building_id: str, body: Dict[str, Any], operator: str
    ) -> Dict[str, Any]:
        building = self._find_building(building_id)
        if building is None:
            raise BackendError("HTTP 404：楼栋不存在（%s）" % building_id)
        name = str(body.get("name") or "").strip()
        if not name:
            raise BackendError("HTTP 422：设备缺少 name")
        position = body.get("position")
        device = {
            "name": name,
            "type": str(body.get("type") or "unknown"),
            "position": dict(position)
            if isinstance(position, dict)
            else {"x": 0.0, "y": 0.0, "z": 0.0},
            "status": str(body.get("status") or "pending"),
            "source": str(body.get("source") or "agent"),
            "model": body.get("model"),
            "mgmt_ip": body.get("mgmt_ip"),
        }
        devices = building.setdefault("devices", [])
        for index, existing in enumerate(devices):
            if str(existing.get("name")) == name:
                devices[index] = device
                break
        else:
            devices.append(device)
        building["updated_at"] = time.time()
        version = self._scene_version_up()
        self._record_audit("scene_upsert_device", "device:%s" % name, building_id, operator)
        return {"ok": True, "device": copy.deepcopy(device), "version": version}

    def _scene_confirm_device(self, name: str, operator: str) -> Dict[str, Any]:
        for building in self._scene.get("buildings") or []:
            for device in building.get("devices") or []:
                if str(device.get("name")) == name:
                    device["status"] = "confirmed"
                    version = self._scene_version_up()
                    self._record_audit(
                        "scene_confirm_device", "device:%s" % name, building.get("id"), operator
                    )
                    return {
                        "ok": True,
                        "device": copy.deepcopy(device),
                        "building_id": building.get("id"),
                        "version": version,
                    }
        raise BackendError("HTTP 404：场景中不存在设备（%s）" % name)

    def _scene_remove_device(self, name: str, operator: str) -> Dict[str, Any]:
        for building in self._scene.get("buildings") or []:
            devices = building.get("devices") or []
            for index, device in enumerate(devices):
                if str(device.get("name")) == name:
                    removed = devices.pop(index)
                    version = self._scene_version_up()
                    self._record_audit(
                        "scene_remove_device", "device:%s" % name, building.get("id"), operator
                    )
                    return {
                        "ok": True,
                        "deleted": 1,
                        "device": copy.deepcopy(removed),
                        "version": version,
                    }
        raise BackendError("HTTP 404：场景中不存在设备（%s）" % name)

    # ------------------------------------------------------------------ #
    # 内部：审计
    # ------------------------------------------------------------------ #
    def _record_audit(self, action: str, target: str, detail: str, operator: str) -> None:
        self._audit_seq += 1
        now = time.time()
        self._audit.append(
            {
                "id": self._audit_seq,
                "created_at": now,
                "created_at_iso": _iso_time(now),
                "operator": operator or AGENT_OPERATOR,
                "action": action,
                "target": target,
                "detail": detail,
                "result": "ok",
            }
        )


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _iso_time(ts: float) -> str:
    from datetime import datetime, timedelta, timezone

    try:
        return (
            datetime.fromtimestamp(float(ts), tz=timezone(timedelta(hours=8)))
            .strftime("%Y-%m-%dT%H:%M:%S%z")
        )
    except (OverflowError, OSError, ValueError):  # pragma: no cover - 防御性
        return ""


def _alert_status(alert: Dict[str, Any]) -> str:
    """归一化告警状态：active / cleared / silenced（对齐真实后端 state 字段）。"""
    state = str(alert.get("state") or "").strip().lower()
    if state == "cleared":
        return "cleared"
    if state == "silenced":
        return "silenced"
    if state == "open":
        return "active"
    status = str(alert.get("status") or "").strip().lower()
    if status in ("cleared", "resolved"):
        return "cleared"
    if status == "silenced":
        return "silenced"
    if status == "active":
        return "active"
    if alert.get("resolved_at") is not None:
        return "cleared"
    if alert.get("silenced_at") is not None:
        return "silenced"
    alert_type = str(alert.get("alert_type") or "")
    title = str(alert.get("title") or "")
    detail = str(alert.get("detail") or "")
    if alert_type.endswith("_cleared") or "已恢复" in title or "已清除" in title or "已恢复" in detail:
        return "cleared"
    return "active"


def _default_health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": "fixture",
        "driver": "fixture",
        "dataset_source": "fixture",
        "stale": False,
        "polls": 0,
        "collect_errors": 0,
        "consecutive_failures": 0,
        "poll_interval": 5,
    }


def _default_notify() -> Dict[str, Any]:
    """W10 通知状态默认值（未配置 webhook）。"""
    return {
        "enabled": False,
        "type": None,
        "min_severity": "critical",
        "last_sent_at": None,
        "last_error": None,
        "sent_count": 0,
        "url_configured": False,
    }


def _default_scene() -> Dict[str, Any]:
    """默认 4 楼栋 / 12 设备场景（与前端现有低模布局一致，首次 seed 用）。"""
    return {
        "version": 1,
        "theme": {"style": "low-poly", "background": "#0b1220"},
        "buildings": [
            {
                "id": "bld-core",
                "name": "中心机房",
                "position": [0.0, 0.0, 0.0],
                "size": [70.0, 40.0, 70.0],
                "style": {"color": "#1f3b57", "label_color": "#8fd3ff"},
                "devices": [
                    {"name": "Core-3650", "type": "switch", "position": [-15.0, 8.0, 0.0], "status": "confirmed", "source": "default"},
                    {"name": "Router-4331", "type": "router", "position": [0.0, 8.0, 0.0], "status": "confirmed", "source": "default"},
                    {"name": "Server-1", "type": "server", "position": [15.0, 8.0, 0.0], "status": "confirmed", "source": "default"},
                    {"name": "NC", "type": "nc", "position": [0.0, 8.0, -18.0], "status": "confirmed", "source": "default"},
                ],
            },
            {
                "id": "bld-jx",
                "name": "教学楼",
                "position": [-120.0, 0.0, 40.0],
                "size": [60.0, 30.0, 50.0],
                "style": {"color": "#24405a", "label_color": "#8fd3ff"},
                "devices": [
                    {"name": "SW-JX-2960", "type": "switch", "position": [0.0, 8.0, 0.0], "status": "confirmed", "source": "default"},
                    {"name": "PC-JX1", "type": "pc", "position": [-12.0, 6.0, 8.0], "status": "confirmed", "source": "default"},
                    {"name": "PC-JX2", "type": "pc", "position": [12.0, 6.0, 8.0], "status": "confirmed", "source": "default"},
                ],
            },
            {
                "id": "bld-sx",
                "name": "实训楼",
                "position": [120.0, 0.0, 40.0],
                "size": [60.0, 30.0, 50.0],
                "style": {"color": "#2a4b62", "label_color": "#8fd3ff"},
                "devices": [
                    {"name": "SW-SX-2960", "type": "switch", "position": [0.0, 8.0, 0.0], "status": "confirmed", "source": "default"},
                    {"name": "PC-SX1", "type": "pc", "position": [0.0, 6.0, 10.0], "status": "confirmed", "source": "default"},
                ],
            },
            {
                "id": "bld-ss",
                "name": "宿舍楼",
                "position": [0.0, 0.0, -140.0],
                "size": [60.0, 30.0, 60.0],
                "style": {"color": "#223f52", "label_color": "#8fd3ff"},
                "devices": [
                    {"name": "SW-SS-2960", "type": "switch", "position": [0.0, 8.0, 0.0], "status": "confirmed", "source": "default"},
                    {"name": "PC-SS1", "type": "pc", "position": [-12.0, 6.0, 10.0], "status": "confirmed", "source": "default"},
                    {"name": "PC-SS2", "type": "pc", "position": [12.0, 6.0, 10.0], "status": "confirmed", "source": "default"},
                ],
            },
        ],
    }


def _slug(name: str) -> str:
    mapping = {
        "中心机房": "core",
        "教学楼": "jx",
        "实训楼": "sx",
        "宿舍楼": "ss",
        "图书馆": "lib",
        "体育馆": "gym",
    }
    if name in mapping:
        return mapping[name]
    safe = "".join(char for char in name if char.isascii() and char.isalnum())
    return safe.lower() or ("bld" + str(abs(hash(name)) % 10000))


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_json(path: Path) -> Dict[str, Any]:
    """读取 JSON 文件（fixture / 场景集）。"""
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        raise BackendError("文件不存在：%s" % path) from None
    except ValueError as exc:
        raise BackendError("JSON 解析失败（%s）：%s" % (path, exc)) from None
    if not isinstance(payload, dict):
        raise BackendError("JSON 顶层必须是对象：%s" % path)
    return payload
