# -*- coding: utf-8 -*-
"""W15-P1：Agent 工具调用可靠性层（重试 / 熔断 / 幂等 / 脏数据归一化）。

设计目标：让“工具/依赖失败”变成可观测、可降级的数据，而不是异常栈。
- 重试：超时/5xx/连接错误，指数退避（默认 1s、3s，可配）；
- 熔断：同一依赖连续失败 N 次（默认 5）后 OPEN 60s，期间快速失败返回中文；
- 幂等：写操作/重复请求按 dedup key + 窗口缓存，返回“已处理”标记；
- 脏数据：非 JSON / 结构异常 / 超大 / 类型错误 → 归一化为 ``ok=false + error``。

该模块不依赖第三方库（httpx/requests 都不是必须），用 ``getattr(resp, "status_code")``
和异常类型名做兼容；测试用标准库 ``http.server`` 注入 timeout/500/脏 JSON/超大响应。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

#: 可重试异常类型名（httpx/requests/urllib 均覆盖，不硬依赖）
_RETRYABLE_EXCEPTIONS = {
    "TimeoutError",
    "ReadTimeout",
    "ConnectTimeout",
    "TimeoutException",
    "ConnectionError",
    "ConnectError",
    "RemoteProtocolError",
    "ProtocolError",
    "OSError",
}


def _is_retryable_exception(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in _RETRYABLE_EXCEPTIONS:
        return True
    text = str(exc).lower()
    return any(word in text for word in ("timeout", "timed out", "connection", "temporarily", "reset"))


def _is_retryable_response(response: Any) -> bool:
    status = getattr(response, "status_code", None)
    return isinstance(status, int) and (status >= 500 or status == 429)


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    if hasattr(response, "json"):
        try:
            return json.dumps(response.json(), ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return str(response)
    return str(response)


@dataclass
class ResilienceConfig:
    max_retries: int = 2
    retry_delays: Tuple[float, ...] = (1.0, 3.0)
    timeout_s: float = 5.0
    failure_threshold: int = 5
    cooldown_s: float = 60.0
    max_payload_chars: int = 300_000
    idempotency_window_s: float = 300.0


@dataclass
class CircuitBreaker:
    """单依赖熔断器（CLOSED → OPEN → HALF_OPEN）。"""

    failure_threshold: int = 5
    cooldown_s: float = 60.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _failures: int = 0
    _opened_at: Optional[float] = None
    state: str = "closed"
    last_error: Optional[str] = None

    def allow(self) -> Tuple[bool, Optional[str]]:
        with self._lock:
            if self.state == "open":
                if self._opened_at is not None and time.monotonic() - self._opened_at >= self.cooldown_s:
                    self.state = "half_open"
                    return True, None
                remain = 0.0
                if self._opened_at is not None:
                    remain = max(0.0, self.cooldown_s - (time.monotonic() - self._opened_at))
                return False, "依赖熔断中，约 %.0fs 后重试（最近错误：%s）" % (
                    remain,
                    (self.last_error or "unknown")[:120],
                )
            return True, None

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self.state = "closed"
            self._opened_at = None
            self.last_error = None

    def record_failure(self, error: str) -> None:
        with self._lock:
            self._failures += 1
            self.last_error = str(error)[:300]
            if self._failures >= self.failure_threshold:
                self.state = "open"
                self._opened_at = time.monotonic()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "failures": self._failures,
                "last_error": self.last_error,
                "opened_at": self._opened_at,
            }


class CircuitBreakerRegistry:
    def __init__(self, config: Optional[ResilienceConfig] = None) -> None:
        self.config = config or ResilienceConfig()
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, dependency: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(dependency)
            if breaker is None:
                breaker = CircuitBreaker(
                    failure_threshold=self.config.failure_threshold,
                    cooldown_s=self.config.cooldown_s,
                )
                self._breakers[dependency] = breaker
            return breaker

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            items = list(self._breakers.items())
        return {name: breaker.snapshot() for name, breaker in items}


class IdempotencyCache:
    """同 key + 窗口内的重复请求检测（内存实现，线程安全）。"""

    def __init__(self, window_s: float = 300.0) -> None:
        self.window_s = float(window_s)
        self._entries: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            created, value = entry
            if time.monotonic() - created > self.window_s:
                self._entries.pop(key, None)
                return None
            return dict(value)

    def put(self, key: str, value: Dict[str, Any]) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), dict(value))
            now = time.monotonic()
            expired = [
                item_key
                for item_key, (created, _) in self._entries.items()
                if now - created > self.window_s
            ]
            for item_key in expired:
                self._entries.pop(item_key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def error_envelope(message: str, *, tool: str = "", dependency: str = "", detail: Any = None) -> Dict[str, Any]:
    return {
        "ok": False,
        "tool": tool,
        "dependency": dependency,
        "summary": message,
        "data": {},
        "error": message,
        "detail": detail,
    }


def normalize_envelope(
    raw: Any,
    *,
    tool: str = "",
    dependency: str = "",
    max_chars: int = 300_000,
) -> Dict[str, Any]:
    """把任意工具返回归一化为 ``{ok, summary, data, error, ...}``，不抛栈。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return error_envelope(
                "工具返回非 JSON 文本（脏数据，已归一化为失败）",
                tool=tool,
                dependency=dependency,
                detail=str(raw)[:500],
            )
    if not isinstance(raw, dict):
        return error_envelope(
            "工具返回类型异常：%s（期望 JSON object）" % type(raw).__name__,
            tool=tool,
            dependency=dependency,
        )
    try:
        encoded = json.dumps(raw, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return error_envelope(
            "工具返回无法序列化：%s" % exc,
            tool=tool,
            dependency=dependency,
        )
    if len(encoded) > max_chars:
        return error_envelope(
            "工具返回过大（%d 字符 > %d），已拒绝并归一化为失败" % (len(encoded), max_chars),
            tool=tool,
            dependency=dependency,
            detail={"payload_chars": len(encoded)},
        )
    envelope = dict(raw)
    ok = envelope.get("ok")
    if not isinstance(ok, bool):
        # 没有 ok 字段时：有 error 视为失败，否则容错为成功并把原 dict 放 data
        envelope["ok"] = not bool(envelope.get("error"))
    if envelope.get("ok"):
        envelope.setdefault("summary", str(envelope.get("summary") or ""))
        data = envelope.get("data")
        if data is None:
            envelope["data"] = {}
        elif not isinstance(data, dict):
            envelope["data"] = {"value": data}
        envelope.setdefault("error", None)
    else:
        error = str(envelope.get("error") or envelope.get("detail") or "工具调用失败")
        envelope["ok"] = False
        envelope["error"] = error
        envelope["summary"] = str(envelope.get("summary") or error)
        envelope.setdefault("data", {})
    envelope.setdefault("tool", tool)
    envelope.setdefault("dependency", dependency)
    return envelope


class ResilientExecutor:
    """带重试/熔断/脏数据归一化的统一调用封装。

    用法::

        executor = ResilientExecutor()
        envelope = executor.call("backend", lambda: client.get_json("/api/x"))
        envelope = executor.call("backend", fetch_fn, idempotency_key="clear-alert-1")
    """

    def __init__(
        self,
        config: Optional[ResilienceConfig] = None,
        *,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.config = config or ResilienceConfig()
        self.breakers = CircuitBreakerRegistry(self.config)
        self.idempotency = IdempotencyCache(self.config.idempotency_window_s)
        self._sleep = sleep or time.sleep
        self.calls = 0
        self.retries = 0
        self.failures = 0
        self.circuit_rejections = 0
        self._lock = threading.Lock()

    # ---- 通用调用 ----
    def call(
        self,
        dependency: str,
        fn: Callable[..., Any],
        *args: Any,
        idempotency_key: Optional[str] = None,
        normalize: bool = True,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if idempotency_key:
            cached = self.idempotency.get(idempotency_key)
            if cached is not None:
                cached = dict(cached)
                cached["cached"] = True
                cached["summary"] = "重复请求已处理：%s" % cached.get("summary", "")
                return cached
        breaker = self.breakers.get(dependency)
        allowed, message = breaker.allow()
        if not allowed:
            with self._lock:
                self.circuit_rejections += 1
            return error_envelope(
                message or "依赖熔断中",
                dependency=dependency,
                detail={"circuit": breaker.snapshot()},
            )
        attempts = int(self.config.max_retries) + 1
        last_error = "工具调用失败"
        for attempt in range(attempts):
            try:
                with self._lock:
                    self.calls += 1
                result = fn(*args, **kwargs)
                # 兼容 httpx/urllib response 对象：先处理状态码，再取 body
                if hasattr(result, "status_code") and not isinstance(result, dict):
                    status = int(getattr(result, "status_code") or 0)
                    body = _response_text(result)
                    if status >= 500 or status == 429:
                        last_error = "HTTP %s（可重试）：%s" % (status, body[:300])
                        if attempt < attempts - 1:
                            with self._lock:
                                self.retries += 1
                            delay = (
                                self.config.retry_delays[attempt]
                                if attempt < len(self.config.retry_delays)
                                else self.config.retry_delays[-1]
                            )
                            self._sleep(max(0.0, float(delay)))
                            continue
                        breaker.record_failure(last_error)
                        with self._lock:
                            self.failures += 1
                        envelope = error_envelope(
                            "工具调用失败（重试 %d 次后仍失败）：%s" % (attempts - 1, last_error),
                            dependency=dependency,
                            detail={"attempts": attempts, "status_code": status},
                        )
                        if idempotency_key:
                            self.idempotency.put(idempotency_key, envelope)
                        return envelope
                    if status >= 400:
                        breaker.record_failure("HTTP %s" % status)
                        with self._lock:
                            self.failures += 1
                        return error_envelope(
                            "工具调用失败（HTTP %s，不可重试）：%s" % (status, body[:300]),
                            dependency=dependency,
                            detail={"status_code": status},
                        )
                    result = body
                if normalize:
                    envelope = normalize_envelope(
                        result,
                        dependency=dependency,
                        max_chars=self.config.max_payload_chars,
                    )
                else:
                    envelope = result if isinstance(result, dict) else {"ok": False, "error": str(result)}
                if envelope.get("ok"):
                    breaker.record_success()
                    if idempotency_key:
                        self.idempotency.put(idempotency_key, envelope)
                    return envelope
                breaker.record_failure(str(envelope.get("error") or "tool failed"))
                with self._lock:
                    self.failures += 1
                if idempotency_key:
                    self.idempotency.put(idempotency_key, envelope)
                return envelope
            except Exception as exc:  # noqa: BLE001
                last_error = "%s: %s" % (type(exc).__name__, str(exc)[:300])
                retryable = _is_retryable_exception(exc)
                if not retryable:
                    breaker.record_failure(last_error)
                    with self._lock:
                        self.failures += 1
                    return error_envelope(
                        "工具调用失败（不可重试）：%s" % last_error,
                        dependency=dependency,
                        detail={"attempts": attempt + 1},
                    )
                if attempt < attempts - 1:
                    with self._lock:
                        self.retries += 1
                    delay = (
                        self.config.retry_delays[attempt]
                        if attempt < len(self.config.retry_delays)
                        else self.config.retry_delays[-1]
                    )
                    self._sleep(max(0.0, float(delay)))
                    continue
                breaker.record_failure(last_error)
                with self._lock:
                    self.failures += 1
                envelope = error_envelope(
                    "工具调用失败（重试 %d 次后仍失败）：%s" % (attempts - 1, last_error),
                    dependency=dependency,
                    detail={"attempts": attempts},
                )
                if idempotency_key:
                    self.idempotency.put(idempotency_key, envelope)
                return envelope
        return error_envelope(last_error, dependency=dependency)

    def stats(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "retries": self.retries,
            "failures": self.failures,
            "circuit_rejections": self.circuit_rejections,
            "breakers": self.breakers.snapshot(),
        }


#: 模块级默认执行器（测试可替换/清理）
_DEFAULT_EXECUTOR: Optional[ResilientExecutor] = None


def get_default_executor() -> ResilientExecutor:
    global _DEFAULT_EXECUTOR
    if _DEFAULT_EXECUTOR is None:
        _DEFAULT_EXECUTOR = ResilientExecutor()
    return _DEFAULT_EXECUTOR


def reset_default_executor() -> None:
    global _DEFAULT_EXECUTOR
    _DEFAULT_EXECUTOR = None
