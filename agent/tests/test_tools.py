# -*- coding: utf-8 -*-
"""只读工具集：schema、真实 fixture 数据、过滤、容错。"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from app.agent.backend import BackendError, FixtureBackendClient
from app.agent.eval.fixture_data import build_scenario_snapshot
from app.agent.tools import OpsToolkit, build_tool_specs

EXPECTED_TOOLS = {
    "get_devices",
    "get_topology",
    "get_hosts",
    "get_issues",
    "get_alerts",
    "get_health",
    "get_device_detail",
    "get_metrics",
    "get_scene",
    "get_audit",
    "get_new_devices",
    "infer_building",
    "get_device_config_template",
    "trace_path",
    "get_alert_groups",
    "get_notify_status",
    "search_knowledge",
    "mcp_list_tools",
    "mcp_list_dir",
    "mcp_read_file",
    "mcp_search_files",
    "mcp_memory_search",
    "mcp_memory_add",
}


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #
def test_tool_schemas_count_and_shape(toolkit: OpsToolkit) -> None:
    schemas = toolkit.schemas()
    assert len(schemas) == 23
    names = {schema["function"]["name"] for schema in schemas}
    assert names == EXPECTED_TOOLS
    assert set(toolkit.tool_names()) == EXPECTED_TOOLS
    for schema in schemas:
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"]
        assert _has_chinese(function["description"])
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        assert isinstance(parameters.get("properties"), dict)
        assert parameters.get("additionalProperties") is False


def test_tool_schemas_are_copies(toolkit: OpsToolkit) -> None:
    first = toolkit.schemas()
    first[0]["function"]["name"] = "hacked"
    second = toolkit.schemas()
    assert second[0]["function"]["name"] != "hacked"


def test_tool_specs_contains_required_parameters() -> None:
    specs = {spec["function"]["name"]: spec["function"] for spec in build_tool_specs()}
    detail_required = specs["get_device_detail"]["parameters"]["required"]
    assert detail_required == ["device_id_or_name"]
    metrics_required = specs["get_metrics"]["parameters"]["required"]
    assert metrics_required == ["device_id_or_name"]
    trace_required = specs["trace_path"]["parameters"]["required"]
    assert trace_required == ["source_ip", "dest_ip"]


# --------------------------------------------------------------------------- #
# 正常路径（fixture 基线快照）
# --------------------------------------------------------------------------- #
def test_every_tool_returns_ok_envelope(toolkit: OpsToolkit) -> None:
    arguments: Dict[str, Dict[str, Any]] = {
        "get_health": {},
        "get_devices": {},
        "get_topology": {},
        "get_hosts": {},
        "get_issues": {},
        "get_alerts": {},
        "get_device_detail": {"device_id_or_name": "SW-JX-2960"},
        "get_metrics": {"device_id_or_name": "Core-3650", "limit": 10},
        "get_scene": {},
        "get_audit": {"limit": 5},
        "get_new_devices": {},
        "infer_building": {"device_id_or_name": "SW-JX-2960"},
        "get_device_config_template": {"device_kind": "2960", "building": "宿舍楼"},
        "trace_path": {"source_ip": "192.168.20.10", "dest_ip": "192.168.50.10"},
        "get_alert_groups": {},
        "get_notify_status": {},
        "search_knowledge": {"query": "2960 上联核心怎么配"},
    }
    for name, args in arguments.items():
        envelope = toolkit.call(name, args)
        assert envelope["ok"] is True, (name, envelope)
        assert envelope["tool"] == name
        assert envelope["data"] is not None
        assert _has_chinese(envelope["summary"])
        assert envelope["error"] is None
        assert envelope["elapsed_ms"] >= 0


def test_get_health_combines_service_and_network_health(toolkit: OpsToolkit) -> None:
    data = toolkit.call("get_health", {})["data"]
    assert data["service"]["status"] == "ok"
    assert data["service"]["stale"] is False
    assert data["network_health"]["total_devices"] == 5
    assert data["network_health"]["healthy_devices"] == 5
    assert data["data_source"] == "fixture"


def test_get_devices_has_real_inventory(toolkit: OpsToolkit) -> None:
    data = toolkit.call("get_devices", {})["data"]
    assert data["count"] == 5
    hostnames = {device["hostname"] for device in data["devices"]}
    assert {"Core-3650", "SW-JX-2960", "SW-SX-2960", "SW-SS-2960", "Router-4331"} == hostnames
    core = next(device for device in data["devices"] if device["hostname"] == "Core-3650")
    assert core["management_ip"] == "192.168.10.1"
    assert core["reachability"] == "Reachable"
    assert core["collection_status"] == "Managed"


def test_get_topology_resolves_labels_and_ports(toolkit: OpsToolkit) -> None:
    data = toolkit.call("get_topology", {})["data"]
    assert data["counts"] == {"nodes": 5, "links": 4}
    link = next(item for item in data["links"] if item["id"] == "link-core-jx")
    assert link["source_label"] == "Core-3650"
    assert link["target_label"] == "SW-JX-2960"
    assert link["start_port_name"] == "Gi1/0/1"
    assert link["end_port_name"] == "Fa0/24"
    assert link["link_status"] == "up"


def test_get_hosts_filters(toolkit: OpsToolkit) -> None:
    success = toolkit.call("get_hosts", {"ping_status": "SUCCESS"})["data"]
    assert success["count"] == 6
    failed = toolkit.call("get_hosts", {"ping_status": "FAILURE"})["data"]
    assert failed["count"] == 0
    teaching = toolkit.call("get_hosts", {"location": "SW-JX-2960"})["data"]
    assert teaching["count"] == 2
    hostnames = {host["hostname"] for host in teaching["hosts"]}
    assert hostnames == {"PC-JX1", "PC-JX2"}


def test_get_device_detail_by_name_and_id(toolkit: OpsToolkit) -> None:
    by_name = toolkit.call("get_device_detail", {"device_id_or_name": "SW-JX-2960"})
    assert by_name["ok"] is True
    data = by_name["data"]
    assert data["found"] is True
    assert data["device"]["management_ip"] == "192.168.10.11"
    assert data["hosts_count"] == 2
    assert {host["hostname"] for host in data["attached_hosts"]} == {"PC-JX1", "PC-JX2"}
    assert data["metrics_summary"]["peak_by_metric"]

    by_id = toolkit.call("get_device_detail", {"device_id_or_name": "dev-router-4331"})
    assert by_id["ok"] is True
    assert by_id["data"]["device"]["hostname"] == "Router-4331"


def test_get_device_detail_unknown_is_soft_failure(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("get_device_detail", {"device_id_or_name": "Not-Exist-9999"})
    assert envelope["ok"] is True
    assert envelope["data"]["found"] is False
    assert len(envelope["data"]["known_devices"]) == 5


def test_get_metrics_returns_peaks(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call(
        "get_metrics", {"device_id_or_name": "Core-3650", "limit": 15}
    )
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["device_id"] == "dev-core-3650"
    assert data["known_device"] is True
    assert data["count"] == 15
    assert "cpu_pct" in data["peak_by_metric"]
    assert "if_utilization_pct" in data["peak_by_metric"]
    assert "cpu_pct" in data["latest_by_metric"]


# --------------------------------------------------------------------------- #
# W9 新工具
# --------------------------------------------------------------------------- #
def test_get_scene_reads_contract(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("get_scene", {})
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["version"] == 1
    assert data["building_count"] == 4
    assert data["device_count"] == 12
    buildings = {building["name"]: building for building in data["buildings"]}
    assert set(buildings) == {"中心机房", "教学楼", "实训楼", "宿舍楼"}
    assert buildings["宿舍楼"]["id"] == "dorm"
    assert buildings["教学楼"]["device_count"] == 3


def test_get_new_devices_empty_on_baseline(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("get_new_devices", {})
    assert envelope["ok"] is True
    assert envelope["data"]["count"] == 0
    assert envelope["data"]["new_devices"] == []


def test_get_audit_reads_items_key(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("get_audit", {"limit": 5})
    assert envelope["ok"] is True
    assert envelope["data"]["count"] == 0


def test_infer_building_existing_device_uses_scene_rule(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("infer_building", {"device_id_or_name": "SW-JX-2960"})
    assert envelope["ok"] is True
    inference = envelope["data"]["inference"]
    assert inference["building"] == "教学楼"
    assert inference["rule"] == "scene_existing"
    assert inference["needs_user"] is False


def test_get_device_config_template_builds_cli(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call(
        "get_device_config_template", {"device_kind": "2960", "building": "宿舍楼"}
    )
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["device_kind"] == "2960"
    template = data["template"]
    assert "hostname" in template["cli"]
    assert "vlan 40" in template["cli"]
    assert template["cabling"][0]["peer_device"] == "Core-3650"
    assert any("show" in command for command in template["verification"])


def test_toolkit_whitelist_isolation(toolkit: OpsToolkit) -> None:
    scene_only = toolkit.restricted(["get_scene"])
    assert scene_only.tool_names() == ["get_scene"]
    assert len(scene_only.schemas()) == 1
    assert scene_only.call("get_scene", {})["ok"] is True
    blocked = scene_only.call("get_devices", {})
    assert blocked["ok"] is False
    assert "白名单" in blocked["error"]
    with pytest.raises(ValueError):
        toolkit.restricted(["not_a_tool"])


# --------------------------------------------------------------------------- #
# W10：路径追踪 / 告警根因分组 / 通知状态
# --------------------------------------------------------------------------- #
def test_trace_path_complete(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call(
        "trace_path", {"source_ip": "192.168.20.10", "dest_ip": "192.168.50.10"}
    )
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert data["trace_status"] == "complete"
    names = [hop["device_name"] for hop in data["hops"]]
    assert names == ["PC-JX1", "SW-JX-2960", "Core-3650", "Server-1"]
    assert data["hops"][0]["role"] == "source"
    assert data["hops"][-1]["role"] == "destination"
    assert data["links"][0]["link_id"] == "link-core-jx"
    assert "路径追踪完成" in envelope["summary"]


def test_trace_path_reports_breakpoint(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]]
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, scenario_map["s32"])
    tenant = OpsToolkit(FixtureBackendClient(snapshot))
    envelope = tenant.call(
        "trace_path", {"source_ip": "192.168.20.10", "dest_ip": "192.168.50.10"}
    )
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["trace_status"] == "partial"
    assert data["hops"][-1]["device_name"] == "SW-JX-2960"
    assert "Fa0/24" in data["error"] and "Gi1/0/1" in data["error"]
    assert "中断" in data["error"]


def test_trace_path_invalid_ip_is_soft_failure(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("trace_path", {"source_ip": "999.1.1.1", "dest_ip": "x"})
    assert envelope["ok"] is False
    assert "IPv4" in envelope["error"]
    missing = toolkit.call("trace_path", {"source_ip": "192.168.20.10"})
    assert missing["ok"] is False
    assert "缺少必填参数" in missing["error"]


def test_trace_path_unknown_host_is_soft_failure(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call(
        "trace_path", {"source_ip": "192.168.99.99", "dest_ip": "192.168.50.10"}
    )
    assert envelope["ok"] is False
    assert "未找到" in envelope["error"]


def test_get_alert_groups_empty(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("get_alert_groups", {})
    assert envelope["ok"] is True
    assert envelope["data"]["count"] == 0
    assert envelope["data"]["groups"] == []
    assert "0 个根因分组" in envelope["summary"]


def test_get_alert_groups_with_suppression(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]]
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, scenario_map["s33"])
    tenant = OpsToolkit(FixtureBackendClient(snapshot))
    envelope = tenant.call("get_alert_groups", {"state": "active"})
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["count"] == 2
    assert data["total_members"] == 4
    assert data["total_suppressed"] == 2
    root_group = next(group for group in data["groups"] if group["root_cause"] == "link_down")
    assert root_group["root_device_name"] == "Core-3650"
    assert root_group["suppressed_count"] == 2
    suppressed = [member for member in root_group["members"] if member["suppressed_by"]]
    assert len(suppressed) == 2
    assert all(member["suppressed_by"] == root_group["group_id"] for member in suppressed)


def test_get_notify_status_default(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("get_notify_status", {})
    assert envelope["ok"] is True
    assert envelope["data"]["enabled"] is False
    assert "未启用" in envelope["summary"]


def test_get_notify_status_enabled(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]]
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, scenario_map["s35"])
    tenant = OpsToolkit(FixtureBackendClient(snapshot))
    envelope = tenant.call("get_notify_status", {})
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["enabled"] is True
    assert data["type"] == "generic"
    assert data["min_severity"] == "critical"
    assert data["sent_count"] == 3
    assert "已启用" in envelope["summary"]


def test_w10_tools_graceful_when_backend_not_ready(base_snapshot: Dict[str, Any]) -> None:
    closed = OpsToolkit(FixtureBackendClient(base_snapshot, w10_available=False))
    for name, args in (
        ("trace_path", {"source_ip": "192.168.20.10", "dest_ip": "192.168.50.10"}),
        ("get_alert_groups", {}),
        ("get_notify_status", {}),
    ):
        envelope = closed.call(name, args)
        assert envelope["ok"] is False, name
        assert "404" in envelope["error"] or "未就绪" in envelope["error"]


# --------------------------------------------------------------------------- #
# 容错
# --------------------------------------------------------------------------- #
class _BrokenBackend:
    mode = "http"

    def get(self, path: str, params: Any = None) -> Dict[str, Any]:
        raise BackendError("后端不可达（http://127.0.0.1:8000）：ConnectError")

    def post(
        self,
        path: str,
        body: Any = None,
        operator: Any = None,
    ) -> Dict[str, Any]:
        raise BackendError("后端不可达（http://127.0.0.1:8000）：ConnectError")

    def delete(self, path: str, operator: Any = None) -> Dict[str, Any]:
        raise BackendError("后端不可达（http://127.0.0.1:8000）：ConnectError")


def test_tools_tolerate_backend_failure() -> None:
    toolkit = OpsToolkit(_BrokenBackend())
    assert toolkit.call("get_devices", {})["ok"] is False
    for name, args in {
        "get_health": {},
        "get_devices": {},
        "get_topology": {},
        "get_hosts": {},
        "get_issues": {},
        "get_alerts": {},
        "get_device_detail": {"device_id_or_name": "x"},
        "get_metrics": {"device_id_or_name": "x"},
        "get_scene": {},
        "get_audit": {},
        "get_new_devices": {},
        "infer_building": {"device_id_or_name": "x"},
        "trace_path": {"source_ip": "192.168.20.10", "dest_ip": "192.168.50.10"},
        "get_alert_groups": {},
        "get_notify_status": {},
    }.items():
        envelope = toolkit.call(name, args)
        assert envelope["ok"] is False, name
        error = str(envelope["error"])
        assert "后端" in error or "未找到" in error
    # 内置模板/知识库工具不依赖后端，后端不可用时仍可用
    template = toolkit.call("get_device_config_template", {"device_kind": "2960"})
    assert template["ok"] is True
    knowledge = toolkit.call("search_knowledge", {"query": "2960 上联核心"})
    assert knowledge["ok"] is True
    assert knowledge["data"]["data_source"] == "builtin_knowledge"


def test_unknown_tool_is_soft_failure(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("get_secret_data", {})
    assert envelope["ok"] is False
    assert "未知工具" in envelope["error"]


def test_missing_required_argument_is_soft_failure(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("get_device_detail", {})
    assert envelope["ok"] is False
    assert "缺少必填参数" in envelope["error"]
    metrics = toolkit.call("get_metrics", {"device_id_or_name": ""})
    assert metrics["ok"] is False


def test_bad_optional_arguments_use_defaults(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("get_alerts", {"limit": "not-a-number", "severity": "bogus"})
    assert envelope["ok"] is True
    assert envelope["data"]["count"] == 0


# --------------------------------------------------------------------------- #
# 排障类场景 fixture 注入后工具能看到故障数据（W9 场景在 test_eval 中覆盖）
# --------------------------------------------------------------------------- #
def test_scenario_snapshots_have_symptoms(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]]
) -> None:
    for scenario in scenario_map.values():
        if not scenario.get("fault"):
            continue
        snapshot = build_scenario_snapshot(base_snapshot, scenario)
        assert len(snapshot["issues"]["issues"]) >= 1, scenario["id"]
        assert len(snapshot["alerts"]["alerts"]) >= 1, scenario["id"]
        tenant = OpsToolkit(FixtureBackendClient(snapshot))
        issues = tenant.call("get_issues", {})
        alerts = tenant.call("get_alerts", {})
        assert issues["ok"] and alerts["ok"]
        assert issues["data"]["count"] >= 1
        assert alerts["data"]["count"] >= 1
        assert issues["data"]["source"] == "netcon"
