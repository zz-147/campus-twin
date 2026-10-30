# -*- coding: utf-8 -*-
"""pytest 公共 fixture：全部离线（不依赖 LLM key / PT / 后端）。"""
from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path
from typing import Any, Dict

import pytest

# LangGraph/LangChain 的 PendingDeprecationWarning 在 import 时很吵，测试中静默
warnings.filterwarnings("ignore", message=".*allowed_objects.*")
try:  # pragma: no cover - 依赖存在时静默具体类别
    from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

    warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
except Exception:  # noqa: BLE001
    pass

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from app.agent.actions import ActionExecutor, ActionStore  # noqa: E402
from app.agent.agent import OpsAgent  # noqa: E402
from app.agent.backend import FixtureBackendClient  # noqa: E402
from app.agent.conversations import ConversationStore  # noqa: E402
from app.agent.eval.fixture_data import (  # noqa: E402
    BASE_SNAPSHOT_PATH,
    SCENARIOS_PATH,
    build_scenario_snapshot,
    load_base_snapshot,
    load_scenarios,
)
from app.agent.providers.factory import build_provider_from_settings  # noqa: E402
from app.agent.providers.mock import MockProvider  # noqa: E402
from app.agent.roles.orchestrator import Orchestrator  # noqa: E402
from app.agent.tools import OpsToolkit  # noqa: E402
from app.config import AgentSettings  # noqa: E402

#: 单测统一使用的纯配置（不读 .env / 进程环境）
MOCK_ENV: Dict[str, str] = {
    "LLM_PROVIDER": "mock",
    "LLM_API_KEY": "",
    "LLM_BASE_URL": "",
    "LLM_MODEL": "",
    "AGENT_DATA_SOURCE": "fixture",
}


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离进程环境：清掉可能存在的真实 key，避免单测意外联网/泄漏。"""
    for key in list(os.environ):
        if (
            key.startswith("LLM_")
            or key.startswith("AGENT_")
            or key.startswith("BACKEND_")
            or key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY")
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "mock")


@pytest.fixture()
def settings() -> AgentSettings:
    return AgentSettings.from_env(env=dict(MOCK_ENV))


@pytest.fixture(scope="session")
def base_snapshot() -> Dict[str, Any]:
    return load_base_snapshot(BASE_SNAPSHOT_PATH)


@pytest.fixture(scope="session")
def scenario_doc() -> Dict[str, Any]:
    return load_scenarios(SCENARIOS_PATH)


@pytest.fixture(scope="session")
def scenario_map(scenario_doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(item["id"]): item for item in scenario_doc["scenarios"]}


@pytest.fixture()
def fixture_backend(base_snapshot: Dict[str, Any]) -> FixtureBackendClient:
    return FixtureBackendClient(base_snapshot)


@pytest.fixture()
def scenario_backend(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]]
) -> FixtureBackendClient:
    """s01（PC-JX1 掉线）场景快照。"""
    snapshot = build_scenario_snapshot(base_snapshot, scenario_map["s01"])
    return FixtureBackendClient(snapshot)


@pytest.fixture()
def toolkit(fixture_backend: FixtureBackendClient) -> OpsToolkit:
    return OpsToolkit(fixture_backend)


@pytest.fixture()
def mock_provider() -> MockProvider:
    return MockProvider()


@pytest.fixture()
def mock_agent(toolkit: OpsToolkit, mock_provider: MockProvider) -> OpsAgent:
    return OpsAgent(provider=mock_provider, toolkit=toolkit, max_steps=6)


# --------------------------------------------------------------------------- #
# W9：会话 / action / 多 Agent 编排
# --------------------------------------------------------------------------- #
@pytest.fixture()
def conversation_store(tmp_path: Any) -> ConversationStore:
    return ConversationStore(tmp_path / "agent.db")


@pytest.fixture()
def action_store(tmp_path: Any) -> ActionStore:
    return ActionStore(tmp_path / "agent.db", ttl_s=600)


@pytest.fixture()
def action_executor(
    fixture_backend: FixtureBackendClient, conversation_store: ConversationStore
) -> ActionExecutor:
    return ActionExecutor(fixture_backend, conversation_store)


@pytest.fixture()
def orchestrator(
    mock_provider: MockProvider,
    toolkit: OpsToolkit,
    conversation_store: ConversationStore,
    action_store: ActionStore,
    settings: AgentSettings,
) -> Orchestrator:
    return Orchestrator(
        provider=mock_provider,
        toolkit=toolkit,
        conversations=conversation_store,
        actions=action_store,
        settings=settings,
    )


@pytest.fixture()
def scenario_factory(
    base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]]
):
    """按场景 id 构造 (fixture_backend, toolkit, orchestrator)。"""

    def build(scenario_id: str) -> Dict[str, Any]:
        scenario = scenario_map[scenario_id]
        snapshot = build_scenario_snapshot(base_snapshot, scenario)
        backend = FixtureBackendClient(snapshot)
        local_toolkit = OpsToolkit(backend)
        return {
            "scenario": scenario,
            "backend": backend,
            "toolkit": local_toolkit,
            "snapshot": snapshot,
        }

    return build
