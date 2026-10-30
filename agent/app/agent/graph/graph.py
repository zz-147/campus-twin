# -*- coding: utf-8 -*-
"""LangGraph 编排引擎（W11）。

架构（StateGraph + 条件边 + checkpointer + interrupt 人机确认）：

    START
      │
    classify ──(条件边：按 plan 并行 fan-out，LangGraph Send)──┐
      │        └─► run_diagnosis / run_onboarding / run_scene / run_recon /
      │            run_steward / run_path_trace / run_alert_groups / run_notify
      ▼
    aggregate（复用现有汇总/润色逻辑；落库 pending actions）
      │ 条件边：有写操作 → wait_confirm；否则 END
      ▼
    wait_confirm（LangGraph interrupt 暂停图，返回 pending actions 给前端）
      │ 用户确认/取消 → POST /api/actions/{id}/confirm|cancel 触发 Command(resume=...)
      ▼
    execute_action（执行 action，带 X-Operator: agent）→ 还有 pending？回 wait_confirm : END

- **checkpointer**：``langgraph.checkpoint.sqlite.SqliteSaver``，thread_id = conversation_id；
  同一会话多轮续聊、进程重启后仍可恢复（图状态持久化在 agent/data/graph.db）。
- **工具**：23 个只读工具用 ``langchain_core.tools`` 包装（同名同参数），诊断节点用
  ``ChatOpenAI.bind_tools`` 驱动（真实 provider）；mock 模式复用现有 mock 诊断循环，离线可跑。
- **回退**：``AGENT_ENGINE=classic`` 时 API 使用原 ``Orchestrator``，行为与 W10 完全一致。
- **API 契约不变**：ChatOutcome/ChatResponse 字段与 classic 一致。
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# LangGraph/LangChain 依赖的 PendingDeprecationWarning 在启动时很吵，统一静默
warnings.simplefilter("ignore")
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, Send, interrupt  # noqa: E402

from app.agent.actions import ActionError, ActionExecutor, ActionStore  # noqa: E402
from app.agent.agent import SYSTEM_PROMPT as DIAGNOSIS_SYSTEM_PROMPT  # noqa: E402
from app.agent.conversations import ConversationStore, title_from_question  # noqa: E402
from app.agent.graph.langchain_tools import build_chat_model, build_diagnosis_tools  # noqa: E402
from app.agent.graph.state import AgentState  # noqa: E402
from app.agent.providers.base import LLMProvider, LLMProviderError  # noqa: E402
from app.agent.rag.retriever import (  # noqa: E402
    augment_answer,
    format_context,
    is_knowledge_query,
    no_hit_answer,
    retrieval_record,
    search as search_knowledge,
)
from app.agent.roles.base import ActionDraft, RoleContext, RoleResult  # noqa: E402
from app.agent.roles.orchestrator import (  # noqa: E402
    ChatOutcome,
    ConversationNotFoundError,
    Orchestrator,
    ProviderUnavailableError,
    Subtask,
    classify_intent,
)
from app.agent.tools import OpsToolkit  # noqa: E402
from app.config import AgentSettings, resolve_provider_config  # noqa: E402

GRAPH_VERSION = "w11-langgraph-1"

#: 可被图路由的节点（= 子 Agent / W10 只读能力 / W13 MCP）
ROLE_NODES: Tuple[str, ...] = (
    "diagnosis",
    "onboarding",
    "scene",
    "recon",
    "steward",
    "path_trace",
    "alert_groups",
    "notify",
    "mcp_read",
    "mcp_tools",
)


class LangGraphEngine:
    """LangGraph 版编排引擎（与 classic Orchestrator 同接口）。"""

    engine_name = "langgraph"

    def __init__(
        self,
        provider: Optional[LLMProvider],
        toolkit: OpsToolkit,
        conversations: ConversationStore,
        actions: ActionStore,
        settings: AgentSettings,
        executor: Optional[ActionExecutor] = None,
        graph_db_path: Optional[Path] = None,
    ) -> None:
        self._provider = provider
        self._toolkit = toolkit
        self._conversations = conversations
        self._actions = actions
        self._settings = settings
        self._executor = executor or ActionExecutor(toolkit.backend, conversations)
        # 复用 classic 的意图规划 / W10 只读实现 / 汇总润色，保证两引擎行为一致
        self._delegate = Orchestrator(provider, toolkit, conversations, actions, settings)
        self._lc_tools = build_diagnosis_tools(toolkit)
        provider_info = provider.info() if provider is not None else None
        try:
            provider_config = resolve_provider_config(settings)
        except Exception:  # noqa: BLE001 - 配置异常时回退 mock 诊断
            provider_config = None
        if provider_info is not None and provider_info.provider != "mock":
            self._chat_model = build_chat_model(provider_config)
        else:
            self._chat_model = None
        self._graph_db_path = Path(graph_db_path or settings.graph_db_path)
        self._graph_db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._token_callback: Optional[Callable[[str], None]] = None
        self._conn = sqlite3.connect(str(self._graph_db_path), check_same_thread=False)
        self._saver = SqliteSaver(self._conn)
        self._graph = self._build_graph()

    # ------------------------------------------------------------------ #
    # 图构建
    # ------------------------------------------------------------------ #
    def _build_graph(self) -> Any:
        builder: StateGraph = StateGraph(AgentState)
        builder.add_node("classify", self._node_classify)
        for role in ROLE_NODES:
            builder.add_node(role, self._make_role_node(role))
        builder.add_node("aggregate", self._node_aggregate)
        builder.add_node("wait_confirm", self._node_wait_confirm)
        builder.add_node("execute_action", self._node_execute_action)

        builder.add_edge(START, "classify")
        builder.add_conditional_edges(
            "classify",
            self._route_from_classify,
            list(ROLE_NODES),
        )
        for role in ROLE_NODES:
            builder.add_edge(role, "aggregate")
        builder.add_conditional_edges(
            "aggregate",
            self._route_from_aggregate,
            ["wait_confirm", END],
        )
        builder.add_conditional_edges(
            "wait_confirm",
            self._route_from_wait_confirm,
            ["execute_action", END],
        )
        builder.add_conditional_edges(
            "execute_action",
            self._route_after_execute,
            ["wait_confirm", END],
        )
        return builder.compile(checkpointer=self._saver)

    # ------------------------------------------------------------------ #
    # 节点：classify / 子 Agent / aggregate / human_confirm / execute
    # ------------------------------------------------------------------ #
    def _node_classify(self, state: AgentState) -> Dict[str, Any]:
        question = str(state.get("question") or "")
        # W12.1：规则优先路由（规则命中 source=rule，直接短路；未命中才回退 LLM/启发式）
        decision = self._delegate.route(question)
        plan_dict = [
            {"role": role, "objective": objective} for role, objective in decision.plan
        ]
        route_trace = decision.to_trace()
        return {
            "intent": decision.intent,
            "plan": plan_dict,
            "route_source": decision.source,
            "route": route_trace,
            "turn": {"intent": decision.intent, "plan": plan_dict, "route": route_trace},
        }

    def _route_from_classify(self, state: AgentState) -> Any:
        question = str(state.get("question") or "")
        conversation_id = str(state.get("conversation_id") or "")
        history = list(state.get("history") or [])
        plan = list(state.get("plan") or state.get("turn", {}).get("plan") or [])
        route = state.get("route") or (state.get("turn") or {}).get("route")
        payload_common = {
            "question": question,
            "conversation_id": conversation_id,
            "history": history,
            "plan_count": len(plan),
            "route": route,
        }
        sends = [Send(item["role"], {**payload_common, "role": item["role"]}) for item in plan]
        if len(sends) == 1:
            return sends[0]
        return sends

    def _make_role_node(self, role: str):
        def node(payload: Dict[str, Any]) -> Dict[str, Any]:
            started = time.perf_counter()
            question = str(payload.get("question") or "")
            conversation_id = str(payload.get("conversation_id") or "")
            history = list(payload.get("history") or [])
            plan_count = int(payload.get("plan_count") or 1)
            route = payload.get("route") if isinstance(payload.get("route"), dict) else None
            ctx = RoleContext(
                question=question,
                conversation_id=conversation_id,
                history=history,
                route=route,
            )
            try:
                result = self._run_role(role, question, history, ctx)
                error = result.error
            except Exception as exc:  # noqa: BLE001 - 子任务失败不拖垮整图
                if role == "diagnosis" and plan_count == 1 and isinstance(
                    exc, (ProviderUnavailableError, LLMProviderError)
                ):
                    raise
                result = RoleResult(
                    role=role,
                    label=ROLE_LABELS.get(role, role),
                    answer="子任务 %s 执行失败：%s" % (role, exc),
                    elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
                    error="%s: %s" % (type(exc).__name__, exc),
                    tools_used=list(dict.fromkeys(ctx.tools_used)),
                )
                error = result.error
            step = {
                "node": role,
                "role": role,
                "label": result.label,
                "ok": error is None,
                "error": error,
                "elapsed_ms": result.elapsed_ms,
                "tools_used": result.tools_used,
                "actions": len(result.actions),
            }
            return {
                "turn": {
                    "results": {role: _role_result_to_dict(result)},
                    "steps": {role: step},
                    "tools_used": {role: list(result.tools_used)},
                },
            }

        node.__name__ = "run_%s" % role
        return node

    def _run_role(
        self,
        role: str,
        question: str,
        history: List[Dict[str, str]],
        ctx: RoleContext,
    ) -> RoleResult:
        knowledge_roles = {"diagnosis", "onboarding", "path_trace", "alert_groups"}
        if role == "diagnosis":
            hits = search_knowledge(question)
            if not hits and is_knowledge_query(question):
                return RoleResult(
                    role="diagnosis",
                    label="排障诊断",
                    answer=no_hit_answer(question),
                    data={"retrieval": retrieval_record("diagnosis", question, [], injected=True)},
                    tools_used=[],
                )
            if self._chat_model is not None:
                result = self._diagnose_with_langchain(
                    question, history, knowledge_context=format_context(hits)
                )
            else:
                if self._provider is None:
                    raise ProviderUnavailableError(
                        "排障对话需要配置 LLM（LLM_API_KEY）；当前可用的能力：路径追踪、"
                        "告警根因分组、通知状态查询（只读，无需 LLM）、加设备引导、"
                        "新设备侦察、场景配置、告警/会话数据治理。"
                    )
                result = self._delegate._run_diagnosis(question, history, ctx)
            return self._attach_knowledge(question, role, result)
        if role in ("onboarding", "scene", "recon", "steward"):
            result = self._delegate._roles[role].run(ctx)
        elif role == "path_trace":
            result = self._delegate._run_path_trace(question, ctx)
        elif role == "alert_groups":
            result = self._delegate._run_alert_groups(question, ctx)
        elif role == "notify":
            result = self._delegate._run_notify(question, ctx)
        elif role == "mcp_read":
            result = self._delegate._run_mcp_read(question, ctx)
        elif role == "mcp_tools":
            result = self._delegate._run_mcp_tools(question, ctx)
        else:
            raise ValueError("未知图节点：%s" % role)
        if role in knowledge_roles and result.error is None:
            result = self._attach_knowledge(question, role, result)
        return result

    def _attach_knowledge(self, question: str, role: str, result: RoleResult) -> RoleResult:
        """W12：Graph 节点的自动 RAG 注入（引用 [K1].. + trace.retrieval）。"""
        data = dict(result.data or {})
        if role == "diagnosis" and not search_knowledge(question) and is_knowledge_query(question):
            result.answer = no_hit_answer(question)
            data["retrieval"] = retrieval_record(role, question, [], injected=True)
            result.data = data
            return result
        answer, record, _ = augment_answer(role, question, result.answer)
        result.answer = answer
        data["retrieval"] = record
        result.data = data
        return result

    def _diagnose_with_langchain(
        self,
        question: str,
        history: List[Dict[str, str]],
        knowledge_context: str = "",
    ) -> RoleResult:
        """用 LangChain ChatOpenAI + bind_tools 执行诊断工具循环（真实 provider）。"""
        started = time.perf_counter()
        try:
            from langchain_core.messages import (
                AIMessage,
                HumanMessage,
                SystemMessage,
                ToolMessage,
            )
        except ImportError:  # pragma: no cover - 依赖已安装
            raise LLMProviderError("未安装 langchain-core，无法使用 LangGraph 真实模型诊断") from None

        system_content = DIAGNOSIS_SYSTEM_PROMPT
        if (knowledge_context or "").strip():
            system_content = (
                system_content
                + "\n\n"
                + str(knowledge_context).strip()
                + "\n\n引用规范：只允许引用以上 [K1].. 原文事实；无相关内容时明确说“知识库无相关内容”，"
                "不得编造配置/参数/密码。"
            )
        messages: List[Any] = [SystemMessage(content=system_content)]
        for item in (history or [])[-8:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            if role == "user" and content:
                messages.append(HumanMessage(content=content))
            elif role == "assistant" and content:
                messages.append(AIMessage(content=content))
        messages.append(HumanMessage(content=question))

        bound = self._chat_model.bind_tools(self._lc_tools)
        tool_sequence: List[str] = []
        steps: List[Dict[str, Any]] = []
        tool_error_count = 0
        answer = ""
        stop_reason = "max_steps"
        for step_index in range(max(1, int(self._settings.max_steps))):
            step_started = time.perf_counter()
            try:
                response = self._invoke_bound_model(bound, messages)
            except Exception as exc:  # noqa: BLE001 - 统一脱敏后抛给 API
                raise LLMProviderError(
                    self._settings.sanitize(
                        "LangGraph 诊断模型调用失败：%s: %s" % (type(exc).__name__, exc)
                    )
                ) from None
            messages.append(response)
            tool_calls = list(getattr(response, "tool_calls", None) or [])
            step_trace: Dict[str, Any] = {
                "step": step_index + 1,
                "provider_elapsed_ms": round((time.perf_counter() - step_started) * 1000.0, 2),
                "tool_calls": [],
                "content_chars": len(str(getattr(response, "content", "") or "")),
                "finish_reason": getattr(response, "response_metadata", {}).get("finish_reason")
                if isinstance(getattr(response, "response_metadata", None), dict)
                else None,
            }
            if not tool_calls:
                answer = str(getattr(response, "content", "") or "").strip()
                stop_reason = "final_answer"
                steps.append(step_trace)
                break
            for call in tool_calls:
                name = str(call.get("name") or "")
                arguments = call.get("args") or {}
                envelope = self._toolkit.call(name, arguments)
                tool_sequence.append(name)
                if not envelope.get("ok"):
                    tool_error_count += 1
                tool_message = ToolMessage(
                    content=json.dumps(envelope, ensure_ascii=False, default=str),
                    tool_call_id=str(call.get("id") or name),
                )
                messages.append(tool_message)
                step_trace["tool_calls"].append(
                    {
                        "name": name,
                        "arguments": arguments,
                        "ok": bool(envelope.get("ok")),
                        "elapsed_ms": envelope.get("elapsed_ms"),
                        "error": envelope.get("error"),
                    }
                )
            steps.append(step_trace)

        if not answer:
            answer = (
                "抱歉，未能在限定步数内完成诊断。已调用工具：%s。请补充故障现象后重试。"
                % ("、".join(dict.fromkeys(tool_sequence)) or "无")
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        provider_info = self._provider.info() if self._provider is not None else None
        trace = {
            "provider": provider_info.provider if provider_info else self._settings.provider,
            "model": provider_info.model if provider_info else self._settings.model,
            "steps": steps,
            "tool_sequence": list(dict.fromkeys(tool_sequence)),
            "tool_call_count": len(tool_sequence),
            "tool_error_count": tool_error_count,
            "tool_errors": [],
            "stop_reason": stop_reason,
            "elapsed_ms": elapsed_ms,
            "history_messages": len(history or []),
            "diagnosis_backend": "langchain_chat_openai",
        }
        return RoleResult(
            role="diagnosis",
            label="排障诊断",
            answer=answer,
            data={
                "diagnosis_trace": trace,
                "provider": provider_info.provider if provider_info else self._settings.provider,
                "model": provider_info.model if provider_info else self._settings.model,
            },
            elapsed_ms=elapsed_ms,
            tools_used=list(dict.fromkeys(tool_sequence)),
        )

    def _node_aggregate(self, state: AgentState) -> Dict[str, Any]:
        question = str(state.get("question") or "")
        conversation_id = str(state.get("conversation_id") or "")
        plan_dict = list(state.get("plan") or state.get("turn", {}).get("plan") or [])
        turn = state.get("turn") or {}
        results_map = turn.get("results") or {}
        results: List[RoleResult] = []
        subtasks: List[Subtask] = []
        for item in plan_dict:
            role = str(item.get("role") or "")
            subtasks.append(Subtask(role=role, objective=str(item.get("objective") or "")))
            payload = results_map.get(role)
            if payload is None:
                results.append(
                    RoleResult(
                        role=role,
                        label=ROLE_LABELS.get(role, role),
                        answer="子任务 %s 未返回结果" % role,
                        error="missing_result",
                    )
                )
                continue
            results.append(_role_result_from_dict(payload, role))
        actions = self._delegate._materialize_actions(results, conversation_id)
        # 流式模式关闭 LLM polish：保证 model.stream 推出的 token 与最终 answer 前缀一致
        delegate_settings = self._delegate._settings
        polish_settings = dataclasses.replace(delegate_settings, polish_llm=False)
        if self._token_callback is not None and delegate_settings.polish_llm:
            self._delegate._settings = polish_settings
        try:
            answer, polish_trace = self._delegate._aggregate(
                question, subtasks, results, actions
            )
        finally:
            self._delegate._settings = delegate_settings
        errors = list(state.get("errors") or []) + [
            result.error for result in results if result.error
        ]
        steps = [turn.get("steps", {}).get(item.role) for item in subtasks]
        steps = [step for step in steps if isinstance(step, dict)]
        tool_sequence: List[str] = []
        tool_error_count = 0
        for result in results:
            tool_sequence.extend(result.tools_used)
            diagnosis_trace = result.data.get("diagnosis_trace") if isinstance(result.data, dict) else None
            if isinstance(diagnosis_trace, dict):
                tool_error_count += int(diagnosis_trace.get("tool_error_count") or 0)
        return {
            "answer": answer,
            "actions": actions,
            "pending_actions": list(actions),
            "subtask_results": [_role_result_to_dict(result) for result in results],
            "steps": steps,
            "tool_results": [
                {"role": result.role, "tools": list(result.tools_used)} for result in results
            ],
            "errors": errors,
            "turn": {
                "polish": polish_trace,
                "tool_sequence": list(dict.fromkeys(tool_sequence)),
                "tool_error_count": tool_error_count,
            },
        }

    def _route_from_aggregate(self, state: AgentState) -> str:
        return "wait_confirm" if state.get("pending_actions") else END

    def _node_wait_confirm(self, state: AgentState) -> Dict[str, Any]:
        pending = list(state.get("pending_actions") or [])
        if not pending:
            return {"resume_decision": None}
        summaries = [
            {
                "id": action.get("id"),
                "type": action.get("type"),
                "confirm_text": action.get("confirm_text"),
                "danger": action.get("danger"),
            }
            for action in pending
        ]
        # LangGraph interrupt()：在这里暂停图，等待前端用户确认/取消
        resume_value = interrupt(
            {
                "type": "human_confirm",
                "conversation_id": state.get("conversation_id"),
                "pending_actions": summaries,
            }
        )
        decision = resume_value if isinstance(resume_value, dict) else {"decision": "cancel"}
        decision.setdefault("decision", "cancel")
        return {"resume_decision": decision}

    def _route_from_wait_confirm(self, state: AgentState) -> str:
        return "execute_action" if state.get("resume_decision") else END

    def _node_execute_action(self, state: AgentState) -> Dict[str, Any]:
        decision = dict(state.get("resume_decision") or {})
        action_id = str(decision.get("action_id") or "")
        mode = str(decision.get("decision") or "cancel")
        pending = list(state.get("pending_actions") or [])
        errors: List[str] = []
        action: Optional[Dict[str, Any]] = None
        try:
            if not action_id:
                raise ActionError("invalid_params", "resume 决策缺少 action_id")
            if mode == "confirm":
                action = self._actions.confirm(action_id, self._executor, operator="agent")
            else:
                action = self._actions.cancel(action_id)
        except ActionError as exc:
            errors.append(exc.message)
            action = self._actions.get(action_id)
        remaining = [item for item in pending if str(item.get("id")) != action_id]
        return {
            "pending_actions": remaining,
            "confirmed_actions": [action] if action else [],
            "resume_decision": None,
            "errors": errors,
            "turn": {
                "confirmations": {
                    action_id: {
                        "status": action.get("status") if action else "failed",
                        "error": errors[-1] if errors else None,
                        "result": action.get("result") if action else None,
                    }
                }
            },
        }

    def _route_after_execute(self, state: AgentState) -> str:
        return "wait_confirm" if state.get("pending_actions") else END

    # ------------------------------------------------------------------ #
    # 流式 token 回调（W12 SSE：LangChain model.stream → 线程安全队列）
    # ------------------------------------------------------------------ #
    def set_token_callback(self, callback: Optional[Callable[[str], None]]) -> None:
        self._token_callback = callback

    def _invoke_bound_model(self, bound: Any, messages: List[Any]) -> Any:
        """调用 bind_tools 模型；有 token 回调时用 stream 逐 token 回调。"""
        if self._token_callback is None:
            return bound.invoke(messages)
        from langchain_core.messages import AIMessage

        accumulated: Any = None
        for chunk in bound.stream(messages):
            content = getattr(chunk, "content", "")
            if isinstance(content, str) and content:
                try:
                    self._token_callback(content)
                except Exception:  # noqa: BLE001 - 回调异常不影响主流程
                    pass
            accumulated = chunk if accumulated is None else accumulated + chunk
        if accumulated is None:
            raise LLMProviderError("LangGraph 流式模型未返回任何 chunk")
        return AIMessage(
            content=str(getattr(accumulated, "content", "") or ""),
            tool_calls=list(getattr(accumulated, "tool_calls", None) or []),
        )

    # ------------------------------------------------------------------ #
    # 图执行
    # ------------------------------------------------------------------ #
    def _graph_config(self, conversation_id: str) -> Dict[str, Any]:
        return {
            "configurable": {"thread_id": conversation_id},
            "recursion_limit": 50,
        }

    def _stream_graph(
        self, graph_input: Any, config: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Tuple[str, ...]]:
        events: List[Dict[str, Any]] = []
        last_time = time.perf_counter()
        try:
            iterator = self._graph.stream(graph_input, config, stream_mode="updates")
            for chunk in iterator:
                now = time.perf_counter()
                if isinstance(chunk, dict):
                    for node_name, update in chunk.items():
                        events.append(
                            {
                                "node": str(node_name),
                                "elapsed_ms": round((now - last_time) * 1000.0, 2),
                                "updated_keys": sorted(update.keys())
                                if isinstance(update, dict)
                                else [],
                            }
                        )
                last_time = now
        except Exception as exc:  # noqa: BLE001 - 中断在部分版本以异常抛出
            if type(exc).__name__ not in ("GraphInterrupt", "GraphBubbleUp"):
                raise
        snapshot = self._graph.get_state(config)
        return dict(snapshot.values or {}), events, tuple(snapshot.next or ())

    def handle(self, question: str, conversation_id: Optional[str] = None) -> ChatOutcome:
        question = (question or "").strip()
        if not question:
            raise ValueError("问题不能为空")
        started = time.perf_counter()

        history: List[Dict[str, str]] = []
        if conversation_id:
            existing = self._conversations.get(conversation_id)
            if existing is None:
                raise ConversationNotFoundError(conversation_id)
            history = self._conversations.recent_messages(conversation_id, limit=8)
        else:
            conversation = self._conversations.create(title=title_from_question(question))
            conversation_id = conversation["id"]
        self._conversations.append_message(
            conversation_id, "user", question, auto_title=True
        )

        config = self._graph_config(conversation_id)
        with self._lock:
            snapshot = self._graph.get_state(config)
            paused = bool(snapshot.next)
            if paused:
                # 图停在 interrupt：先让用户处理待确认操作，不开启新回合
                values = dict(snapshot.values or {})
                pending = self._pending_actions(conversation_id)
                answer = str(values.get("answer") or "").strip()
                if not answer:
                    answer = "当前有操作正在等待确认。"
                note = (
                    "\n\n---\n> 注意：上一轮还有 %d 个操作等待确认，"
                    "请先在下方操作卡片中「确认执行」或「取消」，再继续新的问题。" % len(pending)
                )
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
                trace = self._build_trace(
                    question=question,
                    conversation_id=conversation_id,
                    values=values,
                    events=[],
                    paused=True,
                    elapsed_ms=elapsed_ms,
                )
                outcome = self._build_outcome(
                    answer=answer + note,
                    trace=trace,
                    conversation_id=conversation_id,
                    pending_actions=pending,
                    elapsed_ms=elapsed_ms,
                )
                self._persist_assistant_message(conversation_id, outcome)
                return outcome

            input_state = {
                "question": question,
                "conversation_id": conversation_id,
                "history": history,
                "messages": list(history) + [{"role": "user", "content": question}],
                "intent": "",
                "plan": [],
                "subtask_results": [],
                "tool_results": [],
                "answer": "",
                "actions": [],
                "errors": [],
                "steps": [],
                "turn": {"__reset__": True},
                "pending_actions": [],
                "confirmed_actions": [],
                "resume_decision": None,
                "interrupted": False,
                "checkpointer_thread": conversation_id,
            }
            values, events, next_nodes = self._stream_graph(input_state, config)
            paused = bool(next_nodes)
            pending = list(values.get("pending_actions") or []) if paused else []
            pending = [item for item in pending if self._action_is_pending(item)]
            if paused and not pending:
                pending = self._pending_actions(conversation_id)
            answer = str(values.get("answer") or "").strip()
            if not answer:
                if paused:
                    answer = "已生成待确认操作，请在前端操作卡片中确认或取消。"
                else:
                    answer = "抱歉，本次未能生成有效回答，请稍后重试。"
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            trace = self._build_trace(
                question=question,
                conversation_id=conversation_id,
                values=values,
                events=events,
                paused=paused,
                elapsed_ms=elapsed_ms,
            )
            outcome = self._build_outcome(
                answer=answer,
                trace=trace,
                conversation_id=conversation_id,
                pending_actions=pending,
                elapsed_ms=elapsed_ms,
            )
            self._persist_assistant_message(conversation_id, outcome)
            return outcome

    # ------------------------------------------------------------------ #
    # 人机确认：/api/actions/{id}/confirm|cancel 恢复图
    # ------------------------------------------------------------------ #
    def confirm_action(self, action_id: str) -> Dict[str, Any]:
        return self._resume_action(action_id, decision="confirm")

    def cancel_action(self, action_id: str) -> Dict[str, Any]:
        return self._resume_action(action_id, decision="cancel")

    def _resume_action(self, action_id: str, decision: str) -> Dict[str, Any]:
        action = self._actions.get(action_id)
        if action is None:
            raise ActionError("not_found", "操作不存在或已被清理")
        conversation_id = str(action.get("conversation_id") or "")
        if not conversation_id:
            # 没有会话关联（历史 action）→ 直接执行
            if decision == "confirm":
                return self._actions.confirm(action_id, self._executor, operator="agent")
            return self._actions.cancel(action_id)
        config = self._graph_config(conversation_id)
        with self._lock:
            snapshot = self._graph.get_state(config)
            if not snapshot.next:
                # 图未暂停（例如 action 由 classic 引擎创建）→ 回退直接执行
                if decision == "confirm":
                    return self._actions.confirm(action_id, self._executor, operator="agent")
                return self._actions.cancel(action_id)
            self._stream_graph(
                Command(resume={"action_id": action_id, "decision": decision}), config
            )
        return self._actions.get(action_id) or action

    # ------------------------------------------------------------------ #
    # 信息 / 关闭
    # ------------------------------------------------------------------ #
    def info(self) -> Dict[str, Any]:
        return {
            "engine": "langgraph",
            "graph_version": GRAPH_VERSION,
            "nodes": ["classify", *ROLE_NODES, "aggregate", "wait_confirm", "execute_action"],
            "edges": [
                "START -> classify",
                "classify -> (Send fan-out) role nodes",
                "role nodes -> aggregate",
                "aggregate -> wait_confirm | END",
                "wait_confirm -> execute_action | END (interrupt resume)",
                "execute_action -> wait_confirm | END",
            ],
            "checkpointer": {
                "type": "SqliteSaver",
                "db": str(self._graph_db_path),
                "thread_id_field": "conversation_id",
            },
            "human_in_the_loop": {
                "node": "wait_confirm",
                "mechanism": "interrupt()/Command(resume=...)",
                "action_ttl_s": self._settings.action_ttl_s,
            },
            "diagnosis_backend": (
                "langchain_openai.ChatOpenAI + bind_tools"
                if self._chat_model is not None
                else "mock provider (offline)"
            ),
            "tools": len(self._toolkit.tool_names()),
            "langchain_tools": len(self._lc_tools),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响进程退出
            pass

    def with_provider(self, provider: LLMProvider) -> "LangGraphEngine":
        """请求级 provider 覆盖：新建引擎（复用同一会话/action/graph DB）。"""
        return LangGraphEngine(
            provider=provider,
            toolkit=self._toolkit,
            conversations=self._conversations,
            actions=self._actions,
            settings=self._settings,
            executor=self._executor,
            graph_db_path=self._graph_db_path,
        )

    # ------------------------------------------------------------------ #
    # 内部：结果构造 / trace / 持久化
    # ------------------------------------------------------------------ #
    def _build_outcome(
        self,
        answer: str,
        trace: Dict[str, Any],
        conversation_id: str,
        pending_actions: List[Dict[str, Any]],
        elapsed_ms: float,
    ) -> ChatOutcome:
        provider_info = self._provider.info() if self._provider is not None else None
        return ChatOutcome(
            answer=answer,
            trace=trace,
            provider=provider_info.provider if provider_info else self._settings.provider,
            model=provider_info.model if provider_info else self._settings.model,
            elapsed_ms=elapsed_ms,
            conversation_id=conversation_id,
            actions=pending_actions,
        )

    def _build_trace(
        self,
        question: str,
        conversation_id: str,
        values: Dict[str, Any],
        events: List[Dict[str, Any]],
        paused: bool,
        elapsed_ms: float,
    ) -> Dict[str, Any]:
        provider_info = self._provider.info() if self._provider is not None else None
        plan = list(values.get("plan") or (values.get("turn") or {}).get("plan") or [])
        subtask_results = list(values.get("subtask_results") or [])
        results_by_role = {
            str(item.get("role")): item for item in subtask_results if isinstance(item, dict)
        }
        plan_trace: List[Dict[str, Any]] = []
        for item in plan:
            role = str(item.get("role") or "")
            result = results_by_role.get(role) or {}
            plan_trace.append(
                {
                    "role": role,
                    "objective": item.get("objective"),
                    "label": result.get("label") or ROLE_LABELS.get(role, role),
                    "ok": result.get("error") is None,
                    "error": result.get("error"),
                    "elapsed_ms": result.get("elapsed_ms"),
                    "tools_used": result.get("tools_used") or [],
                    "actions": len(result.get("actions") or []),
                    "node": role,
                }
            )
        turn = values.get("turn") or {}
        tool_sequence = list(turn.get("tool_sequence") or [])
        if not tool_sequence:
            for result in subtask_results:
                tool_sequence.extend(result.get("tools_used") or [])
        tool_error_count = int(turn.get("tool_error_count") or 0)
        actions = list(values.get("actions") or [])
        pending = list(values.get("pending_actions") or [])
        retrieval: List[Dict[str, Any]] = []
        mcp_calls: List[Dict[str, Any]] = []
        for result in subtask_results:
            data = result.get("data") if isinstance(result, dict) else None
            record = data.get("retrieval") if isinstance(data, dict) else None
            if isinstance(record, dict):
                retrieval.append(record)
            calls = data.get("mcp_calls") if isinstance(data, dict) else None
            if isinstance(calls, list):
                mcp_calls.extend(item for item in calls if isinstance(item, dict))
        return {
            "mode": "multi_agent",
            "engine": "langgraph",
            "graph_version": GRAPH_VERSION,
            "roles": ["main", "onboarding", "scene", "recon", "steward"],
            "readonly_capabilities": ["path_trace", "alert_groups", "notify"],
            "provider": provider_info.provider if provider_info else self._settings.provider,
            "model": provider_info.model if provider_info else self._settings.model,
            "base_url": provider_info.base_url if provider_info else self._settings.base_url,
            "key_configured": bool(provider_info.key_configured)
            if provider_info
            else self._settings.has_llm_key,
            "backend_mode": self._toolkit.mode,
            "intent": values.get("intent") or turn.get("intent"),
            "route_source": values.get("route_source")
            or (turn.get("route") or {}).get("source")
            or "llm",
            "route": values.get("route") or turn.get("route"),
            "plan": plan_trace,
            "subtask_count": len(plan_trace),
            "steps": values.get("steps") or [],
            "tool_sequence": list(dict.fromkeys(tool_sequence)),
            "tool_call_count": len(tool_sequence),
            "tool_error_count": tool_error_count,
            "actions_count": len(actions),
            "mcp_calls": mcp_calls,
            "retrieval": retrieval,
            "knowledge": {
                "retriever": "bm25-lite-bigram",
                "corpus": "builtin",
                "hit_count": sum(int(item.get("hit_count") or 0) for item in retrieval),
                "citations": "[K1]..[Kn]",
            },
            "pending_actions_count": len(pending),
            "action_ttl_s": self._settings.action_ttl_s,
            "polish": turn.get("polish") or {"used": False},
            "graph": {
                "engine": "langgraph",
                "graph_version": GRAPH_VERSION,
                "events": events,
                "checkpointer": {
                    "type": "SqliteSaver",
                    "db": self._graph_db_path.name,
                    "thread_id": conversation_id,
                },
                "interrupted": paused,
                "interrupt_node": "wait_confirm" if paused else None,
                "diagnosis_backend": (
                    "langchain_chat_openai" if self._chat_model is not None else "mock_provider"
                ),
            },
            "conversation_id": conversation_id,
            "elapsed_ms": elapsed_ms,
            "stop_reason": "interrupt" if paused else "final_answer",
            "question": question,
        }

    def _persist_assistant_message(self, conversation_id: str, outcome: ChatOutcome) -> None:
        self._conversations.append_message(
            conversation_id,
            "assistant",
            outcome.answer,
            meta={
                "engine": "langgraph",
                "provider": outcome.provider,
                "model": outcome.model,
                "actions": [
                    {
                        "id": action.get("id"),
                        "type": action.get("type"),
                        "status": action.get("status"),
                        "confirm_text": action.get("confirm_text"),
                    }
                    for action in outcome.actions
                ],
                "trace": {
                    "mode": "multi_agent",
                    "engine": "langgraph",
                    "intent": outcome.trace.get("intent"),
                    "plan": [item.get("role") for item in outcome.trace.get("plan") or []],
                    "interrupted": outcome.trace.get("stop_reason") == "interrupt",
                    "elapsed_ms": outcome.elapsed_ms,
                },
            },
        )

    def _pending_actions(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self._actions.list_pending(conversation_id=conversation_id)

    def _action_is_pending(self, action: Dict[str, Any]) -> bool:
        action_id = str(action.get("id") or "")
        stored = self._actions.get(action_id) if action_id else None
        return bool(stored) and str(stored.get("status")) == "pending"


# --------------------------------------------------------------------------- #
# 角色 label / 结果序列化
# --------------------------------------------------------------------------- #
ROLE_LABELS: Dict[str, str] = {
    "diagnosis": "排障诊断",
    "onboarding": "接入引导 Agent",
    "scene": "场景构建 Agent",
    "recon": "接入侦察 Agent",
    "steward": "数据治理 Agent",
    "path_trace": "路径追踪",
    "alert_groups": "告警根因分组",
    "notify": "通知状态",
    "mcp_read": "MCP 文件读取",
    "mcp_tools": "MCP 工具清单",
}


def _role_result_to_dict(result: RoleResult) -> Dict[str, Any]:
    return {
        "role": result.role,
        "label": result.label,
        "answer": result.answer,
        "actions": [draft.to_dict() for draft in result.actions],
        "data": result.data,
        "elapsed_ms": result.elapsed_ms,
        "error": result.error,
        "tools_used": list(result.tools_used),
    }


def _role_result_from_dict(payload: Dict[str, Any], fallback_role: str) -> RoleResult:
    actions: List[ActionDraft] = []
    for draft in payload.get("actions") or []:
        if not isinstance(draft, dict):
            continue
        actions.append(
            ActionDraft(
                type=str(draft.get("type") or ""),
                params=dict(draft.get("params") or {}),
                confirm_text=str(draft.get("confirm_text") or ""),
                danger=bool(draft.get("danger")),
            )
        )
    data = payload.get("data")
    return RoleResult(
        role=str(payload.get("role") or fallback_role),
        label=str(payload.get("label") or ROLE_LABELS.get(fallback_role, fallback_role)),
        answer=str(payload.get("answer") or ""),
        actions=actions,
        data=data if isinstance(data, dict) else {},
        elapsed_ms=float(payload.get("elapsed_ms") or 0.0),
        error=payload.get("error"),
        tools_used=list(payload.get("tools_used") or []),
    )
