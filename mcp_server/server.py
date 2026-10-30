# -*- coding: utf-8 -*-
"""Campus Twin MCP Server（stdio）。

启动（Claude Desktop / Cursor / 任意 MCP 客户端均可接入）::

    D:\\zdsh\\campus-twin\\.venv-mcp314\\Scripts\\python.exe D:\\zdsh\\campus-twin\\mcp_server\\server.py

环境变量：
- ``BACKEND_BASE``：后端地址，默认 ``http://127.0.0.1:8000``
- ``MCP_DOCS_DIR``：docs 目录，默认 ``campus-twin/docs``
- ``MCP_LOG_FILE``：日志文件，默认 ``mcp_server/logs/mcp_server.log``

安全边界：**只读**。10 个工具均只调用后端 GET（trace_path 的 POST 也是只读诊断，
后端不写库）；不暴露任何写接口；resources 只能读 docs/ 白名单内两个固定文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 以脚本方式启动时确保能 import 同目录的 core.py
_SERVER_DIR = Path(__file__).resolve().parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import core  # noqa: E402

# stdout 是 MCP stdio 协议通道，日志只能进文件/stderr
core.configure_logging()

mcp = FastMCP(name=core.SERVER_NAME, instructions=core.server_instructions())
# mcp 1.30 的 FastMCP 构造器尚未暴露 version 参数，直接设置低层 Server 的版本，
# 客户端 initialize 时 serverInfo.version 即为 0.1.0（已在 verify_client.py 断言）。
mcp._mcp_server.version = core.SERVER_VERSION  # noqa: SLF001


# --------------------------------------------------------------------------- #
# Tools（10 个，全部只读）
# --------------------------------------------------------------------------- #
@mcp.tool(
    name="get_devices",
    description=core.TOOL_DESCRIPTIONS["get_devices"],
)
def get_devices() -> str:
    """获取设备清单（只读）。"""
    return core.run_tool("get_devices")


@mcp.tool(
    name="get_topology",
    description=core.TOOL_DESCRIPTIONS["get_topology"],
)
def get_topology() -> str:
    """获取拓扑节点与链路（只读）。"""
    return core.run_tool("get_topology")


@mcp.tool(
    name="get_hosts",
    description=core.TOOL_DESCRIPTIONS["get_hosts"],
)
def get_hosts() -> str:
    """获取主机/终端清单（只读）。"""
    return core.run_tool("get_hosts")


@mcp.tool(
    name="get_issues",
    description=core.TOOL_DESCRIPTIONS["get_issues"],
)
def get_issues() -> str:
    """获取活跃健康问题（只读）。"""
    return core.run_tool("get_issues")


@mcp.tool(
    name="get_alerts",
    description=core.TOOL_DESCRIPTIONS["get_alerts"],
)
def get_alerts(state: str = "active") -> str:
    """获取告警列表；state=active|cleared|info|all。"""
    return core.run_tool("get_alerts", state)


@mcp.tool(
    name="get_alert_groups",
    description=core.TOOL_DESCRIPTIONS["get_alert_groups"],
)
def get_alert_groups() -> str:
    """获取告警根因分组（只读）。"""
    return core.run_tool("get_alert_groups")


@mcp.tool(
    name="get_notify_status",
    description=core.TOOL_DESCRIPTIONS["get_notify_status"],
)
def get_notify_status() -> str:
    """获取 Webhook 通知状态（只读）。"""
    return core.run_tool("get_notify_status")


@mcp.tool(
    name="trace_path",
    description=core.TOOL_DESCRIPTIONS["trace_path"],
)
def trace_path(source_ip: str, dest_ip: str) -> str:
    """追踪源 IP 到目的 IP 的转发路径（只读诊断）。"""
    return core.run_tool("trace_path", source_ip, dest_ip)


@mcp.tool(
    name="get_scene",
    description=core.TOOL_DESCRIPTIONS["get_scene"],
)
def get_scene() -> str:
    """获取 3D 场景配置（只读）。"""
    return core.run_tool("get_scene")


@mcp.tool(
    name="get_audit",
    description=core.TOOL_DESCRIPTIONS["get_audit"],
)
def get_audit(limit: int = 20) -> str:
    """获取最近操作审计（只读，limit 1-100）。"""
    return core.run_tool("get_audit", limit)


# --------------------------------------------------------------------------- #
# Resources（4 个只读资源，其中 docs 为固定白名单）
# --------------------------------------------------------------------------- #
@mcp.resource(
    "campus://scene",
    name="scene",
    description="3D 场景配置（楼栋/设备/坐标/状态），来自 GET /api/scene",
    mime_type="application/json",
)
def scene_resource() -> str:
    return core.read_scene_resource()


@mcp.resource(
    "campus://topology",
    name="topology",
    description="拓扑节点与链路，来自 GET /api/topology",
    mime_type="application/json",
)
def topology_resource() -> str:
    return core.read_topology_resource()


@mcp.resource(
    "campus://docs/network-design",
    name="network-design",
    description="校园网络拓扑设计文档（docs/校园网络拓扑设计.md，只读白名单）",
    mime_type="text/markdown",
)
def network_design_resource() -> str:
    return core.resource_network_design()


@mcp.resource(
    "campus://docs/troubleshooting",
    name="troubleshooting",
    description="故障注入实验记录（docs/故障注入实验记录.md，只读白名单）",
    mime_type="text/markdown",
)
def troubleshooting_resource() -> str:
    return core.resource_troubleshooting()


# --------------------------------------------------------------------------- #
# Prompts（3 个标准排障/接入 SOP）
# --------------------------------------------------------------------------- #
@mcp.prompt(
    name="troubleshoot_port_down",
    description="生成端口 down 的标准化排障提示词（只读诊断）",
)
def troubleshoot_port_down(
    device_name: str, interface_name: str = "UNKNOWN"
) -> str:
    """端口 down 排障 SOP。"""
    return core.prompt_troubleshoot_port_down(device_name, interface_name)


@mcp.prompt(
    name="troubleshoot_link_down",
    description="生成链路 down 的标准化排障提示词（只读诊断）",
)
def troubleshoot_link_down(source_device: str, target_device: str) -> str:
    """链路 down 排障 SOP。"""
    return core.prompt_troubleshoot_link_down(source_device, target_device)


@mcp.prompt(
    name="onboard_device",
    description="生成新设备接入引导提示词（只读检查清单，写操作需人工执行）",
)
def onboard_device(
    device_name: str, building: str = "unknown", model: str = "unknown"
) -> str:
    """新设备接入引导 SOP。"""
    return core.prompt_onboard_device(device_name, building, model)


if __name__ == "__main__":
    # stdio transport：绝不能向 stdout 打印任何内容
    mcp.run(transport="stdio")
