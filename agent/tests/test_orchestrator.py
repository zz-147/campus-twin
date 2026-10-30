# -*- coding: utf-8 -*-
"""主 Agent 编排器：意图路由、子任务计划、汇总、会话、action 安全边界。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from app.agent.actions import ActionStore
from app.agent.backend import FixtureBackendClient
from app.agent.conversations import ConversationStore
from app.agent.eval.fixture_data import build_scenario_snapshot
from app.agent.providers.mock import MockProvider
from app.agent.roles.orchestrator import (
    ChatOutcome,
    ConversationNotFoundError,
    Orchestrator,
    ProviderUnavailableError,
    classify_intent,
)
from app.agent.tools import OpsToolkit
from app.config import AgentSettings


@pytest.mark.parametrize(
    "question,expected",
    [
        ("我想在宿舍楼加一台 2960 交换机", "onboarding"),
        ("教学楼要新增一台 2960 交换机，怎么接线？", "onboarding"),
        ("我要在校园里新建一栋图书馆，放一台 2960 交换机", "onboarding"),
        ("发现新设备了吗？帮我加到场景里", "recon"),
        ("新发现一台设备 ACC-NEW-01 属于哪个楼栋", "recon"),
        ("把已恢复的告警清空", "steward"),
        ("清空所有历史会话", "steward"),
        ("最近谁删了告警？看下审计", "steward"),
        ("把剩下的活跃告警全部删掉", "steward"),
        ("教学楼网络为什么卡？", "diagnosis"),
        ("SW-JX-2960 的管理地址 ping 不通了", "diagnosis"),
        ("实训楼接入交换机 SW-SX-2960 看起来离线了，帮我确认", "diagnosis"),
        # W10 只读查询意图
        ("PC-JX1 到服务器 Server-1 的路径怎么走？", "path_trace"),
        ("从 PC-JX1 访问 Server-1 为什么访问不通？路径在哪里断了？", "path_trace"),
        ("现在有哪些告警？根因是什么？", "alert_groups"),
        ("有多少告警被上游抑制了？", "alert_groups"),
        ("告警通知配好了吗？Webhook 是什么状态？", "notify"),
        # 与诊断 Agent 的边界：不含两个端点的“访问不了”仍归诊断
        ("从教学楼访问不了服务器网段，怀疑默认路由没了，帮忙确认。", "diagnosis"),
        ("Server-1 访问不了，是不是服务器宕了？", "diagnosis"),
    ],
)
def test_classify_intent(question: str, expected: str) -> None:
    assert classify_intent(question) == expected


def test_plan_is_limited_to_three_subtasks(orchestrator: Orchestrator) -> None:
    plan = orchestrator.plan("发现新设备了吗？帮我加到场景里，顺便把已恢复的告警清空并删除历史对话")
    roles = [subtask.role for subtask in plan]
    assert len(plan) <= 3
    assert "steward" in roles
    assert "recon" in roles


def test_handle_persists_conversation(orchestrator: Orchestrator, conversation_store: ConversationStore) -> None:
    first = orchestrator.handle("我想在宿舍楼加一台 2960 交换机")
    assert isinstance(first, ChatOutcome)
    assert first.conversation_id is not None
    conversation = conversation_store.get(first.conversation_id)
    assert conversation is not None
    assert conversation["message_count"] == 2
    assert conversation["messages"][0]["role"] == "user"
    assert conversation["messages"][1]["role"] == "assistant"
    assert conversation["messages"][1]["meta"]["actions"]

    second = orchestrator.handle("那教学楼也加一台", conversation_id=first.conversation_id)
    assert second.conversation_id == first.conversation_id
    assert conversation_store.get(first.conversation_id)["message_count"] == 4


def test_handle_unknown_conversation(orchestrator: Orchestrator) -> None:
    with pytest.raises(ConversationNotFoundError):
        orchestrator.handle("你好", conversation_id="conv_missing")


def test_single_role_answer_and_trace(orchestrator: Orchestrator) -> None:
    outcome = orchestrator.handle("我想在宿舍楼加一台 2960 交换机")
    trace = outcome.trace
    assert trace["mode"] == "multi_agent"
    assert trace["roles"] == ["main", "onboarding", "scene", "recon", "steward"]
    assert trace["plan"][0]["role"] == "onboarding"
    assert trace["subtask_count"] == 1
    assert trace["actions_count"] == 1
    assert trace["action_ttl_s"] == 600
    assert trace["backend_mode"] == "fixture"
    assert trace["polish"]["used"] is False  # mock provider 不做 LLM 润色
    assert outcome.provider == "mock"
    assert outcome.model == "mock-rules"


def test_actions_are_pending_and_not_executed(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, scenario_map["s26"])
    backend = FixtureBackendClient(snapshot)
    orchestrator = Orchestrator(
        MockProvider(),
        OpsToolkit(backend),
        ConversationStore(tmp_path / "auto.db"),
        ActionStore(tmp_path / "auto.db"),
        settings,
    )
    outcome = orchestrator.handle("把已恢复的告警清空")
    assert outcome.actions
    assert all(action["status"] == "pending" for action in outcome.actions)
    # chat 阶段不执行：数据源状态不变
    assert backend.get("/api/alerts")["summary"]["cleared"] == 3


def test_compound_intent_merges_answers_and_actions(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    scenario = {
        "id": "tmp_compound",
        "question": "发现新设备了吗？帮我加到场景里，顺便把已恢复的告警清空。",
        "w9": {
            "alerts": [
                {"id": 201, "state": "cleared", "title": "已恢复：历史链路中断", "detail": "已恢复"},
                {"id": 202, "state": "cleared", "title": "已恢复：历史主机离线", "detail": "已恢复"},
            ],
            "new_devices": [
                {
                    "hostname": "SW-JX-2962",
                    "management_ip": "192.168.10.16",
                    "platform_id": "2960",
                    "in_scene": True,
                    "scene_building": "教学楼",
                }
            ],
        },
    }
    snapshot = build_scenario_snapshot(base_snapshot, scenario)
    orchestrator = Orchestrator(
        MockProvider(),
        OpsToolkit(FixtureBackendClient(snapshot)),
        ConversationStore(tmp_path / "compound.db"),
        ActionStore(tmp_path / "compound.db"),
        settings,
    )
    outcome = orchestrator.handle(scenario["question"])
    roles = [item["role"] for item in outcome.trace["plan"]]
    assert len(roles) == 2
    assert "steward" in roles and "recon" in roles
    assert outcome.trace["subtask_count"] == 2
    types = [action["type"] for action in outcome.actions]
    assert "clear_cleared_alerts" in types
    assert "confirm_scene_device" in types
    assert "# 综合处理方案" in outcome.answer
    assert "待确认操作" in outcome.answer


def test_w9_works_without_llm_provider_but_diagnosis_requires_it(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    orchestrator = Orchestrator(
        None,
        OpsToolkit(FixtureBackendClient(base_snapshot)),
        ConversationStore(tmp_path / "nollm.db"),
        ActionStore(tmp_path / "nollm.db"),
        settings,
    )
    onboarding = orchestrator.handle("我想在宿舍楼加一台 2960 交换机")
    assert onboarding.actions and onboarding.actions[0]["type"] == "apply_scene_device"
    with pytest.raises(ProviderUnavailableError):
        orchestrator.handle("教学楼网络为什么卡？")


def test_actions_are_deduplicated(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    scenario = {
        "id": "tmp_dedupe",
        "question": "发现新设备了吗？帮我加到场景里，并清空已恢复告警。",
        "w9": {
            "alerts": [{"id": 301, "state": "cleared", "title": "已恢复：A", "detail": "已恢复"}],
            "new_devices": [
                {
                    "hostname": "SW-JX-2962",
                    "management_ip": "192.168.10.16",
                    "platform_id": "2960",
                    "in_scene": True,
                    "scene_building": "教学楼",
                }
            ],
        },
    }
    snapshot = build_scenario_snapshot(base_snapshot, scenario)
    orchestrator = Orchestrator(
        MockProvider(),
        OpsToolkit(FixtureBackendClient(snapshot)),
        ConversationStore(tmp_path / "dedupe.db"),
        ActionStore(tmp_path / "dedupe.db"),
        settings,
    )
    outcome = orchestrator.handle(scenario["question"])
    keys = [
        (action["type"], sorted((action["params"] or {}).items()))
        for action in outcome.actions
    ]
    assert len(keys) == len(set(str(key) for key in keys))
    assert len(outcome.actions) <= 5


def test_diagnosis_subtask_does_not_lose_history(
    orchestrator: Orchestrator, conversation_store: ConversationStore
) -> None:
    first = orchestrator.handle("教学楼为什么卡？")
    assert first.trace["plan"][0]["role"] == "diagnosis"
    second = orchestrator.handle("那宿舍楼呢？", conversation_id=first.conversation_id)
    diagnosis = second.trace.get("diagnosis") or {}
    assert diagnosis.get("tool_sequence") is not None
    assert conversation_store.get(first.conversation_id)["message_count"] == 4


# --------------------------------------------------------------------------- #
# W10 只读查询（路径追踪 / 告警根因分组 / 通知状态）
# --------------------------------------------------------------------------- #
def _w10_orchestrator(
    snapshot: Dict[str, Any],
    settings: AgentSettings,
    tmp_path: Path,
    name: str = "w10",
    w10_available: bool = True,
) -> Orchestrator:
    return Orchestrator(
        MockProvider(),
        OpsToolkit(FixtureBackendClient(snapshot, w10_available=w10_available)),
        ConversationStore(tmp_path / ("%s.db" % name)),
        ActionStore(tmp_path / ("%s.db" % name)),
        settings,
    )


def test_path_trace_complete_via_orchestrator(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, scenario_map["s31"])
    outcome = _w10_orchestrator(snapshot, settings, tmp_path).handle(
        "PC-JX1 到服务器 Server-1 的路径怎么走？"
    )
    assert [item["role"] for item in outcome.trace["plan"]] == ["path_trace"]
    assert outcome.actions == []
    assert "路径追踪结果" in outcome.answer
    for keyword in ("PC-JX1", "SW-JX-2960", "Core-3650", "Server-1", "完整可达"):
        assert keyword in outcome.answer
    assert "get_hosts" in outcome.trace["tool_sequence"]
    assert "trace_path" in outcome.trace["tool_sequence"]
    assert outcome.trace["readonly_capabilities"] == ["path_trace", "alert_groups", "notify"]


def test_path_trace_breakpoint_via_orchestrator(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, scenario_map["s32"])
    outcome = _w10_orchestrator(snapshot, settings, tmp_path).handle(
        "从 PC-JX1 访问 Server-1 为什么访问不通？路径在哪里断了？"
    )
    assert outcome.actions == []
    assert "部分可达" in outcome.answer or "路径中断" in outcome.answer
    for keyword in ("断点", "Fa0/24", "Sw-JX-2960".replace("Sw", "SW")):
        assert keyword in outcome.answer


def test_path_trace_without_endpoints_asks_user(
    orchestrator: Orchestrator,
) -> None:
    outcome = orchestrator.handle("从教学楼访问不了服务器网段，路径怎么走？")
    assert [item["role"] for item in outcome.trace["plan"]] == ["path_trace"]
    assert outcome.actions == []
    assert "需要补充路径端点" in outcome.answer
    assert "PC-JX1" in outcome.answer  # 提示可用主机，但不编造路径


def test_alert_groups_via_orchestrator(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, scenario_map["s33"])
    outcome = _w10_orchestrator(snapshot, settings, tmp_path, "groups").handle(
        "现在有哪些告警？根因是什么？"
    )
    assert [item["role"] for item in outcome.trace["plan"]] == ["alert_groups"]
    assert outcome.actions == []
    for keyword in ("告警根因分组", "链路中断", "被抑制", "Core-3650"):
        assert keyword in outcome.answer
    assert "get_alert_groups" in outcome.trace["tool_sequence"]


def test_notify_status_via_orchestrator(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, scenario_map["s35"])
    outcome = _w10_orchestrator(snapshot, settings, tmp_path, "notify").handle(
        "告警通知配好了吗？Webhook 是什么状态？发个测试通知看看。"
    )
    assert [item["role"] for item in outcome.trace["plan"]] == ["notify"]
    assert outcome.actions == []  # 测试通知是写操作，本期不生成 action
    for keyword in ("告警通知状态", "已启用", "generic", "不会自动执行"):
        assert keyword in outcome.answer
    assert "get_notify_status" in outcome.trace["tool_sequence"]


def test_w10_unavailable_degrades_gracefully(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    orchestrator = _w10_orchestrator(
        base_snapshot, settings, tmp_path, "closed", w10_available=False
    )
    path_outcome = orchestrator.handle("PC-JX1 到服务器 Server-1 的路径怎么走？")
    assert path_outcome.actions == []
    assert "路径追踪暂不可用" in path_outcome.answer
    assert "404" in path_outcome.answer or "未就绪" in path_outcome.answer

    groups_outcome = orchestrator.handle("现在有哪些告警？根因是什么？")
    assert groups_outcome.actions == []
    assert "告警根因分组暂不可用" in groups_outcome.answer
    assert "回退查询" in groups_outcome.answer

    notify_outcome = orchestrator.handle("告警通知配好了吗？")
    assert notify_outcome.actions == []
    assert "通知状态暂不可用" in notify_outcome.answer


def test_w10_questions_without_llm_provider(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    orchestrator = Orchestrator(
        None,
        OpsToolkit(FixtureBackendClient(base_snapshot)),
        ConversationStore(tmp_path / "w10-nollm.db"),
        ActionStore(tmp_path / "w10-nollm.db"),
        settings,
    )
    path_outcome = orchestrator.handle("PC-JX1 到 Server-1 的路径怎么走？")
    assert [item["role"] for item in path_outcome.trace["plan"]] == ["path_trace"]
    assert path_outcome.actions == []
    assert "路径追踪" in path_outcome.answer
