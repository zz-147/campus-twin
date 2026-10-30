# -*- coding: utf-8 -*-
"""AI 运维 Agent 编排循环。

流程（最多 ``max_steps`` 步，默认 6）：

1. 系统提示词（校园网运维专家 / 必须引用真实数据 / 不许编造 / 无数据就说无数据）
2. LLM 决策 → 返回工具调用（可并行多个）或最终回答
3. 执行只读工具（HTTP 真实后端 / fixture 离线快照），把结果作为 tool 消息回填
4. 重复 2-3，直到 LLM 给出最终中文回答或达到步数上限
5. 输出 trace：provider/model、每步耗时、工具调用序列、错误数（不含任何密钥）

输出结构固定为「根因 / 依据 / 排障步骤 / 建议命令」，由系统提示词约束。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.agent.providers.base import AssistantMessage, LLMProvider, LLMProviderError
from app.agent.tools import OpsToolkit

SYSTEM_PROMPT = """你是 campus-twin 校园网数字孪生平台的 AI 运维专家，服务于学校网络运维团队。

你的任务：用户用自然语言描述网络现象（如“教学楼网络为什么卡”“哪个设备掉线了”“一号机房带宽为什么高”），
你通过调用只读工具查询后端真实数据，定位根因并给出可执行的排障方案。

后端工具能提供（全部为真实采集/检测数据）：
- get_health：后端服务状态 + 网络健康总览（设备/客户端健康度、采集是否 stale）
- get_devices：受管网络设备（hostname、管理 IP、reachability、collectionStatus、平台型号）
- get_topology：拓扑节点与链路（两端端口、link_status）
- get_hosts：终端主机（IP、ping_status、接入交换机/端口、VLAN）
- get_issues：Network Controller 上报的活跃健康问题
- get_alerts：后端检测引擎产生的告警（device_offline / link_down / host_down / health_issue 等）
- get_device_detail：单台设备详情（关联主机、告警、指标摘要）
- get_metrics：设备指标时序峰值（CPU/内存/接口利用率/流量）

回答必须遵守：
1. 先调用工具获取数据，再下结论；至少调用 3 个工具（全局态势 + 故障明细 + 目标设备详情/指标）。
2. 根因必须定位到具体设备名或主机名，能定位到端口/IP 时一并给出。
3. “依据”部分必须引用工具返回的真实字段值（设备名、管理 IP、ping 状态、端口、告警标题/时间等），
   禁止编造任何设备、IP、端口、时间或指标数值。
4. 如果工具返回无数据、后端不可达或数据 stale，必须明确说明“后端暂无数据/数据不可用”，不要猜测性编造。
5. 时间引用优先使用 observed_at / created_at（采集时宿主机时间）；NC 的 source_ts 是仿真设备时钟，可有偏差。
6. 回答使用中文 Markdown，固定四段：## 根因、## 依据、## 排障步骤、## 建议命令（Cisco IOS 命令）。
7. 不要向用户复述工具调用过程，不要输出 JSON 或内部 trace。"""

STARTED_FALLBACK = (
    "抱歉，我未能在限定步数内得出诊断结论。已调用的工具：{tools}。"
    "请补充故障现象（哪个楼栋/设备/终端、什么时间开始）后重试。"
)


@dataclass
class AgentRunResult:
    """一次 Agent 运行的完整结果（API 直接序列化，密钥零暴露）。"""

    answer: str
    trace: Dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "answer": self.answer,
            "trace": self.trace,
            "provider": self.provider,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
        }


class OpsAgent:
    """编排循环：LLM ↔ 只读工具 ↔ 最终中文回答。"""

    def __init__(
        self,
        provider: LLMProvider,
        toolkit: OpsToolkit,
        max_steps: int = 6,
        max_tool_calls_per_step: int = 8,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._provider = provider
        self._toolkit = toolkit
        self._max_steps = max(1, int(max_steps))
        self._max_tool_calls_per_step = max(1, int(max_tool_calls_per_step))
        self._system_prompt = system_prompt
        self._tool_schemas = toolkit.schemas()

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #
    @property
    def provider(self) -> LLMProvider:
        return self._provider

    @property
    def toolkit(self) -> OpsToolkit:
        return self._toolkit

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(
        self,
        question: str,
        history: Optional[List[Dict[str, str]]] = None,
        knowledge_context: str = "",
    ) -> AgentRunResult:
        question = (question or "").strip()
        if not question:
            raise ValueError("问题不能为空")

        system_prompt = self._system_prompt
        if (knowledge_context or "").strip():
            system_prompt = (
                system_prompt
                + "\n\n"
                + str(knowledge_context).strip()
                + "\n\n引用规范：只允许引用以上 [K1].. 原文事实；无相关内容时明确说“知识库无相关内容”，"
                "不得编造配置/参数/密码。"
            )
        info = self._provider.info()
        started = time.perf_counter()
        start_wall = time.time()
        history_messages: List[Dict[str, Any]] = []
        for message in (history or [])[-12:]:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            content = str(message.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                history_messages.append({"role": role, "content": content})
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]
        messages.extend(history_messages)
        messages.append({"role": "user", "content": question})
        trace: Dict[str, Any] = {
            "question": question,
            "history_messages": len(history_messages),
            "provider": info.provider,
            "model": info.model,
            "base_url": info.base_url,
            "key_configured": info.key_configured,
            "backend_mode": self._toolkit.mode,
            "max_steps": self._max_steps,
            "started_at": start_wall,
            "steps": [],
            "tool_sequence": [],
            "tool_call_count": 0,
            "tool_error_count": 0,
            "tool_errors": [],
            "stop_reason": None,
        }

        answer = ""
        stop_reason = "max_steps"
        for step_index in range(self._max_steps):
            force_final = step_index == self._max_steps - 1
            tools = None if force_final else self._tool_schemas
            step_started = time.perf_counter()
            # provider 异常（网络/鉴权/限流）向上抛出，由 API 层转成 502 中文提示
            assistant = self._provider.chat(messages, tools=tools)
            step_trace: Dict[str, Any] = {
                "step": step_index + 1,
                "provider_elapsed_ms": round((time.perf_counter() - step_started) * 1000.0, 2),
                "tools_offered": len(tools or []),
                "tool_calls": [],
                "content_chars": len(assistant.content or ""),
                "finish_reason": assistant.finish_reason,
            }

            if assistant.tool_calls and tools:
                messages.append(_assistant_message(assistant))
                executed = 0
                for call in assistant.tool_calls:
                    if executed >= self._max_tool_calls_per_step:
                        step_trace["tool_calls"].append(
                            {
                                "name": call.name,
                                "skipped": True,
                                "reason": "单步工具调用数超过上限 %d" % self._max_tool_calls_per_step,
                            }
                        )
                        continue
                    if call.parse_error:
                        envelope: Dict[str, Any] = {
                            "ok": False,
                            "tool": call.name,
                            "summary": "",
                            "data": None,
                            "error": "工具参数解析失败：%s" % call.parse_error,
                            "elapsed_ms": 0.0,
                        }
                    else:
                        envelope = self._toolkit.call(call.name, call.arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id or ("call_%d_%d" % (step_index + 1, executed + 1)),
                            "content": json.dumps(envelope, ensure_ascii=False, default=str),
                        }
                    )
                    trace["tool_sequence"].append(call.name)
                    trace["tool_call_count"] += 1
                    if not envelope.get("ok"):
                        trace["tool_error_count"] += 1
                        trace["tool_errors"].append(
                            {
                                "tool": call.name,
                                "error": str(envelope.get("error") or "")[:300],
                            }
                        )
                    step_trace["tool_calls"].append(
                        {
                            "name": call.name,
                            "arguments": _safe_arguments(call.arguments),
                            "ok": bool(envelope.get("ok")),
                            "elapsed_ms": envelope.get("elapsed_ms"),
                            "result_chars": len(
                                json.dumps(envelope.get("data"), ensure_ascii=False, default=str)
                            ),
                            "error": (
                                None
                                if envelope.get("ok")
                                else str(envelope.get("error") or "")[:300]
                            ),
                        }
                    )
                    executed += 1
                trace["steps"].append(step_trace)
                continue

            answer = (assistant.content or "").strip()
            if force_final and assistant.tool_calls:
                step_trace["ignored_tool_calls"] = len(assistant.tool_calls)
            trace["steps"].append(step_trace)
            if answer:
                stop_reason = "final_answer"
                break
            stop_reason = "empty_response"

        if not answer:
            answer = self._fallback_answer(trace, stop_reason)

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        trace["stop_reason"] = stop_reason
        trace["elapsed_ms"] = elapsed_ms
        return AgentRunResult(
            answer=answer,
            trace=trace,
            provider=info.provider,
            model=info.model,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------ #
    # 兜底回答（模型在步数内未给出内容时）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fallback_answer(trace: Dict[str, Any], stop_reason: str) -> str:
        tools = "、".join(dict.fromkeys(trace.get("tool_sequence") or [])) or "无"
        errors = trace.get("tool_errors") or []
        lines = [
            "## 根因",
            "未能生成最终诊断结论（%s）。" % (
                "已达到最大步数" if stop_reason == "max_steps" else "模型返回空内容"
            ),
            "",
            "## 依据",
            "- 已调用工具：%s" % tools,
        ]
        if errors:
            lines.append(
                "- 其中 %d 次工具调用失败，例如：%s"
                % (len(errors), str(errors[0].get("error") or "")[:200])
            )
        lines.extend(
            [
                "",
                "## 排障步骤",
                "1. 确认后端服务与采集状态正常（/api/health，stale=false）。",
                "2. 缩小故障范围：明确楼栋/设备/终端与发生时间后重试。",
                "3. 如工具持续失败，检查后端 http://127.0.0.1:8000 是否可达。",
                "",
                "## 建议命令",
                "```",
                "curl http://127.0.0.1:8000/api/health",
                "curl http://127.0.0.1:8000/api/devices",
                "```",
            ]
        )
        return "\n".join(lines)


def _assistant_message(assistant: AssistantMessage) -> Dict[str, Any]:
    """把 AssistantMessage 还原成 OpenAI 协议 assistant 消息（含 tool_calls）。"""
    return assistant.to_openai_message()


def _safe_arguments(arguments: Any) -> Dict[str, Any]:
    """trace 里的工具参数只保留短标量，避免把超长内容写进 trace。"""
    if not isinstance(arguments, dict):
        return {}
    safe: Dict[str, Any] = {}
    for key, value in list(arguments.items())[:12]:
        text = str(value)
        safe[str(key)] = text[:200] if len(text) > 200 else value
    return safe
