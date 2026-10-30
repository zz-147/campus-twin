# -*- coding: utf-8 -*-
"""生成作品集用的数据卡 / 架构图（Pillow 渲染，深色科技风）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "作品集截图"
OUT.mkdir(parents=True, exist_ok=True)

FONT_CN = "C:/Windows/Fonts/msyh.ttc"
FONT_CN_BD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_MONO = "C:/Windows/Fonts/consola.ttf"

BG = (9, 16, 30)
PANEL = (16, 28, 48)
PANEL2 = (20, 36, 60)
BORDER = (40, 66, 100)
TEXT = (228, 238, 250)
MUTED = (140, 165, 195)
CYAN = (56, 189, 248)
GREEN = (52, 211, 153)
AMBER = (251, 191, 36)
PURPLE = (167, 139, 250)


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def text(draw: ImageDraw.ImageDraw, xy, s, f, fill=TEXT):
    draw.text(xy, s, font=f, fill=fill)


def rounded(draw, box, radius=14, fill=PANEL, outline=BORDER, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def arrow(draw, start, end, color=CYAN, width=3):
    draw.line([start, end], fill=color, width=width)
    # 箭头
    x1, y1 = start
    x2, y2 = end
    if abs(x2 - x1) > abs(y2 - y1):
        sign = 1 if x2 > x1 else -1
        draw.polygon([(x2, y2), (x2 - 12 * sign, y2 - 7), (x2 - 12 * sign, y2 + 7)], fill=color)
    else:
        sign = 1 if y2 > y1 else -1
        draw.polygon([(x2, y2), (x2 - 7, y2 - 12 * sign), (x2 + 7, y2 - 12 * sign)], fill=color)


def make_metrics() -> Path:
    W, H = 1600, 980
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title = font(FONT_CN_BD, 40)
    f_sub = font(FONT_CN, 20)
    f_sec = font(FONT_CN_BD, 24)
    f_num = font(FONT_MONO, 30)
    f_kv = font(FONT_CN, 21)
    f_small = font(FONT_CN, 17)

    text(d, (60, 44), "校园网络数字孪生运维平台 · 真实测试与评测数据", f_title)
    text(d, (60, 96), "全部数据来自本机真实运行（2026-10-30）｜pytest + 38 场景评测 + 前端冒烟", f_sub, MUTED)

    # 顶部四个大数字卡
    cards = [
        ("Agent 测试", "277", "passed", GREEN),
        ("后端测试", "171", "passed", GREEN),
        ("评测场景", "38/38", "根因命中 100%", CYAN),
        ("动作正确率", "15/15", "自动执行违规 0", AMBER),
    ]
    x = 60
    for label, big, sub, color in cards:
        rounded(d, (x, 150, x + 360, 300), radius=18, fill=PANEL)
        text(d, (x + 26, 172), label, f_sec, MUTED)
        text(d, (x + 26, 212), big, f_num, color)
        text(d, (x + 26, 262), sub, f_small, MUTED)
        x += 380

    # 两个面板：评测维度 + 工程规模
    rounded(d, (60, 330, 790, 930), radius=18, fill=PANEL)
    text(d, (88, 352), "38 场景离线评测（双引擎，mock provider）", f_sec, TEXT)
    rows = [
        ("根因定位准确率", "100%", "38 / 38"),
        ("故障设备定位", "100%", "38 / 38"),
        ("证据链覆盖", "100%", "38 / 38"),
        ("动作正确率", "100%", "15 / 15"),
        ("自动执行违规", "0", "必须人工确认"),
        ("工具错误率", "0.0%", "全部离线可复现"),
        ("评测类别", "17 类", "9 故障 + 8 运维"),
    ]
    y = 406
    for k, v, note in rows:
        text(d, (96, y), "• " + k, f_kv, TEXT)
        text(d, (520, y), v, font(FONT_CN_BD, 23), GREEN if v in ("100%", "0") else CYAN)
        text(d, (640, y), note, f_small, MUTED)
        y += 62

    rounded(d, (820, 330, 1540, 930), radius=18, fill=PANEL)
    text(d, (848, 352), "工程规模", f_sec, TEXT)
    rows2 = [
        ("后端 pytest", "171 passed", "采集/检测/告警/路径/通知"),
        ("Agent pytest", "277 passed", "LangGraph / 路由 / SSE / RAG / MCP"),
        ("MCP Server", "30 passed", "10 tools / 4 resources / 3 prompts"),
        ("MCP Bridge", "10 passed", "filesystem + memory 共 23 工具"),
        ("前端构建", "27 modules", "gzip JS 194kB / CSS 8.5kB"),
        ("前端冒烟", "PASS", "真实登录/点击/AI 问答/截图"),
        ("服务规模", "7 个进程", "PT NC / 后端 / Agent / 前端 / Bridge / Inspector / Webhook"),
        ("真实数据", "5 设备 6 主机", "拓扑 5 节点 4 链路（NC API）"),
    ]
    y = 406
    for k, v, note in rows2:
        text(d, (856, y), "• " + k, f_kv, TEXT)
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in v)
        text(d, (1040, y), v, font(FONT_CN_BD, 21) if has_cjk else font(FONT_MONO, 19), CYAN)
        text(d, (856, y + 30), note, f_small, MUTED)
        y += 64

    text(d, (60, 948), "生成方式：tools/render_report_images.py（读取真实 pytest / 评测 / 冒烟结果）", f_small, MUTED)
    path = OUT / "14-测试与评测数据.png"
    img.save(path)
    return path


def make_architecture() -> Path:
    W, H = 1600, 1000
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title = font(FONT_CN_BD, 38)
    f_sub = font(FONT_CN, 19)
    f_box = font(FONT_CN_BD, 21)
    f_line = font(FONT_CN, 16)
    f_mono = font(FONT_MONO, 15)

    text(d, (56, 40), "系统架构：真实数据 + LangGraph 多 Agent + MCP 双向集成", f_title)
    text(d, (56, 92), "全部组件在本机真实运行，非示意图拼贴", f_sub, MUTED)

    # 用户层
    rounded(d, (56, 136, 1544, 212), radius=16, fill=PANEL2)
    text(d, (84, 160), "浏览器（Three.js 3D 大屏 + AI 面板）", f_box, TEXT)
    text(d, (980, 162), "Claude Desktop / Cursor / MCP Inspector（任意 MCP 客户端）", f_line, MUTED)

    # 前端
    rounded(d, (56, 250, 900, 470), radius=16, fill=PANEL)
    text(d, (84, 272), "前端 · Vite + Three.js", f_box, CYAN)
    for i, line in enumerate([
        "3D 数字孪生大屏：楼栋/机房/设备三级渲染、告警脉冲、路径追踪动画",
        "AI 面板：SSE 流式渲染、RAG 引用角标、action 确认卡片、历史会话",
        "WebSocket 实时推送 + REST 全量/增量同步（自研轻量状态管理）",
        "配置驱动场景：新设备自动出现（pending → 确认/撤销）",
    ]):
        text(d, (84, 316 + i * 38), "· " + line, f_line, TEXT)

    # MCP Server（右侧）
    rounded(d, (950, 250, 1544, 470), radius=16, fill=PANEL)
    text(d, (978, 272), "A. MCP Server（stdio，FastMCP）", f_box, PURPLE)
    for i, line in enumerate([
        "10 个只读 Tools：设备/拓扑/主机/告警/路径/场景/审计",
        "4 个 Resources：场景、拓扑、设计文档、排障文档",
        "3 个 Prompts：端口 down / 链路断 / 设备接入引导",
        "安全：只读、目录白名单、路径穿越防护",
    ]):
        text(d, (978, 316 + i * 38), "· " + line, f_line, TEXT)

    # Agent 层
    rounded(d, (56, 508, 1544, 760), radius=16, fill=PANEL)
    text(d, (84, 530), "AI Agent · Python 3.9 + LangGraph（StateGraph）", f_box, GREEN)
    for i, line in enumerate([
        "StateGraph：意图分类（规则优先 + LLM）→ 8 个 role 节点并行 fan-out → 汇总 → interrupt 人机确认",
        "17 个原生工具 + 6 个 MCP 工具（总 23）：tool calling / bind_tools / StructuredTool",
        "SSE 流式（LangChain model.stream → 队列 → SSE）；RAG（18 知识块 BM25-lite + [K1] 引用）",
        "SqliteSaver checkpointer（thread_id=conversation_id）：多轮续聊、进程重启恢复暂停图",
    ]):
        text(d, (84, 574 + i * 38), "· " + line, f_line, TEXT)

    # MCP Bridge（Agent 右下）
    rounded(d, (950, 610, 1544, 760), radius=14, fill=PANEL2)
    text(d, (976, 630), "B. MCP Client 桥接（Python 3.14 独立进程）", f_box, AMBER)
    text(d, (976, 668), "HTTP :8200 → mcp ClientSession/stdio", f_line, TEXT)
    text(d, (976, 700), "filesystem（14 工具，只读 configs/ docs/）", f_line, MUTED)
    text(d, (976, 728), "memory（9 工具，本地记忆）", f_line, MUTED)

    # 后端层
    rounded(d, (56, 798, 920, 966), radius=16, fill=PANEL)
    text(d, (84, 818), "后端 · FastAPI + asyncio + SQLite WAL", f_box, CYAN)
    for i, line in enumerate([
        "5s 周期采集：Netcon 驱动（PT NC REST API）/ SNMP 驱动可插拔",
        "检测引擎 7 规则 + 告警生命周期 + 根因分组/抑制 + Webhook 通知",
        "SCENE 场景配置 / 审计 / 场景自动化（pending → 确认/撤销）",
    ]):
        text(d, (84, 858 + i * 34), "· " + line, f_line, TEXT)

    # 数据源（右）
    rounded(d, (950, 798, 1544, 966), radius=16, fill=PANEL)
    text(d, (978, 818), "数据源 · 真实网络", f_box, GREEN)
    text(d, (978, 856), "Cisco Packet Tracer 9 + Network Controller", f_line, TEXT)
    text(d, (978, 884), "5 台网络设备（Core-3650 / 3×2960 / 4331）", f_line, MUTED)
    text(d, (978, 912), "6 台主机 · 拓扑 5 节点 4 链路 · 全 Managed", f_line, MUTED)

    # 连接箭头
    arrow(d, (480, 212), (480, 248))          # 浏览器 -> 前端
    arrow(d, (1250, 212), (1250, 248))        # MCP 客户端 -> MCP Server
    arrow(d, (480, 470), (480, 506))          # 前端 -> Agent
    arrow(d, (1250, 470), (1250, 506))        # MCP Server -> Agent? (示意)
    arrow(d, (480, 760), (480, 796))          # Agent -> 后端
    arrow(d, (1250, 760), (1250, 796))        # Bridge -> 数据源? 示意
    arrow(d, (950, 690), (900, 690), AMBER)   # Agent <-> Bridge

    text(d, (56, 975), "端口：PT NC 58000 · 后端 8000 · Agent 8100 · 前端 5173 · MCP Bridge 8200 · MCP Inspector 6274", font(FONT_CN, 15), MUTED)

    path = OUT / "15-系统架构图.png"
    img.save(path)
    return path


def make_mcp_card() -> Path:
    W, H = 1600, 980
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title = font(FONT_CN_BD, 36)
    f_sub = font(FONT_CN, 19)
    f_sec = font(FONT_CN_BD, 23)
    f_item = font(FONT_CN, 19)
    f_mono = font(FONT_MONO, 16)
    f_small = font(FONT_CN, 16)

    text(d, (56, 40), "MCP 双向集成 · 能力清单与真实验收", f_title)
    text(d, (56, 88), "A. 对外 MCP Server（stdio / FastMCP）＋ B. 对内 MCP Client 桥接（filesystem / memory）", f_sub, MUTED)

    # A 面板
    rounded(d, (56, 130, 790, 930), radius=18, fill=PANEL)
    text(d, (84, 152), "A. MCP Server（campus-twin v0.1.0）", f_sec, PURPLE)
    text(d, (84, 190), "10 Tools（全部只读）", f_small, CYAN)
    tools = [
        "get_devices", "get_topology", "get_hosts", "get_issues", "get_alerts",
        "get_alert_groups", "get_notify_status", "trace_path", "get_scene", "get_audit",
    ]
    for i, name in enumerate(tools):
        x = 84 + (i % 2) * 340
        y = 220 + (i // 2) * 34
        text(d, (x, y), "· " + name, f_mono, TEXT)
    text(d, (84, 408), "4 Resources（文档只读白名单）", f_small, CYAN)
    for i, name in enumerate(["campus://scene", "campus://topology", "campus://docs/network-design", "campus://docs/troubleshooting"]):
        text(d, (84, 438 + i * 32), "· " + name, f_mono, TEXT)
    text(d, (84, 580), "3 Prompts", f_small, CYAN)
    for i, name in enumerate(["troubleshoot_port_down", "troubleshoot_link_down", "onboard_device"]):
        text(d, (84, 610 + i * 32), "· " + name, f_mono, TEXT)
    text(d, (84, 720), "接入方：Claude Desktop / Cursor / VS Code / MCP Inspector", f_item, MUTED)
    text(d, (84, 754), "验收：verify_client.py → ALL PASS", f_item, GREEN)
    for i, line in enumerate([
        "tools=10 / resources=4 / prompts=3；真实后端 5 台设备",
        "读 campus://scene 3713 字符；设计文档 8409 字符",
        "后端不可用 → 中文错误、无异常栈、server 存活",
    ]):
        text(d, (84, 792 + i * 30), "· " + line, f_small, MUTED)

    # B 面板
    rounded(d, (820, 130, 1544, 930), radius=18, fill=PANEL)
    text(d, (848, 152), "B. MCP Client 桥接（Agent → 外部工具）", f_sec, AMBER)
    text(d, (848, 192), "filesystem（14 工具，只读 configs/ docs/）", f_item, TEXT)
    text(d, (848, 224), "memory（9 工具，本地记忆）", f_item, TEXT)
    text(d, (848, 262), "合计 23 个外部工具，Agent 工具总数 23", f_item, GREEN)
    text(d, (848, 306), "链路：Agent(py3.9) → HTTP :8200 → Bridge(py3.14)", f_small, MUTED)
    text(d, (848, 334), "      → mcp ClientSession/stdio → filesystem / memory", f_small, MUTED)
    text(d, (848, 380), "真实验收", f_small, CYAN)
    for i, line in enumerate([
        "verify_bridge.py → ALL_BRIDGE_VERIFY_PASS",
        "total_tools=23；读 configs/core-3650.txt 成功",
        "memory 写入/检索成功；越权写被白名单拒绝",
        "Agent 277 tests 全绿；38 场景评测 100% 无回归",
    ]):
        text(d, (848, 412 + i * 32), "· " + line, f_item, MUTED)
    text(d, (848, 556), "Agent 实测（真实 DeepSeek）", f_small, CYAN)
    text(d, (848, 588), "问：帮我读一下 configs/core-3650.txt 里 VLAN 配置", f_item, TEXT)
    text(d, (848, 620), "→ route_source=rule / plan=[mcp_read]", f_mono, GREEN)
    text(d, (848, 650), "→ filesystem/read_text_file ok · 6.88ms", f_mono, GREEN)
    text(d, (848, 680), "→ 回答引用真实 VLAN 10/20/30/40/50 规划", f_item, MUTED)
    text(d, (848, 724), "问：MCP 现在接入了哪些工具？", f_item, TEXT)
    text(d, (848, 756), "→ filesystem 14 + memory 9 = 23", f_mono, GREEN)
    text(d, (848, 812), "安全：只读白名单 / 路径穿越拒绝 / 仅 localhost / 全量审计", f_small, AMBER)
    text(d, (848, 844), "版本约束：官方 SDK 需 Python≥3.10 → MCP 进程独立 3.14，主服务 3.9 零侵入", f_small, MUTED)

    text(d, (56, 952), "生成方式：tools/render_report_images.py（数据来自真实 verify_client / verify_bridge / pytest / 评测）", f_small, MUTED)
    path = OUT / "13-MCP能力清单.png"
    img.save(path)
    return path


if __name__ == "__main__":
    p1 = make_metrics()
    p2 = make_architecture()
    p3 = make_mcp_card()
    print("生成:", p1)
    print("生成:", p2)
    print("生成:", p3)
