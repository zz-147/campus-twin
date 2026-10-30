# -*- coding: utf-8 -*-
"""W3 恢复端 fixture 回放测试（离线，不依赖 PT 在线）。

复刻 2026-10-29 故障注入实验的恢复场景：
    故障阶段：NC /host 中 PC-JX1 pingStatus=FAILURE，NC /assurance/health-issues 有
              "Connectivity / Device is down" issue（issueTimestamp 是仿真时钟）；
    恢复阶段：PC-JX1 pingStatus 已 SUCCESS，但 NC issue 仍在（NC 清除延迟 >3 分钟）。

期望：后端不能等 NC issue 消失，而应基于 host 实际状态生成 cleared，并标记
stale_source=True、detail 说明“NC 问题记录滞后，数据源已恢复”。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx

from app.collectors.netcon import NetconDriver
from app.config import Settings
from app.pipeline import CollectorPipeline
from app.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> Any:
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run_async(coro):
    return asyncio.run(coro)


class ReplayNCServer:
    """用固定 fixture 模拟 NC：可切换 fault / recovered 两个阶段。"""

    def __init__(self) -> None:
        self.phase = "fault"
        self.host_calls = 0
        self.issue_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/ticket"):
            return httpx.Response(
                201,
                json={
                    "response": {
                        "idleTimeout": 900,
                        "serviceTicket": "T-REPLAY",
                        "sessionTimeout": 3600,
                    },
                    "version": "1.0",
                },
            )
        if path.endswith("/network-device"):
            return httpx.Response(
                200,
                json={
                    "response": [
                        {
                            "id": "dev-core-3650",
                            "hostname": "Core-3650",
                            "managementIpAddress": "192.168.10.10",
                            "reachabilityStatus": "Reachable",
                            "collectionStatus": "Managed",
                            "lastUpdated": "2026-10-30 15:00:00",
                        },
                        {
                            "id": "dev-sw-jx",
                            "hostname": "SW-JX-2960",
                            "managementIpAddress": "192.168.10.11",
                            "reachabilityStatus": "Reachable",
                            "collectionStatus": "Managed",
                            "lastUpdated": "2026-10-30 15:00:00",
                        },
                    ],
                    "version": "1.0",
                },
            )
        if path.endswith("/network-device/count"):
            return httpx.Response(200, json={"response": 2, "version": "1.0"})
        if path.endswith("/topology/physical-topology"):
            return httpx.Response(
                200,
                json={
                    "response": {"nodes": [{"id": "FDO13025YJT-uuid", "x": 30, "y": 408}], "links": []},
                    "version": "1.0",
                },
            )
        if path.endswith("/host"):
            self.host_calls += 1
            fixture = (
                "netcon_host_fault.json"
                if self.phase == "fault"
                else "netcon_host_recovered.json"
            )
            return httpx.Response(200, json=load_fixture(fixture))
        if path.endswith("/assurance/health-issues"):
            self.issue_calls += 1
            # 关键：恢复阶段 NC issue 仍然存在（复刻 NC 清除延迟）
            return httpx.Response(200, json=load_fixture("netcon_health_issues_lingering.json"))
        if path.endswith("/network-health"):
            return httpx.Response(
                200,
                json={
                    "healthyClient": "100",
                    "healthyNetworkDevice": "100",
                    "numNetworkDevices": "3",
                    "numUnreachable": "0",
                },
            )
        return httpx.Response(200, json={"version": "1.0"})


def test_fixture_replay_issue_lingering_but_host_recovered(tmp_path):
    replay = ReplayNCServer()
    settings = Settings(
        db_path=str(tmp_path / "replay.db"),
        driver="netcon",
        poll_interval=3600.0,
        simulate_metrics=False,
        nc_timeout=2.0,
    )
    store = Store(settings.db_path)
    store.init_db()
    messages: List[Dict[str, Any]] = []

    async def broadcast(message: Dict[str, Any]) -> None:
        messages.append(message)

    driver = NetconDriver(settings, transport=httpx.MockTransport(replay))
    pipeline = CollectorPipeline(
        settings=settings, store=store, driver=driver, broadcast=broadcast
    )

    async def scenario():
        # ---- 故障阶段（基线轮：静默记录 active 告警）----
        before_poll = time.time()
        await pipeline.poll_once()
        fault_snapshot = pipeline.snapshot_data()
        fault_hosts = {host["hostname"]: host for host in fault_snapshot["hosts"]}
        fault_issues = fault_snapshot["issues"]
        fault_active = store.load_active_alerts()

        # ---- 恢复阶段：host SUCCESS，但 NC issue 仍在 ----
        replay.phase = "recovered"
        await pipeline.poll_once()
        recovered_snapshot = pipeline.snapshot_data()
        # 再多轮询 2 次：NC issue 仍在，但不得反复重新告警（防 flap）
        await pipeline.poll_once()
        await pipeline.poll_once()
        stable_snapshot = pipeline.snapshot_data()
        return (
            before_poll,
            fault_hosts,
            fault_issues,
            fault_active,
            recovered_snapshot,
            stable_snapshot,
        )

    (
        before_poll,
        fault_hosts,
        fault_issues,
        fault_active,
        recovered_snapshot,
        stable_snapshot,
    ) = run_async(scenario())

    # ---- 故障阶段断言 ----
    assert replay.host_calls == 4  # 4 轮采集每轮都刷新 /host（新鲜度）
    assert fault_hosts["PC-JX1"]["ping_status"] == "FAILURE"
    assert len(fault_issues) == 1
    issue = fault_issues[0]
    assert issue["issue_source"] == "PC-JX1"
    assert issue["source_ts"] == "2026-10-29 21:33:14"  # NC 仿真时钟（审计用）
    # observed_at 是宿主机采集时间，不能用仿真时钟
    assert issue["observed_at"] >= before_poll - 1.0
    assert issue["observed_at"] != float(issue["issue_timestamp"].replace("-", "").replace(" ", "").replace(":", ""))
    assert issue["stale_source"] is False

    active_types = {alert.alert_type for alert in fault_active}
    assert active_types == {"host_down", "health_issue"}

    # ---- 恢复阶段断言（核心：不依赖 NC issue 清除）----
    recovered_hosts = {host["hostname"]: host for host in recovered_snapshot["hosts"]}
    assert recovered_hosts["PC-JX1"]["ping_status"] == "SUCCESS"
    assert replay.issue_calls == 4  # 4 轮都收到了仍在的 NC issue

    # NC issue 仍返回，但 host 已恢复 → 标记 stale_source
    lingering_issue = recovered_snapshot["issues"][0]
    assert lingering_issue["stale_source"] is True

    # active 告警全部恢复
    assert store.load_active_alerts() == []
    cleared = store.load_alerts(state="cleared")
    assert len(cleared) == 2
    by_type = {item["alert_type"]: item for item in cleared}
    assert set(by_type) == {"host_down", "health_issue"}
    stale_alert = by_type["health_issue"]
    assert stale_alert["stale_source"] is True
    assert "NC 问题记录滞后" in (stale_alert["clear_reason"] or "")
    host_alert = by_type["host_down"]
    assert host_alert["stale_source"] is False
    assert host_alert["resolved_at"] is not None

    # ---- 稳定性：NC issue 仍存在也不得重新产生 active 告警 ----
    assert store.load_active_alerts() == []
    assert len(store.load_alerts(state="all")) == 2  # 没有新增 incident 行
    assert stable_snapshot["issues"][0]["stale_source"] is True
    assert replay.issue_calls == 4

    # ---- WebSocket：恢复聚合成一条 alerts_cleared 摘要 ----
    cleared_messages = [
        message for message in messages if message["type"] == "alerts_cleared"
    ]
    assert len(cleared_messages) == 1
    summary = cleared_messages[0]["data"]
    assert summary["count"] == 2
    assert summary["stale_source_count"] == 1
    assert "NC 问题记录滞后" in summary["summary"]
    # 恢复阶段没有逐条 alert 帧（只有快照 + 一条摘要）
    recovery_alerts = [
        message
        for message in messages
        if message["type"] == "alert" and message["data"]["alert_type"].endswith("_cleared")
    ]
    assert recovery_alerts == []
