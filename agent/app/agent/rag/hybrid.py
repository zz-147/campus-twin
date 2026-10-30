# -*- coding: utf-8 -*-
"""W15：BM25 + 轻量向量 + RRF 融合 + 轻量 rerank（纯标准库）。

- BM25：复用 ``index.BM25LiteIndex``（中文 unigram/bigram + 标题加权）；
- 向量：``vector_store.HashEmbedder + InMemoryVectorStore``（feature hashing）；
- 融合：Reciprocal Rank Fusion（RRF, k=60）；
- rerank：query 词项覆盖率 + 标题/章节命中 + 短语 bonus + BM25 归一化分数加权；
- 保留 no-hit 语义：最终结果需至少一个 BM25 达标候选（默认 10.0，与 retriever 一致），
  避免向量把无关问题也“硬答”。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.agent.rag.index import BM25LiteIndex, normalize, query_token_weights
from app.agent.rag.vector_store import HashEmbedder, InMemoryVectorStore

#: 运行时 no-hit 门槛（与 retriever.MIN_SCORE 对齐）
DEFAULT_MIN_BM25_SCORE = 10.0

#: RRF 常数
RRF_K = 60


def _rrf_fuse(rankings: Sequence[Sequence[Dict[str, Any]]], k: int = RRF_K) -> List[Dict[str, Any]]:
    """把多路 ranking 融合成按 RRF 分数排序的候选（保留各自原始分数）。"""
    merged: Dict[str, Dict[str, Any]] = {}
    for ratings in rankings:
        for rank, item in enumerate(ratings, start=1):
            chunk_id = str(item.get("id") or "")
            if not chunk_id:
                continue
            entry = merged.setdefault(chunk_id, dict(item))
            entry.update({key: value for key, value in item.items() if key not in entry})
            entry["rrf_score"] = entry.get("rrf_score", 0.0) + 1.0 / (k + rank)
    candidates = list(merged.values())
    for candidate in candidates:
        candidate["hybrid_score"] = round(float(candidate.get("rrf_score") or 0.0), 6)
    candidates.sort(key=lambda item: (-float(item.get("rrf_score") or 0.0), str(item.get("id"))))
    return candidates


def _coverage(query: str, text: str) -> float:
    weights = query_token_weights(query)
    if not weights:
        return 0.0
    lowered = str(text or "").lower()
    total = sum(weights.values()) or 1.0
    matched = sum(weight for token, weight in weights.items() if token in lowered)
    return min(1.0, matched / total)


def rerank(
    query: str,
    candidates: Sequence[Dict[str, Any]],
    chunks_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """轻量 rerank：RRF/BM25 分数 + 词项覆盖 + 标题/章节命中 + 短语 bonus。"""
    if not candidates:
        return []
    chunks_by_id = chunks_by_id or {}
    max_rrf = max(float(item.get("rrf_score") or 0.0) for item in candidates) or 1.0
    max_bm25 = max(float(item.get("bm25_score") or 0.0) for item in candidates) or 1.0
    norm_query = normalize(query)
    reranked: List[Dict[str, Any]] = []
    for item in candidates:
        hit = dict(item)
        chunk = chunks_by_id.get(str(hit.get("id"))) or hit
        title = str(chunk.get("title") or "")
        section = str(chunk.get("section") or "")
        text = str(chunk.get("text") or "")
        body = "%s %s %s" % (title, section, text)
        coverage = _coverage(query, body)
        title_coverage = _coverage(query, title)
        section_coverage = _coverage(query, section)
        phrase_bonus = 1.0 if (len(norm_query) >= 2 and norm_query in normalize(body)) else 0.0
        rrf_norm = float(hit.get("rrf_score") or 0.0) / max_rrf
        bm25_norm = float(hit.get("bm25_score") or 0.0) / max_bm25
        score = (
            0.52 * rrf_norm
            + 0.24 * bm25_norm
            + 0.12 * coverage
            + 0.07 * title_coverage
            + 0.03 * section_coverage
            + 0.02 * phrase_bonus
        )
        hit["rerank_score"] = round(float(score), 6)
        hit["score"] = round(float(score), 6)
        hit["coverage"] = round(float(coverage), 6)
        reranked.append(hit)
    reranked.sort(
        key=lambda item: (
            -float(item.get("rerank_score") or 0.0),
            -float(item.get("bm25_score") or 0.0),
            str(item.get("id")),
        )
    )
    for index, hit in enumerate(reranked, start=1):
        hit["rank"] = index
    return reranked


class HybridSearcher:
    """BM25 + 向量 + RRF + rerank 的统一检索器。"""

    def __init__(
        self,
        chunks: Sequence[Dict[str, Any]],
        dim: int = 384,
        min_bm25_score: float = DEFAULT_MIN_BM25_SCORE,
    ) -> None:
        self.chunks = [dict(chunk) for chunk in chunks]
        self.chunks_by_id = {str(chunk.get("id")): chunk for chunk in self.chunks}
        self.bm25 = BM25LiteIndex(self.chunks)
        self.store = InMemoryVectorStore(HashEmbedder(dim), self.chunks)
        self.min_bm25_score = float(min_bm25_score)

    def _bm25_hits(self, query: str, candidate_k: int) -> List[Dict[str, Any]]:
        hits = self.bm25.search(query, top_k=candidate_k, min_score=0.0)
        for index, hit in enumerate(hits, start=1):
            hit["bm25_score"] = hit.get("score")
            hit["rank"] = index
        return hits

    def _vector_hits(self, query: str, candidate_k: int) -> List[Dict[str, Any]]:
        hits = self.store.search(query, top_k=candidate_k)
        for index, hit in enumerate(hits, start=1):
            hit["vector_score"] = hit.get("score")
            hit["rank"] = index
        return hits

    def search(
        self,
        query: str,
        top_k: int = 5,
        mode: str = "hybrid_rerank",
        candidate_k: int = 20,
        filter_min_bm25: bool = True,
    ) -> List[Dict[str, Any]]:
        """mode: bm25 | vector | hybrid | hybrid_rerank。"""
        top_k = max(1, int(top_k))
        candidate_k = max(top_k, int(candidate_k))
        if not (query or "").strip():
            return []
        bm25_hits = self._bm25_hits(query, candidate_k)
        vector_hits = self._vector_hits(query, candidate_k)
        if mode == "bm25":
            hits = bm25_hits
            for hit in hits:
                hit["score"] = hit.get("bm25_score")
                hit["hybrid_score"] = None
                hit["vector_score"] = None
        elif mode == "vector":
            hits = vector_hits
            for hit in hits:
                hit["score"] = hit.get("vector_score")
                hit["bm25_score"] = None
                hit["hybrid_score"] = None
        else:
            hits = _rrf_fuse([bm25_hits, vector_hits], RRF_K)
            if mode == "hybrid_rerank":
                hits = rerank(query, hits, self.chunks_by_id)
            else:
                for index, hit in enumerate(hits, start=1):
                    hit["rank"] = index
                    hit["score"] = hit.get("hybrid_score")
        if filter_min_bm25:
            bm25_by_id = {str(hit.get("id")): float(hit.get("bm25_score") or 0.0) for hit in bm25_hits}
            filtered = [
                hit
                for hit in hits
                if bm25_by_id.get(str(hit.get("id")), 0.0) >= self.min_bm25_score
            ]
            hits = filtered
        for index, hit in enumerate(hits[:top_k], start=1):
            hit["rank"] = index
        return hits[:top_k]


#: 单例缓存（retriever 用）
_DEFAULT_SEARCHER: Optional[HybridSearcher] = None


def get_default_searcher() -> HybridSearcher:
    global _DEFAULT_SEARCHER
    if _DEFAULT_SEARCHER is None:
        from app.agent.rag.corpus import get_chunks

        _DEFAULT_SEARCHER = HybridSearcher(get_chunks(), dim=384)
    return _DEFAULT_SEARCHER


def reset_default_searcher() -> None:
    """测试/热更新用：清空单例。"""
    global _DEFAULT_SEARCHER
    _DEFAULT_SEARCHER = None


def search_hybrid(
    query: str,
    top_k: int = 5,
    mode: str = "hybrid_rerank",
    chunks: Optional[Sequence[Dict[str, Any]]] = None,
    dim: int = 384,
    filter_min_bm25: bool = True,
) -> List[Dict[str, Any]]:
    if chunks is not None:
        searcher = HybridSearcher(chunks, dim=dim)
    else:
        searcher = get_default_searcher()
    return searcher.search(
        query, top_k=top_k, mode=mode, filter_min_bm25=filter_min_bm25
    )
