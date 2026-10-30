# -*- coding: utf-8 -*-
"""MCP Bridge 单元测试（stdlib unittest，pytest 兼容；py3.14 venv 未装 pytest）。

运行：
    D:\\zdsh\\campus-twin\\.venv-mcp314\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from mcp import types
from starlette.testclient import TestClient

from bridge import (
    BridgeError,
    MCPBridgeManager,
    create_app,
    load_config,
    normalize_result,
    normalize_tool,
)

ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict:
    return {
        "servers": [
            {
                "name": "filesystem",
                "command": "npx",
                "args": ["-y", "server-filesystem"],
                "env": {},
                "allowed_tools": ["read_text_file", "list_directory"],
            },
            {
                "name": "memory",
                "command": "npx",
                "args": ["-y", "server-memory"],
                "env": {},
                "allowed_tools": ["search_nodes"],
            },
        ]
    }


class FakeManager:
    def __init__(self) -> None:
        self.calls = []

    def health(self) -> dict:
        return {
            "servers": [
                {"name": "filesystem", "connected": True, "tools_count": 2, "error": None},
                {"name": "memory", "connected": False, "tools_count": 0, "error": "boom"},
            ],
            "total_tools": 2,
        }

    def list_tools(self) -> dict:
        return {
            "total": 1,
            "tools": [
                {
                    "server": "filesystem",
                    "name": "read_text_file",
                    "description": "read",
                    "input_schema": {"type": "object"},
                }
            ],
        }

    async def call(self, server: str, tool: str, arguments: dict) -> dict:
        self.calls.append((server, tool, arguments))
        if server == "memory":
            return {
                "ok": False,
                "server": server,
                "tool": tool,
                "result": {},
                "error": "server 未连接",
                "elapsed_ms": 1.0,
            }
        return {
            "ok": True,
            "server": server,
            "tool": tool,
            "result": {"text": "HELLO"},
            "error": None,
            "elapsed_ms": 1.0,
        }


class ConfigTests(unittest.TestCase):
    def test_load_config_default_and_validation(self) -> None:
        config = load_config(ROOT / "config.json")
        names = [item["name"] for item in config["servers"]]
        self.assertEqual(names, ["filesystem", "memory"])
        self.assertIn("read_text_file", config["servers"][0]["allowed_tools"])
        self.assertIn("search_nodes", config["servers"][1]["allowed_tools"])

    def test_load_config_rejects_duplicate_and_missing_allowlist(self) -> None:
        temp = Path(tempfile.mkdtemp())
        duplicate = temp / "dup.json"
        duplicate.write_text(
            json.dumps({"servers": _config()["servers"] + [_config()["servers"][0]]}),
            encoding="utf-8",
        )
        with self.assertRaises(BridgeError):
            load_config(duplicate)
        missing = temp / "missing.json"
        missing.write_text(
            json.dumps({"servers": [{"name": "x", "command": "npx", "args": []}]}),
            encoding="utf-8",
        )
        with self.assertRaises(BridgeError):
            load_config(missing)


class NormalizeTests(unittest.TestCase):
    def test_normalize_tool(self) -> None:
        tool = types.Tool(
            name="read_text_file",
            description="read text",
            inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        payload = normalize_tool(tool, "filesystem")
        self.assertEqual(payload["server"], "filesystem")
        self.assertEqual(payload["name"], "read_text_file")
        self.assertEqual(payload["input_schema"]["type"], "object")

    def test_normalize_result_text(self) -> None:
        result = types.CallToolResult(
            content=[types.TextContent(type="text", text="line1"), types.TextContent(type="text", text="line2")],
            isError=False,
        )
        payload = normalize_result(result)
        self.assertFalse(payload["is_error"])
        self.assertEqual(payload["text"], "line1\nline2")
        self.assertEqual(len(payload["content"]), 2)

    def test_normalize_result_error(self) -> None:
        result = types.CallToolResult(content=[types.TextContent(type="text", text="bad")], isError=True)
        payload = normalize_result(result)
        self.assertTrue(payload["is_error"])


class AllowlistTests(unittest.TestCase):
    def test_allowlist_and_unknown_server(self) -> None:
        manager = MCPBridgeManager(_config())
        with self.assertRaises(BridgeError) as caught:
            manager._check_allowed("nope", "read_text_file")
        self.assertIn("未知 MCP server", str(caught.exception))
        # 模拟已连接：白名单外工具拒绝，白名单内放行
        manager._sessions["filesystem"] = object()  # type: ignore[assignment]
        with self.assertRaises(BridgeError) as caught:
            manager._check_allowed("filesystem", "write_file")
        self.assertIn("白名单", str(caught.exception))
        server = manager._check_allowed("filesystem", "read_text_file")
        self.assertEqual(server["name"], "filesystem")

    def test_health_counts_only_connected(self) -> None:
        manager = MCPBridgeManager(_config())
        manager._sessions["filesystem"] = object()  # type: ignore[assignment]
        manager._tools["filesystem"] = [{"name": "read_text_file"}]
        health = manager.health()
        self.assertEqual(health["total_tools"], 1)
        statuses = {item["name"]: item["connected"] for item in health["servers"]}
        self.assertTrue(statuses["filesystem"])
        self.assertFalse(statuses["memory"])


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeManager()
        self.client = TestClient(create_app(manager=self.fake))

    def test_root_health_tools(self) -> None:
        self.assertEqual(self.client.get("/").status_code, 200)
        health = self.client.get("/health").json()
        self.assertEqual(health["total_tools"], 2)
        tools = self.client.get("/tools").json()
        self.assertEqual(tools["total"], 1)
        self.assertEqual(tools["tools"][0]["name"], "read_text_file")

    def test_call_success_and_mcp_error(self) -> None:
        ok = self.client.post(
            "/call",
            json={"server": "filesystem", "tool": "read_text_file", "arguments": {"path": "core-3650.txt"}},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["ok"])
        self.assertEqual(self.fake.calls[0][0], "filesystem")
        failed = self.client.post(
            "/call", json={"server": "memory", "tool": "search_nodes", "arguments": {"query": "x"}}
        ).json()
        self.assertFalse(failed["ok"])
        self.assertIn("未连接", failed["error"])

    def test_bad_requests(self) -> None:
        self.assertEqual(self.client.post("/call", content="not-json").status_code, 400)
        missing = self.client.post("/call", json={"server": "filesystem"}).json()
        self.assertFalse(missing["ok"])
        self.assertIn("缺少", missing["error"])
        bad_args = self.client.post(
            "/call", json={"server": "filesystem", "tool": "read_text_file", "arguments": []}
        ).json()
        self.assertFalse(bad_args["ok"])


if __name__ == "__main__":
    unittest.main()
