# -*- coding: utf-8 -*-
"""simulator 行为模型单测。

覆盖验收要求：
- 仿真数据全部显式 ``source="simulator"``（不冒充真实采集）
- 指标生成是**确定性**行为模型，不是裸随机数
- 分时段基础负载：白天高峰利用率显著高于凌晨
- 故障后流量重分布：链路故障后其余链路利用率上升、故障端口归零
- 生成器可跟随外部（netcon 真实）拓扑生成指标，且不修改传入的真实数据模型
"""
from __future__ import annotations

import asyncio
import inspect
import math
from datetime import datetime
from typing import Any, Dict, List

from app.collectors import simulator as simulator_module
from app.collectors.simulator import SIM_DEVICES, SIM_LINKS, SimulatorDriver
from app.models import Device, Link, MetricPoint, Topology, TopologyNode

# 2026-10-28（周三）15:00 / 03:00 的本地时间戳，作为固定时间片
PEAK_TS = datetime(2026, 10, 28, 15, 0, 0).timestamp()
NIGHT_TS = datetime(2026, 10, 28, 3, 0, 0).timestamp()


def run_async(coro):
    return asyncio.run(coro)


def metrics_by_port(points: List[MetricPoint]) -> Dict[Any, float]:
    """(device_id, interface) -> if_utilization_pct。"""
    return {
        (point.device_id, point.interface): point.value
        for point in points
        if point.metric == "if_utilization_pct"
    }


class TestSimulatorCollect:
    def test_collect_returns_simulated_dataset(self):
        sim = SimulatorDriver()
        result = run_async(sim.collect())
        assert result.ok is True
        assert result.source == "simulator"
        assert len(result.devices) == len(SIM_DEVICES)
        assert all(device.source == "simulator" for device in result.devices)
        assert all(device.hostname.startswith("SIM-") for device in result.devices)
        assert len(result.topology.nodes) == len(SIM_DEVICES)
        assert len(result.topology.links) == len(SIM_LINKS)
        assert result.topology.source == "simulator"
        assert all(link.data_source == "simulator" for link in result.topology.links)
        assert all(link.link_status == "up" for link in result.topology.links)
        assert len(result.hosts) == 4
        assert all(host.source == "simulator" for host in result.hosts)
        assert result.issues == []
        assert result.network_health is not None
        health = result.network_health
        assert health.total_devices == len(SIM_DEVICES)
        assert health.source == "simulator"
        # W3.1：字段自洽（健康 + 不健康 == 总数，百分比为数值）
        assert health.device_total == len(SIM_DEVICES)
        assert health.device_healthy + health.device_unhealthy == health.device_total
        assert health.health_percentage == 100.0

    def test_every_metric_is_labeled_simulator(self):
        result = run_async(SimulatorDriver().collect())
        assert result.metrics
        for point in result.metrics:
            assert point.source == "simulator", "指标伪造来源：{}".format(point)

    def test_metrics_do_not_claim_netcon_source(self):
        result = run_async(SimulatorDriver().collect())
        assert not any(point.source == "netcon" for point in result.metrics)

    def test_default_dataset_has_no_fake_issues(self):
        """未注入故障时不应凭空产生健康问题。"""
        result = run_async(SimulatorDriver().collect())
        assert result.issues == []


class TestBehaviorModel:
    def test_metrics_deterministic_for_same_timestamp(self):
        sim = SimulatorDriver()
        result = run_async(sim.collect())
        first = sim.generate_metrics(result.devices, result.topology, at=PEAK_TS)
        second = sim.generate_metrics(result.devices, result.topology, at=PEAK_TS)
        assert [point.model_dump() for point in first] == [
            point.model_dump() for point in second
        ]

    def test_metrics_change_over_time(self):
        sim = SimulatorDriver()
        result = run_async(sim.collect())
        peak = sim.generate_metrics(result.devices, result.topology, at=PEAK_TS)
        night = sim.generate_metrics(result.devices, result.topology, at=NIGHT_TS)
        peak_utils = [point.value for point in peak if point.metric == "device_utilization_pct"]
        night_utils = [point.value for point in night if point.metric == "device_utilization_pct"]
        assert sum(peak_utils) / len(peak_utils) > sum(night_utils) / len(night_utils) * 1.5

    def test_values_are_finite_and_bounded(self):
        sim = SimulatorDriver()
        result = run_async(sim.collect())
        for point in sim.generate_metrics(result.devices, result.topology, at=PEAK_TS):
            assert math.isfinite(point.value)
            assert point.value >= 0.0
            if point.unit == "%":
                assert point.value <= 100.0

    def test_no_bare_random_in_behavior_model(self):
        """验收要求：行为模型不能用裸随机数（如 random.uniform 采样）。"""
        source = inspect.getsource(simulator_module)
        assert "import random" not in source
        assert "from random import" not in source
        assert "random.uniform(" not in source
        assert "random.random(" not in source
        assert "numpy.random" not in source

    def test_daily_load_curve_peak_vs_night(self):
        sim = SimulatorDriver()
        peak = sim._daily_load(datetime(2026, 10, 28, 15, 0, 0))
        night = sim._daily_load(datetime(2026, 10, 28, 3, 0, 0))
        weekend = sim._daily_load(datetime(2026, 10, 31, 15, 0, 0))  # 周六
        assert 0.03 <= night < peak <= 0.95
        assert weekend < peak

    def test_empty_devices_yield_no_metrics(self):
        sim = SimulatorDriver()
        assert sim.generate_metrics([], Topology()) == []


class TestFailureRedistribution:
    def test_link_failure_redistributes_traffic_to_remaining_links(self):
        sim = SimulatorDriver()
        dataset = run_async(sim.collect())
        before = metrics_by_port(
            sim.generate_metrics(dataset.devices, dataset.topology, at=PEAK_TS)
        )
        target = next(link for link in SIM_LINKS if link["id"] == "sim-l-core")
        assert sim.inject_link_failure("sim-l-core") is True
        after_points = sim.generate_metrics(dataset.devices, dataset.topology, at=PEAK_TS)
        after = metrics_by_port(after_points)

        failed_ports = {
            (target["source"], target["start_port_name"]),
            (target["target"], target["end_port_name"]),
        }
        # 故障链路端口指标归零
        for port in failed_ports:
            assert after[port] == 0.0
        # 其它活跃链路利用率上升（流量重分布）
        increased = [
            port for port, util in before.items()
            if port not in failed_ports and after[port] > util + 1e-6
        ]
        assert increased, "故障后没有链路承接重分布流量"

    def test_failed_link_metrics_are_flagged(self):
        sim = SimulatorDriver()
        dataset = run_async(sim.collect())
        sim.inject_link_failure("sim-l-d1-acc1")
        points = sim.generate_metrics(dataset.devices, dataset.topology, at=PEAK_TS)
        flagged = [
            point for point in points
            if point.tags.get("link_id") == "sim-l-d1-acc1"
        ]
        assert flagged, "故障链路应仍有指标点（值为 0）以便前端展示断流"
        assert all(point.tags.get("failed") is True for point in flagged)
        assert all(point.value == 0.0 for point in flagged)

    def test_device_failure_marks_unreachable_and_creates_issue(self):
        sim = SimulatorDriver()
        assert sim.inject_device_failure("sim-acc-b1") is True
        result = run_async(sim.collect())
        device = next(device for device in result.devices if device.id == "sim-acc-b1")
        assert device.reachability == "UNREACHABLE"
        # 该设备相关链路全部 down
        incident = [
            link for link in result.topology.links
            if link.source == "sim-acc-b1" or link.target == "sim-acc-b1"
        ]
        assert incident and all(link.link_status == "down" for link in incident)
        # 产生一条来源标注正确的健康问题
        assert len(result.issues) == 1
        issue = result.issues[0]
        assert issue.source == "simulator"
        assert issue.severity == "critical"
        assert "sim-acc-b1" in issue.issue_id
        # 故障设备设备级利用率归零
        metrics = sim.generate_metrics(result.devices, result.topology, at=PEAK_TS)
        device_util = next(
            point.value for point in metrics
            if point.device_id == "sim-acc-b1" and point.metric == "device_utilization_pct"
        )
        assert device_util == 0.0

    def test_unknown_failure_ids_rejected(self):
        sim = SimulatorDriver()
        assert sim.inject_link_failure("no-such-link") is False
        assert sim.inject_device_failure("no-such-device") is False
        sim.inject_link_failure("sim-l-core")
        assert sim.failed_links == {"sim-l-core"}
        assert sim.clear_link_failure("sim-l-core") is True
        assert sim.failed_links == set()
        sim.inject_device_failure("sim-core-r1")
        sim.clear_failures()
        assert sim.failed_devices == set()


class TestExternalTopology:
    def test_generate_metrics_for_external_netcon_topology(self):
        """仿真指标生成器可跟随 netcon 真实拓扑，且不篡改真实数据模型。"""
        sim = SimulatorDriver()
        devices = [
            Device(id="real-r1", hostname="CORE-R1", management_ip="10.0.0.1", source="netcon"),
            Device(id="real-sw1", hostname="ACC-SW1", management_ip="10.0.0.2", source="netcon"),
        ]
        topology = Topology(
            nodes=[
                TopologyNode(id="real-r1", x=0, y=0, source="netcon"),
                TopologyNode(id="real-sw1", x=1, y=1, source="netcon"),
            ],
            links=[
                Link(
                    id="real-link-1",
                    source="real-r1",
                    target="real-sw1",
                    start_port_name="Gi0/1",
                    end_port_name="Gi0/2",
                    start_port_speed="1Gbps",
                    end_port_speed="1Gbps",
                    link_status="up",
                    data_source="netcon",
                )
            ],
            source="netcon",
        )
        points = sim.generate_metrics(devices, topology, at=PEAK_TS)
        assert points
        assert {point.device_id for point in points} == {"real-r1", "real-sw1"}
        assert all(point.source == "simulator" for point in points)
        # 传入的真实模型不被改写：拓扑仍标 netcon，设备仍标 netcon
        assert topology.links[0].data_source == "netcon"
        assert devices[0].source == "netcon"

    def test_topology_with_unknown_endpoints_is_tolerated(self):
        sim = SimulatorDriver()
        devices = [Device(id="d1", hostname="R1", source="netcon")]
        topology = Topology(
            links=[
                Link(id="l-unknown", source="d1", target="not-in-device-list"),
                Link(id="l-both-unknown", source="x", target="y"),
            ],
            source="netcon",
        )
        points = sim.generate_metrics(devices, topology, at=PEAK_TS)
        # 不崩溃；只有设备级指标（链路两端设备不全，跳过链路指标）
        assert points
        assert all(point.device_id == "d1" for point in points)
        assert all(point.metric in ("device_utilization_pct", "cpu_pct", "mem_pct") for point in points)


class TestPhysicalConstraints:
    """W3.1：simulator 指标必须物理自洽（bps 不得超过端口速率）。"""

    @staticmethod
    def _external_topology(speed_kbps: int = 100000):
        devices = [
            Device(id="real-r1", hostname="CORE-R1", reachability="Reachable",
                   collection_status="Managed", source="netcon"),
            Device(id="real-sw1", hostname="ACC-SW1", reachability="Reachable",
                   collection_status="Managed", source="netcon"),
        ]
        topology = Topology(
            nodes=[TopologyNode(id="real-r1", source="netcon"),
                   TopologyNode(id="real-sw1", source="netcon")],
            links=[
                Link(
                    id="real-link-1",
                    source="real-r1",
                    target="real-sw1",
                    start_port_name="Gi0/1",
                    end_port_name="Gi0/2",
                    start_port_speed=speed_kbps,  # NC 数值单位 Kbps
                    end_port_speed=speed_kbps,
                    link_status="up",
                    data_source="netcon",
                )
            ],
            source="netcon",
        )
        return devices, topology

    def test_bps_never_exceeds_port_speed_for_100m_and_1g(self):
        sim = SimulatorDriver()
        for speed_kbps, expected_bps in ((100000, 1e8), (1000000, 1e9)):
            devices, topology = self._external_topology(speed_kbps)
            points = sim.generate_metrics(devices, topology, at=PEAK_TS)
            bps_points = [
                point for point in points if point.metric in ("if_in_bps", "if_out_bps")
            ]
            assert bps_points, "应生成端口流量指标"
            for point in bps_points:
                assert point.speed_bps == expected_bps  # 100Mbps -> 1e8 bps
                assert point.value <= expected_bps + 1e-6  # 物理上限
                assert isinstance(point.clamped, bool)
            # 同一端口 in + out 不超过端口速率（物理上瞬时分向之和 <= 线速）
            per_port = {}
            for point in bps_points:
                key = (point.device_id, point.interface)
                per_port[key] = per_port.get(key, 0.0) + point.value
            for total in per_port.values():
                assert total <= expected_bps + 1e-6

    def test_utilization_never_exceeds_100(self):
        sim = SimulatorDriver()
        devices, topology = self._external_topology()
        for point in sim.generate_metrics(devices, topology, at=PEAK_TS):
            if point.metric == "if_utilization_pct":
                assert point.value <= 100.0
                assert point.speed_bps is not None

    def test_clamp_helper_handles_reported_bad_values(self):
        sim = SimulatorDriver()
        # 实测 bug：if_in_bps≈15.31e9 但端口 100Mbps（1e8）→ 必须钳制
        value, clamped = sim._clamp_bps(15.31e9, 1e8)
        assert value == 1e8
        assert clamped is True
        value, clamped = sim._clamp_bps(5e7, 1e8)
        assert value == 5e7
        assert clamped is False
        assert sim._clamp_bps(123.0, None) == (123.0, False)

    def test_collect_metrics_carry_physics_fields(self):
        result = run_async(SimulatorDriver().collect())
        bps_points = [
            point for point in result.metrics if point.metric in ("if_in_bps", "if_out_bps")
        ]
        assert bps_points
        for point in bps_points:
            assert point.speed_bps is not None and point.speed_bps > 0
            assert point.value <= point.speed_bps + 1e-6
            assert point.tags.get("speed_bps") == point.speed_bps
        device_level = [
            point
            for point in result.metrics
            if point.metric in ("cpu_pct", "mem_pct", "device_utilization_pct")
        ]
        assert device_level
        assert all(point.speed_bps is None and point.clamped is False for point in device_level)
