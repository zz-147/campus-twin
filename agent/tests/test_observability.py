# -*- coding: utf-8 -*-
"""W15-P1：trace 可观测性（SQLite trace_events + API 契约）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.agent.observability import TraceStore, estimate_tokens, record_turn
from app.api import create_app
from app.config import AgentSettings


def _store(tmp_path: Path) -> TraceStore:
    return TraceStore(tmp_path / "agent.db")


def test_trace_store_write_query_metrics(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trace_id = store.new_trace_id()
    store.record(trace_id, "turn", conversation_id="conv1", latency_ms=12.5, tokens_in=10, tokens_out=20, provider="mock", model="mock-rules")
    store.record(trace_id, "tool", conversation_id="conv1", step=1, tool="get_alerts", latency_ms=3.0, ok=True)
    store.record(trace_id, "tool", conversation_id="conv1", step=2, tool="trace_path", latency_ms=4.0, ok=False, error="timeout")
    store.record(trace_id, "llm", conversation_id="conv1", provider="mock", model="mock-rules", tokens_in=10, tokens_out=20)
    events = store.query_trace(trace_id)
    assert len(events) == 4
    assert events[0]["event"] == "turn" and events[0]["ok"] is True
    assert events[2]["ok"] is False and events[2]["error"] == "timeout"
    traces = store.list_traces("conv1")
    assert len(traces) == 1
    assert traces[0]["trace_id"] == trace_id
    assert traces[0]["events"] == 4
    metrics = store.metrics()
    assert metrics["trace_count"] == 1
    assert metrics["tool_events"] == 2
    assert metrics["tool_errors"] == 1
    assert metrics["tool_error_rate"] == 0.5
    assert metrics["tokens_total"] == 60
    assert metrics["by_model"] and metrics["by_day"]
    assert store.count() == 4
    assert estimate_tokens("abcd") == 1


def test_record_turn_from_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = {
        "answer": "这是一段回答",
        "trace": {
            "plan": [
                {"role": "steward", "label": "数据治理", "ok": True, "elapsed_ms": 8.0, "tools_used": ["get_alerts"], "actions": 1},
            ],
            "route_source": "rule",
        },
        "actions": [{"id": "act_1", "type": "clear_cleared_alerts", "status": "pending", "danger": True}],
        "pending_actions": [{"id": "act_1"}],
    }
    result = record_turn(
        store,
        trace_id="trace_demo",
        conversation_id="conv_demo",
        question="把已恢复的告警清空",
        payload=payload,
        engine="langgraph",
        provider="mock",
        model="mock-rules",
        latency_ms=15.0,
    )
    events = store.query_trace("trace_demo")
    kinds = [event["event"] for event in events]
    assert "turn" in kinds and "llm" in kinds and "node" in kinds and "tool" in kinds
    assert "action" in kinds and "interrupt" in kinds
    assert result["events"] == len(events)
    assert any(event["tokens_in"] > 0 for event in events)


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    env = {
        "LLM_PROVIDER": "mock",
        "AGENT_DATA_SOURCE": "fixture",
        "AGENT_FIXTURE_SCENARIO": "s01",
        "AGENT_DB_PATH": str(tmp_path / "a.db"),
        "AGENT_GRAPH_DB_PATH": str(tmp_path / "graph.db"),
    }
    settings = AgentSettings.from_env(env=env)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_api_chat_records_trace_and_contract(client: TestClient) -> None:
    response = client.post("/api/chat", json={"question": "现在整体网络状态怎么样"})
    assert response.status_code == 200
    body = response.json()
    conversation_id = body["conversation_id"]
    listing = client.get("/api/traces", params={"conversation_id": conversation_id, "limit": 20})
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["count"] >= 1
    item = payload["items"][0]
    for key in (
        "trace_id", "conversation_id", "step", "event", "tool", "provider", "model",
        "tokens_in", "tokens_out", "latency_ms", "ok", "error", "ts",
    ):
        assert key in item
    assert item["event"] == "turn"
    trace_id = item["trace_id"]

    detail = client.get("/api/traces/%s" % trace_id)
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["trace_id"] == trace_id
    assert detail_body["summary"]["events"] >= 1
    assert detail_body["steps"] and detail_body["events"]
    assert "tool_calls" in detail_body["summary"]

    metrics = client.get("/api/metrics/agent")
    assert metrics.status_code == 200
    metric_body = metrics.json()
    for key in ("avg_latency_ms", "tool_error_rate", "tokens_total", "calls", "by_day", "by_model"):
        assert key in metric_body
    assert metric_body["calls"] >= 1

    rag = client.get("/api/rag/report")
    assert rag.status_code == 200
    rag_body = rag.json()
    assert rag_body["available"] is True
    assert rag_body["recall_at_5"] is not None
    assert rag_body["report"]["queries"]["count"] >= 40

    missing = client.get("/api/traces/not-exist-trace")
    assert missing.status_code == 404


def test_api_stream_records_trace(client: TestClient) -> None:
    with client.stream(
        "POST", "/api/chat/stream", json={"question": "现在整体网络状态怎么样"}
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    events = [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]
    done = [event for event in events if event.get("type") == "done"]
    assert done
    conversation_id = done[0].get("conversation_id")
    if conversation_id:
        listing = client.get("/api/traces", params={"conversation_id": conversation_id})
        assert listing.status_code == 200
        assert listing.json()["count"] >= 1
