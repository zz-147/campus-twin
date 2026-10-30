# -*- coding: utf-8 -*-
"""W10 告警根因分组测试（分组优先级 / 下游抑制 / 整组 ack / API 契约）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi.testclient import TestClient

from app.alert_groups import build_alert_groups
from app.config import Settings
from app.main import create_app
from app.models import Alert


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "groups.db"),
        driver="simulator",
        poll_interval=3600.0,
        simulate_metrics=False,
        log_level="WARNING",
        nc_timeout=0.5,
    )


def alert(
    alert_id: int,
    alert_type: str,
    *,
    device_id: str = None,
    link_id: str = None,
    severity: str = "critical",
    observed_at: float = 1793340000.0,
    dedup_key: str = None,
) -> Dict[str, Any]:
    return {
        "id": alert_id,
        "alert_type": alert_type,
        "severity": severity,
        "device_id": device_id,
        "link_id": link_id,
        "issue_id": "issue-{}".format(alert_id) if alert_type == "health_issue" else None,
        "observed_at": observed_at,
        "created_at": observed_at,
        "title": "{} alert {}".format(alert_type, alert_id),
        "detail": "detail",
        "dedup_key": dedup_key or "{}:{}".format(alert_type, alert_id),
        "state": "active",
    }


def make_snapshot() -> Dict[str, Any]:
    return {
        "devices": [
            {"id": "core", "hostname": "Core-3650", "management_ip": "192.168.20.1"},
            {"id": "sw-jx", "hostname": "SW-JX-2960", "management_ip": "192.168.10.11"},
            {"id": "sw-sx", "hostname": "SW-SX-2960", "management_ip": "192.168.10.12"},
            {"id": "sw-ss", "hostname": "SW-SS-2960", "management_ip": "192.168.10.13"},
        ],
        "topology": {
            "nodes": [],
            "links": [
                {"id": "link-core-jx", "source": "core", "target": "sw-jx"},
                {"id": "link-core-sx", "source": "core", "target": "sw-sx"},
                {"id": "link-core-ss", "source": "core", "target": "sw-ss"},
            ],
        },
    }


class TestBuildGroups:
    def test_link_root_suppresses_downstream_members(self):
        alerts = [
            alert(1, "link_down", device_id="core", link_id="link-core-jx"),
            alert(2, "device_unreachable", device_id="sw-jx"),
            alert(3, "host_down", device_id="sw-jx"),
            alert(4, "host_down", device_id="sw-sx"),  # 其他链路，不应被抑制
            alert(5, "health_issue", device_id="unknown-device"),  # 独立组
        ]
        result = build_alert_groups(alerts, make_snapshot(), state="active")
        groups = result["groups"]
        assert result["count"] == 3
        # 根因 link_down 组：成员 1 条 + 抑制 2 条（sw-jx 的 unreachable/host）
        root = next(group for group in groups if group["root_cause"] == "link_down")
        assert root["root_device_id"] == "core"
        assert root["root_device_name"] == "Core-3650"
        assert root["member_count"] == 1
        assert root["suppressed_count"] == 2
        suppressed_members = [
            member for member in root["members"] if member["suppressed_by"]
        ]
        assert len(suppressed_members) == 2
        assert all(member["suppressed_by"] == root["group_id"] for member in suppressed_members)
        assert "2 项下游告警被抑制" in root["title"]
        # sw-sx 的主机告警仍是独立组（不被无关链路抑制）
        sw_sx_group = next(
            group for group in groups if group["root_device_id"] == "sw-sx"
        )
        assert sw_sx_group["root_cause"] == "host_down"
        assert sw_sx_group["suppressed_count"] == 0
        # 无法映射设备的 issue 单独成组
        assert any(group["root_cause"] == "health_issue" for group in groups)

    def test_device_unreachable_priority_over_host_down(self):
        alerts = [
            alert(1, "host_down", device_id="sw-jx", severity="critical"),
            alert(2, "device_unreachable", device_id="sw-jx", severity="warning"),
        ]
        result = build_alert_groups(alerts, make_snapshot(), state="active")
        assert result["count"] == 1
        group = result["groups"][0]
        assert group["root_cause"] == "device_unreachable"
        assert group["member_count"] == 2
        assert group["severity"] == "critical"

    def test_group_id_stable(self):
        alerts = [alert(1, "link_down", device_id="core", link_id="link-core-jx")]
        first = build_alert_groups(alerts, make_snapshot(), state="active")
        second = build_alert_groups(alerts, make_snapshot(), state="active")
        assert first["groups"][0]["group_id"] == second["groups"][0]["group_id"]
        assert first["groups"][0]["group_id"].startswith("g_")

    def test_cleared_groups_do_not_suppress(self):
        alerts = [
            alert(1, "link_down", device_id="core", link_id="link-core-jx"),
            alert(2, "device_unreachable", device_id="sw-jx"),
        ]
        result = build_alert_groups(alerts, make_snapshot(), state="cleared")
        assert result["count"] == 2
        assert all(group["suppressed_count"] == 0 for group in result["groups"])


class TestAlertGroupsApi:
    def test_api_groups_and_ack(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            topology = client.get("/api/topology").json()
            link = topology["links"][0]
            store = app.state.store
            store.save_alert_events(
                [
                    Alert(
                        alert_type="link_down",
                        severity="critical",
                        title="链路故障",
                        dedup_key="link_down:{}".format(link["id"]),
                        link_id=link["id"],
                        device_id=link["source"],
                        state="active",
                        observed_at=1793340000.0,
                    ),
                    Alert(
                        alert_type="device_unreachable",
                        severity="critical",
                        title="设备不可达",
                        dedup_key="device_unreachable:{}".format(link["target"]),
                        device_id=link["target"],
                        state="active",
                        observed_at=1793340001.0,
                    ),
                    Alert(
                        alert_type="host_down",
                        severity="critical",
                        title="主机离线",
                        dedup_key="host_down:sw-target-host",
                        device_id=link["target"],
                        state="active",
                        observed_at=1793340002.0,
                    ),
                ]
            )

            groups_payload = client.get(
                "/api/alerts/groups", params={"state": "active"}
            ).json()
            assert groups_payload["count"] >= 1
            root = next(
                group
                for group in groups_payload["groups"]
                if group["root_cause"] == "link_down"
            )
            assert root["suppressed_count"] == 2
            assert root["root_device_id"] == link["source"]

            # 整组 ack：成员（含被抑制成员）全部 acknowledged=1
            response = client.post(
                "/api/alerts/groups/{}/ack".format(root["group_id"]),
                headers={"X-Operator": "web-ui"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is True
            assert body["acknowledged"] == len(root["members"])
            rows = store.load_alerts(limit=100, state="active")
            acked = [row for row in rows if row["acknowledged"]]
            assert len(acked) == body["acknowledged"]
            audit = client.get(
                "/api/audit", params={"action": "alert.group_ack"}
            ).json()
            assert audit["count"] == 1
            assert audit["items"][0]["operator"] == "web-ui"
            # 不存在的组 → 404 + rejected 审计
            missing = client.post("/api/alerts/groups/g_not_exist/ack")
            assert missing.status_code == 404

    def test_api_groups_empty_when_no_active_alerts(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            payload = client.get("/api/alerts/groups").json()
            assert payload["count"] == 0
            assert payload["groups"] == []
