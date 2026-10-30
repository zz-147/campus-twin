# -*- coding: utf-8 -*-
"""W12 轻量 RAG：知识库/BM25-lite 检索/引用/无命中/节点自动注入。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from app.agent.actions import ActionExecutor, ActionStore
from app.agent.backend import FixtureBackendClient
from app.agent.conversations import ConversationStore
from app.agent.eval.fixture_data import build_scenario_snapshot, load_base_snapshot
from app.agent.graph.graph import LangGraphEngine
from app.agent.providers.mock import MockProvider
from app.agent.rag import (
    BM25LiteIndex,
    chunk_count,
    format_citations,
    format_context,
    get_chunks,
    is_knowledge_query,
    no_hit_answer,
    retrieval_health,
    retrieval_record,
    search,
    tokenize,
)
from app.agent.roles.orchestrator import Orchestrator
from app.agent.tools import OpsToolkit
from app.config import AgentSettings


def _toolkit(toolkit: OpsToolkit) -> OpsToolkit:
    return toolkit


# --------------------------------------------------------------------------- #
# corpus / index
# --------------------------------------------------------------------------- #
def test_corpus_integrity() -> None:
    chunks = get_chunks()
    assert chunk_count() == len(chunks) >= 15
    ids = [chunk["id"] for chunk in chunks]
    assert len(set(ids)) == len(ids)
    for chunk in chunks:
        assert chunk["id"] and chunk["title"] and chunk["text"]
        assert chunk["source"] in (
            "设备配置模板",
            "校园网络拓扑设计",
            "故障 runbook",
            "项目约束",
        )
    sources = {chunk["source"] for chunk in chunks}
    assert sources == {"设备配置模板", "校园网络拓扑设计", "故障 runbook", "项目约束"}
    # 关键事实存在（来自 docs/校园网络拓扑设计.md）
    corpus_text = " ".join(chunk["text"] for chunk in chunks)
    for fact in ("192.168.20.0/24", "VLAN10", "Core-3650", "trunk"):
        assert fact in corpus_text


def test_tokenizer_bigram_and_weights() -> None:
    ascii_tokens, bigrams = tokenize("2960 上联核心 trunk")
    assert "2960" in ascii_tokens
    assert "trunk" in ascii_tokens
    assert "上联" in bigrams
    assert "联核" in bigrams
    index = BM25LiteIndex(get_chunks())
    hits = index.search("2960 上联核心怎么配置", top_k=3)
    assert hits and hits[0]["id"] == "K-CFG-2960-UPLINK"


def test_search_relevance_ordering_and_topk() -> None:
    hits = search("2960 上联核心怎么配置？", top_k=3)
    assert 2 <= len(hits) <= 3
    assert [hit["rank"] for hit in hits] == list(range(1, len(hits) + 1))
    scores = [hit["score"] for hit in hits]
    assert scores == sorted(scores, reverse=True)
    assert hits[0]["title"].startswith("2960 接入交换机上联核心配置")
    assert search("NC 告警为什么清除慢？", top_k=3)[0]["id"] == "K-CONSTRAINT-NC-LAG"
    assert search("PC-JX1 到服务器 Server-1 的路径怎么走？", top_k=3)[0]["id"] == "K-TOPO-DEVICES"


def test_search_no_hit_returns_empty() -> None:
    for query in ("学校食堂的 Wi-Fi 密码是多少？", "食堂今天吃什么？", "xyzzy 不存在的问题"):
        assert search(query, top_k=5) == []
    assert search("", top_k=5) == []


def test_context_and_citations_real() -> None:
    hits = search("2960 上联核心怎么配置？", top_k=2)
    context = format_context(hits)
    assert "[K1]" in context and "[K2]" in context
    assert hits[0]["title"] in context
    citations = format_citations(hits)
    assert citations.startswith("\n## 知识库引用")
    assert "[K1]" in citations and "[K2]" in citations
    record = retrieval_record("diagnosis", "q", hits)
    assert record["hit_count"] == 2
    assert record["hits"][0]["citation"] == "[K1]"
    assert record["hits"][0]["id"] == hits[0]["id"]
    health = retrieval_health()
    assert health["retriever"] == "bm25-lite-bigram"
    assert health["corpus_chunks"] == chunk_count()
    assert health["network"] is False


def test_no_hit_policy() -> None:
    assert is_knowledge_query("学校食堂的 Wi-Fi 密码是多少？") is True
    assert is_knowledge_query("PC-JX1 掉线了怎么办？") is False
    answer = no_hit_answer("学校食堂的 Wi-Fi 密码是多少？")
    assert "知识库无相关内容" in answer
    assert "不会编造" in answer


# --------------------------------------------------------------------------- #
# search_knowledge 工具
# --------------------------------------------------------------------------- #
def test_search_knowledge_tool(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("search_knowledge", {"query": "2960 上联核心怎么配置", "top_k": 3})
    assert envelope["ok"] is True
    data = envelope["data"]
    assert 1 <= data["count"] <= 3
    assert data["data_source"] == "builtin_knowledge"
    assert data["hits"][0]["citation"] == "[K1]"
    assert data["hits"][0]["text"]
    assert "知识库" in envelope["summary"]
    missing = toolkit.call("search_knowledge", {"query": "食堂 Wi-Fi 密码"})
    assert missing["ok"] is True
    assert missing["data"]["count"] == 0
    assert "知识库无相关内容" in missing["summary"]


# --------------------------------------------------------------------------- #
# 节点自动检索 / 引用注入 / trace.retrieval
# --------------------------------------------------------------------------- #
def _orchestrator(settings: AgentSettings, tmp_path: Path) -> Orchestrator:
    db = tmp_path / "agent.db"
    return Orchestrator(
        MockProvider(),
        OpsToolkit(FixtureBackendClient(load_base_snapshot())),
        ConversationStore(db),
        ActionStore(db),
        settings,
    )


def test_classic_nodes_inject_knowledge(
    settings: AgentSettings, tmp_path: Path
) -> None:
    orchestrator = _orchestrator(settings, tmp_path)
    onboarding = orchestrator.handle("宿舍楼要接入一台 2960 交换机，上联核心怎么配置？")
    assert [item["role"] for item in onboarding.trace["plan"]] == ["onboarding"]
    assert "## 知识库引用" in onboarding.answer
    assert "[K1]" in onboarding.answer
    assert onboarding.trace["retrieval"][0]["hit_count"] >= 1
    assert onboarding.trace["knowledge"]["retriever"] == "bm25-lite-bigram"
    # 引用必须来自内置知识库（chunk id 可追溯）
    hit = onboarding.trace["retrieval"][0]["hits"][0]
    assert hit["id"] in {chunk["id"] for chunk in get_chunks()}

    diagnosis = orchestrator.handle("NC 告警为什么清除慢？")
    assert [item["role"] for item in diagnosis.trace["plan"]] == ["diagnosis"]
    assert "## 知识库引用" in diagnosis.answer
    assert "滞后" in diagnosis.answer
    assert diagnosis.trace["retrieval"][0]["hits"][0]["citation"] == "[K1]"

    no_hit = orchestrator.handle("学校食堂的 Wi-Fi 密码是多少？")
    assert "知识库无相关内容" in no_hit.answer
    assert "不会编造" in no_hit.answer
    assert no_hit.trace["retrieval"][0]["hit_count"] == 0


def test_langgraph_nodes_inject_knowledge(
    base_snapshot: Dict[str, Any], settings: AgentSettings, tmp_path: Path
) -> None:
    db = tmp_path / "graph.db"
    backend = FixtureBackendClient(base_snapshot)
    conversations = ConversationStore(db)
    engine = LangGraphEngine(
        MockProvider(),
        OpsToolkit(backend),
        conversations,
        ActionStore(db),
        settings,
        executor=ActionExecutor(backend, conversations),
        graph_db_path=tmp_path / "checkpoints.db",
    )
    outcome = engine.handle("宿舍楼要接入一台 2960 交换机，上联核心怎么配置？")
    assert "[K1]" in outcome.answer
    assert outcome.trace["retrieval"][0]["role"] == "onboarding"
    assert outcome.trace["retrieval"][0]["hit_count"] >= 1
    assert outcome.trace["knowledge"]["corpus"] == "builtin"
    no_hit = engine.handle("学校食堂的 Wi-Fi 密码是多少？")
    assert "知识库无相关内容" in no_hit.answer
    engine.close()
