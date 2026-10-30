# -*- coding: utf-8 -*-
"""W3 检测引擎包（自建规则 + 恢复判定 + 告警降噪支撑）。

对外只暴露 :class:`DetectionEngine` 及其结果对象；规则细节见 ``rules.py``。
"""
from app.detection.rules import (
    ClearedEvent,
    DetectionConfig,
    DetectionEngine,
    DetectionOutcome,
    RULE_CATALOG,
)

__all__ = [
    "ClearedEvent",
    "DetectionConfig",
    "DetectionEngine",
    "DetectionOutcome",
    "RULE_CATALOG",
]
