# -*- coding: utf-8 -*-
"""FastAPI 接口测试：W5/W6 对话 + W9 多 Agent / 会话 / action 确认协议。

全部离线：mock provider + fixture 数据源（不依赖 key / PT / 后端）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import AgentSettings

BASE_ENV: Dict[str, str] = {
    "LLM_PROVIDER": "mock",
    "LLM_API_KEY": "",
    "LLM_BASE_URL": "",
    "LLM_MODEL": "",
    "AGENT_DATA_SOURCE": "fixture",
    "BACKEND_BASE_URL": "http://127.0.0.1:1",  # fixture 模式不会访问
}


def _settings(**overrides: str) -> AgentSettings:
    env = dict(BASE_ENV)
    env.update(overrides)
    db_path = env.get("AGENT_DB_PATH")
    if db_path and "AGENT_GRAPH_DB_PATH" not in env:
        from pathlib import Path

        env["AGENT_GRAPH_DB_PATH"] = str(Path(db_path).with_name("graph.db"))
    return AgentSettings.from_env(env=env)


@pytest.fixture()
def client(tmp_path: Any) -> Iterator[TestClient]:
    """默认场景 s01（PC-JX1 掉线），用于排障对话 + 基础接口。"""
    settings = _settings(AGENT_FIXTURE_SCENARIO="s01", AGENT_DB_PATH=str(tmp_path / "a.db"))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_root_endpoint(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Campus Twin AI Ops Agent"
    assert body["architecture"] == "1 main agent + 4 specialist sub-agents"
    assert "/api/chat (POST)" in body["endpoints"]


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["provider"] == "mock"
    assert body["model"] == "mock-rules"
    assert body["provider_ready"] is True
    assert body["key_configured"] is False
    assert body["data_source"] == "fixture"
    assert body["backend"]["mode"] == "fixture"
    assert body["w9_backend"]["available"] is True
    assert body["w10_backend"]["available"] is True
    assert body["engine"] == "langgraph"
    assert body["engine_error"] is None
    assert body["graph"]["engine"] == "langgraph"
    assert body["graph"]["graph_version"] == "w11-langgraph-1"
    assert body["graph"]["checkpointer"]["type"] == "SqliteSaver"
    assert "wait_confirm" in body["graph"]["nodes"]
    assert body["conversations"] == 0
    assert body["pending_actions"] == 0
    assert body["roles"] == ["main", "onboarding", "scene", "recon", "steward"]
    serialized = json.dumps(body, ensure_ascii=False)
    assert "sk-" not in serialized
    assert "api_key" not in serialized


def test_roles_endpoint(client: TestClient) -> None:
    body = client.get("/api/roles").json()
    assert body["main"]["label"] == "主 Agent"
    assert body["engine"] == "langgraph"
    assert "wait_confirm" in body["graph"]["nodes"]
    sub_names = [item["name"] for item in body["sub_agents"]]
    assert sub_names == ["onboarding", "scene", "recon", "steward"]
    readonly_names = [item["name"] for item in body["readonly_capabilities"]]
    assert readonly_names == ["path_trace", "alert_groups", "notify"]
    assert "apply_scene_device" in body["action_types"]
    assert len(body["diagnosis_tools"]) == 13
    assert len(body["inference_rules"]) == 5
    assert len(body["all_tools"]) == 23
    assert "trace_path" in body["all_tools"]
    assert "get_alert_groups" in body["all_tools"]
    assert "get_notify_status" in body["all_tools"]
    assert "search_knowledge" in body["all_tools"]
    assert "mcp_read_file" in body["all_tools"]
    assert body["mcp"]["tools"]
    assert "streaming" in body
    assert "knowledge" in body
    assert body["routing"]["rule_first"] is True
    assert body["routing"]["route_source"] == ["rule", "llm"]
    assert "write_clear_alerts" in body["routing"]["rules"]


def test_chat_diagnosis_returns_chinese_answer(client: TestClient) -> None:
    response = client.post(
        "/api/chat",
        json={"question": "教学楼 PC-JX1 上不了网，帮我看下是什么原因？"},
    )
    assert response.status_code == 200, response.text
    assert "charset=utf-8" in response.headers.get("content-type", "").lower()
    body = response.json()
    assert set(body.keys()) == {
        "answer",
        "trace",
        "provider",
        "model",
        "elapsed_ms",
        "conversation_id",
        "actions",
    }
    assert body["provider"] == "mock"
    assert body["conversation_id"].startswith("conv_")
    assert body["actions"] == []
    assert "## 根因" in body["answer"]
    assert "PC-JX1" in body["answer"]
    assert body["trace"]["mode"] == "multi_agent"
    assert [item["role"] for item in body["trace"]["plan"]] == ["diagnosis"]
    assert body["trace"]["backend_mode"] == "fixture"
    assert "sk-" not in json.dumps(body, ensure_ascii=False)


def test_chat_w10_path_trace(tmp_path: Any) -> None:
    settings = _settings(AGENT_FIXTURE_SCENARIO="s31", AGENT_DB_PATH=str(tmp_path / "w10path.db"))
    with TestClient(create_app(settings)) as test_client:
        response = test_client.post(
            "/api/chat", json={"question": "PC-JX1 到服务器 Server-1 的路径怎么走？"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["role"] for item in body["trace"]["plan"]] == ["path_trace"]
        assert body["actions"] == []
        assert "路径追踪结果" in body["answer"]
        assert "Server-1" in body["answer"]


def test_chat_w10_alert_groups(tmp_path: Any) -> None:
    settings = _settings(AGENT_FIXTURE_SCENARIO="s33", AGENT_DB_PATH=str(tmp_path / "w10groups.db"))
    with TestClient(create_app(settings)) as test_client:
        response = test_client.post("/api/chat", json={"question": "现在有哪些告警？根因是什么？"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["role"] for item in body["trace"]["plan"]] == ["alert_groups"]
        assert body["actions"] == []
        assert "告警根因分组" in body["answer"]
        assert "被抑制" in body["answer"]


def test_chat_w10_notify_status(tmp_path: Any) -> None:
    settings = _settings(AGENT_FIXTURE_SCENARIO="s35", AGENT_DB_PATH=str(tmp_path / "w10notify.db"))
    with TestClient(create_app(settings)) as test_client:
        response = test_client.post("/api/chat", json={"question": "告警通知配好了吗？"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["role"] for item in body["trace"]["plan"]] == ["notify"]
        assert body["actions"] == []
        assert "已启用" in body["answer"]
        assert "generic" in body["answer"]



def test_chat_conversation_continuation(client: TestClient) -> None:
    first = client.post("/api/chat", json={"question": "PC-JX1 掉线了吗？"}).json()
    conversation_id = first["conversation_id"]
    second = client.post(
        "/api/chat",
        json={"question": "那 PC-JX2 呢？", "conversation_id": conversation_id},
    )
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id
    conversation = client.get("/api/conversations/%s" % conversation_id).json()
    assert conversation["message_count"] == 4
    assert conversation["title"]
    assert client.post(
        "/api/chat", json={"question": "你好", "conversation_id": "conv_not_exist"}
    ).status_code == 404


def test_conversation_list_and_delete(client: TestClient) -> None:
    first = client.post("/api/chat", json={"question": "教学楼为什么卡？"}).json()
    conversation_id = first["conversation_id"]
    listing = client.get("/api/conversations").json()
    assert listing["count"] == 1
    assert listing["conversations"][0]["id"] == conversation_id
    assert client.delete("/api/conversations/%s" % conversation_id).json()["deleted"] is True
    assert client.get("/api/conversations/%s" % conversation_id).status_code == 404
    assert client.delete("/api/conversations/%s" % conversation_id).status_code == 404


def test_conversation_clear(client: TestClient) -> None:
    first = client.post("/api/chat", json={"question": "第一轮"}).json()
    keep = first["conversation_id"]
    client.post("/api/chat", json={"question": "第二轮"})
    cleared = client.post("/api/conversations/clear", json={"keep_current": keep}).json()
    assert cleared["deleted"] == 1
    assert client.get("/api/conversations").json()["count"] == 1


# --------------------------------------------------------------------------- #
# W9：action 确认协议（fixture 场景 s26：3 条已恢复 + 2 条活跃告警）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def steward_client(tmp_path: Any) -> Iterator[TestClient]:
    settings = _settings(AGENT_FIXTURE_SCENARIO="s26", AGENT_DB_PATH=str(tmp_path / "w9.db"))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_chat_generates_pending_action_not_executed(steward_client: TestClient) -> None:
    body = steward_client.post(
        "/api/chat", json={"question": "把已恢复的告警清空"}
    ).json()
    assert [item["role"] for item in body["trace"]["plan"]] == ["steward"]
    assert len(body["actions"]) == 1
    action = body["actions"][0]
    assert action["type"] == "clear_cleared_alerts"
    assert action["status"] == "pending"
    assert action["danger"] is True
    assert "3" in action["confirm_text"]
    assert action["conversation_id"] == body["conversation_id"]
    # chat 阶段绝不执行：后端告警仍完整
    backend = steward_client.app.state.backend  # type: ignore[attr-defined]
    assert backend.get("/api/alerts")["summary"]["cleared"] == 3
    assert steward_client.get("/api/actions/pending").json()["count"] == 1


def test_action_confirm_executes_after_user_confirmation(steward_client: TestClient) -> None:
    body = steward_client.post("/api/chat", json={"question": "把已恢复的告警清空"}).json()
    action_id = body["actions"][0]["id"]
    response = steward_client.post("/api/actions/%s/confirm" % action_id)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] is True
    assert result["action"]["status"] == "executed"
    assert result["action"]["result"]["deleted"] == 3
    backend = steward_client.app.state.backend  # type: ignore[attr-defined]
    assert backend.get("/api/alerts")["summary"]["cleared"] == 0
    assert backend.get("/api/audit")["count"] == 1
    assert backend.get("/api/audit")["items"][0]["operator"] == "agent"
    # 重复确认必须被拒绝
    again = steward_client.post("/api/actions/%s/confirm" % action_id)
    assert again.status_code == 409
    assert steward_client.get("/api/actions/pending").json()["count"] == 0


def test_action_cancel(steward_client: TestClient) -> None:
    body = steward_client.post("/api/chat", json={"question": "把已恢复的告警清空"}).json()
    action_id = body["actions"][0]["id"]
    cancelled = steward_client.post("/api/actions/%s/cancel" % action_id).json()
    assert cancelled["ok"] is True
    assert cancelled["action"]["status"] == "cancelled"
    assert steward_client.post("/api/actions/%s/confirm" % action_id).status_code == 409


def test_action_unknown_and_get(steward_client: TestClient) -> None:
    assert steward_client.get("/api/actions/act_not_exist").status_code == 404
    body = steward_client.post("/api/chat", json={"question": "把已恢复的告警清空"}).json()
    action_id = body["actions"][0]["id"]
    detail = steward_client.get("/api/actions/%s" % action_id).json()
    assert detail["status"] == "pending"
    assert detail["confirm_text"]


def test_chat_w9_without_llm_key_still_works(tmp_path: Any) -> None:
    """W9 子 Agent 是确定性逻辑：没有 LLM key 也能生成接入方案 + action。"""
    env = dict(BASE_ENV)
    env["LLM_PROVIDER"] = "deepseek"
    env["AGENT_FIXTURE_SCENARIO"] = "s21"
    env["AGENT_DB_PATH"] = str(tmp_path / "nokey.db")
    with TestClient(create_app(AgentSettings.from_env(env=env))) as test_client:
        response = test_client.post(
            "/api/chat", json={"question": "我想在宿舍楼加一台 2960 交换机"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["actions"][0]["type"] == "apply_scene_device"
        assert "宿舍楼" in body["answer"]
        # 排障类仍需要 LLM → 503 中文指引
        health = test_client.get("/api/health").json()
        assert health["provider_ready"] is False
        detail = test_client.post("/api/chat", json={"question": "PC-JX1 为什么掉线？"})
        assert detail.status_code == 503
        assert "LLM_API_KEY" in detail.json()["detail"]


def test_chat_runtime_provider_override(client: TestClient) -> None:
    response = client.post(
        "/api/chat",
        json={"question": "宿舍楼 PC-SS1 掉线了怎么排查？", "provider": "mock"},
    )
    assert response.status_code == 200
    assert response.json()["provider"] == "mock"
    bad = client.post("/api/chat", json={"question": "测试", "provider": "not-a-provider"})
    assert bad.status_code == 400
    assert "LLM_PROVIDER" in bad.json()["detail"]


def test_chat_validates_question(client: TestClient) -> None:
    assert client.post("/api/chat", json={"question": ""}).status_code == 422
    assert client.post("/api/chat", json={}).status_code == 422
    assert client.post("/api/chat", json={"question": "x" * 3000}).status_code == 422


def test_providers_endpoint(client: TestClient) -> None:
    body = client.get("/api/providers").json()
    assert body["default_provider"] == "deepseek"
    assert "mock" in body["supported"]
    assert body["switch_by"] == ["LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"]


def test_eval_scenarios_endpoint(client: TestClient) -> None:
    body = client.get("/api/eval/scenarios").json()
    assert body["count"] == 38
    assert len(body["scenarios"]) == 38
    assert body["meta"]["total"] == 38


def test_eval_report_endpoint(client: TestClient) -> None:
    body = client.get("/api/eval/report").json()
    assert "available" in body
    if body["available"]:
        assert body["report"]["scenarios_total"] == 38
    else:
        assert "run_eval" in body["note"]


def test_health_without_llm_key_reports_actionable_error(tmp_path: Any) -> None:
    env = dict(BASE_ENV)
    env["LLM_PROVIDER"] = "deepseek"
    env["AGENT_DB_PATH"] = str(tmp_path / "nokey2.db")
    with TestClient(create_app(AgentSettings.from_env(env=env))) as test_client:
        health = test_client.get("/api/health").json()
        assert health["provider_ready"] is False
        assert "LLM_API_KEY" in health["provider_error"]
        chat = test_client.post("/api/chat", json={"question": "教学楼为什么卡？"})
        assert chat.status_code == 503
        assert "LLM_API_KEY" in chat.json()["detail"]


# --------------------------------------------------------------------------- #
# W11：LangGraph 引擎（interrupt → confirm 恢复图 / cancel / classic 切换）
# --------------------------------------------------------------------------- #
def test_graph_chat_interrupt_then_confirm_resumes_graph(tmp_path: Any) -> None:
    settings = _settings(
        AGENT_ENGINE="langgraph",
        AGENT_FIXTURE_SCENARIO="s21",
        AGENT_DB_PATH=str(tmp_path / "graph-confirm.db"),
    )
    with TestClient(create_app(settings)) as test_client:
        response = test_client.post(
            "/api/chat", json={"question": "我想在宿舍楼加一台 2960 交换机"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["trace"]["engine"] == "langgraph"
        assert body["trace"]["graph"]["interrupted"] is True
        assert body["trace"]["stop_reason"] == "interrupt"
        assert len(body["actions"]) == 1
        action_id = body["actions"][0]["id"]
        assert body["actions"][0]["type"] == "apply_scene_device"

        confirmed = test_client.post("/api/actions/%s/confirm" % action_id)
        assert confirmed.status_code == 200, confirmed.text
        payload = confirmed.json()
        assert payload["ok"] is True
        assert payload["action"]["status"] == "executed"
        # 图已恢复并跑完（checkpoint 结束）
        engine = test_client.app.state.engine
        state = engine._graph.get_state(engine._graph_config(body["conversation_id"]))
        assert state.next == ()


def test_graph_chat_cancel_action_does_not_execute(tmp_path: Any) -> None:
    settings = _settings(
        AGENT_ENGINE="langgraph",
        AGENT_FIXTURE_SCENARIO="s26",
        AGENT_DB_PATH=str(tmp_path / "graph-cancel.db"),
    )
    with TestClient(create_app(settings)) as test_client:
        body = test_client.post(
            "/api/chat", json={"question": "把已恢复的告警清空"}
        ).json()
        assert len(body["actions"]) == 1
        action_id = body["actions"][0]["id"]
        cancelled = test_client.post("/api/actions/%s/cancel" % action_id).json()
        assert cancelled["action"]["status"] == "cancelled"
        # 指标数据未变化（取消不执行）
        backend = test_client.app.state.backend
        assert backend.get("/api/alerts")["summary"]["cleared"] == 3


def test_graph_paused_conversation_rejects_new_turn(tmp_path: Any) -> None:
    settings = _settings(
        AGENT_ENGINE="langgraph",
        AGENT_FIXTURE_SCENARIO="s21",
        AGENT_DB_PATH=str(tmp_path / "graph-paused.db"),
    )
    with TestClient(create_app(settings)) as test_client:
        first = test_client.post(
            "/api/chat", json={"question": "我想在宿舍楼加一台 2960 交换机"}
        ).json()
        second = test_client.post(
            "/api/chat",
            json={"question": "那 PC-JX1 呢？", "conversation_id": first["conversation_id"]},
        ).json()
        assert second["conversation_id"] == first["conversation_id"]
        assert "等待确认" in second["answer"]
        assert [action["id"] for action in second["actions"]] == [
            action["id"] for action in first["actions"]
        ]
        assert second["trace"]["graph"]["interrupted"] is True


def test_classic_engine_switch_and_confirm(tmp_path: Any) -> None:
    settings = _settings(
        AGENT_ENGINE="classic",
        AGENT_FIXTURE_SCENARIO="s26",
        AGENT_DB_PATH=str(tmp_path / "classic.db"),
    )
    with TestClient(create_app(settings)) as test_client:
        health = test_client.get("/api/health").json()
        assert health["engine"] == "classic"
        assert health["graph"]["engine"] == "classic"
        body = test_client.post("/api/chat", json={"question": "把已恢复的告警清空"}).json()
        assert len(body["actions"]) == 1
        assert body["trace"]["mode"] == "multi_agent"
        action_id = body["actions"][0]["id"]
        confirmed = test_client.post("/api/actions/%s/confirm" % action_id).json()
        assert confirmed["action"]["status"] == "executed"
        backend = test_client.app.state.backend
        assert backend.get("/api/alerts")["summary"]["cleared"] == 0


# --------------------------------------------------------------------------- #
# W12.1：写操作规则优先路由（trace.route_source=rule|llm）
# --------------------------------------------------------------------------- #
def test_write_intent_route_source_rule_and_readonly_llm(tmp_path: Any) -> None:
    settings = _settings(
        AGENT_FIXTURE_SCENARIO="s26", AGENT_DB_PATH=str(tmp_path / "routing.db")
    )
    with TestClient(create_app(settings)) as test_client:
        body = test_client.post(
            "/api/chat", json={"question": "把已恢复的告警清空"}
        ).json()
        assert body["trace"]["route_source"] == "rule"
        assert body["trace"]["route"]["rule"] == "write_clear_alerts"
        assert body["trace"]["route"]["action_hint"] == "clear_cleared_alerts"
        assert [item["role"] for item in body["trace"]["plan"]] == ["steward"]
        assert len(body["actions"]) == 1
        assert body["actions"][0]["type"] == "clear_cleared_alerts"
        assert body["actions"][0]["status"] == "pending"
        test_client.post("/api/actions/%s/cancel" % body["actions"][0]["id"])

        diagnosis = test_client.post(
            "/api/chat", json={"question": "现在整体网络状态怎么样"}
        ).json()
        assert diagnosis["trace"]["route_source"] == "llm"
        assert diagnosis["trace"]["route"]["rule"] is None
        assert [item["role"] for item in diagnosis["trace"]["plan"]] == ["diagnosis"]
        assert diagnosis["actions"] == []
