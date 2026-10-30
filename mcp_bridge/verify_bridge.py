# -*- coding: utf-8 -*-
"""MCP Bridge 端到端验证：真实启动 bridge → 连接 npx MCP servers → 列出工具 →
filesystem 读 configs/core-3650.txt → memory 写入/检索一条 → 全部断言 PASS。

用法（必须用 Python 3.14 venv）：
    D:\\zdsh\\campus-twin\\.venv-mcp314\\Scripts\\python.exe verify_bridge.py
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

import httpx

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
CONFIG = ROOT / "config.json"
CORE_FILE = PROJECT / "configs" / "core-3650.txt"


def _free_port(preferred: int = 8200) -> int:
    for port in (preferred, 8210, 8211, 8212):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("找不到可用端口（8200/8210/8211/8212）")


def _wait_health(base: str, timeout_s: float = 240.0) -> Dict[str, Any]:
    deadline = time.time() + timeout_s
    last_error: Any = None
    while time.time() < deadline:
        try:
            response = httpx.get(base + "/health", timeout=2.0)
            if response.status_code == 200:
                return response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(1.0)
    raise SystemExit("bridge 启动超时（%.0fs）：%s" % (timeout_s, last_error))


def _call(base: str, server: str, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    response = httpx.post(
        base + "/call",
        json={"server": server, "tool": tool, "arguments": arguments},
        timeout=120.0,
    )
    response.raise_for_status()
    return response.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP Bridge verify")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--keep-running", action="store_true", help="验证完不杀 bridge（调试用）")
    args = parser.parse_args(argv)
    port = args.port or _free_port()
    base = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "bridge.py"), "--port", str(port), "--config", str(CONFIG)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print("bridge pid=%d port=%d, waiting for MCP servers ..." % (process.pid, port))
    try:
        health = _wait_health(base)
        print("PASS /health:", json.dumps(health, ensure_ascii=False))
        assert health.get("servers"), "health 无 servers"
        assert health.get("total_tools", 0) > 0, "total_tools=0（MCP servers 未连上）"
        for server in health["servers"]:
            assert server["connected"], "server 未连接：%s" % server
            assert server["tools_count"] > 0, "server 无工具：%s" % server

        tools = httpx.get(base + "/tools", timeout=30.0).json()
        assert tools.get("total", 0) > 0
        names = {(item["server"], item["name"]) for item in tools["tools"]}
        assert ("filesystem", "read_text_file") in names
        assert ("memory", "search_nodes") in names
        print("PASS /tools: total=%d，示例=%s" % (tools["total"], sorted(names)[:5]))

        text = CORE_FILE.read_text(encoding="utf-8")
        read = _call(base, "filesystem", "read_text_file", {"path": str(CORE_FILE)})
        assert read.get("ok"), "filesystem 读取失败：%s" % read.get("error")
        content = (read.get("result") or {}).get("text") or ""
        assert content.strip(), "filesystem 读取内容为空"
        assert "Core-3650" in content and ("VLAN" in content or "vlan" in content)
        assert content[:200] == text[:200]
        print("PASS filesystem.read_text_file: %s 前 6 行：" % CORE_FILE.name)
        print("\n".join(content.splitlines()[:6]))

        created = _call(
            base,
            "memory",
            "create_entities",
            {
                "entities": [
                    {
                        "name": "w13-bridge-verify",
                        "entityType": "verify",
                        "observations": ["MCP bridge verify: filesystem + memory OK"],
                    }
                ]
            },
        )
        assert created.get("ok"), "memory create_entities 失败：%s" % created.get("error")
        found = _call(base, "memory", "search_nodes", {"query": "w13-bridge-verify"})
        assert found.get("ok"), "memory search_nodes 失败：%s" % found.get("error")
        found_text = (found.get("result") or {}).get("text") or ""
        assert "w13-bridge-verify" in found_text, found_text[:300]
        print("PASS memory.create_entities + search_nodes")

        denied = _call(base, "filesystem", "write_file", {"path": "configs/x.txt", "content": "x"})
        assert denied.get("ok") is False and "白名单" in (denied.get("error") or ""), denied
        print("PASS 白名单拒绝 write_file：", denied.get("error"))

        bad = _call(base, "unknown", "x", {})
        assert bad.get("ok") is False and "未知" in (bad.get("error") or ""), bad
        print("PASS 未知 server 错误处理")

        print("ALL_BRIDGE_VERIFY_PASS")
        return 0
    finally:
        if args.keep_running:
            print("bridge 保持运行 pid=%d（--keep-running）" % process.pid)
        else:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
