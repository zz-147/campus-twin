# -*- coding: utf-8 -*-
"""W3-A host 新鲜度修复单测（完全离线）。

故障注入实验暴露：故障期间后端 /api/hosts 的 ping_status 仍是 SUCCESS，
而 NC /host 已是 FAILURE。本文件验证：

1. netcon 驱动**每轮**都重新拉取 /host 并用当轮时间重建 host 模型
   （两轮采集 host 端点被调用两次，第二轮状态立刻反映 FAILURE）；
2. 请求带 no-cache 头 + `_ts` cache-buster，避免内嵌 HTTP 服务/中间层缓存旧响应；
3. pipeline 每轮快照刷新 hosts，`/api/hosts` 数据即最新 NC 状态；
4. 时间基准：HealthIssue.observed_at 是宿主机时间，source_ts 是 NC 原始时间。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import httpx

from app.collectors.netcon import NetconDriver
from app.config import Settings
from app.models import HealthIssue
from app.pipeline import CollectorPipeline
from app.store import Store
from tests.helpers import OBSERVED_AT, SIM_CLOCK_TS, ScriptedDriver, make_host, make_result, run_async

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> Any:
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ticket_response() -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "response": {
                "idleTimeout": 900,
                "serviceTicket": "T-W3",
                "sessionTimeout": 3600,
            },
            "version": "1.0",
        },
    )


def empty_response() -> httpx.Response:
    return httpx.Response(200, json={"version": "1.0"})


def host_payload(ping_status: str) -> Dict[str, Any]:
    """构造一条真实 NC 形态的 /host 记录（hostMac / connectedNetworkDeviceName）。"""
    return {
        "response": [
            {
                "id": "PTT0810C5ONuuid",
                "hostName": "PC-JX1",
                "hostIp": "192.168.20.10",
                "hostMac": "0005.5EB1.6A45",
                "hostType": "Pc",
                "connectedInterfaceName": "FastEthernet0/1",
                "connectedNetworkDeviceName": "SW-JX-2960",
                "connectedNetworkDeviceIpAddress": "192.168.10.11",
                "pingStatus": ping_status,
                "lastUpdated": "2026-10-30 15:00:00",
            }
        ],
        "version": "1.0",
    }


class HostScriptNC:
    """按脚本返回 /host 的 Mock NC：第一次 SUCCESS，第二次 FAILURE。"""

    def __init__(self) -> None:
        self.host_calls = 0
        self.host_requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/ticket"):
            return ticket_response()
        if path.endswith("/host"):
            index = min(self.host_calls, 1)
            self.host_calls += 1
            self.host_requests.append(request)
            status = "SUCCESS" if index == 0 else "FAILURE"
            return httpx.Response(200, json=host_payload(status))
        return empty_response()


class TestNetconHostFreshness:
    def test_host_refetched_every_poll_and_status_updates(self):
        script = HostScriptNC()

        async def scenario():
            driver = NetconDriver(
                Settings(
                    nc_base_url="http://127.0.0.1:58000/api/v1",
                    nc_timeout=1.0,
                ),
                transport=httpx.MockTransport(script),
            )
            try:
                first = await driver.collect()
                second = await driver.collect()
                return first, second, driver
            finally:
                await driver.close()

        first, second, driver = run_async(scenario())
        assert first.ok is True and second.ok is True
        assert script.host_calls == 2  # 每轮都真正拉了一次 /host，没有缓存/跳过
        assert first.hosts[0].ping_status == "SUCCESS"
        assert second.hosts[0].ping_status == "FAILURE"  # 第二轮立即反映 NC 最新值
        assert driver.host_fetch_count == 2
        assert driver.hosts_last_count == 1

        # 每轮的 host 都带当轮采集时间（last_seen == collected_at），证明是新鲜数据
        assert first.hosts[0].last_seen == first.collected_at
        assert second.hosts[0].last_seen == second.collected_at
        assert second.hosts_observed_at is not None
        assert second.hosts_observed_at >= first.hosts_observed_at
        assert second.hosts_observed_at == second.collected_at

    def test_requests_are_cache_busted_and_no_store(self):
        script = HostScriptNC()

        async def scenario():
            driver = NetconDriver(
                Settings(nc_base_url="http://127.0.0.1:58000/api/v1", nc_timeout=1.0),
                transport=httpx.MockTransport(script),
            )
            try:
                await driver.collect()
                await driver.collect()
                return driver
            finally:
                await driver.close()

        driver = run_async(scenario())
        assert len(script.host_requests) == 2
        timestamps = []
        for request in script.host_requests:
            cache_control = request.headers.get("cache-control", "")
            assert "no-cache" in cache_control and "no-store" in cache_control
            assert request.headers.get("pragma") == "no-cache"
            ts = request.url.params.get("_ts")
            assert ts, "请求应带 _ts cache-buster 参数"
            timestamps.append(ts)
        # 两轮都会带 _ts；毫秒级可能相同（快速测试），只要求存在且为数字时间戳
        assert all(ts.isdigit() and len(ts) >= 13 for ts in timestamps)
        assert driver.status()["hosts_observed_at"] is not None
        assert driver.status()["host_fetch_count"] == 2


class TestPipelineHostFreshness:
    def _make_pipeline(self, tmp_path, results):
        settings = Settings(
            db_path=str(tmp_path / "campus.db"),
            driver="netcon",
            poll_interval=3600.0,
            simulate_metrics=False,
        )
        store = Store(settings.db_path)
        store.init_db()
        driver = ScriptedDriver(results)
        messages: List[Dict[str, Any]] = []

        async def broadcast(message: Dict[str, Any]) -> None:
            messages.append(message)

        pipeline = CollectorPipeline(
            settings=settings, store=store, driver=driver, broadcast=broadcast
        )
        return pipeline, store, messages

    def test_snapshot_hosts_and_store_refresh_each_poll(self, tmp_path):
        first_result = make_result(
            devices=[], hosts=[make_host("h1", hostname="PC-JX1", ping_status="SUCCESS")]
        )
        second_result = make_result(
            devices=[], hosts=[make_host("h1", hostname="PC-JX1", ping_status="FAILURE")]
        )
        pipeline, store, messages = self._make_pipeline(
            tmp_path, [first_result, second_result]
        )

        async def scenario():
            await pipeline.poll_once()
            assert pipeline.snapshot_data()["hosts"][0]["ping_status"] == "SUCCESS"
            await pipeline.poll_once()
            return store.load_latest_hosts()

        stored_hosts = run_async(scenario())
        snapshot = pipeline.snapshot_data()
        # /api/hosts 的数据视图每轮刷新为最新 NC 状态
        assert snapshot["hosts"][0]["ping_status"] == "FAILURE"
        assert snapshot["hosts"][0]["last_seen"] == second_result.collected_at
        assert snapshot["hosts_observed_at"] == second_result.hosts_observed_at
        # 持久化层同样是本轮状态（覆盖写，不做增量合并）
        assert stored_hosts[0]["ping_status"] == "FAILURE"
        assert stored_hosts[0]["snapshot_captured_at"] == second_result.collected_at

        # 第二轮的 FAILURE 应触发 host_down 告警（说明检测用的是新鲜 host 状态）
        alert_messages = [msg for msg in messages if msg["type"] == "alert"]
        assert any(msg["data"]["alert_type"] == "host_down" for msg in alert_messages)


class TestTimeBaseNormalization:
    def test_issue_uses_host_observed_at_and_keeps_nc_source_ts(self):
        raw = {
            "issueId": "NC-ISSUE-JX1",
            "issueName": "Connectivity",
            "issueDescription": "Device is down",
            "issueSource": "PC-JX1",
            "issueTimestamp": SIM_CLOCK_TS,  # NC 仿真设备时钟（比宿主机慢约 10 分钟）
        }
        issue = HealthIssue.from_netcon(raw, seen_at=OBSERVED_AT)
        assert issue.observed_at == OBSERVED_AT  # 宿主机采集时间
        assert issue.source_ts == SIM_CLOCK_TS  # NC 原始时间字符串（审计用）
        assert issue.issue_timestamp == SIM_CLOCK_TS  # 原始字段保留
        assert issue.stale_source is False

    def test_issue_numeric_timestamp_is_stringified_for_audit(self):
        raw = {
            "issueId": "i-1",
            "issueName": "Interface Down",
            "issueTimestamp": 1761751200000,
        }
        issue = HealthIssue.from_netcon(raw, seen_at=OBSERVED_AT)
        assert issue.observed_at == OBSERVED_AT
        assert issue.source_ts == "1761751200000"
