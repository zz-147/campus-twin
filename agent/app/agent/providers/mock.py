# -*- coding: utf-8 -*-
"""离线 mock provider：不联网的规则引擎，实现与真实 LLM 相同的「工具调用」协议。

用途：
1. **单测**：完全离线、确定性，覆盖编排循环 / 工具集 / 评测 runner；
2. **无 key 演示**：``LLM_PROVIDER=mock`` 即可起服务、POST /api/chat 返回中文排障建议；
3. **20 场景离线评测**：作为 baseline provider，评测结果可重复。

行为：
- 第 1 轮：规划 ``get_health`` / ``get_devices`` / ``get_hosts``；
- 第 2 轮：规划 ``get_issues`` / ``get_alerts`` / ``get_topology``；
- 第 3 轮：对可疑设备调用 ``get_device_detail``；
- 第 4 轮：对可疑/性能相关设备调用 ``get_metrics``；
- 之后：基于工具结果生成中文诊断（根因 / 依据 / 排障步骤 / 建议命令）。

注意：mock 是**启发式规则** provider，不代表真实 LLM 能力；它的价值是让整条
Agent 链路（工具调用、数据引用、评测打分）在没有 API key / 没有外网时也能验证。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.agent.providers.base import AssistantMessage, ProviderInfo, ToolCallRequest

#: 评测/演示使用固定时区（项目在中国，避免受宿主机 TZ 影响）
_TZ = timezone(timedelta(hours=8))

#: 触发「性能类工具调用」的关键词
PERF_KEYWORDS = (
    "带宽",
    "流量",
    "利用率",
    "cpu",
    "卡",
    "慢",
    "拥塞",
    "负载",
    "高",
    "风暴",
    "环路",
    "内存",
    "延迟",
    "丢包",
    "性能",
)

#: 故障分类规则（顺序敏感：先匹配更具体的根因）
CATEGORY_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("broadcast_storm", ("广播风暴", "广播", "风暴", "broadcast", "storm", "环路", "loop")),
    ("routing_missing", ("路由缺失", "缺省路由", "默认路由", "回程路由", "路由错误", "route", "routing")),
    ("vlan_misconfig", ("vlan 配置错误", "vlan 不一致", "vlan 错误", "vlan mismatch", "vlan")),
    ("cpu_high", ("cpu 过高", "cpu 负载", "cpu 使用率", "cpu utilization", "high cpu", "cpu")),
    ("memory_high", ("内存", "memory")),
    ("bandwidth_high", ("带宽", "流量", "利用率", "拥塞", "congestion", "bandwidth", "utilization")),
    ("link_down", ("链路中断", "链路故障", "链路 down", "link down", "link_down", "link is down")),
    ("port_shutdown", ("端口 shutdown", "端口关闭", "端口被关", "port shutdown", "shutdown")),
    ("device_unreachable", ("设备不可达", "设备离线", "设备失联", "unreachable", "device is unreachable")),
    ("host_down", ("终端离线", "主机离线", "终端掉线", "host down", "device is down", "connectivity", "终端不可达")),
)

#: 各类根因的中文结论模板
ROOT_PHRASES: Dict[str, str] = {
    "host_down": "终端主机离线：{host}（{ip}）ping 检测失败，疑似接入端口或链路故障",
    "device_unreachable": "网络设备不可达：{host}（{ip}），设备失联/离线",
    "link_down": "上联链路中断：{host}",
    "port_shutdown": "接入端口 shutdown：{host}，导致终端无法通信",
    "routing_missing": "路由缺失：{host}（{ip}）缺少缺省路由/回程路由，跨网段流量中断",
    "vlan_misconfig": "VLAN 配置错误：{host}，终端接入 VLAN 与网关不匹配",
    "broadcast_storm": "广播风暴：{host} 存在二层环路，CPU 与端口利用率异常升高",
    "cpu_high": "CPU 负载过高：{host}",
    "memory_high": "内存占用过高：{host}",
    "bandwidth_high": "链路带宽拥塞：{host}",
    "unknown": "未发现明确故障点，需要进一步排查",
}

#: 排障步骤（每类 5 步）
TROUBLESHOOT_STEPS: Dict[str, List[str]] = {
    "host_down": [
        "确认终端网卡/IP/掩码/网关配置是否正确，必要时重启终端网络。",
        "查看接入交换机对应端口的物理状态与 VLAN：show interface status、show vlan brief。",
        "检查端口是否被 shutdown 或触发 err-disabled：show interface <port>。",
        "从接入交换机 ping 终端，确认二层可达；再检查 DHCP/静态地址冲突。",
        "若端口异常，执行 no shutdown / 重新插拔链路，并观察 NC 与后端恢复告警。",
    ],
    "device_unreachable": [
        "从管理网段 ping 设备管理 IP，确认是设备宕机还是链路问题。",
        "检查设备电源/上联链路/管理 VLAN 配置。",
        "登录相邻设备查看 CDP/LLDP 邻居：show cdp neighbors。",
        "查看 NC 的 collectionStatus 与最近采集错误，确认是否单点失联。",
        "恢复后确认后端采集 stale=false、拓扑节点与告警恢复。",
    ],
    "link_down": [
        "确认链路两端端口物理状态：show interface status。",
        "检查端口是否被 shutdown、是否出现 CRC/err-disabled。",
        "核对链路两端速率/双工与 VLAN/trunk 配置是否一致。",
        "查看拓扑与 STP 状态，排除环路或阻塞端口误判。",
        "恢复后观察 NC 链路状态、后端告警 cleared 与主机 ping 状态。",
    ],
    "port_shutdown": [
        "登录接入交换机确认端口管理状态：show running-config interface <port>。",
        "执行 show interface <port> 查看是否 administratively down。",
        "确认该端口 VLAN 归属与终端地址规划一致。",
        "业务需要时执行 no shutdown，并检查端口安全/err-disabled 原因。",
        "回到后端确认主机 ping 恢复、告警状态转 cleared。",
    ],
    "routing_missing": [
        "在边界路由器检查路由表：show ip route，确认默认路由是否存在。",
        "核对 OSPF/静态路由配置与下一跳地址：show running-config | include route。",
        "从核心 ping 边界路由互联地址，确认三层连通性。",
        "检查 NAT/ACL 与回程路由，确认跨网段流量路径。",
        "恢复后从教学楼终端 traceroute 服务器网段验证。",
    ],
    "vlan_misconfig": [
        "核对接入端口 VLAN 归属：show vlan brief、show interface switchport。",
        "确认终端 IP 与所在 VLAN 网段/网关一致。",
        "修正端口 VLAN（如 switchport access vlan 20）并恢复端口。",
        "在核心三层接口确认 SVI 网关状态：show ip interface brief。",
        "从终端重新获取地址并 ping 网关，确认后端主机状态恢复。",
    ],
    "broadcast_storm": [
        "检查交换机端口广播流量与 CPU：show processes cpu、show interface counters。",
        "排查二层环路：show spanning-tree、核对接入端口连线。",
        "确认未启用 BPDU Guard 的端口是否误接环路。",
        "去除环路后用 storm-control 限制广播，并观察 CPU 回落。",
        "在后端查看 CPU/利用率指标与告警是否恢复。",
    ],
    "cpu_high": [
        "查看 CPU 占用进程：show processes cpu sorted。",
        "检查是否由广播风暴/环路或异常流量导致，查看接口利用率。",
        "确认是否有配置变更/大量 ARP 学习等瞬时事件。",
        "必要时限速/隔离异常端口，观察 CPU 回落。",
        "在后端核对 CPU 指标趋势与相关告警。",
    ],
    "memory_high": [
        "查看内存占用：show memory statistics。",
        "排查是否存在路由表膨胀/异常进程。",
        "确认近期配置变更并对比基线。",
        "必要时重启业务无关进程或升级版本。",
        "持续观察指标趋势，确认告警恢复。",
    ],
    "bandwidth_high": [
        "查看接口利用率与流量趋势：show interface、show interface counters。",
        "定位大流量来源（主机/网段），确认是否异常下载或环路广播。",
        "检查链路容量与 QoS/限速配置是否满足业务。",
        "对异常流量做限速/隔离，必要时扩容或负载分担。",
        "在后端核对链路利用率指标与告警是否回落。",
    ],
    "unknown": [
        "先确认后端数据是否新鲜（stale=false、采集轮次正常）。",
        "对比设备/主机/链路状态，列出所有活跃告警与健康问题。",
        "按「核心 → 汇聚 → 接入 → 终端」顺序逐跳排查。",
        "结合最近变更记录定位可能的配置回退点。",
        "如仍无法定位，采集 show tech-support 与后端快照进一步分析。",
    ],
}

#: 建议命令（PT/Cisco IOS 风格）
TROUBLESHOOT_COMMANDS: Dict[str, str] = {
    "host_down": "show interface status\nshow running-config interface FastEthernet0/1\nshow vlan brief\nping <终端IP>",
    "device_unreachable": "show cdp neighbors\nshow ip interface brief\nshow version\nping <管理IP>",
    "link_down": "show interface status\nshow interfaces trunk\nshow spanning-tree\nshow controllers <port>",
    "port_shutdown": "show running-config interface <port>\nshow interface <port>\nconf t\ninterface <port>\n no shutdown",
    "routing_missing": "show ip route\nshow running-config | include ip route\nshow ip ospf neighbor\ntraceroute <目标IP>",
    "vlan_misconfig": "show vlan brief\nshow interface switchport\nconf t\ninterface <port>\n switchport access vlan <正确VLAN>",
    "broadcast_storm": "show processes cpu sorted\nshow interface counters\nshow spanning-tree\nshow storm-control broadcast",
    "cpu_high": "show processes cpu sorted\nshow interface summary\nshow logging | include CPU",
    "memory_high": "show memory statistics\nshow processes memory sorted",
    "bandwidth_high": "show interface\nshow interface counters\nshow policy-map interface",
    "unknown": "show ip interface brief\nshow logging\nshow processes cpu sorted\nshow cdp neighbors",
}


class MockProvider:
    """离线规则 provider（实现 LLMProvider 协议）。"""

    def __init__(self) -> None:
        self._call_count = 0

    # ------------------------------------------------------------------ #
    # 协议方法
    # ------------------------------------------------------------------ #
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            provider="mock",
            model="mock-rules",
            base_url="",
            key_configured=False,
            temperature=0.0,
        )

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AssistantMessage:
        self._call_count += 1
        started = time.perf_counter()
        view = _DataView.from_messages(messages)
        question = _last_user_content(messages)
        available = _available_tools(tools)

        if available:
            planned = self._plan(view, question, available)
            if planned:
                return AssistantMessage(
                    content="",
                    tool_calls=planned,
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                )

        answer = self._answer(view, question, tools_offered=bool(available))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return AssistantMessage(
            content=answer,
            finish_reason="stop",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )

    # ------------------------------------------------------------------ #
    # 规划：分轮次的工具调用
    # ------------------------------------------------------------------ #
    def _plan(self, view: "_DataView", question: str, available: Sequence[str]) -> List[ToolCallRequest]:
        available_set = set(available)
        calls: List[ToolCallRequest] = []

        def add(name: str, arguments: Optional[Dict[str, Any]] = None) -> None:
            if name in available_set:
                calls.append(
                    ToolCallRequest(
                        id="mock_call_%d_%d" % (self._call_count, len(calls) + 1),
                        name=name,
                        arguments=arguments or {},
                        raw_arguments=json.dumps(arguments or {}, ensure_ascii=False),
                    )
                )

        # 第 1 轮：全局态势
        if not view.has("get_devices"):
            add("get_health")
            add("get_devices")
            add("get_hosts")
            return calls

        # 第 2 轮：故障/拓扑明细
        if not (view.has("get_issues") and view.has("get_alerts") and view.has("get_topology")):
            add("get_issues", {"limit": 50})
            add("get_alerts", {"limit": 30})
            add("get_topology")
            return calls

        # 第 3 轮：可疑设备的详情
        detail_targets = [
            device
            for device in _priority_devices(view, question, limit=2)
            if _device_key(device) not in view.detail_targets
        ]
        if detail_targets:
            for device in detail_targets:
                add(
                    "get_device_detail",
                    {"device_id_or_name": device.get("hostname") or device.get("id")},
                )
            return calls

        # 第 4 轮：指标（性能类问题或可疑设备）
        metric_targets = _metric_targets(view, question)
        pending_metrics = [
            device
            for device in metric_targets
            if _device_key(device) not in view.metric_targets
        ]
        if pending_metrics:
            for device in pending_metrics:
                add(
                    "get_metrics",
                    {
                        "device_id_or_name": device.get("hostname") or device.get("id"),
                        "limit": 30,
                    },
                )
            return calls

        return calls

    # ------------------------------------------------------------------ #
    # 最终回答
    # ------------------------------------------------------------------ #
    def _answer(self, view: "_DataView", question: str, tools_offered: bool) -> str:
        if not view.has("get_devices") and not view.has("get_health"):
            return (
                "当前没有可用的后端工具数据，我无法基于真实数据做根因分析。"
                "请确认 campus-twin 后端（http://127.0.0.1:8000）已启动后重试，"
                "或使用 AGENT_DATA_SOURCE=fixture 的离线演示快照。"
            )

        if view.is_backend_unavailable():
            return self._backend_unavailable_answer(view)

        analysis = _analyze(view, question)
        category = analysis["category"]
        primary = analysis["primary"]
        evidence = _evidence_lines(view, analysis, limit=10)

        lines: List[str] = []
        lines.append("针对你的问题：「%s」" % question.strip())
        lines.append("我调用了后端只读接口（%s）获取真实数据后，分析如下。" % "、".join(view.tool_sequence))
        lines.append("")
        lines.append("## 根因")
        if primary:
            phrase = ROOT_PHRASES.get(category, ROOT_PHRASES["unknown"]).format(
                host=primary.get("name") or primary.get("id") or "未知设备",
                ip=primary.get("ip") or "IP 未知",
            )
            lines.append("- %s" % phrase)
        else:
            lines.append("- %s" % ROOT_PHRASES["unknown"])
            if category == "unknown" and analysis.get("fallback_note"):
                lines.append("- %s" % analysis["fallback_note"])
        if analysis.get("secondary_note"):
            lines.append("- 关联影响：%s" % analysis["secondary_note"])

        lines.append("")
        lines.append("## 依据（引用后端真实数据）")
        if evidence:
            for item in evidence:
                lines.append("- %s" % item)
        else:
            lines.append("- 后端当前未返回活跃告警/健康问题，设备与主机状态均正常，未发现可定位的异常。")

        lines.append("")
        lines.append("## 排障步骤")
        for index, step in enumerate(TROUBLESHOOT_STEPS.get(category, TROUBLESHOOT_STEPS["unknown"]), start=1):
            lines.append("%d. %s" % (index, step))

        lines.append("")
        lines.append("## 建议命令")
        lines.append("```")
        lines.append(TROUBLESHOOT_COMMANDS.get(category, TROUBLESHOOT_COMMANDS["unknown"]))
        lines.append("```")

        lines.append("")
        lines.append("## 数据说明")
        lines.append(
            "- 数据来源：后端 campus-twin API（%s 模式），共 %d 台网络设备、%d 台主机、%d 条健康问题、%d 条告警。"
            % (
                view.data_mode or "http",
                len(view.devices),
                len(view.hosts),
                len(view.issues),
                len(view.alerts),
            )
        )
        lines.append(
            "- 本次 provider=mock（离线规则引擎，model=mock-rules），未联网；回答基于上列工具结果生成，不包含编造设备。"
        )
        if view.tool_errors:
            lines.append(
                "- 注意：有 %d 次工具调用失败（%s），结论可能不完整。"
                % (len(view.tool_errors), view.tool_errors[0][:120])
            )
        return "\n".join(lines)

    def _backend_unavailable_answer(self, view: "_DataView") -> str:
        error = view.tool_errors[0] if view.tool_errors else "后端无响应"
        return "\n".join(
            [
                "## 根因",
                "当前无法完成诊断：后端数据接口不可用，获取不到真实拓扑/设备/主机状态。",
                "",
                "## 依据",
                "- 工具调用失败：%s" % error[:300],
                "- 为避免编造数据，这里不给出猜测性结论。",
                "",
                "## 排障步骤",
                "1. 确认 campus-twin 后端已启动：uvicorn app.main:app --port 8000。",
                "2. 浏览器/curl 检查 http://127.0.0.1:8000/api/health 是否返回 status=ok。",
                "3. 确认后端 collectionStatus 正常、接口 /api/devices 有数据。",
                "4. 后端恢复后重新提问，我会基于真实数据做根因定位。",
                "",
                "## 建议命令（检查后端）",
                "```",
                "curl http://127.0.0.1:8000/api/health",
                "curl http://127.0.0.1:8000/api/devices",
                "```",
            ]
        )


# --------------------------------------------------------------------------- #
# 工具结果视图
# --------------------------------------------------------------------------- #
class _DataView:
    """把 messages 里的 tool 结果解析成结构化视图（无状态、可重复解析）。"""

    def __init__(self) -> None:
        self.devices: List[Dict[str, Any]] = []
        self.hosts: List[Dict[str, Any]] = []
        self.issues: List[Dict[str, Any]] = []
        self.alerts: List[Dict[str, Any]] = []
        self.topology_nodes: List[Dict[str, Any]] = []
        self.topology_links: List[Dict[str, Any]] = []
        self.network_health: Optional[Dict[str, Any]] = None
        self.service_health: Optional[Dict[str, Any]] = None
        self.metrics: Dict[str, Dict[str, Any]] = {}
        self.details: Dict[str, Dict[str, Any]] = {}
        self.tool_sequence: List[str] = []
        self.tool_errors: List[str] = []
        self.tool_count = 0
        self.data_mode_hint = ""

    @classmethod
    def from_messages(cls, messages: List[Dict[str, Any]]) -> "_DataView":
        view = cls()
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            view.tool_count += 1
            envelope = _loads(message.get("content"))
            if not isinstance(envelope, dict):
                continue
            tool_name = str(envelope.get("tool") or "")
            if tool_name:
                view.tool_sequence.append(tool_name)
            if not envelope.get("ok"):
                error = str(envelope.get("error") or "工具调用失败")
                view.tool_errors.append("%s: %s" % (tool_name or "tool", error))
                continue
            data = envelope.get("data")
            if not isinstance(data, dict):
                continue
            if tool_name == "get_devices":
                view.devices = list(data.get("devices") or [])
                if data.get("data_source"):
                    view.data_mode_hint = str(data.get("data_source"))
            elif tool_name == "get_hosts":
                view.hosts = list(data.get("hosts") or [])
            elif tool_name == "get_issues":
                view.issues = list(data.get("issues") or [])
            elif tool_name == "get_alerts":
                view.alerts = list(data.get("alerts") or [])
            elif tool_name == "get_topology":
                view.topology_nodes = list(data.get("nodes") or [])
                view.topology_links = list(data.get("links") or [])
            elif tool_name == "get_health":
                health = data.get("network_health")
                view.network_health = health if isinstance(health, dict) else None
                service = data.get("service")
                view.service_health = service if isinstance(service, dict) else None
                if data.get("data_source"):
                    view.data_mode_hint = str(data.get("data_source"))
            elif tool_name == "get_device_detail":
                device = data.get("device") or {}
                key = _device_key(device) or str(data.get("device_id") or "")
                if key:
                    view.details[key] = data
            elif tool_name == "get_metrics":
                key = str(data.get("device_id") or "")
                if not key:
                    device = _find_device_by_name(view.devices, str(data.get("device_name") or ""))
                    key = _device_key(device) if device else ""
                if key:
                    view.metrics[key] = data
        return view

    def has(self, tool_name: str) -> bool:
        return tool_name in self.tool_sequence

    @property
    def data_mode(self) -> str:
        if self.data_mode_hint:
            return self.data_mode_hint
        source = (self.service_health or {}).get("data_source") or ""
        if source:
            return str(source)
        return "fixture" if self.details or self.metrics else "http"

    @property
    def detail_targets(self) -> List[str]:
        return list(self.details.keys())

    @property
    def metric_targets(self) -> List[str]:
        return list(self.metrics.keys())

    def is_backend_unavailable(self) -> bool:
        """所有工具都失败且没有任何数据 → 判定后端不可用（用于给出诚实的降级回答）。"""
        if not self.tool_count or not self.tool_errors:
            return False
        return not bool(self.devices or self.hosts or self.issues or self.alerts)


# --------------------------------------------------------------------------- #
# 分析逻辑
# --------------------------------------------------------------------------- #
def _analyze(view: _DataView, question: str) -> Dict[str, Any]:
    failed_hosts = [host for host in view.hosts if _is_host_failed(host)]
    unreachable = [device for device in view.devices if _is_unreachable(device)]
    down_links = [link for link in view.topology_links if _is_link_down(link)]

    texts: List[str] = []
    for issue in view.issues:
        texts.append(str(issue.get("issue_name") or ""))
        texts.append(str(issue.get("issue_description") or ""))
        texts.append(str(issue.get("issue_source") or ""))
    for alert in view.alerts:
        texts.append(str(alert.get("title") or ""))
        texts.append(str(alert.get("detail") or ""))
        texts.append(str(alert.get("alert_type") or ""))
    combined = " ".join(texts)

    category = _classify(combined)
    abnormal_metrics = _abnormal_metrics(view)

    if category == "unknown":
        if failed_hosts:
            category = "host_down"
        elif down_links:
            category = "link_down"
        elif unreachable:
            category = "device_unreachable"
        elif abnormal_metrics:
            category = {
                "cpu_pct": "cpu_high",
                "mem_pct": "memory_high",
                "if_utilization_pct": "bandwidth_high",
            }.get(abnormal_metrics[0]["metric"], "bandwidth_high")

    primary = _resolve_primary(view, category, failed_hosts, unreachable, down_links, abnormal_metrics, texts)
    secondary_note = _secondary_note(view, category, primary, failed_hosts, unreachable, down_links)
    fallback_note = None
    if category == "unknown" and view.devices:
        fallback_note = (
            "已检查 %d 台设备、%d 台主机，均未发现明显异常；若现象仍在，请补充故障时间与影响范围。"
            % (len(view.devices), len(view.hosts))
        )

    return {
        "category": category,
        "primary": primary,
        "secondary_note": secondary_note,
        "fallback_note": fallback_note,
        "failed_hosts": failed_hosts,
        "unreachable": unreachable,
        "down_links": down_links,
        "abnormal_metrics": abnormal_metrics,
    }


def _classify(text: str) -> str:
    lowered = text.lower()
    for category, keywords in CATEGORY_RULES:
        for keyword in keywords:
            if keyword in lowered:
                return category
    return "unknown"


def _resolve_primary(
    view: _DataView,
    category: str,
    failed_hosts: List[Dict[str, Any]],
    unreachable: List[Dict[str, Any]],
    down_links: List[Dict[str, Any]],
    abnormal_metrics: List[Dict[str, Any]],
    texts: List[str],
) -> Optional[Dict[str, Any]]:
    devices = view.devices

    if category in ("host_down", "port_shutdown", "vlan_misconfig") and failed_hosts:
        host = failed_hosts[0]
        return {
            "kind": "host",
            "name": host.get("hostname"),
            "id": host.get("id"),
            "ip": host.get("ip"),
            "device": host.get("connected_device_name"),
            "port": host.get("connected_interface"),
        }
    if category == "device_unreachable" and unreachable:
        return _device_brief(unreachable[0])
    if category == "link_down" and down_links:
        link = down_links[0]
        node = _node_for(view.topology_nodes, str(link.get("target") or ""))
        end = _node_for(view.topology_nodes, str(link.get("source") or ""))
        target_label = node.get("label") if node else str(link.get("target") or "")
        source_label = end.get("label") if end else str(link.get("source") or "")
        return {
            "kind": "link",
            "name": target_label,
            "id": str(link.get("target") or ""),
            "ip": "",
            "link_text": "%s %s ↔ %s %s"
            % (
                source_label,
                link.get("start_port_name") or "?",
                target_label,
                link.get("end_port_name") or "?",
            ),
            "peer": source_label,
        }

    # 性能类 / 兜底：从告警、问题、指标里找设备
    mentioned = _mentioned_device(texts, devices)
    if mentioned and category in (
        "cpu_high",
        "memory_high",
        "bandwidth_high",
        "broadcast_storm",
        "unknown",
    ):
        return _device_brief(mentioned)

    for device_id in [alert.get("device_id") for alert in view.alerts]:
        device = _find_device_by_id(devices, str(device_id or ""))
        if device:
            return _device_brief(device)

    if abnormal_metrics:
        device = _find_device_by_id(devices, abnormal_metrics[0]["device_id"])
        if device:
            return _device_brief(device)

    if failed_hosts:
        host = failed_hosts[0]
        return {
            "kind": "host",
            "name": host.get("hostname"),
            "id": host.get("id"),
            "ip": host.get("ip"),
            "device": host.get("connected_device_name"),
            "port": host.get("connected_interface"),
        }
    if unreachable:
        return _device_brief(unreachable[0])
    return None


def _secondary_note(
    view: _DataView,
    category: str,
    primary: Optional[Dict[str, Any]],
    failed_hosts: List[Dict[str, Any]],
    unreachable: List[Dict[str, Any]],
    down_links: List[Dict[str, Any]],
) -> Optional[str]:
    notes: List[str] = []
    if category == "link_down" and down_links:
        for host in failed_hosts:
            notes.append(
                "%s（%s）因上联链路中断 ping 失败" % (host.get("hostname"), host.get("ip"))
            )
        if primary and primary.get("peer"):
            notes.append("链路两端：%s 与 %s" % (primary.get("peer"), primary.get("name")))
    elif failed_hosts and category not in ("host_down", "port_shutdown", "vlan_misconfig"):
        for host in failed_hosts[:3]:
            notes.append("%s（%s）离线" % (host.get("hostname"), host.get("ip")))
    if unreachable and category != "device_unreachable":
        for device in unreachable[:2]:
            notes.append("%s（%s）不可达" % (device.get("hostname"), device.get("management_ip")))
    if primary and primary.get("kind") == "host" and primary.get("device"):
        notes.append(
            "接入设备 %s %s" % (primary.get("device"), primary.get("port") or "")
        )
    if not notes:
        return None
    return "；".join(dict.fromkeys(note for note in notes if note.strip()))


def _evidence_lines(view: _DataView, analysis: Dict[str, Any], limit: int = 10) -> List[str]:
    lines: List[str] = []
    health = view.network_health or {}
    if health:
        total = health.get("total_devices") or len(view.devices)
        healthy = health.get("healthy_devices")
        percentage = health.get("healthy_devices_percentage")
        if percentage is None:
            percentage = health.get("health_percentage")
        clients = health.get("healthy_clients_percentage")
        unreachable = health.get("unreachable_devices", 0)
        if total:
            # 部分后端版本只给百分比、不给健康设备数（healthy_devices=0 时不可当真），
            # 这里优先展示可信字段，避免出现「0/5 健康但 100%」的矛盾表述。
            if healthy and int(healthy) > 0:
                lines.append(
                    "网络健康总览：设备 %s/%s 健康（%s%%），客户端健康度 %s%%，不可达设备 %s 台。"
                    % (
                        healthy,
                        total,
                        percentage if percentage is not None else "100",
                        clients if clients is not None else "100",
                        unreachable,
                    )
                )
            else:
                lines.append(
                    "网络健康总览：设备健康度 %s%%，客户端健康度 %s%%，不可达设备 %s 台。"
                    % (
                        percentage if percentage is not None else "100",
                        clients if clients is not None else "100",
                        unreachable,
                    )
                )

    category = analysis.get("category")
    for host in analysis.get("failed_hosts", []):
        lines.append(
            "主机 %s（%s）ping_status=%s，接入 %s %s，VLAN %s，最后更新 %s。"
            % (
                host.get("hostname"),
                host.get("ip"),
                host.get("ping_status"),
                host.get("connected_device_name") or "未知设备",
                host.get("connected_interface") or "未知端口",
                host.get("vlan_id") or "-",
                _iso(host.get("last_seen") or host.get("last_updated")) or "未知时间",
            )
        )
    for device in analysis.get("unreachable", []):
        lines.append(
            "设备 %s（%s）reachability=%s，collectionStatus=%s，平台 %s，最后更新 %s。"
            % (
                device.get("hostname"),
                device.get("management_ip"),
                device.get("reachability"),
                device.get("collection_status"),
                device.get("platform_id") or "-",
                _iso(device.get("last_seen") or device.get("last_updated")) or "未知时间",
            )
        )
    for link in analysis.get("down_links", []):
        source_node = _node_for(view.topology_nodes, str(link.get("source") or ""))
        target_node = _node_for(view.topology_nodes, str(link.get("target") or ""))
        lines.append(
            "链路 %s %s ↔ %s %s 状态=%s（link_id=%s）。"
            % (
                (source_node or {}).get("label") or link.get("source"),
                link.get("start_port_name") or "?",
                (target_node or {}).get("label") or link.get("target"),
                link.get("end_port_name") or "?",
                str(link.get("link_status") or "unknown").lower(),
                link.get("id") or "-",
            )
        )

    for issue in view.issues:
        lines.append(
            "健康问题：%s — %s（来源：%s，时间：%s）。"
            % (
                issue.get("issue_name") or "未命名问题",
                issue.get("issue_description") or "",
                issue.get("issue_source") or "NC",
                _iso(issue.get("observed_at") or issue.get("first_seen") or issue.get("issue_timestamp"))
                or "未知时间",
            )
        )
    for alert in view.alerts:
        lines.append(
            "告警[%s] %s：%s（%s，时间：%s）。"
            % (
                alert.get("severity") or "warning",
                alert.get("title") or "未命名告警",
                alert.get("detail") or "",
                alert.get("alert_type") or "alert",
                _iso(alert.get("created_at")) or "未知时间",
            )
        )
    for item in analysis.get("abnormal_metrics", []):
        lines.append(
            "指标异常：%s %s 峰值 %s%s（接口 %s，时间 %s）。"
            % (
                item.get("device_name") or item.get("device_id"),
                item.get("metric"),
                _fmt_number(item.get("value")),
                item.get("unit") or "",
                item.get("interface") or "-",
                _iso(item.get("ts")) or "未知时间",
            )
        )

    for key, detail in view.details.items():
        device = detail.get("device") or {}
        attached = detail.get("attached_hosts") or []
        related_alerts = detail.get("alerts") or []
        lines.append(
            "设备详情：%s（%s）role=%s，关联主机 %d 台，相关告警 %d 条。"
            % (
                device.get("hostname") or key,
                device.get("management_ip") or "-",
                device.get("role") or "-",
                len(attached),
                len(related_alerts),
            )
        )

    # 无异常时也给出可核对的"全量健康"依据，避免回答只有一句"未发现异常"
    has_symptom = bool(
        analysis.get("failed_hosts")
        or analysis.get("unreachable")
        or analysis.get("down_links")
        or view.issues
        or view.alerts
        or analysis.get("abnormal_metrics")
    )
    if not has_symptom:
        reachable = [
            device.get("hostname")
            for device in view.devices
            if str(device.get("reachability") or "").lower().startswith("reach")
        ]
        if view.devices:
            lines.append(
                "设备状态：%d 台网络设备中 %d 台 Reachable（%s），无不可达设备。"
                % (len(view.devices), len(reachable), "、".join(reachable[:8]) or "-")
            )
        success_hosts = [
            host
            for host in view.hosts
            if str(host.get("ping_status") or "").upper() == "SUCCESS"
        ]
        if view.hosts:
            lines.append(
                "主机状态：%d 台主机中 %d 台 ping_status=SUCCESS（%s），无离线终端。"
                % (
                    len(view.hosts),
                    len(success_hosts),
                    "、".join(
                        "%s %s" % (host.get("hostname"), host.get("ip"))
                        for host in success_hosts[:8]
                    )
                    or "-",
                )
            )

    # 去重并截断
    unique: List[str] = []
    for line in lines:
        if line and line not in unique:
            unique.append(line)
    return unique[:limit]


# --------------------------------------------------------------------------- #
# 数据判定辅助
# --------------------------------------------------------------------------- #
def _is_host_failed(host: Dict[str, Any]) -> bool:
    status = str(host.get("ping_status") or host.get("status") or "").strip().upper()
    return status in ("FAILURE", "FAILED", "DOWN", "UNREACHABLE", "OFFLINE")


def _is_unreachable(device: Dict[str, Any]) -> bool:
    reachability = str(device.get("reachability") or "").strip().lower()
    collection = str(device.get("collection_status") or "").strip().lower()
    if any(word in reachability for word in ("unreachable", "down", "offline")):
        return True
    return collection in ("unreachable", "failed")


def _is_link_down(link: Dict[str, Any]) -> bool:
    status = str(link.get("link_status") or "").strip().lower()
    return status in ("down", "err-disabled", "disabled", "down ", "false")


def _abnormal_metrics(view: _DataView) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for device_id, data in view.metrics.items():
        device = _find_device_by_id(view.devices, device_id)
        name = (device or {}).get("hostname") or data.get("device_name") or device_id
        peaks = data.get("peak_by_metric") or {}
        for metric, threshold in (("cpu_pct", 80.0), ("mem_pct", 85.0), ("if_utilization_pct", 80.0)):
            peak = peaks.get(metric)
            if isinstance(peak, dict):
                value = _to_float(peak.get("value"))
                if value is not None and value >= threshold:
                    rows.append(
                        {
                            "device_id": device_id,
                            "device_name": name,
                            "metric": metric,
                            "value": value,
                            "unit": peak.get("unit") or ("%" if metric.endswith("_pct") else ""),
                            "interface": peak.get("interface"),
                            "ts": peak.get("ts"),
                        }
                    )
    rows.sort(key=lambda row: row["value"], reverse=True)
    return rows


def _priority_devices(view: _DataView, question: str, limit: int = 2) -> List[Dict[str, Any]]:
    """可能出问题的网络设备（用于 device_detail / metrics）。"""
    devices = view.devices
    picked: List[Dict[str, Any]] = []
    seen: set = set()

    def push(device: Optional[Dict[str, Any]]) -> None:
        if not device:
            return
        key = _device_key(device)
        if key and key not in seen:
            seen.add(key)
            picked.append(device)

    # 1) 问题中直接点名的设备
    lowered = question.lower()
    for device in devices:
        hostname = str(device.get("hostname") or "").lower()
        ip = str(device.get("management_ip") or "").lower()
        if hostname and hostname in lowered or (ip and ip in lowered):
            push(device)
    # 2) 不可达设备
    for device in devices:
        if _is_unreachable(device):
            push(device)
    # 3) 告警涉及设备
    for alert in view.alerts:
        push(_find_device_by_id(devices, str(alert.get("device_id") or "")))
    # 4) 掉线主机接入的设备
    for host in view.hosts:
        if _is_host_failed(host):
            push(_find_device_by_id(devices, str(host.get("connected_device_id") or "")))
    return picked[:limit]


def _metric_targets(view: _DataView, question: str) -> List[Dict[str, Any]]:
    priority = _priority_devices(view, question, limit=5)
    if priority:
        return priority
    lowered = question.lower()
    if any(keyword in lowered for keyword in PERF_KEYWORDS):
        return list(view.devices[:5])
    return []


def _mentioned_device(texts: Iterable[str], devices: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    combined = " ".join(str(text) for text in texts).lower()
    for device in devices:
        hostname = str(device.get("hostname") or "").lower()
        ip = str(device.get("management_ip") or "").lower()
        if (hostname and hostname in combined) or (ip and ip in combined):
            return device
    return None


def _device_brief(device: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "device",
        "name": device.get("hostname"),
        "id": device.get("id"),
        "ip": device.get("management_ip"),
    }


def _device_key(device: Dict[str, Any]) -> str:
    return str(device.get("id") or device.get("hostname") or "")


def _find_device_by_id(devices: List[Dict[str, Any]], device_id: str) -> Optional[Dict[str, Any]]:
    if not device_id:
        return None
    for device in devices:
        if str(device.get("id") or "") == device_id:
            return device
    return None


def _find_device_by_name(devices: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    if not name:
        return None
    needle = name.strip().lower()
    for device in devices:
        if str(device.get("hostname") or "").lower() == needle:
            return device
    for device in devices:
        if needle and needle in str(device.get("hostname") or "").lower():
            return device
    return None


def _node_for(nodes: List[Dict[str, Any]], node_id: str) -> Optional[Dict[str, Any]]:
    for node in nodes:
        if str(node.get("id") or "") == node_id:
            return node
    return None


# --------------------------------------------------------------------------- #
# 消息解析辅助
# --------------------------------------------------------------------------- #
def _last_user_content(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _available_tools(tools: Optional[List[Dict[str, Any]]]) -> List[str]:
    names: List[str] = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names


def _loads(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or ""))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _iso(ts: Any) -> str:
    value = _to_float(ts)
    if value is None:
        return ""
    try:
        return datetime.fromtimestamp(value, tz=_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


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
