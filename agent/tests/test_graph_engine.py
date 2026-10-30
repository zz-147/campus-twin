# -*- coding: utf-8 -*-
"""W11 LangGraph 引擎：StateGraph 路由、Send 并行、checkpointer、interrupt/confirm、可回退。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from app.agent.actions import ActionExecutor, ActionStore
from app.agent.backend import FixtureBackendClient
from app.agent.conversations import ConversationStore
from app.agent.eval.fixture_data import build_scenario_snapshot, load_base_snapshot
from app.agent.graph import AgentState, LangGraphEngine
from app.agent.graph.langchain_tools import build_diagnosis_tools, build_langchain_tools
from app.agent.providers.factory import build_provider_from_settings
from app.agent.providers.mock import MockProvider
from app.agent.roles.orchestrator import Orchestrator, ProviderUnavailableError
from app.agent.tools import ALL_TOOL_NAMES, DIAGNOSIS_TOOL_NAMES, OpsToolkit
from app.config import AgentSettings


def _scenario_snapshot(base_snapshot: Dict[str, Any], scenario: Dict[str, Any]) -> Dict[str, Any]:
    return build_scenario_snapshot(base_snapshot, scenario)


_UNSET = object()


def _build_engine(
    snapshot: Dict[str, Any],
    settings: AgentSettings,
    tmp_path: Path,
    name: str = "graph",
    provider: Any = _UNSET,
) -> LangGraphEngine:
    db_path = tmp_path / ("%s-agent.db" % name)
    conversations = ConversationStore(db_path)
    actions = ActionStore(db_path, ttl_s=settings.action_ttl_s)
    backend = FixtureBackendClient(snapshot)
    toolkit = OpsToolkit(backend)
    return LangGraphEngine(
        provider=MockProvider() if provider is _UNSET else provider,
        toolkit=toolkit,
        conversations=conversations,
        actions=actions,
        settings=settings,
        executor=ActionExecutor(backend, conversations),
        graph_db_path=tmp_path / ("%s-graph.db" % name),
    )


def _settings(tmp_path: Path, engine: str = "langgraph") -> AgentSettings:
    return AgentSettings.from_env(
        env={
            "LLM_PROVIDER": "mock",
            "AGENT_ENGINE": engine,
            "AGENT_DB_PATH": str(tmp_path / "agent.db"),
            "AGENT_GRAPH_DB_PATH": str(tmp_path / "graph.db"),
        }
    )


# --------------------------------------------------------------------------- #
# 配置 / 工具 / 信息
# --------------------------------------------------------------------------- #
def test_engine_config_switch() -> None:
    default = AgentSettings.from_env(env={"LLM_PROVIDER": "mock"})
    assert default.engine == "langgraph"
    classic = AgentSettings.from_env(env={"AGENT_ENGINE": "classic"})
    assert classic.engine == "classic"
    invalid = AgentSettings.from_env(env={"AGENT_ENGINE": "bogus"})
    assert invalid.engine == "langgraph"
    assert default.public_dict()["engine"] == "langgraph"
    assert "graph_db_path" in default.public_dict()


def test_langchain_tools_wrap_all_readonly_tools(toolkit: OpsToolkit) -> None:
    tools = build_langchain_tools(toolkit)
    names = [tool.name for tool in tools]
    assert names == list(ALL_TOOL_NAMES)
    diagnosis_tools = build_diagnosis_tools(toolkit)
    assert [tool.name for tool in diagnosis_tools] == list(DIAGNOSIS_TOOL_NAMES)
    hosts_tool = next(tool for tool in tools if tool.name == "get_hosts")
    result = hosts_tool.invoke({})
    assert isinstance(result, dict)
    assert result.get("count") == 6
    # 失败工具返回 ok=false（不抛异常）
    trace_tool = next(tool for tool in tools if tool.name == "trace_path")
    failed = trace_tool.invoke({"source_ip": "192.168.99.99", "dest_ip": "192.168.50.10"})
    assert failed.get("ok") is False


def test_engine_info_describes_graph(
    base_snapshot: Dict[str, Any], tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    engine = _build_engine(base_snapshot, settings, tmp_path)
    info = engine.info()
    assert info["engine"] == "langgraph"
    assert info["graph_version"] == "w11-langgraph-1"
    for node in ("classify", "diagnosis", "aggregate", "wait_confirm", "execute_action"):
        assert node in info["nodes"]
    assert info["checkpointer"]["type"] == "SqliteSaver"
    assert info["human_in_the_loop"]["mechanism"].startswith("interrupt()")
    assert info["tools"] == 23
    assert info["langchain_tools"] == 13
    assert info["diagnosis_backend"] == "mock provider (offline)"
    engine.close()


# --------------------------------------------------------------------------- #
# 路由 / 并行 / 只读
# --------------------------------------------------------------------------- #
def test_single_intent_routing_and_trace(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]], tmp_path: Path
) -> None:
    engine = _build_engine(base_snapshot, _settings(tmp_path), tmp_path, "single")
    outcome = engine.handle("PC-JX1 到服务器 Server-1 的路径怎么走？")
    assert [item["role"] for item in outcome.trace["plan"]] == ["path_trace"]
    assert outcome.trace["engine"] == "langgraph"
    assert outcome.trace["graph"]["interrupted"] is False
    assert outcome.actions == []
    assert "PC-JX1" in outcome.answer and "Server-1" in outcome.answer
    assert all(key in outcome.trace for key in ("plan", "steps", "tool_sequence", "graph"))
    assert outcome.trace["graph"]["checkpointer"]["type"] == "SqliteSaver"
    engine.close()


def test_parallel_fan_out_with_send(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    scenario = {
        "id": "tmp_parallel",
        "question": "发现新设备了吗？帮我加到场景里，顺便把已恢复的告警清空。",
        "w9": {
            "alerts": [
                {"id": 201, "state": "cleared", "title": "已恢复：A", "detail": "已恢复"},
                {"id": 202, "state": "cleared", "title": "已恢复：B", "detail": "已恢复"},
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
    snapshot = _scenario_snapshot(base_snapshot, scenario)
    engine = _build_engine(snapshot, settings, tmp_path, "parallel")
    outcome = engine.handle(scenario["question"])
    roles = [item["role"] for item in outcome.trace["plan"]]
    assert len(roles) == 2 and "steward" in roles and "recon" in roles
    node_events = [event["node"] for event in outcome.trace["graph"]["events"]]
    assert "steward" in node_events and "recon" in node_events
    assert outcome.trace["graph"]["interrupted"] is True
    assert len(outcome.actions) == 2
    engine.close()


def test_readonly_w10_questions_have_no_actions(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]], tmp_path: Path
) -> None:
    engine = _build_engine(
        _scenario_snapshot(base_snapshot, scenario_map["s33"]),
        _settings(tmp_path),
        tmp_path,
        "w10",
    )
    outcome = engine.handle("现在有哪些告警，根因是什么？")
    assert [item["role"] for item in outcome.trace["plan"]] == ["alert_groups"]
    assert outcome.actions == []
    assert "告警根因分组" in outcome.answer
    assert "get_alert_groups" in outcome.trace["tool_sequence"]
    engine.close()


def test_diagnosis_without_provider_raises(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    engine = _build_engine(base_snapshot, settings, tmp_path, "nollm", provider=None)
    with pytest.raises(ProviderUnavailableError):
        engine.handle("教学楼网络为什么卡？")
    engine.close()


# --------------------------------------------------------------------------- #
# interrupt / resume（human-in-the-loop）
# --------------------------------------------------------------------------- #
def _onboarding_engine(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
    name: str = "hitl",
) -> LangGraphEngine:
    snapshot = _scenario_snapshot(base_snapshot, scenario_map["s21"])
    return _build_engine(snapshot, settings, tmp_path, name)


def test_interrupt_pauses_graph_and_confirm_resumes(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
) -> None:
    engine = _onboarding_engine(base_snapshot, scenario_map, settings, tmp_path)
    outcome = engine.handle("我想在宿舍楼加一台 2960 交换机")
    assert outcome.trace["graph"]["interrupted"] is True
    assert outcome.trace["stop_reason"] == "interrupt"
    assert len(outcome.actions) == 1 and outcome.actions[0]["status"] == "pending"
    action_id = outcome.actions[0]["id"]
    # 图确实停在 wait_confirm（checkpoint 持久化）
    snapshot = engine._graph.get_state(engine._graph_config(outcome.conversation_id))
    assert snapshot.next == ("wait_confirm",)
    # 暂停时业务数据未变（场景里没有新设备）
    before = str(engine._toolkit.backend.get("/api/scene")["buildings"])
    assert "SW-SS-2961" not in before
    confirmed = engine.confirm_action(action_id)
    assert confirmed["status"] == "executed"
    assert engine._graph.get_state(engine._graph_config(outcome.conversation_id)).next == ()
    after = str(engine._toolkit.backend.get("/api/scene")["buildings"])
    assert "SW-SS-2961" in after
    engine.close()


def test_interrupt_cancel_does_not_execute(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
) -> None:
    engine = _onboarding_engine(base_snapshot, scenario_map, settings, tmp_path, "cancel")
    outcome = engine.handle("我想在宿舍楼加一台 2960 交换机")
    action_id = outcome.actions[0]["id"]
    cancelled = engine.cancel_action(action_id)
    assert cancelled["status"] == "cancelled"
    assert engine._graph.get_state(engine._graph_config(outcome.conversation_id)).next == ()
    scene = str(engine._toolkit.backend.get("/api/scene")["buildings"])
    assert "SW-SS-2961" not in scene
    engine.close()


def test_new_question_while_paused_returns_pending_actions(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
) -> None:
    engine = _onboarding_engine(base_snapshot, scenario_map, settings, tmp_path, "paused")
    first = engine.handle("我想在宿舍楼加一台 2960 交换机")
    second = engine.handle("那 PC-JX1 呢？", conversation_id=first.conversation_id)
    assert second.actions and second.actions[0]["id"] == first.actions[0]["id"]
    assert "等待确认" in second.answer
    assert second.conversation_id == first.conversation_id
    assert second.trace["graph"]["interrupted"] is True
    # 没有开启新回合：plan 仍是上一轮 onboarding
    assert [item["role"] for item in second.trace["plan"]] == ["onboarding"]
    engine.close()


def test_checkpointer_persists_across_engine_restarts(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
) -> None:
    snapshot = _scenario_snapshot(base_snapshot, scenario_map["s21"])
    first_engine = _build_engine(snapshot, settings, tmp_path, "persist")
    outcome = first_engine.handle("我想在宿舍楼加一台 2960 交换机")
    action_id = outcome.actions[0]["id"]
    first_engine.close()

    second_engine = _build_engine(snapshot, settings, tmp_path, "persist")
    resumed = second_engine.handle("有什么告警？", conversation_id=outcome.conversation_id)
    assert resumed.actions and resumed.actions[0]["id"] == action_id
    assert "等待确认" in resumed.answer
    confirmed = second_engine.confirm_action(action_id)
    assert confirmed["status"] == "executed"
    second_engine.close()


def test_multiturn_same_thread_and_conversation_history(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    engine = _build_engine(base_snapshot, settings, tmp_path, "multi")
    first = engine.handle("PC-JX1 到服务器 Server-1 的路径怎么走？")
    second = engine.handle("现在有哪些告警，根因是什么？", conversation_id=first.conversation_id)
    assert first.conversation_id == second.conversation_id
    assert [item["role"] for item in second.trace["plan"]] == ["alert_groups"]
    assert "告警根因分组" in second.answer
    conversation = engine._conversations.get(first.conversation_id)
    assert conversation["message_count"] == 4
    # checkpoint 线程与 conversation 一致
    state = engine._graph.get_state(engine._graph_config(first.conversation_id))
    assert state.values.get("conversation_id") == first.conversation_id
    engine.close()


# --------------------------------------------------------------------------- #
# classic 回退一致性
# --------------------------------------------------------------------------- #
def test_classic_engine_parity_on_readonly_questions(
    base_snapshot: Dict[str, Any],
    scenario_map: Dict[str, Dict[str, Any]],
    settings: AgentSettings,
    tmp_path: Path,
) -> None:
    scenario = scenario_map["s31"]
    snapshot = _scenario_snapshot(base_snapshot, scenario)
    engine = _build_engine(snapshot, settings, tmp_path, "parity")
    graph_outcome = engine.handle("PC-JX1 到服务器 Server-1 的路径怎么走？")
    engine.close()

    classic_db = tmp_path / "classic-agent.db"
    classic_conversations = ConversationStore(classic_db)
    classic_actions = ActionStore(classic_db, ttl_s=settings.action_ttl_s)
    classic = Orchestrator(
        provider=MockProvider(),
        toolkit=OpsToolkit(FixtureBackendClient(snapshot)),
        conversations=classic_conversations,
        actions=classic_actions,
        settings=settings,
    )
    classic_outcome = classic.handle("PC-JX1 到服务器 Server-1 的路径怎么走？")
    assert graph_outcome.answer == classic_outcome.answer
    assert graph_outcome.provider == classic_outcome.provider
    assert graph_outcome.actions == classic_outcome.actions
    assert [item["role"] for item in graph_outcome.trace["plan"]] == [
        item["role"] for item in classic_outcome.trace["plan"]
    ]
