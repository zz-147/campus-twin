# -*- coding: utf-8 -*-
"""W3.1 network-health 数据口径测试（完全离线）。

核心问题：PT NC 的 ``/network-health`` 只返回百分比（healthyNetworkDevice），
没有 healthyDevices/unhealthyDevices 计数，旧实现透传后出现
``healthy_devices=0 / unhealthy_devices=0 / health_percentage=null`` 却同时
``healthy_devices_percentage=100`` 的自相矛盾。

修复口径：从设备列表自算 device_total/device_healthy/health_percentage
（Managed 且非 Unreachable = 健康），NC 原始值保留在 nc_* 字段，标注 source_of_truth。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

from app.collectors.base import CollectResult
from app.config import Settings
from app.models import (
    Device,
    Link,
    NetworkHealth,
    Topology,
    TopologyNode,
    extract_list,
)
from app.pipeline import CollectorPipeline, PipelineState
from app.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> Any:
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def make_device(
    device_id: str,
    reachability: str = "Reachable",
    collection_status: str = "Managed",
) -> Device:
    return Device(
        id=device_id,
        hostname=device_id.upper(),
        management_ip="10.0.0.{}".format(len(device_id)),
        reachability=reachability,
        collection_status=collection_status,
        source="netcon",
    )


class TestComputeFromDevices:
    def test_managed_and_reachable_is_healthy(self):
        devices = [
            make_device("a", "Reachable", "Managed"),        # 健康
            make_device("b", "Unreachable", "Managed"),      # 不健康（不可达）
            make_device("c", "Reachable", "Unsupported"),    # 不健康（未 Managed）
            make_device("d", "Reachable", None),             # 不健康（无采集状态）
        ]
        health = NetworkHealth.compute_from_devices(devices, source="netcon")
        assert health.source_of_truth == "computed_from_devices"
        assert health.device_total == 4
        assert health.device_healthy == 1
        assert health.device_unhealthy == 3
        assert health.device_unreachable == 1
        assert health.health_percentage == 25.0
        # 不变式：healthy + unhealthy == total，且旧字段与自算字段同值
        assert health.device_healthy + health.device_unhealthy == health.device_total
        assert health.total_devices == health.device_total
        assert health.healthy_devices == health.device_healthy
        assert health.unhealthy_devices == health.device_unhealthy
        assert health.healthy_devices_percentage == health.health_percentage
        # 无 NC 输入时不虚造 NC 原始字段
        assert health.nc_total_devices is None
        assert health.nc_summary == {}

    def test_empty_devices_percentage_is_numeric_zero(self):
        health = NetworkHealth.compute_from_devices([], source="netcon")
        assert health.device_total == 0
        assert health.device_healthy == 0
        assert health.device_unhealthy == 0
        assert health.health_percentage == 0.0  # 不是 None
        assert isinstance(health.health_percentage, float)

    def test_all_healthy_100_percent(self):
        devices = [make_device("a"), make_device("b")]
        health = NetworkHealth.compute_from_devices(devices, source="simulator")
        assert health.health_percentage == 100.0
        assert health.device_unhealthy == 0


class TestNCAnomalyNormalization:
    def test_flat_nc_health_has_no_counts_but_computed_is_self_consistent(self):
        """复现问题：NC 扁平响应只有百分比 → 旧字段矛盾；自算后字段自洽。"""
        nc = NetworkHealth.from_netcon(
            load_fixture("netcon_network_health_real_sample.json")
        )
        # NC 原始解析（source_of_truth=nc_raw）：总数 7、0 台健康计数、健康百分比 100
        assert nc.source_of_truth == "nc_raw"
        assert nc.nc_total_devices == 7
        assert nc.nc_healthy_devices_percentage == 100.0
        assert nc.nc_unreachable_devices == 3
        assert nc.nc_healthy_clients_percentage == 20.0
        assert nc.nc_summary["healthyNetworkDevice"] == "100"

        # 用真实设备列表（3 台：1 Managed+Reachable、1 Unreachable、1 Unsupported）
        docs = extract_list(load_fixture("netcon_network_device_real_sample.json"))
        devices: List[Device] = [Device.from_netcon(raw) for raw in docs]
        health = NetworkHealth.compute_from_devices(
            devices, source="netcon", nc_health=nc
        )
        assert health.source_of_truth == "computed_from_devices"
        assert health.device_total == 3
        assert health.device_healthy == 1
        assert health.device_unhealthy == 2
        assert health.health_percentage == 33.33
        assert health.healthy_devices + health.unhealthy_devices == health.total_devices
        assert health.health_percentage is not None
        # NC 原始字段并存，便于审计对比（不是被丢弃）
        assert health.nc_total_devices == 7
        assert health.nc_healthy_devices_percentage == 100.0
        assert health.nc_healthy_clients_percentage == 20.0
        assert health.nc_summary

    def test_nested_assurance_health_keeps_nc_values_but_recomputes_counts(self):
        doc = {
            "response": {
                "networkDevices": {
                    "totalDevices": 6,
                    "totalPercentage": 83.33,
                    "healthyDevices": 5,
                    "unhealthyDevices": 1,
                }
            },
            "version": "1.0",
        }
        nc = NetworkHealth.from_netcon(doc)
        health = NetworkHealth.compute_from_devices(
            [make_device("a"), make_device("b", "Unreachable")],
            source="netcon",
            nc_health=nc,
        )
        assert health.device_total == 2
        assert health.device_healthy == 1
        assert health.health_percentage == 50.0
        # NC 原始字段仍然保留
        assert health.nc_summary["networkDevices"]["totalDevices"] == 6
        assert health.nc_total_devices is None  # 嵌套响应没有 numNetworkDevices


class TestPipelineSnapshotNetworkHealth:
    def test_snapshot_recomputes_from_devices_even_if_driver_health_inconsistent(self, tmp_path):
        settings = Settings(
            db_path=str(tmp_path / "campus.db"),
            driver="netcon",
            poll_interval=3600.0,
            simulate_metrics=False,
        )
        store = Store(settings.db_path)
        store.init_db()

        class DummyDriver:
            name = "dummy"

            async def probe(self):
                return True

            async def collect(self):
                raise AssertionError("not used")

            async def close(self):
                return None

        pipeline = CollectorPipeline(
            settings=settings, store=store, driver=DummyDriver()
        )
        nc = NetworkHealth.from_netcon(
            load_fixture("netcon_network_health_real_sample.json")
        )
        devices = [make_device("a"), make_device("b", "Unreachable")]
        result = CollectResult(
            devices=devices,
            topology=Topology(
                nodes=[TopologyNode(id=device.id, source="netcon") for device in devices],
                links=[Link(id="l1", source="a", target="b", data_source="netcon")],
                source="netcon",
            ),
            network_health=nc,
            source="netcon",
            ok=True,
        )
        pipeline._state = PipelineState.from_result(result)  # noqa: SLF001 - 单测快照视图

        snapshot = pipeline.snapshot_data()
        health = snapshot["network_health"]
        assert health is not None
        assert health["source_of_truth"] == "computed_from_devices"
        assert health["device_total"] == 2
        assert health["device_healthy"] + health["device_unhealthy"] == 2
        assert health["health_percentage"] == 50.0
        assert health["nc_total_devices"] == 7  # NC 原始字段未丢失
        # JSON 可序列化（WebSocket 推送要求）
        json.dumps(snapshot, ensure_ascii=False)
