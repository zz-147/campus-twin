# -*- coding: utf-8 -*-
"""LangGraph AgentState：主图状态定义（可被 SqliteSaver 序列化）。

设计要点
========
- 顶层字段满足 W11 契约：question / conversation_id / messages / intent / plan /
  subtask_results / tool_results / answer / actions / errors / steps；
- 并行子任务（LangGraph ``Send`` fan-out）通过 ``turn`` 字段的**递归合并 reducer**
  汇总：每个分支只写自己的 role key，互不覆盖；
- 每轮新输入用 ``turn={"__reset__": True}`` 重置本轮状态，保证同一 thread_id
  （= conversation_id）多轮续聊时不会累积上一轮结果；
- 所有值都是 JSON 友好类型，SqliteSaver 可直接持久化。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Any, Dict, List, Optional, TypedDict


def merge_turn_state(current: Optional[Dict[str, Any]], update: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """递归合并本轮状态（并行分支安全 + 支持 __reset__ 重置）。"""
    if isinstance(update, dict) and update.get("__reset__"):
        return {}
    if not isinstance(current, dict):
        current = {}
    if not isinstance(update, dict):
        return current
    merged: Dict[str, Any] = deepcopy(current)
    for key, value in update.items():
        if key == "__reset__":
            continue
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = merge_turn_state(existing, value)
        elif isinstance(existing, list) and isinstance(value, list):
            merged[key] = list(existing) + list(value)
        else:
            merged[key] = deepcopy(value)
    return merged


merge_subtask_results = merge_turn_state
merge_steps = merge_turn_state
merge_tool_results = merge_turn_state


class AgentState(TypedDict, total=False):
    """主 Agent 图状态。"""

    # ---- 契约字段 ----
    question: str
    conversation_id: str
    messages: List[Dict[str, str]]          # 本轮 LLM 上下文（history + 当前问题）
    intent: str                              # classify 节点输出
    plan: List[Dict[str, Any]]               # [{role, objective}]
    route_source: str                        # W12.1：rule（规则短路）/ llm（回退）
    route: Dict[str, Any]                    # W12.1：规则匹配详情（rule/matched/action_hint）
    subtask_results: List[Dict[str, Any]]    # aggregate 后各子任务结果
    tool_results: List[Dict[str, Any]]       # 工具调用摘要
    answer: str                              # 最终中文回答
    actions: List[Dict[str, Any]]            # 本轮生成的 pending actions（含 id）
    errors: List[str]                        # 子任务/执行错误
    steps: List[Dict[str, Any]]              # 每个子任务/节点的耗时与结果
    # ---- 图内部字段 ----
    history: List[Dict[str, str]]            # 会话历史（conversation 表）
    turn: Annotated[Dict[str, Any], merge_turn_state]  # 并行分支合并区
    pending_actions: List[Dict[str, Any]]    # 待确认 action（interrupt 循环）
    confirmed_actions: List[Dict[str, Any]]  # 已确认/取消 actions
    resume_decision: Optional[Dict[str, Any]]  # interrupt resume 的决策
    interrupted: bool
    checkpointer_thread: str
