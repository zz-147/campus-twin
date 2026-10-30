# -*- coding: utf-8 -*-
"""W12 轻量 RAG：内置知识库 corpus + BM25-lite 检索 + 引用/无命中规范。

不引入向量库、不联网、不读用户目录；``[K1]..[Kn]`` 引用只来自内置 corpus。
"""

from app.agent.rag.corpus import KNOWLEDGE_CHUNKS, chunk_count, get_chunks  # noqa: F401
from app.agent.rag.index import BM25LiteIndex, query_token_weights, tokenize  # noqa: F401
from app.agent.rag.retriever import (  # noqa: F401
    DEFAULT_TOP_K,
    augment_answer,
    citations_record,
    corpus_size,
    format_citations,
    format_context,
    get_index,
    is_knowledge_query,
    no_hit_answer,
    retrieval_health,
    retrieval_record,
    search,
)
