# -*- coding: utf-8 -*-
"""W9-2 场景配置 API + 新设备检测测试（完全离线）。"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Device, Link, Topology, TopologyNode
from app.scene import MIN_DEVICE_SPACING, SceneService
from app.store import Store


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "scene.db"),
        driver="simulator",
        poll_interval=3600.0,
        simulate_metrics=False,
        log_level="WARNING",
        nc_timeout=0.5,
    )


def building_by_id(scene: dict, building_id: str) -> dict:
    return next(b for b in scene["buildings"] if b["id"] == building_id)


class TestSceneSeed:
    def test_default_scene_contract(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            scene = client.get("/api/scene").json()
            assert scene["version"] >= 1
            assert scene["theme"]["style"] == "low-poly"
            assert len(scene["buildings"]) == 4
            assert [b["id"] for b in scene["buildings"]] == ["core", "teach", "train", "dorm"]
            positions = {b["id"]: b["position"] for b in scene["buildings"]}
            assert positions["core"] == {"x": 0.0, "z": 0.0}
            assert positions["teach"] == {"x": -120.0, "z": 40.0}
            assert positions["train"] == {"x": 120.0, "z": 40.0}
            assert positions["dorm"] == {"x": 0.0, "z": -140.0}
            # 4 楼栋 / 12 设备
            device_count = sum(len(b["devices"]) for b in scene["buildings"])
            assert device_count == 12
            all_names = {
                device["name"]
                for b in scene["buildings"]
                for device in b["devices"]
            }
            for expected in (
                "Core-3650",
                "Router-4331",
                "Server-1",
                "NC",
                "SW-JX-2960",
                "PC-JX1",
                "PC-JX2",
                "SW-SX-2960",
                "PC-SX1",
                "SW-SS-2960",
                "PC-SS1",
                "PC-SS2",
            ):
                assert expected in all_names
            # 字段契约
            building = scene["buildings"][0]
            assert set(("id", "name", "position", "size", "style", "devices")) <= set(building)
            device = building["devices"][0]
            for field in ("name", "type", "position", "status", "source"):
                assert field in device
            assert set(("x", "y", "z")) <= set(device["position"])

    def test_scene_version_in_health(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            health = client.get("/api/health").json()
            assert health["scene_version"] >= 1
            assert health["db"]["scene_version"] >= 1


class TestSceneMutations:
    def test_add_building_pending_and_audit(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            version_before = client.get("/api/scene").json()["version"]
            response = client.post(
                "/api/scene/buildings",
                json={"name": "实验楼", "position": {"x": 80, "z": -80}},
                headers={"X-Operator": "agent"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is True
            assert body["version"] == version_before + 1
            building = body["building"]
            assert building["name"] == "实验楼"
            assert building["status"] == "pending"  # 默认 pending
            assert building["position"] == {"x": 80.0, "z": -80.0}

            scene = client.get("/api/scene").json()
            assert len(scene["buildings"]) == 5
            assert building_by_id(scene, building["id"])["name"] == "实验楼"
            audit = client.get("/api/audit", params={"action": "scene.building.add"}).json()
            assert audit["count"] == 1
            assert audit["items"][0]["operator"] == "agent"

    def test_upsert_device_collision_auto_offset(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            created = client.post(
                "/api/scene/buildings", json={"name": "测试楼"}
            ).json()
            building_id = created["building"]["id"]
            first = client.post(
                "/api/scene/buildings/{}/devices".format(building_id),
                json={
                    "name": "SW-TEST-1",
                    "type": "switch",
                    "position": {"x": 0, "y": 20, "z": 0},
                },
                headers={"X-Operator": "agent"},
            ).json()
            assert first["device"]["status"] == "pending"
            assert first["adjusted"] is False

            second = client.post(
                "/api/scene/buildings/{}/devices".format(building_id),
                json={
                    "name": "SW-TEST-2",
                    "type": "switch",
                    "position": {"x": 0, "y": 20, "z": 0},
                },
            ).json()
            assert second["adjusted"] is True  # 同坐标冲突 → 自动偏移
            assert second["message"]
            first_pos = first["device"]["position"]
            second_pos = second["device"]["position"]
            distance = (
                (first_pos["x"] - second_pos["x"]) ** 2
                + (first_pos["z"] - second_pos["z"]) ** 2
            ) ** 0.5
            assert distance >= MIN_DEVICE_SPACING

            scene = client.get("/api/scene").json()
            devices = building_by_id(scene, building_id)["devices"]
            assert {device["name"] for device in devices} == {"SW-TEST-1", "SW-TEST-2"}

    def test_confirm_device_pending_to_confirmed(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            scene = client.get("/api/scene").json()
            teach_id = building_by_id(scene, "teach")["id"]
            client.post(
                "/api/scene/buildings/{}/devices".format(teach_id),
                json={"name": "SW-TEACH-NEW", "type": "switch"},
            )
            response = client.post(
                "/api/scene/devices/SW-TEACH-NEW/confirm",
                headers={"X-Operator": "agent"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["changed"] is True
            assert body["device"]["status"] == "confirmed"
            # 二次确认幂等：version 不再增加
            again = client.post("/api/scene/devices/SW-TEACH-NEW/confirm").json()
            assert again["changed"] is False
            assert again["version"] == body["version"]
            audit = client.get("/api/audit", params={"action": "scene.device.confirm"}).json()
            assert audit["count"] == 1
            # 不存在 → 404
            assert client.post("/api/scene/devices/NOPE/confirm").status_code == 404

    def test_delete_device_tombstone(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            scene = client.get("/api/scene").json()
            teach_id = building_by_id(scene, "teach")["id"]
            client.post(
                "/api/scene/buildings/{}/devices".format(teach_id),
                json={"name": "PC-DEL-1", "type": "pc"},
            )
            response = client.delete(
                "/api/scene/devices/PC-DEL-1", headers={"X-Operator": "web-ui"}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["deleted"] == "PC-DEL-1"
            scene = client.get("/api/scene").json()
            names = {
                device["name"]
                for building in scene["buildings"]
                for device in building["devices"]
            }
            assert "PC-DEL-1" not in names
            assert any(
                tombstone["name"] == "PC-DEL-1"
                for tombstone in scene.get("removed_devices", [])
            )
            audit = client.get("/api/audit", params={"action": "scene.device.delete"}).json()
            assert audit["count"] == 1
            assert client.delete("/api/scene/devices/PC-DEL-1").status_code == 404

    def test_reset_scene(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            version = client.get("/api/scene").json()["version"]
            client.post("/api/scene/buildings", json={"name": "临时楼"})
            client.post("/api/scene/reset", headers={"X-Operator": "agent"})
            scene = client.get("/api/scene").json()
            assert len(scene["buildings"]) == 4
            assert sum(len(b["devices"]) for b in scene["buildings"]) == 12
            assert scene["version"] == version + 2  # add + reset
            audit = client.get("/api/audit", params={"action": "scene.reset"}).json()
            assert audit["count"] == 1
            assert audit["items"][0]["operator"] == "agent"

    def test_unknown_building_404(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            response = client.post(
                "/api/scene/buildings/nope/devices", json={"name": "X"}
            )
            assert response.status_code == 404


class TestNewDeviceDetection:
    def test_pending_devices_from_pipeline(self, tmp_path):
        """仿真 inventory（SIM-*）未在默认场景中 → /api/scene/pending 应列出。"""
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            payload = client.get("/api/scene/pending").json()
            assert payload["count"] >= 1
            assert payload["scene_version"] >= 1
            device = payload["devices"][0]
            for field in (
                "name",
                "device_id",
                "mgmt_ip",
                "platform",
                "reachability",
                "collection_status",
                "connected_to",
                "inferred_building",
            ):
                assert field in device
            # 至少一台可通过 hostname 关键词推断到中心机房（SIM-CORE-*）
            assert any(
                item["inferred_building"]
                and item["inferred_building"]["building_id"] == "core"
                for item in payload["devices"]
            )

    def test_service_inference_rules(self, tmp_path):
        store = Store(str(tmp_path / "scene_service.db"))
        store.init_db()
        service = SceneService(store)
        service.get_scene()  # seed

        devices = [
            Device(id="core-3650", hostname="Core-3650", reachability="Reachable",
                   collection_status="Managed", source="netcon"),
            Device(id="new-sw", hostname="SW-XS-2960", management_ip="192.168.10.50",
                   reachability="Reachable", collection_status="Managed", source="netcon"),
            Device(id="new-ap", hostname="AP-JX-1", management_ip="192.168.20.50",
                   reachability="Reachable", collection_status="Managed", source="netcon"),
            Device(id="new-unknown", hostname="UNKNOWN-1", management_ip="10.9.9.9",
                   reachability="Reachable", collection_status="Managed", source="netcon"),
        ]
        topology = Topology(
            nodes=[TopologyNode(id=device.id, source="netcon") for device in devices],
            links=[
                Link(
                    id="l-new-sw",
                    source="new-sw",
                    target="core-3650",
                    start_port_name="Fa0/24",
                    end_port_name="Gi1/0/1",
                    link_status="up",
                    data_source="netcon",
                )
            ],
            source="netcon",
        )
        pending = service.refresh_pending(devices, topology)
        by_name = {item["name"]: item for item in pending}
        # 已在场景中的设备不出现
        assert "Core-3650" not in by_name
        # 1) 拓扑邻居 → 中心机房
        inferred = by_name["SW-XS-2960"]["inferred_building"]
        assert inferred["building_id"] == "core"
        assert inferred["rule"] == "topology_neighbor"
        # 2) hostname 关键词 → 教学楼
        inferred = by_name["AP-JX-1"]["inferred_building"]
        assert inferred["building_id"] == "teach"
        assert inferred["rule"] == "hostname_keyword"
        # 3) 无法推断 → None（交由 Agent/用户确认，不自动建楼）
        assert by_name["UNKNOWN-1"]["inferred_building"] is None
        # 连接关系
        connection = by_name["SW-XS-2960"]["connected_to"][0]
        assert connection["device_name"] == "Core-3650"
        assert connection["local_port"] == "Fa0/24"
        # pending_devices 快照
        snapshot = service.pending_devices()
        assert snapshot["count"] == 3
        assert snapshot["updated_at"] is not None

    def test_ip_prefix_inference(self, tmp_path):
        store = Store(str(tmp_path / "scene_ip.db"))
        store.init_db()
        service = SceneService(store)
        inferred = service.infer_building(name="HOST-A", mgmt_ip="192.168.40.66")
        assert inferred is not None
        assert inferred["building_id"] == "dorm"
        assert inferred["rule"] == "ip_prefix"
