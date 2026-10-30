# -*- coding: utf-8 -*-
"""W12 SSE 流式输出实现（与引擎解耦，classic / langgraph 通用）。

事件契约（每行 ``data: {json}\\n\\n``）：
1. meta(conversation_id/provider/model/engine)
2. status(stage=classify|retrieve|tool|aggregate, label)
3. tool(name/ok/elapsed_ms)
4. token(text) —— 最终回答按增量逐段推送
5. action(action) —— pending action（如有）
6. done(answer/trace/elapsed_ms/conversation_id)
7. error(message)

真实 LLM：LangGraph 诊断节点用 LangChain ``model.stream`` 逐 token 回调，
本模块通过线程安全队列把 token 实时转成 SSE；确定性节点/mock 模式按 10–30 字
切片 + 10–30ms sleep 推送。客户端断开时停止推送并清理回调/任务。
"""
from __future__ import annotations

import asyncio
import inspect
import random
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from app.agent.conversations import title_from_question
from app.agent.rag.retriever import retrieval_health, search as search_knowledge


def _slice_answer(answer: str, seed: int = 7) -> List[str]:
    """把回答切成 10–30 字片段（确定性随机，便于测试复现）。"""
    text = answer or ""
    if not text:
        return []
    rng = random.Random(seed + len(text))
    chunks: List[str] = []
    index = 0
    while index < len(text):
        size = rng.randint(10, 30)
        chunks.append(text[index : index + size])
        index += size
    return chunks


def _sse_chunks_to_tokens(
    answer: str,
    delay_min_ms: int = 10,
    delay_max_ms: int = 30,
    seed: int = 7,
) -> List[Dict[str, Any]]:
    """生成 mock/降级 token 事件（不 sleep，由调用方控制节奏）。"""
    return [{"type": "token", "text": chunk} for chunk in _slice_answer(answer, seed=seed)]


async def stream_chat(
    engine: Any,
    question: str,
    conversation_id: Optional[str] = None,
    provider: Any = None,
) -> AsyncIterator[Dict[str, Any]]:
    """执行一次聊天并以 SSE 事件字典流式返回。

    - engine：``LangGraphEngine`` 或 classic ``Orchestrator``（都实现 ``handle``）；
    - 真实 LLM token：engine 若实现 ``set_token_callback``，在 LangGraph 诊断节点内
      ``model.stream`` 的增量会经队列实时推送；否则最终回答切片推送；
    - 异常转换为 ``error`` 事件，不向上抛（不影响非流式 /api/chat）。
    """
    question = (question or "").strip()
    started = time.perf_counter()
    provider_info = provider.info() if provider is not None else None
    engine_name = str(getattr(engine, "engine_name", "classic"))
    conversations = getattr(engine, "_conversations", None)

    # 会话：流式开始前确定 conversation_id，便于 meta 先发
    if conversation_id:
        if conversations is not None and conversations.get(conversation_id) is None:
            yield {"type": "error", "message": "会话不存在：%s" % conversation_id}
            return
    elif conversations is not None:
        created = conversations.create(title=title_from_question(question))
        conversation_id = created["id"]

    yield {
        "type": "meta",
        "conversation_id": conversation_id,
        "provider": provider_info.provider
        if provider_info
        else str(getattr(engine, "_settings", None).provider if hasattr(engine, "_settings") else "unknown"),
        "model": provider_info.model
        if provider_info
        else str(getattr(engine, "_settings", None).model if hasattr(engine, "_settings") else "unknown"),
        "engine": engine_name,
    }
    yield {"type": "status", "stage": "classify", "label": "正在识别意图"}
    hits = search_knowledge(question)
    yield {
        "type": "status",
        "stage": "retrieve",
        "label": "正在检索知识库（命中 %d 条）" % len(hits),
    }
    if hits:
        yield {
            "type": "tool",
            "name": "search_knowledge",
            "ok": True,
            "elapsed_ms": 0,
        }

    queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
    loop = asyncio.get_running_loop()
    streamed: List[str] = []

    def _on_token(text: str) -> None:
        if not text:
            return
        loop.call_soon_threadsafe(queue.put_nowait, text)

    has_callback = hasattr(engine, "set_token_callback")
    if has_callback:
        try:
            engine.set_token_callback(_on_token)
        except Exception:  # noqa: BLE001 - 回调失败则降级为切片
            has_callback = False

    task: Optional["asyncio.Task[Any]"] = None
    outcome: Any = None
    try:
        task = asyncio.create_task(asyncio.to_thread(engine.handle, question, conversation_id))
        idle_started = time.perf_counter()
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.02)
            except asyncio.TimeoutError:
                if task.done():
                    # 任务结束后把队列剩余 token 全部取出
                    while not queue.empty():
                        item = queue.get_nowait()
                        if item:
                            streamed.append(item)
                            yield {"type": "token", "text": item}
                    break
                if time.perf_counter() - idle_started > 600:  # 安全上限
                    raise TimeoutError("流式执行超时（>600s）")
                continue
            if item is None:
                break
            streamed.append(item)
            yield {"type": "token", "text": item}
        outcome = await task
    except Exception as exc:  # noqa: BLE001 - 流式错误只发 error 事件
        if task is not None and not task.done():
            task.cancel()
        message = str(exc)
        try:
            sanitizer = getattr(getattr(engine, "_settings", None), "sanitize", None)
            if callable(sanitizer):
                message = sanitizer(message)
        except Exception:  # noqa: BLE001
            pass
        yield {"type": "error", "message": "流式对话失败：%s: %s" % (type(exc).__name__, message)}
        return
    finally:
        if has_callback:
            try:
                engine.set_token_callback(None)
            except Exception:  # noqa: BLE001
                pass

    if outcome is None:
        yield {"type": "error", "message": "流式对话失败：未生成回答"}
        return

    trace = outcome.trace or {}
    # 工具事件（按 trace.tool_sequence 去重；ok 参考 tool_error_count 整体情况）
    tool_ok = int(trace.get("tool_error_count") or 0) == 0
    for name in dict.fromkeys(trace.get("tool_sequence") or []):
        if name == "search_knowledge":
            continue
        yield {"type": "tool", "name": name, "ok": tool_ok, "elapsed_ms": 0}
    yield {"type": "status", "stage": "aggregate", "label": "正在生成回答"}

    # token 推送：真实 LLM 已推的 token 作为前缀；剩余部分补推；否则整段切片
    answer = outcome.answer or ""
    prefix = "".join(streamed)
    suffix = ""
    if not prefix:
        suffix = answer
    elif answer.startswith(prefix):
        suffix = answer[len(prefix) :]
    else:
        # 极少数情况（如 polish 改写）：不再重复推送模型 token，改为补推完整答案差异
        suffix = ""
    if suffix:
        for chunk in _slice_answer(suffix):
            yield {"type": "token", "text": chunk}
            await asyncio.sleep(random.Random(len(chunk) + len(answer)).uniform(0.010, 0.030))
    elif not prefix:
        # 空回答兜底
        if answer:
            yield {"type": "token", "text": answer}

    for action in outcome.actions or []:
        yield {"type": "action", "action": action}

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    yield {
        "type": "done",
        "answer": answer,
        "trace": trace,
        "elapsed_ms": elapsed_ms,
        "conversation_id": outcome.conversation_id or conversation_id,
    }


def streaming_health() -> Dict[str, Any]:
    """健康检查：SSE 流式能力描述。"""
    return {
        "endpoint": "/api/chat/stream",
        "transport": "sse",
        "event_types": ["meta", "status", "tool", "token", "action", "done", "error"],
        "real_llm_tokens": "LangGraph diagnosis node uses LangChain model.stream -> SSE",
        "mock_fallback": "10-30 chars slices with 10-30ms sleep",
        "disconnect_safe": True,
        "rag": retrieval_health(),
    }
