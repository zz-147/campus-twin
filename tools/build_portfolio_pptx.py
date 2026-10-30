# -*- coding: utf-8 -*-
"""生成《校园网络数字孪生运维平台 · 作品集》PPTX（随后用 LibreOffice 导出 PDF）。

用法（用 DSH 自带 Python，含 python-pptx）：
  <bundled python> tools/build_portfolio_pptx.py
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Emu, Inches, Pt

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "作品集截图"
OUT_PPTX = ROOT / "docs" / "校园网络数字孪生运维平台-作品集.pptx"

# 主题色
BG = RGBColor(0x0B, 0x12, 0x20)
PANEL = RGBColor(0x10, 0x1C, 0x30)
TEXT = RGBColor(0xE4, 0xEE, 0xFA)
MUTED = RGBColor(0x8C, 0xA5, 0xC3)
CYAN = RGBColor(0x38, 0xBD, 0xF8)
GREEN = RGBColor(0x34, 0xD3, 0x99)
AMBER = RGBColor(0xFB, 0xBF, 0x24)
PURPLE = RGBColor(0xA7, 0x8B, 0xFA)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

FONT = "Microsoft YaHei"
SW, SH = 13.333, 7.5  # 16:9 英寸

prs = Presentation()
prs.slide_width = Inches(SW)
prs.slide_height = Inches(SH)

page_no = {"n": 0}


def new_slide(bg=BG):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = bg
    page_no["n"] += 1
    return slide


def textbox(slide, x, y, w, h, text, size=14, color=TEXT, bold=False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, line_spacing=1.15):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    lines = text.split("\n") if isinstance(text, str) else text
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = line_spacing
        run = p.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.name = FONT
        run.font.color.rgb = color
    return box


def rect(slide, x, y, w, h, fill, line=None, radius=False):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
        Inches(x), Inches(y), Inches(w), Inches(h),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    if line is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = line
        shape.line.width = Pt(1)
    shape.shadow.inherit = False
    if radius:
        try:
            shape.adjustments[0] = 0.12
        except Exception:
            pass
    return shape


def chip(slide, x, y, w, text, color=CYAN, size=12):
    rect(slide, x, y, w, 0.42, PANEL, line=color, radius=True)
    textbox(slide, x + 0.12, y + 0.06, w - 0.24, 0.3, text, size=size, color=color, bold=True, align=PP_ALIGN.CENTER)


def picture_fit(slide, path, x, y, max_w, max_h):
    """按比例放入图片，返回 (left, top, w, h)。"""
    with Image.open(path) as im:
        iw, ih = im.size
    ratio = iw / ih
    w, h = max_w, max_w / ratio
    if h > max_h:
        h, w = max_h, max_h * ratio
    left = x + (max_w - w) / 2
    top = y + (max_h - h) / 2
    slide.shapes.add_picture(str(path), Inches(left), Inches(top), Inches(w), Inches(h))
    return left, top, w, h


def cropped_image(src, out_name, top_px=0, bottom_px=0):
    """裁剪图片上下边（去掉内嵌标题/脚注，避免与幻灯片标题重复）。"""
    src = Path(src)
    dst = ROOT / "logs" / out_name
    with Image.open(src) as im:
        w, h = im.size
        im = im.crop((0, top_px, w, h - bottom_px))
        im.save(dst)
    return dst


def title_block(slide, title, subtitle=None):
    textbox(slide, 0.45, 0.28, 12.4, 0.55, title, size=25, color=TEXT, bold=True)
    if subtitle:
        textbox(slide, 0.45, 0.83, 12.4, 0.32, subtitle, size=12, color=MUTED)


def footer(slide, page):
    textbox(slide, 0.45, 7.1, 8.5, 0.25, "校园网络数字孪生运维平台 · 作品集 · 2026.10", size=9, color=MUTED)
    textbox(slide, 12.2, 7.1, 0.7, 0.25, f"{page:02d}", size=9, color=MUTED, align=PP_ALIGN.RIGHT)


def caption(slide, x, y, w, text, color=MUTED, size=11):
    textbox(slide, x, y, w, 0.5, text, size=size, color=color, line_spacing=1.2)


# ---------------- Slide 1: 封面 ----------------
s = new_slide()
textbox(s, 0.9, 1.35, 11.6, 0.6, "个人项目作品集 · 2026.10", size=14, color=CYAN, bold=True)
textbox(s, 0.9, 1.9, 11.6, 0.9, "校园网络数字孪生运维平台", size=44, color=TEXT, bold=True)
textbox(s, 0.9, 2.9, 11.6, 0.5, "真实网络数据 · LangGraph 多 Agent · MCP 双向集成", size=20, color=MUTED)
chips = [
    ("5 设备 / 6 主机 真实纳管", GREEN),
    ("38 场景评测 100%", AMBER),
    ("171 + 256 tests", CYAN),
    ("MCP 23 工具", PURPLE),
]
cx = 0.9
for label, color in chips:
    w = 2.85
    chip(s, cx, 3.9, w, label, color=color, size=12)
    cx += w + 0.18
textbox(s, 0.9, 5.0, 11.6, 0.4, "从真实网络采集、故障检测、AI 归因到人机确认与 MCP 工具协议化的完整闭环", size=13, color=MUTED)
textbox(s, 0.9, 6.55, 11.6, 0.3, "截图均为真实运行（3 张 Mock 状态图已在正文标注）", size=10, color=MUTED)
footer(s, 1)

# ---------------- Slide 2: PT 实验拓扑 ----------------
s = new_slide()
title_block(s, "01 · 实验环境：Packet Tracer 9 + Network Controller", "拓扑 / 端口 / 可达性由 NC REST API 实时采集，不是静态示意图")
# 白色卡片 + 拓扑图
rect(s, 0.45, 1.3, 8.1, 5.55, WHITE, radius=True)
picture_fit(s, SHOTS / "00-PT实验拓扑.png", 0.6, 1.4, 7.8, 5.35)
# 右侧要点
textbox(s, 8.85, 1.45, 4.1, 0.35, "环境要点", size=15, color=CYAN, bold=True)
points = [
    "5 台网络设备全部被 NC 纳管（Managed）：Router-4331 / Core-3650 / 3×2960",
    "6 台 PC + Server-1；拓扑 5 节点 4 链路，端口级采集",
    "Network Controller 提供 Real World Access REST API（本项目唯一真实数据源）",
    "IOS-XE 16.x / IOS 15.0；VLAN 10/20/30/40/50 + Trunk + OSPF",
]
y = 1.95
for p in points:
    textbox(s, 8.85, y, 4.05, 0.75, "· " + p, size=11.5, color=TEXT, line_spacing=1.25)
    y += 0.82
textbox(s, 8.85, 5.5, 4.05, 0.9, "数据链路：NC REST API → 后端 5s 采集 → WebSocket → 3D 大屏", size=10.5, color=MUTED, line_spacing=1.3)
footer(s, 2)

# ---------------- Slide 3: 系统架构 ----------------
s = new_slide()
title_block(s, "02 · 系统架构：真实数据 + LangGraph 多 Agent + MCP", "全部组件在本机真实运行；7 个进程协同")
arch_img = cropped_image(SHOTS / "15-系统架构图.png", "arch-cropped.png", top_px=132, bottom_px=26)
picture_fit(s, arch_img, 0.45, 1.2, 12.4, 5.6)
textbox(s, 0.45, 6.8, 12.4, 0.25, "端口：PT NC 58000 · 后端 8000 · Agent 8100 · 前端 5173 · MCP Bridge 8200 · MCP Inspector 6274 · Webhook 9000", size=9.5, color=MUTED)
footer(s, 3)

# ---------------- Slide 4: 大屏 + 设备详情 ----------------
s = new_slide()
title_block(s, "03 · 3D 数字孪生大屏（真实数据）", "左侧：平台总览；右侧：点击设备查看真实 NC 数据")
left, top, w, h = picture_fit(s, SHOTS / "01-平台总览.png", 0.45, 1.32, 6.15, 3.2)
caption(s, 0.45, 4.62, 6.15, "平台总览：数据来源「PT NC 真实采集」、5/5 设备在线、采集状态 netcon 正常、WebSocket 已连接")
picture_fit(s, SHOTS / "02-设备详情.png", 6.85, 1.32, 6.15, 3.2)
caption(s, 6.85, 4.62, 6.15, "设备详情：型号、管理 IP、可达性、端口状态与关联主机全部来自 NC API")
textbox(s, 0.45, 5.45, 12.4, 1.3, "· 3D 场景为配置驱动：楼栋/设备/链路坐标来自场景配置，新增设备自动出现（pending → 确认/撤销）\n· 告警脉冲、数据新鲜度、WebSocket 实时推送：故障注入实测 5 秒内大屏更新", size=11.5, color=MUTED, line_spacing=1.35)
footer(s, 4)

# ---------------- Slide 5: 告警 + 新设备 ----------------
s = new_slide()
title_block(s, "04 · 告警生命周期与场景自动化", "左：告警删除/清空（软删除+审计+撤销）；右：PT 加设备自动发现")
picture_fit(s, SHOTS / "03-告警管理与删除.png", 0.45, 1.32, 6.15, 3.2)
caption(s, 0.45, 4.62, 6.15, "活跃告警禁止删除只能静默；已恢复告警可单条删除/一键清空，10 秒内可撤销并写审计")
picture_fit(s, SHOTS / "04-新设备自动发现.png", 6.85, 1.32, 6.15, 3.2)
caption(s, 6.85, 4.62, 6.15, "PT 加设备 → 采集发现 → 侦察 Agent 推断归属楼栋 → 场景自动出现（此张为 Mock 状态图 ⚠️）")
textbox(s, 0.45, 5.45, 12.4, 1.3, "· 告警恢复判定不依赖 NC（NC 记录滞后 >3 分钟）：用主机 ping / 设备可达性独立判定，标记 stale_source\n· 删除/清空类写操作全部走「用户确认」：Agent 只生成 pending action，不直接执行", size=11.5, color=MUTED, line_spacing=1.35)
footer(s, 5)

# ---------------- Slide 6: 多 Agent 轨迹 ----------------
s = new_slide()
title_block(s, "05 · AI 多 Agent 执行轨迹（LangGraph）", "一次提问的完整执行链：意图 → 子任务计划 → 工具序列 → 汇总")
picture_fit(s, SHOTS / "05-AI多Agent执行轨迹.png", 1.05, 1.25, 11.2, 5.35)
caption(s, 1.05, 6.65, 11.2, "LangGraph StateGraph：意图分类（规则优先 + LLM）→ 8 个 role 节点并行 → 汇总；trace 记录 plan、工具调用、耗时与停止原因")
footer(s, 6)

# ---------------- Slide 7: 危险操作确认 ----------------
s = new_slide()
title_block(s, "06 · 危险操作人机确认（interrupt）", "写操作在图中暂停，用户确认后才执行；取消则数据不变")
picture_fit(s, SHOTS / "06-危险操作人工确认.png", 1.05, 1.25, 11.2, 5.35)
caption(s, 1.05, 6.65, 11.2, "LangGraph interrupt() 暂停图 → 前端红色确认卡片 → 取消后 38 条已恢复告警一条未动（实测）；action 10 分钟 TTL + CAS + 审计")
footer(s, 7)

# ---------------- Slide 8: 路径追踪 ----------------
s = new_slide()
title_block(s, "07 · 路径追踪 3D（真实 NC flow-analysis）", "PC-JX1 → SW-JX-2960 → Core-3650 → Server-1：4 跳，约 1 秒")
picture_fit(s, SHOTS / "07-路径追踪3D.png", 0.65, 1.25, 12.0, 5.3)
caption(s, 0.65, 6.62, 12.0, "调用 NC flow-analysis 真实接口；PT 只返回部分中间设备 → 用拓扑 + 主机接入关系 BFS 补全中间 hop（标 synthetic），3D 发光路径 + 逐跳端口 + 断点定位")
footer(s, 8)

# ---------------- Slide 9: 断点 + 根因分组 ----------------
s = new_slide()
title_block(s, "08 · 断点定位与告警根因分组", "左：链路中断时路径断点；右：根因聚合并抑制下游告警")
picture_fit(s, SHOTS / "08-路径断点定位.png", 0.45, 1.32, 6.15, 3.2)
caption(s, 0.45, 4.62, 6.15, "路径 partial 时最后一跳红色 ✕ + 红色末段 + 面板断点原因（此张为 Mock 状态图 ⚠️）")
picture_fit(s, SHOTS / "09-告警根因分组.png", 6.85, 1.32, 6.15, 3.2)
caption(s, 6.85, 4.62, 6.15, "根因组「上联链路中断」：成员 3、被抑制 2；下游告警灰显「被上游抑制」（此张为 Mock 状态图 ⚠️）")
textbox(s, 0.45, 5.45, 12.4, 1.3, "· 根因优先级：link_down > device_unreachable > device_offline > host_down\n· 抑制仅展示层：不删除告警、不改状态，避免「告警风暴」淹没根因", size=11.5, color=MUTED, line_spacing=1.35)
footer(s, 9)

# ---------------- Slide 10: MCP 读配置 ----------------
s = new_slide()
title_block(s, "09 · Agent 通过 MCP 读取真实配置文件", "Agent → MCP Bridge(8200) → filesystem MCP Server(stdio) → 真实文件")
picture_fit(s, SHOTS / "10-Agent通过MCP读配置.png", 1.05, 1.25, 11.2, 5.35)
caption(s, 1.05, 6.65, 11.2, "trace.mcp_calls：filesystem/read_text_file ok · 6.88ms；回答引用真实 VLAN 10/20/30/40/50 与 trunk 配置；只读白名单，写操作被拒绝")
footer(s, 10)

# ---------------- Slide 11: 流式 + RAG ----------------
s = new_slide()
title_block(s, "10 · AI 流式输出与 RAG 引用", "左：SSE 逐 token 渲染中；右：检索增强 + [K1]~[K5] 引用")
picture_fit(s, SHOTS / "11-AI流式输出.png", 0.45, 1.32, 6.15, 3.2)
caption(s, 0.45, 4.62, 6.15, "SSE 流式中：状态条「正在检索知识库（命中 5 条）」+ 工具 chip + 停止生成按钮")
picture_fit(s, SHOTS / "12-RAG引用.png", 6.85, 1.32, 6.15, 3.2)
caption(s, 6.85, 4.62, 6.15, "RAG：18 个知识块 BM25 检索命中 5 条，回答带 [K1]~[K5] 可点击引用与依据")
textbox(s, 0.45, 5.45, 12.4, 1.3, "· 真实 LLM 经 LangChain model.stream 推流，实测单次 2600+ token 事件；前端 80ms 节流渲染 + 停止生成 + 自动回退\n· 无命中时明确回答「知识库无相关内容」，不编造", size=11.5, color=MUTED, line_spacing=1.35)
footer(s, 11)

# ---------------- Slide 12: MCP 能力清单 ----------------
s = new_slide()
title_block(s, "11 · MCP 双向集成能力清单", "A：对外 MCP Server（10 工具/4 资源/3 提示词）；B：对内 MCP Client 桥接（23 工具）")
picture_fit(s, SHOTS / "13-MCP能力清单.png", 0.85, 1.2, 11.6, 5.5)
caption(s, 0.85, 6.7, 11.6, "官方 SDK 需 Python≥3.10 → MCP 进程独立 Python 3.14 环境，主服务 3.9 零侵入；只读白名单 + localhost + 全量审计日志")
footer(s, 12)

# ---------------- Slide 13: 测试与评测 ----------------
s = new_slide()
title_block(s, "12 · 真实测试与评测数据", "后端 171 / Agent 256 / MCP 40 tests；38 场景双引擎 100%")
picture_fit(s, SHOTS / "14-测试与评测数据.png", 0.85, 1.2, 11.6, 5.5)
caption(s, 0.85, 6.7, 11.6, "自动执行违规 0（所有写操作必须人工确认）；评测集覆盖 17 类场景；全部离线可复现")
footer(s, 13)

# ---------------- Slide 14: Inspector + AI 问答 ----------------
s = new_slide()
title_block(s, "13 · MCP 协议验证与真实 AI 排障", "左：MCP Inspector 连接成功；右：真实 DeepSeek 排障回答")
picture_fit(s, SHOTS / "16-MCP-Server已连接.png", 0.45, 1.32, 6.15, 3.2)
caption(s, 0.45, 4.62, 6.15, "MCP Inspector：campus-twin Connected (1258ms)，协议 MCP 2025-11-25，TOOLS/PROMPTS/RESOURCES 列表全部 OK")
picture_fit(s, SHOTS / "17-AI真实问答排障.png", 6.85, 1.32, 6.15, 3.2)
caption(s, 6.85, 4.62, 6.15, "AI 真实排障：根因、依据（设备/IP/端口/时间）、处置建议；危险操作走确认卡片")
textbox(s, 0.45, 5.45, 12.4, 1.3, "· 任意 MCP 客户端（Claude Desktop / Cursor / VS Code / Inspector）可接入平台能力\n· AI 回答强制引用真实数据；无数据明确说明，不编造", size=11.5, color=MUTED, line_spacing=1.35)
footer(s, 14)

# ---------------- Slide W15-A: RAG 评测闭环 ----------------
s = new_slide()
title_block(s, "14 · RAG 评测闭环（W15）", "56 篇文档 / 107 chunk / 3 种切分 / 48 query / 4 种检索配置")
picture_fit(s, SHOTS / "18-RAG评测闭环.png", 0.6, 1.15, 12.1, 5.6)
footer(s, 15)

# ---------------- Slide W15-B: 可靠性 / 可观测性 / 部署 ----------------
s = new_slide()
title_block(s, "15 · 可靠性 / 可观测性 / 可部署性（W15）", "故障注入 · 断点恢复 · trace API · Docker + GitHub Actions CI")
picture_fit(s, SHOTS / "19-可靠性与可观测性.png", 0.6, 1.15, 12.1, 5.6)
footer(s, 16)

# ---------------- Slide 15: 结尾 ----------------
s = new_slide()
textbox(s, 0.9, 1.0, 11.6, 0.6, "技术栈与核心数据", size=30, color=TEXT, bold=True)
textbox(s, 0.9, 1.75, 11.6, 0.4, "全部真实运行，可现场演示 / 复现", size=14, color=CYAN)
textbox(s, 0.9, 2.35, 5.9, 0.35, "技术栈", size=15, color=CYAN, bold=True)
textbox(s, 0.9, 2.75, 6.0, 2.6, "Python / FastAPI / asyncio / SQLite WAL\nLangGraph / LangChain Core / langchain-openai\nMCP（Server + Client 桥接）\nSSE / WebSocket / RAG（BM25-lite）\nThree.js / Vite / 原生 JS\nCisco Packet Tracer 9 / NC REST API\nCisco IOS-XE（3650 / 2960 / ISR4331）\nDeepSeek / OpenAI 兼容 API", size=12.5, color=TEXT, line_spacing=1.45)
textbox(s, 0.9, 5.55, 6.0, 1.3, "说明：截图均为真实运行；04/08/09 三张为 Mock 状态图，用于展示需故障注入的 UI 状态。", size=10.5, color=MUTED, line_spacing=1.3)
textbox(s, 7.2, 2.35, 5.4, 0.35, "核心数据", size=15, color=GREEN, bold=True)
data = [
    ("真实网络", "5 设备 / 6 主机 / 4 链路"),
    ("测试", "171 + 256 + 40 tests"),
    ("评测", "38 场景：根因/设备/动作 100%"),
    ("安全", "自动执行违规 0"),
    ("AI", "LangGraph 12 节点 / 23 工具"),
    ("MCP", "Server 10 工具 + Bridge 23 工具"),
    ("前端", "3D 大屏 / 流式 / 引用"),
]
y = 2.8
for k, v in data:
    textbox(s, 7.2, y, 1.45, 0.3, k, size=11.5, color=MUTED)
    textbox(s, 8.65, y, 4.0, 0.3, v, size=11.5, color=TEXT, bold=True)
    y += 0.52
textbox(s, 0.9, 6.6, 11.6, 0.3, "个人项目 · 独立开发 · 2026.10", size=11, color=MUTED)
footer(s, 17)

OUT_PPTX.parent.mkdir(parents=True, exist_ok=True)
prs.save(OUT_PPTX)

# 校验
check = Presentation(OUT_PPTX)
print("已生成:", OUT_PPTX)
print("幻灯片数:", len(check.slides))
print("尺寸: %.2f x %.2f 英寸" % (check.slide_width / 914400, check.slide_height / 914400))
for i, slide in enumerate(check.slides, 1):
    title = ""
    for shape in slide.shapes:
        if shape.has_text_frame and shape.text.strip():
            title = shape.text.strip().split("\n")[0][:40]
            break
    print(f"  {i:02d}  {title}")
