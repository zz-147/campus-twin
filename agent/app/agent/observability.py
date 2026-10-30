# -*- coding: utf-8 -*-
"""W15-P1：自建 Agent 可观测性（SQLite ``trace_events`` 表，替代 Langfuse）。

为什么自建：本机无外网/账号，Langfuse 等平台接不进来；先用 SQLite 落最小可用 trace，
字段保持通用（trace_id/conversation_id/step/event/tool/provider/model/tokens/latency/ok），
后续可无损上报到 Langfuse/OTel（文档说明“可替换/可上报”）。

- 表：``trace_events``（每事件一行；同一 turn 共享 trace_id）；
- 写入：``TraceStore.record``；Agent 结果摘要：``record_turn``；
- 查询：``query_trace`` / ``list_traces``；
- 指标：``metrics``（平均延迟、工具错误率、tokens 汇总、按天/模型分组）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    conversation_id TEXT,
    step INTEGER NOT NULL DEFAULT 0,
    event TEXT NOT NULL,
    tool TEXT,
    provider TEXT,
    model TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    ts TEXT NOT NULL,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_trace_events_trace ON trace_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_trace_events_conv ON trace_events(conversation_id);
CREATE INDEX IF NOT EXISTS idx_trace_events_ts ON trace_events(ts);
"""


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _row_to_event(row: sqlite3.Row) -> Dict[str, Any]:
    meta = row["meta"]
    try:
        meta = json.loads(meta) if meta else {}
    except Exception:  # noqa: BLE001
        meta = {"raw": meta}
    return {
        "id": row["id"],
        "trace_id": row["trace_id"],
        "conversation_id": row["conversation_id"],
        "step": row["step"],
        "event": row["event"],
        "tool": row["tool"],
        "provider": row["provider"],
        "model": row["model"],
        "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"],
        "latency_ms": row["latency_ms"],
        "ok": bool(row["ok"]),
        "error": row["error"],
        "ts": row["ts"],
        "meta": meta,
    }


class TraceStore:
    """trace_events 读写 + 聚合（线程安全，单文件 SQLite WAL）。"""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def new_trace_id() -> str:
        return uuid.uuid4().hex[:16]

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def record(
        self,
        trace_id: str,
        event: str,
        *,
        conversation_id: Optional[str] = None,
        step: int = 0,
        tool: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: float = 0.0,
        ok: bool = True,
        error: Optional[str] = None,
        ts: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        row = {
            "trace_id": str(trace_id),
            "conversation_id": conversation_id,
            "step": int(step),
            "event": str(event),
            "tool": tool,
            "provider": provider,
            "model": model,
            "tokens_in": max(0, int(tokens_in or 0)),
            "tokens_out": max(0, int(tokens_out or 0)),
            "latency_ms": round(float(latency_ms or 0.0), 2),
            "ok": 1 if ok else 0,
            "error": (str(error)[:500] if error else None),
            "ts": ts or _now_iso(),
            "meta": json.dumps(meta or {}, ensure_ascii=False),
        }
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO trace_events
                   (trace_id, conversation_id, step, event, tool, provider, model,
                    tokens_in, tokens_out, latency_ms, ok, error, ts, meta)
                   VALUES (:trace_id, :conversation_id, :step, :event, :tool, :provider,
                           :model, :tokens_in, :tokens_out, :latency_ms, :ok, :error, :ts, :meta)""",
                row,
            )
            conn.commit()
            row["id"] = cursor.lastrowid
        row["ok"] = bool(row["ok"])
        try:
            row["meta"] = json.loads(row["meta"])
        except Exception:  # noqa: BLE001
            row["meta"] = {}
        return row

    # ------------------------------------------------------------------ #
    # 查
    # ------------------------------------------------------------------ #
    def query_trace(self, trace_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trace_events WHERE trace_id=? ORDER BY id ASC LIMIT ?",
                (str(trace_id), max(1, int(limit))),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def list_traces(
        self,
        conversation_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        where = "WHERE conversation_id=?" if conversation_id else ""
        params: List[Any] = [conversation_id] if conversation_id else []
        params.append(max(1, min(500, int(limit))))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT trace_id, conversation_id,
                          MIN(ts) AS started_at, MAX(ts) AS ended_at,
                          COUNT(*) AS events,
                          SUM(latency_ms) AS total_latency_ms,
                          SUM(tokens_in) AS tokens_in,
                          SUM(tokens_out) AS tokens_out,
                          MIN(ok) AS all_ok,
                          MAX(error) AS last_error
                   FROM trace_events %s
                   GROUP BY trace_id
                   ORDER BY MAX(id) DESC
                   LIMIT ?""" % where,
                params,
            ).fetchall()
        return [
            {
                "trace_id": row["trace_id"],
                "conversation_id": row["conversation_id"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "events": row["events"],
                "total_latency_ms": round(float(row["total_latency_ms"] or 0.0), 2),
                "tokens_in": int(row["tokens_in"] or 0),
                "tokens_out": int(row["tokens_out"] or 0),
                "ok": bool(row["all_ok"]),
                "last_error": row["last_error"],
            }
            for row in rows
        ]

    def metrics(self) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            totals = conn.execute(
                """SELECT COUNT(DISTINCT trace_id) AS trace_count,
                          COUNT(*) AS events,
                          SUM(CASE WHEN event='tool' THEN 1 ELSE 0 END) AS tool_events,
                          SUM(CASE WHEN event='tool' AND ok=0 THEN 1 ELSE 0 END) AS tool_errors,
                          SUM(tokens_in) AS tokens_in,
                          SUM(tokens_out) AS tokens_out
                   FROM trace_events"""
            ).fetchone()
            latency = conn.execute(
                "SELECT AVG(latency_ms) AS avg_latency FROM trace_events WHERE event='turn'"
            ).fetchone()
            by_day = conn.execute(
                """SELECT substr(ts, 1, 10) AS day, COUNT(DISTINCT trace_id) AS traces,
                          AVG(latency_ms) AS avg_latency, SUM(tokens_in) AS tokens_in,
                          SUM(tokens_out) AS tokens_out
                   FROM trace_events GROUP BY day ORDER BY day DESC LIMIT 30"""
            ).fetchall()
            by_model = conn.execute(
                """SELECT provider, model, COUNT(DISTINCT trace_id) AS traces,
                          SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out
                   FROM trace_events WHERE event='llm'
                   GROUP BY provider, model ORDER BY traces DESC LIMIT 20"""
            ).fetchall()
            recent_errors = conn.execute(
                "SELECT trace_id, event, tool, error, ts FROM trace_events WHERE ok=0 ORDER BY id DESC LIMIT 20"
            ).fetchall()
        tool_events = int(totals["tool_events"] or 0)
        tool_errors = int(totals["tool_errors"] or 0)
        return {
            "trace_count": int(totals["trace_count"] or 0),
            "event_count": int(totals["events"] or 0),
            "avg_latency_ms": round(float(latency["avg_latency"] or 0.0), 2),
            "tool_events": tool_events,
            "tool_errors": tool_errors,
            "tool_error_rate": round(tool_errors / float(tool_events), 4) if tool_events else 0.0,
            "tokens_in": int(totals["tokens_in"] or 0),
            "tokens_out": int(totals["tokens_out"] or 0),
            "tokens_total": int(totals["tokens_in"] or 0) + int(totals["tokens_out"] or 0),
            "by_day": [dict(row) for row in by_day],
            "by_model": [dict(row) for row in by_model],
            "recent_errors": [dict(row) for row in recent_errors],
            "storage": "sqlite:trace_events",
        }

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM trace_events").fetchone()
        return int(row["n"] or 0)


def estimate_tokens(text: str) -> int:
    """无 usage 时的估算（字符数/4），meta 标记 estimated。"""
    return max(1, int(len(text or "") / 4))


def record_turn(
    store: TraceStore,
    *,
    trace_id: str,
    conversation_id: Optional[str],
    question: str,
    payload: Dict[str, Any],
    engine: str,
    provider: Optional[str],
    model: Optional[str],
    latency_ms: float,
    started_at: Optional[str] = None,
) -> Dict[str, Any]:
    """把一次 Agent handle 结果拆成 trace_events（turn/node/tool/action/interrupt/llm）。"""
    trace = payload.get("trace") or {}
    answer = str(payload.get("answer") or "")
    actions = list(payload.get("actions") or [])
    tool_sequence: List[str] = []
    plan = list(trace.get("plan") or [])
    tokens_in = estimate_tokens(question)
    tokens_out = estimate_tokens(answer)
    base_ts = started_at or _now_iso()
    event_count = 0
    store.record(
        trace_id,
        "turn",
        conversation_id=conversation_id,
        step=0,
        provider=provider,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        ok=True,
        ts=base_ts,
        meta={
            "engine": engine,
            "route_source": trace.get("route_source"),
            "paused": bool(trace.get("paused")),
            "actions": len(actions),
            "estimated_tokens": True,
        },
    )
    event_count += 1
    store.record(
        trace_id,
        "llm",
        conversation_id=conversation_id,
        step=1,
        provider=provider,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        ok=True,
        ts=base_ts,
        meta={"estimated_tokens": True, "note": "无 provider usage 时按 char/4 估算"},
    )
    event_count += 1
    for index, step in enumerate(plan, start=1):
        tools = list(step.get("tools_used") or [])
        tool_sequence.extend(tools)
        store.record(
            trace_id,
            "node",
            conversation_id=conversation_id,
            step=index,
            provider=provider,
            model=model,
            latency_ms=float(step.get("elapsed_ms") or 0.0),
            ok=bool(step.get("ok", True)),
            error=step.get("error"),
            ts=base_ts,
            meta={
                "role": step.get("role"),
                "label": step.get("label"),
                "objective": step.get("objective"),
                "tools_used": tools,
                "actions": step.get("actions"),
            },
        )
        event_count += 1
        for tool in tools:
            store.record(
                trace_id,
                "tool",
                conversation_id=conversation_id,
                step=index,
                tool=str(tool),
                latency_ms=0.0,
                ok=bool(step.get("ok", True)),
                error=step.get("error"),
                ts=base_ts,
                meta={"role": step.get("role")},
            )
            event_count += 1
    for action in actions:
        store.record(
            trace_id,
            "action",
            conversation_id=conversation_id,
            step=len(plan) + 1,
            tool=str(action.get("type") or "action"),
            provider=provider,
            model=model,
            ok=True,
            ts=base_ts,
            meta={
                "action_id": action.get("id"),
                "type": action.get("type"),
                "status": action.get("status"),
                "danger": action.get("danger"),
            },
        )
        event_count += 1
    if payload.get("pending_actions") or plan:
        paused = bool(trace.get("paused")) or any(
            str(item.get("status") or "") == "pending" for item in actions
        )
        if paused:
            store.record(
                trace_id,
                "interrupt",
                conversation_id=conversation_id,
                step=len(plan) + 2,
                ok=True,
                ts=base_ts,
                meta={"pending_actions": [item.get("id") for item in actions]},
            )
            event_count += 1
    return {"trace_id": trace_id, "events": event_count}
