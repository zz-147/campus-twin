# -*- coding: utf-8 -*-
"""W9-2 场景配置 API + 新设备检测测试（完全离线）。"""
from __future__ import annotations

import json
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


class TestDeviceNameRules:
    """① 类型-名称前缀自动纠正 ② 非法名 409/422 ③ rename 版本/审计 ④ rename 冲突。"""

    def test_add_pc_with_switch_name_auto_corrected(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            before = client.get("/api/scene").json()["version"]
            response = client.post(
                "/api/scene/buildings/teach/devices",
                json={"name": "SW-JX-2961", "type": "pc"},
                headers={"X-Operator": "agent"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["name_corrected"] is True
            assert body["original_name"] == "SW-JX-2961"
            assert body["device"]["name"] == "PC-JX3"
            assert body["device"]["type"] == "pc"
            assert body["version"] == before + 1
            assert "自动纠正" in body["message"]

            scene = client.get("/api/scene").json()
            teach = building_by_id(scene, "teach")
            names = [device["name"] for device in teach["devices"]]
            assert "PC-JX3" in names
            assert "SW-JX-2961" not in names
            assert len(names) == 4  # 默认 3 台 + 新 PC

            # 第二次传同样错误名：命中 alias，更新原设备而不是新建
            again = client.post(
                "/api/scene/buildings/teach/devices",
                json={"name": "SW-JX-2961", "type": "pc"},
            ).json()
            assert again["device"]["name"] == "PC-JX3"
            scene = client.get("/api/scene").json()
            teach_names = [device["name"] for device in building_by_id(scene, "teach")["devices"]]
            assert teach_names.count("PC-JX3") == 1
            assert len(teach_names) == 4

            # 再传另一个错误名 → 同楼同类型最大编号 +1
            third = client.post(
                "/api/scene/buildings/teach/devices",
                json={"name": "SW-JX-9999", "type": "pc"},
            ).json()
            assert third["name_corrected"] is True
            assert third["device"]["name"] == "PC-JX4"

            audit = client.get(
                "/api/audit", params={"action": "scene.device.upsert"}
            ).json()
            assert audit["count"] >= 1
            first_detail = audit["items"][-1]["detail"]
            if isinstance(first_detail, str):
                first_detail = json.loads(first_detail)
            assert first_detail["name_corrected"] is True
            assert first_detail["original_name"] == "SW-JX-2961"
            assert first_detail["requested_name"] == "SW-JX-2961"

    def test_all_type_prefixes_and_controller(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            cases = [
                ("pc", "XX-1", "PC-"),
                ("ap", "XX-2", "AP-"),
                ("server", "XX-3", "SRV-"),
                ("router", "XX-4", "RTR-"),
            ]
            for index, (device_type, bad_name, expected_prefix) in enumerate(cases):
                response = client.post(
                    "/api/scene/buildings/train/devices",
                    json={"name": bad_name, "type": device_type},
                ).json()
                assert response["name_corrected"] is True
                assert response["device"]["name"].startswith(expected_prefix)
            # controller 前缀 NC；默认已有 NC → 生成 NC-2 之类
            controller = client.post(
                "/api/scene/buildings/core/devices",
                json={"name": "WRONG-5", "type": "controller"},
            ).json()
            assert controller["device"]["name"].startswith("NC")

    def test_valid_unknown_and_invalid_names(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            # 合法前缀名不纠正
            valid = client.post(
                "/api/scene/buildings/teach/devices",
                json={"name": "PC-JX8", "type": "pc"},
            ).json()
            assert valid["name_corrected"] is False
            assert valid["device"]["name"] == "PC-JX8"
            # 未知类型不做前缀校验（保留原名）
            unknown = client.post(
                "/api/scene/buildings/teach/devices",
                json={"name": "ZZZ-1", "type": "unknown"},
            ).json()
            assert unknown["name_corrected"] is False
            assert unknown["device"]["name"] == "ZZZ-1"

            assert (
                client.post(
                    "/api/scene/buildings/teach/devices",
                    json={"name": "", "type": "pc"},
                ).status_code
                == 422
            )
            whitespace = client.post(
                "/api/scene/buildings/teach/devices",
                json={"name": "   ", "type": "pc"},
            )
            assert whitespace.status_code in (409, 422)
            illegal = client.post(
                "/api/scene/buildings/teach/devices",
                json={"name": "PC JX 1", "type": "pc"},
            )
            assert illegal.status_code == 422
            too_long = client.post(
                "/api/scene/buildings/teach/devices",
                json={"name": "PC-" + "X" * 80, "type": "pc"},
            )
            assert too_long.status_code in (422,)

    def test_rename_success_version_and_audit(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            before = client.get("/api/scene").json()["version"]
            response = client.patch(
                "/api/scene/devices/PC-JX1",
                json={"new_name": "PC-JX9"},
                headers={"X-Operator": "agent"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is True and body["changed"] is True
            assert body["renamed_from"] == "PC-JX1"
            assert body["device"]["name"] == "PC-JX9"
            assert body["version"] == before + 1
            scene = client.get("/api/scene").json()
            names = {
                device["name"]
                for building in scene["buildings"]
                for device in building["devices"]
            }
            assert "PC-JX9" in names and "PC-JX1" not in names
            assert scene["version"] == before + 1
            # 返回的 scene 是更新后的文档
            assert any(
                device["name"] == "PC-JX9"
                for building in body["scene"]["buildings"]
                for device in building["devices"]
            )
            audit = client.get(
                "/api/audit", params={"action": "scene.device.rename"}
            ).json()
            assert audit["count"] == 1
            item = audit["items"][0]
            assert item["operator"] == "agent"
            assert item["target"] == "PC-JX9"
            detail = item["detail"]
            if isinstance(detail, str):
                detail = json.loads(detail)
            assert detail["old_name"] == "PC-JX1"
            assert detail["new_name"] == "PC-JX9"
            # 同名幂等：不再 version++
            same = client.patch(
                "/api/scene/devices/PC-JX9", json={"new_name": "PC-JX9"}
            ).json()
            assert same["changed"] is False
            assert same["version"] == before + 1

    def test_rename_legacy_name_to_valid_prefix(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            response = client.patch(
                "/api/scene/devices/Server-1",
                json={"new_name": "SRV-CORE-1"},
                headers={"X-Operator": "web-ui"},
            )
            assert response.status_code == 200
            assert response.json()["device"]["name"] == "SRV-CORE-1"
            scene = client.get("/api/scene").json()
            names = {
                device["name"]
                for building in scene["buildings"]
                for device in building["devices"]
            }
            assert "SRV-CORE-1" in names and "Server-1" not in names

    def test_rename_conflicts(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            # 重名冲突
            duplicate = client.patch(
                "/api/scene/devices/PC-JX1", json={"new_name": "PC-JX2"}
            )
            assert duplicate.status_code == 409
            assert "已存在" in duplicate.json()["detail"]
            # 类型前缀不一致
            mismatch = client.patch(
                "/api/scene/devices/PC-JX1", json={"new_name": "SW-JX-9001"}
            )
            assert mismatch.status_code == 409
            assert "不匹配" in mismatch.json()["detail"]
            # 非法字符 → 422
            illegal = client.patch(
                "/api/scene/devices/PC-JX1", json={"new_name": "PC JX 1"}
            )
            assert illegal.status_code == 422
            # 空/空白
            blank = client.patch(
                "/api/scene/devices/PC-JX1", json={"new_name": "   "}
            )
            assert blank.status_code in (409, 422)
            # 不存在设备 → 404
            assert (
                client.patch(
                    "/api/scene/devices/NOPE", json={"new_name": "PC-JX7"}
                ).status_code
                == 404
            )


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
