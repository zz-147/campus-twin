# -*- coding: utf-8 -*-
"""MCP Server 验收客户端：真实 stdio 启动 server.py，list/read/call 全链路断言。

用法::

    D:\\zdsh\\campus-twin\\.venv-mcp314\\Scripts\\python.exe verify_client.py
    D:\\zdsh\\campus-twin\\.venv-mcp314\\Scripts\\python.exe verify_client.py --bad-backend

退出码：0=全部 PASS，1=有失败。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MCP_SERVER_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = MCP_SERVER_DIR / "server.py"
DEFAULT_BACKEND = "http://127.0.0.1:8000"
REQUIRED_TOOLS = [
    "get_devices",
    "get_topology",
    "get_hosts",
    "get_issues",
    "get_alerts",
    "get_alert_groups",
    "get_notify_status",
    "trace_path",
    "get_scene",
    "get_audit",
]
REQUIRED_RESOURCES = [
    "campus://scene",
    "campus://topology",
    "campus://docs/network-design",
    "campus://docs/troubleshooting",
]
REQUIRED_PROMPTS = [
    "troubleshoot_port_down",
    "troubleshoot_link_down",
    "onboard_device",
]

FAILURES: List[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print("  PASS  {}".format(label))
    else:
        print("  FAIL  {}".format(label))
        FAILURES.append(label)


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for item in content:
            parts.append(_text_from_content(item))
        return "".join(parts)
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    return str(content)


async def _call_tool(session: ClientSession, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
    result = await session.call_tool(name, arguments or {})
    return _text_from_content(result.content)


async def _run_session(
    backend_base: str,
    *,
    log_suffix: str,
    call_tools: bool,
) -> None:
    log_file = MCP_SERVER_DIR / "logs" / "verify_client_{}.log".format(log_suffix)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["BACKEND_BASE"] = backend_base
    env["MCP_LOG_FILE"] = str(MCP_SERVER_DIR / "logs" / "mcp_server.log")
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        env=env,
    )
    print("\n[{}] 启动 stdio server：{}".format(log_suffix, backend_base))
    with log_file.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                server_info = init.serverInfo
                print(
                    "  serverInfo: name={} version={}".format(
                        server_info.name, server_info.version
                    )
                )
                check(server_info.name == "campus-twin", "serverInfo.name == campus-twin")
                check(server_info.version == "0.1.0", "serverInfo.version == 0.1.0")

                tools_result = await session.list_tools()
                tool_names = [tool.name for tool in tools_result.tools]
                resources_result = await session.list_resources()
                resource_uris = [str(item.uri) for item in resources_result.resources]
                prompts_result = await session.list_prompts()
                prompt_names = [prompt.name for prompt in prompts_result.prompts]

                print("\n  Tools ({})：".format(len(tool_names)))
                for tool in tools_result.tools:
                    print("    - {:<18} {}".format(tool.name, tool.description or ""))
                print("\n  Resources ({})：".format(len(resource_uris)))
                for uri in resource_uris:
                    print("    - {}".format(uri))
                print("\n  Prompts ({})：".format(len(prompt_names)))
                for prompt in prompts_result.prompts:
                    print(
                        "    - {:<24} {}".format(
                            prompt.name, prompt.description or ""
                        )
                    )

                check(len(tool_names) >= 10, "tools/list >= 10")
                check(
                    set(REQUIRED_TOOLS).issubset(set(tool_names)),
                    "tools/list 包含全部 10 个只读工具",
                )
                check(len(resource_uris) >= 4, "resources/list >= 4")
                check(
                    set(REQUIRED_RESOURCES).issubset(set(resource_uris)),
                    "resources/list 包含 4 个资源",
                )
                check(len(prompt_names) >= 3, "prompts/list >= 3")
                check(
                    set(REQUIRED_PROMPTS).issubset(set(prompt_names)),
                    "prompts/list 包含 3 个提示词",
                )

                if not call_tools:
                    # 后端不可用场景：工具返回错误文本，但 server 必须保持存活
                    print("\n  调用 get_devices（后端不可用模拟）...")
                    payload = await _call_tool(session, "get_devices")
                    print("    返回前 200 字符：{}".format(payload[:200].replace("\n", " ")))
                    check(
                        payload.startswith("错误：") or "无法连接后端服务" in payload,
                        "后端不可用时 get_devices 返回明确错误文本",
                    )
                    check("Traceback" not in payload, "错误文本不含异常栈")
                    # server 仍存活：再 list 一次
                    tools_again = await session.list_tools()
                    check(
                        len(tools_again.tools) >= 10,
                        "后端不可用后 server 仍存活（再次 list_tools 成功）",
                    )
                    return

                # 真实数据：调 get_devices / get_scene / read resource / get_prompt
                print("\n  调用 get_devices ...")
                devices_text = await _call_tool(session, "get_devices")
                check(not devices_text.startswith("错误："), "get_devices 调用成功")
                try:
                    devices = json.loads(devices_text)
                except ValueError:
                    devices = None
                check(isinstance(devices, dict), "get_devices 返回合法 JSON")
                if isinstance(devices, dict):
                    count = devices.get("count", len(devices.get("devices", [])))
                    print("    count={} source={}".format(count, devices.get("source")))
                    for device in (devices.get("devices") or [])[:5]:
                        print(
                            "    - {:<16} ip={:<15} reachability={}".format(
                                str(device.get("hostname")),
                                str(device.get("management_ip")),
                                str(device.get("reachability")),
                            )
                        )
                    check(int(count) >= 1, "get_devices.count >= 1（真实数据）")

                print("\n  调用 get_scene ...")
                scene_text = await _call_tool(session, "get_scene")
                check(not scene_text.startswith("错误："), "get_scene 调用成功")
                try:
                    scene = json.loads(scene_text)
                except ValueError:
                    scene = None
                check(isinstance(scene, dict), "get_scene 返回合法 JSON")
                if isinstance(scene, dict):
                    buildings = scene.get("buildings") or []
                    device_total = sum(
                        len(building.get("devices") or []) for building in buildings
                    )
                    print(
                        "    version={} buildings={} devices={}".format(
                            scene.get("version"), len(buildings), device_total
                        )
                    )
                    for building in buildings[:4]:
                        print(
                            "    - {:<8} {}（{} devices）".format(
                                str(building.get("id")),
                                str(building.get("name")),
                                len(building.get("devices") or []),
                            )
                        )
                    check(len(buildings) >= 1, "get_scene.buildings >= 1")

                print("\n  读取资源 campus://scene ...")
                resource_result = await session.read_resource("campus://scene")
                resource_text = _text_from_content(
                    [item.text for item in resource_result.contents]
                    if resource_result.contents
                    else ""
                )
                check(
                    resource_text.strip().startswith("{"),
                    "read_resource(campus://scene) 返回 JSON 文本",
                )
                print("    长度={} chars".format(len(resource_text)))

                print("\n  读取资源 campus://docs/network-design ...")
                doc_result = await session.read_resource(
                    "campus://docs/network-design"
                )
                doc_text = _text_from_content(
                    [item.text for item in doc_result.contents]
                    if doc_result.contents
                    else ""
                )
                check(
                    len(doc_text) > 500 and not doc_text.startswith("错误："),
                    "read_resource(docs/network-design) 读取白名单文档成功",
                )
                print("    长度={} chars".format(len(doc_text)))

                print("\n  获取提示词 troubleshoot_port_down ...")
                prompt_result = await session.get_prompt(
                    "troubleshoot_port_down",
                    {"device_name": "SW-JX-2960", "interface_name": "FastEthernet0/24"},
                )
                prompt_text = _text_from_content(
                    [
                        message.content
                        for message in prompt_result.messages
                    ]
                )
                check(
                    "get_devices" in prompt_text and "SW-JX-2960" in prompt_text,
                    "get_prompt(troubleshoot_port_down) 内容包含工具与设备参数",
                )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Campus Twin MCP Server verify client")
    parser.add_argument(
        "--bad-backend",
        action="store_true",
        help="额外验证后端不可用（127.0.0.1:9）时工具返回错误文本、server 不崩",
    )
    parser.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        help="真实后端地址（默认 {}）".format(DEFAULT_BACKEND),
    )
    args = parser.parse_args()

    await _run_session(args.backend, log_suffix="live", call_tools=True)
    if args.bad_backend:
        await _run_session(
            "http://127.0.0.1:9", log_suffix="down", call_tools=False
        )

    print("\n" + "=" * 72)
    if FAILURES:
        print("RESULT: FAIL（{} 项失败）".format(len(FAILURES)))
        for item in FAILURES:
            print("  - {}".format(item))
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
