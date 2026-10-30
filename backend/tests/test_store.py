# -*- coding: utf-8 -*-
"""SQLite 存储层单测（WAL、快照/告警/指标读写、健康问题活跃状态）。"""
from __future__ import annotations

import asyncio

from app.collectors.simulator import SimulatorDriver
from app.models import Alert, MetricPoint
from app.store import Store


def run_async(coro):
    return asyncio.run(coro)


def test_wal_mode_enabled(tmp_path):
    store = Store(str(tmp_path / "campus.db"))
    store.init_db()
    assert store.journal_mode() == "wal"
    stats = store.stats()
    assert stats["journal_mode"] == "wal"
    assert stats["device_rows"] == 0


def test_snapshot_roundtrip(tmp_path):
    store = Store(str(tmp_path / "campus.db"))
    store.init_db()
    result = run_async(SimulatorDriver().collect())
    store.save_snapshot(result)

    devices = store.load_latest_devices()
    assert len(devices) == len(result.devices)
    assert all(item["source"] == "simulator" for item in devices)
    stats = store.stats()
    assert stats["device_rows"] == len(result.devices)
    assert stats["active_issue_rows"] == 0


def test_topology_snapshot_only_appended_on_change(tmp_path):
    store = Store(str(tmp_path / "campus.db"))
    store.init_db()
    result = run_async(SimulatorDriver().collect())
    store.save_snapshot(result)
    store.save_snapshot(result)  # 拓扑不变：不新增行
    with store._db() as conn:  # noqa: SLF001 - 测试内部表结构
        count = conn.execute("SELECT COUNT(*) FROM topology_snapshots").fetchone()[0]
    assert count == 1


def test_health_issue_goes_inactive_when_gone(tmp_path):
    store = Store(str(tmp_path / "campus.db"))
    store.init_db()
    simulator = SimulatorDriver()
    simulator.inject_link_failure("sim-l-core")
    with_issue = run_async(simulator.collect())
    store.save_snapshot(with_issue)
    assert len(store.load_issues(active_only=True)) == 1

    simulator.clear_failures()
    without_issue = run_async(simulator.collect())
    store.save_snapshot(without_issue)
    assert store.load_issues(active_only=True) == []
    assert len(store.load_issues(active_only=False)) == 1  # 历史保留


def test_alerts_roundtrip_and_severity_filter(tmp_path):
    store = Store(str(tmp_path / "campus.db"))
    store.init_db()
    alerts = [
        Alert(alert_type="device_offline", severity="critical", title="设备离线：A", dedup_key="device_offline:a"),
        Alert(alert_type="link_up", severity="info", title="链路恢复", dedup_key="link_up:l1"),
    ]
    assert store.save_alerts(alerts) == 2
    assert store.save_alerts([]) == 0
    assert len(store.load_alerts()) == 2
    critical = store.load_alerts(severity="critical")
    assert len(critical) == 1
    assert critical[0]["title"] == "设备离线：A"
    assert critical[0]["acknowledged"] == 0


def test_metrics_roundtrip_and_prune(tmp_path):
    store = Store(str(tmp_path / "campus.db"))
    store.init_db()
    result = run_async(SimulatorDriver().collect())
    assert store.save_metrics(result.metrics) == len(result.metrics)
    device_id = result.devices[0].id
    points = store.load_metrics(device_id, limit=500)
    assert points
    assert all(point["source"] == "simulator" for point in points)
    assert all(point["tags"] for point in points)
    # W3.1：端口流量指标持久化物理约束字段
    bps_points = [point for point in points if point["metric"] in ("if_in_bps", "if_out_bps")]
    assert bps_points
    for point in bps_points:
        assert point["speed_bps"] is not None and point["speed_bps"] > 0
        assert point["value"] <= point["speed_bps"] + 1e-6
        assert isinstance(point["clamped"], bool)
    # 升序返回，便于前端直接画图
    assert points == sorted(points, key=lambda item: item["ts"])
    filtered = store.load_metrics(device_id, limit=500, metric="cpu_pct")
    assert filtered and all(point["metric"] == "cpu_pct" for point in filtered)

    old = MetricPoint(device_id=device_id, metric="cpu_pct", value=1.0, ts=1.0)
    store.save_metrics([old])
    removed = store.prune_metrics(retention_seconds=60)
    assert removed >= 1
    assert not any(point["ts"] == 1.0 for point in store.load_metrics(device_id, limit=5000))
