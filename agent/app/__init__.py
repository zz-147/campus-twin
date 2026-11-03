# -*- coding: utf-8 -*-
"""campus-twin AI 运维 Agent 服务。

对外入口：
- ``app.api``      FastAPI 服务（:8100，POST /api/chat）
- ``app.agent``    编排循环 / provider / 只读工具 / 离线评测
"""

__version__ = "0.7.0"  # W16: 设备接入配置文档 artifact（Markdown 生成/存储/下载）
