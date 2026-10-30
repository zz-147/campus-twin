# -*- coding: utf-8 -*-
"""Campus Twin MCP Server 核心逻辑（与 MCP SDK 解耦，便于 pytest 直接测试）。

设计目标
========
1. **只读**：所有工具只调用后端 GET 接口；唯一 POST 是 ``/api/path-trace``
   （路径追踪是只读诊断动作，后端不写库）。
2. **协议无关**：本模块只依赖 ``httpx`` + 标准库，可在 Python 3.9（项目主 venv，
   用于 pytest）与 Python 3.14（.venv-mcp314，用于真正跑 MCP server）下运行。
3. **绝不抛异常给 MCP 客户端**：后端不可用/超时/返回非法 JSON 时，工具返回
   明确的中文错误文本，server 保持存活。
4. **资源白名单**：``campus://docs/*`` 只允许读取 ``docs/`` 下两个固定文件，
   拒绝任意路径/目录穿越。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
SERVER_NAME = "campus-twin"
SERVER_VERSION = "0.1.0"

MCP_SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MCP_SERVER_DIR.parent
DEFAULT_BACKEND_BASE = "http://127.0.0.1:8000"
DEFAULT_DOCS_DIR = PROJECT_ROOT / "docs"
DEFAULT_LOG_FILE = MCP_SERVER_DIR / "logs" / "mcp_server.log"

# docs 资源白名单：MCP 资源 URI -> docs/ 下的固定文件名
DOC_RESOURCE_FILES: Dict[str, str] = {
    "campus://docs/network-design": "校园网络拓扑设计.md",
    "campus://docs/troubleshooting": "故障注入实验记录.md",
}

TOOL_DESCRIPTIONS: Dict[str, str] = {
    "get_devices": "获取当前网络设备清单（hostname / management_ip / reachability / collection_status / source）",
    "get_topology": "获取设备拓扑节点与链路（nodes / links，含端口与速率）",
    "get_hosts": "获取终端主机清单（hostname / ip / connected_device_name / connected_interface）",
    "get_issues": "获取当前活跃健康问题（NC issue，含 observed_at / stale_source）",
    "get_alerts": "获取告警列表，可选 state=active|cleared|info|all（默认 active）",
    "get_alert_groups": "获取告警根因分组（含下游抑制 suppressed_by / suppressed_count）",
    "get_notify_status": "获取 Webhook 告警通知状态（enabled / type / min_severity / sent_count）",
    "trace_path": "追踪 source_ip → dest_ip 的转发路径（NC flow-analysis + 拓扑补全，只读诊断）",
    "get_scene": "获取 3D 场景配置（楼栋 / 设备 / 坐标 / 状态，配置驱动）",
    "get_audit": "获取最近操作审计记录（默认 20 条，只读）",
}

LOGGER_NAME = "campus_twin.mcp_server"
logger = logging.getLogger(LOGGER_NAME)


# --------------------------------------------------------------------------- #
# 错误与日志
# --------------------------------------------------------------------------- #
class BackendError(RuntimeError):
    """后端不可用 / 返回异常时抛出的内部异常（由工具层转成错误文本）。"""


def get_backend_base() -> str:
    return (
        os.environ.get("BACKEND_BASE", "").strip() or DEFAULT_BACKEND_BASE
    ).rstrip("/")


def get_docs_dir() -> Path:
    configured = os.environ.get("MCP_DOCS_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_DOCS_DIR


def get_log_file() -> Path:
    configured = os.environ.get("MCP_LOG_FILE", "").strip()
    return Path(configured) if configured else DEFAULT_LOG_FILE


def configure_logging() -> logging.Logger:
    """日志写 logs/mcp_server.log + stderr（stdout 留给 MCP stdio 协议，绝不能打印）。"""
    if getattr(logger, "_campus_configured", False):
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        log_file = get_log_file()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # 日志目录不可写不能影响 MCP server 启动
        pass
    stream_handler = logging.StreamHandler(stream=sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger._campus_configured = True  # type: ignore[attr-defined]
    return logger


def log_tool_call(
    tool: str,
    ok: bool,
    elapsed_ms: int,
    error: Optional[str] = None,
) -> None:
    """记录一次工具调用（不记录完整业务数据，避免敏感信息进日志）。"""
    payload: Dict[str, Any] = {
        "tool": tool,
        "ok": bool(ok),
        "elapsed_ms": int(elapsed_ms),
    }
    if error:
        payload["error"] = str(error)[:200]
    configure_logging().info(json.dumps(payload, ensure_ascii=False))


def log_resource_read(uri: str, ok: bool, chars: int = 0, error: Optional[str] = None) -> None:
    payload: Dict[str, Any] = {"resource": uri, "ok": bool(ok), "chars": int(chars)}
    if error:
        payload["error"] = str(error)[:200]
    configure_logging().info(json.dumps(payload, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# 后端 HTTP 客户端
# --------------------------------------------------------------------------- #
class BackendClient:
    """后端 REST 客户端（同步 httpx；MCP tool 每次调用创建一个短连接 client）。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = (base_url or get_backend_base()).rstrip("/")
        self.timeout = float(timeout)
        self.transport = transport

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        url = "{}{}".format(self.base_url, path)
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout or self.timeout, connect=3.0),
                transport=self.transport,
                trust_env=False,
                headers={"Accept": "application/json"},
            ) as client:
                response = client.request(
                    method, url, params=params, json=json_body
                )
        except httpx.HTTPError as exc:
            raise BackendError(
                "无法连接后端服务 {}（请确认 campus-twin backend 已启动）：{}".format(
                    self.base_url, exc
                )
            ) from exc
        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ")
            raise BackendError(
                "后端返回 HTTP {}（{}）：{}".format(response.status_code, path, body)
            )
        try:
            return response.json()
        except ValueError as exc:
            raise BackendError(
                "后端返回非 JSON 响应（{}）：{}".format(path, response.text[:200])
            ) from exc

    def get_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        return self._request("GET", path, params=params, timeout=timeout)

    def post_json(
        self,
        path: str,
        body: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Any:
        return self._request("POST", path, json_body=body, timeout=timeout)


_default_client: Optional[BackendClient] = None


def set_default_client(client: Optional[BackendClient]) -> None:
    """测试/嵌入场景可替换默认后端客户端。"""
    global _default_client
    _default_client = client


def get_client(client: Optional[BackendClient] = None) -> BackendClient:
    if client is not None:
        return client
    if _default_client is not None:
        return _default_client
    return BackendClient()


# --------------------------------------------------------------------------- #
# 工具实现（纯函数；返回紧凑 JSON 文本或中文错误文本）
# --------------------------------------------------------------------------- #
def _json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def _safe(fn) -> str:
    try:
        return fn()
    except BackendError as exc:
        configure_logging().warning("后端访问失败：%s", exc)
        return "错误：{}".format(exc)
    except Exception as exc:  # noqa: BLE001 - MCP 工具绝不能把异常抛给客户端
        configure_logging().exception("工具执行异常")
        return "错误：MCP 工具执行异常：{}".format(exc)


def tool_get_devices(client: Optional[BackendClient] = None) -> str:
    """GET /api/devices"""
    return _safe(lambda: _json_text(get_client(client).get_json("/api/devices")))


def tool_get_topology(client: Optional[BackendClient] = None) -> str:
    """GET /api/topology"""
    return _safe(lambda: _json_text(get_client(client).get_json("/api/topology")))


def tool_get_hosts(client: Optional[BackendClient] = None) -> str:
    """GET /api/hosts"""
    return _safe(lambda: _json_text(get_client(client).get_json("/api/hosts")))


def tool_get_issues(client: Optional[BackendClient] = None) -> str:
    """GET /api/issues"""
    return _safe(lambda: _json_text(get_client(client).get_json("/api/issues")))


def tool_get_alerts(
    state: str = "active", client: Optional[BackendClient] = None
) -> str:
    """GET /api/alerts?state=&limit=50（只读）"""
    state_value = (state or "active").strip().lower()
    if state_value not in ("active", "cleared", "info", "all"):
        return "错误：state 仅支持 active / cleared / info / all"
    return _safe(
        lambda: _json_text(
            get_client(client).get_json(
                "/api/alerts", params={"state": state_value, "limit": 50}
            )
        )
    )


def tool_get_alert_groups(client: Optional[BackendClient] = None) -> str:
    """GET /api/alerts/groups?state=active"""
    return _safe(
        lambda: _json_text(
            get_client(client).get_json(
                "/api/alerts/groups", params={"state": "active"}
            )
        )
    )


def tool_get_notify_status(client: Optional[BackendClient] = None) -> str:
    """GET /api/notify/status"""
    return _safe(
        lambda: _json_text(get_client(client).get_json("/api/notify/status"))
    )


def tool_trace_path(
    source_ip: str, dest_ip: str, client: Optional[BackendClient] = None
) -> str:
    """POST /api/path-trace（只读诊断；后端会调 NC flow-analysis，超时放宽到 45s）"""
    source = str(source_ip or "").strip()
    dest = str(dest_ip or "").strip()
    if not source or not dest:
        return "错误：source_ip 与 dest_ip 不能为空"
    return _safe(
        lambda: _json_text(
            get_client(client).post_json(
                "/api/path-trace",
                {"source_ip": source, "dest_ip": dest},
                timeout=45.0,
            )
        )
    )


def tool_get_scene(client: Optional[BackendClient] = None) -> str:
    """GET /api/scene"""
    return _safe(lambda: _json_text(get_client(client).get_json("/api/scene")))


def tool_get_audit(
    limit: int = 20, client: Optional[BackendClient] = None
) -> str:
    """GET /api/audit?limit=N（默认 20，最大 100）"""
    try:
        limit_value = int(limit)
    except (TypeError, ValueError):
        return "错误：limit 必须是整数"
    limit_value = max(1, min(limit_value, 100))
    return _safe(
        lambda: _json_text(
            get_client(client).get_json(
                "/api/audit", params={"limit": limit_value}
            )
        )
    )


# 工具名 -> (实现, 描述)
TOOL_REGISTRY = {
    "get_devices": (tool_get_devices, TOOL_DESCRIPTIONS["get_devices"]),
    "get_topology": (tool_get_topology, TOOL_DESCRIPTIONS["get_topology"]),
    "get_hosts": (tool_get_hosts, TOOL_DESCRIPTIONS["get_hosts"]),
    "get_issues": (tool_get_issues, TOOL_DESCRIPTIONS["get_issues"]),
    "get_alerts": (tool_get_alerts, TOOL_DESCRIPTIONS["get_alerts"]),
    "get_alert_groups": (tool_get_alert_groups, TOOL_DESCRIPTIONS["get_alert_groups"]),
    "get_notify_status": (tool_get_notify_status, TOOL_DESCRIPTIONS["get_notify_status"]),
    "trace_path": (tool_trace_path, TOOL_DESCRIPTIONS["trace_path"]),
    "get_scene": (tool_get_scene, TOOL_DESCRIPTIONS["get_scene"]),
    "get_audit": (tool_get_audit, TOOL_DESCRIPTIONS["get_audit"]),
}


def run_tool(name: str, *args: Any, **kwargs: Any) -> str:
    """server.py 统一入口：执行 + 计时 + 日志（工具函数本身已兜底异常）。"""
    started = time.perf_counter()
    try:
        registered = TOOL_REGISTRY.get(name)
        if registered is None:
            return "错误：未知工具 {}".format(name)
        result = registered[0](*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 最后一道防线
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        message = "错误：MCP 工具 {} 执行异常：{}".format(name, exc)
        log_tool_call(name, False, elapsed_ms, error=message)
        return message
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    ok = not str(result).startswith("错误：")
    log_tool_call(
        name,
        ok,
        elapsed_ms,
        error=None if ok else str(result)[:200],
    )
    return result


# --------------------------------------------------------------------------- #
# Resources（4 个只读资源）
# --------------------------------------------------------------------------- #
def _safe_resource(uri: str, fn) -> str:
    started = time.perf_counter()
    try:
        text = fn()
    except Exception as exc:  # noqa: BLE001 - resource 读取失败返回错误文本
        message = "错误：读取资源 {} 失败：{}".format(uri, exc)
        log_resource_read(uri, False, error=message)
        return message
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    ok = not text.startswith("错误：")
    log_resource_read(uri, ok, chars=len(text), error=None if ok else text[:200])
    logger.debug("resource %s read in %dms", uri, elapsed_ms)
    return text


def read_scene_resource(client: Optional[BackendClient] = None) -> str:
    """campus://scene -> /api/scene"""
    return _safe_resource(
        "campus://scene",
        lambda: _json_text(get_client(client).get_json("/api/scene")),
    )


def read_topology_resource(client: Optional[BackendClient] = None) -> str:
    """campus://topology -> /api/topology"""
    return _safe_resource(
        "campus://topology",
        lambda: _json_text(get_client(client).get_json("/api/topology")),
    )


def read_doc_resource(uri: str) -> str:
    """读取 docs/ 白名单文件；拒绝白名单之外的任何路径。"""
    filename = DOC_RESOURCE_FILES.get(uri)
    if not filename:
        return "错误：资源 {} 不在只读白名单内（允许：{}）".format(
            uri, ", ".join(sorted(DOC_RESOURCE_FILES))
        )
    docs_dir = get_docs_dir().resolve()
    try:
        target = (docs_dir / filename).resolve()
    except OSError as exc:
        return "错误：解析资源路径失败：{}".format(exc)
    # 关键安全约束：目标必须是 docs 目录下的固定白名单文件
    if target != (docs_dir / filename).resolve():
        return "错误：资源路径不在只读白名单内"
    if docs_dir not in target.parents:
        return "错误：资源路径不在只读白名单内"
    if not target.exists() or not target.is_file():
        return "错误：白名单文档不存在：{}/{}".format(docs_dir, filename)
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        return "错误：读取文档失败：{}".format(exc)


def resource_network_design() -> str:
    return read_doc_resource("campus://docs/network-design")


def resource_troubleshooting() -> str:
    return read_doc_resource("campus://docs/troubleshooting")


RESOURCE_URIS = (
    "campus://scene",
    "campus://topology",
    "campus://docs/network-design",
    "campus://docs/troubleshooting",
)


# --------------------------------------------------------------------------- #
# Prompts（3 个标准 SOP 提示词）
# --------------------------------------------------------------------------- #
def prompt_troubleshoot_port_down(
    device_name: str, interface_name: str = "UNKNOWN"
) -> str:
    """端口 down 排障 SOP。"""
    return """你是校园网运维助手，请按以下步骤排查端口 down 问题（只读诊断，不要编造数据）：

目标：设备 {device} 的接口 {interface} 处于 down / 异常状态。

1. 调用 get_devices，确认设备 {device} 在线（reachability / collection_status）。
2. 调用 get_topology，定位接口 {interface} 所在的链路，记录对端设备与端口。
3. 调用 get_hosts，判断该端口是否连接终端主机；若是，检查主机 ping_status。
4. 调用 get_issues 与 get_alerts(state="active")，查看是否有 link_down / device_unreachable
   告警；若存在根因告警，调用 get_alert_groups 确认是否被上游抑制。
5. 如需路径视角，调用 trace_path(source_ip, dest_ip) 并关注 hops[].ingress_port / egress_port
   与 synthetic=true 的拓扑推断 hop。
6. 用 get_audit 查看近期是否有人做过相关操作（软删/静默/场景变更）。

输出要求：
- 先给结论（端口 down / 链路 down / 终端问题 / 数据不足），再给证据（工具返回的具体字段）。
- 给出 1-3 条可执行建议（检查线缆、端口配置、对端端口、终端网卡），本 MCP 只读，
  不要声称已执行任何修复操作。
""".format(device=device_name or "UNKNOWN", interface=interface_name or "UNKNOWN")


def prompt_troubleshoot_link_down(
    source_device: str, target_device: str
) -> str:
    """链路 down 排障 SOP。"""
    return """你是校园网运维助手，请排查 {source} ↔ {target} 链路故障（只读诊断）：

1. 调用 get_topology，找到 {source} 与 {target} 之间的 link（link_status / 两端端口）。
2. 调用 get_devices 确认两端设备是否在线、是否 Unreachable。
3. 调用 get_issues 与 get_alerts(state="active")，检索 link_down / device_unreachable。
4. 调用 get_alert_groups，判断该链路告警是否为根因组、下游成员是否被 suppressed_by 抑制。
5. 调用 get_hosts 评估受影响终端（按 connected_device_name 关联）。
6. 需要定位路径断点时调用 trace_path；若 hops 中出现 synthetic=true，说明是拓扑推断补全的
   中间设备，输出时要注明“推断”。

输出要求：根因判断（物理链路 / 光模块 / 配置 / 对端设备 / 数据不足）、影响面（受影响主机与
告警数）、建议动作。只读边界：不得声称已修改任何配置。
""".format(
        source=source_device or "UNKNOWN", target=target_device or "UNKNOWN"
    )


def prompt_onboard_device(
    device_name: str, building: str = "unknown", model: str = "unknown"
) -> str:
    """新设备接入引导 SOP。"""
    return """你是校园网接入助手，请为以下新设备生成接入检查清单（只读；写操作需人工在
后端/场景配置中执行）：

设备：{device}；楼栋：{building}；型号：{model}

1. 调用 get_devices 与 get_hosts，确认该设备/终端是否已被 NC 采集到（inventory 中是否存在）。
2. 调用 get_scene，检查场景中是否已有该设备、所在楼栋 id 与坐标是否冲突；
   调用 /api/scene/pending 对应的 MCP 只读视角可通过 get_audit 查看最近确认记录。
3. 调用 get_topology，确认上联端口与所在楼栋的接入交换机。
4. 调用 get_issues / get_alerts(state="active")，确认接入前无未处理故障。
5. 参考资源 campus://docs/network-design 的 VLAN/网段规划，给出建议的 VLAN、管理 IP、
   上联端口与设备命名。
6. 整理接入步骤：物理连线 → 端口配置（VLAN/trunk）→ 管理 IP → NC 纳管 → 场景确认，
   并明确标注每一步需要人工执行。MCP Server 只读，不提供写接口。
""".format(
        device=device_name or "UNKNOWN",
        building=building or "unknown",
        model=model or "unknown",
    )


PROMPTS = {
    "troubleshoot_port_down": prompt_troubleshoot_port_down,
    "troubleshoot_link_down": prompt_troubleshoot_link_down,
    "onboard_device": prompt_onboard_device,
}


def server_instructions() -> str:
    return (
        "Campus Twin MCP Server（只读）。可查询校园网设备、拓扑、主机、健康问题、告警、"
        "告警根因分组、通知状态、3D 场景与操作审计，并可追踪两点间转发路径。"
        "所有工具只读，不暴露任何写接口；数据来自 campus-twin backend（默认 8000）。"
    )
