# -*- coding: utf-8 -*-
"""多 Agent 角色基础设施：角色上下文 / 输出 / 基类。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.agent.conversations import ConversationStore
from app.agent.tools import OpsToolkit


@dataclass
class RoleContext:
    """一次子 Agent 执行所需的上下文（问题 / 会话 / 工具调用记录 / 规则路由结果）。"""

    question: str
    conversation_id: Optional[str] = None
    history: List[Dict[str, str]] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    #: W12.1 规则路由结果（rule=确定性短路；llm=回退路由）；供角色做确定性动作 hint
    route: Optional[Dict[str, Any]] = None

    def call_tool(
        self, toolkit: OpsToolkit, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """调用白名单内的只读工具，并记录工具名（供 trace 使用）。"""
        self.tools_used.append(name)
        return toolkit.call(name, arguments or {})


@dataclass
class ActionDraft:
    """子 Agent 生成的操作草案（由编排层统一落库成带 id 的 pending action）。"""

    type: str
    params: Dict[str, Any] = field(default_factory=dict)
    confirm_text: str = ""
    danger: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "params": self.params,
            "confirm_text": self.confirm_text,
            "danger": self.danger,
        }


@dataclass
class RoleResult:
    """子 Agent 输出：中文答复 + 待确认操作草案 + 结构化数据。"""

    role: str
    label: str
    answer: str
    actions: List[ActionDraft] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    tools_used: List[str] = field(default_factory=list)

    def to_trace(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "label": self.label,
            "ok": self.error is None,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "tools_used": list(self.tools_used),
            "actions": [action.to_dict() for action in self.actions],
        }


class BaseRole:
    """子 Agent 基类：声明工具白名单 + 统一的运行入口。"""

    name: str = "base"
    label: str = "子 Agent"
    allowed_tools: Sequence[str] = ()

    def __init__(
        self,
        toolkit: OpsToolkit,
        conversations: Optional[ConversationStore] = None,
    ) -> None:
        self._full_toolkit = toolkit
        self._toolkit = toolkit.restricted(self.allowed_tools) if self.allowed_tools else toolkit
        self._conversations = conversations

    @property
    def toolkit(self) -> OpsToolkit:
        return self._toolkit

    @property
    def full_toolkit(self) -> OpsToolkit:
        return self._full_toolkit

    def run(self, ctx: RoleContext) -> RoleResult:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 输出与计时辅助
    # ------------------------------------------------------------------ #
    def _start(self) -> float:
        return time.perf_counter()

    def _finish(
        self,
        ctx: RoleContext,
        started: float,
        answer: str,
        actions: Optional[List[ActionDraft]] = None,
        data: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> RoleResult:
        return RoleResult(
            role=self.name,
            label=self.label,
            answer=answer,
            actions=actions or [],
            data=data or {},
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
            error=error,
            tools_used=list(dict.fromkeys(ctx.tools_used)),
        )
