# -*- coding: utf-8 -*-
"""campus-twin AI 运维 Agent 服务。

对外入口：
- ``app.api``      FastAPI 服务（:8100，POST /api/chat）
- ``app.agent``    编排循环 / provider / 只读工具 / 离线评测
"""

__version__ = "0.4.0"  # W13: MCP Client 桥接（filesystem/memory 外部只读工具）
