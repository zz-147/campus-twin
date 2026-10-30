# -*- coding: utf-8 -*-
"""35 场景评测：场景集完整性、故障/W9/W10 状态注入、打分口径、离线可重复。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.agent.backend import FixtureBackendClient
from app.agent.eval.fixture_data import (
    BASE_SNAPSHOT_PATH,
    FAULT_TYPES,
    W9_CATEGORIES,
    W10_CATEGORIES,
    build_scenario_snapshot,
    load_base_snapshot,
    load_scenarios,
)
from app.agent.eval.run_eval import run_evaluation, score_actions, score_answer
from app.agent.tools import OpsToolkit
from app.config import AgentSettings

DIAGNOSIS_REQUIRED_FIELDS = {
    "id",
    "category",
    "question",
    "expected_root_cause",
    "expected_device",
    "expected_evidence_keywords",
}
W9_REQUIRED_FIELDS = DIAGNOSIS_REQUIRED_FIELDS | {"expected_actions"}
ALL_CATEGORIES = {
    "host_down",
    "device_unreachable",
    "link_down",
    "port_shutdown",
    "routing_missing",
    "vlan_misconfig",
    "broadcast_storm",
    "high_cpu",
    "bandwidth_congestion",
    "onboarding",
    "recon",
    "steward",
    "new_building",
    "path_trace",
    "alert_groups",
    "notify",
    "knowledge",
}


# --------------------------------------------------------------------------- #
# 场景集结构
# --------------------------------------------------------------------------- #
def test_scenarios_file_has_38_valid_items(scenario_doc: Dict[str, Any]) -> None:
    scenarios = scenario_doc["scenarios"]
    assert len(scenarios) == 38
    ids = [scenario["id"] for scenario in scenarios]
    assert len(set(ids)) == 38
    for scenario in scenarios:
        assert DIAGNOSIS_REQUIRED_FIELDS <= set(scenario), scenario.get("id")
        assert scenario["question"].strip()
        assert isinstance(scenario["expected_evidence_keywords"], list)
        assert isinstance(scenario["expected_root_cause_keywords"], list)
        if scenario["category"] in W9_CATEGORIES + W10_CATEGORIES:
            assert W9_REQUIRED_FIELDS <= set(scenario), scenario["id"]
            assert isinstance(scenario["expected_actions"], list)
        fault = scenario.get("fault") or {}
        if fault:
            assert fault["type"] in FAULT_TYPES, scenario["id"]
    assert scenario_doc["meta"]["total"] == 38


def test_scenario_categories_cover_required_faults(scenario_doc: Dict[str, Any]) -> None:
    categories = {scenario["category"] for scenario in scenario_doc["scenarios"]}
    assert categories == ALL_CATEGORIES
    w9_count = sum(1 for item in scenario_doc["scenarios"] if item["category"] in W9_CATEGORIES)
    w10_count = sum(1 for item in scenario_doc["scenarios"] if item["category"] in W10_CATEGORIES)
    knowledge_count = sum(
        1 for item in scenario_doc["scenarios"] if item["category"] == "knowledge"
    )
    assert w9_count == 10
    assert w10_count == 5
    assert knowledge_count == 3


def test_scenario_fault_references_exist_in_base(
    base_snapshot: Dict[str, Any], scenario_doc: Dict[str, Any]
) -> None:
    device_names = {item["hostname"] for item in base_snapshot["devices"]["devices"]}
    device_ids = {item["id"] for item in base_snapshot["devices"]["devices"]}
    host_names = {item["hostname"] for item in base_snapshot["hosts"]["hosts"]}
    link_ids = {item["id"] for item in base_snapshot["topology"]["links"]}
    for scenario in scenario_doc["scenarios"]:
        fault = scenario.get("fault") or {}
        if not fault:
            continue
        if "hostname" in fault:
            assert fault["hostname"] in host_names, scenario["id"]
        if fault.get("type") == "link_down":
            assert fault["link_id"] in link_ids, scenario["id"]
        if "device" in fault and fault["type"] != "link_down":
            assert fault["device"] in device_names | device_ids, scenario["id"]
        if "switch" in fault:
            assert fault["switch"] in device_names, scenario["id"]
    # W9 new building / onboarding 场景的 expected_device 是动态生成名或楼栋名，不做 base 校验


# --------------------------------------------------------------------------- #
# 基线快照 / 注入
# --------------------------------------------------------------------------- #
def test_base_snapshot_is_healthy_and_has_scene(base_snapshot: Dict[str, Any]) -> None:
    health = base_snapshot["network_health"]["network_health"]
    assert health["healthy_devices"] == 5
    assert health["unreachable_devices"] == 0
    assert all(host["ping_status"] == "SUCCESS" for host in base_snapshot["hosts"]["hosts"])
    assert all(link["link_status"] == "up" for link in base_snapshot["topology"]["links"])
    scene = base_snapshot["scene"]
    assert scene["version"] == 1
    assert len(scene["buildings"]) == 4
    assert sum(len(building["devices"]) for building in scene["buildings"]) == 12
    assert scene["device_assignment"]["ip_building_map"]["192.168.20."] == "teach"
    assert all(
        device["status"] == "confirmed"
        for building in scene["buildings"]
        for device in building["devices"]
    )
    assert base_snapshot["notify"]["enabled"] is False


def test_fault_injection_produces_expected_symptoms(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]]
) -> None:
    for scenario in scenario_map.values():
        fault = scenario.get("fault") or {}
        if not fault:
            continue
        snapshot = build_scenario_snapshot(base_snapshot, scenario)
        devices_by_name = {item["hostname"]: item for item in snapshot["devices"]["devices"]}
        assert len(snapshot["issues"]["issues"]) >= 1
        assert len(snapshot["alerts"]["alerts"]) >= 1
        fault_type = fault["type"]
        if fault_type == "host_down":
            host = next(
                item for item in snapshot["hosts"]["hosts"] if item["hostname"] == fault["hostname"]
            )
            assert host["ping_status"] == "FAILURE"
        elif fault_type == "device_unreachable":
            device = devices_by_name[fault["device"]]
            assert device["reachability"] == "Unreachable"
        elif fault_type == "link_down":
            link = next(
                item for item in snapshot["topology"]["links"] if item["id"] == fault["link_id"]
            )
            assert link["link_status"] == "down"
        elif fault_type == "port_shutdown":
            text = " ".join(issue["issue_description"] for issue in snapshot["issues"]["issues"])
            assert "shutdown" in text
        elif fault_type in ("broadcast_storm", "high_cpu", "bandwidth_congestion"):
            if fault_type == "broadcast_storm":
                device_id = devices_by_name[fault["switch"]]["id"]
                metric = "cpu_pct"
            elif fault_type == "high_cpu":
                device_id = devices_by_name[fault["device"]]["id"]
                metric = "cpu_pct"
            else:
                device_id = devices_by_name[fault["device"]]["id"]
                metric = "if_utilization_pct"
            peak = max(
                float(point["value"])
                for point in snapshot["metrics"][device_id]["metrics"]
                if point["metric"] == metric
            )
            assert peak >= 80.0


def test_w9_state_injection(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]]
) -> None:
    # s24：新设备进入场景 pending
    s24 = build_scenario_snapshot(base_snapshot, scenario_map["s24"])
    dorm_devices = [
        device
        for building in s24["scene"]["buildings"]
        for device in building["devices"]
        if device["status"] == "pending"
    ]
    assert [device["name"] for device in dorm_devices] == ["SW-JX-2962"]
    pending = FixtureBackendClient(s24).get("/api/scene/pending")
    assert pending["count"] == 1
    assert pending["devices"][0]["building_name"] == "教学楼"

    # s26：3 条已恢复 + 2 条活跃告警
    s26 = build_scenario_snapshot(base_snapshot, scenario_map["s26"])
    alerts = FixtureBackendClient(s26).get("/api/alerts")["alerts"]
    states = [alert["state"] for alert in alerts]
    assert states.count("cleared") == 3
    assert states.count("open") == 2

    # s27：历史会话种子在 run_eval 中创建（场景本身带 seed 数据）
    s27_seeds = scenario_map["s27"].get("w9", {}).get("conversations_seed", [])
    assert len(s27_seeds) == 3


def test_w10_state_injection(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]]
) -> None:
    # s35：通知状态覆盖为已启用
    s35 = build_scenario_snapshot(base_snapshot, scenario_map["s35"])
    assert s35["notify"]["enabled"] is True
    assert s35["notify"]["type"] == "generic"
    assert s35["notify"]["sent_count"] == 3

    # s33：告警根因分组 + 抑制（链路故障 + 下游主机告警）
    s33 = build_scenario_snapshot(base_snapshot, scenario_map["s33"])
    tenant = FixtureBackendClient(s33)
    groups = tenant.get("/api/alerts/groups", params={"state": "active"})
    assert groups["count"] == 2
    root_group = next(group for group in groups["groups"] if group["root_cause"] == "link_down")
    assert root_group["suppressed_count"] == 2

    # s32：断链路径追踪（partial + 断点端口）
    s32 = build_scenario_snapshot(base_snapshot, scenario_map["s32"])
    trace = FixtureBackendClient(s32).post(
        "/api/path-trace",
        {"source_ip": "192.168.20.10", "dest_ip": "192.168.50.10"},
        operator=None,
    )
    assert trace["trace_status"] == "partial"
    assert "Fa0/24" in trace["error"]

    # W10 场景全部只读：expected_actions 必须为空
    for scenario in scenario_map.values():
        if scenario["category"] in W10_CATEGORIES:
            assert scenario["expected_actions"] == [], scenario["id"]


# --------------------------------------------------------------------------- #
# 打分口径
# --------------------------------------------------------------------------- #
def test_score_answer_hit() -> None:
    scenario = {
        "expected_root_cause_keywords": ["终端主机离线", "Device is down"],
        "expected_device": "PC-JX1",
        "expected_device_ip": "192.168.20.10",
        "expected_evidence_keywords": ["PC-JX1", "FAILURE"],
    }
    scores = score_answer("根因：终端主机离线，PC-JX1（192.168.20.10）ping=FAILURE", scenario)
    assert scores["root_cause_hit"] is True
    assert scores["device_hit"] is True
    assert scores["evidence_hit_rate"] == 1.0
    assert scores["root_cause_matched_keyword"] == "终端主机离线"


def test_score_answer_miss() -> None:
    scenario = {
        "expected_root_cause_keywords": ["广播风暴"],
        "expected_device": "SW-JX-2960",
        "expected_device_ip": "192.168.10.11",
        "expected_evidence_keywords": ["广播", "CPU"],
    }
    scores = score_answer("一切正常，未发现异常。", scenario)
    assert scores["root_cause_hit"] is False
    assert scores["device_hit"] is False
    assert scores["evidence_hit_rate"] == 0.0


def test_score_actions_rules() -> None:
    scenario = {
        "expected_actions": ["apply_scene_device"],
        "expected_danger": False,
        "forbidden_actions": ["delete_alert"],
    }
    good = score_actions(
        [
            {
                "type": "apply_scene_device",
                "status": "pending",
                "danger": False,
                "confirm_text": "确认新增？",
            }
        ],
        scenario,
    )
    assert good["action_hit"] is True
    assert good["all_pending"] is True
    assert good["danger_ok"] is True
    assert good["confirm_text_ok"] is True

    bad = score_actions(
        [
            {
                "type": "delete_alert",
                "status": "executed",
                "danger": True,
                "confirm_text": "",
            }
        ],
        scenario,
    )
    assert bad["action_hit"] is False
    assert bad["forbidden_hit"] == ["delete_alert"]
    assert bad["all_pending"] is False

    no_actions = score_actions([], {"expected_actions": []})
    assert no_actions["action_hit"] is True

    unscored = score_actions([], {})
    assert unscored["scored"] is False


# --------------------------------------------------------------------------- #
# 端到端评测（mock，离线）
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def eval_report(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    report_path = tmp_path_factory.mktemp("eval") / "report.json"
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "mock"})
    return run_evaluation(
        settings=settings,
        provider_override="mock",
        report_path=report_path,
        progress=None,
    )


def test_run_evaluation_metrics(eval_report: Dict[str, Any]) -> None:
    assert eval_report["engine"] == "langgraph"
    assert eval_report["scenarios_total"] == 38
    assert eval_report["provider"] == "mock"
    metrics = eval_report["metrics"]
    assert metrics["root_cause_hits"] == 38
    assert metrics["root_cause_accuracy"] == 1.0
    assert metrics["device_hits"] == 38
    assert metrics["device_accuracy"] == 1.0
    assert metrics["evidence_hit_rate"] == 1.0
    assert metrics["provider_errors"] == 0
    assert metrics["tool_error_rate"] == 0.0
    action_metrics = eval_report["action_metrics"]
    assert action_metrics["scenarios_scored"] == 15
    assert action_metrics["action_hits"] == 15
    assert action_metrics["action_accuracy"] == 1.0
    assert action_metrics["danger_gate_rate"] == 1.0
    assert action_metrics["auto_execution_violations"] == 0
    assert len(eval_report["results"]) == 38
    for result in eval_report["results"]:
        assert result["error"] is None, result["id"]
        assert result["root_cause_hit"] is True, result["id"]
        assert result["device_hit"] is True, result["id"]
        for action in result["actions"]:
            assert action["status"] == "pending", result["id"]
        if result["category"] in W10_CATEGORIES:
            assert result["actions"] == [], result["id"]


def test_run_evaluation_category_metrics(eval_report: Dict[str, Any]) -> None:
    breakdown = eval_report["category_metrics"]
    assert set(breakdown) == ALL_CATEGORIES
    for category, bucket in breakdown.items():
        assert bucket["root_cause_accuracy"] == 1.0, category
        assert bucket["device_accuracy"] == 1.0, category
        if bucket["action_scored"]:
            assert bucket["action_accuracy"] == 1.0, category
    assert breakdown["host_down"]["total"] == 5
    assert breakdown["link_down"]["total"] == 4
    assert breakdown["onboarding"]["action_scored"] == 3
    assert breakdown["path_trace"]["action_scored"] == 2
    assert breakdown["alert_groups"]["action_scored"] == 2
    assert breakdown["notify"]["action_scored"] == 1
    assert breakdown["knowledge"]["total"] == 3
    assert breakdown["knowledge"]["action_scored"] == 0


def test_run_evaluation_writes_report(eval_report: Dict[str, Any]) -> None:
    path = Path(eval_report["report_path"])
    assert path.exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["metrics"]["root_cause_accuracy"] == 1.0
    assert loaded["action_metrics"]["action_accuracy"] == 1.0
    assert "results" in loaded


def test_run_evaluation_engine_switch(tmp_path: Path) -> None:
    """35 场景在 langgraph 与 classic 两种引擎下都必须 100%。"""
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "mock"})
    graph_report = run_evaluation(
        settings=settings,
        provider_override="mock",
        report_path=tmp_path / "langgraph.json",
        progress=None,
        engine="langgraph",
    )
    classic_report = run_evaluation(
        settings=settings,
        provider_override="mock",
        report_path=tmp_path / "classic.json",
        progress=None,
        engine="classic",
    )
    assert graph_report["engine"] == "langgraph"
    assert classic_report["engine"] == "classic"
    for report in (graph_report, classic_report):
        metrics = report["metrics"]
        assert metrics["root_cause_accuracy"] == 1.0
        assert metrics["device_accuracy"] == 1.0
        assert metrics["evidence_hit_rate"] == 1.0
        action_metrics = report["action_metrics"]
        assert action_metrics["scenarios_scored"] == 15
        assert action_metrics["action_hits"] == 15
        assert action_metrics["auto_execution_violations"] == 0
        assert len(report["results"]) == 38
        for result in report["results"]:
            assert result["error"] is None, result["id"]
            assert result["root_cause_hit"] is True, result["id"]


def test_run_evaluation_is_reproducible(tmp_path: Path) -> None:
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "mock"})
    first = run_evaluation(
        settings=settings, provider_override="mock", report_path=tmp_path / "a.json", progress=None
    )
    second = run_evaluation(
        settings=settings, provider_override="mock", report_path=tmp_path / "b.json", progress=None
    )

    def stable(metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in metrics.items() if key != "avg_elapsed_ms"}

    assert stable(first["metrics"]) == stable(second["metrics"])
    assert first["action_metrics"] == second["action_metrics"]
    for left, right in zip(first["results"], second["results"]):
        assert left["answer"] == right["answer"]
        assert left["root_cause_hit"] == right["root_cause_hit"]
        assert left["action_score"] == right["action_score"]


def test_report_contains_no_api_key(tmp_path: Path) -> None:
    secret = "sk-eval-secret-0123456789abcdef"
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "mock", "LLM_API_KEY": secret})
    report = run_evaluation(
        settings=settings, provider_override="mock", report_path=tmp_path / "report.json", progress=None
    )
    serialized = json.dumps(report, ensure_ascii=False)
    assert secret not in serialized
    assert "sk-" not in serialized
