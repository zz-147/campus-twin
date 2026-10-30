# -*- coding: utf-8 -*-
"""W3-C 告警降噪单测：incident 生命周期去重、恢复聚合、历史告警 TTL。

对应故障注入实验噪声问题：换核心期间 11 条历史 issue 恢复时集中产生 11 条 info
“已清除”告警。W3 方案：
1. 同一 dedup_key 同时最多一条 active 告警（故障持续不重复推送）；
2. 恢复只把 active 行置为 cleared，不新增 info 行；
3. 同一轮多条恢复聚合成一条 alerts_cleared 摘要（WebSocket）；
4. 历史告警默认 7 天 TTL，只清理 cleared/info，active 故障不删。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from app.config import Settings
from app.models import Alert, Link, MetricPoint, Topology
from app.pipeline import CollectorPipeline
from app.store import Store
from tests.helpers import ScriptedDriver, make_host, make_issue, make_result, run_async


def make_alert(
    dedup_key: str,
    alert_type: str = "host_down",
    severity: str = "critical",
    state: str = "active",
    observed_at: float = 1793340000.0,
    stale_source: bool = False,
) -> Alert:
    return Alert(
        alert_type=alert_type,
        severity=severity,
        title="测试告警 {}".format(dedup_key),
        dedup_key=dedup_key,
        observed_at=observed_at,
        created_at=observed_at,
        state=state,
        stale_source=stale_source,
    )


class TestAlertLifecycleStore:
    def test_active_dedup_and_resolve_flow(self, tmp_path):
        store = Store(str(tmp_path / "campus.db"))
        store.init_db()
        first = make_alert("host_down:h1")
        assert store.save_alert_events([first]) == 1
        assert len(store.load_active_alerts()) == 1

        # 恢复：只改状态，不新增行
        updated = store.resolve_alert(
            "host_down:h1", observed_at=1793340100.0, reason="pingStatus SUCCESS"
        )
        assert updated is not None
        assert updated["state"] == "cleared"
        assert updated["resolved_at"] == 1793340100.0
        assert updated["clear_reason"] == "pingStatus SUCCESS"
        assert store.load_active_alerts() == []
        assert store.load_alerts(state="cleared")[0]["state"] == "cleared"
        assert store.load_alerts(state="active") == []
        # 重复 resolve 不会二次修改/报错
        assert store.resolve_alert("host_down:h1", 1793340200.0) is None
        assert store.alert_stats()["cleared"] == 1
        assert store.alert_stats()["total"] == 1  # 没有产生第 2 条 info 行

    def test_stale_source_flag_persisted(self, tmp_path):
        store = Store(str(tmp_path / "campus.db"))
        store.init_db()
        store.save_alert_events([make_alert("health_issue:i-1")])
        store.resolve_alert(
            "health_issue:i-1",
            observed_at=1793340100.0,
            reason="NC 问题记录滞后，数据源已恢复",
            stale_source=True,
        )
        cleared = store.load_alerts(state="cleared")[0]
        assert cleared["stale_source"] is True
        assert "NC 问题记录滞后" in cleared["clear_reason"]

    def test_prune_alerts_ttl_keeps_active(self, tmp_path):
        store = Store(str(tmp_path / "campus.db"))
        store.init_db()
        now = time.time()
        old = now - 8 * 86400
        store.save_alert_events(
            [
                make_alert("host_down:old-active", state="active", observed_at=old),
                make_alert("host_down:old-cleared", state="cleared", observed_at=old),
                make_alert("device_online:old", alert_type="device_online", severity="info", state="info", observed_at=old),
                make_alert("host_down:recent-cleared", state="cleared", observed_at=now),
            ]
        )
        removed = store.prune_alerts(retention_days=7)
        assert removed == 2  # 旧 cleared + 旧 info
        remaining = {item["dedup_key"] for item in store.load_alerts(state="all")}
        assert "host_down:old-active" in remaining  # active 故障不因 TTL 删除
        assert "host_down:recent-cleared" in remaining
        assert "host_down:old-cleared" not in remaining
        assert "device_online:old" not in remaining

    def test_schema_migration_v2_to_v3_adds_metric_columns(self, tmp_path):
        """W3.1：旧库 metric_points 自动迁移，历史错误 bps 按拓扑端口速率回填并钳制。"""
        import sqlite3

        topology = Topology(
            links=[
                Link(
                    id="l1",
                    source="d1",
                    target="d2",
                    start_port_name="Gi0/1",
                    end_port_name="Gi0/2",
                    start_port_speed=100000,  # NC Kbps -> 100Mbps
                    end_port_speed=100000,
                    data_source="netcon",
                )
            ],
            source="netcon",
        )
        db_path = str(tmp_path / "v2_metric.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE topology_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at REAL NOT NULL, source TEXT NOT NULL,
                node_count INTEGER NOT NULL DEFAULT 0,
                link_count INTEGER NOT NULL DEFAULT 0,
                payload_hash TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE metric_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
                unit TEXT, interface TEXT, ts REAL NOT NULL, source TEXT, tags TEXT
            );
            INSERT INTO metric_points(device_id, metric, value, ts, source)
            VALUES('d1', 'cpu_pct', 42.0, 1700000000.0, 'simulator');
            INSERT INTO metric_points(device_id, metric, value, unit, interface, ts, source, tags)
            VALUES('d1', 'if_in_bps', 15310000000.0, 'bps', 'Gi0/1', 1700000000.0, 'simulator',
                   '{"link_id": "l1"}');
            """
        )
        conn.execute(
            "INSERT INTO topology_snapshots(captured_at, source, node_count, link_count, "
            "payload_hash, data) VALUES(?, ?, ?, ?, ?, ?)",
            (1700000000.0, "netcon", 2, 1, "hash", topology.model_dump_json()),
        )
        conn.commit()
        conn.close()

        store = Store(db_path)
        store.init_db()  # v2 -> v3：metric_points 增加 speed_bps / clamped + 历史回填
        old_points = store.load_metrics("d1", limit=10)
        by_metric = {point["metric"]: point for point in old_points}
        # 设备级指标无端口速率上限，保持 NULL/False
        assert by_metric["cpu_pct"]["value"] == 42.0
        assert by_metric["cpu_pct"]["speed_bps"] is None
        assert by_metric["cpu_pct"]["clamped"] is False
        # 历史端口 bps：用拓扑端口速率回填，并把 15.31e9 钳制到 100Mbps
        legacy = by_metric["if_in_bps"]
        assert legacy["speed_bps"] == 1e8
        assert legacy["value"] == 1e8
        assert legacy["clamped"] is True

        # 新数据可写入并读出物理约束字段
        store.save_metrics(
            [
                MetricPoint(
                    device_id="d1",
                    metric="if_in_bps",
                    value=9.9e7,
                    unit="bps",
                    interface="Gi0/1",
                    ts=1700000001.0,
                    speed_bps=1e8,
                    clamped=True,
                )
            ]
        )
        new_points = [
            point
            for point in store.load_metrics("d1", limit=10, metric="if_in_bps")
            if point["value"] == 9.9e7
        ]
        assert len(new_points) == 1
        assert new_points[0]["speed_bps"] == 1e8
        assert new_points[0]["clamped"] is True

    def test_schema_migration_from_v1(self, tmp_path):
        """旧库（v1 alerts 表）启动时应自动迁移出 W3 字段，不丢历史。"""
        import sqlite3

        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL,
                detail TEXT, dedup_key TEXT, device_id TEXT, link_id TEXT, issue_id TEXT,
                created_at REAL NOT NULL, source TEXT,
                acknowledged INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO alerts(alert_type, severity, title, dedup_key, created_at, source)
            VALUES('device_offline', 'critical', '旧告警', 'device_offline:x', 1700000000.0, 'netcon');
            """
        )
        conn.commit()
        conn.close()

        store = Store(db_path)
        store.init_db()  # 自动迁移
        rows = store.load_alerts(state="all")
        assert len(rows) == 1
        assert rows[0]["state"] == "active"  # 旧告警视作 active
        assert rows[0]["observed_at"] == 1700000000.0  # 用 created_at 回填
        assert rows[0]["source_ts"] is None
        assert store.journal_mode() == "wal"


class TestPipelineNoiseReduction:
    def _make_pipeline(self, tmp_path):
        settings = Settings(
            db_path=str(tmp_path / "campus.db"),
            driver="netcon",
            poll_interval=3600.0,
            simulate_metrics=False,
        )
        store = Store(settings.db_path)
        store.init_db()
        messages: List[Dict[str, Any]] = []

        async def broadcast(message: Dict[str, Any]) -> None:
            messages.append(message)

        return settings, store, messages, broadcast

    def test_repeated_fault_alerts_once(self, tmp_path):
        """故障持续多轮：只推送一次 alert，DB 只有一条 active。"""
        settings, store, messages, broadcast = self._make_pipeline(tmp_path)
        fault = make_result(hosts=[make_host("h1", hostname="PC-1", ping_status="FAILURE")])
        driver = ScriptedDriver([fault, fault, fault])
        pipeline = CollectorPipeline(
            settings=settings, store=store, driver=driver, broadcast=broadcast
        )

        async def scenario():
            await pipeline.poll_once()
            await pipeline.poll_once()
            await pipeline.poll_once()

        run_async(scenario())
        # 首轮基线静默持久化（不广播），此后故障持续 + active 去重 → 不再广播
        alert_messages = [msg for msg in messages if msg["type"] == "alert"]
        assert len(alert_messages) == 0
        assert len(store.load_active_alerts()) == 1
        assert store.alert_stats()["active"] == 1

    def test_multi_clear_aggregated_into_one_summary(self, tmp_path):
        """一次恢复涉及多条告警：只推一条 alerts_cleared 摘要，不逐条刷屏。"""
        settings, store, messages, broadcast = self._make_pipeline(tmp_path)
        # 第一轮：3 台主机故障 + 3 条 NC issue
        fault = make_result(
            hosts=[
                make_host("h1", hostname="PC-1", ping_status="FAILURE"),
                make_host("h2", hostname="PC-2", ping_status="FAILURE"),
                make_host("h3", hostname="PC-3", ping_status="FAILURE"),
            ],
            issues=[
                make_issue("i-1", issue_source="PC-1"),
                make_issue("i-2", issue_source="PC-2"),
                make_issue("i-3", issue_source="PC-3"),
            ],
        )
        # 第二轮：主机恢复，NC issue 仍存在（stale_source）→ 6 条恢复事件
        recovered = make_result(
            hosts=[
                make_host("h1", hostname="PC-1", ping_status="SUCCESS"),
                make_host("h2", hostname="PC-2", ping_status="SUCCESS"),
                make_host("h3", hostname="PC-3", ping_status="SUCCESS"),
            ],
            issues=[
                make_issue("i-1", issue_source="PC-1"),
                make_issue("i-2", issue_source="PC-2"),
                make_issue("i-3", issue_source="PC-3"),
            ],
        )
        driver = ScriptedDriver([fault, recovered])
        pipeline = CollectorPipeline(
            settings=settings, store=store, driver=driver, broadcast=broadcast
        )

        async def scenario():
            await pipeline.poll_once()  # 基线：静默记录 6 条 active
            await pipeline.poll_once()  # 恢复：6 条 cleared 聚合为 1 条摘要

        run_async(scenario())
        cleared_messages = [msg for msg in messages if msg["type"] == "alerts_cleared"]
        assert len(cleared_messages) == 1
        summary = cleared_messages[0]["data"]
        assert summary["count"] == 6
        assert summary["stale_source_count"] == 3  # NC issue 滞后导致的 stale_source
        assert "6 条告警" in summary["summary"]
        assert "NC 问题记录滞后" in summary["summary"]
        assert len(summary["items"]) == 6
        # 恢复事件不再产生额外 info 行；6 条 incident 全部变 cleared
        assert store.alert_stats()["active"] == 0
        assert store.alert_stats()["cleared"] == 6
        assert store.alert_stats()["info"] == 0
        assert store.alert_stats()["total"] == 6

    def test_no_duplicate_summary_after_clear(self, tmp_path):
        """恢复后继续轮询：不应重复推送 alerts_cleared。"""
        settings, store, messages, broadcast = self._make_pipeline(tmp_path)
        fault = make_result(hosts=[make_host("h1", ping_status="FAILURE")])
        recovered = make_result(hosts=[make_host("h1", ping_status="SUCCESS")])
        driver = ScriptedDriver([fault, recovered, recovered])
        pipeline = CollectorPipeline(
            settings=settings, store=store, driver=driver, broadcast=broadcast
        )

        async def scenario():
            await pipeline.poll_once()
            await pipeline.poll_once()
            await pipeline.poll_once()

        run_async(scenario())
        cleared_messages = [msg for msg in messages if msg["type"] == "alerts_cleared"]
        assert len(cleared_messages) == 1
        assert store.load_active_alerts() == []
