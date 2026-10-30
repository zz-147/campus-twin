# -*- coding: utf-8 -*-
"""字符 bigram + 关键词加权的 BM25-lite 检索（无外部依赖、不联网）。

设计
====
- 中文：按连续 CJK 串切 **字符 unigram + bigram**（bigram 权重更高），覆盖
  “2960 上联核心怎么配”“NC 告警清除慢”这类短查询；
- 英文/数字：按 ``[a-z0-9][a-z0-9_.:/-]*`` 切词（如 2960、trunk、nc、vlan20）；
- 文档侧把 title 重复 2 次、source 重复 1 次，实现“标题/来源关键词加权”；
- 打分：BM25（k1=1.5, b=0.75）+ 长片段短语命中 bonus；低于 ``min_score`` 直接丢弃，
  保证无关问题返回空（RAG 不编造）。
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Tuple

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]*")
_STRIP_RE = re.compile(r"[\s，。、；：！？,.!?;:（）()\[\]【】\"'“”‘’]+")
_PUNCT_RE = re.compile(r"[^\u4e00-\u9fffa-z0-9]+")


def normalize(text: str) -> str:
    return _STRIP_RE.sub("", (text or "").lower())


def tokenize(text: str) -> Tuple[List[str], List[str]]:
    """返回 (unigrams+ascii, bigrams) 两类 token。"""
    lowered = (text or "").lower()
    unigrams: List[str] = []
    bigrams: List[str] = []
    for run in _CJK_RE.findall(lowered):
        unigrams.extend(list(run))
        if len(run) >= 2:
            bigrams.extend(run[index : index + 2] for index in range(len(run) - 1))
    ascii_tokens = _ASCII_RE.findall(lowered)
    return ascii_tokens + unigrams, bigrams


#: 查询侧停用词（疑问/填充词不参与 BM25，避免“是多少/为什么”误命中知识库）
_QUERY_STOP_BIGRAMS = {
    "是多", "多少", "是什", "什么", "怎么", "么配", "为什", "为何", "如何", "请问",
    "一下", "可以", "是否", "哪些", "那个", "这个", "怎样", "多少", "吗？",
}
_QUERY_STOP_UNIGRAMS = set("是多什么怎么为何如何请问一下可以是否哪些那个这怎样吗呢吧啊")


def query_token_weights(text: str) -> Dict[str, float]:
    """查询 token 权重：bigram 2.0、中文 unigram 0.6、英文/数字 1.5（过滤疑问停用词）。"""
    ascii_tokens, bigrams = tokenize(text)
    weights: Dict[str, float] = {}
    for token in ascii_tokens:
        if token in _QUERY_STOP_UNIGRAMS or token in _QUERY_STOP_BIGRAMS:
            continue
        if token.isascii():
            weights[token] = max(weights.get(token, 0.0), 1.5)
        else:
            weights[token] = max(weights.get(token, 0.0), 0.6)
    for token in bigrams:
        if token in _QUERY_STOP_BIGRAMS:
            continue
        weights[token] = max(weights.get(token, 0.0), 2.0)
    return weights


class BM25LiteIndex:
    """极简 BM25 索引（内存，几十条 chunk 足够快）。"""

    def __init__(self, chunks: Iterable[Dict[str, Any]], k1: float = 1.5, b: float = 0.75) -> None:
        self._chunks: List[Dict[str, Any]] = [dict(chunk) for chunk in chunks]
        self._k1 = k1
        self._b = b
        self._doc_tokens: List[Dict[str, int]] = []
        self._doc_len: List[int] = []
        self._doc_texts: List[str] = []
        df: Dict[str, int] = {}
        for chunk in self._chunks:
            text = "%s %s %s" % (
                chunk.get("title") or "",
                (chunk.get("title") or "") + " " + (chunk.get("source") or ""),
                chunk.get("text") or "",
            )
            ascii_tokens, bigrams = tokenize(text)
            counts: Dict[str, int] = {}
            for token in ascii_tokens + bigrams:
                counts[token] = counts.get(token, 0) + 1
            self._doc_tokens.append(counts)
            self._doc_len.append(sum(counts.values()))
            self._doc_texts.append(normalize(text))
            for token in counts:
                df[token] = df.get(token, 0) + 1
        self._df = df
        self._avg_len = (sum(self._doc_len) / float(len(self._doc_len))) if self._doc_len else 0.0
        self._total_docs = len(self._doc_len)

    def _idf(self, token: str) -> float:
        df = self._df.get(token, 0)
        if df <= 0:
            return 0.0
        return math.log(1.0 + (self._total_docs - df + 0.5) / (df + 0.5))

    def _score_doc(self, index: int, weights: Dict[str, float], norm_query: str) -> float:
        counts = self._doc_tokens[index]
        doc_len = self._doc_len[index] or 1
        avg_len = self._avg_len or 1.0
        score = 0.0
        for token, weight in weights.items():
            tf = counts.get(token, 0)
            if not tf:
                continue
            idf = self._idf(token)
            denominator = tf + self._k1 * (1.0 - self._b + self._b * doc_len / avg_len)
            score += weight * idf * tf * (self._k1 + 1.0) / denominator
        # 短语命中 bonus（规范化整句出现在正文中；过长查询则跳过）
        if 4 <= len(norm_query) <= 80 and norm_query in self._doc_texts[index]:
            score += 3.0
        return score

    def search(
        self, query: str, top_k: int = 5, min_score: float = 1.5
    ) -> List[Dict[str, Any]]:
        query = (query or "").strip()
        if not query or not self._chunks:
            return []
        weights = query_token_weights(query)
        if not weights:
            return []
        norm_query = _PUNCT_RE.sub("", query.lower())
        scored: List[Tuple[float, int]] = []
        for index in range(len(self._chunks)):
            score = self._score_doc(index, weights, norm_query)
            if score >= min_score:
                scored.append((score, index))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        results: List[Dict[str, Any]] = []
        for rank, (score, index) in enumerate(scored[: max(1, int(top_k))], start=1):
            chunk = self._chunks[index]
            results.append(
                {
                    "id": chunk.get("id"),
                    "score": round(score, 4),
                    "rank": rank,
                    "title": chunk.get("title"),
                    "source": chunk.get("source"),
                    "text": chunk.get("text"),
                }
            )
        return results
