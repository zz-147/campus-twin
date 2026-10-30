# -*- coding: utf-8 -*-
"""W13 MCP Bridge HTTP 客户端（Agent 侧，Python 3.9，不依赖 mcp SDK）。

Agent（py3.9）不直接跑 MCP SDK，而是通过本机 HTTP 桥（py3.14 + mcp SDK）访问外部
MCP servers（filesystem/memory）。桥不可用时抛 ``MCPBridgeError``，工具层转成
``ok=false`` 中文提示，Agent 不崩、不编造。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import httpx


class MCPBridgeError(RuntimeError):
    """MCP Bridge 不可达 / 返回错误 / 工具白名单拒绝。"""


class MCPBridgeClient:
    """本机 MCP Bridge HTTP 客户端（只读工具调用；不直接启动 MCP servers）。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_s: float = 5.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("MCP_BRIDGE_BASE") or "http://127.0.0.1:8200"
        ).rstrip("/")
        self.timeout_s = max(1.0, float(timeout_s or 5.0))
        self._transport = transport
        self._client: Optional[httpx.Client] = None

    # ------------------------------------------------------------------ #
    # HTTP 基础
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_s,
                transport=self._transport,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MCPBridgeError(
                "MCP Bridge 未启动或不可达（%s）：%s: %s。请先启动 "
                "campus-twin/mcp_bridge/bridge.py（127.0.0.1:8200）"
                % (self.base_url, type(exc).__name__, exc)
            ) from None
        if response.status_code >= 400:
            raise MCPBridgeError(
                "MCP Bridge HTTP %d：%s" % (response.status_code, response.text[:300])
            )
        try:
            payload = response.json()
        except ValueError:
            raise MCPBridgeError("MCP Bridge 返回非 JSON：%s" % response.text[:300]) from None
        if not isinstance(payload, dict):
            raise MCPBridgeError("MCP Bridge 返回格式异常（期望 JSON 对象）")
        return payload

    # ------------------------------------------------------------------ #
    # 协议方法
    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/health")

    def list_tools(self) -> Dict[str, Any]:
        return self._request("GET", "/tools")

    def call(
        self,
        server: str,
        tool: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = self._request(
            "POST",
            "/call",
            json={"server": server, "tool": tool, "arguments": arguments or {}},
        )
        if not payload.get("ok"):
            raise MCPBridgeError(
                "MCP 工具 %s/%s 调用失败：%s"
                % (server, tool, payload.get("error") or "未知错误")
            )
        return payload


def probe_mcp_bridge(client: MCPBridgeClient, timeout_s: float = 1.5) -> Dict[str, Any]:
    """健康探测（/api/health 用）：桥未启动时返回 available=false，不抛异常。"""
    started = time.perf_counter()
    try:
        health = client.health()
        servers = health.get("servers") or []
        return {
            "available": True,
            "base_url": client.base_url,
            "total_tools": health.get("total_tools"),
            "servers": [
                {
                    "name": item.get("name"),
                    "connected": bool(item.get("connected")),
                    "tools_count": item.get("tools_count"),
                    "error": item.get("error"),
                }
                for item in servers
            ],
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }
    except Exception as exc:  # noqa: BLE001 - 健康探测失败不影响主服务
        return {
            "available": False,
            "base_url": client.base_url,
            "error": "%s: %s" % (type(exc).__name__, str(exc)[:300]),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }


def tool_names() -> List[str]:
    """Agent 侧接入的 MCP 只读工具名（供 roles/README 展示）。"""
    return [
        "mcp_list_tools",
        "mcp_list_dir",
        "mcp_read_file",
        "mcp_search_files",
        "mcp_memory_search",
        "mcp_memory_add",
    ]
