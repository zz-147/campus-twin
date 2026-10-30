# -*- coding: utf-8 -*-
"""敏感信息脱敏工具。

约束（任务硬性要求）：API key 只从 .env / 环境变量读取，
任何日志、异常消息、API 响应、trace 中都不得回显 key。
本模块集中实现脱敏，供 provider / 配置 / smoke 脚本复用。
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

REDACTED = "***REDACTED***"

# 常见密钥形态：sk-xxx / Bearer xxx / api_key=xxx / Authorization: xxx
_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9_\-\.]{6,}\b"),
    re.compile(r"(?i)\b(sk|key|token)[-_]?[A-Za-z0-9]{0,4}[-_][A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s,;\"']+)"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|secret)\s*[=:]\s*)([^\s,;\"'&]+)"),
)


def redact(text: object, secrets: Iterable[Optional[str]] = ()) -> str:
    """把文本中的密钥替换成 ``***REDACTED***``。

    ``secrets`` 是当前配置里已知的 key，直接按值替换（处理非 sk- 前缀的 key）。
    """
    value = "" if text is None else str(text)
    for secret in secrets:
        if not secret or len(str(secret)) < 4:
            continue
        value = value.replace(str(secret), REDACTED)
    for pattern in _PATTERNS:
        value = pattern.sub(lambda match: (match.group(1) if match.groups() else "") + REDACTED, value)
    return value


def key_hint(api_key: Optional[str]) -> str:
    """密钥状态提示（绝不回显内容，仅给出是否配置/长度量级）。"""
    if not api_key:
        return "未配置"
    length = len(str(api_key))
    if length < 12:
        bucket = "过短(可能无效)"
    elif length < 32:
        bucket = "已配置"
    else:
        bucket = "已配置(标准长度)"
    return "%s，长度 %d" % (bucket, length)
