# -*- coding: utf-8 -*-
"""W12.2 回归：规则路由下 steward 必须按真实 cleared/active 数量确定性生成 pending action。

覆盖：真实库告警过多导致工具结果截断（>20000 字符）的场景、cleared=0、静默 active、
会话清理；classic + langgraph + SSE。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app.agent.actions import ActionExecutor, ActionStore
from app.agent.backend import FixtureBackendClient
from app.agent.conversations import ConversationStore
from app.agent.eval.fixture_data import build_scenario_snapshot
from app.agent.graph.graph import LangGraphEngine
from app.agent.providers.mock import MockProvider
from app.agent.roles.orchestrator import Orchestrator
from app.agent.tools import OpsToolkit
from app.config import AgentSettings


def _alert(index: int, state: str) -> Dict[str, Any]:
    return {
        "id": 500 + index,
        "alert_type": "device_unreachable",
        "severity": "warning",
        "title": "设备采集异常：SW-SX-2960 #%d" % index,
        "detail": (
            "reachabilityStatus=Reachable，collectionStatus=Unreachable（192.168.10.12）"
            " 数据源实际状态已恢复，等待 NC 采集周期同步；" * 12
        ),
        "device_id": "CAT10101HTH-uuid",
        "link_id": None,
        "issue_id": None,
        "created_at": 1793365000 + index,
        "state": state,
        "status": "open",
        "resolved_at": (1793365100 + index) if state == "cleared" else None,
        "clear_reason": ("数据源实际状态已恢复" if state == "cleared" else None),
        "silenced_at": None,
        "source": "netcon",
    }


def _scenario(count: int, state: str) -> Dict[str, Any]:
    return {
        "id": "tmp_w122",
        "w9": {"alerts": [_alert(index, state) for index in range(count)]},
    }


def _classic(snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path) -> Orchestrator:
    db = tmp_path / "classic.db"
    return Orchestrator(
        MockProvider(),
        OpsToolkit(FixtureBackendClient(snapshot)),
        ConversationStore(db),
        ActionStore(db),
        settings,
    )


def _graph(snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path) -> LangGraphEngine:
    db = tmp_path / "graph.db"
    backend = FixtureBackendClient(snapshot)
    conversations = ConversationStore(db)
    return LangGraphEngine(
        MockProvider(),
        OpsToolkit(backend),
        conversations,
        ActionStore(db),
        settings,
        executor=ActionExecutor(backend, conversations),
        graph_db_path=tmp_path / "graph-checkpoint.db",
    )


@pytest.mark.parametrize("engine", ["classic", "langgraph"])
def test_truncated_cleared_alerts_still_generate_action(
    base_snapshot: Dict[str, Any],
    settings: AgentSettings,
    tmp_path: Path,
    engine: str,
) -> None:
    """37 条长告警会把 get_alerts 结果截断（>20000 字符），仍必须生成 37 条清空 action。"""
    snapshot = build_scenario_snapshot(base_snapshot, _scenario(37, "cleared"))
    runner = _classic(snapshot, settings, tmp_path) if engine == "classic" else _graph(
        snapshot, settings, tmp_path
    )
    outcome = runner.handle("把已恢复的告警清空")
    assert outcome.trace["route_source"] == "rule"
    assert (outcome.trace.get("route") or {}).get("rule") == "write_clear_alerts"
    assert [item["role"] for item in outcome.trace["plan"]] == ["steward"]
    assert len(outcome.actions) == 1
    action = outcome.actions[0]
    assert action["type"] == "clear_cleared_alerts"
    assert action["status"] == "pending"
    assert "37 条已恢复告警" in action["confirm_text"]
    assert "37" in outcome.answer
    assert "待确认操作" in outcome.answer
    if engine == "langgraph":
        runner.close()


@pytest.mark.parametrize("engine", ["classic", "langgraph"])
def test_zero_cleared_alerts_no_empty_action(
    base_snapshot: Dict[str, Any],
    settings: AgentSettings,
    tmp_path: Path,
    engine: str,
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, {"id": "tmp_zero", "w9": {"alerts": []}})
    runner = _classic(snapshot, settings, tmp_path) if engine == "classic" else _graph(
        snapshot, settings, tmp_path
    )
    outcome = runner.handle("把已恢复的告警清空")
    assert outcome.trace["route_source"] == "rule"
    assert outcome.actions == []
    assert "当前没有已恢复告警可清空" in outcome.answer
    if engine == "langgraph":
        runner.close()


@pytest.mark.parametrize("engine", ["classic", "langgraph"])
def test_truncated_active_alerts_silence_actions(
    base_snapshot: Dict[str, Any],
    settings: AgentSettings,
    tmp_path: Path,
    engine: str,
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, _scenario(37, "open"))
    runner = _classic(snapshot, settings, tmp_path) if engine == "classic" else _graph(
        snapshot, settings, tmp_path
    )
    outcome = runner.handle("静默一下这些活跃告警")
    assert outcome.trace["route_source"] == "rule"
    assert (outcome.trace.get("route") or {}).get("action_hint") == "silence_alert"
    assert 1 <= len(outcome.actions) <= 3
    assert all(action["type"] == "silence_alert" for action in outcome.actions)
    assert all(action["status"] == "pending" for action in outcome.actions)
    if engine == "langgraph":
        runner.close()


def test_conversation_clear_action_still_generated(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, {"id": "tmp_conv", "w9": {"alerts": []}})
    db = tmp_path / "conv.db"
    conversations = ConversationStore(db)
    for index in range(2):
        conversations.create(title="历史会话 %d" % index)
    orchestrator = Orchestrator(
        MockProvider(),
        OpsToolkit(FixtureBackendClient(snapshot)),
        conversations,
        ActionStore(db),
        settings,
    )
    outcome = orchestrator.handle("清空所有历史会话")
    assert outcome.trace["route_source"] == "rule"
    assert (outcome.trace.get("route") or {}).get("rule") == "write_conversations"
    assert len(outcome.actions) == 1
    assert outcome.actions[0]["type"] == "clear_conversations"
    assert outcome.actions[0]["status"] == "pending"


def test_sse_carries_action_with_real_count(tmp_path: Any) -> None:
    from app.api import create_app

    settings = AgentSettings.from_env(
        env={
            "LLM_PROVIDER": "mock",
            "AGENT_DATA_SOURCE": "fixture",
            "AGENT_FIXTURE_SCENARIO": "s26",
            "AGENT_DB_PATH": str(tmp_path / "sse.db"),
            "AGENT_GRAPH_DB_PATH": str(tmp_path / "sse-graph.db"),
        }
    )

    def parse(response: Any) -> List[Dict[str, Any]]:
        import json

        events: List[Dict[str, Any]] = []
        buffer = ""
        for chunk in response.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                raw = raw.strip()
                if raw.startswith("data: "):
                    events.append(json.loads(raw[6:]))
        return events

    with TestClient(create_app(settings)) as client:
        with client.stream(
            "POST", "/api/chat/stream", json={"question": "把已恢复的告警清空"}
        ) as response:
            events = parse(response)
        actions = [event["action"] for event in events if event["type"] == "action"]
        done = [event for event in events if event["type"] == "done"][0]
        assert done["trace"]["route_source"] == "rule"
        assert len(actions) == 1
        assert actions[0]["type"] == "clear_cleared_alerts"
        assert "3 条已恢复告警" in actions[0]["confirm_text"]
        client.post("/api/actions/%s/cancel" % actions[0]["id"])
