# -*- coding: utf-8 -*-
"""基于 openai SDK 的通用 OpenAI 兼容 provider。

一套代码覆盖：
- DeepSeek 官方（默认）：``https://api.deepseek.com/v1`` + ``deepseek-chat``
- OpenAI 官方：``https://api.openai.com/v1`` + 任意 chat 模型
- Ollama 本地：``http://127.0.0.1:11434/v1``（key 可空，SDK 需要占位符）
- 任意自建 OpenAI 兼容端点：只改 ``LLM_BASE_URL`` / ``LLM_MODEL``

安全：错误消息统一经 ``ProviderConfig.sanitize`` 脱敏，绝不回显 API key。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.agent.providers.base import (
    AssistantMessage,
    LLMProviderError,
    ProviderInfo,
    ToolCallRequest,
)
from app.config import ProviderConfig


class OpenAICompatProvider:
    """OpenAI Chat Completions 协议 provider（DeepSeek/OpenAI/Ollama/自建均可）。"""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._client: Any = None

    # ------------------------------------------------------------------ #
    # 元信息
    # ------------------------------------------------------------------ #
    def info(self) -> ProviderInfo:
        config = self._config
        return ProviderInfo(
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            key_configured=config.key_configured,
            temperature=config.temperature,
        )

    @property
    def config(self) -> ProviderConfig:
        return self._config

    # ------------------------------------------------------------------ #
    # 客户端（惰性构造：导入本模块不要求 openai 已安装）
    # ------------------------------------------------------------------ #
    def _client_instance(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError:  # pragma: no cover - 依赖缺失时给出可操作提示
            raise LLMProviderError(
                "未安装 openai SDK，无法使用真实 LLM provider；"
                "请执行：python -m pip install openai（或使用 LLM_PROVIDER=mock 离线模式）"
            ) from None

        kwargs: Dict[str, Any] = {
            "api_key": self._config.api_key or "not-needed",
            "timeout": self._config.timeout_s,
            "max_retries": self._config.max_retries,
        }
        if self._config.base_url:
            kwargs["base_url"] = self._config.base_url
        try:
            self._client = OpenAI(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 统一转成脱敏后的领域异常
            raise LLMProviderError(
                self._sanitize(
                    "初始化 OpenAI 兼容客户端失败（provider=%s, base_url=%s）：%s: %s"
                    % (
                        self._config.provider,
                        self._config.base_url,
                        type(exc).__name__,
                        exc,
                    )
                )
            ) from None
        return self._client

    # ------------------------------------------------------------------ #
    # 对话
    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AssistantMessage:
        client = self._client_instance()
        request: Dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "temperature": self._config.temperature,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        try:
            completion = client.chat.completions.create(**request)
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权/限流统一处理，消息脱敏
            raise LLMProviderError(
                self._sanitize(
                    "LLM 调用失败（provider=%s, model=%s）：%s: %s"
                    % (
                        self._config.provider,
                        self._config.model,
                        type(exc).__name__,
                        _safe_message(exc),
                    )
                )
            ) from None

        return self._parse_completion(completion)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _sanitize(self, text: object) -> str:
        return self._config.sanitize(text)

    def _parse_completion(self, completion: Any) -> AssistantMessage:
        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise LLMProviderError(
                self._sanitize(
                    "LLM 返回空 choices（provider=%s, model=%s）"
                    % (self._config.provider, self._config.model)
                )
            )
        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise LLMProviderError("LLM 返回缺少 message 字段")

        content = getattr(message, "content", None) or ""
        tool_calls: List[ToolCallRequest] = []
        for index, call in enumerate(getattr(message, "tool_calls", None) or []):
            function = getattr(call, "function", None)
            name = str(getattr(function, "name", "") or "")
            raw_arguments = str(getattr(function, "arguments", "") or "")
            arguments: Dict[str, Any] = {}
            parse_error: Optional[str] = None
            if raw_arguments.strip():
                try:
                    parsed = json.loads(raw_arguments)
                except ValueError as exc:
                    parse_error = "工具参数不是合法 JSON：%s" % exc
                    parsed = None
                if parsed is not None and not isinstance(parsed, dict):
                    parse_error = "工具参数必须是 JSON 对象"
                elif isinstance(parsed, dict):
                    arguments = parsed
            tool_calls.append(
                ToolCallRequest(
                    id=str(getattr(call, "id", "") or ("call_%d" % (index + 1))),
                    name=name,
                    arguments=arguments,
                    raw_arguments=raw_arguments,
                    parse_error=parse_error,
                )
            )

        usage: Optional[Dict[str, Any]] = None
        raw_usage = getattr(completion, "usage", None)
        if raw_usage is not None:
            prompt_tokens = getattr(raw_usage, "prompt_tokens", None)
            completion_tokens = getattr(raw_usage, "completion_tokens", None)
            total_tokens = getattr(raw_usage, "total_tokens", None)
            if any(value is not None for value in (prompt_tokens, completion_tokens, total_tokens)):
                usage = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                }

        return AssistantMessage(
            content=content,
            tool_calls=tool_calls,
            finish_reason=getattr(choice, "finish_reason", None),
            usage=usage,
        )


def _safe_message(exc: BaseException) -> str:
    """优先取 SDK 错误里的 status_code/message，避免打印完整请求体。"""
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "message", None) or str(exc)
    if status is not None:
        return "HTTP %s: %s" % (status, message)
    return message
