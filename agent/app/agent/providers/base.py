# -*- coding: utf-8 -*-
"""LLM provider 抽象协议。

设计目标（用户硬性要求「随时能换模型」）：
- 上层编排循环只依赖 ``LLMProvider`` 协议，不关心背后是 DeepSeek / OpenAI /
  Ollama / 任意 OpenAI 兼容端点，还是离线 mock 规则引擎；
- provider 通过环境变量（LLM_PROVIDER / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）
  配置，切换模型不需要改代码；
- 统一的消息结构：``chat(messages, tools=None) -> AssistantMessage``。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


class ProviderConfigError(ValueError):
    """provider 配置错误（缺 API key / 缺 base_url / provider 名非法）。

    **消息中不得包含密钥明文**（由抛出方保证），可直接展示给用户。
    """


class LLMProviderError(RuntimeError):
    """模型调用失败。消息已脱敏，可安全写入日志/API 响应。"""


@dataclass
class ToolCallRequest:
    """LLM 请求执行的一次工具调用。"""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: Optional[str] = None


@dataclass
class AssistantMessage:
    """一次 LLM 调用的归一化输出（文本 或 工具调用）。"""

    content: str = ""
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None

    def to_openai_message(self) -> Dict[str, Any]:
        """还原成 OpenAI 协议里的 assistant 消息，供编排循环回填上下文。"""
        message: Dict[str, Any] = {
            "role": "assistant",
            "content": self.content or None,
        }
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments
                        or json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return message


@dataclass(frozen=True)
class ProviderInfo:
    """provider 的可公开信息（绝不含密钥）。"""

    provider: str
    model: str
    base_url: str = ""
    key_configured: bool = False
    temperature: float = 0.2

    def public_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "key_configured": self.key_configured,
            "temperature": self.temperature,
        }


@runtime_checkable
class LLMProvider(Protocol):
    """所有 provider（真实/离线 mock）实现的统一协议。"""

    def info(self) -> ProviderInfo:
        """返回 provider 元信息（不含密钥）。"""
        ...

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AssistantMessage:
        """执行一次对话补全。

        :param messages: OpenAI 风格消息列表（system/user/assistant/tool）
        :param tools: OpenAI function-calling schema 列表；None/空表示不允许调工具
        """
        ...
