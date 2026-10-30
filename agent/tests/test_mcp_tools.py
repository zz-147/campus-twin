# -*- coding: utf-8 -*-
"""W13 MCP 工具接入：schema/只读调用/白名单/优雅降级/路由节点/classic+langgraph。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.agent.actions import ActionExecutor, ActionStore
from app.agent.backend import FixtureBackendClient
from app.agent.conversations import ConversationStore
from app.agent.eval.fixture_data import build_scenario_snapshot
from app.agent.graph.graph import LangGraphEngine
from app.agent.mcp_client import MCPBridgeClient, MCPBridgeError
from app.agent.providers.mock import MockProvider
from app.agent.roles.base import RoleContext
from app.agent.roles.onboarding import OnboardingRole
from app.agent.roles.orchestrator import Orchestrator
from app.agent.routing import route_question
from app.agent.tools import (
    ALL_TOOL_NAMES,
    DIAGNOSIS_TOOL_NAMES,
    OpsToolkit,
)
from app.config import AgentSettings

FILE_TEXT = """! Core-3650 config
enable
configure terminal
hostname Core-3650
vlan 20
 name TEACHING
interface GigabitEthernet1/0/1
 switchport mode trunk
 switchport trunk allowed vlan 10,20
 ip address 192.168.10.1 255.255.255.0
"""

MCP_TOOLS = [
    "mcp_list_tools",
    "mcp_list_dir",
    "mcp_read_file",
    "mcp_search_files",
    "mcp_memory_search",
    "mcp_memory_add",
]


class FakeMCPBridge:
    """模拟 MCP Bridge HTTP 客户端（与 MCPBridgeClient 同接口）。"""

    base_url = "fake://mcp-bridge"
    calls: List[Any] = []

    def list_tools(self) -> Dict[str, Any]:
        return {
            "total": 23,
            "tools": [
                {
                    "server": "filesystem",
                    "name": "read_text_file",
                    "description": "read file",
                    "input_schema": {"type": "object"},
                },
                {
                    "server": "memory",
                    "name": "search_nodes",
                    "description": "search memory",
                    "input_schema": {"type": "object"},
                },
            ],
        }

    def call(self, server: str, tool: str, arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
        self.calls.append((server, tool, arguments or {}))
        if server == "filesystem" and tool in ("read_text_file", "read_file"):
            return {
                "ok": True,
                "server": server,
                "tool": tool,
                "result": {"text": FILE_TEXT, "content": [{"type": "text", "text": FILE_TEXT}]},
                "error": None,
                "elapsed_ms": 3.2,
            }
        if server == "filesystem" and tool == "list_directory":
            return {
                "ok": True,
                "server": server,
                "tool": tool,
                "result": {"text": "core-3650.txt\nsw-jx-2960.txt"},
                "error": None,
                "elapsed_ms": 1.0,
            }
        if server == "filesystem" and tool == "search_files":
            return {
                "ok": True,
                "server": server,
                "tool": tool,
                "result": {"text": "Found 1: core-3650.txt"},
                "error": None,
                "elapsed_ms": 1.0,
            }
        if server == "memory" and tool == "search_nodes":
            return {
                "ok": True,
                "server": server,
                "tool": tool,
                "result": {"text": '{"entities":[{"name":"bridge-verify"}]}'},
                "error": None,
                "elapsed_ms": 1.0,
            }
        if server == "memory" and tool == "create_entities":
            return {
                "ok": True,
                "server": server,
                "tool": tool,
                "result": {"text": '{"entities":[{"name":"campus-note"}]}'},
                "error": None,
                "elapsed_ms": 1.0,
            }
        return {
            "ok": False,
            "server": server,
            "tool": tool,
            "result": {},
            "error": "not allowed",
            "elapsed_ms": 1.0,
        }


class UnavailableMCPBridge:
    base_url = "http://127.0.0.1:8200"

    def list_tools(self) -> Dict[str, Any]:
        raise MCPBridgeError("MCP Bridge 未启动或不可达（http://127.0.0.1:8200）")

    def call(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise MCPBridgeError("MCP Bridge 未启动或不可达（http://127.0.0.1:8200）")


def _toolkit(base_snapshot: Dict[str, Any], bridge: Any) -> OpsToolkit:
    return OpsToolkit(FixtureBackendClient(base_snapshot), mcp_bridge=bridge)


def test_mcp_tools_registered_in_schema_and_whitelists() -> None:
    for name in MCP_TOOLS:
        assert name in ALL_TOOL_NAMES
    for name in MCP_TOOLS[:5]:
        assert name in DIAGNOSIS_TOOL_NAMES
    role = OnboardingRole(OpsToolkit(FixtureBackendClient({})))
    assert set(MCP_TOOLS[:5]) <= set(role.allowed_tools)


def test_mcp_tool_calls_through_toolkit(base_snapshot: Dict[str, Any]) -> None:
    bridge = FakeMCPBridge()
    toolkit = _toolkit(base_snapshot, bridge)
    listing = toolkit.call("mcp_list_tools", {})
    assert listing["ok"] is True
    assert listing["data"]["total"] == 23
    assert "filesystem" in listing["data"]["servers"]

    read = toolkit.call(
        "mcp_read_file", {"path": r"D:\zdsh\campus-twin\configs\core-3650.txt"}
    )
    assert read["ok"] is True
    assert "vlan 20" in read["data"]["content"]
    assert read["data"]["mcp_calls"][0]["server"] == "filesystem"
    assert read["data"]["mcp_calls"][0]["ok"] is True

    listed = toolkit.call("mcp_list_dir", {"path": r"D:\zdsh\campus-twin\configs"})
    assert listed["ok"] is True and "core-3650.txt" in listed["data"]["text"]
    searched = toolkit.call(
        "mcp_search_files", {"path": r"D:\zdsh\campus-twin\configs", "pattern": "core-*.txt"}
    )
    assert searched["ok"] is True and "core-3650.txt" in searched["data"]["text"]
    memory = toolkit.call("mcp_memory_search", {"query": "bridge-verify"})
    assert memory["ok"] is True and "bridge-verify" in memory["data"]["text"]
    added = toolkit.call("mcp_memory_add", {"content": "note for test"})
    assert added["ok"] is True
    assert "MCP memory" in added["summary"]
    assert added["data"]["note"].startswith("本地 memory")


def test_mcp_bridge_unavailable_graceful(base_snapshot: Dict[str, Any]) -> None:
    toolkit = _toolkit(base_snapshot, UnavailableMCPBridge())
    for name, args in (
        ("mcp_list_tools", {}),
        ("mcp_list_dir", {"path": "configs"}),
        ("mcp_read_file", {"path": "configs/core-3650.txt"}),
        ("mcp_search_files", {"path": "configs", "pattern": "*.txt"}),
        ("mcp_memory_search", {"query": "x"}),
        ("mcp_memory_add", {"content": "x"}),
    ):
        envelope = toolkit.call(name, args)
        assert envelope["ok"] is False, name
        assert "MCP Bridge" in (envelope["error"] or ""), envelope["error"]


def test_mcp_routing_rules() -> None:
    read = route_question("帮我读一下 configs/core-3650.txt 里 VLAN 配置")
    assert read.source == "rule"
    assert read.intent == "mcp_read"
    assert read.rule == "mcp_read"
    listed = route_question("MCP 现在接入了哪些工具")
    assert listed.source == "rule"
    assert listed.intent == "mcp_tools"
    assert listed.rule == "mcp_tools"
    # 反例：不带“读/内容”等动作的路径提及不应短路
    assert route_question("configs 目录是什么").source == "llm"


def test_orchestrator_mcp_read_and_tools(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, {"id": "tmp_mcp", "w9": {}})
    db = tmp_path / "mcp.db"
    orchestrator = Orchestrator(
        MockProvider(),
        _toolkit(snapshot, FakeMCPBridge()),
        ConversationStore(db),
        ActionStore(db),
        settings,
    )
    read = orchestrator.handle("帮我读一下 configs/core-3650.txt 里 VLAN 配置")
    assert [item["role"] for item in read.trace["plan"]] == ["mcp_read"]
    assert read.trace["route_source"] == "rule"
    assert "switchport mode trunk" in read.answer
    assert "Core-3650" in read.answer
    assert read.actions == []
    assert read.trace["mcp_calls"]
    assert "mcp_read_file" in read.trace["tool_sequence"]

    tools = orchestrator.handle("MCP 现在接入了哪些工具")
    assert [item["role"] for item in tools.trace["plan"]] == ["mcp_tools"]
    assert "filesystem" in tools.answer and "memory" in tools.answer
    assert tools.trace["mcp_calls"]


def test_langgraph_mcp_read(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    snapshot = build_scenario_snapshot(base_snapshot, {"id": "tmp_mcp_graph", "w9": {}})
    db = tmp_path / "graph.db"
    backend = FixtureBackendClient(snapshot)
    conversations = ConversationStore(db)
    engine = LangGraphEngine(
        MockProvider(),
        _toolkit(snapshot, FakeMCPBridge()),
        conversations,
        ActionStore(db),
        settings,
        executor=ActionExecutor(backend, conversations),
        graph_db_path=tmp_path / "graph-checkpoint.db",
    )
    outcome = engine.handle("帮我读一下 configs/core-3650.txt 里 VLAN 配置")
    assert [item["role"] for item in outcome.trace["plan"]] == ["mcp_read"]
    assert "switchport mode trunk" in outcome.answer
    assert outcome.trace["route_source"] == "rule"
    assert outcome.trace["mcp_calls"]
    engine.close()
