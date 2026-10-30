# -*- coding: utf-8 -*-
"""campus-twin AI 运维 Agent 服务。

对外入口：
- ``app.api``      FastAPI 服务（:8100，POST /api/chat）
- ``app.agent``    编排循环 / provider / 只读工具 / 离线评测
"""

__version__ = "0.6.0"  # W15-P1: 可靠性（重试/熔断/幂等）+ 断点恢复 + trace 可观测性
