# -*- coding: utf-8 -*-
"""W15 RAG 评测闭环：100+ 语料 / 3 种 chunking / 哈希向量库 / hybrid+rerank / 指标。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from app.agent.rag import corpus
from app.agent.rag.chunking import (
    chunk_by_sections,
    chunk_documents,
    chunk_fixed,
    chunk_semantic,
    chunk_stats,
)
from app.agent.rag.corpus import get_chunks, source_documents
from app.agent.rag.eval_rag import (
    EVAL_QUERIES,
    build_chunk_sets,
    evaluate_retrieval,
    run_evaluation,
    _gold_document_keys,
    _is_relevant,
)
from app.agent.rag.hybrid import HybridSearcher, _rrf_fuse, rerank, search_hybrid
from app.agent.rag.retriever import retrieval_health, search
from app.agent.rag.vector_store import HashEmbedder, InMemoryVectorStore, build_vector_store
from app.agent.tools import OpsToolkit


def test_corpus_has_100_plus_chunks_and_schema() -> None:
    chunks = get_chunks()
    assert len(chunks) >= 100
    assert corpus.chunk_count() == len(chunks)
    ids = [chunk["id"] for chunk in chunks]
    assert len(ids) == len(set(ids))
    for chunk in chunks:
        for key in ("id", "title", "source", "section", "text", "length", "doc_id"):
            assert key in chunk
        assert chunk["text"].strip()
        assert int(chunk["length"]) == len(chunk["text"])
    sources = {chunk["source"] for chunk in chunks}
    assert sources == {"设备配置模板", "校园网络拓扑设计", "故障 runbook", "项目约束"}
    # 原 18 个核心 chunk 保持不变（search_knowledge 兼容）
    legacy = next(chunk for chunk in chunks if chunk["id"] == "K-CFG-2960-UPLINK")
    assert "switchport mode trunk" in legacy["text"]
    assert legacy["title"].startswith("2960 接入交换机上联核心配置")
    assert corpus.chunk_count() >= 100


def test_three_chunking_strategies() -> None:
    docs = source_documents()
    fixed = chunk_documents(docs, "fixed", target_chars=200, overlap=40)
    section = chunk_documents(docs, "section")
    semantic = chunk_documents(docs, "semantic", target_chars=240)
    assert fixed and section and semantic
    assert len({chunk["id"] for chunk in fixed}) == len(fixed)
    assert all(chunk["strategy"] == "fixed" for chunk in fixed)
    assert all(chunk["strategy"] == "section" for chunk in section)
    assert all(chunk["strategy"] == "semantic" for chunk in semantic)
    # fixed 带 overlap：窗口步长小于目标长度
    fixed_stats = chunk_stats(fixed)
    section_stats = chunk_stats(section)
    assert fixed_stats["count"] >= section_stats["count"]
    assert all(0 < chunk["length"] <= 260 for chunk in fixed)
    assert fixed_stats["avg_length"] >= 120
    # semantic 加标题+章节前缀
    assert all("【" in chunk["text"] for chunk in semantic)
    assert next(item for item in section if item["id"] == "K-CFG-2960-UPLINK")
    # 单文档 API
    one = chunk_semantic({"doc_id": "x", "title": "T", "source": "项目约束", "sections": [{"section": "S", "text": "一二三四五六七八九十。" * 40}]})
    assert one and all(chunk["section"] == "S" for chunk in one)


def test_vector_store_hash_embedding_and_persist(tmp_path: Path) -> None:
    chunks = [
        {"id": "c1", "title": "trunk 配置", "section": "上联", "text": "switchport mode trunk allowed vlan 10,20"},
        {"id": "c2", "title": "告警滞后", "section": "NC", "text": "NC 告警清除滞后 stale_source"},
        {"id": "c3", "title": "MCP", "section": "工具", "text": "MCP tools resources prompts"},
    ]
    embedder = HashEmbedder(dim=256)
    vector = embedder.embed("trunk 配置")
    assert len(vector) == 256
    assert pytest.approx(sum(value * value for value in vector), 0.0001) == 1.0
    store = InMemoryVectorStore(embedder, chunks)
    assert store.size == 3
    hits = store.search("trunk allowed vlan", top_k=2)
    assert hits and hits[0]["id"] == "c1"
    assert hits[0]["vector_score"] > 0
    path = tmp_path / "vectors.json"
    store.persist(path)
    assert path.exists()
    loaded = InMemoryVectorStore()
    loaded.load(path)
    assert loaded.size == 3
    assert loaded.search("NC 告警滞后", top_k=1)[0]["id"] == "c2"
    assert loaded.chunks[0]["id"] == "c1"
    assert "chroma" in InMemoryVectorStore.backends_doc()


def test_hybrid_rrf_and_rerank() -> None:
    bm25 = [
        {"id": "a", "title": "A", "text": "alpha", "bm25_score": 30.0},
        {"id": "b", "title": "B", "text": "beta", "bm25_score": 20.0},
    ]
    vector = [
        {"id": "b", "title": "B", "text": "beta", "vector_score": 0.8},
        {"id": "c", "title": "C", "text": "gamma", "vector_score": 0.7},
    ]
    fused = _rrf_fuse([bm25, vector])
    ids = [item["id"] for item in fused]
    assert set(ids) == {"a", "b", "c"}
    assert set(ids[:2]) == {"a", "b"}  # 两路都命中的 b 至少在前二
    chunks_by_id = {item["id"]: dict(item, section="s", source="src") for item in fused}
    reranked = rerank("alpha beta", fused, chunks_by_id)
    assert reranked and reranked[0]["id"] in ("a", "b")
    assert reranked[0]["score"] >= reranked[-1]["score"]
    # 真实 hybrid：默认返回带 bm25/vector/hybrid/rerank 分数
    hits = search_hybrid("2960 上联核心怎么配置？", top_k=5)
    assert hits and hits[0]["id"] == "K-CFG-2960-UPLINK"
    assert hits[0].get("bm25_score") is not None
    assert hits[0].get("vector_score") is not None
    assert hits[0].get("hybrid_score") is not None
    assert hits[0].get("rerank_score") is not None
    assert search_hybrid("xyzzy 不存在的问题", top_k=5) == []


def test_retrieval_health_reports_w15() -> None:
    health = retrieval_health()
    assert health["enabled"] is True
    assert health["retriever"] == "bm25-lite-bigram"  # 兼容旧字段
    assert health["corpus_chunks"] == corpus.chunk_count() >= 100
    assert health["mode"] == "hybrid_rerank"
    assert health["vector"] == "feature-hashing"
    assert health["network"] is False


def test_eval_metrics_small_fixture() -> None:
    chunks = [
        {"id": "g1", "doc_id": "D1", "title": "gold one", "section": "s1", "text": "trunk vlan"},
        {"id": "g2", "doc_id": "D1", "title": "gold two", "section": "s2", "text": "trunk config"},
        {"id": "x9", "doc_id": "D9", "title": "other", "section": "s9", "text": "unrelated"},
    ]
    specs = [
        {"id": "q1", "category": "配置", "query": "trunk vlan", "gold_ids": ["g1"]},
        {"id": "q2", "category": "配置", "query": "trunk config", "gold_ids": ["g2"]},
    ]
    metrics = evaluate_retrieval(specs, chunks, mode="bm25", doc_level=False)
    assert metrics["queries"] == 2
    assert 0.0 <= metrics["recall_at_5"] <= 1.0
    assert 0.0 <= metrics["mrr"] <= 1.0
    assert 0.0 <= metrics["citation_precision"] <= 1.0
    assert metrics["citation_traceability"] == 1.0
    assert 0.0 <= metrics["faithfulness"] <= 1.0
    assert _gold_document_keys({"gold_ids": ["K-CFG-CORE3650-S03"]}) >= {
        "K-CFG-CORE3650-S03", "K-CFG-CORE3650", "CFG-CORE3650",
    }
    assert _is_relevant(
        {"id": "K-CFG-CORE3650-F001", "doc_id": "CFG-CORE3650"},
        {"gold_ids": ["K-CFG-CORE3650-S03"]},
        doc_level=True,
    )


def test_eval_rag_full_report(tmp_path: Path) -> None:
    report_path = tmp_path / "rag_report.json"
    report = run_evaluation(report_path=report_path)
    assert report_path.exists()
    loaded = json.loads(report_path.read_text(encoding="utf-8"))
    assert loaded["queries"]["count"] >= 40
    assert loaded["corpus"]["default_chunks"] >= 100
    assert loaded["embedding"]["neural"] is False
    assert set(loaded["strategies"]) == {"fixed", "section", "semantic"}
    assert set(loaded["configs"]) == {"bm25", "vector", "hybrid", "hybrid_rerank"}
    assert loaded["best_strategy"] in {"fixed", "section", "semantic"}
    for mode, metrics in loaded["configs"].items():
        assert 0.0 <= metrics["recall_at_5"] <= 1.0, mode
        assert 0.0 <= metrics["mrr"] <= 1.0, mode
        assert 0.0 <= metrics["citation_precision"] <= 1.0, mode
        assert metrics["citation_traceability"] == 1.0, mode
        assert 0.0 <= metrics["faithfulness"] <= 1.0, mode
    assert len(EVAL_QUERIES) >= 40


def test_search_knowledge_tool_carries_scores(toolkit: OpsToolkit) -> None:
    envelope = toolkit.call("search_knowledge", {"query": "2960 上联核心怎么配置", "top_k": 3})
    assert envelope["ok"] is True
    hits = envelope["data"]["hits"]
    assert hits and hits[0]["id"] == "K-CFG-2960-UPLINK"
    assert hits[0].get("citation") == "[K1]"
    # trace 侧 retrieval 分数由 retriever.citations_record 提供
    from app.agent.rag.retriever import citations_record

    record = citations_record(search("2960 上联核心怎么配置", top_k=3))
    assert record[0]["scores"]["bm25"] is not None
    assert record[0]["scores"]["vector"] is not None
