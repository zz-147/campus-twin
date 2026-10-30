# -*- coding: utf-8 -*-
"""配置解析 + provider 工厂 + OpenAI 兼容层（用 fake client，不联网）。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.agent.providers.base import (
    AssistantMessage,
    LLMProviderError,
    ProviderConfigError,
)
from app.agent.providers.factory import (
    build_provider,
    build_provider_from_settings,
    provider_catalog,
)
from app.agent.providers.mock import MockProvider
from app.agent.providers.openai_compat import OpenAICompatProvider
from app.config import AgentSettings, resolve_provider_config

SECRET = "sk-unit-test-secret-0123456789abcdef"
DEEPSEEK_KEY = "sk-deepseek-secret-0123456789abcdef"


# --------------------------------------------------------------------------- #
# 默认值与 preset
# --------------------------------------------------------------------------- #
def test_default_provider_is_deepseek() -> None:
    settings = AgentSettings.from_env(env={})
    assert settings.provider == "deepseek"
    assert settings.base_url == "https://api.deepseek.com/v1"
    assert settings.model == "deepseek-chat"
    assert settings.api_key == ""
    assert settings.has_llm_key is False


def test_env_switch_to_openai() -> None:
    settings = AgentSettings.from_env(
        env={"LLM_PROVIDER": "openai", "LLM_API_KEY": SECRET, "LLM_MODEL": "gpt-4o"}
    )
    assert settings.provider == "openai"
    assert settings.base_url == "https://api.openai.com/v1"
    assert settings.model == "gpt-4o"
    assert settings.key_source == "LLM_API_KEY"
    provider = build_provider_from_settings(settings)
    assert isinstance(provider, OpenAICompatProvider)
    info = provider.info()
    assert info.provider == "openai"
    assert info.model == "gpt-4o"
    assert info.key_configured is True


def test_base_url_and_model_override() -> None:
    settings = AgentSettings.from_env(
        env={
            "LLM_PROVIDER": "deepseek",
            "LLM_API_KEY": DEEPSEEK_KEY,
            "LLM_BASE_URL": "http://192.168.1.5:8000/v1",
            "LLM_MODEL": "my-deepseek-model",
        }
    )
    assert settings.base_url == "http://192.168.1.5:8000/v1"
    assert settings.model == "my-deepseek-model"
    config = settings.provider_config
    assert config.base_url == "http://192.168.1.5:8000/v1"
    assert config.model == "my-deepseek-model"


def test_provider_specific_key_fallback() -> None:
    settings = AgentSettings.from_env(
        env={"LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": DEEPSEEK_KEY}
    )
    assert settings.api_key == DEEPSEEK_KEY
    assert settings.key_source == "DEEPSEEK_API_KEY"
    provider = build_provider_from_settings(settings)
    assert provider.info().key_configured is True


def test_ollama_preset_without_key() -> None:
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "ollama"})
    provider = build_provider_from_settings(settings)
    info = provider.info()
    assert info.provider == "ollama"
    assert info.base_url == "http://127.0.0.1:11434/v1"
    assert info.model == "qwen2.5:7b"
    assert info.key_configured is False


def test_mock_provider_needs_no_key() -> None:
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "mock"})
    provider = build_provider_from_settings(settings)
    assert isinstance(provider, MockProvider)
    assert provider.info().provider == "mock"


def test_openai_compat_requires_base_url_and_model() -> None:
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "openai_compat"})
    with pytest.raises(ProviderConfigError) as exc_info:
        build_provider_from_settings(settings)
    message = str(exc_info.value)
    assert "LLM_BASE_URL" in message
    assert "LLM_MODEL" in message


def test_openai_compat_custom_endpoint() -> None:
    settings = AgentSettings.from_env(
        env={
            "LLM_PROVIDER": "openai_compat",
            "LLM_BASE_URL": "http://10.0.0.5:8000/v1",
            "LLM_MODEL": "qwen2.5-14b",
        }
    )
    provider = build_provider_from_settings(settings)
    assert isinstance(provider, OpenAICompatProvider)
    info = provider.info()
    assert info.base_url == "http://10.0.0.5:8000/v1"
    assert info.model == "qwen2.5-14b"
    # 本地兼容端点允许不带 key（openai SDK 会拿到占位符）
    assert info.key_configured is False


# --------------------------------------------------------------------------- #
# 错误与密钥安全
# --------------------------------------------------------------------------- #
def test_deepseek_missing_key_error_is_actionable_and_safe() -> None:
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "deepseek", "LLM_API_KEY": ""})
    with pytest.raises(ProviderConfigError) as exc_info:
        build_provider_from_settings(settings)
    message = str(exc_info.value)
    assert "LLM_API_KEY" in message
    assert ".env" in message
    assert "sk-" not in message


def test_api_key_never_in_repr_public_dict_or_sanitize() -> None:
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "deepseek", "LLM_API_KEY": SECRET})
    config = settings.provider_config
    assert SECRET not in repr(config)
    assert SECRET not in str(config)
    assert SECRET not in json.dumps(config.public_dict(), ensure_ascii=False)
    assert config.public_dict()["key_configured"] is True
    assert config.public_dict()["key_hint"] != SECRET
    cleaned = config.sanitize("call failed: providers said api_key=%s" % SECRET)
    assert SECRET not in cleaned
    assert "REDACTED" in cleaned
    assert SECRET not in repr(settings)


def test_unknown_provider_is_rejected_with_choices() -> None:
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "some-unknown-llm"})
    with pytest.raises(ProviderConfigError) as exc_info:
        build_provider_from_settings(settings)
    message = str(exc_info.value)
    assert "deepseek" in message
    assert "mock" in message


def test_provider_catalog_contains_no_secrets() -> None:
    catalog = provider_catalog()
    assert catalog["default_provider"] == "deepseek"
    supported = set(catalog["supported"])
    assert {"deepseek", "openai", "ollama", "mock", "openai_compat"} <= supported
    serialized = json.dumps(catalog, ensure_ascii=False)
    assert "sk-" not in serialized
    assert catalog["switch_by"] == [
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
    ]


# --------------------------------------------------------------------------- #
# 运行时覆盖（切换 provider/model）
# --------------------------------------------------------------------------- #
def test_runtime_provider_override(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = AgentSettings.from_env(
        env={"LLM_PROVIDER": "deepseek", "LLM_API_KEY": DEEPSEEK_KEY}
    )
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    override = build_provider_from_settings(settings, provider_override="openai")
    assert isinstance(override, OpenAICompatProvider)
    assert override.info().provider == "openai"
    assert override.info().model == "gpt-4o-mini"
    assert override.info().base_url == "https://api.openai.com/v1"

    settings_with_model = AgentSettings.from_env(
        env={"LLM_PROVIDER": "deepseek", "LLM_API_KEY": DEEPSEEK_KEY}
    )
    override_model = build_provider_from_settings(
        settings_with_model, provider_override="openai", model_override="gpt-4o"
    )
    assert override_model.info().model == "gpt-4o"

    mock = build_provider_from_settings(settings, provider_override="mock")
    assert isinstance(mock, MockProvider)

    # 显式指定的 provider 就是当前 provider 时：保留环境配置的 model/base_url
    custom = AgentSettings.from_env(
        env={
            "LLM_PROVIDER": "deepseek",
            "LLM_API_KEY": DEEPSEEK_KEY,
            "LLM_MODEL": "deepseek-flash",
        }
    )
    same = build_provider_from_settings(custom, provider_override="deepseek")
    assert same.info().provider == "deepseek"
    assert same.info().model == "deepseek-flash"
    assert same.info().base_url == "https://api.deepseek.com/v1"


def test_resolve_provider_config_keeps_key_out() -> None:
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "deepseek", "LLM_API_KEY": SECRET})
    config = resolve_provider_config(settings)
    assert config.api_key == SECRET
    assert SECRET not in repr(config)


# --------------------------------------------------------------------------- #
# W9 配置：会话 DB / action TTL / LLM 汇总开关
# --------------------------------------------------------------------------- #
def test_w9_settings_defaults() -> None:
    settings = AgentSettings.from_env(env={})
    assert settings.action_ttl_s == 600
    assert settings.polish_llm is True
    assert str(settings.db_path).endswith("agent.db")
    assert settings.ensure_dirs() is None


def test_w9_settings_env_overrides() -> None:
    settings = AgentSettings.from_env(
        env={
            "AGENT_DB_PATH": "D:/tmp/test-agent.db",
            "AGENT_ACTION_TTL_S": "120",
            "AGENT_POLISH_LLM": "0",
        }
    )
    assert str(settings.db_path).replace("\\", "/") == "D:/tmp/test-agent.db"
    assert settings.action_ttl_s == 120
    assert settings.polish_llm is False
    public = settings.public_dict()
    assert public["action_ttl_s"] == 120
    assert public["polish_llm"] is False


# --------------------------------------------------------------------------- #
# OpenAI 兼容层：fake SDK（不联网）
# --------------------------------------------------------------------------- #
class _FakeCompletions:
    def __init__(self, captured: Dict[str, Any], response: Any, error: Optional[Exception]) -> None:
        self._captured = captured
        self._response = response
        self._error = error

    def create(self, **kwargs: Any) -> Any:
        self._captured["request"] = kwargs
        if self._error is not None:
            raise self._error
        return self._response


class _FakeChat:
    def __init__(self, captured: Dict[str, Any], response: Any, error: Optional[Exception]) -> None:
        self.completions = _FakeCompletions(captured, response, error)


class _FakeOpenAIClient:  # pragma: no cover - 仅用于类型说明，测试内联构造
    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs


def _make_completion(
    content: str = "",
    tool_name: Optional[str] = None,
    tool_arguments: str = "{}",
) -> Any:
    tool_calls = None
    if tool_name is not None:
        tool_calls = [
            SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name=tool_name, arguments=tool_arguments),
            )
        ]
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
    )


def _patch_openai(
    monkeypatch: pytest.MonkeyPatch,
    response: Any = None,
    error: Optional[Exception] = None,
) -> Dict[str, Any]:
    captured: Dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["init_kwargs"] = kwargs
            self.chat = _FakeChat(captured, response, error)

    monkeypatch.setattr("openai.OpenAI", FakeClient)
    return captured


def test_openai_compat_chat_and_tool_call_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_openai(
        monkeypatch,
        response=_make_completion(content="", tool_name="get_devices", tool_arguments='{"limit": 5}'),
    )
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "deepseek", "LLM_API_KEY": SECRET})
    provider = build_provider_from_settings(settings)
    message: AssistantMessage = provider.chat(
        [{"role": "user", "content": "查设备"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_devices",
                    "description": "设备清单",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    init_kwargs = captured["init_kwargs"]
    assert init_kwargs["base_url"] == "https://api.deepseek.com/v1"
    assert init_kwargs["api_key"] == SECRET
    assert captured["request"]["model"] == "deepseek-chat"
    assert captured["request"]["tools"][0]["function"]["name"] == "get_devices"
    assert message.tool_calls[0].name == "get_devices"
    assert message.tool_calls[0].arguments == {"limit": 5}
    assert message.tool_calls[0].parse_error is None
    assert message.usage is not None and message.usage["total_tokens"] == 8


def test_openai_compat_invalid_tool_arguments_marks_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_openai(
        monkeypatch,
        response=_make_completion(content="", tool_name="get_devices", tool_arguments="{not-json"),
    )
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "openai", "LLM_API_KEY": SECRET})
    provider = build_provider_from_settings(settings)
    message = provider.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "get_devices", "parameters": {"type": "object", "properties": {}}}}])
    assert message.tool_calls[0].arguments == {}
    assert message.tool_calls[0].parse_error is not None


def test_openai_compat_error_message_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_openai(
        monkeypatch,
        error=RuntimeError("401 auth failed with api_key=" + SECRET),
    )
    settings = AgentSettings.from_env(env={"LLM_PROVIDER": "deepseek", "LLM_API_KEY": SECRET})
    provider = build_provider_from_settings(settings)
    with pytest.raises(LLMProviderError) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    message = str(exc_info.value)
    assert SECRET not in message
    assert "deepseek" in message
