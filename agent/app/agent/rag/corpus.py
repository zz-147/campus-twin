# -*- coding: utf-8 -*-
"""W12 轻量 RAG 内置知识库（corpus）。

约束：**只读内置文本，不联网、不读用户目录**；每条 chunk 带稳定 id/source/title/text。
内容来源：
1. 设备配置模板（2960 接入 / 3650 核心 / 4331 路由器命令）
2. 校园网 IP/VLAN/拓扑规划（来自 docs/校园网络拓扑设计.md 的核心事实）
3. 故障处置 runbook（端口 down / 链路断 / 路由缺失 / VLAN 错 / 广播风暴）
4. 本项目关键约束（NC 告警滞后、时间基准、路径追踪限制、告警抑制、action 协议）
"""
from __future__ import annotations

from typing import Any, Dict, List

#: 内置知识库分块（title/source/text 均为中文，id 稳定用于 citation [K1]..）
KNOWLEDGE_CHUNKS: List[Dict[str, Any]] = [
    # ------------------------------------------------------------------ #
    # 1) 设备配置模板
    # ------------------------------------------------------------------ #
    {
        "id": "K-CFG-2960-UPLINK",
        "source": "设备配置模板",
        "title": "2960 接入交换机上联核心配置（Trunk + 管理 SVI）",
        "text": (
            " Cisco 2960-24TT 接入交换机上联核心：Fa0/24 配置为 trunk，允许管理 VLAN 10 与业务 VLAN；"
            "上联端口命令：interface FastEthernet0/24 / switchport mode trunk / switchport trunk allowed vlan 10,20 /"
            " no shutdown。管理 SVI：interface Vlan10 / ip address 192.168.10.x 255.255.255.0 / no shutdown；"
            "默认网关指向核心 SVI：ip default-gateway 192.168.10.1。接入端口配 access VLAN（教学楼 20、"
            "实训楼 30、宿舍楼 40）并启用 spanning-tree portfast。核心侧对应端口：Core-3650 Gi1/0/1→SW-JX-2960、"
            "Gi1/0/2→SW-SX-2960、Gi1/0/3→SW-SS-2960，均 trunk（VLAN 10 + 对应业务 VLAN）。"
        ),
    },
    {
        "id": "K-CFG-3650-CORE",
        "source": "设备配置模板",
        "title": "3650 核心交换机 VLAN/OSPF/SVI 配置模板",
        "text": (
            " Cisco 3650-24PS 核心三层交换机：启用 ip routing；为每个业务网段建 SVI：VLAN10 MGMT 192.168.10.0/24、"
            "VLAN20 TEACHING 192.168.20.0/24、VLAN30 TRAINING 192.168.30.0/24、VLAN40 DORM 192.168.40.0/24、"
            "VLAN50 SERVER 192.168.50.0/24，网关分别为 .1。下联接入交换机端口 trunk 放行 VLAN10+业务 VLAN；"
            "服务器端口 access VLAN50；NC 端口 access VLAN10。OSPF：router ospf 1 / network 192.168.10.0 0.0.0.255 area 0 /"
            " network 192.168.20.0 0.0.0.255 area 0（其余网段同理）。上行边界路由器互联可配默认路由指向 Router-4331。"
        ),
    },
    {
        "id": "K-CFG-4331-ROUTER",
        "source": "设备配置模板",
        "title": "4331 边界路由器静态路由/NAT 要点",
        "text": (
            " Cisco 4331 边界路由器：与 Core-3650 三层互联（如 Gi0/0/0↔Core Gi1/0/4），内网回程路由 ip route"
            " 192.168.10.0 255.255.255.0 10.0.0.1（管理网示例）、业务网段指向核心；出口 NAT：interface Gi0/0/1 /"
            " ip nat outside，内网接口 ip nat inside，access-list 1 permit 192.168.0.0 0.0.255.255 / ip nat inside source"
            " list 1 interface Gi0/0/1 overload；默认路由 ip route 0.0.0.0 0.0.0.0 <运营商网关>。"
            "校内部署时 Router-4331 也承担跨 VLAN 兜底路由，排障先 show ip route 看默认路由是否存在。"
        ),
    },
    # ------------------------------------------------------------------ #
    # 2) IP/VLAN/拓扑规划
    # ------------------------------------------------------------------ #
    {
        "id": "K-TOPO-DEVICES",
        "source": "校园网络拓扑设计",
        "title": "校园网设备与端口拓扑（5 网络设备 + 6 终端 + 1 服务器）",
        "text": (
            " 核心：Core-3650（3650-24PS，管理 192.168.10.1，中心机房）；接入：SW-JX-2960（教学楼，192.168.10.11）、"
            "SW-SX-2960（实训楼，192.168.10.12）、SW-SS-2960（宿舍楼，192.168.10.13）；边界：Router-4331"
            "（Lo0 192.168.10.254）；NC 192.168.10.100。终端：PC-JX1 192.168.20.10、PC-JX2 192.168.20.11、"
            "PC-SX1 192.168.30.10、PC-SS1 192.168.40.10、PC-SS2 192.168.40.11；服务器 Server-1 192.168.50.10。"
            "链路：Core Gi1/0/1↔SW-JX Fa0/24、Gi1/0/2↔SW-SX Fa0/24、Gi1/0/3↔SW-SS Fa0/24 均 trunk；"
            "Core Gi1/0/4↔NC access VLAN10，Gi1/0/5↔Server-1 access VLAN50；接入交换机 Fa0/1..接终端。"
        ),
    },
    {
        "id": "K-TOPO-VLAN",
        "source": "校园网络拓扑设计",
        "title": "校园网 IP/VLAN 规划（VLAN10/20/30/40/50）",
        "text": (
            " VLAN 规划：VLAN10 MGMT 192.168.10.0/24（设备管理+NC，网关 Core SVI10 .1）；VLAN20 TEACHING"
            " 192.168.20.0/24（教学楼终端，网关 .1）；VLAN30 TRAINING 192.168.30.0/24（实训楼终端，网关 .1）；"
            "VLAN40 DORM 192.168.40.0/24（宿舍楼终端，网关 .1）；VLAN50 SERVER 192.168.50.0/24（服务器，网关 .1）。"
            "终端网关统一由核心 SVI 提供；跨 VLAN 互访经核心三层转发。新增接入交换机时管理 IP 从 192.168.10.0/24 分配，"
            "业务 VLAN 按楼栋选择（教学楼 20 / 实训楼 30 / 宿舍楼 40）。"
        ),
    },
    {
        "id": "K-TOPO-NC",
        "source": "校园网络拓扑设计",
        "title": "Network Controller（NC）纳管要求与发现限制",
        "text": (
            " NC 192.168.10.100 由 Core Gi1/0/4（access VLAN10）接入，NC 自身需配默认网关 192.168.10.1 才能跨网段纳管设备；"
            "同网段设备无需网关即可纳管。NC 拓扑只包含已纳管网元，链路来自邻接关系，核心未纳管时 links=0；"
            "终端（host）发现依赖网络设备侧信息，核心纳管后 6 台终端自动发现。核心 Neighbor 信息来自 CDP/LLDP，"
            "若邻居缺失先检查接口 VLAN 与物理连线。"
        ),
    },
    # ------------------------------------------------------------------ #
    # 3) 故障处置 runbook
    # ------------------------------------------------------------------ #
    {
        "id": "K-RB-PORT-DOWN",
        "source": "故障 runbook",
        "title": "接入端口 down / shutdown 排查步骤",
        "text": (
            " 现象：单台终端 ping 不通，NC 出现 host_down 或接口 down 告警。步骤：1) 在接入交换机 show interfaces status 看端口"
            "是否 notconnect/down/err-disabled；2) show running-config interface Fa0/x 看是否 shutdown；3) 检查终端网线/网卡；"
            "4) 若是 err-disabled，看 show interfaces status err-disabled 与日志（BPDU guard/port security）；"
            "5) 恢复：interface Fa0/x / no shutdown / switchport access vlan 20；6) 在核心 ping 终端 IP 验证，"
            "NC 告警约 1-2 个采集周期后自动恢复。"
        ),
    },
    {
        "id": "K-RB-LINK-DOWN",
        "source": "故障 runbook",
        "title": "上联链路中断（link_down）定位与恢复",
        "text": (
            " 现象：整栋楼终端离线、核心/接入设备管理仍可能在线，NC 拓扑链路 link_status=down 或整段不可达。"
            "步骤：1) 在断点两侧 show interfaces <端口> 看 up/down、CRC、err-disabled；2) show cdp neighbors detail 确认邻居；"
            "3) 检查 trunk 允许 VLAN 列表（show interfaces trunk）与 native VLAN 是否一致；4) 光纤/铜缆与光模块；"
            "5) 恢复后 show interfaces trunk 与 ping 业务网关验证；6) 路径追踪 trace_path 应恢复 complete。"
            "若链路 down 导致下游主机批量 host_down，告警应按 link_down 根因分组并抑制下游成员（展示层）。"
        ),
    },
    {
        "id": "K-RB-ROUTING-MISSING",
        "source": "故障 runbook",
        "title": "跨网段不可达 / 默认路由缺失排查",
        "text": (
            " 现象：同网段可通、跨网段（教学楼 192.168.20.0/24 → 服务器 192.168.50.0/24）不通。步骤："
            "1) 核心 show ip route 检查是否有目标网段直连/OSPF 路由与默认路由；2) show ip protocols 看 OSPF 邻居；"
            "3) 若缺默认路由：ip route 0.0.0.0 0.0.0.0 <下一跳>；4) 检查边界路由器 Router-4331 回程路由与 NAT；"
            "5) 终端 ipconfig 网关是否正确（如 PC-JX1 网关 192.168.20.1）；6) traceroute 确认在哪一跳进入黑洞。"
            "路由缺失时路径追踪可能出现 partial 或 failed，以工具返回断点为准。"
        ),
    },
    {
        "id": "K-RB-VLAN-MISCONFIG",
        "source": "故障 runbook",
        "title": "VLAN 错配（端口 VLAN 错误）排查",
        "text": (
            " 现象：终端拿不到正确网关 / 同楼其他终端正常、单端口异常；NC 可能有 host_down。步骤："
            "1) show interfaces status 与 show running-config interface Fa0/x 看 switchport access vlan；"
            "2) 对照规划：教学楼终端应为 VLAN20、实训楼 VLAN30、宿舍楼 VLAN40；3) 错误端口改回："
            "interface Fa0/x / switchport access vlan 20；4) 检查 trunk 是否放行该 VLAN（show interfaces trunk）；"
            "5) 终端释放/重取 DHCP 或静态网关核对；6) 用核心 ping 终端验证恢复。"
        ),
    },
    {
        "id": "K-RB-BROADCAST-STORM",
        "source": "故障 runbook",
        "title": "广播风暴 / 环路排查（CPU 飙升、全网卡顿）",
        "text": (
            " 现象：多台设备同时卡顿、CPU 利用率飙升、端口广播报文激增、丢包。步骤：1) 核心与接入 show interfaces"
            " 看 broadcast 速率与 output drops；2) show spanning-tree 检查 STP 根桥/阻塞端口是否异常、是否存在环路；"
            "3) 查看 MAC 表震荡（show mac address-table count / flapping）；4) 临时 shutdown 可疑接入端口逐段隔离；"
            "5) 确认 access 端口启用 portfast/bpduguard 防误接交换机；6) 恢复后观察 CPU 与广播速率回落，"
            "补充 STP 根桥规划。"
        ),
    },
    # ------------------------------------------------------------------ #
    # 4) 项目关键约束
    # ------------------------------------------------------------------ #
    {
        "id": "K-CONSTRAINT-NC-LAG",
        "source": "项目约束",
        "title": "NC 告警为什么清除慢：告警滞后与时间基准",
        "text": (
            " NC（Network Controller）告警清除有采集/判定滞后：NC pingStatus 恢复后，Connectivity/Device is down 问题"
            "可能仍保留 1-2 个采集周期，属于 stale_source（数据源滞后），并非链路仍断。后端告警状态可能与 NC 问题列表"
            "不同步：host_down 告警在 ping 恢复后自动 cleared；已恢复告警保留用于审计，不会自动删除。"
            "时间基准：后端/fixture 时间基于 2026-10-30（UTC+8），NC 事件时间可能为 UTC，比对时注意时区；"
            "回答引用告警时间时应说明是采集时间或事件时间，不要臆测。排查“清除慢”先看 get_hosts 的 pingStatus、"
            "告警 cleared 状态与采集批次，再判断是否需要手动清理。"
        ),
    },
    {
        "id": "K-CONSTRAINT-PATH-TRACE",
        "source": "项目约束",
        "title": "路径追踪（trace_path）能力边界与限制",
        "text": (
            " 路径追踪依赖后端 POST /api/path-trace 与 PT Network Controller 的 flow-analysis：需要 DRIVER=netcon 且 NC 在线；"
            "simulator 驱动或 NC 离线时返回 ok=false（中文 error），不会伪造 hop。后端有约 2 分钟缓存；"
            "trace_status 为 complete/partial/failed，断点信息在 error/links 中；hop 的 ingress_port 可能为 UNKNOWN（NC 未采集），"
            "属正常现象。Agent 只能使用返回的 hop/link/error，不得编造设备或端口；无结果时建议 traceroute 与 show ip route 交叉验证。"
        ),
    },
    {
        "id": "K-CONSTRAINT-ALERT-GROUPS",
        "source": "项目约束",
        "title": "告警根因分组与上游抑制（展示层抑制）",
        "text": (
            " GET /api/alerts/groups 按根因聚合：根因优先级 link_down > device_unreachable > device_offline > host_down；"
            "根因为链路/设备级时，下游主机/设备告警标记 suppressed_by（仅展示层抑制），被抑制告警仍保留 active 状态、"
            "不会删除；处理根因（如恢复链路）后整组告警随之恢复。回答“多少被抑制”应引用接口 member_count/"
            "suppressed_count，不得自行推算或编造组 id。"
        ),
    },
    {
        "id": "K-CONSTRAINT-ACTION",
        "source": "项目约束",
        "title": "写操作确认协议与编排引擎约束",
        "text": (
            " Agent 只读工具绝不直接写数据；清空告警/删除告警/静默/新增或移除场景设备/删会话等写操作只生成 pending action，"
            "经用户确认后由 ActionExecutor 带 X-Operator: agent 调后端并写审计，action 默认 10 分钟过期。"
            "LangGraph 引擎（AGENT_ENGINE=langgraph）在 wait_confirm 节点用 interrupt() 暂停图，用户确认/取消后恢复；"
            "classic 引擎行为等价（W10 Orchestrator）。active 告警不能删除只能静默；chat 阶段绝不自动执行写操作。"
            "时间/数据以工具实时返回为准，评测 fixture 仅用于离线回归。"
        ),
    },
    {
        "id": "K-CONSTRAINT-METRICS",
        "source": "项目约束",
        "title": "指标与 fixture 数据口径（simulator 来源）",
        "text": (
            " 后端 get_metrics 的设备指标是行为仿真（source=simulator），不是 PT 真实接口计数器；回答需注明来源，"
            "不能把仿真值说成实测。评测使用 fixture 合成快照（结构对齐后端 API，故障/W9/W10 状态确定性注入），"
            "不等于实时抓包；真实问答应依赖实时工具数据。健康度/设备状态以 /api/network-health 与 /api/devices 为准，"
            "fixture 与真实后端可能不同。"
        ),
    },
    {
        "id": "K-CONSTRAINT-SSE",
        "source": "项目约束",
        "title": "SSE 流式接口契约（/api/chat/stream）",
        "text": (
            " POST /api/chat/stream 返回 text/event-stream，每行 data: {json}；事件类型：meta(conversation_id/provider/model/engine)、"
            "status(stage classify|retrieve|tool|aggregate)、tool(name/ok/elapsed_ms)、token(text 增量)、"
            "action(pending action 对象)、done(answer/trace/elapsed_ms/conversation_id)、error(message)。"
            "非流式 POST /api/chat 保留；客户端断开时应停止推送并释放资源；流式失败只发 error 事件，不影响非流式。"
            "mock 模式按 10-30 字切片并 sleep 10-30ms；真实 LLM 按 token 增量推送。"
        ),
    },
    # ------------------------------------------------------------------ #
    # 5) 回答规范（防止无据编造）
    # ------------------------------------------------------------------ #
    {
        "id": "K-ANSWER-NO-FABRICATION",
        "source": "项目约束",
        "title": "RAG 引用与无命中时的回答规范",
        "text": (
            " 使用内置知识库时：只允许引用检索结果中的原文事实，引用标记为 [K1]/[K2]...（按命中顺序），"
            "不要编造知识库之外的配置、参数或文档；若检索结果为空或与问题无关，必须明确回答"
            "“知识库无相关内容”，并说明可以基于实时工具数据排查，不得用想象补全。"
            "实时工具数据与知识库冲突时，以实时工具数据为准并说明差异。"
        ),
    },
]


def chunk_count() -> int:
    """知识库分块数（健康检查/测试用）。"""
    return len(KNOWLEDGE_CHUNKS)


def get_chunks() -> List[Dict[str, Any]]:
    """返回知识库副本（避免调用方修改内置数据）。"""
    return [dict(chunk) for chunk in KNOWLEDGE_CHUNKS]
