# -*- coding: utf-8 -*-
"""W15 RAG 评测闭环（纯标准库，离线可重复）。

对比：
- 3 种 chunking：fixed / section / semantic；
- 4 种检索配置：BM25 / 向量 / hybrid（RRF）/ hybrid+rerank。

指标：
- recall@5：top5 内是否命中 gold（gold id 或 gold 关键词）；
- MRR：第一个 gold 的倒数排名；
- 引用准确率：top5 引用中与 gold 相关占比（citation_precision）；
- 引用可溯源率：引用 chunk id 都能在语料中找到（citation_traceability）；
- faithfulness：引用 chunk 文本对 query 关键实体的覆盖率（启发式，可选接入 LLM judge）。

输出：``app/agent/eval/rag_report.json`` + 控制台表格。
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.agent.rag.chunking import chunk_documents, chunk_stats
from app.agent.rag.corpus import SOURCE_DOCUMENTS, get_chunks
from app.agent.rag.hybrid import HybridSearcher
from app.agent.rag.index import tokenize

ROOT = Path(__file__).resolve().parents[3]
REPORT_PATH = ROOT / "app" / "agent" / "eval" / "rag_report.json"

STRATEGIES: Tuple[str, ...] = ("fixed", "section", "semantic")
MODES: Tuple[str, ...] = ("bm25", "vector", "hybrid", "hybrid_rerank")

#: 评测 query：48 条，覆盖 6 类；gold_ids 优先，gold_keywords 兜底
EVAL_QUERIES: List[Dict[str, Any]] = [
    # 配置（7）
    {"id": "q01", "category": "配置", "query": "2960 上联核心 trunk 怎么配置",
     "gold_ids": ["K-CFG-2960-UPLINK"], "gold_keywords": ["trunk", "switchport mode trunk"]},
    {"id": "q02", "category": "配置", "query": "3650 核心 SVI 网关地址怎么配",
     "gold_ids": ["K-CFG-CORE3650-S03"], "gold_keywords": ["SVI", "192.168.10.1"]},
    {"id": "q03", "category": "配置", "query": "4331 边界路由器 NAT 怎么做",
     "gold_ids": ["K-CFG-ROUTER4331-S03"], "gold_keywords": ["NAT", "ip nat inside"]},
    {"id": "q04", "category": "配置", "query": "接入端口 access VLAN 和 portfast 怎么配",
     "gold_ids": ["K-CFG-2960-GENERIC-S03"], "gold_keywords": ["switchport access vlan", "portfast"]},
    {"id": "q05", "category": "配置", "query": "核心 Gi1/0/x 下联端口映射",
     "gold_ids": ["K-TOPO-PORTMAP-S01"], "gold_keywords": ["Gi1/0/1", "trunk"]},
    {"id": "q06", "category": "配置", "query": "2960 管理 SVI 地址和默认网关",
     "gold_ids": ["K-CFG-SWJX2960-S02"], "gold_keywords": ["192.168.10.11", "default-gateway"]},
    {"id": "q07", "category": "配置", "query": "路由器静态路由和默认路由怎么配",
     "gold_ids": ["K-CFG-ROUTER4331-S02"], "gold_keywords": ["ip route", "0.0.0.0"]},
    # 拓扑/IP（7）
    {"id": "q08", "category": "拓扑", "query": "VLAN40 宿舍楼网段和网关是多少",
     "gold_ids": ["K-VLAN-PLAN-S04"], "gold_keywords": ["VLAN40", "192.168.40.0/24"]},
    {"id": "q09", "category": "拓扑", "query": "VLAN50 服务器网段规划",
     "gold_ids": ["K-VLAN-PLAN-S05"], "gold_keywords": ["VLAN50", "192.168.50.0/24"]},
    {"id": "q10", "category": "拓扑", "query": "校园网核心、接入、终端设备清单",
     "gold_ids": ["K-TOPO-DEVICES-DETAIL-S01"], "gold_keywords": ["Core-3650", "SW-JX-2960"]},
    {"id": "q11", "category": "拓扑", "query": "NC 纳管需要默认网关吗",
     "gold_ids": ["K-NC-MANAGE-S01"], "gold_keywords": ["默认网关", "192.168.10.1"]},
    {"id": "q12", "category": "拓扑", "query": "终端自动发现依赖什么",
     "gold_ids": ["K-NC-MANAGE-S02"], "gold_keywords": ["终端", "发现"]},
    {"id": "q13", "category": "拓扑", "query": "核心到三栋楼的端口和 VLAN",
     "gold_ids": ["K-TOPO-PORTMAP-S01"], "gold_keywords": ["Gi1/0/1", "VLAN"]},
    {"id": "q14", "category": "拓扑", "query": "Server-1 接核心哪个端口和 VLAN",
     "gold_ids": ["K-TOPO-PORTMAP-S02"], "gold_keywords": ["Gi1/0/5", "VLAN50"]},
    # 故障（9）
    {"id": "q15", "category": "故障", "query": "主机离线 host_down 怎么排查",
     "gold_ids": ["K-RB-HOST-DOWN-S01"], "gold_keywords": ["host_down", "ping_status"]},
    {"id": "q16", "category": "故障", "query": "设备不可达 device_unreachable 怎么定位",
     "gold_ids": ["K-RB-DEVICE-UNREACHABLE-S01"], "gold_keywords": ["设备不可达", "管理 IP"]},
    {"id": "q17", "category": "故障", "query": "端口 shutdown 后怎么恢复",
     "gold_ids": ["K-RB-PORT-DOWN-S02"], "gold_keywords": ["no shutdown", "interface Fa0"]},
    {"id": "q18", "category": "故障", "query": "链路中断如何定位断点",
     "gold_ids": ["K-RB-LINK-DOWN-S01"], "gold_keywords": ["show interfaces", "cdp neighbors"]},
    {"id": "q19", "category": "故障", "query": "跨网段不通怀疑路由缺失",
     "gold_ids": ["K-RB-ROUTING-MISSING-S01"], "gold_keywords": ["show ip route", "默认路由"]},
    {"id": "q20", "category": "故障", "query": "VLAN 错配终端拿不到网关",
     "gold_ids": ["K-RB-VLAN-MISCONFIG-S01"], "gold_keywords": ["switchport access vlan", "VLAN"]},
    {"id": "q21", "category": "故障", "query": "广播风暴和环路怎么排查",
     "gold_ids": ["K-RB-BROADCAST-STORM-S01"], "gold_keywords": ["广播", "spanning-tree"]},
    {"id": "q22", "category": "故障", "query": "设备 CPU 高怎么定位",
     "gold_ids": ["K-RB-HIGH-CPU-S01"], "gold_keywords": ["CPU", "show processes"]},
    {"id": "q23", "category": "故障", "query": "上联带宽拥塞怎么处理",
     "gold_ids": ["K-RB-BANDWIDTH-S01"], "gold_keywords": ["utilization", "丢包"]},
    # 项目约束（12）
    {"id": "q24", "category": "约束", "query": "NC 告警为什么清除慢",
     "gold_ids": ["K-CONSTRAINT-NC-LAG"], "gold_keywords": ["滞后", "stale_source"]},
    {"id": "q25", "category": "约束", "query": "observed_at 和 source_ts 双时间基准",
     "gold_ids": ["K-CONSTRAINT-TIME-S01"], "gold_keywords": ["observed_at", "source_ts"]},
    {"id": "q26", "category": "约束", "query": "告警生命周期和 dedup 去重",
     "gold_ids": ["K-CONSTRAINT-ALERT-LIFECYCLE-S01"], "gold_keywords": ["告警状态", "dedup"]},
    {"id": "q27", "category": "约束", "query": "根因分组优先级和上游抑制",
     "gold_ids": ["K-CONSTRAINT-ALERT-GROUPS-S01"], "gold_keywords": ["link_down", "抑制"]},
    {"id": "q28", "category": "约束", "query": "软删除审计与撤销",
     "gold_ids": ["K-CONSTRAINT-SOFT-DELETE-S01"], "gold_keywords": ["软删除", "审计"]},
    {"id": "q29", "category": "约束", "query": "写操作确认 TTL 和 CAS 防重",
     "gold_ids": ["K-CONSTRAINT-ACTION-S02"], "gold_keywords": ["TTL", "CAS"]},
    {"id": "q30", "category": "约束", "query": "SSE 事件有哪些、断线怎么处理",
     "gold_ids": ["K-CONSTRAINT-SSE-S01"], "gold_keywords": ["text/event-stream", "done"]},
    {"id": "q31", "category": "约束", "query": "RAG 无命中应该怎么回答",
     "gold_ids": ["K-CONSTRAINT-RAG-S02"], "gold_keywords": ["知识库无相关内容", "不得编造"]},
    {"id": "q32", "category": "约束", "query": "LangGraph interrupt 节点重跑陷阱",
     "gold_ids": ["K-CONSTRAINT-LANGGRAPH-S03"], "gold_keywords": ["interrupt", "从头重跑"]},
    {"id": "q33", "category": "约束", "query": "checkpointer thread_id 会话恢复",
     "gold_ids": ["K-CONSTRAINT-LANGGRAPH-S02"], "gold_keywords": ["SqliteSaver", "thread_id"]},
    {"id": "q34", "category": "约束", "query": "规则优先路由和反例保护",
     "gold_ids": ["K-ROUTING-RULES-S01"], "gold_keywords": ["route_source", "规则"]},
    {"id": "q35", "category": "约束", "query": "工具结果截断导致动作丢失的修复",
     "gold_ids": ["K-ROUTING-TRUNCATION-S02"], "gold_keywords": ["截断", "summary"]},
    # MCP（7）
    {"id": "q36", "category": "MCP", "query": "MCP 的 tools resources prompts 是什么",
     "gold_ids": ["K-MCP-PROTOCOL-S01"], "gold_keywords": ["tools", "resources", "prompts"]},
    {"id": "q37", "category": "MCP", "query": "MCP Server 10 个工具清单",
     "gold_ids": ["K-CONSTRAINT-MCP-SERVER-S01"], "gold_keywords": ["get_devices", "trace_path"]},
    {"id": "q38", "category": "MCP", "query": "MCP 4 个 resources 和 3 个 prompts",
     "gold_ids": ["K-CONSTRAINT-MCP-SERVER-S02"], "gold_keywords": ["campus://scene", "troubleshoot"]},
    {"id": "q39", "category": "MCP", "query": "MCP Client 桥接架构怎么工作",
     "gold_ids": ["K-CONSTRAINT-MCP-BRIDGE-S01"], "gold_keywords": ["Bridge", "stdio"]},
    {"id": "q40", "category": "MCP", "query": "MCP Bridge 安全白名单边界",
     "gold_ids": ["K-CONSTRAINT-MCP-BRIDGE-S02"], "gold_keywords": ["白名单", "127.0.0.1"]},
    {"id": "q41", "category": "MCP", "query": "MCP 和 function calling 的区别",
     "gold_ids": ["K-MCP-PROTOCOL-S02"], "gold_keywords": ["function calling", "MCP"]},
    {"id": "q42", "category": "MCP", "query": "MCP tools 里怎么查告警和路径",
     "gold_ids": ["K-MCP-TOOLS-SERVER-S01"], "gold_keywords": ["get_alerts", "trace_path"]},
    # 评测/工程（6）
    {"id": "q43", "category": "评测", "query": "38 场景评测有哪些指标",
     "gold_ids": ["K-CONSTRAINT-EVAL-S01"], "gold_keywords": ["根因", "动作正确率"]},
    {"id": "q44", "category": "评测", "query": "RAG 评测 recall@5 MRR faithfulness",
     "gold_ids": ["K-EVAL-RAG-S01"], "gold_keywords": ["recall@5", "faithfulness"]},
    {"id": "q45", "category": "评测", "query": "3D 大屏和 AI 面板有哪些能力",
     "gold_ids": ["K-CONSTRAINT-FRONTEND-S01"], "gold_keywords": ["Three.js", "路径追踪"]},
    {"id": "q46", "category": "评测", "query": "部署边界和生产化差距",
     "gold_ids": ["K-CONSTRAINT-DEPLOY-S01"], "gold_keywords": ["单机", "生产"]},
    {"id": "q47", "category": "评测", "query": "自动执行违规和动作安全门",
     "gold_ids": ["K-CONSTRAINT-EVAL-S02"], "gold_keywords": ["自动执行违规", "pending"]},
    {"id": "q48", "category": "评测", "query": "告警抑制会不会删除告警",
     "gold_ids": ["K-CONSTRAINT-ALERT-GROUPS-S02"], "gold_keywords": ["展示层", "不删除"]},
]


def build_chunk_sets() -> Dict[str, List[Dict[str, Any]]]:
    documents = SOURCE_DOCUMENTS
    return {
        "fixed": chunk_documents(documents, "fixed", target_chars=240, overlap=40),
        "section": chunk_documents(documents, "section"),
        "semantic": chunk_documents(documents, "semantic", target_chars=280),
    }


def _query_keywords(query: str) -> List[str]:
    ascii_tokens, bigrams = tokenize(query)
    keywords: List[str] = []
    for token in ascii_tokens:
        if token.isascii() and len(token) >= 2 and token not in keywords:
            keywords.append(token)
    for token in bigrams:
        if token not in keywords:
            keywords.append(token)
    return keywords[:30]


def _gold_document_keys(spec: Dict[str, Any]) -> set:
    """gold chunk id 也映射到所属 doc_id，供 fixed/semantic chunk 集做同文档相关性判断。"""
    keys = set()
    for chunk_id in spec.get("gold_ids") or []:
        chunk_id = str(chunk_id)
        keys.add(chunk_id)
        keys.add("LEGACY-" + chunk_id)
        parent, _, suffix = chunk_id.rpartition("-")
        if parent and re.match(r"^[SFM]\d+$", suffix):
            keys.add(parent)
            if parent.startswith("K-"):
                keys.add(parent[2:])
    return keys


def _hit_doc_id(hit: Dict[str, Any]) -> str:
    doc_id = str(hit.get("doc_id") or "")
    if doc_id:
        return doc_id
    chunk_id = str(hit.get("id") or "")
    match = re.match(r"^K-(.+)-[SFM]\d+$", chunk_id)
    return match.group(1) if match else ""


def _is_relevant(
    hit: Dict[str, Any],
    spec: Dict[str, Any],
    doc_level: bool = False,
) -> bool:
    """gold_ids 优先（严格）；fixed/semantic 集允许同文档（chunk id 不同）。"""
    chunk_id = str(hit.get("id") or "")
    gold_ids = set(spec.get("gold_ids") or [])
    if gold_ids:
        if chunk_id in gold_ids:
            return True
        if doc_level:
            doc_id = _hit_doc_id(hit)
            if doc_id and doc_id in _gold_document_keys(spec):
                return True
        return False
    body = "%s %s %s" % (hit.get("title") or "", hit.get("section") or "", hit.get("text") or "")
    lowered = body.lower()
    return any(str(keyword).lower() in lowered for keyword in (spec.get("gold_keywords") or []))


def _faithfulness(query: str, hits: Sequence[Dict[str, Any]]) -> float:
    keywords = _query_keywords(query)
    if not keywords:
        return 0.0
    context = " ".join(
        "%s %s %s" % (hit.get("title") or "", hit.get("section") or "", hit.get("text") or "")
        for hit in hits
    ).lower()
    matched = sum(1 for keyword in keywords if keyword.lower() in context)
    return round(matched / float(len(keywords)), 4)


def evaluate_retrieval(
    queries: Sequence[Dict[str, Any]],
    chunks: Sequence[Dict[str, Any]],
    mode: str,
    dim: int = 384,
    top_k: int = 5,
    doc_level: bool = False,
) -> Dict[str, Any]:
    """对一份 chunk 集 + 一种检索模式计算 recall@5 / MRR / 引用/faithfulness。

    ``doc_level=True``：fixed/semantic chunk id 与 gold section id 不同，按所属 doc 判定相关。
    """
    searcher = HybridSearcher(chunks, dim=dim)
    chunks_by_id = {str(chunk.get("id")): chunk for chunk in chunks}
    recall = 0
    reciprocal_ranks: List[float] = []
    cited_total = 0
    cited_relevant = 0
    traceable_total = 0
    traceable_hits = 0
    faithfulness: List[float] = []
    per_query: List[Dict[str, Any]] = []
    for spec in queries:
        hits = searcher.search(
            str(spec.get("query") or ""),
            top_k=top_k,
            mode=mode,
            filter_min_bm25=False,
        )
        ranks = [index for index, hit in enumerate(hits, start=1) if _is_relevant(hit, spec, doc_level)]
        if ranks:
            recall += 1
            reciprocal_ranks.append(1.0 / ranks[0])
        else:
            reciprocal_ranks.append(0.0)
        for hit in hits:
            cited_total += 1
            if _is_relevant(hit, spec, doc_level):
                cited_relevant += 1
            traceable_total += 1
            if str(hit.get("id")) in chunks_by_id:
                traceable_hits += 1
        faithfulness.append(_faithfulness(str(spec.get("query") or ""), hits))
        per_query.append(
            {
                "id": spec.get("id"),
                "category": spec.get("category"),
                "hit_ids": [hit.get("id") for hit in hits],
                "first_gold_rank": ranks[0] if ranks else None,
                "faithfulness": faithfulness[-1],
            }
        )
    total = len(queries) or 1
    return {
        "recall_at_5": round(recall / float(total), 4),
        "mrr": round(sum(reciprocal_ranks) / float(total), 4),
        "citation_precision": round(cited_relevant / float(cited_total), 4) if cited_total else 0.0,
        "citation_traceability": round(traceable_hits / float(traceable_total), 4) if traceable_total else 0.0,
        "faithfulness": round(sum(faithfulness) / float(total), 4),
        "queries": len(queries),
        "per_query": per_query,
    }


def run_evaluation(
    queries: Optional[Sequence[Dict[str, Any]]] = None,
    report_path: Optional[Path] = None,
    dim: int = 384,
    write_report: bool = True,
) -> Dict[str, Any]:
    started = time.perf_counter()
    specs = list(queries or EVAL_QUERIES)
    chunk_sets = build_chunk_sets()
    strategies: Dict[str, Any] = {}
    for strategy, chunks in chunk_sets.items():
        configs: Dict[str, Any] = {}
        doc_level = strategy != "section"
        for mode in MODES:
            configs[mode] = evaluate_retrieval(
                specs, chunks, mode, dim=dim, doc_level=doc_level
            )
            configs[mode].pop("per_query", None)  # report 里保留汇总，避免过大
        strategies[strategy] = {
            "stats": chunk_stats(chunks),
            "configs": configs,
        }
    # 选最佳 chunking：hybrid_rerank recall@5 优先，其次 MRR，再 citation_precision
    def _rank(strategy: str) -> Tuple[float, float, float]:
        metrics = strategies[strategy]["configs"]["hybrid_rerank"]
        return (
            float(metrics["recall_at_5"]),
            float(metrics["mrr"]),
            float(metrics["citation_precision"]),
        )

    best_strategy = max(STRATEGIES, key=_rank)
    report: Dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "engine": "pure-stdlib-hash-embedding-bm25-rrf-rerank",
        "corpus": {
            "documents": len(SOURCE_DOCUMENTS),
            "default_chunks": len(get_chunks()),
            "strategies": {name: data["stats"] for name, data in strategies.items()},
        },
        "queries": {
            "count": len(specs),
            "categories": sorted({str(spec.get("category") or "-") for spec in specs}),
        },
        "top_k": 5,
        "embedding": {
            "type": "feature_hashing",
            "dim": dim,
            "neural": False,
            "reason": "本机 pip/代理受限，无法安装 torch/sentence-transformers/chromadb/faiss，且不能联网下载模型；接口已预留 Chroma/FAISS/BGE",
        },
        "strategies": strategies,
        "best_strategy": best_strategy,
        "configs": strategies[best_strategy]["configs"],
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
    }
    target = Path(report_path) if report_path else REPORT_PATH
    if write_report:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(str(target), "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        report["report_path"] = str(target)
    return report


def print_report(report: Dict[str, Any]) -> None:
    print("=" * 92)
    print("W15 RAG 评测闭环（纯标准库：feature hashing + BM25 + RRF + rerank）")
    print(
        "语料：%d docs / %d default chunks | query=%d | best chunking=%s | %.1fms"
        % (
            report["corpus"]["documents"],
            report["corpus"]["default_chunks"],
            report["queries"]["count"],
            report["best_strategy"],
            report["elapsed_ms"],
        )
    )
    print("-" * 92)
    print("%-10s %8s %8s %10s %10s %10s %10s" % (
        "strategy", "chunks", "avg_len", "config", "recall@5", "MRR", "cite_prec"))
    for strategy, payload in report["strategies"].items():
        stats = payload["stats"]
        for mode, metrics in payload["configs"].items():
            print(
                "%-10s %8d %8.1f %10s %10.3f %8.3f %10.3f"
                % (
                    strategy,
                    stats["count"],
                    stats["avg_length"],
                    mode,
                    metrics["recall_at_5"],
                    metrics["mrr"],
                    metrics["citation_precision"],
                )
            )
    print("-" * 92)
    best = report["configs"]
    print("最佳 chunking = %s；4 组配置：" % report["best_strategy"])
    for mode in MODES:
        m = best[mode]
        print(
            "  %-14s recall@5=%.3f MRR=%.3f citation_precision=%.3f traceability=%.3f faithfulness=%.3f"
            % (mode, m["recall_at_5"], m["mrr"], m["citation_precision"],
               m["citation_traceability"], m["faithfulness"])
        )
    print("报告：%s" % report.get("report_path", REPORT_PATH))
    print("=" * 92)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="W15 RAG 评测闭环")
    parser.add_argument("--report", default=None, help="报告输出路径")
    parser.add_argument("--dim", type=int, default=384, help="哈希向量维度（256/512）")
    parser.add_argument("--quiet", action="store_true", help="只输出报告路径")
    args = parser.parse_args(argv)
    report = run_evaluation(
        report_path=Path(args.report) if args.report else None,
        dim=args.dim,
    )
    if args.quiet:
        print(report.get("report_path"))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
