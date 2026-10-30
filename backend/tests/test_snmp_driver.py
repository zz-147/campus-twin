# -*- coding: utf-8 -*-
"""W10 SNMP 驱动测试（mock 客户端，无真实网络；pysnmp 仅做可用性断言）。"""
from __future__ import annotations

import asyncio
import copy
import time
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from app.collectors.snmp import (
    SnmpDeviceSample,
    SnmpDriver,
    SnmpInterfaceSample,
    _platform_from_sys_descr,
)
from app.config import Settings
from app.main import create_app
from app.pipeline import build_drivers as build_drivers_from_pipeline

DEVICE_CONFIGS = [
    {
        "name": "SW-TEST",
        "ip": "127.0.0.1",
        "community": "public",
        "version": "2c",
        "building": "core",
    }
]


def run_async(coro):
    return asyncio.run(coro)


def make_sample(
    name: str = "SW-TEST",
    speed_bps: float = 1e8,
    in_octets: int = 1_000_000,
    out_octets: int = 500_000,
    oper_status: int = 1,
) -> SnmpDeviceSample:
    return SnmpDeviceSample(
        sys_descr=(
            "Cisco IOS Software, C2960 Software (C2960-LANBASEK9-M), "
            "Version 15.0(2)SE4, RELEASE SOFTWARE (fc1)"
        ),
        sys_name=name,
        sys_up_time=3600.0,
        interfaces=[
            SnmpInterfaceSample(
                index=1,
                name="GigabitEthernet0/1",
                oper_status=oper_status,
                speed_bps=speed_bps,
                in_octets=in_octets,
                out_octets=out_octets,
            ),
            SnmpInterfaceSample(
                index=2,
                name="GigabitEthernet0/2",
                oper_status=2,
                speed_bps=speed_bps,
                in_octets=2_000_000,
                out_octets=1_000_000,
            ),
        ],
    )


class MockSnmpClient:
    """按 ip 返回固定采样或抛异常，用于验证驱动容错/归一化。"""

    def __init__(self, samples: Dict[str, Any]) -> None:
        self.samples = dict(samples)
        self.calls: List[str] = []
        self.closed = False

    async def collect_device(
        self,
        ip: str,
        community: str = "public",
        version: str = "2c",
        timeout: float = 2.0,
        retries: int = 1,
    ) -> SnmpDeviceSample:
        self.calls.append(ip)
        value = self.samples.get(ip)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError("no mock sample for {}".format(ip))
        return copy.deepcopy(value)

    async def close(self) -> None:
        self.closed = True


def make_settings(devices=None, **overrides) -> Settings:
    defaults = dict(
        driver="snmp",
        snmp_devices=devices if devices is not None else list(DEVICE_CONFIGS),
        snmp_timeout=0.05,
        snmp_retries=0,
        poll_interval=3600.0,
        log_level="WARNING",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class TestSnmpDriverCollect:
    def test_first_poll_normalizes_device_and_metrics(self):
        client = MockSnmpClient({"127.0.0.1": make_sample()})
        driver = SnmpDriver(make_settings(), client=client)
        result = run_async(driver.collect())
        assert result.ok is True
        assert result.source == "snmp"
        assert driver.reachable is True
        assert len(result.devices) == 1
        device = result.devices[0]
        assert device.hostname == "SW-TEST"
        assert device.management_ip == "127.0.0.1"
        assert device.reachability == "Reachable"
        assert device.collection_status == "Managed"
        assert device.source == "snmp"
        assert device.interface_count == 2
        assert device.platform_id and "C2960" in device.platform_id
        assert result.network_health is not None
        assert result.network_health.total_devices == 1
        # 指标全部 source=snmp，首轮无历史计数器 → bps=0，但带 speed_bps/clamped
        bps_metrics = [p for p in result.metrics if p.metric in ("if_in_bps", "if_out_bps")]
        assert bps_metrics
        for point in bps_metrics:
            assert point.source == "snmp"
            assert point.value == 0.0
            assert point.speed_bps == 1e8
            assert point.clamped is False
            assert point.tags.get("oper_status") in ("up", "down")
        util_metrics = [p for p in result.metrics if p.metric == "if_utilization_pct"]
        assert len(util_metrics) == 2
        assert all(point.value == 0.0 for point in util_metrics)
        assert any(p.metric == "device_utilization_pct" for p in result.metrics)

    def test_second_poll_computes_bps_within_speed(self):
        client = MockSnmpClient({"127.0.0.1": make_sample()})
        driver = SnmpDriver(make_settings(), client=client)
        run_async(driver.collect())
        # 人为缩短时间基准并模拟 1s 内 1,000,000 字节增量（≈8Mbps < 100Mbps）
        old = driver._last_counters[("127.0.0.1", 1)]  # noqa: SLF001 - 单测内部状态
        driver._last_counters[("127.0.0.1", 1)] = (  # noqa: SLF001
            old[0] - 1.0,
            old[1],
            old[2],
        )
        client.samples["127.0.0.1"] = make_sample(
            in_octets=1_000_000 + 1_000_000, out_octets=500_000 + 500_000
        )
        result = run_async(driver.collect())
        points = {
            point.metric: point
            for point in result.metrics
            if point.device_id == result.devices[0].id and point.interface == "GigabitEthernet0/1"
        }
        assert 7_500_000 <= points["if_in_bps"].value <= 8_500_000
        assert 3_500_000 <= points["if_out_bps"].value <= 4_500_000
        assert points["if_in_bps"].value <= 1e8
        assert points["if_in_bps"].clamped is False
        assert points["if_utilization_pct"].value <= 100.0

    def test_bps_clamped_to_port_speed(self):
        client = MockSnmpClient({"127.0.0.1": make_sample()})
        driver = SnmpDriver(make_settings(), client=client)
        run_async(driver.collect())
        now = time.time()
        driver._last_counters[("127.0.0.1", 1)] = (  # noqa: SLF001
            now - 1.0,
            0,
            0,
        )
        # 1s 内增长 100MB → 800Mbps，超过 100Mbps 端口 → 必须钳制
        client.samples["127.0.0.1"] = make_sample(
            in_octets=100_000_000, out_octets=100_000_000
        )
        result = run_async(driver.collect())
        device_id = result.devices[0].id
        in_point = next(
            point
            for point in result.metrics
            if point.device_id == device_id
            and point.metric == "if_in_bps"
            and point.interface == "GigabitEthernet0/1"
        )
        assert in_point.value == 1e8
        assert in_point.speed_bps == 1e8
        assert in_point.clamped is True

    def test_single_device_failure_does_not_break_others(self):
        configs = [
            {"name": "BAD", "ip": "10.0.0.2", "community": "public"},
            {"name": "GOOD", "ip": "127.0.0.1", "community": "public"},
        ]
        client = MockSnmpClient(
            {"10.0.0.2": TimeoutError("SNMP timeout"), "127.0.0.1": make_sample()}
        )
        driver = SnmpDriver(make_settings(devices=configs), client=client)
        result = run_async(driver.collect())
        assert result.ok is True
        assert len(result.devices) == 1
        assert result.devices[0].management_ip == "127.0.0.1"
        assert "10.0.0.2" in result.errors
        assert driver.reachable is True

    def test_all_devices_fail_degrades(self):
        configs = [
            {"name": "BAD1", "ip": "10.0.0.2"},
            {"name": "BAD2", "ip": "10.0.0.3"},
        ]
        client = MockSnmpClient(
            {
                "10.0.0.2": TimeoutError("SNMP timeout"),
                "10.0.0.3": RuntimeError("community error"),
            }
        )
        driver = SnmpDriver(make_settings(devices=configs), client=client)
        result = run_async(driver.collect())
        assert result.ok is False
        assert result.devices == []
        assert result.source == "snmp"
        assert "全部 SNMP 设备采集失败" in result.error
        assert driver.reachable is False
        assert run_async(driver.probe()) is False

    def test_no_devices_configured_is_graceful(self):
        driver = SnmpDriver(make_settings(devices=[]), client=MockSnmpClient({}))
        assert run_async(driver.probe()) is False
        result = run_async(driver.collect())
        assert result.ok is True
        assert result.devices == []
        assert result.source == "snmp"
        assert result.network_health is not None
        assert result.network_health.total_devices == 0
        assert "SNMP_DEVICES 未配置" in (result.error or "")

    def test_missing_speed_has_no_speed_field(self):
        sample = make_sample(speed_bps=None)
        for interface in sample.interfaces:
            interface.speed_bps = None
        client = MockSnmpClient({"127.0.0.1": sample})
        driver = SnmpDriver(make_settings(), client=client)
        result = run_async(driver.collect())
        points = [
            point
            for point in result.metrics
            if point.metric in ("if_in_bps", "if_out_bps", "if_utilization_pct")
        ]
        assert points
        for point in points:
            if point.metric != "if_utilization_pct":
                assert point.speed_bps is None
                assert point.clamped is False


class TestSnmpDriverFactoryAndApi:
    def test_build_drivers_selects_snmp(self):
        driver, metrics_driver = run_async(
            build_drivers_from_pipeline(make_settings())
        )
        try:
            assert isinstance(driver, SnmpDriver)
            assert driver.name == "snmp"
            assert metrics_driver is None  # SNMP 指标是真实采集，不需要 simulator
        finally:
            run_async(driver.close())

    def test_app_with_snmp_driver_no_devices_degrades(self, tmp_path):
        settings = make_settings(devices=[])
        settings.db_path = str(tmp_path / "snmp.db")
        app = create_app(settings)
        with TestClient(app) as client:
            health = client.get("/api/health").json()
            assert health["status"] == "ok"
            assert health["driver"] == "snmp"
            assert health["counts"]["devices"] == 0
            devices = client.get("/api/devices").json()
            assert devices["count"] == 0
            assert client.get("/api/alerts").json()["alerts"] == []

    def test_pysnmp_installed(self):
        import pysnmp

        assert pysnmp.__version__

    def test_platform_parse(self):
        assert "C2960" in (_platform_from_sys_descr(
            "Cisco IOS Software, C2960 Software (C2960-LANBASEK9-M)"
        ) or "")
        assert "ISR4331" in (_platform_from_sys_descr(
            "Cisco IOS XE Software, Version 16.06.04, Cisco ISR4331/K9"
        ) or "")
        assert _platform_from_sys_descr("") is None
