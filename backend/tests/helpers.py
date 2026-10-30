# -*- coding: utf-8 -*-
"""测试公共构造器：CollectResult / Device / Host / Link / Issue 的简洁工厂。

让规则单测只关心「场景差异」，不用重复拼装 Pydantic 模型。
所有时间都显式传入，保证时间基准断言可复现。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from app.collectors.base import CollectResult
from app.detection.rules import DetectionEngine
from app.models import Alert, Device, HealthIssue, Host, Link, Topology, TopologyNode

# 固定“宿主机采集时间”，模拟 PT 仿真时钟比宿主机慢 10 分钟的场景
OBSERVED_AT = 1793340000.0  # 2026-10-30 前后任意固定 epoch
SIM_CLOCK_TS = "2026-10-29 21:33:14"  # NC issueTimestamp（仿真设备时钟）


def run_async(coro):
    return asyncio.run(coro)


def make_device(
    device_id: str = "d1",
    hostname: str = "SW-1",
    reachability: str = "Reachable",
    ip: str = "10.0.0.1",
    collection_status: str = "Managed",
    last_updated: Optional[str] = None,
) -> Device:
    return Device(
        id=device_id,
        hostname=hostname,
        management_ip=ip,
        reachability=reachability,
        collection_status=collection_status,
        last_updated=last_updated,
        source="netcon",
    )


def make_host(
    host_id: str = "h1",
    hostname: str = "PC-1",
    ping_status: str = "SUCCESS",
    ip: str = "10.0.20.10",
    connected_device_name: Optional[str] = "SW-1",
    connected_device_id: Optional[str] = None,
    connected_interface: Optional[str] = "Fa0/1",
    last_updated: Optional[str] = None,
) -> Host:
    return Host(
        id=host_id,
        hostname=hostname,
        ip=ip,
        ping_status=ping_status,
        connected_device_name=connected_device_name,
        connected_device_id=connected_device_id,
        connected_interface=connected_interface,
        last_updated=last_updated,
        source="netcon",
    )


def make_link(
    link_id: str = "l1",
    source: str = "d1",
    target: str = "d2",
    status: Optional[str] = "up",
    start_port: str = "Gi0/1",
    end_port: str = "Gi0/2",
) -> Link:
    return Link(
        id=link_id,
        source=source,
        target=target,
        start_port_name=start_port,
        end_port_name=end_port,
        link_status=status,
        data_source="netcon",
    )


def make_issue(
    issue_id: str = "i-1",
    name: str = "Connectivity",
    description: str = "Device is down",
    issue_source: Optional[str] = "PC-1",
    severity: str = "critical",
    source_ts: Optional[str] = SIM_CLOCK_TS,
) -> HealthIssue:
    return HealthIssue(
        issue_id=issue_id,
        issue_name=name,
        issue_description=description,
        issue_source=issue_source,
        severity=severity,
        issue_timestamp=source_ts,
        source_ts=source_ts,
        observed_at=OBSERVED_AT,
        source="netcon",
        first_seen=OBSERVED_AT,
        last_seen=OBSERVED_AT,
    )


def make_result(
    devices: Optional[List[Device]] = None,
    hosts: Optional[List[Host]] = None,
    links: Optional[List[Link]] = None,
    issues: Optional[List[HealthIssue]] = None,
    errors: Optional[Dict[str, str]] = None,
    collected_at: float = OBSERVED_AT,
    ok: bool = True,
) -> CollectResult:
    device_list = list(devices or [])
    host_list = list(hosts or [])
    link_list = list(links or [])
    issue_list = list(issues or [])
    return CollectResult(
        devices=device_list,
        hosts=host_list,
        topology=Topology(
            nodes=[TopologyNode(id=device.id, source="netcon") for device in device_list],
            links=link_list,
            captured_at=collected_at,
            source="netcon",
        ),
        issues=issue_list,
        source="netcon",
        ok=ok,
        errors=errors or {},
        collected_at=collected_at,
        hosts_observed_at=collected_at if not (errors or {}).get("hosts") else None,
    )


def evaluate(
    current: CollectResult,
    previous_result: Optional[CollectResult] = None,
    active_alerts: Optional[List[Alert]] = None,
    baseline: bool = False,
    engine: Optional[DetectionEngine] = None,
):
    """便捷入口：直接跑一轮检测（测试用）。"""
    from app.pipeline import PipelineState

    engine = engine or DetectionEngine()
    previous = PipelineState.from_result(previous_result) if previous_result else None
    return engine.evaluate(
        previous,
        current,
        active_alerts=active_alerts or [],
        baseline=baseline,
    )


class ScriptedDriver:
    """按脚本返回 CollectResult 的驱动（pipeline 集成测试用，完全离线）。"""

    name = "scripted"

    def __init__(self, results: List[CollectResult]) -> None:
        self.results = results
        self.calls = 0

    async def probe(self) -> bool:
        return True

    async def collect(self) -> CollectResult:
        index = min(self.calls, len(self.results) - 1)
        self.calls += 1
        result = self.results[index]
        # 每轮模拟新的宿主机采集时间（保持顺序，便于断言 observed_at 递进），
        # 并像真实驱动一样用本轮 seen_at 刷新 host.last_seen。
        if self.calls > 1:
            result.collected_at = result.collected_at + self.calls
            result.hosts_observed_at = result.collected_at
        for host in result.hosts:
            host.last_seen = result.collected_at
        return result

    async def close(self) -> None:
        return None


async def fake_broadcast_factory():
    messages: List[Dict[str, Any]] = []

    async def broadcast(message: Dict[str, Any]) -> None:
        messages.append(message)

    return messages, broadcast
