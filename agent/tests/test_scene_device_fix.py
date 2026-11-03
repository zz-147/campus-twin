# -*- coding: utf-8 -*-
"""W15 修复：设备命名按类型选前缀 + 场景设备删除意图（含“刚加的设备”记忆）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from app.agent.actions import ActionExecutor, ActionStore
from app.agent.backend import FixtureBackendClient
from app.agent.conversations import ConversationStore
from app.agent.graph.graph import LangGraphEngine
from app.agent.knowledge import (
    device_name_prefix,
    next_device_hostname,
)
from app.agent.providers.mock import MockProvider
from app.agent.roles.orchestrator import Orchestrator
from app.agent.routing import route_question
from app.agent.tools import OpsToolkit
from app.config import AgentSettings


def test_device_name_prefix_and_next_hostname() -> None:
    assert device_name_prefix("PC", "教学楼") == "PC-JX"
    assert device_name_prefix("2960", "宿舍楼") == "SW-SS"
    assert device_name_prefix("ap", "教学楼") == "AP-"
    assert device_name_prefix("server", "中心机房") == "SRV-"
    assert device_name_prefix("4331", "中心机房") == "RTR-"
    assert device_name_prefix("controller", "中心机房") == "NC"
    # 已有 PC-JX1/PC-JX2（录像中的场景命名风格）→ PC-JX3
    assert next_device_hostname("PC", "教学楼", ["PC-JX1", "PC-JX2"]) == "PC-JX3"
    # 交换机沿用 SW-JX-2960 → SW-JX-2961，不与已有名冲突
    assert next_device_hostname("2960", "教学楼", ["SW-JX-2960"]) == "SW-JX-2961"
    assert next_device_hostname("PC", "宿舍楼", ["PC-SS1"]) == "PC-SS2"
    assert next_device_hostname("PC", "教学楼", ["PC-JX1", "PC-JX2", "PC-JX3"]) == "PC-JX4"


def test_scene_delete_route_rule() -> None:
    decision = route_question("把刚才加的设备删掉")
    assert decision.source == "rule"
    assert decision.action_hint == "remove_scene_device"
    assert decision.rule == "scene_delete"
    assert decision.plan and decision.plan[0][0] == "onboarding"
    # 反例：解释型问题不吞成删除动作
    assert route_question("删除设备后为什么场景里还有？").source == "llm"


def _orchestrator(
    snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> Dict[str, Any]:
    db = tmp_path / "agent.db"
    backend = FixtureBackendClient(snapshot)
    conversations = ConversationStore(db)
    actions = ActionStore(db, ttl_s=600)
    orchestrator = Orchestrator(
        MockProvider(),
        OpsToolkit(backend),
        conversations,
        actions,
        settings,
    )
    return {
        "orchestrator": orchestrator,
        "actions": actions,
        "conversations": conversations,
        "executor": ActionExecutor(backend, conversations),
    }


def test_add_pc_uses_pc_prefix(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    runner = _orchestrator(base_snapshot, settings, tmp_path)
    outcome = runner["orchestrator"].handle("教学楼加一台PC")
    assert len(outcome.actions) == 1
    action = outcome.actions[0]
    assert action["type"] == "apply_scene_device"
    assert action["params"]["device"]["name"] == "PC-JX3"
    assert action["params"]["device"]["type"] == "pc"
    assert "PC-JX3" in outcome.answer


def test_delete_just_added_device_uses_recent_action(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    runner = _orchestrator(base_snapshot, settings, tmp_path)
    add = runner["orchestrator"].handle("教学楼加一台PC")
    add_action = add.actions[0]
    assert add_action["params"]["device"]["name"] == "PC-JX3"
    # 确认新增，设备进入场景（然后新会话删除，模拟录像中的“刚加的设备”）
    runner["actions"].confirm(
        add_action["id"], runner["executor"], operator="agent"
    )
    scene = runner["executor"]._backend.get("/api/scene")
    names = [
        device.get("name")
        for building in scene.get("buildings") or []
        for device in building.get("devices") or []
    ]
    assert "PC-JX3" in names

    delete = runner["orchestrator"].handle("把刚才加的设备删掉")
    assert len(delete.actions) == 1
    remove_action = delete.actions[0]
    assert remove_action["type"] == "remove_scene_device"
    assert remove_action["danger"] is True
    assert remove_action["params"]["device_name"] == "PC-JX3"
    assert "PC-JX3" in delete.answer
    assert "确认执行" in delete.answer
    # 生成 action 后设备仍在，确认后才删除
    assert "PC-JX3" in [
        device.get("name")
        for building in runner["executor"]._backend.get("/api/scene").get("buildings") or []
        for device in building.get("devices") or []
    ]
    assert runner["actions"].get(remove_action["id"])["status"] == "pending"


def test_delete_unknown_device_lists_candidates_without_action(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    runner = _orchestrator(base_snapshot, settings, tmp_path)
    outcome = runner["orchestrator"].handle("删除设备 XYZ-NOT-EXIST")
    assert outcome.actions == []
    assert "需要确认删除哪台设备" in outcome.answer
    assert "PC-JX1" in outcome.answer  # 候选列表
    assert "未找到" in outcome.answer


def test_delete_explicit_existing_device(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    runner = _orchestrator(base_snapshot, settings, tmp_path)
    outcome = runner["orchestrator"].handle("删除场景设备 PC-JX1")
    assert len(outcome.actions) == 1
    action = outcome.actions[0]
    assert action["type"] == "remove_scene_device"
    assert action["params"]["device_name"] == "PC-JX1"
    assert action["danger"] is True


def test_langgraph_add_pc_and_delete_just_added(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    db = tmp_path / "graph-agent.db"
    backend = FixtureBackendClient(base_snapshot)
    conversations = ConversationStore(db)
    actions = ActionStore(db, ttl_s=600)
    engine = LangGraphEngine(
        MockProvider(),
        OpsToolkit(backend),
        conversations,
        actions,
        settings,
        executor=ActionExecutor(backend, conversations),
        graph_db_path=tmp_path / "graph-checkpoint.db",
    )
    add = engine.handle("教学楼加一台PC")
    assert add.actions and add.actions[0]["params"]["device"]["name"] == "PC-JX3"
    add_id = add.actions[0]["id"]
    # 确认新增（LangGraph interrupt resume）
    engine.confirm_action(add_id)
    # 新会话：图/动作存储都能回退到最近一次 apply_scene_device
    delete = engine.handle("把刚才加的设备删掉")
    assert delete.actions and delete.actions[0]["type"] == "remove_scene_device"
    assert delete.actions[0]["params"]["device_name"] == "PC-JX3"
    engine.close()


def test_scene_delete_rule_chinese_adjacent_device_name():
    """中文紧邻设备名（无空格）也必须命中 scene_delete（历史 \\b 正则 bug 回归测试）。"""
    from app.agent.routing import route_question

    cases = [
        "把PC-JX3从场景里删除",
        "把 PC-JX3 删掉",
        "删除PC-JX3",
        "把PC-JX3移除",
        "将 SW-JX-2961 从3D场景里去掉",
    ]
    for question in cases:
        decision = route_question(question)
        assert decision.rule == "scene_delete", f"{question} -> rule={decision.rule}"
        assert decision.action_hint == "remove_scene_device", f"{question} -> hint={decision.action_hint}"
