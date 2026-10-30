# -*- coding: utf-8 -*-
"""FastAPI 接口 / WebSocket 集成测试（TestClient，不依赖 PT 与外部网络）。

两个关键验收场景：
1. 仿真模式：所有 REST 接口有数据、WebSocket 能连上并收到首帧快照、ping/pong 正常。
2. netcon 驱动但 NC 不可达（端口不通）：服务不崩、接口返回空结构、health 仍 ok。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make_settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        db_path=str(tmp_path / "test.db"),
        driver="simulator",
        poll_interval=3600.0,  # 测试中只依赖启动时的首轮采集
        simulate_metrics=True,
        log_level="WARNING",
        nc_timeout=0.5,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_simulator_mode_endpoints(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        # ---- 服务健康 ----
        health = client.get("/api/health")
        assert health.status_code == 200
        payload = health.json()
        assert payload["status"] == "ok"
        assert payload["driver"] == "simulator"
        assert payload["metrics_source"] == "simulator"
        assert payload["db"]["journal_mode"] == "wal"
        assert payload["counts"]["devices"] > 0

        # ---- 设备/拓扑/主机/健康 ----
        devices = client.get("/api/devices").json()
        assert devices["count"] > 0
        assert all(device["source"] == "simulator" for device in devices["devices"])

        topology = client.get("/api/topology").json()
        assert topology["counts"]["nodes"] > 0
        assert topology["counts"]["links"] > 0
        assert topology["source"] == "simulator"

        hosts = client.get("/api/hosts").json()
        assert hosts["count"] > 0
        # W3-A：主机快照带宿主机观测时间与新鲜度
        assert hosts["observed_at"] is not None
        assert hosts["snapshot_age_seconds"] is not None
        assert all("ping_status" in host for host in hosts["hosts"])

        issues = client.get("/api/issues").json()
        assert issues["count"] == 0
        assert issues["issues"] == []

        # W3.1：network-health 字段自洽（自算口径 + NC 原始字段并存）
        network_health = client.get("/api/network-health").json()
        assert network_health["source_of_truth"] == "computed_from_devices"
        health = network_health["network_health"]
        device_count = network_health["device_count"]
        assert device_count > 0
        assert health["device_total"] == device_count
        assert health["device_healthy"] + health["device_unhealthy"] == health["device_total"]
        assert health["total_devices"] == health["device_total"]
        assert health["healthy_devices"] == health["device_healthy"]
        assert isinstance(health["health_percentage"], (int, float))
        assert health["health_percentage"] == round(
            health["device_healthy"] / health["device_total"] * 100.0, 2
        )
        assert health["source_of_truth"] == "computed_from_devices"

        alerts = client.get("/api/alerts").json()
        assert alerts["count"] == 0
        assert alerts["alerts"] == []

        drivers = client.get("/api/drivers").json()
        assert drivers["active"] == "simulator"
        assert set(drivers["available"]) == {"netcon", "simulator"}

        # ---- W3 检测规则目录 / 告警生命周期 ----
        rules = client.get("/api/detection/rules").json()
        rule_ids = {item["rule_id"] for item in rules["rules"]}
        assert {"host_down", "device_unreachable", "link_down", "health_issue"} <= rule_ids
        assert rules["alert_states"] == {"active": 0, "cleared": 0, "info": 0, "total": 0}
        health = client.get("/api/health").json()
        assert health["detection"]["engine"] == "self-rules-v1"
        assert "host_down" in health["detection"]["rules"]

        # ---- 指标时序 ----
        device_id = devices["devices"][0]["id"]
        metrics = client.get("/api/metrics/{}".format(device_id), params={"limit": 500}).json()
        assert metrics["known_device"] is True
        assert metrics["source"] == "simulator"
        assert metrics["count"] > 0
        assert all(point["source"] == "simulator" for point in metrics["metrics"])
        # W3.1 物理约束：端口 bps 不得超过 speed_bps
        bps_points = [
            point
            for point in metrics["metrics"]
            if point["metric"] in ("if_in_bps", "if_out_bps")
        ]
        assert bps_points
        for point in bps_points:
            assert point["speed_bps"] is not None and point["speed_bps"] > 0
            assert point["value"] <= point["speed_bps"] + 1e-6
            assert isinstance(point["clamped"], bool)
        assert "physical_constraint" in metrics
        filtered = client.get(
            "/api/metrics/{}".format(device_id),
            params={"limit": 50, "metric": "cpu_pct"},
        ).json()
        assert filtered["count"] > 0
        assert all(point["metric"] == "cpu_pct" for point in filtered["metrics"])
        unknown = client.get("/api/metrics/no-such-device").json()
        assert unknown["count"] == 0
        assert unknown["known_device"] is False

        # ---- WebSocket ----
        with client.websocket_connect("/ws") as websocket:
            first = websocket.receive_json()
            assert first["type"] == "snapshot"
            assert first["data"]["counts"]["devices"] > 0
            assert first["data"]["source"] == "simulator"
            websocket.send_text("ping")
            pong = websocket.receive_json()
            assert pong["type"] == "pong"


def test_web_socket_receives_alert_broadcast(tmp_path):
    """注入故障后再跑一轮采集：WebSocket 客户端应收到 alert 帧。"""
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            first = websocket.receive_json()  # 首帧快照
            assert first["type"] == "snapshot"
            # 注入链路故障，并手动触发一轮采集（周期任务因 poll_interval 很大不会跑）
            response = client.post(
                "/api/debug/simulator/link-failure", json={"link_id": "sim-l-core"}
            )
            assert response.status_code == 200
            pipeline = app.state.pipeline
            loop = pipeline._task.get_loop()  # noqa: SLF001 - 与采集任务同一事件循环
            future = asyncio.run_coroutine_threadsafe(pipeline.poll_once(), loop)
            future.result(timeout=10)

            # poll_once 先广播 snapshot，再广播本轮告警
            second = websocket.receive_json()
            assert second["type"] == "snapshot"
            third = websocket.receive_json()
            assert third["type"] == "alert"
            assert third["data"]["alert_type"] == "link_down"
            assert third["data"]["severity"] == "warning"  # W3 规则：链路故障 warning
            # W3-D 时间基准：实时告警同时带 observed_at（宿主机）与 source_ts（NC 原始）
            assert third["data"]["observed_at"] > 0
            assert "source_ts" in third["data"]
            assert third["data"]["state"] == "active"


def test_auto_driver_falls_back_to_simulator_when_nc_offline(tmp_path):
    """DRIVER=auto：NC 不可用时自动回退仿真驱动，服务照常提供数据。"""
    app = create_app(
        make_settings(
            tmp_path,
            driver="auto",
            nc_base_url="http://127.0.0.1:59999/api/v1",
            nc_timeout=0.5,
        )
    )
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["driver"] == "simulator"
        assert health["counts"]["devices"] > 0
        devices = client.get("/api/devices").json()
        assert devices["source"] == "simulator"
        assert devices["count"] > 0


def test_netcon_unavailable_degrades_gracefully(tmp_path):
    """NC 未启动（端口不通）：服务必须以空数据集正常运行，不崩溃、不卡死。"""
    app = create_app(
        make_settings(
            tmp_path,
            driver="netcon",
            nc_base_url="http://127.0.0.1:59999/api/v1",
            simulate_metrics=True,
        )
    )
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["driver"] == "netcon"
        assert health["driver_status"]["reachable"] is False
        assert health["counts"]["devices"] == 0

        devices = client.get("/api/devices").json()
        assert devices["source"] is None  # 从未成功采集过
        assert devices["count"] == 0
        assert devices["devices"] == []

        topology = client.get("/api/topology").json()
        assert topology["counts"] == {"nodes": 0, "links": 0}
        assert topology["nodes"] == []
        assert topology["links"] == []

        assert client.get("/api/issues").json()["issues"] == []
        assert client.get("/api/alerts").json()["alerts"] == []
        assert client.get("/api/hosts").json()["hosts"] == []
        metrics = client.get("/api/metrics/anything").json()
        assert metrics["count"] == 0

        # 进程不挂：再请求一次健康接口仍正常
        assert client.get("/api/health").json()["status"] == "ok"


def test_alert_lifecycle_state_and_cleanup(tmp_path):
    """注入故障→修复：告警 active → cleared，恢复不新增噪声行。"""
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        pipeline = app.state.pipeline
        loop = pipeline._task.get_loop()  # noqa: SLF001

        def poll_once():
            future = asyncio.run_coroutine_threadsafe(pipeline.poll_once(), loop)
            future.result(timeout=10)

        assert client.post(
            "/api/debug/simulator/link-failure", json={"link_id": "sim-l-core"}
        ).status_code == 200
        poll_once()

        active = client.get("/api/alerts", params={"state": "active"}).json()
        # 仿真驱动注入链路故障会同时产生 link_down 与 NC 原生 HealthIssue 两类告警
        active_types = {alert["alert_type"] for alert in active["alerts"]}
        assert {"link_down", "health_issue"} <= active_types
        incident = active["alerts"][0]
        assert incident["state"] == "active"
        assert incident["observed_at"] > 0  # 宿主机时间
        assert incident["resolved_at"] is None
        assert active["summary"]["active"] == active["count"]

        assert client.post("/api/debug/simulator/reset").status_code == 200
        poll_once()

        active_after = client.get("/api/alerts", params={"state": "active"}).json()
        cleared_after = client.get("/api/alerts", params={"state": "cleared"}).json()
        assert active_after["count"] == 0
        assert cleared_after["count"] == active["count"]
        for cleared in cleared_after["alerts"]:
            assert cleared["state"] == "cleared"
            assert cleared["resolved_at"] is not None
            assert cleared["clear_reason"]
        # 恢复只更新原 incident 行，没有新增 info “已清除”行
        all_rows = client.get("/api/alerts", params={"state": "all"}).json()
        assert all_rows["count"] == active["count"]
        assert all_rows["summary"]["active"] == 0
        assert all_rows["summary"]["cleared"] == active["count"]
        assert all_rows["summary"]["info"] == 0


def test_issues_api_exposes_time_base(tmp_path):
    """W3-D：/api/issues 返回 observed_at（宿主机）与 source_ts（NC 原始时间）字段位。"""
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        issues = client.get("/api/issues").json()
        assert issues["observed_at"] is not None
        assert "count" in issues
        # 仿真模式当前无 issue；字段契约由检测引擎单测覆盖真实取值
        assert issues["issues"] == []
