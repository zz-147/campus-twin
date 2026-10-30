# -*- coding: utf-8 -*-
"""W12.1 规则优先路由：写操作确定性短路、反例保护、trace.route_source、5 次一致性。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.agent.actions import ActionExecutor, ActionStore
from app.agent.backend import FixtureBackendClient
from app.agent.conversations import ConversationStore
from app.agent.eval.fixture_data import build_scenario_snapshot
from app.agent.graph.graph import LangGraphEngine
from app.agent.providers.mock import MockProvider
from app.agent.roles.orchestrator import Orchestrator, classify_intent
from app.agent.routing import route_question
from app.agent.tools import OpsToolkit
from app.config import AgentSettings


def _route(question: str) -> Any:
    return route_question(question)


# --------------------------------------------------------------------------- #
# 规则正例
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question,intent,rule,hint",
    [
        ("把已恢复的告警清空", "steward", "write_clear_alerts", "clear_cleared_alerts"),
        ("清空所有历史告警", "steward", "write_clear_alerts", "clear_cleared_alerts"),
        ("删除已恢复告警", "steward", "write_clear_alerts", "clear_cleared_alerts"),
        ("清理一下告警列表", "steward", "write_clear_alerts", "clear_cleared_alerts"),
        ("移除这些告警", "steward", "write_clear_alerts", "clear_cleared_alerts"),
        ("clear all cleared alerts", "steward", "write_clear_alerts", "clear_cleared_alerts"),
        ("delete resolved alerts", "steward", "write_clear_alerts", "clear_cleared_alerts"),
        ("静默一下活跃告警", "steward", "write_silence_alerts", "silence_alert"),
        ("忽略这条告警", "steward", "write_silence_alerts", "silence_alert"),
        ("silence this alert", "steward", "write_silence_alerts", "silence_alert"),
        ("清空所有历史会话", "steward", "write_conversations", "clear_conversations"),
        ("删除当前会话", "steward", "write_conversations", "delete_conversation"),
        ("宿舍楼要接入一台 2960 交换机，上联核心怎么配置？", "onboarding", "onboarding", None),
        ("PC-JX1 到服务器 Server-1 的路径怎么走？", "path_trace", "path", None),
        ("从 192.168.20.10 到 192.168.50.10 为什么访问不通？", "path_trace", "path", None),
        ("现在有哪些告警？根因是什么？", "alert_groups", "alert_groups", None),
        ("告警通知配好了吗？Webhook 是什么状态？", "notify", "notify", None),
        ("发现新设备了吗？帮我加到场景里", "recon", "recon", None),
    ],
)
def test_rule_positive_routing(
    question: str, intent: str, rule: str, hint: Any
) -> None:
    decision = _route(question)
    assert decision.source == "rule", question
    assert decision.intent == intent
    assert rule in (decision.rule or "")
    assert decision.action_hint == hint
    assert [role for role, _ in decision.plan] == [intent]
    assert decision.confidence == 1.0
    assert classify_intent(question) == intent


def test_route_priority_compound_write_and_recon() -> None:
    decision = _route("发现新设备了吗？帮我加到场景里，顺便把已恢复的告警清空并删除历史对话")
    assert decision.source == "rule"
    roles = [role for role, _ in decision.plan]
    # plan 固定优先级：steward 在 recon 之前
    assert roles == ["steward", "recon"]
    assert decision.action_hint == "clear_cleared_alerts"


# --------------------------------------------------------------------------- #
# 规则反例（不能被吞成写操作 / 必须回退诊断）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question",
    [
        "删除告警后为什么还在",
        "最近谁删了告警？看下审计",
        "NC 告警为什么清除慢",
        "告警清除延迟的原因是什么",
        "从教学楼访问不了服务器网段，怀疑默认路由没了",
        "接入交换机 SW-SX-2960 看起来离线了，帮我确认",
        "为什么宿舍楼终端上不了网",
        "现在整体网络状态怎么样",
    ],
)
def test_rule_negative_falls_back_to_llm(question: str) -> None:
    decision = _route(question)
    assert decision.source == "llm", question
    assert decision.rule is None
    assert decision.plan == []


@pytest.mark.parametrize(
    "question,expected",
    [
        ("删除告警后为什么还在", "diagnosis"),
        ("最近谁删了告警？看下审计", "steward"),  # 原有启发式回退（审计类）
        ("现在整体网络状态怎么样", "diagnosis"),
        ("NC 告警为什么清除慢", "diagnosis"),
    ],
)
def test_fallback_classify_intent(question: str, expected: str) -> None:
    assert _route(question).source == "llm"
    assert classify_intent(question) == expected


# --------------------------------------------------------------------------- #
# 引擎确定性：同一写操作问题连续 5 次都生成 pending action
# --------------------------------------------------------------------------- #
def _settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings.from_env(env={"LLM_PROVIDER": "mock"})


def _scenario(base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return build_scenario_snapshot(base_snapshot, scenario_map["s26"])


def test_classic_write_intent_is_deterministic_5x(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]], tmp_path: Path
) -> None:
    snapshot = _scenario(base_snapshot, scenario_map)
    settings = _settings(tmp_path)
    for index in range(5):
        db = tmp_path / ("classic-%d.db" % index)
        orchestrator = Orchestrator(
            MockProvider(),
            OpsToolkit(FixtureBackendClient(snapshot)),
            ConversationStore(db),
            ActionStore(db),
            settings,
        )
        outcome = orchestrator.handle("把已恢复的告警清空")
        assert outcome.trace["route_source"] == "rule"
        assert outcome.trace["route"]["rule"] == "write_clear_alerts"
        assert [item["role"] for item in outcome.trace["plan"]] == ["steward"]
        assert len(outcome.actions) == 1, index
        assert outcome.actions[0]["type"] == "clear_cleared_alerts"
        assert outcome.actions[0]["status"] == "pending"


def test_graph_write_intent_is_deterministic_5x(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]], tmp_path: Path
) -> None:
    snapshot = _scenario(base_snapshot, scenario_map)
    settings = _settings(tmp_path)
    actions_seen: List[str] = []
    for index in range(5):
        db = tmp_path / ("graph-%d.db" % index)
        backend = FixtureBackendClient(snapshot)
        conversations = ConversationStore(db)
        engine = LangGraphEngine(
            MockProvider(),
            OpsToolkit(backend),
            conversations,
            ActionStore(db),
            settings,
            executor=ActionExecutor(backend, conversations),
            graph_db_path=tmp_path / ("graph-checkpoint-%d.db" % index),
        )
        outcome = engine.handle("把已恢复的告警清空")
        assert outcome.trace["route_source"] == "rule"
        assert outcome.trace["route"]["action_hint"] == "clear_cleared_alerts"
        assert [item["role"] for item in outcome.trace["plan"]] == ["steward"]
        assert len(outcome.actions) == 1, index
        actions_seen.append(outcome.actions[0]["type"])
        engine.close()
    assert actions_seen == ["clear_cleared_alerts"] * 5


def test_readonly_diagnosis_question_marks_llm_route(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    orchestrator = Orchestrator(
        MockProvider(),
        OpsToolkit(FixtureBackendClient(base_snapshot)),
        ConversationStore(tmp_path / "diag.db"),
        ActionStore(tmp_path / "diag.db"),
        settings,
    )
    outcome = orchestrator.handle("现在整体网络状态怎么样")
    assert outcome.trace["route_source"] == "llm"
    assert [item["role"] for item in outcome.trace["plan"]] == ["diagnosis"]
    assert outcome.actions == []