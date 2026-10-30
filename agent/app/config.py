# -*- coding: utf-8 -*-
"""Agent 服务配置：环境变量 / .env / provider preset。

用户要求「随时能换模型」，因此模型相关配置全部走环境变量（四个核心变量）：

- ``LLM_PROVIDER``    deepseek | openai | ollama | mock | openai_compat（任意兼容端点）
- ``LLM_BASE_URL``    OpenAI 兼容 base_url（覆盖 preset）
- ``LLM_API_KEY``     API key（只从环境变量/.env 读取，绝不写进代码）
- ``LLM_MODEL``       模型名（覆盖 preset）

默认（不配置任何环境变量）使用 **DeepSeek**：``https://api.deepseek.com/v1`` + ``deepseek-chat``。
Ollama / 自建端点走 ``openai_compat`` + 自定义 ``LLM_BASE_URL``。
``mock`` 是离线规则 provider，无需 key，用于单测、演示与 20 场景离线评测。

安全约束：密钥只在 ``AgentSettings.api_key`` / ``ProviderConfig.api_key`` 中短暂存在，
``repr`` / ``public_dict`` / 异常消息均不包含密钥。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from app.agent.providers.base import ProviderConfigError
from app.agent.redact import key_hint, redact

# campus-twin/agent/app/config.py -> parents[1] == campus-twin/agent
AGENT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = AGENT_ROOT / ".env"
DEFAULT_FIXTURE = AGENT_ROOT / "app" / "agent" / "eval" / "fixtures" / "base_snapshot.json"

#: 各 provider 的默认端点 / 默认模型 / 可接受的 key 环境变量。
PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_envs": ("LLM_API_KEY", "DEEPSEEK_API_KEY"),
        "key_required": True,
        "label": "DeepSeek 官方 API（默认）",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_envs": ("LLM_API_KEY", "OPENAI_API_KEY"),
        "key_required": True,
        "label": "OpenAI 官方 API",
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "api_key_envs": ("LLM_API_KEY",),
        "key_required": False,
        "label": "本地 Ollama（OpenAI 兼容接口，无需 key）",
    },
    "mock": {
        "base_url": "",
        "model": "mock-rules",
        "api_key_envs": (),
        "key_required": False,
        "label": "离线 mock 规则引擎（不联网，用于单测/演示/评测）",
    },
    "openai_compat": {
        "base_url": "",
        "model": "",
        "api_key_envs": ("LLM_API_KEY",),
        "key_required": False,
        "label": "任意 OpenAI 兼容端点（base_url/model 必填）",
    },
}

SUPPORTED_PROVIDERS: Tuple[str, ...] = tuple(PROVIDER_PRESETS.keys())

_ENV_LOADED = False


def load_env_file(path: Optional[Path] = None, force: bool = False) -> None:
    """加载 campus-twin/agent/.env（可选文件；不存在也不报错）。

    ``override=False``：已存在的真实环境变量优先，便于测试/CI 用环境变量覆盖 .env。
    """
    global _ENV_LOADED
    if _ENV_LOADED and not force:
        return
    target = Path(path) if path is not None else ENV_FILE
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=str(target), override=False)
    except Exception:  # noqa: BLE001 - .env 可选，缺 python-dotenv 也不影响启动
        pass
    finally:
        _ENV_LOADED = True


def _env(source: Mapping[str, str], name: str, default: Optional[str] = None) -> Optional[str]:
    raw = source.get(name)
    if raw is None:
        return default
    value = str(raw).strip()
    return value if value else default


def _int_env(source: Mapping[str, str], name: str, default: int) -> int:
    raw = _env(source, name)
    try:
        return int(str(raw)) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _float_env(source: Mapping[str, str], name: str, default: float) -> float:
    raw = _env(source, name)
    try:
        return float(str(raw)) if raw is not None else default
    except (TypeError, ValueError):
        return default


def normalize_provider(name: Optional[str]) -> str:
    """provider 名归一化（大小写/空格/常见别名）。"""
    value = (name or "").strip().lower().replace("-", "_")
    aliases = {
        "ds": "deepseek",
        "deep_seek": "deepseek",
        "openai_compatible": "openai_compat",
        "compatible": "openai_compat",
        "openai_compatible_endpoint": "openai_compat",
        "local": "ollama",
    }
    return aliases.get(value, value or "deepseek")


@dataclass
class ProviderConfig:
    """构造真实 provider 所需的完整配置（含密钥，勿直接序列化）。"""

    provider: str = "deepseek"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    key_source: Optional[str] = None
    temperature: float = 0.2
    timeout_s: float = 30.0
    max_retries: int = 2

    @property
    def key_configured(self) -> bool:
        return bool(self.api_key)

    def public_dict(self) -> Dict[str, Any]:
        """可安全写日志/返回 API 的公开信息。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "key_configured": self.key_configured,
            "key_source": self.key_source,
            "key_hint": key_hint(self.api_key),
            "temperature": self.temperature,
        }

    def sanitize(self, text: object) -> str:
        """对任意文本做密钥脱敏（异常回显前必须调用）。"""
        return redact(text, secrets=(self.api_key,))

    def __repr__(self) -> str:  # pragma: no cover - 防御性：防止误打印密钥
        return (
            "ProviderConfig(provider=%r, model=%r, base_url=%r, key_configured=%r)"
            % (self.provider, self.model, self.base_url, self.key_configured)
        )

    __str__ = __repr__


@dataclass
class AgentSettings:
    """Agent 服务全部配置（provider 部分 + 后端采集服务 + API 服务）。"""

    # ---- LLM provider（四件套解析后的结果） ----
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key: str = ""
    key_source: Optional[str] = None
    temperature: float = 0.2
    timeout_s: float = 30.0
    max_retries: int = 2
    # ---- Agent 行为 ----
    max_steps: int = 6
    max_tool_calls_per_step: int = 8
    # ---- 后端采集服务（真实数据源） ----
    backend_base_url: str = "http://127.0.0.1:8000"
    backend_timeout_s: float = 5.0
    data_source: str = "http"  # http | fixture
    fixture_snapshot: Path = field(default_factory=lambda: DEFAULT_FIXTURE)
    fixture_scenario: Optional[str] = None
    # ---- W9：会话持久化 / action 确认协议 / 多 Agent ----
    db_path: Path = field(default_factory=lambda: AGENT_ROOT / "data" / "agent.db")
    action_ttl_s: int = 600
    polish_llm: bool = True
    # ---- W11：编排引擎（LangGraph / classic 可回退） ----
    engine: str = "langgraph"
    graph_db_path: Path = field(default_factory=lambda: AGENT_ROOT / "data" / "graph.db")
    # ---- W13：MCP Bridge（外部 filesystem/memory 工具，只读接入） ----
    mcp_bridge_base: str = "http://127.0.0.1:8200"
    mcp_bridge_timeout_s: float = 5.0
    # ---- API 服务 ----
    api_host: str = "127.0.0.1"
    api_port: int = 8100
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "AgentSettings":
        """从环境变量构造配置。

        :param env: 测试可注入的纯字典（不读进程环境、不读 .env），保证单测确定性
        """
        if env is None:
            load_env_file()
            source: Mapping[str, str] = os.environ
        else:
            source = env

        provider = normalize_provider(_env(source, "LLM_PROVIDER", "deepseek"))
        preset = PROVIDER_PRESETS.get(provider, {})
        base_url = _env(source, "LLM_BASE_URL") or str(preset.get("base_url") or "")
        model = _env(source, "LLM_MODEL") or str(preset.get("model") or "")
        api_key, key_source = _resolve_api_key(source, provider, preset)

        data_source = (_env(source, "AGENT_DATA_SOURCE") or "http").strip().lower()
        if data_source not in ("http", "fixture"):
            data_source = "http"

        fixture = _env(source, "AGENT_FIXTURE_SNAPSHOT")
        fixture_path = Path(fixture) if fixture else DEFAULT_FIXTURE

        engine_raw = (_env(source, "AGENT_ENGINE") or "langgraph").strip().lower()
        engine = engine_raw if engine_raw in ("langgraph", "classic") else "langgraph"
        graph_db = _env(source, "AGENT_GRAPH_DB_PATH")

        return cls(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            key_source=key_source,
            temperature=_float_env(source, "LLM_TEMPERATURE", 0.2),
            timeout_s=_float_env(source, "LLM_TIMEOUT_S", 30.0),
            max_retries=max(0, _int_env(source, "LLM_MAX_RETRIES", 2)),
            max_steps=max(1, _int_env(source, "AGENT_MAX_STEPS", 6)),
            max_tool_calls_per_step=max(1, _int_env(source, "AGENT_MAX_TOOL_CALLS", 8)),
            backend_base_url=_env(source, "BACKEND_BASE_URL") or "http://127.0.0.1:8000",
            backend_timeout_s=_float_env(source, "BACKEND_TIMEOUT_S", 5.0),
            data_source=data_source,
            fixture_snapshot=fixture_path,
            fixture_scenario=_env(source, "AGENT_FIXTURE_SCENARIO"),
            db_path=Path(_env(source, "AGENT_DB_PATH") or str(AGENT_ROOT / "data" / "agent.db")),
            action_ttl_s=max(30, _int_env(source, "AGENT_ACTION_TTL_S", 600)),
            polish_llm=(_env(source, "AGENT_POLISH_LLM") or "1").strip().lower()
            not in ("0", "false", "no", "off"),
            engine=engine,
            graph_db_path=Path(graph_db or str(AGENT_ROOT / "data" / "graph.db")),
            mcp_bridge_base=_env(source, "MCP_BRIDGE_BASE") or "http://127.0.0.1:8200",
            mcp_bridge_timeout_s=_float_env(source, "MCP_BRIDGE_TIMEOUT_S", 5.0),
            api_host=_env(source, "AGENT_API_HOST") or "127.0.0.1",
            api_port=_int_env(source, "AGENT_API_PORT", 8100),
            log_level=(_env(source, "AGENT_LOG_LEVEL") or "INFO").upper(),
        )

    # ------------------------------------------------------------------ #
    # 派生视图（均不含密钥）
    # ------------------------------------------------------------------ #
    def ensure_dirs(self) -> None:
        """确保运行目录存在（会话/action/graph checkpointer SQLite 所在目录）。"""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.graph_db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - 目录不可写时交由存储层报错
            pass

    @property
    def provider_config(self) -> ProviderConfig:
        return ProviderConfig(
            provider=self.provider,
            base_url=self.base_url,
            model=self.model,
            api_key=self.api_key,
            key_source=self.key_source,
            temperature=self.temperature,
            timeout_s=self.timeout_s,
            max_retries=self.max_retries,
        )

    @property
    def has_llm_key(self) -> bool:
        return bool(self.api_key)

    def public_dict(self) -> Dict[str, Any]:
        """可安全写入 /api/health 的配置摘要。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "key_configured": self.has_llm_key,
            "key_source": self.key_source,
            "max_steps": self.max_steps,
            "data_source": self.data_source,
            "backend_base_url": self.backend_base_url,
            "action_ttl_s": self.action_ttl_s,
            "polish_llm": self.polish_llm,
            "engine": self.engine,
            "graph_db_path": str(self.graph_db_path),
            "mcp_bridge_base": self.mcp_bridge_base,
            "mcp_bridge_timeout_s": self.mcp_bridge_timeout_s,
        }

    def sanitize(self, text: object) -> str:
        return redact(text, secrets=(self.api_key,))

    def __repr__(self) -> str:  # pragma: no cover - 防御性
        return (
            "AgentSettings(provider=%r, model=%r, base_url=%r, key_configured=%r, "
            "data_source=%r, backend_base_url=%r)"
            % (
                self.provider,
                self.model,
                self.base_url,
                self.has_llm_key,
                self.data_source,
                self.backend_base_url,
            )
        )

    __str__ = __repr__


def _resolve_api_key(
    source: Mapping[str, str],
    provider: str,
    preset: Mapping[str, Any],
) -> Tuple[str, Optional[str]]:
    """按优先级解析 API key：LLM_API_KEY > provider 专属变量（如 DEEPSEEK_API_KEY）。

    返回值第二项是 key 来源的环境变量名（安全，可展示）；密钥原文只存在于配置对象内。
    """
    if provider == "mock":
        return "", None
    candidates = list(preset.get("api_key_envs") or ())
    # LLM_API_KEY 始终第一优先级（即使 preset 未列出）
    if "LLM_API_KEY" not in candidates:
        candidates.insert(0, "LLM_API_KEY")
    for name in candidates:
        value = _env(source, name)
        if value:
            return value, name
    return "", None


def resolve_provider_config(
    settings: Optional[AgentSettings] = None,
    provider_override: Optional[str] = None,
    model_override: Optional[str] = None,
) -> ProviderConfig:
    """生成 ProviderConfig；当覆盖了 provider 时按 preset 重新解析端点/模型。

    该函数只做「配置解析」，不做网络请求；缺少 key 时由 ``build_provider`` 抛
    ``ProviderConfigError``（便于 /api/health 在上游展示配置状态）。
    """
    settings = settings or AgentSettings.from_env()
    provider = normalize_provider(provider_override) if provider_override else settings.provider
    if provider not in PROVIDER_PRESETS:
        raise ProviderConfigError(
            "未知的 LLM_PROVIDER=%r，可用值：%s"
            % (provider, ", ".join(SUPPORTED_PROVIDERS))
        )
    preset = PROVIDER_PRESETS[provider]
    same_as_configured = not provider_override or provider == settings.provider
    if provider_override and not same_as_configured:
        # 切换到另一个 provider：用该 provider 的默认端点/模型，避免沿用上一个 provider 的地址
        base_url = str(preset.get("base_url") or "")
        model = str(preset.get("model") or "")
        load_env_file()
        api_key, key_source = _resolve_api_key(os.environ, provider, preset)
    else:
        # 未切换（或显式指定的就是当前 provider）：沿用环境配置的 base_url/model/key
        base_url = settings.base_url
        model = settings.model
        api_key = settings.api_key
        key_source = settings.key_source

    if model_override:
        model = model_override

    return ProviderConfig(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        key_source=key_source,
        temperature=settings.temperature,
        timeout_s=settings.timeout_s,
        max_retries=settings.max_retries,
    )
