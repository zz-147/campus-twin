# -*- coding: utf-8 -*-
"""W10 Webhook 通知测试（本地 HTTP 接收器，不依赖外部网络）。"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Alert
from app.notify import NotifyService
from app.pipeline import CollectorPipeline
from app.store import Store
from tests.helpers import ScriptedDriver, make_host, make_result, run_async


class LocalReceiver:
    """本地 webhook 接收器（线程 HTTP server），记录收到的 JSON 并可按序返回状态码。"""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.response_statuses: List[int] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                try:
                    receiver.requests.append(json.loads(raw.decode("utf-8")))
                except ValueError:
                    receiver.requests.append({"_raw": raw.decode("utf-8", "replace")})
                status = (
                    receiver.response_statuses.pop(0)
                    if receiver.response_statuses
                    else 200
                )
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):  # noqa: A003
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:{}/webhook".format(self.port)

    def start(self) -> "LocalReceiver":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@contextmanager
def receiver() -> Iterator[LocalReceiver]:
    server = LocalReceiver().start()
    try:
        yield server
    finally:
        server.stop()


def make_settings(url: str, tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        db_path=str(tmp_path / "notify.db"),
        driver="simulator",
        poll_interval=3600.0,
        simulate_metrics=False,
        log_level="WARNING",
        nc_timeout=0.5,
        notify_enabled=True,
        notify_webhook_url=url,
        notify_webhook_type="generic",
        notify_min_severity="critical",
        notify_dedup_seconds=300.0,
        notify_timeout=2.0,
        notify_retry_delays=(0.01, 0.01),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_alert(
    title: str = "测试告警",
    severity: str = "critical",
    dedup_key: str = "link_down:x",
    state: str = "active",
    status: str = "open",
) -> Alert:
    return Alert(
        alert_type="link_down",
        severity=severity,
        title=title,
        detail="链路中断详情",
        dedup_key=dedup_key,
        device_id="dev-1",
        observed_at=time.time(),
        state=state,
        status=status,
    )


class TestNotifyService:
    def test_send_generic_payload_and_dedup(self, tmp_path):
        with receiver() as server:
            settings = make_settings(server.url, tmp_path)

            async def scenario():
                service = NotifyService(settings)
                await service.start()
                try:
                    first = service.enqueue(make_alert(dedup_key="link_down:a"))
                    duplicate = service.enqueue(make_alert(dedup_key="link_down:a"))
                    await asyncio.wait_for(service._queue.join(), timeout=5)
                    return service.status(), first, duplicate
                finally:
                    await service.stop()

            status, first, duplicate = run_async(scenario())
            assert first is True
            assert duplicate is False  # 同 dedup_key 5 分钟内去重
            assert len(server.requests) == 1
            payload = server.requests[0]
            assert payload["title"] == "测试告警"
            assert payload["severity"] == "critical"
            assert payload["device"] == "dev-1"
            assert payload["alert_id"] is None
            assert "time" in payload and "detail" in payload
            assert status["enabled"] is True
            assert status["configured"] is True
            assert status["type"] == "generic"
            assert status["sent_count"] == 1
            assert status["last_sent_at"] is not None
            assert status["last_error"] is None

    def test_severity_state_and_silence_filters(self, tmp_path):
        with receiver() as server:
            settings = make_settings(server.url, tmp_path)

            async def scenario():
                service = NotifyService(settings)
                await service.start()
                try:
                    results = {
                        "warning": service.enqueue(make_alert(severity="warning", dedup_key="w")),
                        "info": service.enqueue(make_alert(severity="info", dedup_key="i")),
                        "cleared": service.enqueue(make_alert(state="cleared", dedup_key="c")),
                        "silenced": service.enqueue(
                            make_alert(status="silenced", dedup_key="s")
                        ),
                    }
                    await asyncio.sleep(0.05)
                    return results, len(server.requests)

                finally:
                    await service.stop()

            results, request_count = run_async(scenario())
            assert results == {
                "warning": False,
                "info": False,
                "cleared": False,
                "silenced": False,
            }
            assert request_count == 0

    def test_retry_on_failure_then_success(self, tmp_path):
        with receiver() as server:
            server.response_statuses = [500, 500, 200]
            settings = make_settings(server.url, tmp_path)

            async def scenario():
                service = NotifyService(settings)
                await service.start()
                try:
                    service.enqueue(make_alert(dedup_key="retry:1"))
                    await asyncio.wait_for(service._queue.join(), timeout=10)
                    return service.status()
                finally:
                    await service.stop()

            status = run_async(scenario())
            assert len(server.requests) == 3  # 初次 + 2 次重试
            assert status["sent_count"] == 1
            assert status["failed_count"] == 2
            assert status["last_error"] is None

    def test_permanent_failure_records_error(self, tmp_path):
        with receiver() as server:
            server.response_statuses = [500, 500, 500]
            settings = make_settings(server.url, tmp_path)

            async def scenario():
                service = NotifyService(settings)
                await service.start()
                try:
                    service.enqueue(make_alert(dedup_key="fail:1"))
                    await asyncio.wait_for(service._queue.join(), timeout=10)
                    return service.status()
                finally:
                    await service.stop()

            status = run_async(scenario())
            assert status["sent_count"] == 0
            assert status["failed_count"] == 3
            assert status["last_error"] and "HTTP 500" in status["last_error"]

    def test_message_format_variants(self, tmp_path):
        for webhook_type in ("dingtalk", "feishu", "wecom"):
            with receiver() as server:
                settings = make_settings(
                    server.url, tmp_path, notify_webhook_type=webhook_type
                )

                async def scenario():
                    service = NotifyService(settings)
                    await service.start()
                    try:
                        service.enqueue(make_alert(dedup_key="fmt:{}".format(webhook_type)))
                        await asyncio.wait_for(service._queue.join(), timeout=5)
                        return service.status()
                    finally:
                        await service.stop()

                run_async(scenario())
                payload = server.requests[0]
                if webhook_type == "feishu":
                    assert payload["msg_type"] == "text"
                    assert "校园网告警" in payload["content"]["text"]
                else:
                    assert payload["msgtype"] == "markdown"
                    assert "测试告警" in json.dumps(payload, ensure_ascii=False)

    def test_send_test_and_disabled(self, tmp_path):
        with receiver() as server:
            enabled = make_settings(server.url, tmp_path)

            async def scenario():
                service = NotifyService(enabled)
                await service.start()
                try:
                    result = await service.send_test()
                    return result, service.status()
                finally:
                    await service.stop()

            result, status = run_async(scenario())
            assert result["ok"] is True and result["sent"] is True
            assert len(server.requests) == 1
            assert "测试" in json.dumps(server.requests[0], ensure_ascii=False)
            assert status["sent_count"] == 1

        disabled = make_settings("http://127.0.0.1:1/webhook", tmp_path, notify_enabled=False)

        async def disabled_scenario():
            service = NotifyService(disabled)
            await service.start()
            try:
                return await service.send_test(), service.status()
            finally:
                await service.stop()

        result, status = run_async(disabled_scenario())
        assert result["ok"] is False
        assert "未启用" in result["error"]
        assert status["enabled"] is False


class TestNotifyApi:
    def test_status_and_test_endpoints(self, tmp_path):
        with receiver() as server:
            app = create_app(make_settings(server.url, tmp_path))
            with TestClient(app) as client:
                status = client.get("/api/notify/status").json()
                assert status["enabled"] is True
                assert status["type"] == "generic"
                assert status["min_severity"] == "critical"
                result = client.post("/api/notify/test").json()
                assert result["ok"] is True
                assert result["sent"] is True
                assert result["status"]["sent_count"] >= 1
                assert len(server.requests) == 1

    def test_disabled_status_and_test(self, tmp_path):
        app = create_app(make_settings("", tmp_path, notify_enabled=False))
        with TestClient(app) as client:
            status = client.get("/api/notify/status").json()
            assert status["enabled"] is False
            result = client.post("/api/notify/test").json()
            assert result["ok"] is False


class TestPipelineNotifyIntegration:
    def test_new_active_alert_enqueued_only_after_baseline(self, tmp_path):
        with receiver() as server:
            settings = make_settings(server.url, tmp_path, driver="netcon")
            store = Store(settings.db_path)
            store.init_db()
            healthy = make_result(
                hosts=[make_host("h1", hostname="PC-1", ping_status="SUCCESS")]
            )
            fault = make_result(
                hosts=[make_host("h1", hostname="PC-1", ping_status="FAILURE")]
            )
            driver = ScriptedDriver([healthy, fault])

            async def scenario():
                service = NotifyService(settings)
                await service.start()
                pipeline = CollectorPipeline(
                    settings=settings,
                    store=store,
                    driver=driver,
                    metrics_driver=None,
                    notify=service,
                )
                try:
                    await pipeline.poll_once()  # 基线：无告警，不发通知
                    await pipeline.poll_once()  # 新 active host_down → 入队
                    await asyncio.wait_for(service._queue.join(), timeout=10)
                    return service.status()
                finally:
                    await service.stop()

            status = run_async(scenario())
            assert status["sent_count"] == 1
            assert len(server.requests) == 1
            assert "主机离线" in server.requests[0]["title"]
            # 入库后回填 DB id，Webhook 消息 alert_id 不为空
            assert server.requests[0]["alert_id"] is not None
