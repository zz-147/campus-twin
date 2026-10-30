# -*- coding: utf-8 -*-
"""MCP Server 核心逻辑 pytest（不依赖 mcp SDK / 不依赖真实后端）。

运行（项目主 venv 是 Python 3.9；core.py 只依赖 httpx+标准库，可直接测试）::

    cd D:\\zdsh\\campus-twin\\mcp_server
    & 'D:\\zdsh\\campus-twin\\.venv\\Scripts\\python.exe' -m pytest tests -q

真实 MCP 协议链路（tools/resources/prompts + 实际调用）由 verify_client.py 在
.venv-mcp314 下验收；本文件覆盖工具归一化/错误处理/资源白名单/prompts。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx
import pytest

MCP_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(MCP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_SERVER_DIR))

import core  # noqa: E402


# --------------------------------------------------------------------------- #
# 测试工具
# --------------------------------------------------------------------------- #
def make_client(
    handler,
) -> core.BackendClient:
    return core.BackendClient(
        base_url="http://backend.test",
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )


def json_response(data: Any, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=data,
        headers={"content-type": "application/json"},
    )


def sample_devices() -> Dict[str, Any]:
    return {
        "source": "netcon",
        "stale": False,
        "captured_at": 1793359000.0,
        "count": 2,
        "devices": [
            {
                "id": "CAT10109K2T-uuid",
                "hostname": "Core-3650",
                "management_ip": "192.168.10.1",
                "reachability": "Reachable",
                "collection_status": "Managed",
            },
            {
                "id": "CAT1010BLK9-uuid",
                "hostname": "SW-JX-2960",
                "management_ip": "192.168.10.11",
                "reachability": "Reachable",
                "collection_status": "Managed",
            },
        ],
    }


def sample_scene() -> Dict[str, Any]:
    return {
        "version": 1,
        "theme": {},
        "buildings": [
            {
                "id": "core",
                "name": "中心机房",
                "position": {"x": 0, "z": 0},
                "devices": [{"name": "Core-3650", "status": "confirmed"}],
            }
        ],
    }


class Recorder:
    """记录 MockTransport 收到的请求，便于断言 query/body。"""

    def __init__(self, responses: Dict[Tuple[str, str], Any], status: int = 200):
        self.responses = responses
        self.status = status
        self.requests: List[Tuple[str, str, Dict[str, Any]]] = []
        self.last_body: Dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        body: Dict[str, Any] = {}
        if request.content:
            try:
                body = json.loads(request.content.decode("utf-8"))
            except ValueError:
                body = {}
        self.requests.append((request.method, request.url.path, params))
        self.last_body = body
        payload = self.responses.get((request.method, request.url.path))
        if payload is None:
            return httpx.Response(404, json={"detail": "not found"})
        return json_response(payload, self.status)


# --------------------------------------------------------------------------- #
# 工具：正常路径
# --------------------------------------------------------------------------- #
def test_tool_registry_has_exactly_ten_readonly_tools():
    assert len(core.TOOL_REGISTRY) == 10
    assert set(core.TOOL_REGISTRY) == {
        "get_devices",
        "get_topology",
        "get_hosts",
        "get_issues",
        "get_alerts",
        "get_alert_groups",
        "get_notify_status",
        "trace_path",
        "get_scene",
        "get_audit",
    }


def test_get_devices_returns_compact_json():
    recorder = Recorder({("GET", "/api/devices"): sample_devices()})
    result = core.tool_get_devices(make_client(recorder))
    assert result.startswith("{")
    assert json.loads(result)["count"] == 2
    # 紧凑格式：无多余空格
    assert ": " not in result and ", " not in result
    assert recorder.requests == [("GET", "/api/devices", {})]


@pytest.mark.parametrize(
    "tool_name, path",
    [
        ("get_topology", "/api/topology"),
        ("get_hosts", "/api/hosts"),
        ("get_issues", "/api/issues"),
        ("get_scene", "/api/scene"),
        ("get_notify_status", "/api/notify/status"),
        ("get_alert_groups", "/api/alerts/groups"),
    ],
)
def test_simple_get_tools(tool_name: str, path: str):
    payload = {"ok": True, "path": path}
    recorder = Recorder({("GET", path): payload})
    result = core.TOOL_REGISTRY[tool_name][0](client=make_client(recorder))
    assert json.loads(result)["path"] == path
    assert recorder.requests[0][0] == "GET"


def test_get_alerts_passes_state_and_limit():
    payload = {"count": 0, "alerts": [], "state": "active"}
    recorder = Recorder({("GET", "/api/alerts"): payload})
    result = core.tool_get_alerts("cleared", client=make_client(recorder))
    assert json.loads(result)["count"] == 0
    method, path, params = recorder.requests[0]
    assert (method, path) == ("GET", "/api/alerts")
    assert params == {"state": "cleared", "limit": "50"}


def test_get_alerts_rejects_invalid_state_without_request():
    recorder = Recorder({})
    result = core.tool_get_alerts("deleted", client=make_client(recorder))
    assert result.startswith("错误：")
    assert recorder.requests == []


def test_get_audit_limit_is_clamped():
    recorder = Recorder({("GET", "/api/audit"): {"count": 0, "items": []}})
    core.tool_get_audit(500, client=make_client(recorder))
    assert recorder.requests[0][2]["limit"] == "100"
    recorder2 = Recorder({("GET", "/api/audit"): {"count": 0, "items": []}})
    core.tool_get_audit(-5, client=make_client(recorder2))
    assert recorder2.requests[0][2]["limit"] == "1"


def test_trace_path_posts_ips_and_returns_json():
    payload = {"ok": True, "trace_status": "complete", "hops": [], "links": []}
    recorder = Recorder({("POST", "/api/path-trace"): payload})
    result = core.tool_trace_path(
        " 192.168.20.10 ", "192.168.50.10", client=make_client(recorder)
    )
    body = json.loads(result)
    assert body["ok"] is True
    method, path, _ = recorder.requests[0]
    assert (method, path) == ("POST", "/api/path-trace")
    # 请求体 IP 已 strip
    assert recorder.last_body == {
        "source_ip": "192.168.20.10",
        "dest_ip": "192.168.50.10",
    }


def test_trace_path_rejects_empty_ip():
    recorder = Recorder({})
    result = core.tool_trace_path("", "1.1.1.1", client=make_client(recorder))
    assert result.startswith("错误：")
    assert recorder.requests == []


# --------------------------------------------------------------------------- #
# 工具：后端异常降级（绝不抛异常）
# --------------------------------------------------------------------------- #
def test_backend_connection_error_returns_text():
    def raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = core.tool_get_devices(make_client(raise_connect_error))
    assert result.startswith("错误：")
    assert "无法连接后端服务" in result
    assert "Traceback" not in result


def test_backend_http_error_returns_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="database is locked")

    result = core.tool_get_devices(make_client(handler))
    assert result.startswith("错误：")
    assert "HTTP 500" in result


def test_backend_invalid_json_returns_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    result = core.tool_get_topology(make_client(handler))
    assert result.startswith("错误：")
    assert "非 JSON" in result


def test_run_tool_unknown_tool_returns_error():
    result = core.run_tool("not_a_tool")
    assert result.startswith("错误：未知工具")


def test_default_client_can_be_injected():
    recorder = Recorder({("GET", "/api/devices"): sample_devices()})
    core.set_default_client(make_client(recorder))
    try:
        result = core.tool_get_devices()  # 不显式传 client
        assert json.loads(result)["count"] == 2
    finally:
        core.set_default_client(None)


# --------------------------------------------------------------------------- #
# Resources：白名单与降级
# --------------------------------------------------------------------------- #
def test_doc_resource_whitelist_allows_network_design():
    text = core.read_doc_resource("campus://docs/network-design")
    assert not text.startswith("错误：")
    assert len(text) > 500
    assert "Core-3650" in text or "校园网" in text


def test_doc_resource_whitelist_allows_troubleshooting():
    text = core.read_doc_resource("campus://docs/troubleshooting")
    assert not text.startswith("错误：")
    assert len(text) > 300


def test_doc_resource_rejects_unknown_uri():
    text = core.read_doc_resource("campus://docs/secrets")
    assert text.startswith("错误：")
    assert "白名单" in text


def test_doc_resource_rejects_path_traversal():
    for uri in (
        "../../backend/data/campus_twin.db",
        "campus://docs/../../backend/data/campus_twin.db",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
    ):
        text = core.read_doc_resource(uri)
        assert text.startswith("错误：")
        assert "白名单" in text


def test_scene_and_topology_resources_fetch_backend():
    recorder = Recorder(
        {
            ("GET", "/api/scene"): sample_scene(),
            ("GET", "/api/topology"): {"nodes": [], "links": []},
        }
    )
    scene = core.read_scene_resource(make_client(recorder))
    topology = core.read_topology_resource(make_client(recorder))
    assert json.loads(scene)["version"] == 1
    assert json.loads(topology)["nodes"] == []


def test_resource_returns_error_text_when_backend_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    text = core.read_scene_resource(make_client(handler))
    assert text.startswith("错误：")
    assert "无法连接后端服务" in text


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def test_troubleshoot_port_down_prompt_mentions_tools_and_params():
    text = core.prompt_troubleshoot_port_down("SW-JX-2960", "FastEthernet0/24")
    assert "SW-JX-2960" in text
    assert "FastEthernet0/24" in text
    for tool_name in ("get_devices", "get_topology", "get_hosts", "get_alerts"):
        assert tool_name in text
    assert "只读" in text


def test_troubleshoot_link_down_prompt():
    text = core.prompt_troubleshoot_link_down("Core-3650", "SW-JX-2960")
    assert "Core-3650" in text and "SW-JX-2960" in text
    assert "get_alert_groups" in text
    assert "trace_path" in text


def test_onboard_device_prompt_mentions_all_params():
    text = core.prompt_onboard_device("AP-Teach-1", "teach", "AIR-AP2802I")
    assert "AP-Teach-1" in text
    assert "teach" in text
    assert "AIR-AP2802I" in text
    assert "campus://docs/network-design" in text


def test_prompt_registry_complete():
    assert set(core.PROMPTS) == {
        "troubleshoot_port_down",
        "troubleshoot_link_down",
        "onboard_device",
    }


# --------------------------------------------------------------------------- #
# 安全边界
# --------------------------------------------------------------------------- #
def test_core_only_exposes_readonly_backend_calls():
    """核心工具只允许 GET + path-trace 的 POST；源码中不出现写接口路径。"""
    source = (MCP_SERVER_DIR / "core.py").read_text(encoding="utf-8")
    write_paths = [
        "/api/alerts/clear",
        "/api/alerts/groups/{",
        "/silence",
        "/api/scene/buildings",
        "/api/scene/devices",
        "/api/alerts/{",
    ]
    for path in write_paths:
        assert path not in source
    # 唯一 POST 是 trace_path
    assert source.count(".post_json(") == 1  # 仅 trace_path 调用一次，无其它 POST
    assert '/api/path-trace' in source


def test_missing_doc_file_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DOCS_DIR", str(tmp_path))
    text = core.read_doc_resource("campus://docs/network-design")
    assert text.startswith("错误：")
    assert "不存在" in text
