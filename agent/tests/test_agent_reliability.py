# -*- coding: utf-8 -*-
"""W15-P1：Agent 可靠性 / HITL / 断点恢复演示测试。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from app.agent.actions import ActionExecutor, ActionStore
from app.agent.backend import FixtureBackendClient
from app.agent.conversations import ConversationStore
from app.agent.eval.fixture_data import build_scenario_snapshot
from app.agent.graph.graph import LangGraphEngine
from app.agent.providers.mock import MockProvider
from app.agent.recovery_demo import REPORT_PATH, run_all
from app.agent.tools import OpsToolkit
from app.config import AgentSettings


def _alert(index: int) -> Dict[str, Any]:
    return {
        "id": 800 + index,
        "alert_type": "device_unreachable",
        "severity": "warning",
        "title": "设备采集异常 #%d" % index,
        "detail": "数据源已恢复，等待 NC 同步",
        "device_id": "dev-1",
        "link_id": None,
        "issue_id": None,
        "created_at": 1793365000 + index,
        "state": "cleared",
        "status": "open",
        "resolved_at": 1793365100 + index,
        "clear_reason": "数据源实际状态已恢复",
        "silenced_at": None,
        "source": "netcon",
    }


def _engine(base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path) -> LangGraphEngine:
    snapshot = build_scenario_snapshot(
        base_snapshot,
        {"id": "w15_hitl", "w9": {"alerts": [_alert(i) for i in range(2)]}},
    )
    db = tmp_path / "agent.db"
    backend = FixtureBackendClient(snapshot)
    conversations = ConversationStore(db)
    return LangGraphEngine(
        MockProvider(),
        OpsToolkit(backend),
        conversations,
        ActionStore(db, ttl_s=600),
        settings,
        executor=ActionExecutor(backend, conversations),
        graph_db_path=tmp_path / "graph.db",
    )


def test_hitl_pending_action_is_not_auto_executed(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    engine = _engine(base_snapshot, settings, tmp_path)
    outcome = engine.handle("把已恢复的告警清空")
    assert outcome.actions
    action = outcome.actions[0]
    assert action["type"] == "clear_cleared_alerts"
    assert action["status"] == "pending"
    snapshot = engine._graph.get_state(engine._graph_config(outcome.conversation_id))
    assert snapshot.next  # 图停在 wait_confirm/interrupt
    # 未确认前保持 pending（没有自动执行）
    assert engine._actions.get(action["id"])["status"] == "pending"
    engine.close()


def test_hitl_confirm_executes_after_resume(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    engine = _engine(base_snapshot, settings, tmp_path)
    outcome = engine.handle("把已恢复的告警清空")
    action_id = outcome.actions[0]["id"]
    result = engine.confirm_action(action_id)
    assert result["status"] == "executed"
    engine.close()


def test_recovery_demo_runs_offline(tmp_path: Path) -> None:
    workdir = tmp_path / "recovery-demo"
    report = run_all(workdir)
    assert report["process_restarted"] is True
    checks = report["checks"]
    assert checks["paused_before_restart"] is True
    assert checks["paused_after_restart"] is True
    assert checks["resumed"] is True
    assert checks["executed"] is True
    assert checks["start_process_pid"] != checks["new_process_pid"]
    assert Path(report["report_path"]).exists()
    assert REPORT_PATH.exists()
