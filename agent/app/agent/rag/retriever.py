# -*- coding: utf-8 -*-
"""轻量 RAG 检索器：search / format_context / citation / 无命中规范。

- 单例内存索引（内置 corpus，无网络、无向量库）；
- ``[K1]..[Kn]`` 引用编号按本轮检索命中顺序生成；
- 提供注入系统提示的上下文、答案引用块、无命中提示、retrieval trace 记录；
- 严格“只引用检索结果原文”，无命中必须明说“知识库无相关内容”。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from app.agent.rag.corpus import chunk_count, get_chunks
from app.agent.rag.index import BM25LiteIndex

#: 检索默认参数（top_k=5 与 W12 契约一致）
DEFAULT_TOP_K = 5
#: 最低相关分（低于该值视为无相关内容；避免“食堂/今天”等常见字误命中）
MIN_SCORE = 10.0

#: 知识型问题标志（无命中时用于明确回答“知识库无相关内容”）
_KNOWLEDGE_MARKERS = (
    "知识库",
    "文档",
    "手册",
    "是什么",
    "是多少",
    "多少",
    "密码",
    "怎么配",
    "如何配",
    "配置",
    "参数",
    "规划",
    "为什么",
    "原理",
    "步骤",
    "命令",
)
#: 故障现象标志（有这些词时即使无 KB 命中，也走实时工具排障，而不是直接回“无知识”）
_FAULT_MARKERS = (
    "离线",
    "掉线",
    "不通",
    "中断",
    "异常",
    "故障",
    "ping",
    "断了",
    "不可达",
    "上不了网",
    "卡",
    "风暴",
    "丢包",
    "抖动",
    "down",
    "宕",
    "访问不了",
)


@lru_cache(maxsize=1)
def get_index() -> BM25LiteIndex:
    """单例 BM25-lite 索引（只读内置 corpus）。"""
    return BM25LiteIndex(get_chunks())


def search(query: str, top_k: int = DEFAULT_TOP_K) -> List[Dict[str, Any]]:
    """检索内置知识库；无命中返回 []（绝不返回编造内容）。"""
    if not (query or "").strip():
        return []
    return get_index().search(query, top_k=max(1, int(top_k)), min_score=MIN_SCORE)


def format_context(hits: List[Dict[str, Any]]) -> str:
    """生成注入系统提示的知识上下文（[K1] title（source）+ 原文）。"""
    if not hits:
        return ""
    lines = [
        "以下是本项目内部知识库检索结果（仅可使用这些原文事实，回答必须引用 [K1]..；"
        "无相关内容就明确说“知识库无相关内容”，禁止编造）："
    ]
    for rank, hit in enumerate(hits, start=1):
        lines.append(
            "[K%d] %s（来源：%s）\n%s"
            % (rank, hit.get("title"), hit.get("source"), hit.get("text"))
        )
    return "\n\n".join(lines)


def format_citations(hits: List[Dict[str, Any]], snippet_chars: int = 180) -> str:
    """生成追加在答案末尾的“知识库引用”块；返回空字符串表示无命中。"""
    if not hits:
        return ""
    lines = ["", "## 知识库引用"]
    for rank, hit in enumerate(hits, start=1):
        snippet = str(hit.get("text") or "").strip().replace("\n", " ")
        if len(snippet) > snippet_chars:
            snippet = snippet[:snippet_chars] + "…"
        lines.append(
            "[K%d] %s（%s）\n> %s" % (rank, hit.get("title"), hit.get("source"), snippet)
        )
    return "\n".join(lines)


def citations_record(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """trace.retrieval 中的命中摘要（不含长正文，避免 trace 过大）。"""
    return [
        {
            "rank": rank,
            "id": hit.get("id"),
            "score": hit.get("score"),
            "title": hit.get("title"),
            "source": hit.get("source"),
            "citation": "[K%d]" % rank,
        }
        for rank, hit in enumerate(hits, start=1)
    ]


def retrieval_record(
    role: str,
    query: str,
    hits: List[Dict[str, Any]],
    injected: bool = True,
) -> Dict[str, Any]:
    """统一的 trace.retrieval 记录。"""
    return {
        "role": role,
        "query": query,
        "top_k": DEFAULT_TOP_K,
        "injected": bool(injected),
        "hit_count": len(hits),
        "hits": citations_record(hits),
    }


def is_knowledge_query(question: str) -> bool:
    """判断是否是“知识型问题”（用于无命中时明确回答，而不是硬套排障）。"""
    text = (question or "").lower()
    if not any(marker.lower() in text for marker in _KNOWLEDGE_MARKERS):
        return False
    return not any(marker.lower() in text for marker in _FAULT_MARKERS)


def no_hit_answer(question: str) -> str:
    """无命中时的诚实回答（不编造）。"""
    return "\n".join(
        [
            "## 知识库无相关内容",
            "- 在内置知识库（设备配置模板 / IP-VLAN 规划 / 故障 runbook / 项目约束）中"
            "没有检索到与「%s」相关的资料。" % (question or "").strip(),
            "- 我不会编造密码、配置或参数；可以补充资料后再检索，或改用实时工具查询网络状态。",
        ]
    )


def augment_answer(
    role: str,
    question: str,
    answer: str,
    top_k: int = DEFAULT_TOP_K,
) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """给节点答案追加引用块，并返回 (answer, retrieval_record, hits)。

    - 有命中：追加 ``## 知识库引用``（[K1]..），供前端/评测校验引用真实性；
    - 无命中：追加“知识库无相关内容”提示（不编造）；知识型问题会走 no_hit_answer。
    """
    hits = search(question, top_k=top_k)
    text = (answer or "").strip()
    if hits:
        citations = format_citations(hits)
        if citations and citations not in text:
            text = text + "\n" + citations
    else:
        note = "> 知识库无相关内容（本回答未使用内置知识库）。"
        if note not in text:
            text = text + "\n\n" + note
    return text, retrieval_record(role, question, hits), hits


def corpus_size() -> int:
    return chunk_count()


def retrieval_health() -> Dict[str, Any]:
    """健康检查摘要。"""
    return {
        "enabled": True,
        "retriever": "bm25-lite-bigram",
        "corpus_chunks": corpus_size(),
        "default_top_k": DEFAULT_TOP_K,
        "citations": "[K1]..[Kn]",
        "no_external_dependency": True,
        "network": False,
    }
