# -*- coding: utf-8 -*-
"""provider 工厂：根据配置构造可切换的 LLMProvider。

切换方式（不改代码）：
- ``LLM_PROVIDER=deepseek``（默认）+ ``LLM_API_KEY``
- ``LLM_PROVIDER=openai`` + ``LLM_API_KEY``（或 OPENAI_API_KEY）
- ``LLM_PROVIDER=ollama``（本地，无需 key）
- ``LLM_PROVIDER=openai_compat`` + ``LLM_BASE_URL`` / ``LLM_MODEL``（任意兼容端点）
- ``LLM_PROVIDER=mock``（离线规则引擎）
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.agent.providers.base import LLMProvider, ProviderConfigError
from app.agent.providers.mock import MockProvider
from app.agent.providers.openai_compat import OpenAICompatProvider
from app.config import (
    PROVIDER_PRESETS,
    SUPPORTED_PROVIDERS,
    AgentSettings,
    ProviderConfig,
    normalize_provider,
    resolve_provider_config,
)


def build_provider(config: ProviderConfig) -> LLMProvider:
    """按 ProviderConfig 构造 provider；配置不合法时抛 ProviderConfigError。"""
    provider = normalize_provider(config.provider)
    if provider not in PROVIDER_PRESETS:
        raise ProviderConfigError(
            "未知的 LLM_PROVIDER=%r，可用值：%s"
            % (config.provider, ", ".join(SUPPORTED_PROVIDERS))
        )
    preset = PROVIDER_PRESETS[provider]

    if provider == "mock":
        return MockProvider()

    if provider == "openai_compat":
        missing = []
        if not config.base_url:
            missing.append("LLM_BASE_URL（OpenAI 兼容端点地址）")
        if not config.model:
            missing.append("LLM_MODEL（模型名）")
        if missing:
            raise ProviderConfigError(
                "LLM_PROVIDER=openai_compat 缺少必填配置：%s。"
                "示例：LLM_PROVIDER=openai_compat LLM_BASE_URL=http://127.0.0.1:11434/v1 LLM_MODEL=qwen2.5:7b"
                % "、".join(missing)
            )
    else:
        if not config.model:
            raise ProviderConfigError(
                "provider=%s 缺少模型名：请设置 LLM_MODEL，或使用该 provider 默认模型。"
                % provider
            )
        if bool(preset.get("key_required")) and not config.api_key:
            raise ProviderConfigError(
                "未配置 LLM API key：请把 key 写入 campus-twin/agent/.env 的 LLM_API_KEY"
                "（或环境变量 %s；当前 provider=%s）。API key 只从环境变量/.env 读取。"
                % ("/".join(preset.get("api_key_envs") or ("LLM_API_KEY",)), provider)
            )

    return OpenAICompatProvider(config)


def build_provider_from_settings(
    settings: Optional[AgentSettings] = None,
    provider_override: Optional[str] = None,
    model_override: Optional[str] = None,
) -> LLMProvider:
    """从环境配置（可带运行时覆盖）构造 provider。"""
    config = resolve_provider_config(settings, provider_override, model_override)
    return build_provider(config)


def provider_catalog() -> Dict[str, Any]:
    """返回 provider 能力清单（不含任何密钥），供 README/接口展示。"""
    catalog: Dict[str, Any] = {}
    for name, preset in PROVIDER_PRESETS.items():
        catalog[name] = {
            "label": preset.get("label"),
            "default_base_url": preset.get("base_url") or None,
            "default_model": preset.get("model") or None,
            "key_required": bool(preset.get("key_required")),
            "api_key_envs": list(preset.get("api_key_envs") or ()),
        }
    return {
        "default_provider": "deepseek",
        "supported": list(SUPPORTED_PROVIDERS),
        "providers": catalog,
        "switch_by": ["LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"],
        "note": "provider 全部实现同一 LLMProvider 协议；Ollama/自建端点走 openai_compat，"
        "mock 为离线规则引擎（无需 key）。",
    }
