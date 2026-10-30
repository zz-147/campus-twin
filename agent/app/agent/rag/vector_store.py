# -*- coding: utf-8 -*-
"""W15：哈希嵌入 + 内存向量库（纯标准库，无 torch/faiss/chromadb）。

为什么不用神经网络 embedding（如实说明）：
- 本机 pip 被代理卡死，无法安装 torch / sentence-transformers / chromadb / faiss；
- 不能联网下载 BGE 等 embedding 模型；
- 因此用 **feature hashing（特征哈希）**：把中文 unigram/bigram + 英文/数字 token
  哈希到固定维度（默认 384，可配 256/512），L2 归一化后用余弦相似度。
- 这不是语义 embedding，泛化能力有限，但**零依赖、确定性、可离线复现、可解释**。
- 接口 ``VectorStoreProtocol`` 已按 add/search/persist/load 设计，后续可直接替换
  ChromaDB / FAISS / BGE；文档与 eval_rag 中如实标注该边界。
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from app.agent.rag.index import tokenize


class EmbedderProtocol(Protocol):
    dim: int

    def embed(self, text: str) -> List[float]: ...


class VectorStoreProtocol(Protocol):
    def add(self, chunks: Sequence[Dict[str, Any]]) -> None: ...

    def search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]: ...

    def persist(self, path: Path) -> None: ...

    def load(self, path: Path) -> None: ...


class HashEmbedder:
    """Feature hashing（确定性、零模型）。"""

    def __init__(self, dim: int = 384) -> None:
        if int(dim) < 32:
            raise ValueError("向量维度至少 32")
        self.dim = int(dim)

    def _features(self, text: str) -> List[Tuple[str, float]]:
        ascii_tokens, bigrams = tokenize(str(text or "").lower())
        features: List[Tuple[str, float]] = []
        for token in ascii_tokens:
            weight = 1.5 if token.isascii() else 0.6
            features.append((token, weight))
        for token in bigrams:
            features.append((token, 2.0))
        # 整段短语特征，帮助长 query/标题匹配
        normalized = "".join(str(text or "").lower().split())
        if 2 <= len(normalized) <= 60:
            features.append(("phrase:" + normalized, 1.0))
        return features

    def embed(self, text: str) -> List[float]:
        vector = [0.0] * self.dim
        for token, weight in self._features(text):
            digest = hashlib.sha1(token.encode("utf-8")).hexdigest()
            index = int(digest[:8], 16) % self.dim
            vector[index] += float(weight)
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector


class InMemoryVectorStore:
    """内存向量库：余弦检索 + JSON 持久化；可替换 Chroma/FAISS（同接口）。"""

    def __init__(
        self,
        embedder: Optional[EmbedderProtocol] = None,
        chunks: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> None:
        self.embedder: EmbedderProtocol = embedder or HashEmbedder(384)
        self._chunks: List[Dict[str, Any]] = []
        self._vectors: List[List[float]] = []
        if chunks:
            self.add(chunks)

    # ---- 基本属性 ----
    @property
    def size(self) -> int:
        return len(self._chunks)

    @property
    def chunks(self) -> List[Dict[str, Any]]:
        return [dict(chunk) for chunk in self._chunks]

    # ---- 写 ----
    def add(self, chunks: Sequence[Dict[str, Any]]) -> None:
        for chunk in chunks or []:
            text = "%s %s %s" % (
                chunk.get("title") or "",
                chunk.get("section") or "",
                chunk.get("text") or "",
            )
            self._chunks.append(dict(chunk))
            self._vectors.append(self.embedder.embed(text))

    def clear(self) -> None:
        self._chunks = []
        self._vectors = []

    # ---- 查 ----
    def search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        query_vector = self.embedder.embed(query)
        scored: List[Tuple[float, int]] = []
        for index, vector in enumerate(self._vectors):
            score = sum(left * right for left, right in zip(query_vector, vector))
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        hits: List[Dict[str, Any]] = []
        for rank, (score, index) in enumerate(scored[: max(1, int(top_k))], start=1):
            hit = dict(self._chunks[index])
            hit["vector_score"] = round(float(score), 6)
            hit["score"] = round(float(score), 6)
            hit["rank"] = rank
            hits.append(hit)
        return hits

    # ---- 持久化 ----
    def persist(self, path: Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = {
            "version": 1,
            "dim": int(getattr(self.embedder, "dim", 384)),
            "embedder": type(self.embedder).__name__,
            "chunks": self._chunks,
            "vectors": self._vectors,
        }
        with open(str(target), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        self._path = str(target)

    def load(self, path: Path) -> None:
        with open(str(Path(path)), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.embedder = HashEmbedder(int(payload.get("dim") or 384))
        self._chunks = [dict(item) for item in payload.get("chunks") or []]
        self._vectors = [list(map(float, vec)) for vec in payload.get("vectors") or []]
        if len(self._chunks) != len(self._vectors):
            raise ValueError("向量库文件损坏：chunks 与 vectors 数量不一致")

    # ---- 兼容接口说明 ----
    @staticmethod
    def backends_doc() -> Dict[str, str]:
        return {
            "memory": "当前实现：InMemoryVectorStore（纯标准库 JSON 持久化）",
            "chroma": "替换方式：实现 VectorStoreProtocol.add/search/persist/load，内部调用 chromadb collection",
            "faiss": "替换方式：faiss.IndexFlatIP + 同一 EmbedderProtocol（BGE/其他 embedding）",
            "embedding": "替换 EmbedderProtocol（如 BGE-small-zh）即可，不改 hybrid/rerank 代码",
        }


#: 兼容别名
MemoryVectorStore = InMemoryVectorStore


def build_vector_store(
    chunks: Sequence[Dict[str, Any]], dim: int = 384
) -> InMemoryVectorStore:
    return InMemoryVectorStore(HashEmbedder(dim), chunks)
