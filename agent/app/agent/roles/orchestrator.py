# -*- coding: utf-8 -*-
"""主 Agent / 编排器：意图识别 → 子任务计划（≤3）→ 子 Agent 并行/串行执行 → 汇总。

角色分工（docs/W9-Agent扩展设计.md）：
- 主 Agent（本模块）：只做路由与汇总，输出 answer + trace + actions + conversation_id；
- onboarding / scene / recon / steward：各带独立提示词与**工具白名单**（只读）；
- 排障诊断复用既有 OpsAgent（LLM + 8 个只读工具），保持 W5/W6 能力不回归。

安全：主 Agent 与子 Agent 都不直接执行写操作——子 Agent 只产出 ``ActionDraft``，
由本模块统一落库为 pending action，必须用户调用 /api/actions/{id}/confirm 才执行。
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.agent.actions import ActionStore
from app.agent.conversations import ConversationStore, title_from_question
from app.agent.providers.base import LLMProvider, LLMProviderError
from app.agent.rag.retriever import (
    augment_answer,
    citations_record,
    format_context,
    is_knowledge_query,
    no_hit_answer,
    retrieval_record,
    search as search_knowledge,
)
from app.agent.roles.base import ActionDraft, RoleContext, RoleResult
from app.agent.routing import RouteDecision, extract_mcp_path, route_question
from app.agent.roles.onboarding import OnboardingRole
from app.agent.roles.recon import ReconRole
from app.agent.roles.scene import SceneRole
from app.agent.roles.steward import StewardRole
from app.agent.tools import OpsToolkit
from app.config import AgentSettings


class ProviderUnavailableError(RuntimeError):
    """排障对话需要 LLM，但当前 provider 未就绪。"""


class ConversationNotFoundError(KeyError):
    """指定的会话不存在。"""


@dataclass
class Subtask:
    role: str
    objective: str


@dataclass
class ChatOutcome:
    answer: str
    trace: Dict[str, Any]
    provider: str
    model: str
    elapsed_ms: float
    conversation_id: Optional[str] = None
    actions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "answer": self.answer,
            "trace": self.trace,
            "provider": self.provider,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
            "conversation_id": self.conversation_id,
            "actions": self.actions,
        }


# --------------------------------------------------------------------------- #
# 意图识别
# --------------------------------------------------------------------------- #
_STEWARD_DATA_WORDS = (
    "告警",
    "故障记录",
    "健康问题",
    "对话",
    "会话",
    "聊天记录",
    "聊天历史",
    "历史记录",
    "审计",
    "操作记录",
)
_STEWARD_ACTION_WORDS = (
    "删除",
    "删掉",
    "删了",
    "清空",
    "清理",
    "清除",
    "静默",
    "撤销",
    "恢复",
    "审计",
    "操作记录",
)
_RECON_WORDS = (
    "新设备",
    "发现新设备",
    "发现设备",
    "新接入",
    "新加的",
    "新上线",
    "待确认的新设备",
    "pending",
    "加到场景",
    "加入场景",
    "加进场景",
)
_ONBOARDING_WORDS = (
    "加设备",
    "新增设备",
    "加一台",
    "新增一台",
    "再加一台",
    "再接一台",
    "接入一台",
    "加一个新",
    "部署一台",
    "配置一台",
    "想加",
    "帮我加",
    "加个",
    "新建",
    "新楼",
    "建一栋",
    "放一台",
    "放个",
    "放置",
)

#: 出现这些词说明是故障排查（"接入交换机离线" 不是要加设备）
_DIAGNOSIS_HINTS = ("离线", "掉线", "故障", "不可达", "异常", "ping", "断了", "中断", "为什么")
#: 弱接入词只有配合"怎么做/方案"类词才算 onboarding
_ONBOARDING_HOW_WORDS = ("怎么", "如何", "方案", "步骤", "配置", "教程", "接线", "部署")
_DEVICE_WORDS = ("交换机", "路由器", "服务器", "2960", "3650", "4331", "设备", "pc", "电脑")
_SCENE_WORDS = ("场景", "位置", "布局", "摆放", "楼栋", "挪", "移动", "样式", "3d")

#: W10 路径追踪意图词
_PATH_WORDS = (
    "路径",
    "怎么走",
    "经过",
    "trace",
    "path",
    "哪条链路",
    "通路上",
    "哪里断",
    "断点",
    "哪一跳",
    "哪一段",
    "路由路径",
)
#: W10 告警根因分组意图词
_ALERT_GROUP_WORDS = ("根因", "分组", "聚合", "抑制", "告警组", "根因分析")
#: W10 通知状态意图词
_NOTIFY_WORDS = ("通知", "webhook", "钉钉", "飞书", "企微", "告警推送", "告警通知")


def classify_intent(question: str) -> str:
    """粗粒度意图分类（W12.1：规则优先；未命中走启发式/LLM 兜底）。"""
    decision = route_question(question)
    if decision.source == "rule" and decision.intent:
        return decision.intent
    return _classify_fallback(question)


def _classify_fallback(question: str) -> str:
    """原有启发式分类（规则未命中时的 LLM/启发式回退）。"""
    text = (question or "").lower()
    if _is_steward(text):
        return "steward"
    if _is_path_intent(text):
        return "path_trace"
    if _is_alert_groups_intent(text):
        return "alert_groups"
    if _is_notify_intent(text):
        return "notify"
    if _is_recon(text):
        return "recon"
    if _is_onboarding(text):
        return "onboarding"
    if "场景" in text and any(word in text for word in _SCENE_WORDS):
        return "scene"
    return "diagnosis"


def _is_steward(text: str) -> bool:
    has_data = any(word in text for word in _STEWARD_DATA_WORDS)
    has_action = any(word in text for word in _STEWARD_ACTION_WORDS)
    # “NC 告警为什么清除慢/清除延迟”是知识型诊断问题，不是数据治理请求，避免误路由
    if has_action and any(
        phrase in text for phrase in ("清除慢", "清除延迟", "清除滞后", "清除不及时")
    ) and any(word in text for word in ("为什么", "为何", "原因", "为啥", "怎么")):
        return False
    # 解释型问题（“删除告警后为什么还在/为什么没清掉”）是诊断，不是执行写操作；
    # 除非有明确祈使词（请/帮我/执行/立刻…）
    if has_data and has_action and any(
        word in text for word in ("为什么", "为何", "原因", "为啥", "怎么还", "还在")
    ) and not any(
        word in text for word in ("请", "帮我", "帮忙", "执行", "立即", "立刻", "现在把", "麻烦")
    ):
        return False
    return has_data and has_action


def _is_recon(text: str) -> bool:
    if any(word in text for word in _RECON_WORDS):
        return True
    return "发现" in text and "设备" in text


def _is_path_intent(text: str) -> bool:
    """路径追踪意图：显式路径词，或“从 A 到 B 访问不通”且能识别出两个端点。

    普通排障（如“从教学楼访问不了服务器网段，怀疑路由没了”）不命中，避免与诊断 Agent 冲突。
    """
    if any(word in text for word in _PATH_WORDS):
        return True
    endpoints = set(
        re.findall(
            r"(?:\d{1,3}\.){3}\d{1,3}|pc-[a-z0-9-]+|server-\d+|sw-[a-z0-9-]+|core-\d+|router-\d+",
            text,
        )
    )
    if len(endpoints) < 2:
        return False
    return any(
        word in text for word in ("访问不通", "访问不了", "通不通", "怎么访问", "为什么访问")
    )


def _is_alert_groups_intent(text: str) -> bool:
    if any(word in text for word in _ALERT_GROUP_WORDS):
        return True
    # 通知类问题（即使包含“告警”字样）优先走通知状态查询
    if any(word in text for word in _NOTIFY_WORDS):
        return False
    has_alerts = any(word in text for word in ("告警", "故障记录", "健康问题"))
    return has_alerts and any(
        word in text
        for word in ("有哪些", "哪些", "多少", "几条", "概况", "情况", "状态", "现在", "当前")
    )


def _is_notify_intent(text: str) -> bool:
    return any(word in text for word in _NOTIFY_WORDS)


def _is_valid_ipv4(value: str) -> bool:
    parts = str(value or "").strip().split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit():
            return False
        if not 0 <= int(part) <= 255:
            return False
    return True


def _is_onboarding(text: str) -> bool:
    has_device = any(word in text for word in _DEVICE_WORDS)
    if not has_device:
        return False
    if any(word in text for word in _ONBOARDING_WORDS):
        return True
    # "接入两台交换机怎么配" 这类说法：弱接入词 + 怎么做/方案类词，且不是故障描述
    if (
        any(word in text for word in ("接入", "接线", "部署"))
        and any(word in text for word in _ONBOARDING_HOW_WORDS)
        and not any(word in text for word in _DIAGNOSIS_HINTS)
    ):
        return True
    # “加” + 设备型号（如“宿舍楼加 2960”）
    return any(word in text for word in ("加", "新增", "新装", "装一台")) and any(
        word in text for word in ("交换机", "路由器", "2960", "3650", "4331", "服务器")
    )


# --------------------------------------------------------------------------- #
# 编排器
# --------------------------------------------------------------------------- #
class Orchestrator:
    """1 主 Agent + 4 专职子 Agent 的编排实现（单服务多角色）。"""

    def __init__(
        self,
        provider: Optional[LLMProvider],
        toolkit: OpsToolkit,
        conversations: ConversationStore,
        actions: ActionStore,
        settings: AgentSettings,
    ) -> None:
        self._provider = provider
        self._toolkit = toolkit
        self._conversations = conversations
        self._actions = actions
        self._settings = settings
        self._roles = {
            "onboarding": OnboardingRole(toolkit, conversations),
            "scene": SceneRole(toolkit, conversations),
            "recon": ReconRole(toolkit, conversations),
            "steward": StewardRole(toolkit, conversations),
        }

    # ------------------------------------------------------------------ #
    # 计划 / 路由（W12.1：规则优先，未命中才走启发式/LLM 回退）
    # ------------------------------------------------------------------ #
    def _fallback_plan(self, question: str) -> List[Subtask]:
        """规则未命中时的原有计划逻辑（启发式/LLM 回退）。"""
        text = (question or "").lower()
        subtasks: List[Subtask] = []
        if _is_steward(text):
            subtasks.append(
                Subtask("steward", "处理数据管理请求（告警 / 会话 / 审计）")
            )
        if _is_path_intent(text):
            subtasks.append(Subtask("path_trace", "追踪源/目的主机之间的网络路径"))
        if _is_alert_groups_intent(text):
            subtasks.append(Subtask("alert_groups", "查询告警根因分组与上游抑制情况"))
        if _is_notify_intent(text):
            subtasks.append(Subtask("notify", "查询告警通知（Webhook）状态"))
        if _is_recon(text):
            subtasks.append(Subtask("recon", "侦察新设备并推断归属（可加入场景）"))
        elif _is_onboarding(text):
            subtasks.append(Subtask("onboarding", "生成设备接入方案与场景操作"))
        if not subtasks:
            if "场景" in text and any(word in text for word in _SCENE_WORDS):
                subtasks.append(Subtask("scene", "生成 3D 场景布局建议"))
            else:
                subtasks.append(Subtask("diagnosis", "排障诊断（复用既有诊断 Agent）"))
        return subtasks[:3]

    def route(self, question: str) -> RouteDecision:
        """W12.1 路由入口：规则命中 → source=rule 短路；未命中 → source=llm 回退。"""
        decision = route_question(question)
        if decision.source == "rule" and decision.plan:
            return decision
        subtasks = self._fallback_plan(question)
        return RouteDecision(
            intent=_classify_fallback(question),
            source="llm",
            rule=None,
            confidence=0.5,
            reason="规则未命中，回退启发式/LLM 路由",
            matched=[],
            plan=[(subtask.role, subtask.objective) for subtask in subtasks],
            action_hint=None,
        )

    def plan(self, question: str) -> List[Subtask]:
        return [Subtask(role, objective) for role, objective in self.route(question).plan]

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def handle(self, question: str, conversation_id: Optional[str] = None) -> ChatOutcome:
        question = (question or "").strip()
        if not question:
            raise ValueError("问题不能为空")
        started = time.perf_counter()

        # ---- 会话 ----
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

        decision = self.route(question)
        plan = [Subtask(role, objective) for role, objective in decision.plan]
        results: List[RoleResult] = self._execute_plan(
            plan, question, conversation_id, history, route=decision.to_trace()
        )
        actions = self._materialize_actions(results, conversation_id)
        answer, polish_trace = self._aggregate(question, plan, results, actions)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        trace = self._build_trace(
            plan, results, actions, conversation_id, elapsed_ms, polish_trace, route=decision
        )
        provider_info = self._provider.info() if self._provider is not None else None
        outcome = ChatOutcome(
            answer=answer,
            trace=trace,
            provider=provider_info.provider if provider_info else self._settings.provider,
            model=provider_info.model if provider_info else self._settings.model,
            elapsed_ms=elapsed_ms,
            conversation_id=conversation_id,
            actions=actions,
        )

        # ---- 落库 assistant 消息 ----
        self._conversations.append_message(
            conversation_id,
            "assistant",
            answer,
            meta={
                "provider": outcome.provider,
                "model": outcome.model,
                "actions": [
                    {
                        "id": action.get("id"),
                        "type": action.get("type"),
                        "status": action.get("status"),
                        "confirm_text": action.get("confirm_text"),
                    }
                    for action in actions
                ],
                "trace": {
                    "mode": "multi_agent",
                    "intent": decision.intent,
                    "route_source": decision.source,
                    "route_rule": decision.rule,
                    "plan": [subtask.role for subtask in plan],
                    "elapsed_ms": elapsed_ms,
                },
            },
        )
        return outcome

    # ------------------------------------------------------------------ #
    # 子任务执行
    # ------------------------------------------------------------------ #
    def _execute_plan(
        self,
        plan: Sequence[Subtask],
        question: str,
        conversation_id: str,
        history: List[Dict[str, str]],
        route: Optional[Dict[str, Any]] = None,
    ) -> List[RoleResult]:
        #: W12：自动 RAG 检索 + 引用注入的节点（诊断/接入引导/路径/告警分组）
        knowledge_roles = {"diagnosis", "onboarding", "path_trace", "alert_groups"}

        def execute(subtask: Subtask) -> RoleResult:
            ctx = RoleContext(
                question=question,
                conversation_id=conversation_id,
                history=list(history),
                route=route,
            )
            if subtask.role == "diagnosis":
                result = self._run_diagnosis(question, history, ctx)
            elif subtask.role == "path_trace":
                result = self._run_path_trace(question, ctx)
            elif subtask.role == "alert_groups":
                result = self._run_alert_groups(question, ctx)
            elif subtask.role == "notify":
                result = self._run_notify(question, ctx)
            elif subtask.role == "mcp_read":
                result = self._run_mcp_read(question, ctx)
            elif subtask.role == "mcp_tools":
                result = self._run_mcp_tools(question, ctx)
            else:
                result = self._roles[subtask.role].run(ctx)
            if subtask.role in knowledge_roles and result.error is None:
                result = self._attach_knowledge(question, subtask.role, result)
            return result

        if len(plan) == 1:
            subtask = plan[0]
            try:
                return [execute(subtask)]
            except ProviderUnavailableError:
                if subtask.role == "diagnosis":
                    raise
                raise

        results: List[RoleResult] = []
        with ThreadPoolExecutor(max_workers=min(3, len(plan))) as pool:
            futures = [(subtask, pool.submit(execute, subtask)) for subtask in plan]
            for subtask, future in futures:
                try:
                    results.append(future.result())
                except ProviderUnavailableError as exc:
                    if subtask.role == "diagnosis":
                        results.append(
                            RoleResult(
                                role="diagnosis",
                                label="排障诊断",
                                answer="（排障诊断需要 LLM，当前 provider 未就绪，已跳过排障部分；"
                                "设备接入/数据治理类请求仍可正常处理。）",
                                error=str(exc),
                            )
                        )
                    else:
                        results.append(
                            RoleResult(
                                role=subtask.role,
                                label=subtask.role,
                                answer="子任务执行失败：%s" % exc,
                                error=str(exc),
                            )
                        )
        return results

    def _attach_knowledge(
        self,
        question: str,
        role: str,
        result: RoleResult,
        hits: Optional[List[Dict[str, Any]]] = None,
    ) -> RoleResult:
        """W12：把内置知识库检索结果注入节点结果（引用 [K1].. + trace.retrieval）。"""
        if hits is None:
            hits = search_knowledge(question)
        data = dict(result.data or {})
        if role == "diagnosis" and not hits and is_knowledge_query(question):
            result.answer = no_hit_answer(question)
            data["retrieval"] = retrieval_record(role, question, [], injected=True)
            result.data = data
            return result
        answer, record, _ = augment_answer(role, question, result.answer)
        result.answer = answer
        data["retrieval"] = record
        result.data = data
        return result

    def _run_diagnosis(
        self,
        question: str,
        history: List[Dict[str, str]],
        ctx: RoleContext,
    ) -> RoleResult:
        if self._provider is None:
            raise ProviderUnavailableError(
                "排障对话需要配置 LLM（LLM_API_KEY）；当前可用的能力：路径追踪、告警根因分组、"
                "通知状态查询（只读，无需 LLM）、加设备引导、新设备侦察、场景配置、"
                "告警/会话数据治理。"
            )
        from app.agent.agent import OpsAgent
        from app.agent.tools import DIAGNOSIS_TOOL_NAMES

        # W12：先检索内置知识库，注入系统提示（真实 LLM）；无命中且是知识型问题则直接诚实回答
        hits = search_knowledge(question)
        if not hits and is_knowledge_query(question):
            return RoleResult(
                role="diagnosis",
                label="排障诊断",
                answer=no_hit_answer(question),
                data={"retrieval": retrieval_record("diagnosis", question, [], injected=True)},
                tools_used=[],
            )
        diag_toolkit = self._toolkit.restricted(DIAGNOSIS_TOOL_NAMES)
        agent = OpsAgent(
            provider=self._provider,
            toolkit=diag_toolkit,
            max_steps=self._settings.max_steps,
            max_tool_calls_per_step=self._settings.max_tool_calls_per_step,
        )
        run = agent.run(
            question,
            history=history,
            knowledge_context=format_context(hits),
        )
        return RoleResult(
            role="diagnosis",
            label="排障诊断",
            answer=run.answer,
            data={
                "diagnosis_trace": run.trace,
                "provider": run.provider,
                "model": run.model,
            },
            elapsed_ms=run.elapsed_ms,
            tools_used=list(run.trace.get("tool_sequence") or []),
        )

    # ------------------------------------------------------------------ #
    # W10 只读查询（主 Agent 直接调用只读工具，不产生写 action）
    # ------------------------------------------------------------------ #
    def _run_path_trace(self, question: str, ctx: RoleContext) -> RoleResult:
        started = time.perf_counter()
        hosts_env = ctx.call_tool(self._toolkit, "get_hosts", {})
        hosts = []
        if hosts_env.get("ok"):
            hosts = list(((hosts_env.get("data") or {}).get("hosts")) or [])
        endpoints = self._extract_path_endpoints(question, hosts)
        if len(endpoints) < 2:
            known = "、".join(
                "%s(%s)" % (host.get("hostname"), host.get("ip"))
                for host in hosts[:8]
                if host.get("ip")
            )
            extra = ""
            if endpoints:
                extra = "\n- 我已识别到：%s，还缺另一个端点。" % "、".join(
                    "%s(%s)" % (item["name"] or item["ip"], item["ip"]) for item in endpoints
                )
            answer = "\n".join(
                [
                    "## 需要补充路径端点",
                    "- 路径追踪需要源 IP 和目的 IP 两个端点，但问题里没有提取到完整的一对。",
                    extra.strip() if extra else "",
                    "- 可用的主机：%s" % (known or "（后端暂未返回主机列表）"),
                    "",
                    "## 请这样提问",
                    "- 例如：「从 PC-JX1 到 Server-1 的路径怎么走？」或直接给两个 IP。",
                ]
            ).strip()
            return RoleResult(
                role="path_trace",
                label="路径追踪",
                answer=answer,
                data={"endpoints": endpoints, "needs_endpoints": True},
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
                tools_used=list(dict.fromkeys(ctx.tools_used)),
            )
        source, dest = endpoints[0], endpoints[1]
        trace_env = ctx.call_tool(
            self._toolkit,
            "trace_path",
            {"source_ip": source["ip"], "dest_ip": dest["ip"]},
        )
        if not trace_env.get("ok"):
            answer = "\n".join(
                [
                    "## 路径追踪暂不可用",
                    "- 端点：%s（%s）→ %s（%s）"
                    % (source["name"] or source["ip"], source["ip"], dest["name"] or dest["ip"], dest["ip"]),
                    "- 原因：%s" % (trace_env.get("error") or "未知错误"),
                    "",
                    "## 可先做的排查",
                    "1. 在源主机执行 `traceroute <目的IP>` / `tracert <目的IP>` 确认断点。",
                    "2. 在核心交换机检查 `show ip route` 与接口状态（`show interfaces status`）。",
                    "3. 确认后端 `/api/path-trace` 已就绪、Network Controller 在线（flow-analysis 需要 NC）。",
                ]
            )
            return RoleResult(
                role="path_trace",
                label="路径追踪",
                answer=answer,
                data={"source": source, "dest": dest, "error": trace_env.get("error")},
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
                tools_used=list(dict.fromkeys(ctx.tools_used)),
            )
        data = trace_env.get("data") or {}
        answer = self._render_path_answer(source, dest, data)
        return RoleResult(
            role="path_trace",
            label="路径追踪",
            answer=answer,
            data={"source": source, "dest": dest, "trace": data},
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
            tools_used=list(dict.fromkeys(ctx.tools_used)),
        )

    def _run_alert_groups(self, question: str, ctx: RoleContext) -> RoleResult:
        started = time.perf_counter()
        groups_env = ctx.call_tool(self._toolkit, "get_alert_groups", {"state": "active"})
        if groups_env.get("ok"):
            data = groups_env.get("data") or {}
            answer = self._render_alert_groups_answer(data)
            return RoleResult(
                role="alert_groups",
                label="告警根因分组",
                answer=answer,
                data=data,
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
                tools_used=list(dict.fromkeys(ctx.tools_used)),
            )
        # 后端 W10 接口未就绪时回退到原始告警，仍然给出诚实答案
        alerts_env = ctx.call_tool(self._toolkit, "get_alerts", {"limit": 30})
        alerts_data = (alerts_env.get("data") or {}) if alerts_env.get("ok") else {}
        summary = alerts_data.get("summary") or {}
        answer = "\n".join(
            [
                "## 告警根因分组暂不可用",
                "- 原因：%s" % (groups_env.get("error") or "后端 /api/alerts/groups 接口未就绪"),
                "",
                "## 原始告警概况（回退查询 get_alerts）",
                "- 告警总数：%s；活跃：%s；已恢复：%s；被静默：%s"
                % (
                    summary.get("total", alerts_data.get("count", "未知")),
                    summary.get("active", "未知"),
                    summary.get("cleared", "未知"),
                    summary.get("silenced", "未知"),
                ),
                "- 根因分组/上游抑制统计需要后端 W10 接口就绪后再查询。",
                "",
                "## 说明",
                "- 我不会在缺少分组数据时编造根因或抑制数量。",
            ]
        )
        return RoleResult(
            role="alert_groups",
            label="告警根因分组",
            answer=answer,
            data={"error": groups_env.get("error"), "fallback_alerts": alerts_data},
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
            tools_used=list(dict.fromkeys(ctx.tools_used)),
        )

    def _run_notify(self, question: str, ctx: RoleContext) -> RoleResult:
        started = time.perf_counter()
        notify_env = ctx.call_tool(self._toolkit, "get_notify_status", {})
        wants_test = any(word in question for word in ("测试通知", "发个通知", "发测试", "发送测试"))
        if not notify_env.get("ok"):
            answer = "\n".join(
                [
                    "## 通知状态暂不可用",
                    "- 原因：%s" % (notify_env.get("error") or "后端 /api/notify/status 接口未就绪"),
                    "",
                    "## 说明",
                    "- 通知是否启用 / 最近发送情况需要后端 W10 接口就绪后再查询，我不会猜测。",
                ]
            )
        else:
            data = notify_env.get("data") or {}
            lines = ["## 告警通知状态"]
            if data.get("enabled"):
                lines.append("- 状态：**已启用**（Webhook）")
                lines.append("- 通知类型：%s" % (data.get("type") or "unknown"))
                lines.append("- 最低告警级别：%s" % (data.get("min_severity") or "unknown"))
                lines.append("- 累计发送：%s 条" % data.get("sent_count"))
                lines.append("- 最近发送：%s" % (data.get("last_sent_at") or "无记录"))
                lines.append("- 最近错误：%s" % (data.get("last_error") or "无"))
            else:
                lines.append("- 状态：**未启用**（未配置 Webhook）")
                lines.append("- 最低告警级别：%s" % (data.get("min_severity") or "critical"))
            lines.extend(
                [
                    "",
                    "## 说明",
                    "- 发送测试通知属于写操作：本期 Agent 只做只读查询，不生成 action。",
                    "- 可在前端「发送测试通知」按钮触发，或由后端 `POST /api/notify/test` 执行。",
                ]
            )
            if wants_test:
                lines.append("- 你这次要求发送测试通知，我不会自动执行；请使用上述按钮/接口。")
            answer = "\n".join(lines)
            return RoleResult(
                role="notify",
                label="通知状态",
                answer=answer,
                data=data,
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
                tools_used=list(dict.fromkeys(ctx.tools_used)),
            )
        return RoleResult(
            role="notify",
            label="通知状态",
            answer=answer,
            data={"error": notify_env.get("error")},
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
            tools_used=list(dict.fromkeys(ctx.tools_used)),
        )

    # ------------------------------------------------------------------ #
    # W13：MCP 外部工具（经本机 Bridge，只读）
    # ------------------------------------------------------------------ #
    def _run_mcp_tools(self, question: str, ctx: RoleContext) -> RoleResult:
        started = time.perf_counter()
        env = ctx.call_tool(self._toolkit, "mcp_list_tools", {})
        if not env.get("ok"):
            answer = "\n".join(
                [
                    "## MCP 工具暂不可用",
                    "- 原因：%s" % (env.get("error") or "MCP Bridge 未启动"),
                    "",
                    "## 说明",
                    "- 请先启动 MCP Bridge：`campus-twin/mcp_bridge/bridge.py`（127.0.0.1:8200），"
                    "再重试；Agent 不会编造外部工具。",
                ]
            )
            return RoleResult(
                role="mcp_tools",
                label="MCP 工具清单",
                answer=answer,
                data={"error": env.get("error")},
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
                tools_used=list(dict.fromkeys(ctx.tools_used)),
            )
        data = env.get("data") or {}
        tools = list(data.get("tools") or [])
        servers = dict(data.get("servers") or {})
        lines = [
            "## MCP 外部工具清单",
            "- 来源：Agent → HTTP MCP Bridge → mcp Client → stdio MCP servers",
            "- 共 **%d** 个工具：%s"
            % (
                len(tools),
                "，".join("%s：%d 个" % (name, count) for name, count in sorted(servers.items())),
            ),
            "",
            "| server | 工具 | 说明 |",
            "|---|---|---|",
        ]
        for tool in tools[:12]:
            lines.append(
                "| %s | `%s` | %s |"
                % (
                    tool.get("server"),
                    tool.get("name"),
                    str(tool.get("description") or "")[:70],
                )
            )
        lines.extend(
            [
                "",
                "## 说明",
                "- filesystem 工具只允许读取 `configs/`、`docs/`（由 MCP server 白名单保证），写工具被 Bridge 拒绝；",
                "- memory 工具操作本地知识图谱（`mcp_bridge/data/memory.json`），不涉及网络设备写操作；",
                "- 可直接自然语言让我读取文件，例如「读一下 configs/core-3650.txt 的 VLAN 配置」。",
            ]
        )
        return RoleResult(
            role="mcp_tools",
            label="MCP 工具清单",
            answer="\n".join(lines),
            data={
                "mcp_calls": data.get("mcp_calls") or [],
                "total": len(tools),
                "servers": servers,
                "tools": tools,
            },
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
            tools_used=list(dict.fromkeys(ctx.tools_used)),
        )

    def _run_mcp_read(self, question: str, ctx: RoleContext) -> RoleResult:
        started = time.perf_counter()
        path = extract_mcp_path(question)
        if not path:
            answer = "\n".join(
                [
                    "## 需要补充文件路径",
                    "- 请提供 configs/ 或 docs/ 下的文件路径，例如「读一下 configs/core-3650.txt 的 VLAN 配置」。",
                ]
            )
            return RoleResult(
                role="mcp_read",
                label="MCP 文件读取",
                answer=answer,
                data={"error": "missing_path"},
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
                tools_used=list(dict.fromkeys(ctx.tools_used)),
            )
        env = ctx.call_tool(self._toolkit, "mcp_read_file", {"path": path})
        if not env.get("ok"):
            answer = "\n".join(
                [
                    "## MCP 文件读取失败",
                    "- 文件：`%s`" % path,
                    "- 原因：%s" % (env.get("error") or "MCP Bridge 未启动"),
                    "",
                    "## 可先检查",
                    "1. MCP Bridge 是否已启动：`python mcp_bridge/bridge.py --port 8200`；",
                    "2. 路径是否在 `configs/`、`docs/` 白名单目录内；",
                    "3. Bridge `/health` 中 filesystem server 是否 connected。",
                    "",
                    "## 说明",
                    "- 我不会编造文件内容（未读取成功时不做内容总结）。",
                ]
            )
            return RoleResult(
                role="mcp_read",
                label="MCP 文件读取",
                answer=answer,
                data={"path": path, "error": env.get("error"),
                      "mcp_calls": [{"server": "filesystem", "tool": "mcp_read_file", "ok": False, "elapsed_ms": None}]},
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
                tools_used=list(dict.fromkeys(ctx.tools_used)),
            )
        data = env.get("data") or {}
        content = str(data.get("content") or "")
        lines = [
            "## MCP 文件读取（只读）",
            "- 文件：`%s`（%d 字符）" % (path, len(content)),
            "- 来源：MCP filesystem server（经 MCP Bridge，只读白名单）",
            "",
        ]
        markers = ("vlan", "interface", "switchport", "trunk", "ip address", "hostname", "router", "enable")
        highlights = [
            line
            for line in content.splitlines()
            if any(marker in line.lower() for marker in markers)
        ][:20]
        if highlights:
            lines.extend(
                [
                    "## VLAN/接口配置要点",
                    "```",
                    *highlights,
                    "```",
                ]
            )
        else:
            lines.extend(
                [
                    "## 文件内容（前 20 行）",
                    "```",
                    *content.splitlines()[:20],
                    "```",
                ]
            )
        lines.extend(
            [
                "",
                "## 说明",
                "- 以上内容由 `mcp_read_file` 工具真实读取；文件较大时仅展示配置要点。",
                "- filesystem MCP server 只允许读取 configs/docs，写操作会被 Bridge 白名单拒绝。",
            ]
        )
        return RoleResult(
            role="mcp_read",
            label="MCP 文件读取",
            answer="\n".join(lines),
            data={
                "path": path,
                "chars": len(content),
                "content": content,
                "mcp_calls": data.get("mcp_calls") or [],
            },
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
            tools_used=list(dict.fromkeys(ctx.tools_used)),
        )

    # ------------------------------------------------------------------ #
    # W10 输出渲染 / 端点提取
    # ------------------------------------------------------------------ #
    def _extract_path_endpoints(
        self, question: str, hosts: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """从问题中提取源/目的主机端点（主机名→IP 反查 + 直接 IP）。"""
        text = (question or "").lower()
        candidates: List[Dict[str, Any]] = []
        seen_ips: set = set()
        for host in hosts or []:
            ip = str(host.get("ip") or "").strip()
            name = str(host.get("hostname") or "").strip()
            if not ip:
                continue
            tokens = [token for token in (name.lower(), ip.lower()) if token]
            positions = [text.find(token) for token in tokens if text.find(token) >= 0]
            if not positions or ip in seen_ips:
                continue
            seen_ips.add(ip)
            candidates.append({"pos": min(positions), "ip": ip, "name": name})
        for match in re.finditer(
            r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", question or ""
        ):
            ip = match.group(0)
            if ip in seen_ips or not _is_valid_ipv4(ip):
                continue
            seen_ips.add(ip)
            candidates.append({"pos": match.start(), "ip": ip, "name": ""})
        candidates.sort(key=lambda item: item["pos"])
        return candidates[:2]

    def _render_path_answer(
        self,
        source: Dict[str, Any],
        dest: Dict[str, Any],
        data: Dict[str, Any],
    ) -> str:
        status = str(data.get("trace_status") or "unknown")
        status_text = {
            "complete": "完整可达",
            "partial": "部分可达（路径中断）",
            "failed": "失败/不可达",
        }.get(status, status)
        hops = list(data.get("hops") or [])
        links = list(data.get("links") or [])
        path_text = " → ".join(
            str(hop.get("device_name")) for hop in hops if hop.get("device_name")
        ) or "无 hop 数据"
        lines = [
            "## 路径追踪结果",
            "- 源：%s（%s）" % (source.get("name") or source.get("ip"), source.get("ip")),
            "- 目的：%s（%s）" % (dest.get("name") or dest.get("ip"), dest.get("ip")),
            "- 状态：**%s**（trace_status=%s）" % (status_text, status),
            "- 路径：%s（共 %d 跳）" % (path_text, len(hops)),
        ]
        if data.get("error"):
            lines.append("- 断点/错误：%s" % data.get("error"))
        lines.extend(
            [
                "",
                "## 逐跳明细",
                "| # | 设备 | 角色 | 入端口 | 出端口 |",
                "|---|---|---|---|---|",
            ]
        )
        role_labels = {"source": "源", "transit": "转发", "destination": "目的"}
        for hop in hops:
            lines.append(
                "| %s | %s | %s | %s | %s |"
                % (
                    hop.get("index"),
                    hop.get("device_name") or hop.get("device_id"),
                    role_labels.get(str(hop.get("role")), hop.get("role") or "-"),
                    hop.get("ingress_port") or "-",
                    hop.get("egress_port") or "-",
                )
            )
        if links:
            lines.extend(["", "## 链路（用于 3D 高亮）"])
            for link in links:
                lines.append(
                    "- %s → %s（link_id=%s）"
                    % (link.get("source"), link.get("target"), link.get("link_id"))
                )
        lines.extend(["", "## 建议排查"])
        if status == "complete":
            lines.extend(
                [
                    "- 路径完整可达；如业务仍不通，优先检查终端网关、ACL/NAT、DNS 与应用端口。",
                    "- 可在 3D 大屏「路径追踪」面板用上述 links 高亮整条路径。",
                ]
            )
        else:
            lines.extend(
                [
                    "1. 在断点两侧设备检查接口状态：`show interfaces status` / `show interfaces <端口>`。",
                    "2. 检查链路是否被 shutdown、err-disabled 或速率/双工异常。",
                    "3. 从源主机执行 `traceroute <目的IP>` 交叉验证断点位置。",
                    "4. 修复后重新发起路径追踪，确认状态恢复 complete。",
                ]
            )
        return "\n".join(lines)

    def _render_alert_groups_answer(self, data: Dict[str, Any]) -> str:
        groups = list(data.get("groups") or [])
        total_members = int(data.get("total_members") or 0)
        total_suppressed = int(data.get("total_suppressed") or 0)
        lines = [
            "## 告警根因分组（active）",
            "- 共 **%d** 个根因分组，覆盖 **%d** 条告警，其中 **%d** 条被上游根因抑制。"
            % (len(groups), total_members, total_suppressed),
        ]
        if not groups:
            lines.extend(
                [
                    "",
                    "## 结论",
                    "- 当前没有活跃告警分组（无活跃告警），无需处理。",
                ]
            )
            return "\n".join(lines)
        lines.extend(
            [
                "",
                "| 组 | 根因 | 根因设备 | 级别 | 成员数 | 被抑制 |",
                "|---|---|---|---|---|---|",
            ]
        )
        for group in groups[:10]:
            lines.append(
                "| %s | %s | %s | %s | %s | %s |"
                % (
                    group.get("group_id"),
                    group.get("root_cause"),
                    group.get("root_device_name"),
                    group.get("severity"),
                    group.get("member_count"),
                    group.get("suppressed_count"),
                )
            )
        lines.append("")
        for group in groups[:5]:
            lines.append(
                "### %s：%s" % (group.get("group_id"), group.get("title") or group.get("root_cause"))
            )
            lines.append(
                "- 根因：%s；根因设备：%s；级别：%s"
                % (group.get("root_cause"), group.get("root_device_name"), group.get("severity"))
            )
            suppressed = [
                member
                for member in group.get("members") or []
                if member.get("suppressed_by")
            ]
            if suppressed:
                lines.append("- 被上游抑制的成员（展示层抑制，仍保留 active）：")
                for member in suppressed[:5]:
                    lines.append(
                        "  - `%s` %s：%s"
                        % (
                            member.get("id"),
                            member.get("device_name"),
                            str(member.get("detail") or "")[:80],
                        )
                    )
            lines.append("")
        lines.extend(
            [
                "## 说明",
                "- 抑制只影响展示层：被抑制告警不会被删除、状态仍为 active。",
                "- 可在大屏告警面板切换「按根因分组」查看组卡片与成员。",
                "- 处理根因（如恢复上行链路）后，整组告警会随之恢复。",
            ]
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 汇总
    # ------------------------------------------------------------------ #
    def _materialize_actions(
        self, results: List[RoleResult], conversation_id: str
    ) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []
        seen: set = set()
        for result in results:
            for draft in result.actions:
                key = (
                    draft.type,
                    json.dumps(draft.params, ensure_ascii=False, sort_keys=True, default=str),
                )
                if key in seen:
                    continue
                seen.add(key)
                stored = self._actions.create(
                    action_type=draft.type,
                    params=draft.params,
                    confirm_text=draft.confirm_text,
                    danger=draft.danger,
                    conversation_id=conversation_id,
                )
                actions.append(stored)
                if len(actions) >= 5:
                    return actions
        return actions

    def _aggregate(
        self,
        question: str,
        plan: Sequence[Subtask],
        results: List[RoleResult],
        actions: List[Dict[str, Any]],
    ) -> Tuple[str, Dict[str, Any]]:
        if not results:
            return "未能完成本次请求：没有可执行的子任务。", {"used": False}
        if len(results) == 1:
            answer = results[0].answer
            polish_trace = {"used": False, "reason": "single_subtask"}
            # 单角色 + 真实 LLM：可选一次汇总润色（排障本身已由 LLM 生成，跳过）
            if (
                results[0].role != "diagnosis"
                and self._provider is not None
                and self._settings.polish_llm
                and self._provider.info().provider != "mock"
            ):
                polished, polish_trace = self._polish(question, results[0].answer, actions)
                if polished:
                    answer = polished
            return answer, polish_trace

        lines: List[str] = ["# 综合处理方案", ""]
        lines.append(
            "主 Agent 已把请求拆成 %d 个子任务并完成：%s。"
            % (
                len(plan),
                "、".join("%s（%s）" % (result.label, subtask.objective) for result, subtask in zip(results, plan)),
            )
        )
        lines.append("")
        for result in results:
            lines.append("## %s" % result.label)
            lines.append(result.answer.strip())
            lines.append("")
        if actions:
            lines.append("## 待确认操作（需你确认后执行，10 分钟内有效）")
            for index, action in enumerate(actions, start=1):
                lines.append(
                    "%d. %s%s"
                    % (
                        index,
                        "【危险】" if action.get("danger") else "",
                        action.get("confirm_text"),
                    )
                )
            lines.append("")
        answer = "\n".join(lines)
        polish_trace = {"used": False, "reason": "deterministic_merge"}
        if (
            self._provider is not None
            and self._settings.polish_llm
            and self._provider.info().provider != "mock"
        ):
            polished, polish_trace = self._polish(question, answer, actions)
            if polished:
                answer = polished
        return answer, polish_trace

    def _polish(
        self, question: str, draft_answer: str, actions: List[Dict[str, Any]]
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """用真实 LLM 把子 Agent 结论汇总润色（事实不得变，失败则回退模板汇总）。"""
        if self._provider is None:
            return None, {"used": False, "reason": "no_provider"}
        started = time.perf_counter()
        system = (
            "你是 campus-twin 校园网数字孪生平台的『主 Agent』，负责把子 Agent / 只读查询的结论"
            "汇总成给学校运维人员的最终中文答复。可用能力包括：排障诊断、路径追踪"
            "（trace_path）、告警根因分组（get_alert_groups）、通知状态（get_notify_status）、"
            "设备接入引导、场景配置、告警/会话数据治理。硬性规则：\n"
            "1) 只能使用给定材料中的设备名/IP/数字/结论，禁止编造；\n"
            "2) 必须保留关键操作信息（CLI、连线表、排障步骤、确认卡片说明、hop 链/断点）；\n"
            "3) 如果材料中有待确认操作，必须明确提示“需点击确认后才会执行”；\n"
            "4) 用 Markdown，结构清晰，不要输出 JSON，不要复述内部工具调用过程。"
        )
        user = (
            "用户问题：%s\n\n"
            "子 Agent 结论材料：\n%s\n\n"
            "待确认操作：%s\n\n"
            "请输出最终答复。"
            % (
                question,
                draft_answer,
                "；".join(str(action.get("confirm_text")) for action in actions) or "无",
            )
        )
        try:
            message = self._provider.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                tools=None,
            )
        except LLMProviderError as exc:
            return None, {
                "used": False,
                "reason": "llm_error",
                "error": str(exc)[:300],
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
            }
        content = (message.content or "").strip()
        return (content or None), {
            "used": bool(content),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }

    # ------------------------------------------------------------------ #
    # trace
    # ------------------------------------------------------------------ #
    def _build_trace(
        self,
        plan: Sequence[Subtask],
        results: List[RoleResult],
        actions: List[Dict[str, Any]],
        conversation_id: Optional[str],
        elapsed_ms: float,
        polish_trace: Dict[str, Any],
        route: Optional[RouteDecision] = None,
    ) -> Dict[str, Any]:
        plan_trace: List[Dict[str, Any]] = []
        tool_sequence: List[str] = []
        tool_error_count = 0
        diagnosis_trace: Optional[Dict[str, Any]] = None
        retrieval: List[Dict[str, Any]] = []
        mcp_calls: List[Dict[str, Any]] = []
        for subtask, result in zip(plan, results):
            plan_trace.append(
                {
                    "role": subtask.role,
                    "objective": subtask.objective,
                    "label": result.label,
                    "ok": result.error is None,
                    "error": result.error,
                    "elapsed_ms": result.elapsed_ms,
                    "tools_used": result.tools_used,
                    "actions": len(result.actions),
                }
            )
            tool_sequence.extend(result.tools_used)
            record = (result.data or {}).get("retrieval") if isinstance(result.data, dict) else None
            if isinstance(record, dict):
                retrieval.append(record)
            calls = (result.data or {}).get("mcp_calls") if isinstance(result.data, dict) else None
            if isinstance(calls, list):
                mcp_calls.extend(item for item in calls if isinstance(item, dict))
            if result.role == "diagnosis":
                diagnosis_trace = result.data.get("diagnosis_trace")
                if diagnosis_trace:
                    tool_error_count += int(diagnosis_trace.get("tool_error_count") or 0)
        provider_info = self._provider.info() if self._provider is not None else None
        trace: Dict[str, Any] = {
            "mode": "multi_agent",
            "roles": ["main", "onboarding", "scene", "recon", "steward"],
            "readonly_capabilities": ["path_trace", "alert_groups", "notify"],
            "provider": provider_info.provider if provider_info else self._settings.provider,
            "model": provider_info.model if provider_info else self._settings.model,
            "base_url": provider_info.base_url if provider_info else self._settings.base_url,
            "key_configured": bool(provider_info.key_configured) if provider_info else self._settings.has_llm_key,
            "backend_mode": self._toolkit.mode,
            "intent": route.intent if route is not None else None,
            "route_source": route.source if route is not None else "llm",
            "route": route.to_trace() if route is not None else None,
            "plan": plan_trace,
            "subtask_count": len(plan),
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
            "action_ttl_s": self._settings.action_ttl_s,
            "polish": polish_trace,
            "conversation_id": conversation_id,
            "elapsed_ms": elapsed_ms,
            "stop_reason": "final_answer",
        }
        if diagnosis_trace is not None:
            trace["diagnosis"] = {
                "provider": diagnosis_trace.get("provider"),
                "model": diagnosis_trace.get("model"),
                "tool_sequence": diagnosis_trace.get("tool_sequence"),
                "steps": len(diagnosis_trace.get("steps") or []),
                "elapsed_ms": diagnosis_trace.get("elapsed_ms"),
            }
        return trace
