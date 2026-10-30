# -*- coding: utf-8 -*-
"""W15-P1：断点恢复演示（interrupt 暂停 → 进程重启 → SqliteSaver 恢复 → 确认执行）。

用法（离线，mock provider + fixture 后端）::

    python -m app.agent.recovery_demo            # 父进程：启动 start 子进程 → 退出 →
                                                 # 启动 resume 子进程 → 确认执行
    python -m app.agent.recovery_demo --phase start --workdir <dir>
    python -m app.agent.recovery_demo --phase resume --workdir <dir>

设计：
- start 子进程创建 LangGraph 图，ask「把已恢复的告警清空」→ 规则路由生成 pending action，
  图在 ``wait_confirm`` 的 ``interrupt()`` 暂停；写 ``state.json`` 后**进程退出**；
- resume 子进程用同一 conversation_id + 同一 SqliteSaver 文件重新建图，先验证图仍暂停，
  再 ``confirm_action`` → ``Command(resume=...)`` 恢复并执行；
- 父进程合并两阶段事件写 ``app/agent/eval/recovery_report.json``（含 pid、耗时、暂停/恢复标记）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]  # agent/
REPORT_PATH = ROOT / "app" / "agent" / "eval" / "recovery_report.json"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _alert(index: int) -> Dict[str, Any]:
    return {
        "id": 700 + index,
        "alert_type": "device_unreachable",
        "severity": "warning",
        "title": "设备采集异常：SW-SX-2960 #%d" % index,
        "detail": "reachabilityStatus=Reachable，数据源实际状态已恢复，等待 NC 同步；" * 3,
        "device_id": "CAT10101HTH-uuid",
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


def _settings(workdir: Path):
    from app.config import AgentSettings

    settings = AgentSettings.from_env()
    return replace(
        settings,
        db_path=workdir / "agent.db",
        graph_db_path=workdir / "graph-checkpoint.db",
    )


def _build_engine(workdir: Path):
    from app.agent.actions import ActionExecutor, ActionStore
    from app.agent.backend import FixtureBackendClient
    from app.agent.conversations import ConversationStore
    from app.agent.eval.fixture_data import build_scenario_snapshot, load_base_snapshot
    from app.agent.graph.graph import LangGraphEngine
    from app.agent.providers.mock import MockProvider
    from app.agent.tools import OpsToolkit

    snapshot = build_scenario_snapshot(
        load_base_snapshot(), {"id": "recovery_demo", "w9": {"alerts": [_alert(i) for i in range(3)]}}
    )
    settings = _settings(workdir)
    backend = FixtureBackendClient(snapshot)
    conversations = ConversationStore(settings.db_path)
    engine = LangGraphEngine(
        MockProvider(),
        OpsToolkit(backend),
        conversations,
        ActionStore(settings.db_path, ttl_s=settings.action_ttl_s),
        settings,
        executor=ActionExecutor(backend, conversations),
        graph_db_path=settings.graph_db_path,
    )
    return engine, settings


def phase_start(workdir: Path) -> Dict[str, Any]:
    started = time.perf_counter()
    engine, _settings_obj = _build_engine(workdir)
    outcome = engine.handle("把已恢复的告警清空")
    actions = list(getattr(outcome, "actions", None) or [])
    pending = [item for item in actions if str(item.get("status") or "") == "pending"]
    action = pending[0] if pending else None
    snapshot = engine._graph.get_state(engine._graph_config(outcome.conversation_id))
    paused = bool(snapshot.next)
    state = {
        "workdir": str(workdir),
        "conversation_id": outcome.conversation_id,
        "action_id": (action or {}).get("id"),
        "action_type": (action or {}).get("type"),
        "paused": paused,
        "route_source": (outcome.trace or {}).get("route_source"),
        "process": "start",
        "pid": os.getpid(),
        "started_at": _now(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "pending_count": len(pending),
    }
    (workdir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    engine.close()
    state["event"] = "interrupt_paused"
    print("PHASE_RESULT_JSON:" + json.dumps(state, ensure_ascii=False), flush=True)
    return state


def phase_resume(workdir: Path) -> Dict[str, Any]:
    from app.agent.graph.graph import LangGraphEngine

    started = time.perf_counter()
    state_path = workdir / "state.json"
    if not state_path.exists():
        raise SystemExit("缺少 state.json，请先运行 --phase start")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    engine, _settings_obj = _build_engine(workdir)
    config = engine._graph_config(state["conversation_id"])
    snapshot = engine._graph.get_state(config)
    paused_before = bool(snapshot.next)
    action = engine.confirm_action(state["action_id"]) if state.get("action_id") else None
    status = (action or {}).get("status")
    result = {
        "process": "resume",
        "pid": os.getpid(),
        "conversation_id": state["conversation_id"],
        "action_id": state.get("action_id"),
        "paused_before_resume": paused_before,
        "resumed": bool(paused_before),
        "executed": status == "executed",
        "action_status": status,
        "started_at": _now(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "event": "resumed_and_executed" if status == "executed" else "resume_incomplete",
    }
    engine.close()
    (workdir / "resume.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PHASE_RESULT_JSON:" + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def _run_phase(phase: str, workdir: Path) -> Dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "app.agent.recovery_demo",
        "--phase",
        phase,
        "--workdir",
        str(workdir),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise SystemExit(
            "recovery %s 子进程失败（%s）：\n%s\n%s"
            % (phase, proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:])
        )
    result: Dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        if line.startswith("PHASE_RESULT_JSON:"):
            result = json.loads(line[len("PHASE_RESULT_JSON:") :])
    if not result:
        raise SystemExit("recovery %s 子进程没有输出 PHASE_RESULT_JSON" % phase)
    result["child_returncode"] = proc.returncode
    return result


def run_all(workdir: Optional[Path] = None) -> Dict[str, Any]:
    started = time.perf_counter()
    own_workdir = workdir is None
    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="recovery-demo-", dir=str(ROOT / "data")))
    workdir.mkdir(parents=True, exist_ok=True)
    events: List[Dict[str, Any]] = []
    try:
        start = _run_phase("start", workdir)
        events.append({**start, "phase_order": 1, "process_boundary": "started"})
        resume = _run_phase("resume", workdir)
        events.append({**resume, "phase_order": 2, "process_boundary": "restarted"})
    finally:
        pass
    report = {
        "generated_at": _now(),
        "scenario": "写操作 interrupt 暂停 → 进程重启 → SqliteSaver 恢复 → 用户确认执行",
        "provider": "mock",
        "backend": "fixture",
        "process_restarted": True,
        "workdir": str(workdir),
        "events": events,
        "checks": {
            "paused_before_restart": bool(events[0].get("paused")),
            "new_process_pid": events[1].get("pid"),
            "start_process_pid": events[0].get("pid"),
            "paused_after_restart": bool(events[1].get("paused_before_resume")),
            "resumed": bool(events[1].get("resumed")),
            "executed": bool(events[1].get("executed")),
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(REPORT_PATH)
    if own_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="W15-P1 断点恢复演示")
    parser.add_argument("--phase", choices=["start", "resume", "all"], default="all")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args(argv)
    workdir = Path(args.workdir) if args.workdir else None
    if args.phase == "all":
        report = run_all(workdir)
        target = Path(args.report) if args.report else REPORT_PATH
        if target != REPORT_PATH:
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("=" * 72)
        print("W15-P1 断点恢复演示")
        print("  场景：interrupt 暂停 → 进程重启 → SqliteSaver 恢复 → 确认执行")
        print("  事件：")
        for event in report["events"]:
            print(
                "    phase=%s pid=%s paused=%s resumed=%s executed=%s action=%s %sms"
                % (
                    event.get("process"),
                    event.get("pid"),
                    event.get("paused") if event.get("process") == "start" else event.get("paused_before_resume"),
                    event.get("resumed"),
                    event.get("executed"),
                    event.get("action_id"),
                    event.get("elapsed_ms"),
                )
            )
        print("  checks:", json.dumps(report["checks"], ensure_ascii=False))
        print("  报告：%s" % report.get("report_path"))
        print("=" * 72)
        return 0
    if not workdir:
        raise SystemExit("--phase start/resume 需要 --workdir")
    if args.phase == "start":
        phase_start(workdir)
    else:
        phase_resume(workdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
