# -*- coding: utf-8 -*-
"""W12.1 规则优先层：写操作/明确意图在 LLM 之前确定性短路路由。

背景：真实 LLM 下“把已恢复的告警清空”意图路由不稳定，偶尔不生成危险 action 卡片。
本模块提供**纯规则、确定性、零网络**的路由层：

- 先匹配显式写操作 / onboarding / path / alert_groups / notify / recon 规则；
- 命中则直接给出 plan，trace 标 ``route_source=rule``，不进入 LLM 路由；
- 未命中才回退到原有启发式/LLM 路由，trace 标 ``route_source=llm``；
- 规则带反例保护：解释型问题（“删除告警后为什么还在”“谁删了告警？看审计”）不会被吞成写操作。

优先级（先命中先短路，plan 按固定优先级合并）：
1. 清空/删除/清理/移除 + 告警 → steward（action_hint=clear_cleared_alerts）
2. 静默/忽略/屏蔽 + 告警 → steward（action_hint=silence_alert）
3. 删除/清空 + 对话/会话/历史记录 → steward（delete_conversation/clear_conversations）
4. 路径/怎么走/断点 + 两个端点 → path_trace
5. 告警根因/分组/抑制 → alert_groups
6. 通知/Webhook → notify
7. 新设备/发现设备/加到场景 → recon
8. 加设备/新增/接入 + 设备 + 配置/怎么加 → onboarding
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: campus-twin 项目根（routing.py -> agent/app/agent -> agent/app -> agent -> campus-twin）
PROJECT_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------- #
# 词表
# --------------------------------------------------------------------------- #
_WRITE_VERBS = ("清空", "删除", "删掉", "清理", "移除", "清除", "清掉", "清除掉",
                "clear", "delete", "remove", "clean")
_ALERT_OBJECTS = ("告警", "故障记录", "健康问题", "alert")
_CONVERSATION_OBJECTS = ("对话", "会话", "聊天记录", "聊天历史", "历史记录",
                         "conversation", "history", "chat")
_SILENCE_VERBS = ("静默", "忽略", "屏蔽", "忽略掉", "silence", "mute", "ignore")
_EXPLANATORY_MARKERS = (
    "为什么", "为何", "为啥", "原因", "怎么还", "还在", "是否", "会不会",
    "怎么解决", "如何解决", "审计", "谁删", "谁清", "谁改", "操作记录",
    "查看", "查询", "看看", "记录里", "怎么办",
)
_PATH_WORDS = ("路径", "怎么走", "经过", "trace", "path", "哪条链路",
               "通路上", "哪里断", "断点", "哪一跳", "哪一段", "路由路径")
_ACCESS_FAIL_WORDS = ("访问不通", "访问不了", "通不通", "怎么访问", "为什么访问")
_ALERT_GROUP_WORDS = ("根因", "分组", "聚合", "抑制", "告警组", "根因分析")
_ALERT_GROUP_QUERY_WORDS = ("有哪些", "哪些", "多少", "几条", "概况", "情况", "状态", "现在", "当前")
_NOTIFY_WORDS = ("通知", "webhook", "钉钉", "飞书", "企微", "告警推送", "告警通知")
_RECON_WORDS = ("新设备", "发现新设备", "发现设备", "新接入", "新加的", "新上线",
                "待确认的新设备", "pending", "加到场景", "加入场景", "加进场景")
_ONBOARDING_STRONG = ("加设备", "新增设备", "接入一台", "接入交换机", "加一台", "新增一台",
                      "再加一台", "再接一台", "想加", "帮我加", "加个", "放一台", "放个",
                      "部署一台", "新装", "新接入", "新建一栋", "建一栋", "新建楼")
_DEVICE_WORDS = ("交换机", "路由器", "服务器", "2960", "3650", "4331", "设备", "pc", "电脑")
_ONBOARDING_HOW_WORDS = ("怎么", "如何", "方案", "步骤", "配置", "教程", "接线", "部署", "怎么加")
_DIAGNOSIS_HINTS = ("离线", "掉线", "故障", "不可达", "异常", "ping", "断了", "中断",
                    "为什么", "宕", "卡", "丢包")
_ENDPOINT_RE = re.compile(
    r"(?:\d{1,3}\.){3}\d{1,3}|pc-[a-z0-9-]+|server-\d+|sw-[a-z0-9-]+|core-\d+|router-\d+",
    re.IGNORECASE,
)
#: W13：MCP filesystem 文件读取（configs/docs 下路径）
_MCP_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/])?(?:configs|docs)[\\/][^\s，。；;\"'<>|]+", re.IGNORECASE
)
_MCP_READ_MARKERS = ("读", "读取", "看一下", "看下", "内容", "vlan", "配置", "文件", "有什么")


@dataclass
class RouteDecision:
    """规则/回退路由结果（可安全写入 trace，不含敏感信息）。"""

    intent: str
    source: str = "llm"  # rule | llm
    rule: Optional[str] = None
    confidence: float = 0.5
    reason: str = ""
    matched: List[str] = field(default_factory=list)
    plan: List[Tuple[str, str]] = field(default_factory=list)
    action_hint: Optional[str] = None

    def to_trace(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "intent": self.intent,
            "rule": self.rule,
            "confidence": self.confidence,
            "reason": self.reason,
            "matched": list(self.matched),
            "action_hint": self.action_hint,
            "plan": [{"role": role, "objective": objective} for role, objective in self.plan],
        }


def _contains_any(text: str, words: Tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _has_explanatory_marker(text: str) -> bool:
    return any(marker in text for marker in _EXPLANATORY_MARKERS)


def _endpoints(text: str) -> List[str]:
    return sorted(set(match.group(0).lower() for match in _ENDPOINT_RE.finditer(text or "")))


def extract_mcp_path(question: str) -> Optional[str]:
    """从问题中提取 MCP filesystem 路径（configs/docs 下），供 mcp_read 节点使用。

    相对路径（如 ``configs/core-3650.txt``）会解析为 campus-twin 项目根下的绝对路径，
    以匹配 MCP filesystem server 的 allowed directories。
    """
    match = _MCP_PATH_RE.search(question or "")
    if not match:
        return None
    raw = match.group(0).replace("/", "\\")
    if re.match(r"^[A-Za-z]:", raw):
        return raw
    return str((PROJECT_ROOT / raw).resolve())


def _match_write_alerts(text: str) -> Optional[Tuple[str, str, str]]:
    """返回 (intent, rule, action_hint) 或 None。"""
    if not (_contains_any(text, _WRITE_VERBS) and _contains_any(text, _ALERT_OBJECTS)):
        return None
    if _has_explanatory_marker(text):
        return None
    return "steward", "write_clear_alerts", "clear_cleared_alerts"


def _match_silence_alerts(text: str) -> Optional[Tuple[str, str, str]]:
    if not (_contains_any(text, _SILENCE_VERBS) and _contains_any(text, _ALERT_OBJECTS)):
        return None
    if _has_explanatory_marker(text):
        return None
    return "steward", "write_silence_alerts", "silence_alert"


def _match_write_conversations(text: str) -> Optional[Tuple[str, str, str]]:
    if not (_contains_any(text, _WRITE_VERBS) and _contains_any(text, _CONVERSATION_OBJECTS)):
        return None
    if _has_explanatory_marker(text):
        return None
    # “删除/移除 + 会话” → delete_conversation；“清空/清理 + 所有/历史” → clear_conversations
    delete_style = any(word in text for word in ("删除", "删掉", "移除", "delete", "remove"))
    clear_style = any(word in text for word in ("清空", "清理", "清除", "所有", "全部", "历史",
                                                "clear", "clean"))
    hint = "delete_conversation" if delete_style and not clear_style else "clear_conversations"
    return "steward", "write_conversations", hint


def _match_path(text: str) -> Optional[Tuple[str, str, str]]:
    if _contains_any(text, _PATH_WORDS):
        return "path_trace", "path_explicit", None
    endpoints = _endpoints(text)
    if len(endpoints) >= 2 and _contains_any(text, _ACCESS_FAIL_WORDS):
        return "path_trace", "path_endpoints", None
    return None


def _match_alert_groups(text: str) -> Optional[Tuple[str, str, str]]:
    if _contains_any(text, _ALERT_GROUP_WORDS):
        return "alert_groups", "alert_groups_keyword", None
    if _contains_any(text, _NOTIFY_WORDS):
        return None
    if _contains_any(text, ("告警", "故障记录", "健康问题")) and _contains_any(
        text, _ALERT_GROUP_QUERY_WORDS
    ):
        return "alert_groups", "alert_groups_query", None
    return None


def _match_notify(text: str) -> Optional[Tuple[str, str, str]]:
    if _contains_any(text, _NOTIFY_WORDS):
        return "notify", "notify_keyword", None
    return None


def _match_recon(text: str) -> Optional[Tuple[str, str, str]]:
    if _contains_any(text, _RECON_WORDS):
        return "recon", "recon_keyword", None
    if "发现" in text and "设备" in text:
        return "recon", "recon_discover", None
    return None


def _match_onboarding(text: str) -> Optional[Tuple[str, str, str]]:
    if not _contains_any(text, _DEVICE_WORDS):
        return None
    # “发现新设备/加到场景”属于 recon，不是加设备
    if _contains_any(text, ("加到场景", "加入场景", "加进场景", "发现新设备", "待确认的新设备")):
        return None
    # 故障描述优先诊断，不能当“加设备”
    if _contains_any(text, _DIAGNOSIS_HINTS) and not any(
        word in text for word in ("怎么加", "如何加", "方案", "配置", "接线")
    ):
        return None
    if _contains_any(text, _ONBOARDING_STRONG):
        return "onboarding", "onboarding_device", None
    if "接入" in text and _contains_any(text, _ONBOARDING_HOW_WORDS):
        return "onboarding", "onboarding_how", None
    if _contains_any(text, ("加", "新增", "新装", "装一台")) and _contains_any(
        text, ("交换机", "路由器", "2960", "3650", "4331", "服务器")
    ):
        return "onboarding", "onboarding_add", None
    return None


def _match_mcp_read(text: str) -> Optional[Tuple[str, str, str]]:
    match = _MCP_PATH_RE.search(text)
    if not match:
        return None
    if not any(marker in text for marker in _MCP_READ_MARKERS):
        return None
    return "mcp_read", "mcp_read_file", None


def _match_mcp_tools(text: str) -> Optional[Tuple[str, str, str]]:
    if "mcp" not in text:
        return None
    if any(word in text for word in ("工具", "tool", "接入", "server", "服务", "能力")):
        return "mcp_tools", "mcp_list_tools", None
    return None


#: 规则匹配器（顺序即优先级；命中后合并 plan 仍按固定优先级）
_MATCHERS = (
    ("mcp_read", _match_mcp_read),
    ("mcp_tools", _match_mcp_tools),
    ("write_clear_alerts", _match_write_alerts),
    ("write_silence_alerts", _match_silence_alerts),
    ("write_conversations", _match_write_conversations),
    ("path", _match_path),
    ("alert_groups", _match_alert_groups),
    ("notify", _match_notify),
    ("recon", _match_recon),
    ("onboarding", _match_onboarding),
)

#: plan 固定优先级（与 classic plan 的既有顺序一致：steward → path → alert_groups → notify → recon → onboarding）
_PLAN_PRIORITY = (
    "mcp_read",
    "mcp_tools",
    "steward",
    "path_trace",
    "alert_groups",
    "notify",
    "recon",
    "onboarding",
    "scene",
)

#: 对外暴露的规则名（/api/roles、README、测试用）
ROUTE_RULE_NAMES: Tuple[str, ...] = tuple(name for name, _ in _MATCHERS)

_OBJECTIVES = {
    "mcp_read": "通过 MCP filesystem 读取 configs/docs 文件（只读）",
    "mcp_tools": "列出外部 MCP servers 接入的工具（只读）",
    "steward": "处理数据管理请求（告警 / 会话 / 审计）",
    "path_trace": "追踪源/目的主机之间的网络路径",
    "alert_groups": "查询告警根因分组与上游抑制情况",
    "notify": "查询告警通知（Webhook）状态",
    "recon": "侦察新设备并推断归属（可加入场景）",
    "onboarding": "生成设备接入方案与场景操作",
    "scene": "生成 3D 场景布局建议",
}


def route_question(question: str) -> RouteDecision:
    """规则优先路由：命中返回 source=rule；未命中返回 source=llm（空 plan，由调用方回退）。"""
    text = (question or "").strip().lower()
    if not text:
        return RouteDecision(intent="", source="llm", reason="empty question")
    matched: List[Tuple[str, str, str]] = []
    for rule_name, matcher in _MATCHERS:
        result = matcher(text)
        if result is not None:
            matched.append((rule_name, result[0], result[2] or ""))
    if not matched:
        return RouteDecision(intent="", source="llm", reason="no explicit rule matched")
    roles: List[str] = []
    action_hint: Optional[str] = None
    for rule_name, role, hint in matched:
        if role not in roles:
            roles.append(role)
        if hint and action_hint is None:
            action_hint = hint
    roles.sort(key=lambda role: _PLAN_PRIORITY.index(role) if role in _PLAN_PRIORITY else 99)
    plan = [(role, _OBJECTIVES.get(role, role)) for role in roles[:3]]
    return RouteDecision(
        intent=roles[0],
        source="rule",
        rule="+".join(name for name, _, _ in matched),
        confidence=1.0,
        reason="explicit rule matched: %s" % ", ".join(name for name, _, _ in matched),
        matched=[name for name, _, _ in matched],
        plan=plan,
        action_hint=action_hint,
    )
