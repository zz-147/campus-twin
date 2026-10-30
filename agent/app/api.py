# -*- coding: utf-8 -*-
"""AI 运维 Agent 的 FastAPI 服务（:8100；W9 多 Agent + W10 只读查询 + W11 LangGraph）。

接口一览：
- ``POST /api/chat``                 {question, conversation_id?, provider?, model?}
  → {answer, trace, provider, model, elapsed_ms, conversation_id, actions[]}
  （内部按 ``AGENT_ENGINE`` 跑 LangGraph 图或 classic Orchestrator；interrupt 时返回 pending actions）
- ``GET  /api/health``               服务/provider/后端数据源/W9-W11 健康状态（含 engine/graph，绝不返回 key）
- ``GET  /api/providers``            provider 能力清单（切换方式说明）
- ``GET  /api/roles``                主 Agent + 4 子 Agent、W10 只读能力、图节点与工具白名单
- ``GET  /api/conversations``        会话列表；``GET/DELETE /api/conversations/{id}``
- ``POST /api/conversations/clear``  清空会话（可保留当前会话）
- ``GET  /api/actions/{id}``         查询 action 状态
- ``GET  /api/actions/pending``      待确认 action 列表
- ``POST /api/actions/{id}/confirm`` 用户确认后执行（LangGraph 先 Command(resume=...) 恢复图，
  再带 X-Operator: agent 写后端）
- ``POST /api/actions/{id}/cancel``  取消（LangGraph 同样恢复图走 cancel 分支）
- ``POST /api/chat/stream``          W12 SSE 流式（text/event-stream：meta/status/tool/token/action/done/error）
- ``GET  /api/eval/scenarios``       38 场景评测集
- ``GET  /api/eval/report``          最近一次评测报告

启动（在 campus-twin/agent 目录）：
    LLM_PROVIDER=mock AGENT_ENGINE=langgraph ..\\.venv\\Scripts\\python.exe -m uvicorn app.api:app --port 8100
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import __version__
from app.agent.actions import (
    ACTION_LABELS,
    ACTION_TTL_S,
    ACTION_TYPES,
    ActionError,
    ActionExecutor,
    ActionStore,
)
from app.agent.backend import BackendError, FixtureBackendClient, HttpBackendClient
from app.agent.conversations import ConversationStore
from app.agent.eval.fixture_data import (
    SCENARIOS_PATH,
    build_scenario_snapshot,
    load_base_snapshot,
    load_scenarios,
)
from app.agent.mcp_client import MCPBridgeClient, probe_mcp_bridge, tool_names as mcp_tool_names
from app.agent.observability import TraceStore, record_turn
from app.agent.providers.base import LLMProviderError, ProviderConfigError
from app.agent.resilience import ResilienceConfig, ResilientExecutor
from app.agent.providers.factory import build_provider_from_settings, provider_catalog
from app.agent.rag.retriever import retrieval_health
from app.agent.roles.orchestrator import (
    ConversationNotFoundError,
    Orchestrator,
    ProviderUnavailableError,
)
from app.agent.routing import ROUTE_RULE_NAMES
from app.agent.streaming import stream_chat, streaming_health
from app.agent.tools import ALL_TOOL_NAMES, DIAGNOSIS_TOOL_NAMES, OpsToolkit
from app.config import AgentSettings

logger = logging.getLogger("campus_twin.agent.api")

REPORT_PATH = Path(__file__).resolve().parent / "agent" / "eval" / "report.json"
RAG_REPORT_PATH = Path(__file__).resolve().parent / "agent" / "eval" / "rag_report.json"

ROLE_WHITELISTS: Dict[str, List[str]] = {
            "main": ["diagnosis(8 后端只读 + 5 MCP 只读) + 路由/汇总"],
    "onboarding": [
        "get_devices",
        "get_topology",
        "get_hosts",
        "get_scene",
        "get_device_config_template",
        "get_new_devices",
        "mcp_list_tools",
        "mcp_list_dir",
        "mcp_read_file",
        "mcp_search_files",
        "mcp_memory_search",
    ],
    "scene": ["get_scene"],
    "recon": ["get_devices", "get_topology", "get_hosts", "get_scene", "get_new_devices"],
    "steward": ["get_alerts", "get_audit"],
}


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="自然语言运维问题")
    conversation_id: Optional[str] = Field(
        default=None, description="续聊会话 id；不传则新建会话"
    )
    provider: Optional[str] = Field(
        default=None,
        description="可选：本次请求临时覆盖 provider（deepseek/openai/ollama/openai_compat/mock）",
    )
    model: Optional[str] = Field(default=None, description="可选：本次请求临时覆盖模型名")


class ChatResponse(BaseModel):
    answer: str
    trace: Dict[str, Any]
    provider: str
    model: str
    elapsed_ms: float
    conversation_id: Optional[str] = None
    actions: List[Dict[str, Any]] = Field(default_factory=list)


class ConversationClearRequest(BaseModel):
    keep_current: Optional[str] = Field(
        default=None, description="可选：保留该会话（通常是当前会话 id）"
    )


def create_app(settings: Optional[AgentSettings] = None) -> FastAPI:
    """应用工厂（测试可传入独立 settings；uvicorn 使用默认环境变量配置）。"""
    base_settings = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = base_settings or AgentSettings.from_env()
        config.ensure_dirs()
        app.state.settings = config
        app.state.backend_error = None
        app.state.provider_error = None
        app.state.provider = None

        # 1) 数据源：真实后端 HTTP（默认）或离线 fixture
        if config.data_source == "fixture":
            try:
                snapshot = _load_fixture_snapshot(config)
                app.state.backend = FixtureBackendClient(snapshot)
            except Exception as exc:  # noqa: BLE001 - 启动失败也要能返回健康状态
                app.state.backend = FixtureBackendClient({})
                app.state.backend_error = "fixture 加载失败：%s" % exc
        else:
            app.state.backend = HttpBackendClient(config.backend_base_url, config.backend_timeout_s)
        app.state.toolkit = OpsToolkit(
            app.state.backend,
            mcp_bridge=MCPBridgeClient(config.mcp_bridge_base, config.mcp_bridge_timeout_s),
            resilience=ResilientExecutor(
                ResilienceConfig(max_retries=2, retry_delays=(1.0, 3.0))
            ),
        )
        app.state.mcp_bridge = app.state.toolkit._mcp_bridge

        # W15-P1：自建 trace 可观测性（SQLite trace_events，与 agent.db 同库）
        app.state.traces = TraceStore(config.db_path)

        # 2) W9：会话 / action 存储 + 执行器
        app.state.conversations = ConversationStore(config.db_path)
        app.state.actions = ActionStore(config.db_path, ttl_s=config.action_ttl_s)
        app.state.executor = ActionExecutor(app.state.backend, app.state.conversations)

        # 3) LLM provider（mock 永远可用；真实 provider 缺 key 时记录错误，服务本身仍启动）
        try:
            app.state.provider = build_provider_from_settings(config)
        except ProviderConfigError as exc:
            app.state.provider_error = str(exc)
            logger.warning("LLM provider 未就绪：%s", exc)
            app.state.provider = None

        # 4) 编排引擎：W11 LangGraph（默认）或 classic（可回退）
        app.state.engine, app.state.engine_error = _build_engine(
            config=config,
            provider=app.state.provider,
            toolkit=app.state.toolkit,
            conversations=app.state.conversations,
            actions=app.state.actions,
            executor=app.state.executor,
        )
        app.state.orchestrator = app.state.engine  # 兼容旧引用（classic 时就是 Orchestrator）
        app.state.engine_name = getattr(app.state.engine, "engine_name", "classic")

        logger.info(
            "Agent 服务启动：engine=%s provider=%s model=%s provider_ready=%s data_source=%s backend=%s db=%s",
            app.state.engine_name,
            config.provider,
            config.model,
            app.state.provider is not None,
            config.data_source,
            config.backend_base_url,
            config.db_path,
        )
        try:
            yield
        finally:
            close = getattr(app.state.engine, "close", None)
            if callable(close):
                close()
            backend_close = getattr(app.state.backend, "close", None)
            if callable(backend_close):
                backend_close()

    app = FastAPI(
        title="Campus Twin AI Ops Agent",
        version=__version__,
        description=(
            "校园网数字孪生平台的 AI 运维 Agent：1 主 Agent + 4 专职子 Agent、"
            "provider 可切换（默认 DeepSeek）、action 确认协议、会话持久化。"
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    @app.middleware("http")
    async def _ensure_utf8_json_charset(request, call_next):
        """给 JSON 响应补 ``charset=utf-8``（Windows Invoke-WebRequest 中文不乱码）。"""
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("application/json") and "charset" not in content_type.lower():
            response.headers["content-type"] = content_type + "; charset=utf-8"
        return response

    # ------------------------------------------------------------------ #
    # 元信息
    # ------------------------------------------------------------------ #
    @app.get("/", tags=["meta"])
    def root() -> Dict[str, Any]:
        return {
            "name": "Campus Twin AI Ops Agent",
            "version": __version__,
            "architecture": "1 main agent + 4 specialist sub-agents",
            "endpoints": [
                "/api/chat (POST)",
                "/api/chat/stream (POST, SSE)",
                "/api/health",
                "/api/providers",
                "/api/roles",
                "/api/conversations",
                "/api/conversations/{id}",
                "/api/conversations/clear (POST)",
                "/api/actions/{id}",
                "/api/actions/pending",
                "/api/actions/{id}/confirm (POST)",
                "/api/actions/{id}/cancel (POST)",
                "/api/eval/scenarios",
                "/api/eval/report",
            ],
            "docs": "/docs",
        }

    @app.get("/api/health", tags=["meta"])
    def health() -> Dict[str, Any]:
        config: AgentSettings = app.state.settings
        provider = app.state.provider
        info = provider.info() if provider is not None else None
        conversations = app.state.conversations
        actions = app.state.actions
        return {
            "status": "ok",
            "service": "campus-twin-agent",
            "version": __version__,
            "provider": info.provider if info else config.provider,
            "model": info.model if info else config.model,
            "base_url": info.base_url if info else config.base_url,
            "key_configured": bool(info.key_configured) if info else config.has_llm_key,
            "provider_ready": provider is not None,
            "provider_error": app.state.provider_error,
            "data_source": config.data_source,
            "backend": _backend_health(app.state.backend),
            "w9_backend": _w9_backend_health(app.state.backend),
            "w10_backend": _w10_backend_health(app.state.backend),
            "engine": app.state.engine_name,
            "engine_error": app.state.engine_error,
            "graph": _engine_info(app.state.engine),
            "streaming": streaming_health(),
            "rag": retrieval_health(),
            "mcp_bridge": probe_mcp_bridge(app.state.mcp_bridge),
            "observability": {
                "storage": "sqlite:trace_events",
                "trace_count": app.state.traces.count(),
                "endpoints": [
                    "/api/traces",
                    "/api/traces/{trace_id}",
                    "/api/metrics/agent",
                    "/api/rag/report",
                ],
            },
            "conversations": conversations.count(),
            "pending_actions": actions.count("pending"),
            "action_ttl_s": config.action_ttl_s,
            "max_steps": config.max_steps,
            "roles": ["main", "onboarding", "scene", "recon", "steward"],
        }

    @app.get("/api/providers", tags=["meta"])
    def providers() -> Dict[str, Any]:
        return provider_catalog()

    @app.get("/api/roles", tags=["meta"])
    def roles() -> Dict[str, Any]:
        return {
            "main": {
                "label": "主 Agent",
                "duty": "意图识别 → 拆任务（≤3）→ 派给子 Agent/只读查询 → 汇总 answer + actions",
                "tools": ["排障时复用诊断 Agent（8 个只读工具）"],
            },
            "readonly_capabilities": [
                {
                    "name": "path_trace",
                    "label": "路径追踪",
                    "tools": ["get_hosts", "trace_path"],
                    "note": "从问题或 get_hosts 反查源/目的 IP；提取不到就回问用户，不猜测",
                },
                {
                    "name": "alert_groups",
                    "label": "告警根因分组",
                    "tools": ["get_alert_groups"],
                    "note": "根因分组 + 上游抑制统计；接口未就绪时回退 get_alerts 并诚实说明",
                },
                {
                    "name": "notify",
                    "label": "通知状态",
                    "tools": ["get_notify_status"],
                    "note": "只读查询 Webhook 状态；发送测试通知属写操作，本期不生成 action",
                },
            ],
            "sub_agents": [
                {"name": "onboarding", "label": "接入引导 Agent", "tools": ROLE_WHITELISTS["onboarding"]},
                {"name": "scene", "label": "场景构建 Agent", "tools": ROLE_WHITELISTS["scene"]},
                {"name": "recon", "label": "接入侦察 Agent", "tools": ROLE_WHITELISTS["recon"]},
                {"name": "steward", "label": "数据治理 Agent", "tools": ROLE_WHITELISTS["steward"]},
            ],
            "inference_rules": [
                "1. 拓扑邻居楼栋",
                "2. hostname 关键词（JX/SX/SS/CORE）",
                "3. 管理 IP / VLAN 段映射",
                "4. 询问用户",
                "5. 新楼栋（用户确认后由场景 Agent 生成）",
            ],
            "action_types": list(ACTION_TYPES),
            "action_labels": ACTION_LABELS,
            "diagnosis_tools": list(DIAGNOSIS_TOOL_NAMES),
            "all_tools": list(ALL_TOOL_NAMES),
            "engine": app.state.engine_name,
            "graph": _engine_info(app.state.engine),
            "knowledge": {
                "tool": "search_knowledge",
                "retriever": "bm25-lite-bigram",
                "citations": "[K1]..[Kn]",
                "auto_inject_roles": ["diagnosis", "onboarding", "path_trace", "alert_groups"],
            },
            "streaming": {"endpoint": "/api/chat/stream", "transport": "sse"},
            "mcp": {
                "bridge": app.state.settings.mcp_bridge_base,
                "transport": "Agent(py3.9) → HTTP → MCP Bridge(py3.14) → stdio → filesystem/memory servers",
                "tools": mcp_tool_names(),
                "diagnosis_whitelist": [
                    name for name in DIAGNOSIS_TOOL_NAMES if name.startswith("mcp_")
                ],
                "filesystem_allowed_dirs": ["campus-twin/configs", "campus-twin/docs"],
                "note": "filesystem 只读；memory add 仅本地知识图谱写入，不是网络设备写操作",
            },
            "routing": {
                "rule_first": True,
                "route_source": ["rule", "llm"],
                "rules": list(ROUTE_RULE_NAMES),
                "priority": [
                    "write_clear_alerts",
                    "write_silence_alerts",
                    "write_conversations",
                    "path",
                    "alert_groups",
                    "notify",
                    "recon",
                    "onboarding",
                ],
                "note": "规则命中时在 LLM 之前确定性短路（trace.route_source=rule），未命中才回退",
            },
        }

    # ------------------------------------------------------------------ #
    # 对话
    # ------------------------------------------------------------------ #
    @app.post("/api/chat", response_model=ChatResponse, tags=["chat"])
    async def chat(payload: ChatRequest) -> Dict[str, Any]:
        config: AgentSettings = app.state.settings
        engine = app.state.engine
        temp_engine = None
        trace_id = app.state.traces.new_trace_id()

        # 运行时临时切换 provider/model（不修改环境变量，便于演示「随时换模型」）
        if payload.provider or payload.model:
            try:
                provider = build_provider_from_settings(
                    config, payload.provider, payload.model
                )
            except ProviderConfigError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            if hasattr(engine, "with_provider"):
                temp_engine = engine.with_provider(provider)
                engine = temp_engine
            else:
                temp_engine = Orchestrator(
                    provider=provider,
                    toolkit=app.state.toolkit,
                    conversations=app.state.conversations,
                    actions=app.state.actions,
                    settings=config,
                )
                engine = temp_engine

        try:
            outcome = await run_in_threadpool(
                engine.handle, payload.question, payload.conversation_id
            )
        except ConversationNotFoundError:
            raise HTTPException(
                status_code=404,
                detail="会话不存在：%s（请新建会话或省略 conversation_id）"
                % payload.conversation_id,
            ) from None
        except ProviderUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except LLMProviderError as exc:
            # provider 内部已脱敏，不含 API key
            raise HTTPException(status_code=502, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        finally:
            close = getattr(temp_engine, "close", None)
            if callable(close):
                close()
        result = outcome.to_dict()
        try:
            record_turn(
                app.state.traces,
                trace_id=trace_id,
                conversation_id=result.get("conversation_id"),
                question=payload.question,
                payload=result,
                engine=getattr(engine, "engine_name", "classic"),
                provider=payload.provider or config.provider,
                model=payload.model or config.model,
                latency_ms=float(result.get("elapsed_ms") or 0.0),
            )
        except Exception as exc:  # noqa: BLE001 - trace 失败不影响主流程
            logger.warning("trace 写入失败：%s", exc)
        return result

    # ------------------------------------------------------------------ #
    # SSE 流式对话（W12；事件契约见 app/agent/streaming.py）
    # ------------------------------------------------------------------ #
    @app.post("/api/chat/stream", tags=["chat"])
    async def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
        config: AgentSettings = app.state.settings
        engine = app.state.engine
        temp_engine = None
        provider = app.state.provider

        if payload.provider or payload.model:
            try:
                provider = build_provider_from_settings(
                    config, payload.provider, payload.model
                )
            except ProviderConfigError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            if hasattr(engine, "with_provider"):
                temp_engine = engine.with_provider(provider)
                engine = temp_engine
            else:
                temp_engine = Orchestrator(
                    provider=provider,
                    toolkit=app.state.toolkit,
                    conversations=app.state.conversations,
                    actions=app.state.actions,
                    settings=config,
                )
                engine = temp_engine

        trace_id = app.state.traces.new_trace_id()
        started = time.perf_counter()

        async def event_source() -> Any:
            done_payload: Optional[Dict[str, Any]] = None
            try:
                async for event in stream_chat(
                    engine,
                    payload.question,
                    payload.conversation_id,
                    provider=provider,
                ):
                    if await request.is_disconnected():
                        break
                    if event.get("type") == "done":
                        done_payload = dict(event)
                        event = done_payload
                    yield "data: %s\n\n" % json.dumps(event, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001 - SSE 错误只发 error 事件
                message = config.sanitize("%s: %s" % (type(exc).__name__, exc))
                yield "data: %s\n\n" % json.dumps(
                    {"type": "error", "message": "流式对话异常：%s" % message},
                    ensure_ascii=False,
                )
            finally:
                if done_payload is not None:
                    try:
                        record_turn(
                            app.state.traces,
                            trace_id=trace_id,
                            conversation_id=done_payload.get("conversation_id")
                            or payload.conversation_id,
                            question=payload.question,
                            payload=done_payload,
                            engine=getattr(engine, "engine_name", "classic"),
                            provider=payload.provider or config.provider,
                            model=payload.model or config.model,
                            latency_ms=float(
                                done_payload.get("elapsed_ms")
                                or (time.perf_counter() - started) * 1000.0
                            ),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("stream trace 写入失败：%s", exc)
                close = getattr(temp_engine, "close", None)
                if callable(close):
                    close()

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------ #
    # 会话持久化
    # ------------------------------------------------------------------ #
    @app.get("/api/conversations", tags=["conversations"])
    def list_conversations(limit: int = Query(default=50, ge=1, le=200)) -> Dict[str, Any]:
        conversations = app.state.conversations.list(limit=limit)
        return {"count": len(conversations), "conversations": conversations}

    @app.post("/api/conversations/clear", tags=["conversations"])
    def clear_conversations(payload: Optional[ConversationClearRequest] = None) -> Dict[str, Any]:
        keep_current = payload.keep_current if payload else None
        deleted = app.state.conversations.clear(keep_id=keep_current)
        return {"deleted": deleted, "kept": keep_current}

    @app.get("/api/conversations/{conversation_id}", tags=["conversations"])
    def get_conversation(conversation_id: str) -> Dict[str, Any]:
        conversation = app.state.conversations.get(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在：%s" % conversation_id)
        return conversation

    @app.delete("/api/conversations/{conversation_id}", tags=["conversations"])
    def delete_conversation(conversation_id: str) -> Dict[str, Any]:
        deleted = app.state.conversations.delete(conversation_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="会话不存在：%s" % conversation_id)
        return {"deleted": True, "conversation_id": conversation_id}

    # ------------------------------------------------------------------ #
    # Action 确认协议（Agent 不直接执行写操作）
    # ------------------------------------------------------------------ #
    @app.get("/api/actions/pending", tags=["actions"])
    def pending_actions(
        conversation_id: Optional[str] = Query(default=None)
    ) -> Dict[str, Any]:
        actions = app.state.actions.list_pending(conversation_id=conversation_id)
        return {"count": len(actions), "actions": actions}

    @app.get("/api/actions/{action_id}", tags=["actions"])
    def get_action(action_id: str) -> Dict[str, Any]:
        action = app.state.actions.get(action_id)
        if action is None:
            raise HTTPException(status_code=404, detail="操作不存在或已被清理：%s" % action_id)
        return action

    @app.post("/api/actions/{action_id}/confirm", tags=["actions"])
    def confirm_action(action_id: str) -> Dict[str, Any]:
        try:
            engine = app.state.engine
            if hasattr(engine, "confirm_action"):
                action = engine.confirm_action(action_id)  # LangGraph interrupt resume
            else:
                action = app.state.actions.confirm(
                    action_id, app.state.executor, operator="agent"
                )
        except ActionError as exc:
            _raise_action_error(exc)
        return {"ok": action.get("status") == "executed", "action": action}

    @app.post("/api/actions/{action_id}/cancel", tags=["actions"])
    def cancel_action(action_id: str) -> Dict[str, Any]:
        try:
            engine = app.state.engine
            if hasattr(engine, "cancel_action"):
                action = engine.cancel_action(action_id)  # LangGraph interrupt resume
            else:
                action = app.state.actions.cancel(action_id)
        except ActionError as exc:
            _raise_action_error(exc)
        return {"ok": True, "action": action}

    # ------------------------------------------------------------------ #
    # W15-P1：可观测性（trace / metrics / RAG 报告）
    # ------------------------------------------------------------------ #
    @app.get("/api/traces", tags=["observability"])
    def list_traces(
        conversation_id: Optional[str] = Query(default=None),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> Dict[str, Any]:
        store: TraceStore = app.state.traces
        summaries = store.list_traces(conversation_id, limit=limit)
        items: List[Dict[str, Any]] = []
        for summary in summaries:
            events = store.query_trace(summary["trace_id"], limit=500)
            llm = next((item for item in events if item.get("event") == "llm"), {})
            last_error = summary.get("last_error")
            items.append(
                {
                    "trace_id": summary["trace_id"],
                    "conversation_id": summary.get("conversation_id"),
                    "step": summary.get("events"),
                    "event": "turn",
                    "tool": None,
                    "provider": llm.get("provider"),
                    "model": llm.get("model"),
                    "tokens_in": summary.get("tokens_in") or 0,
                    "tokens_out": summary.get("tokens_out") or 0,
                    "latency_ms": summary.get("total_latency_ms") or 0.0,
                    "ok": bool(summary.get("ok")),
                    "error": last_error,
                    "ts": summary.get("started_at"),
                    "events_count": summary.get("events"),
                    "ended_at": summary.get("ended_at"),
                }
            )
        return {"count": len(items), "items": items, "traces": summaries, "limit": limit}

    @app.get("/api/traces/{trace_id}", tags=["observability"])
    def get_trace(trace_id: str) -> Dict[str, Any]:
        store: TraceStore = app.state.traces
        events = store.query_trace(trace_id, limit=1000)
        if not events:
            raise HTTPException(status_code=404, detail="trace 不存在：%s" % trace_id)
        tool_events = [item for item in events if item.get("event") == "tool"]
        summary = {
            "trace_id": trace_id,
            "conversation_id": events[0].get("conversation_id"),
            "started_at": events[0].get("ts"),
            "ended_at": events[-1].get("ts"),
            "events": len(events),
            "steps": len(events),
            "tool_calls": len(tool_events),
            "tokens_in": sum(int(item.get("tokens_in") or 0) for item in events),
            "tokens_out": sum(int(item.get("tokens_out") or 0) for item in events),
            "latency_ms": round(sum(float(item.get("latency_ms") or 0.0) for item in events), 2),
            "ok": all(bool(item.get("ok")) for item in events),
        }
        return {
            "trace_id": trace_id,
            "summary": summary,
            "steps": events,
            "events": events,
            "count": len(events),
        }

    @app.get("/api/metrics/agent", tags=["observability"])
    def agent_metrics() -> Dict[str, Any]:
        store: TraceStore = app.state.traces
        metrics = store.metrics()
        metrics["calls"] = metrics.get("trace_count", 0)
        return metrics

    @app.get("/api/rag/report", tags=["observability"])
    def rag_report() -> Dict[str, Any]:
        if not RAG_REPORT_PATH.exists():
            raise HTTPException(status_code=404, detail="RAG 评测报告尚未生成")
        try:
            report = json.loads(RAG_REPORT_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="RAG 报告读取失败：%s" % exc) from None
        configs = report.get("configs") or {}
        best = configs.get("hybrid_rerank") or next(iter(configs.values()), {})
        return {
            "available": True,
            "generated_at": report.get("generated_at"),
            "best_strategy": report.get("best_strategy"),
            "query_count": (report.get("queries") or {}).get("count"),
            "corpus_chunks": (report.get("corpus") or {}).get("default_chunks"),
            "recall_at_5": best.get("recall_at_5"),
            "mrr": best.get("mrr"),
            "citation_precision": best.get("citation_precision"),
            "citation_traceability": best.get("citation_traceability"),
            "faithfulness": best.get("faithfulness"),
            "metrics": best,
            "report": report,
        }

    # ------------------------------------------------------------------ #
    # 评测集 / 报告
    # ------------------------------------------------------------------ #
    @app.get("/api/eval/scenarios", tags=["eval"])
    def eval_scenarios() -> Dict[str, Any]:
        document = load_scenarios(SCENARIOS_PATH)
        return {
            "count": len(document.get("scenarios") or []),
            "meta": document.get("meta"),
            "scenarios": document.get("scenarios") or [],
        }

    @app.get("/api/eval/report", tags=["eval"])
    def eval_report() -> Dict[str, Any]:
        if not REPORT_PATH.exists():
            return {
                "available": False,
                "note": "尚未生成评测报告，请先运行：python -m app.agent.eval.run_eval",
            }
        try:
            with open(str(REPORT_PATH), "r", encoding="utf-8") as handle:
                return {"available": True, "report": json_load(handle)}
        except ValueError as exc:
            raise HTTPException(status_code=500, detail="评测报告不是合法 JSON：%s" % exc)

    return app


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _raise_action_error(exc: ActionError) -> None:
    status = {
        "not_found": 404,
        "expired": 410,
        "not_pending": 409,
        "invalid_type": 400,
        "invalid_params": 400,
    }.get(exc.code, 400)
    raise HTTPException(status_code=status, detail=exc.to_dict())


def _load_fixture_snapshot(config: AgentSettings) -> Dict[str, Any]:
    base = load_base_snapshot(config.fixture_snapshot)
    if not config.fixture_scenario:
        return base
    document = load_scenarios()
    for scenario in document.get("scenarios") or []:
        if str(scenario.get("id")) == str(config.fixture_scenario):
            return build_scenario_snapshot(base, scenario)
    raise ValueError(
        "未知的 AGENT_FIXTURE_SCENARIO=%r（可用示例：s01、s15、s21、s26）"
        % config.fixture_scenario
    )


def _backend_health(backend: Any) -> Dict[str, Any]:
    mode = str(getattr(backend, "mode", "http"))
    if mode == "fixture":
        snapshot = getattr(backend, "snapshot", {}) or {}
        devices = ((snapshot.get("devices") or {}).get("devices")) or []
        return {
            "mode": "fixture",
            "base_url": None,
            "reachable": True,
            "note": "离线 fixture 快照（评测/演示模式，不读取实时后端）",
            "devices": len(devices),
        }
    try:
        payload = backend.get("/api/health")
        return {
            "mode": "http",
            "base_url": getattr(backend, "base_url", None),
            "reachable": True,
            "status": payload.get("status"),
            "stale": payload.get("stale"),
            "driver": payload.get("driver"),
            "dataset_source": payload.get("dataset_source"),
            "polls": payload.get("polls"),
            "error": None,
        }
    except BackendError as exc:
        return {
            "mode": "http",
            "base_url": getattr(backend, "base_url", None),
            "reachable": False,
            "error": str(exc)[:300],
        }


def _w9_backend_health(backend: Any) -> Dict[str, Any]:
    probe = getattr(backend, "probe_w9", None)
    if callable(probe):
        try:
            return probe()
        except Exception as exc:  # noqa: BLE001 - 健康探测失败不影响主服务
            return {"available": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"available": False, "error": "backend 未实现 W9 探测"}


def _w10_backend_health(backend: Any) -> Dict[str, Any]:
    probe = getattr(backend, "probe_w10", None)
    if callable(probe):
        try:
            return probe()
        except Exception as exc:  # noqa: BLE001 - 健康探测失败不影响主服务
            return {"available": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"available": False, "error": "backend 未实现 W10 探测"}


def _build_engine(
    config: AgentSettings,
    provider: Any,
    toolkit: Any,
    conversations: Any,
    actions: Any,
    executor: Any,
) -> Tuple[Any, Optional[str]]:
    """按 AGENT_ENGINE 构造编排引擎；LangGraph 不可用时自动回退 classic。"""
    if config.engine == "langgraph":
        try:
            from app.agent.graph.graph import LangGraphEngine

            engine = LangGraphEngine(
                provider=provider,
                toolkit=toolkit,
                conversations=conversations,
                actions=actions,
                settings=config,
                executor=executor,
            )
            return engine, None
        except Exception as exc:  # noqa: BLE001 - 回退 classic，服务不中断
            logger.exception("LangGraph 引擎初始化失败，回退 classic：%s", exc)
            error = "LangGraph 初始化失败，已回退 classic：%s: %s" % (
                type(exc).__name__,
                exc,
            )
            return (
                Orchestrator(
                    provider=provider,
                    toolkit=toolkit,
                    conversations=conversations,
                    actions=actions,
                    settings=config,
                ),
                error,
            )
    return (
        Orchestrator(
            provider=provider,
            toolkit=toolkit,
            conversations=conversations,
            actions=actions,
            settings=config,
        ),
        None,
    )


def _engine_info(engine: Any) -> Dict[str, Any]:
    """健康/角色接口用的引擎信息（classic 引擎返回最小结构）。"""
    info = getattr(engine, "info", None)
    if callable(info):
        try:
            return info()
        except Exception as exc:  # noqa: BLE001 - 信息接口失败不影响健康检查
            return {"engine": "unknown", "error": "%s: %s" % (type(exc).__name__, exc)}
    return {
        "engine": "classic",
        "graph_version": None,
        "checkpointer": None,
        "human_in_the_loop": None,
        "note": "classic = W10 Orchestrator（可回退）",
    }


def json_load(handle: Any) -> Any:
    import json

    return json.load(handle)


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _settings = AgentSettings.from_env()
    uvicorn.run(
        "app.api:app",
        host=_settings.api_host,
        port=_settings.api_port,
        log_level=_settings.log_level.lower(),
    )
