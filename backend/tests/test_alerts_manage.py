# -*- coding: utf-8 -*-
"""W9-1 告警数据管理测试：软删除 / 清空 / 静默 / 撤销 + 审计 + active 不可删不变式。"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Alert


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "alerts_manage.db"),
        driver="simulator",
        poll_interval=3600.0,
        simulate_metrics=False,
        log_level="WARNING",
        nc_timeout=0.5,
    )


def make_alert(
    dedup_key: str,
    alert_type: str = "host_down",
    severity: str = "critical",
    state: str = "cleared",
    observed_at: float = 1793340000.0,
) -> Alert:
    return Alert(
        alert_type=alert_type,
        severity=severity,
        title="测试告警 {}".format(dedup_key),
        detail="detail",
        dedup_key=dedup_key,
        state=state,
        observed_at=observed_at,
        created_at=observed_at,
    )


def seed_alerts(app) -> dict:
    """向应用 DB 写入多状态告警，返回 dedup_key -> id 映射。"""
    store = app.state.store
    store.save_alert_events(
        [
            make_alert("h:cleared-1", state="cleared"),
            make_alert("h:cleared-2", state="cleared", observed_at=1793350000.0),
            make_alert("h:info-1", alert_type="device_online", severity="info", state="info"),
            make_alert("h:active-1", state="active"),
        ]
    )
    rows = store.load_alerts(limit=50, state="all", include_deleted=True)
    return {row["dedup_key"]: row["id"] for row in rows}


def test_soft_delete_and_restore_cleared_alert(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        ids = seed_alerts(app)
        alert_id = ids["h:cleared-1"]

        listed_before = client.get("/api/alerts", params={"state": "all"}).json()
        assert alert_id in [row["id"] for row in listed_before["alerts"]]

        response = client.delete(
            "/api/alerts/{}".format(alert_id), headers={"X-Operator": "agent"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["deleted"] == 1
        assert body["id"] == alert_id
        assert body["deleted_at"] is not None

        # 默认列表过滤软删除
        listed_after = client.get("/api/alerts", params={"state": "all"}).json()
        assert alert_id not in [row["id"] for row in listed_after["alerts"]]
        # include_deleted 可见（带 deleted_at）
        listed_all = client.get(
            "/api/alerts", params={"state": "all", "include_deleted": True}
        ).json()
        deleted_row = next(row for row in listed_all["alerts"] if row["id"] == alert_id)
        assert deleted_row["deleted_at"] is not None

        # 撤销软删除
        restored = client.post(
            "/api/alerts/{}/restore".format(alert_id), headers={"X-Operator": "agent"}
        )
        assert restored.status_code == 200
        assert restored.json()["restored"] == 1
        listed_restored = client.get("/api/alerts", params={"state": "all"}).json()
        assert alert_id in [row["id"] for row in listed_restored["alerts"]]

        # 审计记录两笔操作
        audit = client.get("/api/audit", params={"action": "alert.delete"}).json()
        assert audit["count"] >= 1
        assert audit["items"][0]["operator"] == "agent"
        assert audit["items"][0]["target"] == str(alert_id)
        restore_audit = client.get("/api/audit", params={"action": "alert.restore"}).json()
        assert restore_audit["count"] >= 1
        # 未删除的告警 restore → 409 + 审计 rejected
        conflict = client.post("/api/alerts/{}/restore".format(alert_id))
        assert conflict.status_code == 409


def test_active_alert_cannot_be_deleted(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        ids = seed_alerts(app)
        active_id = ids["h:active-1"]
        response = client.delete("/api/alerts/{}".format(active_id))
        assert response.status_code == 409
        assert "active" in response.json()["detail"]
        # active 告警仍在列表
        active = client.get("/api/alerts", params={"state": "active"}).json()
        assert active["count"] == 1
        # 审计记录 rejected
        audit = client.get("/api/audit", params={"action": "alert.delete"}).json()
        rejected = [item for item in audit["items"] if item["result"].startswith("rejected")]
        assert rejected and rejected[0]["target"] == str(active_id)


def test_clear_scopes_only_remove_recovered(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        ids = seed_alerts(app)
        # scope=cleared：只删 cleared（2 条），info/active 保留
        body = client.post(
            "/api/alerts/clear", json={"scope": "cleared"}, headers={"X-Operator": "web-ui"}
        ).json()
        assert body["deleted"] == 2
        remaining = client.get(
            "/api/alerts", params={"state": "all", "include_deleted": True}
        ).json()
        keys = {row["dedup_key"] for row in remaining["alerts"] if not row["deleted_at"]}
        assert keys == {"h:info-1", "h:active-1"}
        assert ids["h:cleared-1"] not in keys
        # scope=all_cleared：清掉 info
        body = client.post("/api/alerts/clear", json={"scope": "all_cleared"}).json()
        assert body["deleted"] == 1
        remaining = client.get(
            "/api/alerts", params={"state": "all", "include_deleted": True}
        ).json()
        keys = {row["dedup_key"] for row in remaining["alerts"] if not row["deleted_at"]}
        assert keys == {"h:active-1"}
        # summary 只统计未删除
        summary = client.get("/api/alerts/summary").json()
        assert summary["active"] == 1
        assert summary["cleared"] == 0


def test_clear_before_filter(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        seed_alerts(app)
        # before 介于 1793340000 与 1793350000 之间：只应删除第一条
        body = client.post(
            "/api/alerts/clear",
            json={"scope": "cleared", "before": 1793345000.0},
        ).json()
        assert body["deleted"] == 1
        remaining = client.get(
            "/api/alerts", params={"state": "all", "include_deleted": True}
        ).json()
        cleared = [
            row
            for row in remaining["alerts"]
            if row["state"] == "cleared" and not row["deleted_at"]
        ]
        assert len(cleared) == 1
        assert cleared[0]["dedup_key"] == "h:cleared-2"


def test_silence_active_alert_and_filters(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        ids = seed_alerts(app)
        active_id = ids["h:active-1"]
        response = client.post(
            "/api/alerts/{}/silence".format(active_id),
            headers={"X-Operator": "agent"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["silenced"] == 1
        assert body["status"] == "silenced"
        assert body["state"] == "active"  # 静默不改变生命周期状态

        silenced = client.get("/api/alerts", params={"status": "silenced"}).json()
        assert silenced["count"] == 1
        assert silenced["alerts"][0]["id"] == active_id
        open_alerts = client.get(
            "/api/alerts", params={"state": "active", "status": "open"}
        ).json()
        assert open_alerts["count"] == 0
        # 静默后的 active 仍不可删除（不变式：active 只能静默）
        assert client.delete("/api/alerts/{}".format(active_id)).status_code == 409
        # 非 active 告警不能 silence
        cleared_id = ids["h:cleared-1"]
        assert (
            client.post("/api/alerts/{}/silence".format(cleared_id)).status_code == 409
        )
        # 不存在 → 404
        assert client.post("/api/alerts/99999/silence").status_code == 404


def test_alerts_audit_alias_and_limit(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        ids = seed_alerts(app)
        client.delete("/api/alerts/{}".format(ids["h:cleared-1"]))
        for _ in range(3):
            client.post("/api/scene/buildings", json={"name": "测试楼"})
        audit = client.get("/api/alerts/audit", params={"limit": 2}).json()
        assert audit["count"] == 2
        assert len(audit["items"]) == 2
        assert all("created_at_iso" in item for item in audit["items"])
