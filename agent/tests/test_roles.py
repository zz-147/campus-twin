# -*- coding: utf-8 -*-
"""W9 子 Agent（scene / onboarding / recon / steward）与归属推断规则单元测试。"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

from app.agent.actions import ActionStore
from app.agent.backend import FixtureBackendClient
from app.agent.conversations import ConversationStore
from app.agent.eval.fixture_data import build_scenario_snapshot
from app.agent.inference import infer_building
from app.agent.knowledge import detect_building_name, detect_device_kind, render_cli
from app.agent.providers.mock import MockProvider
from app.agent.roles.orchestrator import Orchestrator
from app.agent.roles.scene import SceneRole
from app.agent.tools import OpsToolkit
from app.config import AgentSettings


def _orchestrator_for(
    scenario_id: str,
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_dir: Path,
) -> Orchestrator:
    snapshot = build_scenario_snapshot(base_snapshot, scenario_map[scenario_id])
    toolkit = OpsToolkit(FixtureBackendClient(snapshot))
    conversations = ConversationStore(tmp_dir / ("%s.db" % scenario_id))
    actions = ActionStore(tmp_dir / ("%s.db" % scenario_id))
    for seed in (scenario_map[scenario_id].get("w9") or {}).get("conversations_seed") or []:
        conversation = conversations.create(title=str(seed.get("title") or ""))
        for message in seed.get("messages") or []:
            conversations.append_message(
                conversation["id"], str(message.get("role") or "user"), str(message.get("content") or "")
            )
    return Orchestrator(MockProvider(), toolkit, conversations, actions, settings)


@pytest.fixture()
def make_orchestrator(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
):
    def build(scenario_id: str) -> Orchestrator:
        return _orchestrator_for(scenario_id, base_snapshot, scenario_map, settings, tmp_path)

    return build


# --------------------------------------------------------------------------- #
# Scene 角色（纯计算）
# --------------------------------------------------------------------------- #
def _scene_role(toolkit: OpsToolkit) -> SceneRole:
    return SceneRole(toolkit)


def test_scene_propose_device_position(base_snapshot: Dict[str, Any], toolkit: OpsToolkit) -> None:
    role = _scene_role(toolkit)
    scene = base_snapshot["scene"]
    dorm = next(item for item in scene["buildings"] if item["id"] == "dorm")
    placement = role.propose_device(scene, dorm, "SW-SS-2961", "switch", device_kind="2960")
    position = placement["position"]
    assert set(position.keys()) == {"x", "y", "z"}
    assert abs(position["x"] - 0.0) <= 40.0
    assert abs(position["z"] + 140.0) <= 20.0
    assert position["y"] > 10.0
    assert placement["status"] == "pending"
    assert placement["source"] == "agent"
    assert placement["type"] == "switch"


def test_scene_propose_building_contract(base_snapshot: Dict[str, Any], toolkit: OpsToolkit) -> None:
    role = _scene_role(toolkit)
    building = role.propose_building(base_snapshot["scene"], "图书馆")
    assert building["id"] == "bld-lib"
    assert building["name"] == "图书馆"
    assert set(building["position"].keys()) == {"x", "z"}
    assert set(building["size"].keys()) == {"width", "depth", "height"}
    assert set(building["style"].keys()) == {"color", "borderColor", "opacity"}
    assert building["status"] == "pending"


# --------------------------------------------------------------------------- #
# Onboarding 角色
# --------------------------------------------------------------------------- #
def test_onboarding_existing_building(make_orchestrator) -> None:
    outcome = make_orchestrator("s21").handle("我想在宿舍楼加一台 2960 交换机")
    assert len(outcome.actions) == 1
    action = outcome.actions[0]
    assert action["type"] == "apply_scene_device"
    assert action["status"] == "pending"
    assert action["danger"] is False
    assert "宿舍楼" in action["confirm_text"]
    params = action["params"]
    assert params["building_id"] == "dorm"
    device = params["device"]
    assert device["name"] == "SW-SS-2961"
    assert device["status"] == "pending"
    assert device["source"] == "agent"
    assert device["model"] == "Catalyst 2960"
    assert device["mgmt_ip"] == "192.168.10.14"
    assert set(device["position"].keys()) == {"x", "y", "z"}
    answer = outcome.answer
    for keyword in ("接入方案", "端口连线表", "完整 CLI 配置", "验证命令", "撤销命令"):
        assert keyword in answer
    assert "vlan 40" in answer
    assert "SW-SS-2961" in answer


def test_onboarding_teaching_and_training(make_orchestrator) -> None:
    teaching = make_orchestrator("s22").handle("教学楼要新增一台 2960 交换机，帮我给出接线表和完整配置。")
    assert teaching.actions[0]["params"]["device"]["name"] == "SW-JX-2961"
    assert teaching.actions[0]["params"]["building_id"] == "teach"
    training = make_orchestrator("s23").handle("实训楼再加一台 2960 交换机，给个接入方案。")
    assert training.actions[0]["params"]["device"]["name"] == "SW-SX-2961"
    assert training.actions[0]["params"]["building_id"] == "train"


def test_onboarding_new_building(make_orchestrator) -> None:
    outcome = make_orchestrator("s29").handle("我要在校园里新建一栋图书馆，放一台 2960 交换机。")
    assert len(outcome.actions) == 1
    action = outcome.actions[0]
    assert action["type"] == "apply_scene_building"
    assert action["danger"] is False
    building = action["params"]["building"]
    assert building["name"] == "图书馆"
    assert building["status"] == "pending"
    assert set(building["position"].keys()) == {"x", "z"}
    devices = action["params"]["devices"]
    assert len(devices) == 1
    assert devices[0]["name"] == "SW-LIB-1"
    assert "VLAN 60" in outcome.answer
    assert "新建" in outcome.answer


def test_onboarding_asks_for_building_when_missing(make_orchestrator) -> None:
    outcome = make_orchestrator("s21").handle("我想加一台 2960 交换机")
    assert outcome.actions == []
    assert "目标楼栋" in outcome.answer


# --------------------------------------------------------------------------- #
# Recon 角色
# --------------------------------------------------------------------------- #
def test_recon_pending_device_confirm_action(make_orchestrator) -> None:
    outcome = make_orchestrator("s24").handle("发现新设备了吗？帮我加到场景里。")
    assert len(outcome.actions) == 1
    action = outcome.actions[0]
    assert action["type"] == "confirm_scene_device"
    assert action["params"]["device_name"] == "SW-JX-2962"
    assert action["status"] == "pending"
    assert "SW-JX-2962" in outcome.answer
    assert "教学楼" in outcome.answer
    assert "pending" in outcome.answer


def test_recon_inventory_diff_inference_without_action(make_orchestrator) -> None:
    outcome = make_orchestrator("s25").handle("新发现一台设备 ACC-NEW-01（192.168.20.20），它属于哪个楼栋？")
    assert outcome.actions == []
    answer = outcome.answer
    assert "ACC-NEW-01" in answer
    assert "教学楼" in answer
    assert "管理 IP" in answer


def test_recon_unknown_device_asks_user(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    scenario = {
        "id": "tmp_recon",
        "category": "recon",
        "question": "发现新设备 ACC-UNKNOWN-01，它属于哪个楼栋？",
        "w9": {
            "new_devices": [
                {
                    "hostname": "ACC-UNKNOWN-01",
                    "management_ip": "10.99.0.5",
                    "platform_id": "2960",
                }
            ]
        },
    }
    snapshot = build_scenario_snapshot(base_snapshot, scenario)
    orchestrator = Orchestrator(
        MockProvider(),
        OpsToolkit(FixtureBackendClient(snapshot)),
        ConversationStore(tmp_path / "r.db"),
        ActionStore(tmp_path / "r.db"),
        settings,
    )
    outcome = orchestrator.handle(scenario["question"])
    assert outcome.actions == []
    assert "无法自动" in outcome.answer or "请告诉我" in outcome.answer
    assert "哪栋楼" in outcome.answer


# --------------------------------------------------------------------------- #
# Steward 角色
# --------------------------------------------------------------------------- #
def test_steward_clear_cleared_alerts(make_orchestrator) -> None:
    outcome = make_orchestrator("s26").handle("把已恢复的告警清空。")
    assert len(outcome.actions) == 1
    action = outcome.actions[0]
    assert action["type"] == "clear_cleared_alerts"
    assert action["danger"] is True
    assert action["status"] == "pending"
    assert "3 条已恢复告警" in action["confirm_text"]
    assert "已恢复" in outcome.answer
    assert "软删除" in action["confirm_text"]


def test_steward_active_alerts_cannot_be_deleted(make_orchestrator) -> None:
    outcome = make_orchestrator("s28").handle("把剩下的活跃告警全部删掉。")
    action_types = [action["type"] for action in outcome.actions]
    assert "delete_alert" not in action_types
    assert "clear_cleared_alerts" not in action_types
    assert action_types.count("silence_alert") == 2
    assert all(action["danger"] is False for action in outcome.actions)
    assert "不可删除" in outcome.answer
    assert "静默" in outcome.answer


def test_steward_clear_conversations(make_orchestrator) -> None:
    outcome = make_orchestrator("s27").handle("清空所有历史会话，只保留当前对话。")
    assert len(outcome.actions) == 1
    action = outcome.actions[0]
    assert action["type"] == "clear_conversations"
    assert action["params"]["keep_current"] == outcome.conversation_id
    assert action["danger"] is True
    assert "3 个历史会话" in action["confirm_text"]
    assert "保留当前会话" in action["confirm_text"]


def test_steward_audit_query(base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path) -> None:
    scenario = {
        "id": "tmp_audit",
        "question": "最近谁删了告警？给我看操作审计。",
        "w9": {
            "audit": [
                {"id": 1, "operator": "web-ui", "action": "alert.delete", "target": "19", "detail": "清理历史告警"},
                {"id": 2, "operator": "agent", "action": "scene.confirm", "target": "SW-SS-2961", "detail": "确认场景设备"},
            ]
        },
    }
    snapshot = build_scenario_snapshot(base_snapshot, scenario)
    orchestrator = Orchestrator(
        MockProvider(),
        OpsToolkit(FixtureBackendClient(snapshot)),
        ConversationStore(tmp_path / "audit.db"),
        ActionStore(tmp_path / "audit.db"),
        settings,
    )
    outcome = orchestrator.handle(scenario["question"])
    assert outcome.actions == []
    assert "操作审计" in outcome.answer
    assert "web-ui" in outcome.answer
    assert "alert.delete" in outcome.answer


# --------------------------------------------------------------------------- #
# 归属推断五级规则
# --------------------------------------------------------------------------- #
def test_inference_scene_existing(base_snapshot: Dict[str, Any]) -> None:
    result = infer_building(
        {"hostname": "SW-JX-2960", "management_ip": "192.168.10.11"},
        base_snapshot["scene"],
        base_snapshot["topology"]["links"],
        {},
    )
    assert result.building == "教学楼"
    assert result.rule == "scene_existing"
    assert result.confidence == "high"


def test_inference_topology_neighbor(base_snapshot: Dict[str, Any]) -> None:
    devices = [
        {"id": "dev-sw-jx-2960", "hostname": "SW-JX-2960", "management_ip": "192.168.10.11"},
        {"id": "dev-new", "hostname": "ACC-X-01", "management_ip": "192.168.99.9"},
    ]
    links = [
        {
            "source": "dev-sw-jx-2960",
            "target": "dev-new",
            "source_label": "SW-JX-2960",
            "target_label": "ACC-X-01",
        }
    ]
    index = {"sw-jx-2960": devices[0], "acc-x-01": devices[1]}
    result = infer_building(devices[1], base_snapshot["scene"], links, index)
    assert result.building == "教学楼"
    assert result.rule == "topology_neighbor"
    assert result.rule_index == 1


def test_inference_hostname_and_ip_rules(base_snapshot: Dict[str, Any]) -> None:
    by_hostname = infer_building(
        {"hostname": "SW-SS-2965", "management_ip": "10.99.0.1"}, base_snapshot["scene"], [], {}
    )
    assert by_hostname.building == "宿舍楼"
    assert by_hostname.rule == "hostname_keyword"
    by_ip = infer_building(
        {"hostname": "ACC-NEW-01", "management_ip": "192.168.20.20"}, base_snapshot["scene"], [], {}
    )
    assert by_ip.building == "教学楼"
    assert by_ip.rule == "ip_segment"
    assert by_ip.rule_index == 3


def test_inference_needs_user(base_snapshot: Dict[str, Any]) -> None:
    result = infer_building(
        {"hostname": "ACC-UNKNOWN-01", "management_ip": "10.99.0.5"}, base_snapshot["scene"], [], {}
    )
    assert result.building is None
    assert result.needs_user is True
    assert result.rule == "need_user"
    assert result.rule_index == 4
    assert result.question
    assert set(result.candidates) == {"中心机房", "教学楼", "实训楼", "宿舍楼"}


def test_inference_device_assignment_override(base_snapshot: Dict[str, Any]) -> None:
    scene = {
        "buildings": [
            {"id": "core", "name": "中心机房", "devices": []},
            {"id": "special", "name": "实验楼", "devices": []},
        ],
        "device_assignment": {
            "hostname_rules": [{"building_id": "special", "keywords": ["LAB"]}],
            "ip_building_map": {"172.16.9.": "special"},
        },
    }
    by_name = infer_building({"hostname": "SW-LAB-1", "management_ip": "1.1.1.1"}, scene, [], {})
    assert by_name.building == "实验楼"
    by_ip = infer_building({"hostname": "ACC-X", "management_ip": "172.16.9.8"}, scene, [], {})
    assert by_ip.building == "实验楼"


# --------------------------------------------------------------------------- #
# 知识库
# --------------------------------------------------------------------------- #
def test_knowledge_detectors() -> None:
    assert detect_device_kind("我想在宿舍楼加一台 2960 交换机") == "2960"
    assert detect_device_kind("核心用 3650") == "3650"
    assert detect_device_kind("加一个路由器") == "4331"
    assert detect_device_kind("新增一台服务器") == "Server"
    assert detect_building_name("我想在宿舍楼加一台 2960") == "宿舍楼"
    assert detect_building_name("新建一栋图书馆") == "图书馆"


def test_knowledge_cli_templates() -> None:
    switch_cli = render_cli(
        "2960", "SW-SS-2961", 40, "DORM", "192.168.10.14", "192.168.40.1", "Gi0/1", ["Fa0/1"]
    )
    assert "hostname SW-SS-2961" in switch_cli
    assert "ip default-gateway 192.168.40.1" in switch_cli
    assert "switchport trunk allowed vlan 40" in switch_cli
    core_cli = render_cli(
        "3650", "Core-3651", 10, "MGMT", "192.168.10.15", "192.168.10.1", "Gi1/0/9", []
    )
    assert "ip routing" in core_cli
    router_cli = render_cli(
        "4331", "Router-4332", 10, "MGMT", "10.0.0.6", "192.168.10.254", "Gi0/0/1", []
    )
    assert "interface Gi0/0/1" in router_cli
