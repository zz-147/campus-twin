# -*- coding: utf-8 -*-
"""AI 运维 Agent 38 场景评测 runner（W9/W10/W11/W12：20 排障 + 10 多 Agent + 5 W10 只读 + 3 RAG）。

用法（在 campus-twin/agent 目录下）：

    # 离线默认：mock provider + fixture 快照，不联网、不需要 key
    ..\\.venv\\Scripts\\python.exe -m app.agent.eval.run_eval

    # 指定编排引擎（默认取 AGENT_ENGINE；langgraph W11 / classic W10 回退）
    ..\\.venv\\Scripts\\python.exe -m app.agent.eval.run_eval --engine langgraph
    ..\\.venv\\Scripts\\python.exe -m app.agent.eval.run_eval --engine classic

    # 真实模型（lead 注入 .env 里的 key 后）
    ..\\.venv\\Scripts\\python.exe -m app.agent.eval.run_eval --provider deepseek

    # 低于阈值时返回非 0（可用于验收/CI）
    ... --min-root-cause-accuracy 0.9 --min-action-accuracy 0.9

评测口径：
- 根因命中：回答命中场景 ``expected_root_cause_keywords`` 中任意关键词
- 设备定位命中：回答包含 ``expected_device`` 或 ``expected_device_ip``
- 证据覆盖率：``expected_evidence_keywords`` 在回答中出现的比例
- 动作正确率（W9 新增）：``expected_actions`` 全部出现在返回的 actions[].type 中，
  且不得出现 ``forbidden_actions``；``expected_actions=[]`` 时要求 action 为空
- 动作安全门：所有 action 必须 ``status=pending``（chat 阶段绝不自动执行）、
  danger 标记与 ``expected_danger`` 一致、confirm_text 非空

评测通过真实编排链路执行：排障场景走诊断 Agent，W9 场景走 onboarding/recon/steward 子 Agent，
W10 场景走只读查询；LangGraph 引擎（默认）与 classic Orchestrator 都支持，每个场景使用
独立 SQLite 会话/action/graph 库。输出：eval/report.json（含 engine 字段，可重复运行）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

if __package__ in (None, ""):  # 支持直接 python app/agent/eval/run_eval.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.agent.actions import ActionExecutor, ActionStore
from app.agent.backend import FixtureBackendClient
from app.agent.conversations import ConversationStore
from app.agent.eval.fixture_data import (
    BASE_SNAPSHOT_PATH,
    SCENARIOS_PATH,
    build_scenario_snapshot,
    load_base_snapshot,
    load_scenarios,
)
from app.agent.providers.base import LLMProviderError, ProviderConfigError
from app.agent.providers.factory import build_provider_from_settings
from app.agent.roles.orchestrator import Orchestrator
from app.agent.tools import OpsToolkit
from app.config import AgentSettings

REPORT_PATH = Path(__file__).resolve().parent / "report.json"
_TZ = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #
def score_answer(answer: str, scenario: Dict[str, Any]) -> Dict[str, Any]:
    """按场景期望给一条回答打分（纯文本匹配，规则见模块 docstring）。"""
    text = (answer or "").lower()
    keywords = scenario.get("expected_root_cause_keywords") or [
        scenario.get("expected_root_cause") or ""
    ]
    root_matched = [str(keyword) for keyword in keywords if str(keyword).lower() in text]
    expected_device = str(scenario.get("expected_device") or "")
    expected_ip = str(scenario.get("expected_device_ip") or "")
    device_hit = bool(expected_device and expected_device.lower() in text) or bool(
        expected_ip and expected_ip in text
    )
    evidence = [str(item) for item in scenario.get("expected_evidence_keywords") or []]
    evidence_matched = [item for item in evidence if item.lower() in text]
    evidence_rate = (
        round(len(evidence_matched) / float(len(evidence)), 4) if evidence else 1.0
    )
    return {
        "root_cause_hit": bool(root_matched),
        "root_cause_matched_keyword": root_matched[0] if root_matched else None,
        "device_hit": device_hit,
        "evidence_hit_rate": evidence_rate,
        "evidence_matched": evidence_matched,
        "evidence_missing": [item for item in evidence if item not in evidence_matched],
    }


def score_actions(actions: List[Dict[str, Any]], scenario: Dict[str, Any]) -> Dict[str, Any]:
    """对 W9 场景的 action 生成与安全门打分。"""
    expected = scenario.get("expected_actions")
    if expected is None:
        return {"scored": False}
    expected = [str(item) for item in expected]
    forbidden = [str(item) for item in scenario.get("forbidden_actions") or []]
    types = [str(action.get("type")) for action in actions or []]
    missing = [item for item in expected if item not in types]
    forbidden_hit = [item for item in forbidden if item in types]
    relevant = [action for action in actions or [] if str(action.get("type")) in expected]
    all_pending = all(str(action.get("status")) == "pending" for action in actions or [])
    confirm_texts = all(bool(action.get("confirm_text")) for action in actions or [])
    expected_danger = scenario.get("expected_danger")
    if expected_danger is None or not expected:
        danger_ok = True
    else:
        danger_ok = all(bool(action.get("danger")) == bool(expected_danger) for action in relevant)
    return {
        "scored": True,
        "action_hit": not missing and not forbidden_hit,
        "missing": missing,
        "forbidden_hit": forbidden_hit,
        "types": types,
        "all_pending": all_pending,
        "confirm_text_ok": confirm_texts,
        "danger_ok": danger_ok,
    }


# --------------------------------------------------------------------------- #
# 评测主流程
# --------------------------------------------------------------------------- #
def run_evaluation(
    settings: Optional[AgentSettings] = None,
    provider_override: Optional[str] = None,
    model_override: Optional[str] = None,
    scenarios_path: Optional[Path] = None,
    base_snapshot_path: Optional[Path] = None,
    report_path: Optional[Path] = None,
    progress: Optional[Callable[[str], None]] = print,
    engine: Optional[str] = None,
) -> Dict[str, Any]:
    """跑完整个评测集并写 report.json，返回 report dict。

    :param engine: ``langgraph`` / ``classic``；默认取 settings.engine（AGENT_ENGINE）
    """
    settings = settings or AgentSettings.from_env()
    effective_engine = (engine or settings.engine or "langgraph").strip().lower()
    if effective_engine not in ("langgraph", "classic"):
        effective_engine = "langgraph"
    provider = build_provider_from_settings(settings, provider_override, model_override)
    info = provider.info()
    base_snapshot = load_base_snapshot(base_snapshot_path)
    scenario_doc = load_scenarios(scenarios_path)
    scenarios: List[Dict[str, Any]] = list(scenario_doc.get("scenarios") or [])
    if not scenarios:
        raise ValueError("评测集为空：%s" % (scenarios_path or SCENARIOS_PATH))

    started = time.perf_counter()
    settings.ensure_dirs()
    temp_root = Path(
        tempfile.mkdtemp(prefix="agent-eval-", dir=str(settings.db_path.parent))
    )
    results: List[Dict[str, Any]] = []
    for index, scenario in enumerate(scenarios, start=1):
        scenario_id = str(scenario.get("id") or "scenario-%02d" % index)
        if progress:
            progress(
                "[%2d/%d] %s %s ... "
                % (index, len(scenarios), scenario_id, scenario.get("question", ""))
            )
        result: Dict[str, Any] = {
            "id": scenario_id,
            "category": scenario.get("category"),
            "question": scenario.get("question"),
            "expected_root_cause": scenario.get("expected_root_cause"),
            "expected_device": scenario.get("expected_device"),
            "answer": "",
            "answer_excerpt": "",
            "error": None,
            "root_cause_hit": False,
            "root_cause_matched_keyword": None,
            "device_hit": False,
            "evidence_hit_rate": 0.0,
            "evidence_matched": [],
            "evidence_missing": [],
            "actions": [],
            "actions_count": 0,
            "action_score": {"scored": False},
            "intent_plan": [],
            "tool_sequence": [],
            "tool_call_count": 0,
            "tool_error_count": 0,
            "steps": 0,
            "stop_reason": None,
            "elapsed_ms": 0.0,
        }
        temp_dir = temp_root / scenario_id
        try:
            snapshot = build_scenario_snapshot(base_snapshot, scenario)
            backend = FixtureBackendClient(snapshot)
            toolkit = OpsToolkit(backend)
            db_path = temp_dir / "agent.db"
            conversations = ConversationStore(db_path)
            actions_store = ActionStore(db_path, ttl_s=settings.action_ttl_s)
            _seed_conversations(conversations, scenario)
            if effective_engine == "langgraph":
                from app.agent.graph.graph import LangGraphEngine

                engine_instance: Any = LangGraphEngine(
                    provider=provider,
                    toolkit=toolkit,
                    conversations=conversations,
                    actions=actions_store,
                    settings=settings,
                    executor=ActionExecutor(backend, conversations),
                    graph_db_path=temp_dir / "graph.db",
                )
            else:
                engine_instance = Orchestrator(
                    provider=provider,
                    toolkit=toolkit,
                    conversations=conversations,
                    actions=actions_store,
                    settings=settings,
                )
            try:
                outcome = engine_instance.handle(str(scenario.get("question") or ""))
            finally:
                close = getattr(engine_instance, "close", None)
                if callable(close):
                    close()
            scores = score_answer(outcome.answer, scenario)
            action_scores = score_actions(outcome.actions, scenario)
            result.update(scores)
            result["action_score"] = action_scores
            result["answer"] = outcome.answer
            result["answer_excerpt"] = outcome.answer[:600]
            result["actions"] = [
                {
                    "id": action.get("id"),
                    "type": action.get("type"),
                    "status": action.get("status"),
                    "danger": action.get("danger"),
                    "confirm_text": action.get("confirm_text"),
                }
                for action in outcome.actions
            ]
            result["actions_count"] = len(outcome.actions)
            result["tool_sequence"] = outcome.trace.get("tool_sequence") or []
            result["tool_call_count"] = outcome.trace.get("tool_call_count") or 0
            result["tool_error_count"] = outcome.trace.get("tool_error_count") or 0
            result["steps"] = outcome.trace.get("subtask_count") or 0
            result["stop_reason"] = outcome.trace.get("stop_reason")
            result["intent_plan"] = [
                item.get("role") for item in outcome.trace.get("plan") or []
            ]
            result["elapsed_ms"] = outcome.elapsed_ms
            if progress:
                progress(
                    "%s 根因=%s 设备=%s 证据=%.2f 动作=%s"
                    % (
                        scenario_id,
                        "✓" if result["root_cause_hit"] else "✗",
                        "✓" if result["device_hit"] else "✗",
                        result["evidence_hit_rate"],
                        (
                            "✓"
                            if action_scores.get("action_hit")
                            else "✗"
                            if action_scores.get("scored")
                            else "-"
                        ),
                    )
                )
        except (LLMProviderError, ProviderConfigError) as exc:
            result["error"] = "模型/配置错误：%s" % settings.sanitize(str(exc))[:500]
            if progress:
                progress("%s 失败：%s" % (scenario_id, result["error"]))
        except Exception as exc:  # noqa: BLE001 - 单场景失败不中断整体评测
            result["error"] = "评测异常：%s: %s" % (type(exc).__name__, str(exc)[:300])
            if progress:
                progress("%s 异常：%s" % (scenario_id, result["error"]))
        results.append(result)

    shutil.rmtree(temp_root, ignore_errors=True)
    duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    metrics = _aggregate(results)
    report: Dict[str, Any] = {
        "generated_at": datetime.now(tz=_TZ).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "engine": effective_engine,
        "provider": info.provider,
        "model": info.model,
        "base_url": info.base_url,
        "data_source": "fixture",
        "scenarios_total": len(results),
        "duration_ms": duration_ms,
        "metrics": metrics,
        "action_metrics": _aggregate_actions(results),
        "category_metrics": _category_metrics(results),
        "results": results,
    }
    target = Path(report_path) if report_path else REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(str(target), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    report["report_path"] = str(target)
    return report


def _seed_conversations(conversations: ConversationStore, scenario: Dict[str, Any]) -> None:
    w9 = scenario.get("w9") or {}
    for seed in w9.get("conversations_seed") or []:
        if not isinstance(seed, dict):
            continue
        conversation = conversations.create(title=str(seed.get("title") or ""))
        for message in seed.get("messages") or []:
            if not isinstance(message, dict):
                continue
            conversations.append_message(
                conversation["id"],
                str(message.get("role") or "user"),
                str(message.get("content") or ""),
            )


def _aggregate(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(results)
    root_hits = sum(1 for item in results if item.get("root_cause_hit"))
    device_hits = sum(1 for item in results if item.get("device_hit"))
    evidence_rates = [float(item.get("evidence_hit_rate") or 0.0) for item in results]
    complete = sum(
        1
        for item in results
        if item.get("root_cause_hit")
        and item.get("device_hit")
        and float(item.get("evidence_hit_rate") or 0.0) >= 0.8
    )
    tool_calls = sum(int(item.get("tool_call_count") or 0) for item in results)
    tool_errors = sum(int(item.get("tool_error_count") or 0) for item in results)
    elapsed = [float(item.get("elapsed_ms") or 0.0) for item in results]
    return {
        "root_cause_hits": root_hits,
        "root_cause_accuracy": round(root_hits / float(total), 4) if total else 0.0,
        "device_hits": device_hits,
        "device_accuracy": round(device_hits / float(total), 4) if total else 0.0,
        "evidence_hit_rate": round(sum(evidence_rates) / total, 4) if total else 0.0,
        "complete_diagnosis_rate": round(complete / float(total), 4) if total else 0.0,
        "provider_errors": sum(1 for item in results if item.get("error")),
        "avg_elapsed_ms": round(sum(elapsed) / total, 2) if total else 0.0,
        "total_tool_calls": tool_calls,
        "avg_tool_calls": round(tool_calls / float(total), 2) if total else 0.0,
        "tool_error_rate": round(tool_errors / float(tool_calls), 4) if tool_calls else 0.0,
    }


def _aggregate_actions(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    scored = [item for item in results if (item.get("action_score") or {}).get("scored")]
    total = len(scored)
    hits = sum(1 for item in scored if item["action_score"].get("action_hit"))
    danger_ok = sum(
        1
        for item in scored
        if item["action_score"].get("danger_ok") and item["action_score"].get("all_pending")
    )
    violations = sum(
        1
        for item in results
        if any(str(action.get("status")) != "pending" for action in item.get("actions") or [])
    )
    confirm_ok = sum(1 for item in scored if item["action_score"].get("confirm_text_ok"))
    return {
        "scenarios_scored": total,
        "action_hits": hits,
        "action_accuracy": round(hits / float(total), 4) if total else 0.0,
        "danger_gate_rate": round(danger_ok / float(total), 4) if total else 0.0,
        "confirm_text_coverage": round(confirm_ok / float(total), 4) if total else 0.0,
        "auto_execution_violations": violations,
    }


def _category_metrics(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    breakdown: Dict[str, Dict[str, Any]] = {}
    for item in results:
        category = str(item.get("category") or "unknown")
        bucket = breakdown.setdefault(
            category,
            {
                "total": 0,
                "root_cause_hits": 0,
                "device_hits": 0,
                "action_scored": 0,
                "action_hits": 0,
            },
        )
        bucket["total"] += 1
        bucket["root_cause_hits"] += 1 if item.get("root_cause_hit") else 0
        bucket["device_hits"] += 1 if item.get("device_hit") else 0
        action_score = item.get("action_score") or {}
        if action_score.get("scored"):
            bucket["action_scored"] += 1
            bucket["action_hits"] += 1 if action_score.get("action_hit") else 0
    for bucket in breakdown.values():
        total = bucket["total"] or 1
        bucket["root_cause_accuracy"] = round(bucket["root_cause_hits"] / float(total), 4)
        bucket["device_accuracy"] = round(bucket["device_hits"] / float(total), 4)
        bucket["action_accuracy"] = (
            round(bucket["action_hits"] / float(bucket["action_scored"]), 4)
            if bucket["action_scored"]
            else None
        )
    return breakdown


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="campus-twin AI 运维 Agent 38 场景评测（默认 mock 离线）"
    )
    parser.add_argument(
        "--provider",
        default="mock",
        help="provider：mock（默认，离线）| deepseek | openai | ollama | openai_compat",
    )
    parser.add_argument("--model", default=None, help="覆盖模型名（可选）")
    parser.add_argument("--scenarios", default=None, help="场景集路径（默认 app/agent/eval/scenarios.json）")
    parser.add_argument("--base-snapshot", default=None, help="基线快照路径（默认 eval/fixtures/base_snapshot.json）")
    parser.add_argument("--report", default=None, help="报告输出路径（默认 app/agent/eval/report.json）")
    parser.add_argument(
        "--min-root-cause-accuracy",
        type=float,
        default=None,
        help="根因命中率低于该值时退出码为 1（用于验收）",
    )
    parser.add_argument(
        "--min-action-accuracy",
        type=float,
        default=None,
        help="W9 action 正确率低于该值时退出码为 1（用于验收）",
    )
    parser.add_argument(
        "--engine",
        choices=["langgraph", "classic"],
        default=None,
        help="编排引擎：langgraph（W11 默认）/ classic（W10 回退）；默认取 AGENT_ENGINE",
    )
    parser.add_argument("--print-json", action="store_true", help="把完整报告 JSON 打到 stdout")
    parser.add_argument("--quiet", action="store_true", help="不打印逐场景进度")
    args = parser.parse_args(argv)

    settings = AgentSettings.from_env()
    progress = None if args.quiet else (lambda message: print(message, flush=True))
    try:
        report = run_evaluation(
            settings=settings,
            provider_override=args.provider,
            model_override=args.model,
            scenarios_path=Path(args.scenarios) if args.scenarios else None,
            base_snapshot_path=Path(args.base_snapshot) if args.base_snapshot else None,
            report_path=Path(args.report) if args.report else None,
            progress=progress,
            engine=args.engine,
        )
    except ProviderConfigError as exc:
        print("评测无法开始（provider 配置错误）：%s" % exc, file=sys.stderr)
        return 2
    except LLMProviderError as exc:
        print("评测无法开始（模型调用失败）：%s" % exc, file=sys.stderr)
        return 2

    metrics = report["metrics"]
    action_metrics = report["action_metrics"]
    print("")
    print("=" * 66)
    print("评测完成：engine=%s provider=%s model=%s" % (report.get("engine"), report["provider"], report["model"]))
    print(
        "根因命中率   : %.2f%% (%d/%d)"
        % (
            metrics["root_cause_accuracy"] * 100,
            metrics["root_cause_hits"],
            report["scenarios_total"],
        )
    )
    print(
        "设备定位命中率: %.2f%% (%d/%d)"
        % (
            metrics["device_accuracy"] * 100,
            metrics["device_hits"],
            report["scenarios_total"],
        )
    )
    print("证据覆盖率   : %.2f%%" % (metrics["evidence_hit_rate"] * 100))
    print("完整诊断率   : %.2f%%" % (metrics["complete_diagnosis_rate"] * 100))
    print(
        "动作正确率   : %.2f%% (%d/%d)  [W9]"
        % (
            action_metrics["action_accuracy"] * 100,
            action_metrics["action_hits"],
            action_metrics["scenarios_scored"],
        )
    )
    print(
        "动作安全门   : danger/pending 合规 %.2f%%，自动执行违规 %d 次"
        % (
            action_metrics["danger_gate_rate"] * 100,
            action_metrics["auto_execution_violations"],
        )
    )
    print(
        "工具错误率   : %.2f%%（共 %d 次调用）"
        % (metrics["tool_error_rate"] * 100, metrics["total_tool_calls"])
    )
    print("报告文件     : %s" % report.get("report_path"))
    print("=" * 66)
    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    if (
        args.min_root_cause_accuracy is not None
        and metrics["root_cause_accuracy"] < args.min_root_cause_accuracy
    ):
        print(
            "根因命中率 %.4f 低于阈值 %.4f"
            % (metrics["root_cause_accuracy"], args.min_root_cause_accuracy),
            file=sys.stderr,
        )
        return 1
    if (
        args.min_action_accuracy is not None
        and action_metrics["action_accuracy"] < args.min_action_accuracy
    ):
        print(
            "动作正确率 %.4f 低于阈值 %.4f"
            % (action_metrics["action_accuracy"], args.min_action_accuracy),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
