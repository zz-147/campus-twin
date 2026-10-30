# -*- coding: utf-8 -*-
"""MCP Bridge：用 mcp SDK Client 通过 stdio 连接外部 MCP Servers，暴露本机 HTTP API。

为什么用 Starlette 而不是 FastAPI：本任务指定的 Python 3.14 venv（.venv-mcp314）在离线环境
只预装了 **Starlette 1.10 + uvicorn**（FastAPI 未安装且无法联网安装），Starlette 与 FastAPI 同属
ASGI 栈、接口行为等价；桥对上层只暴露 HTTP JSON，Agent 侧不感知实现框架。

架构：
    Agent(8100/8101, py3.9) → HTTP 127.0.0.1:8200 → MCPBridgeManager
      → mcp.ClientSession(stdio) → npx @modelcontextprotocol/server-filesystem
                                  → npx @modelcontextprotocol/server-memory

HTTP API：
    GET  /health → {"servers":[{name,connected,tools_count,error}], "total_tools"}
    GET  /tools  → {"total", "tools":[{server,name,description,input_schema}]}
    POST /call   → {"server","tool","arguments"} → {"ok","result","error","elapsed_ms"}

安全：只监听 127.0.0.1；调用白名单（filesystem 只读工具 + memory 工具）；所有调用写
logs/mcp_bridge.log；filesystem 目录白名单由 MCP server 参数保证（configs/docs）。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:  # Starlette 1.x（离线 venv 已带）
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
except ImportError:  # pragma: no cover - 兼容 FastAPI 环境
    Starlette = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]
    Route = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_LOG = ROOT / "logs" / "mcp_bridge.log"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8200

logger = logging.getLogger("mcp_bridge")


class BridgeError(RuntimeError):
    """桥层错误（配置/白名单/服务器不可用）。"""


# --------------------------------------------------------------------------- #
# 配置 / 日志
# --------------------------------------------------------------------------- #
def setup_logging(log_path: Optional[Path] = None) -> None:
    path = Path(log_path or os.environ.get("MCP_BRIDGE_LOG") or DEFAULT_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(str(path), encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    config_path = Path(path or os.environ.get("MCP_BRIDGE_CONFIG") or DEFAULT_CONFIG)
    with open(str(config_path), "r", encoding="utf-8") as handle:
        config = json.load(handle)
    servers = config.get("servers")
    if not isinstance(servers, list) or not servers:
        raise BridgeError("config.json 缺少 servers 列表：%s" % config_path)
    names = set()
    for server in servers:
        name = str(server.get("name") or "").strip()
        if not name:
            raise BridgeError("server 缺少 name：%s" % server)
        if name in names:
            raise BridgeError("server name 重复：%s" % name)
        names.add(name)
        if not server.get("command"):
            raise BridgeError("server %s 缺少 command" % name)
        allowed = server.get("allowed_tools") or []
        server["allowed_tools"] = [str(item) for item in allowed]
        if not server["allowed_tools"]:
            raise BridgeError("server %s 缺少 allowed_tools 白名单" % name)
    config["servers"] = servers
    return config


# --------------------------------------------------------------------------- #
# MCP 结果归一化
# --------------------------------------------------------------------------- #
def normalize_tool(tool: Any, server: str) -> Dict[str, Any]:
    return {
        "server": server,
        "name": str(getattr(tool, "name", "") or ""),
        "description": str(getattr(tool, "description", "") or ""),
        "input_schema": getattr(tool, "inputSchema", None)
        or getattr(tool, "input_schema", None)
        or {},
    }


def normalize_result(result: Any) -> Dict[str, Any]:
    """把 MCP CallToolResult 归一化成 JSON 友好结构（content/text/structured/is_error）。"""
    content: List[Dict[str, Any]] = []
    for item in getattr(result, "content", None) or []:
        if hasattr(item, "model_dump"):
            try:
                data = item.model_dump(exclude_none=True)
            except Exception:  # noqa: BLE001 - 非 pydantic 内容兜底
                data = {"type": getattr(item, "type", "unknown")}
        else:
            data = {"type": type(item).__name__}
        if not isinstance(data, dict):
            data = {"type": "unknown", "data": str(data)}
        if data.get("type") == "text":
            content.append({"type": "text", "text": str(data.get("text") or "")})
        else:
            content.append(data)
    text = "\n".join(
        str(item.get("text") or "") for item in content if item.get("type") == "text"
    )
    payload: Dict[str, Any] = {
        "content": content,
        "text": text,
        "is_error": bool(getattr(result, "isError", False)),
    }
    structured = getattr(result, "structuredContent", None)
    if structured:
        payload["structured"] = structured
    return payload


# --------------------------------------------------------------------------- #
# 桥管理器：持有 stdio MCP sessions
# --------------------------------------------------------------------------- #
class MCPBridgeManager:
    """连接外部 MCP servers 并提供 list/call/health（异步）。"""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._config = config
        self._servers: Dict[str, Dict[str, Any]] = {
            str(server["name"]): server for server in config.get("servers") or []
        }
        self._sessions: Dict[str, ClientSession] = {}
        self._tools: Dict[str, List[Dict[str, Any]]] = {}
        self._errors: Dict[str, Optional[str]] = {}
        self._stack: Optional[AsyncExitStack] = None
        self._lock = asyncio.Lock()
        self._started = False

    # ---- 生命周期 ----
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stack = AsyncExitStack()
        for name, server in self._servers.items():
            started = time.perf_counter()
            try:
                env = dict(os.environ)
                env.update(
                    {str(key): str(value) for key, value in (server.get("env") or {}).items()}
                )
                params = StdioServerParameters(
                    command=str(server["command"]),
                    args=[str(item) for item in (server.get("args") or [])],
                    env=env,
                )
                stack = self._stack
                assert stack is not None
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                tools_result = await session.list_tools()
                tools = [
                    normalize_tool(tool, name) for tool in (getattr(tools_result, "tools", None) or [])
                ]
                self._sessions[name] = session
                self._tools[name] = tools
                self._errors[name] = None
                logger.info(
                    "connected server=%s tools=%d elapsed_ms=%.1f",
                    name,
                    len(tools),
                    (time.perf_counter() - started) * 1000.0,
                )
            except Exception as exc:  # noqa: BLE001 - 单 server 失败不影响其它
                self._sessions.pop(name, None)
                self._tools[name] = []
                self._errors[name] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
                logger.error("connect server=%s failed: %s", name, self._errors[name])

    async def stop(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as exc:  # noqa: BLE001 - 退出清理失败不影响进程
                logger.warning("stop bridge error: %s: %s", type(exc).__name__, exc)
            self._stack = None
        self._sessions.clear()
        self._tools.clear()
        self._started = False

    # ---- 查询 ----
    def health(self) -> Dict[str, Any]:
        servers = []
        total = 0
        for name in self._servers:
            connected = name in self._sessions
            tools_count = len(self._tools.get(name) or [])
            if connected:
                total += tools_count
            servers.append(
                {
                    "name": name,
                    "connected": connected,
                    "tools_count": tools_count,
                    "error": self._errors.get(name),
                }
            )
        return {"servers": servers, "total_tools": total}

    def list_tools(self) -> Dict[str, Any]:
        tools: List[Dict[str, Any]] = []
        for name in self._servers:
            tools.extend(self._tools.get(name) or [])
        return {"total": len(tools), "tools": tools}

    # ---- 白名单 / 调用 ----
    def _check_allowed(self, server: str, tool: str) -> Dict[str, Any]:
        if server not in self._servers:
            raise BridgeError("未知 MCP server：%s（可用：%s）" % (server, ", ".join(self._servers)))
        if server not in self._sessions:
            error = self._errors.get(server) or "未连接"
            raise BridgeError("MCP server %s 未连接：%s" % (server, error))
        allowed = self._servers[server].get("allowed_tools") or []
        if tool not in allowed:
            raise BridgeError(
                "工具 %s/%s 不在桥白名单内（允许：%s）"
                % (server, tool, ", ".join(allowed))
            )
        return self._servers[server]

    async def call(
        self, server: str, tool: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            self._check_allowed(server, tool)
            session = self._sessions[server]
            async with self._lock:
                result = await session.call_tool(tool, arguments or {})
            payload = normalize_result(result)
            ok = not bool(payload.get("is_error"))
            error = None if ok else (payload.get("text") or "MCP 工具返回错误")[:500]
        except BridgeError as exc:
            payload = {}
            ok = False
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 调用失败归一化返回
            payload = {}
            ok = False
            error = "%s: %s" % (type(exc).__name__, str(exc)[:500])
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        logger.info(
            "call server=%s tool=%s ok=%s elapsed_ms=%.2f error=%s",
            server,
            tool,
            ok,
            elapsed_ms,
            (error or "")[:200],
        )
        return {
            "ok": bool(ok),
            "server": server,
            "tool": tool,
            "result": payload,
            "error": error,
            "elapsed_ms": elapsed_ms,
        }


# --------------------------------------------------------------------------- #
# ASGI app
# --------------------------------------------------------------------------- #
def create_app(
    config_path: Optional[Path] = None,
    manager: Optional[MCPBridgeManager] = None,
) -> Any:
    manage_lifecycle = manager is None
    bridge_manager = manager or MCPBridgeManager(load_config(config_path))

    @contextlib.asynccontextmanager
    async def lifespan(app: Any):
        if manage_lifecycle:
            await bridge_manager.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                await bridge_manager.stop()

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(bridge_manager.health())

    async def tools(request: Request) -> JSONResponse:
        return JSONResponse(bridge_manager.list_tools())

    async def call(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 非法 JSON
            return JSONResponse(
                {"ok": False, "result": None, "error": "请求体不是合法 JSON", "elapsed_ms": 0.0},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "result": None, "error": "请求体必须是 JSON 对象", "elapsed_ms": 0.0},
                status_code=400,
            )
        server = str(body.get("server") or "").strip()
        tool = str(body.get("tool") or "").strip()
        arguments = body.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not server or not tool:
            return JSONResponse(
                {"ok": False, "result": None, "error": "缺少 server 或 tool", "elapsed_ms": 0.0},
                status_code=400,
            )
        if not isinstance(arguments, dict):
            return JSONResponse(
                {"ok": False, "result": None, "error": "arguments 必须是 JSON 对象", "elapsed_ms": 0.0},
                status_code=400,
            )
        payload = await bridge_manager.call(server, tool, arguments)
        return JSONResponse(payload)

    async def root(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "name": "campus-twin MCP Bridge",
                "version": "0.1.0",
                "transport": "stdio -> MCP servers",
                "endpoints": ["/health", "/tools", "/call (POST)"],
            }
        )

    if Starlette is None:  # pragma: no cover - 理论上不会发生
        raise RuntimeError("未安装 Starlette，无法启动 MCP Bridge")

    routes = [
        Route("/", root, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/tools", tools, methods=["GET"]),
        Route("/call", call, methods=["POST"]),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="campus-twin MCP Bridge (stdio -> HTTP)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--config", default=None)
    parser.add_argument("--log", default=None)
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("安全限制：MCP Bridge 只允许监听 127.0.0.1/localhost", file=sys.stderr)
        return 2
    setup_logging(Path(args.log) if args.log else None)
    import uvicorn

    app = create_app(Path(args.config) if args.config else None)
    logger.info("MCP Bridge listening on http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
