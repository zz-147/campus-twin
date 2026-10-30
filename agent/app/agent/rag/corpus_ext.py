# -*- coding: utf-8 -*-
"""W15 RAG 扩展语料（100+ chunks）。

本模块只提供 **原始文档 + 章节**（SOURCE DOCUMENTS）；``corpus.py`` 用章节 chunking
把它们展开成带 id/title/source/section/text/length 的 chunk，保证：
- 原有 18 个核心 chunk 的 id/title/text **完全不变**（search_knowledge/评测兼容）；
- 新增内容覆盖：设备配置模板、IP/VLAN/拓扑规划、9 类故障 runbook、项目约束、
  LangGraph/SSE/RAG/MCP/评测/前端/部署等，共 100+ 块。

source 仍归一到原有 4 类（``设备配置模板 / 校园网络拓扑设计 / 故障 runbook / 项目约束``），
便于保持既有测试与 citation 语义一致；具体主题写在 title/section 里。
"""
from __future__ import annotations

from typing import Any, Dict, List


def _sec(section: str, text: str) -> Dict[str, str]:
    return {"section": section, "text": text}


EXTRA_DOCS: List[Dict[str, Any]] = [
    # ================= 设备配置模板：3650 =================
    {
        "doc_id": "CFG-CORE3650",
        "title": "Core-3650 核心交换机配置手册",
        "source": "设备配置模板",
        "sections": [
            _sec("VLAN 规划", "Core-3650 上创建 VLAN 10 MGMT、VLAN 20 TEACHING、VLAN 30 TRAINING、VLAN 40 DORM、VLAN 50 SERVER；VLAN 名称与用途需与校园网规划一致，新增业务 VLAN 先规划再创建。"),
            _sec("下联 Trunk", "下联接入交换机端口：Gi1/0/1→SW-JX-2960 Fa0/24、Gi1/0/2→SW-SX-2960 Fa0/24、Gi1/0/3→SW-SS-2960 Fa0/24，均为 trunk，allowed vlan 10+对应业务 VLAN，native VLAN 保持一致。"),
            _sec("SVI 网关", "各 VLAN SVI 地址：VLAN10 192.168.10.1/24、VLAN20 192.168.20.1/24、VLAN30 192.168.30.1/24、VLAN40 192.168.40.1/24、VLAN50 192.168.50.1/24；终端网关统一指向核心 SVI。"),
            _sec("三层路由", "启用 ip routing；OSPF 进程 network 192.168.0.0 0.0.255.255 area 0；到边界路由器可配默认路由或静态回程；show ip route 检查直连/OSPF/默认路由。"),
            _sec("上联与边界", "Core Gi1/0/6 上联 Router-2911/4331（10.0.0.0/30 路由口）；跨网段访问服务器 VLAN50 经核心三层转发，不走二层。"),
            _sec("验证命令", "show vlan brief、show interfaces trunk、show ip interface brief、show ip route、show ip ospf neighbor、ping 192.168.20.1/192.168.50.10。"),
        ],
    },
    # ================= 设备配置模板：2960 接入 =================
    {
        "doc_id": "CFG-SWJX2960",
        "title": "SW-JX-2960 教学楼接入交换机配置",
        "source": "设备配置模板",
        "sections": [
            _sec("上联配置", "SW-JX-2960 Fa0/24 上联 Core Gi1/0/1：switchport mode trunk、switchport trunk allowed vlan 10,20、no shutdown；trunk 两端 allowed VLAN 必须一致。"),
            _sec("管理 SVI", "VLAN10 管理地址 192.168.10.11/24，网关 192.168.10.1；ip default-gateway 192.168.10.1；管理 VLAN 不能随意改。"),
            _sec("接入端口", "Fa0/1→PC-JX1、Fa0/2→PC-JX2，均 switchport mode access、switchport access vlan 20、spanning-tree portfast；接入端口不要接交换机以防环路。"),
            _sec("验证命令", "show interfaces status、show interfaces trunk、show running-config interface Fa0/24、ping 192.168.10.1、show cdp neighbors。"),
        ],
    },
    {
        "doc_id": "CFG-SWSX2960",
        "title": "SW-SX-2960 实训楼接入交换机配置",
        "source": "设备配置模板",
        "sections": [
            _sec("上联与地址", "SW-SX-2960 Fa0/24 上联 Core Gi1/0/2，trunk allowed vlan 10,30；管理 SVI Vlan10 192.168.10.12/24，网关 192.168.10.1。"),
            _sec("接入端口", "Fa0/1 接 PC-SX1，access VLAN30；端口启用 portfast；VLAN 30 与实训楼网段 192.168.30.0/24 对应。"),
        ],
    },
    {
        "doc_id": "CFG-SWSS2960",
        "title": "SW-SS-2960 宿舍楼接入交换机配置",
        "source": "设备配置模板",
        "sections": [
            _sec("上联与地址", "SW-SS-2960 Fa0/24 上联 Core Gi1/0/3，trunk allowed vlan 10,40；管理 SVI Vlan10 192.168.10.13/24，网关 192.168.10.1。"),
            _sec("接入端口", "Fa0/1→PC-SS1、Fa0/2→PC-SS2，access VLAN40；宿舍楼终端网段 192.168.40.0/24。"),
        ],
    },
    # ================= 设备配置模板：路由器 =================
    {
        "doc_id": "CFG-ROUTER4331",
        "title": "Router-4331 边界路由器配置手册",
        "source": "设备配置模板",
        "sections": [
            _sec("接口地址", "Router-4331 Gi0/0 与 Core 上联口互联（10.0.0.0/30），Gi0/1 接运营商/出口；接口地址与掩码按规划配置。"),
            _sec("静态路由", "内网回程：ip route 192.168.10.0 255.255.255.0 10.0.0.1；业务网段同理指向核心；默认路由 ip route 0.0.0.0 0.0.0.0 运营商网关。"),
            _sec("NAT 与出口", "ip nat inside/outside、access-list permit 192.168.0.0 0.0.255.255、ip nat inside source list 1 interface Gi0/1 overload；show ip nat translations 验证。"),
            _sec("验证命令", "show ip interface brief、show ip route、show ip nat translations、ping 10.0.0.1、traceroute 8.8.8.8。"),
        ],
    },
    # ================= 设备配置模板：2960 通用 =================
    {
        "doc_id": "CFG-2960-GENERIC",
        "title": "2960 交换机通用配置与端口操作",
        "source": "设备配置模板",
        "sections": [
            _sec("基础配置", "enable / configure terminal / hostname / enable secret / username admin privilege 15 secret；管理 VLAN SVI 配 IP 与默认网关；关闭未使用端口或划入隔离 VLAN。"),
            _sec("VLAN 与 Trunk", "vlan <id> / name <NAME>；上联：interface Fa0/24、switchport mode trunk、switchport trunk allowed vlan 10,20；show interfaces trunk 检查放行列表。"),
            _sec("接入端口", "interface Fa0/x、switchport mode access、switchport access vlan 20、spanning-tree portfast、no shutdown；show interfaces status 查看 connected/notconnect。"),
            _sec("故障恢复", "shutdown 端口恢复：interface Fa0/x、no shutdown；err-disabled 先看 show interfaces status err-disabled 与日志，再 shutdown/no shutdown 或按原因处理。"),
        ],
    },
    # ================= IP / VLAN / 拓扑 =================
    {
        "doc_id": "TOPO-DEVICES-DETAIL",
        "title": "校园网设备清单与角色",
        "source": "校园网络拓扑设计",
        "sections": [
            _sec("核心设备", "Core-3650（3650-24PS，IOS-XE 16.3.2）管理 192.168.10.1，中心机房；承担 VLAN 网关、三层转发、OSPF。"),
            _sec("接入设备", "SW-JX-2960 教学楼 192.168.10.11、SW-SX-2960 实训楼 192.168.10.12、SW-SS-2960 宿舍楼 192.168.10.13；均为 Managed。"),
            _sec("边界与 NC", "Router-4331（Lo0 192.168.10.254）边界出口；Network Controller 192.168.10.100 接 Core Gi1/0/4 access VLAN10。"),
            _sec("终端与服务器", "PC-JX1/PC-JX2 192.168.20.10/11、PC-SX1 192.168.30.10、PC-SS1/PC-SS2 192.168.40.10/11、Server-1 192.168.50.10。"),
        ],
    },
    {
        "doc_id": "TOPO-PORTMAP",
        "title": "核心与接入端口映射表",
        "source": "校园网络拓扑设计",
        "sections": [
            _sec("核心下行", "Core Gi1/0/1↔SW-JX Fa0/24(trunk 10,20)、Gi1/0/2↔SW-SX Fa0/24(trunk 10,30)、Gi1/0/3↔SW-SS Fa0/24(trunk 10,40)。"),
            _sec("核心其他端口", "Core Gi1/0/4↔NC access VLAN10；Gi1/0/5↔Server-1 access VLAN50；Gi1/0/6↔边界路由器 routed 10.0.0.0/30。"),
        ],
    },
    {
        "doc_id": "VLAN-PLAN",
        "title": "VLAN 10-50 规划明细",
        "source": "校园网络拓扑设计",
        "sections": [
            _sec("VLAN10 管理", "VLAN10 MGMT：192.168.10.0/24，网关 192.168.10.1，设备管理 + NC；新接入设备管理 IP 从该网段分配。"),
            _sec("VLAN20 教学", "VLAN20 TEACHING：192.168.20.0/24，网关 192.168.20.1，教学楼终端（PC-JX1/2）。"),
            _sec("VLAN30 实训", "VLAN30 TRAINING：192.168.30.0/24，网关 192.168.30.1，实训楼终端（PC-SX1）。"),
            _sec("VLAN40 宿舍", "VLAN40 DORM：192.168.40.0/24，网关 192.168.40.1，宿舍楼终端（PC-SS1/2）。"),
            _sec("VLAN50 服务器", "VLAN50 SERVER：192.168.50.0/24，网关 192.168.50.1，Server-1 服务器。"),
        ],
    },
    {
        "doc_id": "NC-MANAGE",
        "title": "Network Controller 纳管与发现",
        "source": "校园网络拓扑设计",
        "sections": [
            _sec("纳管要求", "NC 自身需配置默认网关 192.168.10.1 才能跨网段纳管；同网段设备无需网关；NC 拓扑只包含已纳管网元。"),
            _sec("发现限制", "终端发现依赖网络设备侧信息（MAC/邻居）；核心纳管后 6 台终端可自动发现；邻居缺失先查接口 VLAN/物理连线。"),
        ],
    },
    # ================= 故障 runbook（9 类 × 2） =================
    {
        "doc_id": "RB-HOST-DOWN",
        "title": "主机离线（host_down）处置 runbook",
        "source": "故障 runbook",
        "sections": [
            _sec("现象与定位", "单台终端 ping 不通，NC 出现 host_down/终端离线告警；先 get_hosts 看 ping_status，再查接入端口与终端网卡/网线。"),
            _sec("恢复与验证", "interface Fa0/x 检查 shutdown/err-disabled；no shutdown 或改回 VLAN；核心 ping 终端 IP，NC 告警 1-2 个采集周期自动恢复。"),
        ],
    },
    {
        "doc_id": "RB-DEVICE-UNREACHABLE",
        "title": "设备不可达（device_unreachable）处置",
        "source": "故障 runbook",
        "sections": [
            _sec("定位步骤", "确认设备管理 IP、NC 采集状态、上联链路与 VLAN；show interfaces status、ping 管理 IP、检查 ACL/网关；区分设备宕机与采集异常。"),
            _sec("恢复验证", "设备恢复后 NC reachability=Reachable、collectionStatus=Managed；健康度回升；告警按恢复事件 cleared。"),
        ],
    },
    {
        "doc_id": "RB-PORT-DOWN",
        "title": "端口 down/shutdown 处置",
        "source": "故障 runbook",
        "sections": [
            _sec("排查", "show interfaces status 看 notconnect/down/err-disabled；show running-config interface Fa0/x 看 shutdown；检查网线/光模块/BPDU guard。"),
            _sec("恢复", "interface Fa0/x / no shutdown；恢复后 show interfaces status、ping 终端；NC 告警自动 cleared。"),
        ],
    },
    {
        "doc_id": "RB-LINK-DOWN",
        "title": "链路中断（link_down）处置",
        "source": "故障 runbook",
        "sections": [
            _sec("定位", "整栋楼/整段离线，show interfaces 看端口 up/down、CRC、err；show cdp neighbors 确认邻居；show interfaces trunk 检查 allowed VLAN/native。"),
            _sec("恢复", "修复光纤/铜缆/模块或配置；trunk 恢复后 ping 业务网关；trace_path 应从 partial 恢复 complete；下游 host_down 按根因抑制展示。"),
        ],
    },
    {
        "doc_id": "RB-ROUTING-MISSING",
        "title": "跨网段路由缺失处置",
        "source": "故障 runbook",
        "sections": [
            _sec("定位", "同网段通、跨网段不通：核心 show ip route 看目标网段与默认路由；show ip protocols 看 OSPF 邻居；边界路由器回程路由。"),
            _sec("恢复", "补默认/静态/OSPF 路由；终端 ipconfig 网关核对；traceroute 看在哪一跳丢包；路径追踪可能 partial/failed，以断点为准。"),
        ],
    },
    {
        "doc_id": "RB-VLAN-MISCONFIG",
        "title": "VLAN 错配处置",
        "source": "故障 runbook",
        "sections": [
            _sec("定位", "单端口异常、同楼其他终端正常；show interfaces status 与 running-config 看 access vlan；对照规划教学楼20/实训30/宿舍40。"),
            _sec("恢复", "interface Fa0/x / switchport access vlan 20；检查 trunk 是否放行；终端重取 DHCP/核对网关；核心 ping 验证。"),
        ],
    },
    {
        "doc_id": "RB-BROADCAST-STORM",
        "title": "广播风暴/环路处置",
        "source": "故障 runbook",
        "sections": [
            _sec("定位", "多设备卡顿、CPU 飙升、广播速率异常、丢包；show interfaces 看 broadcast/input errors；show spanning-tree 看根桥/阻塞端口；MAC 表震荡。"),
            _sec("恢复", "逐段 shutdown 可疑接入端口隔离环路；access 端口启用 portfast/bpduguard；恢复后观察 CPU/广播回落并补 STP 根桥规划。"),
        ],
    },
    {
        "doc_id": "RB-HIGH-CPU",
        "title": "设备高 CPU 处置",
        "source": "故障 runbook",
        "sections": [
            _sec("定位", "show processes cpu sorted 看 top 进程；检查广播风暴/路由震荡/ACL 命中；对比指标曲线确定突发还是持续。"),
            _sec("恢复", "抑制风暴源/优化 ACL/升级 IOS/调整采样；持续高 CPU 需厂商支持；指标为仿真来源时回答标注 source=simulator。"),
        ],
    },
    {
        "doc_id": "RB-BANDWIDTH",
        "title": "带宽拥塞处置",
        "source": "故障 runbook",
        "sections": [
            _sec("定位", "上联端口利用率持续 >80%、丢包/延迟上升；show interfaces 看 output drops/速率；定位大流量主机或链路瓶颈。"),
            _sec("恢复", "QoS/限速/扩容上联/分流；观察 if_utilization_pct 指标回落；记录峰值与时间段，避免把仿真指标当实测。"),
        ],
    },
    # ================= 项目约束 =================
    {
        "doc_id": "CONSTRAINT-NC",
        "title": "NC 告警滞后与恢复判定设计",
        "source": "项目约束",
        "sections": [
            _sec("滞后原因", "NC pingStatus 恢复后，Connectivity/Device is down 问题可能仍保留 1-2 个采集周期，属于 stale_source（数据源滞后），不是链路仍断。"),
            _sec("自建恢复判定", "后端按数据源实际状态（ping/可达性）判定 host_down/device_unreachable 恢复并 cleared；NC issue 状态单独保留；>3 分钟滞后在 UI/回答中说明。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-TIME",
        "title": "双时间基准 observed_at / source_ts",
        "source": "项目约束",
        "sections": [
            _sec("双时间定义", "observed_at 是后端采集观测时间；source_ts 是 NC 事件时间；NC 仿真时钟与本地约差 10 分钟；回答必须说明引用的是哪个时间。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-ALERT-LIFECYCLE",
        "title": "告警生命周期与状态机",
        "source": "项目约束",
        "sections": [
            _sec("状态", "告警状态 open→cleared/ silenced；cleared 保留历史用于审计；deleted_at 软删除；同一 dedup_key 5 分钟去重。"),
            _sec("恢复事件", "恢复时写 cleared 事件（clear_reason），不直接删除；已恢复告警可按需清空；活跃告警不可删除只能静默。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-ALERT-GROUPS",
        "title": "告警根因分组与抑制规则",
        "source": "项目约束",
        "sections": [
            _sec("根因优先级", "link_down > device_unreachable > device_offline > host_down > health_issue > other；同组按拓扑/设备关联聚合。"),
            _sec("抑制展示", "链路/设备根因的下游告警标记 suppressed_by；仅展示层抑制，被抑制告警仍 active、不删除；处理根因后整组恢复。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-SOFT-DELETE",
        "title": "软删除、审计与撤销",
        "source": "项目约束",
        "sections": [
            _sec("软删除", "删除告警/会话等写操作均为软删除 + 审计记录，可在审计中追溯并支持撤销；不物理删除生产数据。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-ACTION",
        "title": "写操作确认协议（pending action）",
        "source": "项目约束",
        "sections": [
            _sec("确认流程", "Agent 只生成 pending action 卡片，用户确认后由 ActionExecutor 带 X-Operator: agent 调后端；取消/过期不执行。"),
            _sec("安全不变式", "危险操作 danger=true 前端二次确认；active 告警禁删可静默；10 分钟 TTL；CAS 状态机防重复执行；全部写审计。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-SSE",
        "title": "SSE 流式契约",
        "source": "项目约束",
        "sections": [
            _sec("事件模型", "POST /api/chat/stream 返回 text/event-stream：meta/status/tool/token/action/done/error；每行 data: {json}。"),
            _sec("实现与降级", "真实 LLM 用 LangChain model.stream 逐 token；mock 按 10-30 字切片；断开停止推送；流式失败只发 error，不影响非流式 /api/chat。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-RAG",
        "title": "RAG 引用与无命中规范",
        "source": "项目约束",
        "sections": [
            _sec("引用规则", "只允许引用检索结果原文，标记 [K1]..；引用必须能回查 chunk id；实时工具数据与知识库冲突时以实时数据为准。"),
            _sec("无命中", "检索为空或与问句无关时明确回答“知识库无相关内容”，不得编造配置/参数；知识型问句不硬套排障模板。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-LANGGRAPH",
        "title": "LangGraph 编排与中断语义",
        "source": "项目约束",
        "sections": [
            _sec("图结构", "StateGraph + TypedDict 状态；classify→角色节点（Send 并行）→aggregate→wait_confirm→execute_action；条件边路由。"),
            _sec("checkpointer", "SqliteSaver 持久化，thread_id=conversation_id；每个 superstep 存档；snapshot.next 判断暂停；进程重启后可恢复。"),
            _sec("interrupt 陷阱", "interrupt 恢复时节点从头重跑；wait_confirm 只做纯读 + interrupt，真正执行放在 execute_action，避免重复副作用。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-MCP-SERVER",
        "title": "自建 MCP Server（FastMCP/stdio）",
        "source": "项目约束",
        "sections": [
            _sec("能力清单", "10 tools：get_devices/get_topology/get_hosts/get_issues/get_alerts/get_alert_groups/get_notify_status/trace_path/get_scene/get_audit。"),
            _sec("资源与提示", "4 resources：campus://scene、campus://topology、campus://docs/network-design、campus://docs/troubleshooting；3 prompts：troubleshoot_port_down、troubleshoot_link_down、onboard_device。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-MCP-BRIDGE",
        "title": "MCP Client 桥接与安全边界",
        "source": "项目约束",
        "sections": [
            _sec("架构", "Agent(py3.9) → HTTP:8200 → Bridge(py3.14 + mcp 1.30) → stdio → filesystem/memory servers；外部 23 工具（filesystem14+memory9）。"),
            _sec("安全", "Bridge 只听 127.0.0.1；filesystem 只读 configs/docs（write/edit/move 拒绝）；memory 只写本地；所有调用写日志。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-EVAL",
        "title": "38 场景评测与质量门",
        "source": "项目约束",
        "sections": [
            _sec("场景构成", "38 = 20 排障 + 10 多 Agent + 5 只读(W10) + 3 RAG；覆盖主机离线/设备不可达/链路/端口/路由/VLAN/风暴/CPU/带宽。"),
            _sec("指标", "根因命中、设备定位、证据覆盖、动作正确率、自动执行违规=0；mock 离线可重复；langgraph/classic 双引擎对照。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-FRONTEND",
        "title": "3D 大屏与 AI 面板",
        "source": "项目约束",
        "sections": [
            _sec("前端能力", "Vite+Three.js：4 楼栋、路径追踪发光动画、告警面板（按根因分组/抑制标签）、AI 面板（流式 token/引用/action 卡片/历史会话）；headless Edge 冒烟。"),
        ],
    },
    {
        "doc_id": "CONSTRAINT-DEPLOY",
        "title": "部署边界与生产化差距",
        "source": "项目约束",
        "sections": [
            _sec("当前形态", "Windows 单机演示：后端 8000、Agent 8100、前端 5173、MCP Bridge 8200、MCP Server stdio；离线 mock/fixture 可降级。"),
            _sec("生产化", "需要认证鉴权、密钥管理、HA/多实例、可观测、数据治理、限流和真实设备接入；当前是演示原型，不宣称生产。"),
        ],
    },
    # ================= MCP / 路由专项 =================
    {
        "doc_id": "MCP-PROTOCOL",
        "title": "MCP 协议与 tools/resources/prompts",
        "source": "项目约束",
        "sections": [
            _sec("协议", "Model Context Protocol 是 Host/Client/Server 之间发现和调用能力的标准；JSON-RPC over stdio/HTTP；工具、资源、提示词统一描述。"),
            _sec("与 function calling", "function calling 解决模型↔宿主工具调用；MCP 解决工具/资源跨客户端复用；两者结合：原生工具用 function calling，外部工具走 MCP 桥。"),
        ],
    },
    {
        "doc_id": "MCP-TOOLS-SERVER",
        "title": "MCP Server 工具与资源使用",
        "source": "项目约束",
        "sections": [
            _sec("工具示例", "get_devices 列设备、get_alerts 查告警、trace_path 追踪路径、get_scene 取 3D 场景、get_audit 查审计；全部只读。"),
            _sec("资源示例", "campus://scene 返回场景 JSON、campus://topology 返回拓扑、campus://docs/* 返回项目文档；prompts 用于排障/接入标准提问。"),
        ],
    },
    {
        "doc_id": "ROUTING-RULES",
        "title": "规则优先确定性路由",
        "source": "项目约束",
        "sections": [
            _sec("写操作规则", "清空/删除/清理/移除+告警→steward write_clear_alerts；静默/忽略+告警→silence；删除/清空+会话→conversation；规则在 LLM 前短路。"),
            _sec("反例与 trace", "解释型“删除告警后为什么还在”走诊断；trace 标 route_source=rule|llm、route.rule/action_hint；规则命中不依赖模型随机性。"),
        ],
    },
    {
        "doc_id": "ROUTING-TRUNCATION",
        "title": "W12.2 工具截断与动作回归",
        "source": "项目约束",
        "sections": [
            _sec("问题", "真实库 37 条告警使 get_alerts 返回超 20000 字符被截断，steward 从丢失的 alerts 推导 cleared=0 → 规则命中但无 action。"),
            _sec("修复", "steward 优先用 summary/limit=1 取真实 cleared/active 数量；>0 生成 pending action（confirm_text 带数量），=0 明确无可清空；加 classic/langgraph/SSE 回归测试。"),
        ],
    },
    {
        "doc_id": "EVAL-RAG",
        "title": "RAG 评测方法",
        "source": "项目约束",
        "sections": [
            _sec("评测集", "100+ 文档块、3 种 chunking、40+ query（配置/拓扑/故障/约束/MCP/评测 6 类）；对比 BM25/向量/hybrid/hybrid+rerank。"),
            _sec("指标", "recall@5、MRR、引用准确率（引用指向 gold 占比/可溯源率）、faithfulness（引用 chunk 覆盖 query 关键实体）；报告落盘 rag_report.json。"),
        ],
    },
]
