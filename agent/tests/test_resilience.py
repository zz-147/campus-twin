# -*- coding: utf-8 -*-
"""W15-P1：可靠性层单测（重试 / 熔断 / 幂等 / 脏数据归一化，本地 HTTP mock）。"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterator, List

import pytest

from app.agent.resilience import (
    CircuitBreaker,
    IdempotencyCache,
    ResilienceConfig,
    ResilientExecutor,
    normalize_envelope,
)


class _Handler(BaseHTTPRequestHandler):
    counts: Dict[str, int] = {}
    lock = threading.Lock()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _increment(self, key: str) -> None:
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + 1

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        self._increment(path)
        if path == "/timeout":
            time.sleep(0.35)
            self._send(200, json.dumps({"ok": True, "summary": "late"}))
        elif path == "/500":
            self._send(500, json.dumps({"error": "upstream failed"}))
        elif path == "/dirty":
            self._send(200, "this is not json <html>")
        elif path == "/huge":
            self._send(200, json.dumps({"ok": True, "data": {"blob": "x" * 5000}}))
        else:
            self._send(200, json.dumps({"ok": True, "summary": "ok", "data": {"path": path}}))

    def _send(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:  # noqa: BLE001
            pass


class _Response:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


@pytest.fixture()
def mock_server() -> Iterator[Dict[str, Any]]:
    _Handler.counts = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]

    def fetch(path: str, timeout: float = 0.5) -> Any:
        try:
            with urllib.request.urlopen(base + path, timeout=timeout) as response:
                return _Response(response.status, response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # 500/4xx 不抛，交给执行器判定
            return _Response(exc.code, exc.read().decode("utf-8"))

    yield {"fetch": fetch, "base": base, "counts": _Handler.counts}
    server.shutdown()
    server.server_close()


def _executor(**kwargs: Any) -> ResilientExecutor:
    config = ResilienceConfig(
        max_retries=2,
        retry_delays=(0.0, 0.0),
        failure_threshold=2,
        cooldown_s=0.05,
        max_payload_chars=1000,
        idempotency_window_s=60,
        **kwargs,
    )
    return ResilientExecutor(config, sleep=lambda _seconds: None)


def test_success_and_dirty_and_oversize(mock_server: Dict[str, Any]) -> None:
    executor = _executor()
    ok = executor.call("backend", mock_server["fetch"], "/ok")
    assert ok["ok"] is True
    assert ok["data"]["path"] == "/ok"
    dirty = executor.call("backend", mock_server["fetch"], "/dirty")
    assert dirty["ok"] is False
    assert "非 JSON" in dirty["error"]
    huge = executor.call("backend", mock_server["fetch"], "/huge")
    assert huge["ok"] is False
    assert "过大" in huge["error"]


def test_retry_5xx_and_timeout(mock_server: Dict[str, Any]) -> None:
    executor = _executor()
    result = executor.call("backend", mock_server["fetch"], "/500")
    assert result["ok"] is False
    assert "500" in (result["error"] or "") or "重试" in (result["error"] or "")
    assert executor.retries == 2
    assert mock_server["counts"].get("/500") == 3
    # timeout：urllib 超时 → 可重试 → 重试 2 次后失败
    timeout_result = executor.call(
        "backend", lambda: mock_server["fetch"]("/timeout", timeout=0.1)
    )
    assert timeout_result["ok"] is False
    assert mock_server["counts"].get("/timeout", 0) >= 1
    assert executor.retries >= 3


def test_circuit_breaker_open_and_recover(mock_server: Dict[str, Any]) -> None:
    executor = _executor()
    for _ in range(2):
        result = executor.call("backend", mock_server["fetch"], "/500")
        assert result["ok"] is False
    breaker = executor.breakers.get("backend")
    assert breaker.state == "open"
    before = mock_server["counts"].get("/500", 0)
    fast = executor.call("backend", mock_server["fetch"], "/500")
    assert fast["ok"] is False
    assert "熔断" in fast["error"]
    assert mock_server["counts"].get("/500", 0) == before  # 不再发请求
    assert executor.circuit_rejections == 1
    time.sleep(0.06)
    recovered = executor.call("backend", mock_server["fetch"], "/ok")
    assert recovered["ok"] is True
    assert executor.breakers.get("backend").state == "closed"


def test_idempotency_cache(mock_server: Dict[str, Any]) -> None:
    executor = _executor()
    first = executor.call("write", mock_server["fetch"], "/ok", idempotency_key="same-key")
    second = executor.call("write", mock_server["fetch"], "/ok", idempotency_key="same-key")
    assert first["ok"] is True and second["ok"] is True
    assert second["cached"] is True
    assert mock_server["counts"].get("/ok") == 1
    assert "重复请求" in second["summary"]


def test_normalize_envelope_edge_cases() -> None:
    assert normalize_envelope([1, 2, 3])["ok"] is False
    assert "类型异常" in normalize_envelope([1, 2, 3])["error"]
    envelope = normalize_envelope({"summary": "no ok field"})
    assert envelope["ok"] is True
    assert envelope["data"] == {}
    failed = normalize_envelope({"ok": False, "error": "boom"})
    assert failed["ok"] is False and failed["error"] == "boom"
    assert CircuitBreaker(failure_threshold=2).state == "closed"


def test_idempotency_cache_direct() -> None:
    cache = IdempotencyCache(window_s=0.02)
    assert cache.get("k") is None
    cache.put("k", {"ok": True})
    assert cache.get("k")["ok"] is True
    time.sleep(0.03)
    assert cache.get("k") is None
