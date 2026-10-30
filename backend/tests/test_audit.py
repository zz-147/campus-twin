# -*- coding: utf-8 -*-
"""W9-3 审计接口 + schema v3→v4 迁移测试（离线）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Alert
from app.scene import SceneService
from app.store import SCHEMA_VERSION, Store


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "audit.db"),
        driver="simulator",
        poll_interval=3600.0,
        simulate_metrics=False,
        log_level="WARNING",
        nc_timeout=0.5,
    )


class TestAuditApi:
    def test_audit_records_format_and_filters(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            # 1) 场景写操作（Agent）
            client.post(
                "/api/scene/buildings",
                json={"name": "审计楼"},
                headers={"X-Operator": "agent"},
            )
            # 2) 告警软删除（默认 web-ui）
            store = app.state.store
            store.save_alert_events(
                [
                    Alert(
                        alert_type="host_down",
                        severity="critical",
                        title="审计测试告警",
                        dedup_key="host_down:audit-1",
                        state="cleared",
                    ),
                    Alert(
                        alert_type="host_down",
                        severity="critical",
                        title="活跃告警",
                        dedup_key="host_down:audit-active",
                        state="active",
                    ),
                ]
            )
            rows = {
                row["dedup_key"]: row["id"]
                for row in store.load_alerts(limit=10, state="all", include_deleted=True)
            }
            assert (
                client.delete("/api/alerts/{}".format(rows["host_down:audit-1"])).status_code
                == 200
            )
            # 3) 被拒绝的写操作（active 不可删）也要审计
            assert (
                client.delete("/api/alerts/{}".format(rows["host_down:audit-active"])).status_code
                == 409
            )

            audit = client.get("/api/audit", params={"limit": 50}).json()
            assert audit["count"] >= 3
            first = audit["items"][0]
            for field in (
                "id",
                "created_at",
                "created_at_iso",
                "operator",
                "action",
                "target",
                "detail",
                "result",
            ):
                assert field in first
            # 时间倒序
            timestamps = [item["created_at"] for item in audit["items"]]
            assert timestamps == sorted(timestamps, reverse=True)

            # action 过滤
            deletes = client.get(
                "/api/audit", params={"action": "alert.delete", "limit": 10}
            ).json()
            assert deletes["count"] == 2
            results = {item["result"] for item in deletes["items"]}
            assert "ok" in results
            assert any(result.startswith("rejected") for result in results)
            # operator 过滤
            agent_items = client.get(
                "/api/audit", params={"operator": "agent", "limit": 10}
            ).json()
            assert agent_items["count"] == 1
            assert agent_items["items"][0]["action"] == "scene.building.add"
            # limit 生效
            limited = client.get("/api/audit", params={"limit": 2}).json()
            assert limited["count"] == 2

    def test_audit_alias_endpoint(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            client.post("/api/scene/reset", headers={"X-Operator": "agent"})
            alias = client.get("/api/alerts/audit").json()
            assert alias["count"] == 1
            assert alias["items"][0]["action"] == "scene.reset"

    def test_scene_mutations_audited_by_default_operator(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            client.post("/api/scene/buildings", json={"name": "默认操作人楼"})
            audit = client.get("/api/audit", params={"action": "scene.building.add"}).json()
            assert audit["items"][0]["operator"] == "web-ui"


class TestMigrationV3ToV4:
    def _create_v3_database(self, db_path: str) -> None:
        """造一个 v3 形态的库：alerts 有 W3 字段但无 deleted_at/status/silenced_at。"""
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta(key, value) VALUES('schema_version', '3');
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL,
                detail TEXT, dedup_key TEXT, device_id TEXT, link_id TEXT, issue_id TEXT,
                created_at REAL NOT NULL, source TEXT,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                observed_at REAL, source_ts TEXT,
                state TEXT NOT NULL DEFAULT 'active',
                resolved_at REAL, clear_reason TEXT,
                stale_source INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO alerts(alert_type, severity, title, dedup_key, created_at, observed_at,
                               source, state, resolved_at)
            VALUES('device_unreachable', 'critical', '旧告警', 'device_unreachable:old',
                   1793340000.0, 1793340000.0, 'netcon', 'cleared', 1793340100.0);
            """
        )
        conn.commit()
        conn.close()

    def test_v3_to_v4_migration_preserves_alerts_and_seeds_scene(self, tmp_path):
        db_path = str(tmp_path / "v3.db")
        self._create_v3_database(db_path)
        store = Store(db_path)
        store.init_db()

        # 版本升级 + 旧告警保留（新字段默认值正确）
        rows = store.load_alerts(limit=10, state="all", include_deleted=True)
        assert len(rows) == 1
        assert rows[0]["title"] == "旧告警"
        assert rows[0]["state"] == "cleared"
        assert rows[0]["deleted_at"] is None
        assert rows[0]["status"] == "open"
        assert rows[0]["silenced_at"] is None
        meta = store.stats()["meta"]
        assert meta.get("schema_version") == str(SCHEMA_VERSION)
        assert SCHEMA_VERSION == 4

        # scene_config / audit_log 新表存在（scene 首次访问自动 seed 4 楼栋/12 设备）
        assert store.scene_version() == 0
        scene = SceneService(store).get_scene()
        assert len(scene["buildings"]) == 4
        assert sum(len(b["devices"]) for b in scene["buildings"]) == 12
        assert store.scene_version() == 1
        assert store.load_audit() == []

        # 迁移后新字段可正常写入（软删/静默/审计）
        store.add_audit("agent", "migration.check", "db", "ok")
        assert len(store.load_audit()) == 1


def test_schema_version_constant():
    assert SCHEMA_VERSION == 4
