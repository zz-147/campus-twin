# -*- coding: utf-8 -*-
"""Agent 只读工具集（Function Calling）。

23 个工具全部通过 campus-twin 后端 REST API（或内置知识库/MCP Bridge）获取数据，不直接访问 PT/NC：

- ``get_health``        服务 + 网络健康总览
- ``get_devices``       网络设备清单
- ``get_topology``      拓扑节点/链路（含 down 链路）
- ``get_hosts``         终端主机清单（可过滤）
- ``get_issues``        NC 健康问题
- ``get_alerts``        后端告警（可过滤）
- ``get_device_detail`` 单设备详情（关联主机/告警/指标摘要）
- ``get_metrics``       设备指标时序（峰值/最新值）
- ``get_scene``         W9：3D 场景配置（楼栋/设备位置）
- ``get_audit``         W9：操作审计
- ``get_new_devices``   W9：待加入场景的新设备（pending / inventory diff）
- ``infer_building``    W9：新设备归属推断（五级规则）
- ``get_device_config_template`` W9：设备接入 CLI/连线模板（内置知识库）
- ``trace_path``        W10：主机间路径追踪（hop 链/断点定位）
- ``get_alert_groups``  W10：告警根因分组 + 抑制统计
- ``get_notify_status`` W10：Webhook 通知状态
- ``search_knowledge``  W12：内置知识库检索（BM25-lite，带 [K1] 引用）
- ``mcp_list_tools`` / ``mcp_list_dir`` / ``mcp_read_file`` / ``mcp_search_files`` /
  ``mcp_memory_search`` / ``mcp_memory_add`` W13：外部 MCP filesystem/memory 工具（经本机 Bridge）

所有工具均为**只读**；写操作（清告警/加场景设备等）只能由 Agent 生成 action，
经用户确认后由 ``app.agent.actions.ActionExecutor`` 执行。

容错原则：任何工具失败都返回 ``{"ok": false, "error": ...}`` 而不是抛异常，
编排循环会把错误回填给 LLM，让模型基于可用数据给出诚实回答（不编造）。
"""
from __future__ import annotations

import copy
import json
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.agent.mcp_client import MCPBridgeClient, MCPBridgeError
from app.agent.rag.retriever import search as search_knowledge

from app.agent.backend import BackendClient, BackendError
from app.agent.inference import infer_building, inventory_new_devices, scene_buildings
from app.agent.knowledge import (
    DEVICE_SPECS,
    building_profile,
    detect_building_name,
    detect_device_kind,
    format_cabling_markdown,
    next_management_ip,
    next_hostname,
    render_cli,
    render_device_access_steps,
    render_undo,
    render_verification,
    scene_type_label,
)

#: 单次工具结果的最大 JSON 字符数（防止把海量数据灌进 LLM 上下文）
MAX_TOOL_RESULT_CHARS = 20000

#: metrics 的已知指标名（仅用于描述，不做强校验）
METRIC_NAMES = ("cpu_pct", "mem_pct", "if_utilization_pct", "if_in_bps", "if_out_bps")


class ToolArgumentError(ValueError):
    """工具参数错误（会以 ok=false 回填给 LLM）。"""


def build_tool_specs() -> List[Dict[str, Any]]:
    """返回 8 个工具的 OpenAI function-calling schema。"""
    empty_object = {"type": "object", "properties": {}, "additionalProperties": False}
    return [
        {
            "type": "function",
            "function": {
                "name": "get_health",
                "description": (
                    "查询 campus-twin 后端服务健康状态 + 校园网健康总览"
                    "（设备健康数/百分比、客户端健康度、采集是否 stale）。"
                    "任何排障都建议先调用它了解全局。"
                ),
                "parameters": empty_object,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_devices",
                "description": (
                    "查询校园网受管网络设备清单（交换机/路由器），"
                    "包含 hostname、management_ip、reachability、collectionStatus、平台型号等真实字段。"
                ),
                "parameters": empty_object,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_topology",
                "description": (
                    "查询物理拓扑节点与链路，链路包含两端设备/端口名与 link_status，"
                    "用于定位链路中断、拓扑断链。"
                ),
                "parameters": empty_object,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_hosts",
                "description": (
                    "查询终端主机清单（PC/服务器），包含 ip、ping_status、接入网络设备与端口、VLAN。"
                    "定位终端掉线/离线问题。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "可选：按主机名/IP/接入设备名关键字过滤，如 '教学楼'、'PC-JX'、'SW-JX-2960'。",
                        },
                        "ping_status": {
                            "type": "string",
                            "description": "可选：按 ping 状态过滤，如 SUCCESS 或 FAILURE。",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_issues",
                "description": (
                    "查询 Network Controller 上报的活跃健康问题（Connectivity/Device is down 等），"
                    "包含问题名称、描述、来源、时间。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "最多返回条数，默认 50。",
                            "minimum": 1,
                            "maximum": 200,
                        }
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_alerts",
                "description": (
                    "查询后端检测引擎产生的告警（device_offline / link_down / host_down / "
                    "health_issue 等），包含 severity、title、detail、device_id、时间。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "最多返回条数，默认 30。",
                            "minimum": 1,
                            "maximum": 200,
                        },
                        "severity": {
                            "type": "string",
                            "description": "可选：按级别过滤 critical / warning / info。",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_device_detail",
                "description": (
                    "查询单台网络设备详情：设备状态、该设备接入的主机、相关告警、指标摘要。"
                    "参数可传设备名（如 SW-JX-2960）或设备 id。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id_or_name": {
                            "type": "string",
                            "description": "设备 hostname 或 id，例如 'SW-JX-2960'。",
                        }
                    },
                    "required": ["device_id_or_name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_metrics",
                "description": (
                    "查询设备指标时序（cpu_pct/mem_pct/if_utilization_pct/if_in_bps/if_out_bps），"
                    "返回最新值与峰值，用于判定 CPU 高、带宽拥塞等性能问题。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id_or_name": {
                            "type": "string",
                            "description": "设备 hostname 或 id，例如 'Core-3650'。",
                        },
                        "metric": {
                            "type": "string",
                            "description": "可选：只查单个指标，如 cpu_pct。",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回最近多少个指标点，默认 30。",
                            "minimum": 1,
                            "maximum": 500,
                        },
                    },
                    "required": ["device_id_or_name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_scene",
                "description": (
                    "W9：查询 3D 场景配置（GET /api/scene），返回楼栋（名称/位置/尺寸/配色）"
                    "与每栋楼里的设备位置/状态。用于判断新设备是否已在场景中、计算摆放位置。"
                ),
                "parameters": empty_object,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_audit",
                "description": (
                    "W9：查询操作审计（GET /api/audit），返回 who/action/target/time/detail，"
                    "用于回答“刚才谁删了告警/改过哪些配置”。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "最多返回条数，默认 20。",
                            "minimum": 1,
                            "maximum": 200,
                        }
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_new_devices",
                "description": (
                    "W9：查询发现但尚未确认的新设备：优先读 /api/scene/pending，"
                    "并与 inventory（/api/devices）做差集兜底；返回设备名/管理 IP/推断楼栋。"
                ),
                "parameters": empty_object,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "infer_building",
                "description": (
                    "W9：对一台新设备执行归属推断（五级规则：拓扑邻居→hostname 关键词→"
                    "管理 IP/VLAN 段→询问用户→新建楼栋），返回楼栋、规则、置信度。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id_or_name": {
                            "type": "string",
                            "description": "新设备 hostname 或 id，例如 'SW-XS-2960'。",
                        }
                    },
                    "required": ["device_id_or_name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_device_config_template",
                "description": (
                    "W9：按型号生成设备接入模板：PT 型号、端口级连线表、完整 Cisco IOS CLI、"
                    "验证命令与撤销命令（内置 2960/3650/4331/PC/Server 模板，不写配置）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_kind": {
                            "type": "string",
                            "description": "型号：2960 / 3650 / 4331 / PC / Server 或中文（交换机/路由器/服务器）。",
                        },
                        "building": {
                            "type": "string",
                            "description": "目标楼栋，如 宿舍楼 / 教学楼 / 中心机房。",
                        },
                        "hostname": {
                            "type": "string",
                            "description": "可选：新设备 hostname；不传则按楼栋命名规则生成。",
                        },
                        "vlan": {
                            "type": "integer",
                            "description": "可选：VLAN ID；不传则按楼栋规划取默认 VLAN。",
                        },
                        "mgmt_ip": {
                            "type": "string",
                            "description": "可选：管理 IP；不传则给出待分配的示例地址。",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "trace_path",
                "description": (
                    "W10：追踪两台主机之间的网络路径（POST /api/path-trace），返回 hop 链"
                    "（设备名/进出端口/角色）、链路与断点状态。用于回答"
                    "“PC 到服务器路径怎么走 / 为什么访问不通 / 哪里断了”。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_ip": {
                            "type": "string",
                            "description": "源主机 IP，例如 192.168.20.10（先用 get_hosts 把主机名反查成 IP）。",
                        },
                        "dest_ip": {
                            "type": "string",
                            "description": "目的主机 IP，例如 192.168.50.10。",
                        },
                    },
                    "required": ["source_ip", "dest_ip"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_alert_groups",
                "description": (
                    "W10：查询告警根因分组（GET /api/alerts/groups?state=active）。返回按根因"
                    "聚合的组（link_down/device_unreachable 等）、根因设备、成员数与被上游抑制的"
                    "告警数。用于回答“有哪些告警/根因是什么/多少被抑制”。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "description": "可选：active（默认）/ cleared / all。",
                        }
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_notify_status",
                "description": (
                    "W10：查询告警通知（Webhook）状态（GET /api/notify/status）：是否启用、"
                    "通知类型、最低告警级别、最近发送时间/错误、累计发送数。只读；"
                    "发送测试通知属于写操作，本期不生成 action。"
                ),
                "parameters": empty_object,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": (
                    "W12：检索本项目内置知识库（设备配置模板/IP-VLAN 拓扑规划/故障 runbook/"
                    "项目约束），仅返回知识库原文，不联网、不编造。用于回答配置方法、规划参数、"
                    "故障处置步骤、项目约束等问题；命中会带 [K1].. 引用编号。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "检索问题/关键词，例如“2960 上联核心 trunk 配置”。",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "可选：返回条数（1-10，默认 5）。",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mcp_list_tools",
                "description": (
                    "W13：列出外部 MCP servers（filesystem/memory）接入的工具（经本机 MCP Bridge）。"
                    "只读；用于回答“MCP 现在接入了哪些工具/能力”。"
                ),
                "parameters": empty_object,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mcp_list_dir",
                "description": (
                    "W13：通过 MCP filesystem server 列出 configs/docs 目录内容（只读白名单由 Bridge 保证）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "目录路径（只允许 campus-twin/configs 或 campus-twin/docs 下），例如 D:\\zdsh\\campus-twin\\configs。",
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mcp_read_file",
                "description": (
                    "W13：通过 MCP filesystem server 读取 configs/docs 下的文本文件（只读，文件大小受 Bridge/工具限制）。"
                    "用于回答“读一下 configs/core-3650.txt 的 VLAN 配置”。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "文件路径，例如 D:\\zdsh\\campus-twin\\configs\\core-3650.txt。",
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mcp_search_files",
                "description": "W13：通过 MCP filesystem server 在 configs/docs 下按文件名模式搜索文件（只读）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "搜索起始目录（configs/docs 下）。"},
                        "pattern": {"type": "string", "description": "文件名模式，例如 core-*.txt / *.md。"},
                    },
                    "required": ["path", "pattern"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mcp_memory_search",
                "description": "W13：通过 MCP memory server 检索本地知识图谱记忆（只读查询）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索关键词/实体名。"}
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mcp_memory_add",
                "description": (
                    "W13：向 MCP memory server 的本地知识图谱写入一条笔记。注意：这是本地记忆写入，"
                    "不是网络设备写操作；不需要 action 确认，但会在答案中标注。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "要写入本地记忆的文本。"}
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
            },
        },
    ]


#: 全部工具名（诊断 Agent 可用全集）
ALL_TOOL_NAMES: Tuple[str, ...] = tuple(
    spec["function"]["name"] for spec in build_tool_specs()
)

#: 排障诊断需要的核心工具白名单（8 后端只读 + 5 个只读 MCP 工具）
DIAGNOSIS_TOOL_NAMES: Tuple[str, ...] = (
    "get_health",
    "get_devices",
    "get_topology",
    "get_hosts",
    "get_issues",
    "get_alerts",
    "get_device_detail",
    "get_metrics",
    "mcp_list_tools",
    "mcp_list_dir",
    "mcp_read_file",
    "mcp_search_files",
    "mcp_memory_search",
)


class OpsToolkit:
    """只读工具集：绑定一个 BackendClient，暴露 schemas + call。

    ``allowed`` 用于子 Agent 工具白名单隔离（如 onboarding 只拿只读工具、
    scene Agent 只拿 get_scene，写操作永远不在工具里）。
    """

    def __init__(
        self,
        backend: BackendClient,
        allowed: Optional[Iterable[str]] = None,
        mcp_bridge: Optional[MCPBridgeClient] = None,
    ) -> None:
        self._backend = backend
        self._mcp_bridge: MCPBridgeClient = mcp_bridge or MCPBridgeClient()
        self._allowed: Optional[List[str]] = None
        if allowed is not None:
            requested = [str(name) for name in allowed]
            unknown = [name for name in requested if name not in ALL_TOOL_NAMES]
            if unknown:
                raise ValueError("工具白名单包含未知工具：%s" % ", ".join(unknown))
            self._allowed = list(dict.fromkeys(requested))
        specs = build_tool_specs()
        self._specs: Dict[str, Dict[str, Any]] = {
            spec["function"]["name"]: spec for spec in specs
        }
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Tuple[Dict[str, Any], str]]] = {
            "get_health": self._get_health,
            "get_devices": self._get_devices,
            "get_topology": self._get_topology,
            "get_hosts": self._get_hosts,
            "get_issues": self._get_issues,
            "get_alerts": self._get_alerts,
            "get_device_detail": self._get_device_detail,
            "get_metrics": self._get_metrics,
            "get_scene": self._get_scene,
            "get_audit": self._get_audit,
            "get_new_devices": self._get_new_devices,
            "infer_building": self._infer_building,
            "get_device_config_template": self._get_device_config_template,
            "trace_path": self._trace_path,
            "get_alert_groups": self._get_alert_groups,
            "get_notify_status": self._get_notify_status,
            "search_knowledge": self._search_knowledge,
            "mcp_list_tools": self._mcp_list_tools,
            "mcp_list_dir": self._mcp_list_dir,
            "mcp_read_file": self._mcp_read_file,
            "mcp_search_files": self._mcp_search_files,
            "mcp_memory_search": self._mcp_memory_search,
            "mcp_memory_add": self._mcp_memory_add,
        }

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @property
    def backend(self) -> BackendClient:
        return self._backend

    @property
    def mode(self) -> str:
        return str(getattr(self._backend, "mode", "http"))

    def allows(self, name: str) -> bool:
        return self._allowed is None or name in self._allowed

    def tool_names(self) -> List[str]:
        return [
            name for name in self._specs.keys() if self._allowed is None or name in self._allowed
        ]

    def restricted(self, names: Iterable[str]) -> "OpsToolkit":
        """返回共享同一 backend / MCP Bridge 的工具白名单视图（子 Agent 隔离用）。"""
        return OpsToolkit(self._backend, allowed=names, mcp_bridge=self._mcp_bridge)

    def schemas(self) -> List[Dict[str, Any]]:
        """OpenAI function-calling schema（深拷贝，避免调用方污染）。"""
        names = set(self.tool_names())
        return [
            copy.deepcopy(spec)
            for name, spec in self._specs.items()
            if name in names
        ]

    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """执行工具调用，返回统一 envelope（永不抛异常给编排循环之外的调用方）。"""
        started = time.perf_counter()
        args: Dict[str, Any] = arguments if isinstance(arguments, dict) else {}
        envelope: Dict[str, Any] = {
            "ok": False,
            "tool": name,
            "summary": "",
            "data": None,
            "error": None,
            "elapsed_ms": 0.0,
        }
        handler = self._handlers.get(name)
        if handler is None:
            envelope["error"] = "未知工具：%s（可用：%s）" % (name, ", ".join(self.tool_names()))
        elif not self.allows(name):
            envelope["error"] = "工具 %s 不在当前角色白名单内（允许：%s）" % (
                name,
                ", ".join(self.tool_names()),
            )
        else:
            try:
                data, summary = handler(args)
                serialized = json.dumps(data, ensure_ascii=False, default=str)
                if len(serialized) > MAX_TOOL_RESULT_CHARS:
                    data = {
                        "truncated": True,
                        "note": "工具返回数据过大已截断（>%d 字符）" % MAX_TOOL_RESULT_CHARS,
                        "preview": serialized[:MAX_TOOL_RESULT_CHARS],
                    }
                    summary = summary + "（数据过大已截断）"
                envelope.update({"ok": True, "data": data, "summary": summary})
            except ToolArgumentError as exc:
                envelope["error"] = "参数错误：%s" % exc
            except BackendError as exc:
                envelope["error"] = "后端数据获取失败：%s" % exc
            except Exception as exc:  # noqa: BLE001 - 工具失败不能中断 Agent
                envelope["error"] = "工具执行异常：%s: %s" % (type(exc).__name__, exc)
        envelope["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        return envelope

    # ------------------------------------------------------------------ #
    # 工具实现
    # ------------------------------------------------------------------ #
    def _get_health(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        service = self._backend.get("/api/health")
        network_payload = self._backend.get("/api/network-health")
        network_health = network_payload.get("network_health")
        data = {
            "data_source": self.mode,
            "service": _trim_service_health(service),
            "network_health": network_health,
        }
        if isinstance(network_health, dict):
            total = network_health.get("total_devices")
            healthy = network_health.get("healthy_devices")
            percentage = network_health.get("healthy_devices_percentage")
            if percentage is None:
                percentage = network_health.get("health_percentage")
            summary = "后端服务正常，采集 stale=%s；网络健康：%s/%s 台设备健康（%s%%）" % (
                service.get("stale"),
                healthy,
                total,
                percentage,
            )
        else:
            summary = "后端服务正常（stale=%s），但未获取到网络健康总览" % service.get("stale")
        return data, summary

    def _get_devices(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        payload = self._backend.get("/api/devices")
        devices = [self._trim_device(item) for item in payload.get("devices") or []]
        data = {
            "data_source": self.mode,
            "source": payload.get("source"),
            "stale": payload.get("stale"),
            "captured_at_iso": payload.get("captured_at_iso"),
            "count": len(devices),
            "devices": devices,
        }
        reachable = sum(
            1 for device in devices if str(device.get("reachability") or "").lower().startswith("reach")
        )
        unreachable_names = [
            str(device.get("hostname"))
            for device in devices
            if str(device.get("reachability") or "").lower().startswith("unreach")
        ]
        summary = "共 %d 台网络设备，%d 台可达" % (len(devices), reachable)
        if unreachable_names:
            summary += "；不可达：%s" % "、".join(unreachable_names)
        summary += "；数据源=%s，stale=%s" % (payload.get("source"), payload.get("stale"))
        return data, summary

    def _get_topology(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        payload = self._backend.get("/api/topology")
        nodes = [
            {
                "id": node.get("id"),
                "label": node.get("label"),
                "role": node.get("role"),
            }
            for node in payload.get("nodes") or []
        ]
        labels = {node.get("id"): node.get("label") for node in nodes}
        # NC 的 topology node 不一定带 label：用 /api/devices 的 hostname 兜底，
        # 保证 LLM 看到的是设备名而不是 UUID。
        if any(not label for label in labels.values()):
            try:
                devices_payload = self._backend.get("/api/devices")
                for device in devices_payload.get("devices") or []:
                    device_id = device.get("id")
                    if device_id in labels and not labels.get(device_id):
                        labels[device_id] = device.get("hostname")
                for node in nodes:
                    node["label"] = labels.get(node.get("id")) or node.get("label")
            except BackendError:
                pass
        links: List[Dict[str, Any]] = []
        for link in payload.get("links") or []:
            links.append(
                {
                    "id": link.get("id"),
                    "source": link.get("source"),
                    "source_label": labels.get(link.get("source")),
                    "target": link.get("target"),
                    "target_label": labels.get(link.get("target")),
                    "start_port_name": link.get("start_port_name"),
                    "end_port_name": link.get("end_port_name"),
                    "start_port_speed": link.get("start_port_speed"),
                    "end_port_speed": link.get("end_port_speed"),
                    "link_status": link.get("link_status"),
                }
            )
        data = {
            "data_source": self.mode,
            "source": payload.get("source"),
            "stale": payload.get("stale"),
            "counts": {"nodes": len(nodes), "links": len(links)},
            "nodes": nodes,
            "links": links,
        }
        down = [link for link in links if _link_is_down(link)]
        summary = "拓扑共 %d 个节点、%d 条链路" % (len(nodes), len(links))
        if down:
            names = [
                "%s %s ↔ %s %s"
                % (
                    link.get("source_label"),
                    link.get("start_port_name"),
                    link.get("target_label"),
                    link.get("end_port_name"),
                )
                for link in down
            ]
            summary += "；down 链路 %d 条：%s" % (len(down), "；".join(names))
        return data, summary

    def _get_hosts(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        location = _optional_str(args, "location")
        ping_status = _optional_str(args, "ping_status")
        payload = self._backend.get("/api/hosts")
        hosts = [self._trim_host(item) for item in payload.get("hosts") or []]
        filtered = hosts
        if location:
            needle = location.lower()
            filtered = [
                host
                for host in filtered
                if needle
                in " ".join(
                    str(host.get(key) or "")
                    for key in ("hostname", "ip", "connected_device_name", "vlan_id")
                ).lower()
            ]
        if ping_status:
            needle = ping_status.strip().upper()
            filtered = [
                host
                for host in filtered
                if str(host.get("ping_status") or "").upper() == needle
            ]
        data = {
            "data_source": self.mode,
            "source": payload.get("source"),
            "stale": payload.get("stale"),
            "filters": {"location": location, "ping_status": ping_status},
            "count": len(filtered),
            "hosts": filtered,
        }
        failed = [host for host in filtered if _host_is_failed(host)]
        summary = "共 %d 台主机" % len(filtered)
        if failed:
            summary += "，%d 台 ping 失败：%s" % (
                len(failed),
                "、".join(
                    "%s(%s)" % (host.get("hostname"), host.get("ip")) for host in failed[:6]
                ),
            )
        else:
            summary += "，ping 状态未发现失败"
        return data, summary

    def _get_issues(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        limit = _optional_int(args, "limit", default=50, minimum=1, maximum=200)
        payload = self._backend.get("/api/issues", params={"limit": limit})
        issues = [self._trim_issue(item) for item in (payload.get("issues") or [])[:limit]]
        data = {
            "data_source": self.mode,
            "source": payload.get("source"),
            "stale": payload.get("stale"),
            "count": len(issues),
            "issues": issues,
        }
        summary = "共 %d 条活跃健康问题" % len(issues)
        if issues:
            summary += "：%s" % "；".join(
                str(issue.get("issue_name") or issue.get("issue_id")) for issue in issues[:4]
            )
        return data, summary

    def _get_alerts(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        limit = _optional_int(args, "limit", default=30, minimum=1, maximum=200)
        severity = _optional_str(args, "severity")
        if severity and severity.strip().lower() not in ("critical", "warning", "info"):
            severity = None
        params: Dict[str, Any] = {"limit": limit}
        if severity:
            params["severity"] = severity
        payload = self._backend.get("/api/alerts", params=params)
        alerts = [self._trim_alert(item) for item in payload.get("alerts") or []]
        data = {
            "data_source": self.mode,
            "count": len(alerts),
            "summary": payload.get("summary"),
            "alerts": alerts,
        }
        severity_count: Dict[str, int] = {}
        for alert in alerts:
            key = str(alert.get("severity") or "unknown")
            severity_count[key] = severity_count.get(key, 0) + 1
        summary = "共 %d 条告警" % len(alerts)
        if severity_count:
            summary += "（%s）" % "，".join(
                "%s=%d" % (key, value) for key, value in sorted(severity_count.items())
            )
        if alerts:
            summary += "；最新：%s" % str(alerts[0].get("title") or "")
        return data, summary

    def _get_device_detail(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        query = _require_str(args, "device_id_or_name")
        devices_payload = self._backend.get("/api/devices")
        device = _resolve_device(devices_payload.get("devices") or [], query)
        if device is None:
            known = [
                {
                    "id": item.get("id"),
                    "hostname": item.get("hostname"),
                    "management_ip": item.get("management_ip"),
                }
                for item in devices_payload.get("devices") or []
            ]
            data = {
                "data_source": self.mode,
                "found": False,
                "query": query,
                "known_devices": known,
                "note": "未找到匹配设备，可从 known_devices 中选择准确的 hostname/id 重试。",
            }
            return data, "未找到设备 %r；现有设备：%s" % (
                query,
                "、".join(str(item.get("hostname")) for item in known),
            )

        device_id = str(device.get("id") or "")
        hosts_payload = self._backend.get("/api/hosts")
        attached_hosts = [
            self._trim_host(host)
            for host in hosts_payload.get("hosts") or []
            if str(host.get("connected_device_id") or "") == device_id
        ]
        alerts: List[Dict[str, Any]] = []
        try:
            alerts_payload = self._backend.get("/api/alerts", params={"limit": 100})
            alerts = [
                self._trim_alert(alert)
                for alert in alerts_payload.get("alerts") or []
                if str(alert.get("device_id") or "") == device_id
            ]
        except BackendError:
            alerts = []
        metrics_summary: Optional[Dict[str, Any]] = None
        try:
            metrics_payload = self._backend.get(
                "/api/metrics/%s" % device_id, params={"limit": 30}
            )
            metrics_summary = _summarize_metrics(metrics_payload)
        except BackendError:
            metrics_summary = None

        data = {
            "data_source": self.mode,
            "found": True,
            "device": self._trim_device(device),
            "attached_hosts": attached_hosts,
            "hosts_count": len(attached_hosts),
            "alerts": alerts,
            "alerts_count": len(alerts),
            "metrics_summary": metrics_summary,
        }
        summary = "设备 %s（%s）reachability=%s，接入主机 %d 台，相关告警 %d 条" % (
            device.get("hostname"),
            device.get("management_ip"),
            device.get("reachability"),
            len(attached_hosts),
            len(alerts),
        )
        return data, summary

    def _get_metrics(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        query = _require_str(args, "device_id_or_name")
        metric = _optional_str(args, "metric")
        limit = _optional_int(args, "limit", default=30, minimum=1, maximum=500)
        devices_payload = self._backend.get("/api/devices")
        device = _resolve_device(devices_payload.get("devices") or [], query)
        device_id = str((device or {}).get("id") or query)
        params: Dict[str, Any] = {"limit": limit}
        if metric:
            params["metric"] = metric
        payload = self._backend.get("/api/metrics/%s" % device_id, params=params)
        summary_data = _summarize_metrics(payload)
        data = {
            "data_source": self.mode,
            "device_id": payload.get("device_id") or device_id,
            "device_name": (device or {}).get("hostname"),
            "known_device": payload.get("known_device"),
            "metric_filter": metric,
            "source": payload.get("source"),
            "count": payload.get("count"),
            "latest_by_metric": summary_data.get("latest_by_metric"),
            "peak_by_metric": summary_data.get("peak_by_metric"),
            "points": summary_data.get("points"),
        }
        peaks = data.get("peak_by_metric") or {}
        peak_parts = [
            "%s=%s%s" % (name, _fmt_number(item.get("value")), item.get("unit") or "")
            for name, item in list(peaks.items())[:4]
        ]
        summary = "设备 %s 最近 %s 个指标点；峰值：%s" % (
            data.get("device_name") or device_id,
            data.get("count"),
            "，".join(peak_parts) if peak_parts else "无数据",
        )
        return data, summary

    # ------------------------------------------------------------------ #
    # W9 只读工具：场景 / 审计 / 新设备 / 归属推断 / 接入模板
    # ------------------------------------------------------------------ #
    def _get_scene(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        payload = self._backend.get("/api/scene")
        buildings: List[Dict[str, Any]] = []
        device_count = 0
        for building in payload.get("buildings") or []:
            if not isinstance(building, dict):
                continue
            devices = [
                self._trim_scene_device(device)
                for device in building.get("devices") or []
                if isinstance(device, dict)
            ]
            device_count += len(devices)
            buildings.append(
                {
                    "id": building.get("id"),
                    "name": building.get("name"),
                    "position": building.get("position"),
                    "size": building.get("size"),
                    "style": building.get("style"),
                    "device_count": len(devices),
                    "devices": devices,
                }
            )
        data = {
            "data_source": self.mode,
            "version": payload.get("version"),
            "theme": payload.get("theme"),
            "building_count": len(buildings),
            "device_count": device_count,
            "buildings": buildings,
        }
        summary = "场景 v%s：%d 个楼栋、%d 台设备（%s）" % (
            payload.get("version"),
            len(buildings),
            device_count,
            "、".join(
                "%s(%d)" % (building.get("name"), building.get("device_count"))
                for building in buildings
            ),
        )
        return data, summary

    def _get_audit(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        limit = _optional_int(args, "limit", default=20, minimum=1, maximum=200)
        payload = self._backend.get("/api/audit", params={"limit": limit})
        raw_entries = payload.get("items")
        if raw_entries is None:
            raw_entries = payload.get("entries")
        entries = [
            self._trim_audit(entry)
            for entry in (raw_entries or [])[:limit]
            if isinstance(entry, dict)
        ]
        data = {
            "data_source": self.mode,
            "count": len(entries),
            "entries": entries,
        }
        summary = "最近 %d 条审计记录" % len(entries)
        if entries:
            latest = entries[0]
            summary += "；最新：%s 对 %s 执行 %s" % (
                latest.get("who"),
                latest.get("target"),
                latest.get("action"),
            )
        return data, summary

    def _get_new_devices(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        pending: List[Dict[str, Any]] = []
        pending_available = True
        errors: List[str] = []
        try:
            pending_payload = self._backend.get("/api/scene/pending")
            raw_pending = pending_payload.get("devices")
            if raw_pending is None:
                raw_pending = pending_payload.get("pending")
            pending = [item for item in raw_pending or [] if isinstance(item, dict)]
        except BackendError as exc:
            pending_available = False
            errors.append("scene/pending: %s" % exc)

        devices: List[Dict[str, Any]] = []
        scene: Dict[str, Any] = {}
        links: List[Dict[str, Any]] = []
        try:
            devices = list((self._backend.get("/api/devices").get("devices")) or [])
        except BackendError as exc:
            devices = []
            errors.append("devices: %s" % exc)
        try:
            scene = self._backend.get("/api/scene")
        except BackendError as exc:
            scene = {}
            errors.append("scene: %s" % exc)
        try:
            topology_data, _ = self._get_topology({})
            links = topology_data.get("links") or []
        except BackendError as exc:
            links = []
            errors.append("topology: %s" % exc)
        if not devices and not scene and not pending:
            raise BackendError(
                "后端数据获取失败（无法读取 pending/inventory/scene）：%s"
                % "；".join(error[:120] for error in errors[:2])
            )

        index = _device_index(devices)
        results: List[Dict[str, Any]] = []
        seen: set = set()

        def _append(
            device: Dict[str, Any],
            source: str,
            building_hint: Optional[str],
            inference_hint: Optional[Dict[str, Any]] = None,
        ) -> None:
            name = str(device.get("hostname") or device.get("name") or "")
            if not name or name.lower() in seen:
                return
            seen.add(name.lower())
            inference = infer_building(
                device,
                scene,
                links,
                index,
                allow_new_building=False,
            ).to_dict()
            if inference_hint and inference_hint.get("building") and not inference.get(
                "building"
            ):
                inference.update(
                    {
                        "building": inference_hint.get("building"),
                        "building_id": inference_hint.get("building_id"),
                        "rule": inference_hint.get("rule") or "backend_inference",
                        "rule_index": inference_hint.get("rule_index") or 0,
                        "confidence": inference_hint.get("confidence") or "medium",
                        "reason": inference_hint.get("reason")
                        or "后端 /api/scene/pending 已给出归属推断",
                    }
                )
            if building_hint and not inference.get("building"):
                building_name = _building_name_from_hint(scene, building_hint)
                if building_name:
                    inference.update(
                        {
                            "building": building_name,
                            "building_id": building_hint,
                            "rule": "scene_pending",
                            "rule_index": 0,
                            "confidence": "medium",
                            "reason": "后端 pending 队列显示它属于「%s」" % building_name,
                        }
                    )
            results.append(
                {
                    "name": name,
                    "type": device.get("platform_id") or device.get("type") or "",
                    "management_ip": device.get("management_ip") or device.get("ip"),
                    "source": source,
                    "inference": inference,
                }
            )

        for item in pending:
            name = str(item.get("name") or "")
            device = index.get(name.lower()) or index.get(name) or {
                "hostname": name,
                "management_ip": item.get("mgmt_ip") or item.get("management_ip"),
                "platform_id": item.get("model") or item.get("platform_id"),
                "collection_status": item.get("collection_status"),
                "reachability": item.get("reachability"),
            }
            building_hint = _pending_building_hint(item, scene)
            inference_hint = item.get("inference") if isinstance(item.get("inference"), dict) else None
            _append(device, "scene_pending", building_hint, inference_hint)
        if not pending_available or True:  # inventory 差集兜底始终参与
            for device in inventory_new_devices(devices, scene):
                _append(device, "inventory_diff", None, None)

        data = {
            "data_source": self.mode,
            "pending_endpoint_available": pending_available,
            "count": len(results),
            "new_devices": results,
        }
        summary = "发现 %d 台待处理新设备" % len(results)
        if results:
            summary += "：" + "；".join(
                "%s（推测：%s）"
                % (
                    item.get("name"),
                    (item.get("inference") or {}).get("building") or "归属待确认",
                )
                for item in results[:5]
            )
        return data, summary

    def _infer_building(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        query = _require_str(args, "device_id_or_name")
        devices_payload = self._backend.get("/api/devices")
        device = _resolve_device(devices_payload.get("devices") or [], query)
        if device is None:
            return (
                {
                    "data_source": self.mode,
                    "found": False,
                    "query": query,
                    "known_devices": [
                        item.get("hostname") for item in devices_payload.get("devices") or []
                    ],
                },
                "未找到设备 %r，无法推断归属" % query,
            )
        topology_data, _ = self._get_topology({})
        scene = self._backend.get("/api/scene")
        index = _device_index(devices_payload.get("devices") or [])
        inference = infer_building(
            device, scene, topology_data.get("links") or [], index
        )
        data = {
            "data_source": self.mode,
            "found": True,
            "device": self._trim_device(device),
            "inference": inference.to_dict(),
        }
        if inference.building:
            summary = "设备 %s 归属推断：%s（规则 %s，置信度 %s）" % (
                device.get("hostname"),
                inference.building,
                inference.rule,
                inference.confidence,
            )
        else:
            summary = "设备 %s 归属无法自动推断，需询问用户（候选楼栋：%s）" % (
                device.get("hostname"),
                "、".join(inference.candidates) or "无",
            )
        return data, summary

    def _get_device_config_template(
        self, args: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], str]:
        kind_raw = (
            _optional_str(args, "device_kind")
            or _optional_str(args, "device_type")
            or "2960"
        )
        kind = detect_device_kind(kind_raw) or kind_raw
        if kind not in DEVICE_SPECS:
            kind = detect_device_kind(kind_raw) or "2960"
        building = _optional_str(args, "building") or detect_building_name(kind_raw) or "教学楼"
        profile = building_profile(building)
        spec = DEVICE_SPECS[kind]
        vlan = _optional_int(args, "vlan", profile["vlan"], minimum=1, maximum=4000)
        hostname = _optional_str(args, "hostname") or next_hostname(
            [], str(profile["hostname_prefix"])
        )
        mgmt_ip = _optional_str(args, "mgmt_ip") or "%s20" % profile["mgmt_segment"]
        gateway = profile["gateway"]
        uplink_device = "Router-4331" if profile["name"] == "中心机房" else "Core-3650"
        uplink_local_port = (spec.get("uplink_ports") or ["Gi0/1"])[0]
        uplink_remote_port = "Gi1/0/7"
        access_ports: List[str] = []
        if spec["kind"] == "switch":
            template = spec.get("access_port_template")
            if template:
                access_ports = [template % 1]
        cli = render_cli(
            kind,
            hostname,
            vlan,
            profile.get("vlan_name") or "USERS",
            mgmt_ip,
            gateway,
            uplink_local_port,
            access_ports,
        )
        cabling = [
            {
                "local_device": hostname,
                "local_port": uplink_local_port,
                "peer_device": uplink_device,
                "peer_port": uplink_remote_port,
                "cable": "直通线（Copper Straight-Through）",
                "purpose": "上联 Trunk（VLAN %d）" % vlan,
            }
        ]
        data = {
            "data_source": self.mode,
            "device_kind": kind,
            "spec": {
                "label": spec.get("label"),
                "model": spec.get("model"),
                "kind": spec.get("kind"),
                "scene_type": spec.get("scene_type"),
                "ios": spec.get("ios"),
            },
            "building": {
                "name": profile.get("name"),
                "building_id": profile.get("building_id"),
                "vlan": vlan,
                "vlan_name": profile.get("vlan_name"),
                "gateway": gateway,
                "mgmt_segment": profile.get("mgmt_segment"),
                "is_new": profile.get("is_new"),
            },
            "template": {
                "hostname": hostname,
                "mgmt_ip": mgmt_ip,
                "uplink_device": uplink_device,
                "uplink_local_port": uplink_local_port,
                "uplink_remote_port": uplink_remote_port,
                "access_ports": access_ports,
                "cli": cli,
                "cabling": cabling,
                "cabling_markdown": format_cabling_markdown(cabling),
                "verification": render_verification(kind, hostname),
                "undo": render_undo(kind, hostname, vlan),
                "access_steps": (
                    render_device_access_steps(hostname, building)
                    if spec["kind"] in ("pc", "server")
                    else []
                ),
            },
        }
        summary = "已生成 %s 接入模板：%s / VLAN %d / 管理 IP %s / 上联 %s" % (
            spec.get("label"),
            building,
            vlan,
            mgmt_ip,
            uplink_device,
        )
        return data, summary

    # ------------------------------------------------------------------ #
    # W10 只读查询：路径追踪 / 告警根因分组 / 通知状态
    # ------------------------------------------------------------------ #
    def _trace_path(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        source_ip = _require_str(args, "source_ip")
        dest_ip = _require_str(args, "dest_ip")
        if not _is_ipv4(source_ip):
            raise ToolArgumentError("source_ip 不是合法 IPv4 地址：%s" % source_ip)
        if not _is_ipv4(dest_ip):
            raise ToolArgumentError("dest_ip 不是合法 IPv4 地址：%s" % dest_ip)
        # 路径追踪是只读 POST，不带 X-Operator
        payload = self._backend.post(
            "/api/path-trace",
            {"source_ip": source_ip, "dest_ip": dest_ip},
            operator=None,
        )
        if not isinstance(payload, dict):
            raise BackendError("路径追踪响应格式异常（期望 JSON 对象）")
        if payload.get("ok") is False:
            raise BackendError(
                "路径追踪失败：%s" % (payload.get("error") or "后端未返回具体原因")
            )
        hops = [
            self._trim_hop(hop)
            for hop in payload.get("hops") or []
            if isinstance(hop, dict)
        ]
        links = [
            {
                "source": link.get("source"),
                "target": link.get("target"),
                "link_id": link.get("link_id"),
            }
            for link in payload.get("links") or []
            if isinstance(link, dict)
        ]
        trace_status = str(payload.get("trace_status") or "unknown")
        data = {
            "data_source": self.mode,
            "source_ip": payload.get("source_ip") or source_ip,
            "dest_ip": payload.get("dest_ip") or dest_ip,
            "trace_status": trace_status,
            "hops": hops,
            "links": links,
            "elapsed_ms": payload.get("elapsed_ms"),
            "error": payload.get("error"),
        }
        names = [str(hop.get("device_name") or "") for hop in hops if hop.get("device_name")]
        path_text = " → ".join(names) if names else "无 hop 数据"
        if trace_status == "complete":
            summary = "路径追踪完成：%s（共 %d 跳）" % (path_text, len(hops))
        elif trace_status == "partial":
            summary = "路径部分可达：%s；断点：%s" % (
                path_text,
                payload.get("error") or "未知",
            )
        else:
            summary = "路径追踪失败/中断：%s；原因：%s" % (
                path_text,
                payload.get("error") or "未知",
            )
        return data, summary

    def _get_alert_groups(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        state = _optional_str(args, "state") or "active"
        if state not in ("active", "cleared", "all"):
            state = "active"
        payload = self._backend.get("/api/alerts/groups", params={"state": state})
        groups = [
            self._trim_group(group)
            for group in payload.get("groups") or []
            if isinstance(group, dict)
        ]
        total_members = sum(int(group.get("member_count") or 0) for group in groups)
        total_suppressed = sum(int(group.get("suppressed_count") or 0) for group in groups)
        data = {
            "data_source": self.mode,
            "state": payload.get("state") or state,
            "count": len(groups),
            "total_members": total_members,
            "total_suppressed": total_suppressed,
            "groups": groups,
        }
        summary = "共 %d 个根因分组，覆盖 %d 条告警、%d 条被上游抑制" % (
            len(groups),
            total_members,
            total_suppressed,
        )
        if groups:
            summary += "；主要根因：%s" % "、".join(
                "%s(%s)" % (group.get("root_cause"), group.get("root_device_name"))
                for group in groups[:3]
            )
        return data, summary

    def _get_notify_status(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        payload = self._backend.get("/api/notify/status")
        enabled = bool(payload.get("enabled"))
        data = {
            "data_source": self.mode,
            "enabled": enabled,
            "type": payload.get("type"),
            "min_severity": payload.get("min_severity"),
            "last_sent_at": payload.get("last_sent_at"),
            "last_error": payload.get("last_error"),
            "sent_count": payload.get("sent_count"),
            "url_configured": payload.get("url_configured"),
        }
        if enabled:
            summary = "告警通知已启用（type=%s，最低级别=%s，累计发送 %s 条）" % (
                payload.get("type") or "unknown",
                payload.get("min_severity") or "unknown",
                payload.get("sent_count"),
            )
        else:
            summary = "告警通知未启用（未配置 Webhook）"
        if payload.get("last_error"):
            summary += "；最近错误：%s" % str(payload.get("last_error"))[:120]
        return data, summary

    # ------------------------------------------------------------------ #
    # W12：内置知识库检索（轻量 RAG，只读）
    # ------------------------------------------------------------------ #
    def _search_knowledge(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        query = _require_str(args, "query")
        try:
            top_k = int(args.get("top_k") or 5)
        except (TypeError, ValueError):
            top_k = 5
        top_k = max(1, min(10, top_k))
        hits = search_knowledge(query, top_k=top_k)
        data = {
            "data_source": "builtin_knowledge",
            "query": query,
            "count": len(hits),
            "hits": [
                {
                    "rank": hit.get("rank"),
                    "id": hit.get("id"),
                    "score": hit.get("score"),
                    "title": hit.get("title"),
                    "source": hit.get("source"),
                    "text": hit.get("text"),
                    "citation": "[K%d]" % int(hit.get("rank") or 0),
                }
                for hit in hits
            ],
            "note": (
                "以下内容仅来自内置知识库，回答请引用 [K1].."
                if hits
                else "知识库无相关内容"
            ),
        }
        if hits:
            summary = "知识库检索到 %d 条相关内容：%s" % (
                len(hits),
                "；".join("[K%d] %s" % (hit["rank"], hit["title"]) for hit in hits),
            )
        else:
            summary = "知识库无相关内容（内置知识库未命中，回答不得编造）"
        return data, summary

    # ------------------------------------------------------------------ #
    # W13：MCP Bridge 只读工具（filesystem / memory 外部工具）
    # ------------------------------------------------------------------ #
    def _mcp_call(
        self, server: str, tool: str, arguments: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
        """调用 MCP Bridge；返回 (归一化 result, summary, mcp_call trace)。"""
        try:
            payload = self._mcp_bridge.call(server, tool, arguments)
        except MCPBridgeError as exc:
            # OpsToolkit.call 会统一转成 ok=false，不中断 Agent
            raise BackendError(str(exc))
        except Exception as exc:  # noqa: BLE001 - 桥异常归一化
            raise BackendError(
                "MCP Bridge 调用异常：%s: %s" % (type(exc).__name__, str(exc)[:300])
            )
        result = payload.get("result") or {}
        meta = {
            "server": server,
            "tool": tool,
            "ok": bool(payload.get("ok")),
            "elapsed_ms": payload.get("elapsed_ms"),
        }
        text = str((result or {}).get("text") or "")
        summary = "MCP %s/%s 调用成功（%s 字符）" % (
            server,
            tool,
            len(text),
        )
        return result, summary, meta

    def _mcp_list_tools(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        try:
            payload = self._mcp_bridge.list_tools()
        except MCPBridgeError as exc:
            raise BackendError(str(exc))
        except Exception as exc:  # noqa: BLE001
            raise BackendError(
                "MCP Bridge 调用异常：%s: %s" % (type(exc).__name__, str(exc)[:300])
            )
        tools = [
            {
                "server": item.get("server"),
                "name": item.get("name"),
                "description": item.get("description"),
                "input_schema": item.get("input_schema"),
            }
            for item in (payload.get("tools") or [])
        ]
        by_server: Dict[str, int] = {}
        for item in tools:
            key = str(item.get("server") or "unknown")
            by_server[key] = by_server.get(key, 0) + 1
        data = {
            "data_source": "mcp_bridge",
            "total": int(payload.get("total") or len(tools)),
            "count": len(tools),
            "servers": by_server,
            "tools": tools,
            "mcp_calls": [
                {
                    "server": "bridge",
                    "tool": "list_tools",
                    "ok": True,
                    "elapsed_ms": None,
                }
            ],
        }
        summary = "MCP Bridge 接入了 %d 个外部工具（%s）" % (
            len(tools),
            "、".join("%s %d 个" % (name, count) for name, count in sorted(by_server.items())),
        )
        return data, summary

    def _mcp_list_dir(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        path = _require_str(args, "path")
        result, summary, meta = self._mcp_call(
            "filesystem", "list_directory", {"path": path}
        )
        text = str((result or {}).get("text") or "")
        data = {
            "data_source": "mcp_bridge",
            "path": path,
            "text": text,
            "content": (result or {}).get("content"),
            "mcp_calls": [meta],
        }
        return data, "MCP filesystem 列目录 %s：%d 字符" % (path, len(text))

    def _mcp_read_file(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        path = _require_str(args, "path")
        try:
            result, summary, meta = self._mcp_call(
                "filesystem", "read_text_file", {"path": path}
            )
        except BackendError:
            # 兼容旧版 filesystem server 的 read_file 工具名
            result, summary, meta = self._mcp_call(
                "filesystem", "read_file", {"path": path}
            )
        text = str((result or {}).get("text") or "")
        data = {
            "data_source": "mcp_bridge",
            "path": path,
            "content": text,
            "text": text,
            "chars": len(text),
            "mcp_calls": [meta],
        }
        return data, "MCP filesystem 读取 %s：%d 字符" % (path, len(text))

    def _mcp_search_files(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        path = _require_str(args, "path")
        pattern = _require_str(args, "pattern")
        result, summary, meta = self._mcp_call(
            "filesystem", "search_files", {"path": path, "pattern": pattern}
        )
        text = str((result or {}).get("text") or "")
        data = {
            "data_source": "mcp_bridge",
            "path": path,
            "pattern": pattern,
            "text": text,
            "content": (result or {}).get("content"),
            "mcp_calls": [meta],
        }
        return data, "MCP filesystem 搜索 %s/%s：%d 字符" % (path, pattern, len(text))

    def _mcp_memory_search(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        query = _require_str(args, "query")
        result, summary, meta = self._mcp_call(
            "memory", "search_nodes", {"query": query}
        )
        text = str((result or {}).get("text") or "")
        data = {
            "data_source": "mcp_bridge",
            "query": query,
            "text": text,
            "content": (result or {}).get("content"),
            "mcp_calls": [meta],
        }
        return data, "MCP memory 检索「%s」：%d 字符" % (query, len(text))

    def _mcp_memory_add(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        content = _require_str(args, "content")
        entity_name = "campus-note-%d" % int(time.time())
        result, summary, meta = self._mcp_call(
            "memory",
            "create_entities",
            {
                "entities": [
                    {
                        "name": entity_name,
                        "entityType": "note",
                        "observations": [content],
                    }
                ]
            },
        )
        text = str((result or {}).get("text") or "")
        data = {
            "data_source": "mcp_bridge",
            "entity": entity_name,
            "text": text,
            "mcp_calls": [meta],
            "note": "本地 memory 写入（仅本地知识图谱，不是网络设备写操作）",
        }
        return data, "已写入本地 MCP memory（entity=%s，仅本地记忆）" % entity_name

    # ------------------------------------------------------------------ #
    # 字段裁剪（把 NC 原始大字段裁掉，控制上下文长度）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _trim_hop(hop: Dict[str, Any]) -> Dict[str, Any]:
        keys = ("index", "device_name", "device_id", "ingress_port", "egress_port", "role")
        return {key: hop.get(key) for key in keys if key in hop}

    @staticmethod
    def _trim_group(group: Dict[str, Any]) -> Dict[str, Any]:
        keys = (
            "group_id",
            "root_cause",
            "root_device_id",
            "root_device_name",
            "severity",
            "title",
            "alert_ids",
            "member_count",
            "suppressed_count",
            "first_seen",
            "last_seen",
        )
        data = {key: group.get(key) for key in keys if key in group}
        data["members"] = [
            {
                "id": member.get("id"),
                "alert_type": member.get("alert_type"),
                "severity": member.get("severity"),
                "device_name": member.get("device_name"),
                "detail": member.get("detail"),
                "suppressed_by": member.get("suppressed_by"),
            }
            for member in group.get("members") or []
            if isinstance(member, dict)
        ][:20]
        return data

    @staticmethod
    def _trim_scene_device(device: Dict[str, Any]) -> Dict[str, Any]:
        keys = ("name", "type", "position", "status", "source", "model", "mgmt_ip")
        return {key: device.get(key) for key in keys if key in device}

    @staticmethod
    def _trim_audit(entry: Dict[str, Any]) -> Dict[str, Any]:
        keys = (
            "id",
            "operator",
            "who",
            "action",
            "target",
            "time",
            "created_at",
            "created_at_iso",
            "detail",
            "result",
        )
        return {key: entry.get(key) for key in keys if key in entry}

    @staticmethod
    def _trim_device(device: Dict[str, Any]) -> Dict[str, Any]:
        keys = (
            "id",
            "hostname",
            "management_ip",
            "reachability",
            "collection_status",
            "platform_id",
            "product_id",
            "device_type",
            "role",
            "software_version",
            "up_time",
            "last_seen",
            "source",
        )
        return {key: device.get(key) for key in keys if key in device}

    @staticmethod
    def _trim_host(host: Dict[str, Any]) -> Dict[str, Any]:
        keys = (
            "id",
            "hostname",
            "ip",
            "mac",
            "connected_device_id",
            "connected_device_name",
            "connected_interface",
            "host_type",
            "ping_status",
            "vlan_id",
            "last_seen",
            "source",
        )
        return {key: host.get(key) for key in keys if key in host}

    @staticmethod
    def _trim_issue(issue: Dict[str, Any]) -> Dict[str, Any]:
        keys = (
            "issue_id",
            "issue_name",
            "issue_description",
            "issue_source",
            "severity",
            "status",
            "observed_at",
            "source_ts",
            "issue_timestamp",
            "first_seen",
            "last_seen",
            "source",
        )
        return {key: issue.get(key) for key in keys if key in issue}

    @staticmethod
    def _trim_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
        keys = (
            "id",
            "alert_id",
            "alert_type",
            "severity",
            "title",
            "detail",
            "device_id",
            "link_id",
            "issue_id",
            "state",
            "status",
            "created_at",
            "observed_at",
            "source_ts",
            "resolved_at",
            "clear_reason",
            "stale_source",
            "deleted_at",
            "silenced_at",
            "source",
            "acknowledged",
        )
        return {key: alert.get(key) for key in keys if key in alert}


# --------------------------------------------------------------------------- #
# 内部工具函数
# --------------------------------------------------------------------------- #
def _trim_service_health(service: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "status",
        "version",
        "driver",
        "metrics_source",
        "dataset_source",
        "stale",
        "polls",
        "collect_errors",
        "consecutive_failures",
        "poll_interval",
        "server_time",
    )
    return {key: service.get(key) for key in keys if key in service}


def _resolve_device(devices: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
    """按 id / hostname / 管理 IP 解析设备（大小写不敏感，支持包含匹配）。"""
    needle = str(query or "").strip().lower()
    if not needle:
        return None
    for device in devices:
        if str(device.get("id") or "").lower() == needle:
            return device
    for device in devices:
        if str(device.get("hostname") or "").lower() == needle:
            return device
    for device in devices:
        if str(device.get("management_ip") or "").lower() == needle:
            return device
    for device in devices:
        if needle in str(device.get("hostname") or "").lower():
            return device
    for device in devices:
        if needle in str(device.get("id") or "").lower():
            return device
    return None


def _summarize_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把 metrics payload 压缩成 points/latest/peak 三块。"""
    raw_points = payload.get("metrics") or []
    points: List[Dict[str, Any]] = []
    latest: Dict[str, Any] = {}
    peak: Dict[str, Any] = {}
    for item in raw_points:
        name = item.get("metric")
        value = _to_float(item.get("value"))
        if name is None or value is None:
            continue
        point = {
            "metric": name,
            "value": value,
            "unit": item.get("unit"),
            "interface": item.get("interface"),
            "ts": item.get("ts"),
        }
        points.append(point)
        latest[str(name)] = value
        current = peak.get(str(name))
        if current is None or value > float(current.get("value") or 0):
            peak[str(name)] = {
                "value": value,
                "unit": item.get("unit"),
                "interface": item.get("interface"),
                "ts": item.get("ts"),
            }
    return {
        "points": points[-120:],  # 控制上下文长度
        "latest_by_metric": latest,
        "peak_by_metric": peak,
    }


def _link_is_down(link: Dict[str, Any]) -> bool:
    status = str(link.get("link_status") or "").strip().lower()
    return status in ("down", "err-disabled", "disabled")


def _host_is_failed(host: Dict[str, Any]) -> bool:
    status = str(host.get("ping_status") or "").strip().upper()
    return status in ("FAILURE", "FAILED", "DOWN", "UNREACHABLE", "OFFLINE")


def _optional_str(args: Dict[str, Any], key: str) -> Optional[str]:
    value = args.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(
    args: Dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = args.get(key)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _require_str(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    if value is None or not str(value).strip():
        raise ToolArgumentError("缺少必填参数 %s" % key)
    return str(value).strip()


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_number(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return "-"
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return "%.2f" % number


def _device_index(devices: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """设备索引：hostname / 管理 IP / id → 设备对象（用于拓扑邻居解析）。"""
    index: Dict[str, Dict[str, Any]] = {}
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        hostname = str(device.get("hostname") or "").strip()
        management_ip = str(device.get("management_ip") or "").strip()
        device_id = str(device.get("id") or "").strip()
        if hostname:
            index[hostname.lower()] = device
            index[hostname] = device
        if management_ip:
            index[management_ip] = device
        if device_id:
            index[device_id] = device
    return index


def _is_ipv4(value: str) -> bool:
    """简单 IPv4 校验（用于 trace_path 参数）。"""
    parts = str(value or "").strip().split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit():
            return False
        number = int(part)
        if number < 0 or number > 255:
            return False
    return True


def _pending_building_hint(
    item: Dict[str, Any], scene: Dict[str, Any]
) -> Optional[str]:
    """从后端 pending 设备条目中提取楼栋线索（名称或 id）。"""
    for key in ("building_name", "inferred_building", "building", "building_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    inference = item.get("inference")
    if isinstance(inference, dict):
        for key in ("building", "building_name", "building_id"):
            value = inference.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _building_name_from_hint(scene: Dict[str, Any], hint: str) -> Optional[str]:
    """把楼栋 id / 名称线索转成场景里的中文楼栋名。"""
    needle = str(hint or "").strip()
    if not needle:
        return None
    for building in (scene or {}).get("buildings") or []:
        if str(building.get("id") or "") == needle or str(building.get("name") or "") == needle:
            return str(building.get("name") or "") or None
    known = {
        "core": "中心机房",
        "teach": "教学楼",
        "train": "实训楼",
        "dorm": "宿舍楼",
    }
    return known.get(needle, needle)
