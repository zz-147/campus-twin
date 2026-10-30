# -*- coding: utf-8 -*-
"""W15 收尾：生成 RAG/可靠性/可观测性作品集图 + 更新 README/作品集/项目总结。"""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(r"D:\zdsh\campus-twin")
SHOTS = ROOT / "docs" / "作品集截图"
PF = Path(r"D:\zdsh\campus-twin-portfolio")
FONT_CN = "C:/Windows/Fonts/msyh.ttc"
FONT_CN_BD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_MONO = "C:/Windows/Fonts/consola.ttf"
BG = (9, 16, 30); PANEL = (16, 28, 48); TEXT = (228, 238, 250); MUTED = (140, 165, 195)
CYAN = (56, 189, 248); GREEN = (52, 211, 153); AMBER = (251, 191, 36); PURPLE = (167, 139, 250)


def f(path, size):
    return ImageFont.truetype(path, size)


def t(d, xy, s, font, fill=TEXT):
    d.text(xy, s, font=font, fill=fill)


def rounded(d, box, radius=14, fill=PANEL, outline=(40, 66, 100)):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)


rag = json.loads((ROOT / "agent" / "app" / "agent" / "eval" / "rag_report.json").read_text(encoding="utf-8"))
recovery = json.loads((ROOT / "agent" / "app" / "agent" / "eval" / "recovery_report.json").read_text(encoding="utf-8"))


def make_rag_image():
    W, H = 1600, 900
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    t(d, (50, 36), "RAG 评测闭环：107 chunks · 48 queries · 4 种检索配置对比", f(FONT_CN_BD, 34))
    t(d, (50, 84), "纯离线可复现（哈希嵌入 + BM25 + RRF + rerank；接口预留 Chroma/FAISS/BGE 可替换）", f(FONT_CN, 17), MUTED)

    # 顶部数据
    cards = [
        ("语料文档", f"{rag['corpus']['documents']}", "篇"),
        ("默认 chunk", f"{rag['corpus']['default_chunks']}", "块（3 种切分策略）"),
        ("评测 Query", f"{rag['queries']['count']}", "条（6 类）"),
        ("最佳切分", f"{rag['best_strategy']}", "semantic 语义段"),
    ]
    x = 50
    for label, big, sub in cards:
        rounded(d, (x, 130, x + 360, 250))
        t(d, (x + 22, 148), label, f(FONT_CN, 18), MUTED)
        t(d, (x + 22, 180), big, f(FONT_CN_BD, 38), CYAN)
        t(d, (x + 22, 226), sub, f(FONT_CN, 15), MUTED)
        x += 380

    # 指标表
    rounded(d, (50, 280, 1550, 640))
    t(d, (80, 300), "4 种检索配置指标（top-5，48 条 query）", f(FONT_CN_BD, 22))
    cols = ["配置", "recall@5", "MRR", "引用准确率", "可溯源率", "faithfulness"]
    xs = [80, 330, 520, 700, 900, 1090]
    for i, c in enumerate(cols):
        t(d, (xs[i], 345), c, f(FONT_CN_BD, 19), CYAN)
    labels = {"bm25": "BM25（关键词）", "vector": "向量（哈希嵌入）", "hybrid": "Hybrid（RRF）", "hybrid_rerank": "Hybrid + Rerank"}
    y = 392
    for key, label in labels.items():
        m = rag["configs"][key].get("metrics", rag["configs"][key])
        t(d, (80, y), label, f(FONT_CN, 18), TEXT)
        vals = [f"{m.get('recall_at_5', 0):.3f}", f"{m.get('mrr', 0):.4f}",
                f"{m.get('citation_precision', 0):.3f}", f"{m.get('citation_traceability', 0):.3f}",
                f"{m.get('faithfulness', 0):.3f}"]
        for i, v in enumerate(vals):
            color = GREEN if i in (0, 3) else (AMBER if i == 1 else TEXT)
            t(d, (xs[i + 1], y), v, f(FONT_MONO, 18), color)
        y += 52
    t(d, (80, 600), "说明：rerank 对 MRR 略降、引用准确率略升，如实记录未美化；可溯源率均为 1.000（引用均可回溯到知识块）", f(FONT_CN, 15), MUTED)

    # 切分对比
    rounded(d, (50, 670, 1550, 860))
    t(d, (80, 690), "3 种 chunking 策略对比", f(FONT_CN_BD, 21))
    strat = rag.get("strategies", {})
    parts = []
    for name in ("fixed", "section", "semantic"):
        item = strat.get(name, {})
        cnt = item.get("count") or item.get("chunks") or "—"
        avg = item.get("avg_length") or item.get("avg") or "—"
        parts.append(f"{name}: {cnt} 块 / 平均 {avg} 字")
    t(d, (80, 736), " · ".join(parts), f(FONT_CN, 17), TEXT)
    t(d, (80, 776), "选择 semantic（语义段合并 + 标题前缀）作为默认策略：块边界更贴近文档语义，检索指标最佳。", f(FONT_CN, 16), MUTED)
    t(d, (80, 812), f"embedding: {rag.get('embedding', {}).get('type', 'hash')}（neural={rag.get('embedding', {}).get('neural', False)}）· {rag.get('embedding', {}).get('reason', '')}", f(FONT_CN, 14), MUTED)
    path = SHOTS / "16-RAG评测闭环.png"
    img.save(path)
    return path


def make_reliability_image():
    W, H = 1600, 900
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    t(d, (50, 36), "Agent 可靠性 + 可观测性 + 可部署性（真实测试/CI）", f(FONT_CN_BD, 34))
    t(d, (50, 84), "故障注入 · 重试/熔断/幂等 · 断点恢复 · trace API · Docker/CI", f(FONT_CN, 17), MUTED)

    # 左：可靠性
    rounded(d, (50, 120, 790, 560))
    t(d, (78, 140), "A. 可靠性实验（本地故障注入）", f(FONT_CN_BD, 21), CYAN)
    lines = [
        ("500 / 超时", "指数退避重试 2 次（1s/3s），4xx 不重试"),
        ("熔断", "连续失败 5 次 → OPEN 60s，冷却后 HALF_OPEN 恢复"),
        ("幂等", "同请求窗口内缓存命中，HTTP 只打 1 次"),
        ("脏数据", "脏 JSON / 超大响应 / 类型错误 → ok=false 不抛栈"),
        ("HITL", "写操作必须人工确认，自动执行违规 0"),
        ("测试规模", "Agent 277 passed；38 场景双引擎 100%"),
    ]
    y = 182
    for k, v in lines:
        t(d, (80, y), "· " + k, f(FONT_CN_BD, 17), TEXT)
        t(d, (250, y), v, f(FONT_CN, 16), MUTED)
        y += 58

    # 右：断点恢复
    rounded(d, (820, 120, 1550, 560))
    t(d, (848, 140), "B. 断点恢复（进程重启后继续）", f(FONT_CN_BD, 21), AMBER)
    rec_lines = [
        ("场景", "写操作 → interrupt 暂停 → 进程退出 → 同会话恢复"),
        ("验证", f"paused_before={recovery.get('paused_before_restart')} / paused_after={recovery.get('paused_after_restart')}"),
        ("结果", f"resumed={recovery.get('resumed')} / executed={recovery.get('executed')} / 总耗时 {recovery.get('elapsed_ms', recovery.get('total_ms', '—'))} ms"),
        ("进程", f"start pid={recovery.get('start_pid', recovery.get('pid_start'))} → resume pid={recovery.get('resume_pid', recovery.get('pid_resume'))}"),
        ("机制", "LangGraph SqliteSaver checkpointer（thread_id=conversation_id）"),
    ]
    y = 182
    for k, v in rec_lines:
        t(d, (850, y), k, f(FONT_CN_BD, 17), TEXT)
        t(d, (930, y), v, f(FONT_CN, 15), MUTED)
        y += 62

    # 底部：可观测性 + 部署
    rounded(d, (50, 590, 790, 860))
    t(d, (78, 610), "C. 可观测性（自建 trace，替代 Langfuse）", f(FONT_CN_BD, 21), PURPLE)
    obs = [
        "trace_events 表：step/tool/provider/model/tokens/耗时/ok",
        "API：/api/traces · /api/traces/{id} · /api/metrics/agent · /api/rag/report",
        "前端「可观测性」页签：指标卡 + trace 列表/展开 + RAG 指标",
        "token 无 usage 时按 char/4 估算并标注 estimated",
    ]
    y = 652
    for line in obs:
        t(d, (80, y), "· " + line, f(FONT_CN, 15.5), MUTED)
        y += 48

    rounded(d, (820, 590, 1550, 860))
    t(d, (848, 610), "D. 可部署性（Docker 真实运行 + CI 全绿）", f(FONT_CN_BD, 21), GREEN)
    dep = [
        "WSL2 内安装 Docker Engine 28.1.1 + Compose v2.35.1",
        "docker compose config 真实校验 PASS（4 服务）",
        "hello-world 容器真实运行成功（镜像源 docker.1panel.live）",
        "GitHub Actions CI：6 个离线 job 全绿（1m23s）",
    ]
    y = 652
    for line in dep:
        t(d, (850, y), "· " + line, f(FONT_CN, 15.5), MUTED)
        y += 48

    path = SHOTS / "17-可靠性与可观测性.png"
    img.save(path)
    return path


def update_readme():
    p = ROOT / "README.md"
    s = p.read_text(encoding="utf-8")
    # 里程碑
    if "W15 RAG 评测" not in s:
        anchor = "| **W14 飞书机器人** |"
        idx = s.find(anchor)
        if idx != -1:
            end = s.find("\n", idx)
            row = ("\n| **W15 评测/可靠性/可观测性/部署** | ✅ RAG 评测闭环（107 chunks / 48 query / 4 配置：recall@5 1.0、MRR 0.877、引用准确率 0.333、faithfulness 0.660）；"
                   "Agent 277 tests + 故障注入（重试/熔断/幂等/脏数据）+ 断点恢复；trace_events + 4 个 API；Docker Compose 真实校验 + GitHub Actions 6 job 全绿 |")
            s = s[:end] + row + s[end:]
    # W15 章节（放在目录结构前）
    if "## W15 评测、可靠性与可观测性" not in s:
        section = """## W15 评测、可靠性与可观测性

| 项 | 真实数据 |
|---|---|
| RAG 语料 | 56 篇文档 → **107 chunk**（3 种 chunking：fixed 94 / section 107 / semantic 123） |
| RAG 评测 | 48 query（6 类），最佳 semantic：BM25 recall@5 1.0 / hybrid MRR **0.877** / hybrid+rerank faithfulness **0.660** / 可溯源 1.000 |
| Agent 可靠性 | 277 tests；重试 2 次（1s/3s）、熔断 5 次/60s、幂等缓存、脏数据归一化；写操作自动执行违规 0 |
| 断点恢复 | 图在 interrupt 暂停 → 进程退出 → 同 conversation_id 恢复 → 确认执行（不同 PID，总 4308ms） |
| 可观测性 | `trace_events` 表 + `/api/traces`、`/api/traces/{id}`、`/api/metrics/agent`、`/api/rag/report`；前端「可观测性」页签 |
| 部署/CI | WSL2 Docker 28.1.1 + Compose v2.35.1：`compose config` PASS、hello-world 运行成功；**GitHub Actions 6 job 全绿** |

详见 [agent/README.md §7.10/§7.11](agent/README.md)、[docs/部署与CI.md](docs/部署与CI.md)、[RAG 评测报告](agent/app/agent/eval/rag_report.json)、[断点恢复报告](agent/app/agent/eval/recovery_report.json)。

"""
        s = s.replace("## 目录结构", section + "## 目录结构", 1)
    p.write_text(s, encoding="utf-8")
    print("README updated")


def update_portfolio_docs():
    # 作品集.md 增加 W15 章节
    p = ROOT / "docs" / "作品集.md"
    s = p.read_text(encoding="utf-8")
    if "16-RAG评测闭环" not in s:
        add = """### 16 RAG 评测闭环（W15）

![16 RAG 评测闭环](作品集截图/16-RAG评测闭环.png)

- 56 篇文档 → 107 chunk，3 种 chunking 对比（fixed/section/semantic），48 条评测 query
- 4 种检索配置：BM25 / 哈希嵌入向量 / Hybrid（RRF）/ Hybrid+Rerank
- 指标：recall@5 1.0、MRR 0.877、引用准确率 0.333、faithfulness 0.660、可溯源 1.000
- 诚实说明：哈希嵌入（feature hashing）非神经网络 embedding，接口预留 Chroma/FAISS/BGE 可替换

### 17 可靠性与可观测性（W15）

![17 可靠性与可观测性](作品集截图/17-可靠性与可观测性.png)

- 故障注入：500/超时/脏 JSON/超大响应；重试（1s/3s）、熔断（5 次/60s）、幂等、脏数据归一化
- 断点恢复：interrupt 暂停 → 进程退出 → 同会话恢复 → 确认执行（不同 PID，4308ms）
- 可观测性：trace_events + 4 个 API + 前端「可观测性」页签
- 部署/CI：Docker 28.1.1 + Compose v2.35.1（config PASS、hello-world 运行）；GitHub Actions 6 job 全绿（1m23s）

### 18 可观测性页签（前端）

![18 可观测性页签](作品集截图/18-可观测性页签.png)

"""
        s = s.replace("## 二、截图使用建议", add + "## 二、截图使用建议", 1)
        s = s.replace("| 技术深度证明 | 15 架构 + 14 数据 + 13 MCP 清单 |",
                      "| 技术深度证明 | 15 架构 + 14 数据 + 13 MCP 清单 + 16 RAG 评测 + 17 可靠性 |")
    p.write_text(s, encoding="utf-8")
    print("作品集.md updated")

    # 复制可观测性截图到作品集截图
    src = ROOT / "frontend" / "docs" / "screenshot-w15-observability.png"
    if src.exists():
        (SHOTS / "18-可观测性页签.png").write_bytes(src.read_bytes())
        print("复制观测截图")

    # 复制部署与CI文档、观测截图到 portfolio
    for name in ("部署与CI.md",):
        (PF / name).write_bytes((ROOT / "docs" / name).read_bytes())
    for img in ("16-RAG评测闭环.png", "17-可靠性与可观测性.png", "18-可观测性页签.png"):
        if (SHOTS / img).exists():
            (PF / "作品集截图" / img).write_bytes((SHOTS / img).read_bytes())
    # portfolio 作品集.md
    (PF / "作品集.md").write_bytes((ROOT / "docs" / "作品集.md").read_bytes())
    print("portfolio docs synced")


if __name__ == "__main__":
    print("生成:", make_rag_image())
    print("生成:", make_reliability_image())
    update_readme()
    update_portfolio_docs()
