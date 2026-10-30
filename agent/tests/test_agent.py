# -*- coding: utf-8 -*-
"""Agent 编排循环：mock 全链路、步数上限、工具错误容错、trace 无密钥。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.agent.agent import AgentRunResult, OpsAgent, SYSTEM_PROMPT
from app.agent.backend import BackendError
from app.agent.providers.base import (
    AssistantMessage,
    ProviderInfo,
    ToolCallRequest,
)
from app.agent.providers.factory import build_provider_from_settings
from app.agent.providers.mock import MockProvider
from app.agent.tools import OpsToolkit
from app.config import AgentSettings

SECRET = "sk-agent-trace-secret-0123456789"


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


# --------------------------------------------------------------------------- #
# 使用真实 mock provider + fixture 工具的完整链路
# --------------------------------------------------------------------------- #
def test_mock_agent_full_loop(mock_agent: OpsAgent) -> None:
    result = mock_agent.run("教学楼 PC-JX1 上不了网，帮我看下是什么原因？")
    assert isinstance(result, AgentRunResult)
    assert _has_chinese(result.answer)
    for section in ("## 根因", "## 依据", "## 排障步骤", "## 建议命令"):
        assert section in result.answer
    trace = result.trace
    assert trace["provider"] == "mock"
    assert trace["model"] == "mock-rules"
    assert trace["backend_mode"] == "fixture"
    assert trace["stop_reason"] == "final_answer"
    assert trace["tool_call_count"] >= 3
    assert trace["tool_error_count"] == 0
    assert trace["steps"]
    assert len(trace["steps"]) <= trace["max_steps"]
    sequence = trace["tool_sequence"]
    for expected in ("get_health", "get_devices", "get_hosts", "get_issues", "get_alerts"):
        assert expected in sequence
    assert result.elapsed_ms >= 0.0


def test_mock_agent_uses_device_status_in_answer(mock_agent: OpsAgent) -> None:
    result = mock_agent.run("宿舍楼 PC-SS2 掉线了，帮忙定位。")
    # 基线快照没有故障 → 诚实回答"未发现明确异常"，并给出数据统计
    assert "未发现明确故障" in result.answer or "未发现" in result.answer
    assert "6 台主机" in result.answer


def test_mock_agent_with_fault_scenario() -> None:
    from app.agent.backend import FixtureBackendClient
    from app.agent.eval.fixture_data import build_scenario_snapshot, load_base_snapshot, load_scenarios

    base = load_base_snapshot()
    scenarios = {item["id"]: item for item in load_scenarios()["scenarios"]}
    snapshot = build_scenario_snapshot(base, scenarios["s01"])
    agent = OpsAgent(MockProvider(), OpsToolkit(FixtureBackendClient(snapshot)))
    result = agent.run("教学楼 PC-JX1 上不了网，帮我看下是什么原因？")
    assert "PC-JX1" in result.answer
    assert "192.168.20.10" in result.answer
    assert "FAILURE" in result.answer
    assert "终端" in result.answer


# --------------------------------------------------------------------------- #
# 步数上限 / 强制收敛
# --------------------------------------------------------------------------- #
class _AlwaysToolProvider:
    """每轮都要求调工具，用来验证 max_steps 上限与最后一步强制不给工具。"""

    def __init__(self) -> None:
        self.calls = 0
        self.tools_per_call: List[Optional[List[Dict[str, Any]]]] = []

    def info(self) -> ProviderInfo:
        return ProviderInfo(provider="fake", model="always-tool")

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AssistantMessage:
        self.calls += 1
        self.tools_per_call.append(tools)
        return AssistantMessage(
            content="",
            tool_calls=[
                ToolCallRequest(id="call_%d" % self.calls, name="get_devices", arguments={})
            ],
            finish_reason="tool_calls",
        )


def test_agent_respects_max_steps_and_forces_final(toolkit: OpsToolkit) -> None:
    provider = _AlwaysToolProvider()
    agent = OpsAgent(provider, toolkit, max_steps=3)
    result = agent.run("测试问题")
    assert provider.calls == 3
    assert provider.tools_per_call[0] is not None
    assert provider.tools_per_call[-1] is None  # 最后一步强制不再给工具
    assert len(result.trace["steps"]) == 3
    assert result.trace["stop_reason"] in ("empty_response", "max_steps")
    assert _has_chinese(result.answer)


class _ToolThenAnswerProvider:
    def __init__(self) -> None:
        self.calls = 0

    def info(self) -> ProviderInfo:
        return ProviderInfo(provider="fake", model="tool-then-answer")

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> AssistantMessage:
        self.calls += 1
        if tools:
            return AssistantMessage(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="get_devices", arguments={})],
                finish_reason="tool_calls",
            )
        return AssistantMessage(content="## 根因\n设备均正常。", finish_reason="stop")


def test_agent_final_answer_from_forced_step(toolkit: OpsToolkit) -> None:
    agent = OpsAgent(_ToolThenAnswerProvider(), toolkit, max_steps=2)
    result = agent.run("测试")
    assert result.answer == "## 根因\n设备均正常。"
    assert result.trace["stop_reason"] == "final_answer"


# --------------------------------------------------------------------------- #
# 工具失败容错
# --------------------------------------------------------------------------- #
class _BrokenBackend:
    mode = "http"

    def get(self, path: str, params: Any = None) -> Dict[str, Any]:
        raise BackendError("后端不可达（http://127.0.0.1:8000）：ConnectError")


def test_agent_degrades_gracefully_when_backend_down() -> None:
    toolkit = OpsToolkit(_BrokenBackend())
    agent = OpsAgent(MockProvider(), toolkit, max_steps=6)
    result = agent.run("教学楼 PC-JX1 为什么掉线？")
    assert _has_chinese(result.answer)
    assert result.trace["tool_error_count"] > 0
    assert "后端" in result.answer or "无法" in result.answer
    # 工具错误信息进入 trace，但不泄露任何密钥
    assert "sk-" not in json.dumps(result.trace, ensure_ascii=False)


def test_empty_question_rejected(mock_agent: OpsAgent) -> None:
    with pytest.raises(ValueError):
        mock_agent.run("   ")


# --------------------------------------------------------------------------- #
# 密钥零暴露
# --------------------------------------------------------------------------- #
def test_trace_never_contains_api_key(monkeypatch: pytest.MonkeyPatch, toolkit: OpsToolkit) -> None:
    class FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="## 根因\n终端正常。", tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("openai.OpenAI", FakeClient)
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "deepseek", "LLM_API_KEY": SECRET})
    provider = build_provider_from_settings(settings)
    agent = OpsAgent(provider, toolkit)
    result = agent.run("教学楼为什么卡？")
    serialized_trace = json.dumps(result.trace, ensure_ascii=False)
    assert SECRET not in serialized_trace
    assert SECRET not in result.answer
    assert result.trace["provider"] == "deepseek"
    assert result.trace["key_configured"] is True
    assert "sk-" not in serialized_trace


def test_system_prompt_enforces_grounding() -> None:
    for keyword in ("禁止编造", "根因", "依据", "排障步骤", "建议命令", "真实数据"):
        assert keyword in SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# 续聊：历史消息注入（W9 会话持久化配套）
# --------------------------------------------------------------------------- #
class _RecordingProvider:
    def __init__(self) -> None:
        self.seen_messages: List[Dict[str, Any]] = []

    def info(self) -> ProviderInfo:
        return ProviderInfo(provider="fake", model="recording")

    def chat(self, messages: List[Dict[str, Any]], tools: Any = None) -> AssistantMessage:
        self.seen_messages = messages
        return AssistantMessage(content="## 根因\n沿用前文。", finish_reason="stop")


def test_ops_agent_receives_conversation_history(toolkit: OpsToolkit) -> None:
    provider = _RecordingProvider()
    agent = OpsAgent(provider, toolkit)
    history = [
        {"role": "user", "content": "上一轮问题：PC-JX1 掉线"},
        {"role": "assistant", "content": "上一轮回答：终端离线"},
    ]
    result = agent.run("那 PC-JX2 呢？", history=history)
    contents = [message["content"] for message in provider.seen_messages]
    assert provider.seen_messages[0]["role"] == "system"
    assert "上一轮问题：PC-JX1 掉线" in contents
    assert "上一轮回答：终端离线" in contents
    assert contents[-1] == "那 PC-JX2 呢？"
    assert result.trace["history_messages"] == 2


def test_ops_agent_ignores_invalid_history(toolkit: OpsToolkit) -> None:
    provider = _RecordingProvider()
    agent = OpsAgent(provider, toolkit)
    result = agent.run(
        "继续",
        history=[{"role": "system", "content": "不应注入"}, {"role": "user", "content": ""}],
    )
    assert result.trace["history_messages"] == 0
