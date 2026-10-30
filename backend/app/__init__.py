# -*- coding: utf-8 -*-
"""Campus Twin 后端服务包。

分层：

    config.py             运行配置（环境变量可覆盖）
    models.py             跨数据源统一的 Pydantic 数据模型 + from_netcon 归一化
    collectors/           可插拔采集驱动（netcon 真 REST / simulator 行为模型）
    store.py              SQLite 持久化（WAL）
    pipeline.py           异步采集循环 + 变更检测 + 告警 + WebSocket 广播
    main.py               FastAPI 应用（REST + WebSocket）
"""

__version__ = "0.1.0"
