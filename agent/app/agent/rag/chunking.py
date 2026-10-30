# -*- coding: utf-8 -*-
"""W15：3 种 chunking 策略（纯标准库，用于 RAG 评测对比）。

- ``fixed``：固定长度窗口 + overlap（字符数）；
- ``section``：按标题/章节切分（默认运行时 corpus 的策略，保留原 chunk id）；
- ``semantic``：按段落/句子合并到目标长度，并给每个 chunk 加“标题+章节”前缀（轻量语义段）。

所有策略输出统一字段：id/doc_id/source/title/section/text/length/strategy。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

_CHUNK_SEP_RE = re.compile(r"(?<=[。！？!?；;])")
_WS_RE = re.compile(r"\s+")


def _make_chunk(
    doc: Dict[str, Any],
    chunk_id: str,
    section: str,
    text: str,
    strategy: str,
) -> Dict[str, Any]:
    clean = _WS_RE.sub(" ", text or "").strip()
    return {
        "id": chunk_id,
        "doc_id": str(doc.get("doc_id") or "doc"),
        "source": str(doc.get("source") or "项目约束"),
        "title": str(doc.get("title") or doc.get("doc_id") or "doc"),
        "section": section,
        "text": clean,
        "length": len(clean),
        "strategy": strategy,
    }


def chunk_fixed(
    doc: Dict[str, Any],
    target_chars: int = 240,
    overlap: int = 40,
) -> List[Dict[str, Any]]:
    """固定长度窗口（带 overlap）——最简单、可复现的 baseline。"""
    text = "\n".join(
        str(section.get("text") or "").strip()
        for section in (doc.get("sections") or [])
        if str(section.get("text") or "").strip()
    )
    if not text:
        return []
    step = max(40, int(target_chars) - max(0, int(overlap)))
    chunks: List[Dict[str, Any]] = []
    index = 0
    position = 0
    while position < len(text):
        piece = text[position : position + int(target_chars)]
        index += 1
        chunks.append(
            _make_chunk(
                doc,
                "K-%s-F%03d" % (doc.get("doc_id") or "doc", index),
                "固定窗口",
                piece,
                "fixed",
            )
        )
        position += step
    return chunks


def chunk_by_sections(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按标题/章节切分；兼容 corpus 的 legacy chunk id（doc.chunk_id）。"""
    chunks: List[Dict[str, Any]] = []
    legacy_id = str(doc.get("chunk_id") or "")
    for index, section in enumerate(doc.get("sections") or [], start=1):
        text = str(section.get("text") or "").strip()
        if not text:
            continue
        chunk_id = legacy_id or "K-%s-S%02d" % (doc.get("doc_id") or "doc", index)
        chunks.append(
            _make_chunk(
                doc,
                chunk_id,
                str(section.get("section") or "概述"),
                text,
                "section",
            )
        )
    return chunks


def chunk_semantic(
    doc: Dict[str, Any],
    target_chars: int = 280,
    max_chars: int = 520,
) -> List[Dict[str, Any]]:
    """按段落/句子合并到目标长度，并加“标题+章节”前缀的轻量语义段。"""
    pieces: List[Dict[str, str]] = []
    for section in doc.get("sections") or []:
        section_name = str(section.get("section") or "概述")
        text = str(section.get("text") or "").strip()
        if not text:
            continue
        parts = [part.strip() for part in _CHUNK_SEP_RE.split(text) if part.strip()]
        for part in parts:
            pieces.append({"section": section_name, "text": part})
    if not pieces:
        return []
    chunks: List[Dict[str, Any]] = []
    buffer: List[str] = []
    buffer_len = 0
    current_section = pieces[0]["section"]

    def flush() -> None:
        nonlocal buffer, buffer_len
        if not buffer:
            return
        text = " ".join(buffer).strip()
        if text:
            prefix = "【%s】%s" % (doc.get("title") or "", current_section)
            index = len(chunks) + 1
            chunks.append(
                _make_chunk(
                    doc,
                    "K-%s-M%03d" % (doc.get("doc_id") or "doc", index),
                    current_section,
                    "%s：%s" % (prefix, text),
                    "semantic",
                )
            )
        buffer = []
        buffer_len = 0

    for piece in pieces:
        if piece["section"] != current_section:
            flush()
            current_section = piece["section"]
        if buffer_len + len(piece["text"]) > target_chars and buffer:
            flush()
        buffer.append(piece["text"])
        buffer_len += len(piece["text"])
        if buffer_len >= max_chars:
            flush()
    flush()
    return chunks


def chunk_document(doc: Dict[str, Any], strategy: str, **kwargs: Any) -> List[Dict[str, Any]]:
    if strategy == "fixed":
        return chunk_fixed(doc, **kwargs)
    if strategy == "section":
        return chunk_by_sections(doc)
    if strategy == "semantic":
        return chunk_semantic(doc, **kwargs)
    raise ValueError("未知 chunking 策略：%s（可用 fixed/section/semantic）" % strategy)


def chunk_documents(
    documents: List[Dict[str, Any]], strategy: str, **kwargs: Any
) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    for doc in documents:
        chunks.extend(chunk_document(doc, strategy, **kwargs))
    return chunks


def chunk_stats(chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    lengths = [int(chunk.get("length") or len(chunk.get("text") or "")) for chunk in chunks]
    return {
        "count": len(chunks),
        "avg_length": round(sum(lengths) / len(lengths), 1) if lengths else 0.0,
        "min_length": min(lengths) if lengths else 0,
        "max_length": max(lengths) if lengths else 0,
    }
