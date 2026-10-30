# -*- coding: utf-8 -*-
"""W12 SSE 流式输出：事件契约 / mock 切片 / action / RAG 引用 / classic 降级 / 断开。"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import AgentSettings

BASE_ENV: Dict[str, str] = {
    "LLM_PROVIDER": "mock",
    "AGENT_DATA_SOURCE": "fixture",
    "BACKEND_BASE_URL": "http://127.0.0.1:1",
}


def _settings(tmp_path: Any, engine: str = "langgraph", scenario: str = "s26") -> AgentSettings:
    db = tmp_path / ("%s-%s.db" % (engine, scenario))
    return AgentSettings.from_env(
        env={
            **BASE_ENV,
            "AGENT_ENGINE": engine,
            "AGENT_FIXTURE_SCENARIO": scenario,
            "AGENT_DB_PATH": str(db),
            "AGENT_GRAPH_DB_PATH": str(db.with_name("graph.db")),
        }
    )


@pytest.fixture()
def stream_client(tmp_path: Any) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(tmp_path))) as client:
        yield client


def parse_sse(response: Any) -> List[Dict[str, Any]]:
    """把 text/event-stream 响应解析成事件列表。"""
    events: List[Dict[str, Any]] = []
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            raw, buffer = buffer.split("\n\n", 1)
            raw = raw.strip()
            if raw.startswith("data: "):
                events.append(json.loads(raw[len("data: ") :]))
    return events


def test_stream_event_contract(stream_client: TestClient) -> None:
    with stream_client.stream(
        "POST",
        "/api/chat/stream",
        json={"question": "PC-JX1 到服务器 Server-1 的路径怎么走？"},
    ) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        events = parse_sse(response)
    types = [event["type"] for event in events]
    assert types[0] == "meta"
    assert "done" not in types[:-1]
    assert types[-1] == "done"
    assert types.count("token") >= 2
    meta = events[0]
    assert meta["conversation_id"].startswith("conv_")
    assert meta["engine"] == "langgraph"
    assert meta["provider"] == "mock"
    status_stages = {event.get("stage") for event in events if event["type"] == "status"}
    assert {"classify", "retrieve", "aggregate"} <= status_stages
    tool_events = [event for event in events if event["type"] == "tool"]
    assert any(event["name"] == "search_knowledge" for event in tool_events)
    assert all(set(event) >= {"name", "ok", "elapsed_ms"} for event in tool_events)
    tokens = [event for event in events if event["type"] == "token"]
    assert all(event["text"] != "" for event in tokens)
    done = events[-1]
    assert done["answer"]
    assert "".join(event["text"] for event in tokens) == done["answer"]
    assert set(done) >= {"answer", "trace", "elapsed_ms", "conversation_id"}
    assert done["trace"]["engine"] == "langgraph"
    assert done["trace"]["retrieval"]
    assert done["trace"]["route_source"] == "rule"
    assert done["trace"]["route"]["rule"] == "path"


def test_stream_action_event_and_cancel(stream_client: TestClient) -> None:
    with stream_client.stream(
        "POST", "/api/chat/stream", json={"question": "把已恢复的告警清空"}
    ) as response:
        events = parse_sse(response)
    actions = [event["action"] for event in events if event["type"] == "action"]
    assert len(actions) == 1
    assert actions[0]["type"] == "clear_cleared_alerts"
    assert actions[0]["status"] == "pending"
    done = [event for event in events if event["type"] == "done"][0]
    assert "数据现状" in done["answer"]
    assert done["trace"]["route_source"] == "rule"
    assert done["trace"]["route"]["action_hint"] == "clear_cleared_alerts"
    cancelled = stream_client.post("/api/actions/%s/cancel" % actions[0]["id"]).json()
    assert cancelled["action"]["status"] == "cancelled"


def test_stream_rag_citation_and_no_hit(stream_client: TestClient) -> None:
    with stream_client.stream(
        "POST",
        "/api/chat/stream",
        json={"question": "宿舍楼要接入一台 2960 交换机，上联核心怎么配置？"},
    ) as response:
        events = parse_sse(response)
    done = events[-1]
    assert "## 知识库引用" in done["answer"]
    assert "[K1]" in done["answer"]
    assert done["trace"]["retrieval"][0]["hit_count"] >= 1

    with stream_client.stream(
        "POST", "/api/chat/stream", json={"question": "学校食堂的 Wi-Fi 密码是多少？"}
    ) as response:
        events = parse_sse(response)
    done = events[-1]
    assert "知识库无相关内容" in done["answer"]
    assert done["trace"]["retrieval"][0]["hit_count"] == 0


def test_stream_error_event_for_unknown_conversation(stream_client: TestClient) -> None:
    with stream_client.stream(
        "POST",
        "/api/chat/stream",
        json={"question": "现在有哪些告警？", "conversation_id": "conv_missing"},
    ) as response:
        assert response.status_code == 200
        events = parse_sse(response)
    assert events[0]["type"] == "error"
    assert "会话不存在" in events[-1]["message"]


def test_stream_classic_engine_fallback(tmp_path: Any) -> None:
    with TestClient(create_app(_settings(tmp_path, engine="classic", scenario="s26"))) as client:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={"question": "PC-JX1 到服务器 Server-1 的路径怎么走？"},
        ) as response:
            events = parse_sse(response)
    assert events[0]["engine"] == "classic"
    tokens = [event for event in events if event["type"] == "token"]
    done = events[-1]
    assert tokens and "".join(event["text"] for event in tokens) == done["answer"]


def test_stream_disconnect_does_not_raise(stream_client: TestClient) -> None:
    with stream_client.stream(
        "POST",
        "/api/chat/stream",
        json={"question": "现在整体网络状态怎么样？"},
    ) as response:
        iterator = response.iter_text()
        next(iterator)  # 客户端只读第一个 chunk 就断开
    # 断开后服务仍可正常处理非流式请求
    assert stream_client.post("/api/chat", json={"question": "现在有哪些告警？"}).status_code == 200
