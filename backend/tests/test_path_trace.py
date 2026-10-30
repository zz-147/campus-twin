# -*- coding: utf-8 -*-
"""W10 路径追踪测试（NC flow-analysis 归一化 / 缓存 / 错误降级 / API 契约）。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
from fastapi.testclient import TestClient

from app.collectors.netcon import NetconDriver, NetconEndpointError
from app.config import Settings
from app.main import create_app
from app.path_trace import PathTraceService, normalize_flow_analysis

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> Any:
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run_async(coro):
    return asyncio.run(coro)


def make_snapshot() -> Dict[str, Any]:
    """与后端 pipeline.snapshot_data() 同构的最小快照（真实名称/IP）。"""
    return {
        "devices": [
            {
                "id": "CAT10109K2T-uuid",
                "hostname": "Core-3650",
                "management_ip": "192.168.20.1",
                "reachability": "Reachable",
            },
            {
                "id": "CAT10101HTH-uuid",
                "hostname": "SW-JX-2960",
                "management_ip": "192.168.10.11",
                "reachability": "Reachable",
            },
        ],
        "hosts": [
            {
                "id": "PTT0810C5ONuuid",
                "hostname": "PC-JX1",
                "ip": "192.168.20.10",
                "connected_device_id": "CAT10101HTH-uuid",
                "connected_interface": "FastEthernet0/1",
            },
            {
                "id": "PTT08100K33uuid",
                "hostname": "Server-1",
                "ip": "192.168.50.10",
                "connected_device_id": "CAT10109K2T-uuid",
                "connected_interface": "GigabitEthernet1/0/5",
            },
        ],
        "topology": {
            "nodes": [],
            "links": [
                {
                    "id": "link-core-jx",
                    "source": "CAT10109K2T-uuid",
                    "target": "CAT10101HTH-uuid",
                    "start_port_name": "GigabitEthernet1/0/1",
                    "end_port_name": "FastEthernet0/24",
                }
            ],
        },
    }


class FakeDriver:
    """可控 flow_analysis 驱动（用于服务层单测，无需 NC 在线）。"""

    name = "fake-netcon"

    def __init__(self, response: Dict[str, Any], delay: float = 0.0) -> None:
        self.response = response
        self.delay = delay
        self.calls: List[Any] = []

    async def flow_analysis(self, source_ip: str, dest_ip: str, **kwargs: Any):
        self.calls.append((source_ip, dest_ip))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.response

    async def close(self) -> None:
        return None


class UnsupportedDriver:
    name = "simulator"

    async def collect(self):
        raise NotImplementedError

    async def close(self) -> None:
        return None


class TestNormalizeFlowAnalysis:
    def test_completed_trace_normalizes_hops_and_links(self):
        raw = load_fixture("netcon_flow_analysis_completed.json")
        result = normalize_flow_analysis(raw, "192.168.20.10", "192.168.50.10", make_snapshot())
        assert result["trace_status"] == "complete"
        hops = result["hops"]
        # W10.1：NC 只返回 PC→Core→Server，拓扑推断补回教学楼接入交换机
        assert [hop["device_name"] for hop in hops] == [
            "PC-JX1",
            "SW-JX-2960",
            "Core-3650",
            "Server-1",
        ]
        assert [hop["role"] for hop in hops] == [
            "source",
            "transit",
            "transit",
            "destination",
        ]
        assert hops[0]["index"] == 0 and hops[-1]["index"] == 3
        assert result["synthetic_hop_count"] == 1
        # 补全 hop 标记 synthetic + topology_inference，端口能拿到就填
        inferred = hops[1]
        assert inferred["synthetic"] is True
        assert inferred["source"] == "topology_inference"
        assert inferred["inferred"] is True
        assert inferred["ingress_port"] == "UNKNOWN"  # 主机侧端口 NC 不提供
        assert inferred["egress_port"] == "FastEthernet0/24"  # 上联 Core 的端口
        # NC 原始 hop 不被篡改，端口来自 ingressInterface/egressInterface.physicalInterface.name
        assert hops[2]["synthetic"] is False
        assert hops[2]["ingress_port"] == "Vlan20"
        assert hops[2]["egress_port"] == "GigabitEthernet1/0/5"
        assert hops[3]["ingress_port"] == "UNKNOWN"
        # hops 顺序修正后 links 同步补齐：主机接入段 + 真实拓扑段 + 主机接入段
        links = result["links"]
        assert len(links) == 3
        assert links[0]["source_name"] == "PC-JX1"
        assert links[0]["target_name"] == "SW-JX-2960"
        assert links[0]["synthetic"] is True
        assert links[0]["reason"] == "host_attachment"
        assert links[1]["source_name"] == "SW-JX-2960"
        assert links[1]["target_name"] == "Core-3650"
        assert links[1]["link_id"] == "link-core-jx"
        assert links[1]["synthetic"] is False
        assert links[1]["reason"] == "topology"
        assert links[1]["source_port"] == "FastEthernet0/24"
        assert links[2]["target_name"] == "Server-1"
        assert links[2]["synthetic"] is True
        # warning 明确说明补全了哪台设备
        assert any(
            "SW-JX-2960" in warning and "拓扑推断补全" in warning
            for warning in result["warnings"]
        )
        assert result["raw_element_count"] == 3

    def test_missing_endpoint_hops_are_synthesized_from_snapshot(self):
        raw = {
            "response": {
                "request": {"status": "COMPLETED"},
                "networkElementsInfo": [
                    {
                        "id": "CAT10109K2T-uuid",
                        "name": "Core-3650",
                        "ip": "192.168.20.1",
                        "ingressInterface": {"physicalInterface": {"name": "Vlan20"}},
                        "egressInterface": {"physicalInterface": {"name": "Gi1/0/5"}},
                    }
                ],
            }
        }
        result = normalize_flow_analysis(raw, "192.168.20.10", "192.168.50.10", make_snapshot())
        names = [hop["device_name"] for hop in result["hops"]]
        assert names[0] == "PC-JX1"  # 从 snapshot host 合成源端
        assert names[-1] == "Server-1"  # 从 snapshot host 合成目的端
        assert "SW-JX-2960" in names  # 拓扑推断补回中间接入交换机
        assert result["trace_status"] == "complete"
        assert result["raw_element_count"] == 1
        assert result["synthetic_hop_count"] == 1

    def test_empty_elements_fail(self):
        result = normalize_flow_analysis(
            {"response": {"request": {"status": "COMPLETED"}}},
            "192.168.20.10",
            "192.168.50.10",
            make_snapshot(),
        )
        assert result["trace_status"] == "failed"

    def test_direct_topology_hops_are_not_duplicated(self):
        """若 NC 已返回完整 hop（含 SW），拓扑补全不应重复插入。"""
        raw = {
            "response": {
                "request": {"status": "COMPLETED"},
                "networkElementsInfo": [
                    {"id": "PTT0810C5ONuuid", "name": "PC-JX1", "ip": "192.168.20.10"},
                    {
                        "id": "CAT10101HTH-uuid",
                        "name": "SW-JX-2960",
                        "ip": "192.168.10.11",
                        "ingressInterface": {"physicalInterface": {"name": "FastEthernet0/1"}},
                        "egressInterface": {"physicalInterface": {"name": "FastEthernet0/24"}},
                    },
                    {"id": "CAT10109K2T-uuid", "name": "Core-3650", "ip": "192.168.20.1"},
                    {"id": "PTT08100K33uuid", "name": "Server-1", "ip": "192.168.50.10"},
                ],
            }
        }
        result = normalize_flow_analysis(raw, "192.168.20.10", "192.168.50.10", make_snapshot())
        assert [hop["device_name"] for hop in result["hops"]] == [
            "PC-JX1",
            "SW-JX-2960",
            "Core-3650",
            "Server-1",
        ]
        assert result["synthetic_hop_count"] == 0
        assert all(hop["synthetic"] is False for hop in result["hops"])

    def test_unreachable_topology_keeps_original_hops(self):
        """拓扑上 A、B 之间不可达时保持 NC 原始 hop，并给 warning。"""
        snapshot = {
            "devices": [
                {
                    "id": "CAT10109K2T-uuid",
                    "hostname": "Core-3650",
                    "management_ip": "192.168.20.1",
                }
            ],
            "hosts": [
                {
                    "id": "PTT0810C5ONuuid",
                    "hostname": "PC-JX1",
                    "ip": "192.168.20.10",
                    "connected_device_name": "SW-UNKNOWN-2960",
                    "connected_interface": "FastEthernet0/1",
                },
                {
                    "id": "PTT08100K33uuid",
                    "hostname": "Server-1",
                    "ip": "192.168.50.10",
                    "connected_device_name": "Core-3650",
                    "connected_interface": "GigabitEthernet1/0/5",
                },
            ],
            "topology": {"nodes": [], "links": []},  # 没有任何可推断链路
        }
        raw = load_fixture("netcon_flow_analysis_completed.json")
        result = normalize_flow_analysis(raw, "192.168.20.10", "192.168.50.10", snapshot)
        # PC-JX1 / Core / Server-1 之间没有拓扑边，无法补全 → 保持原样
        names = [hop["device_name"] for hop in result["hops"]]
        assert names == ["PC-JX1", "Core-3650", "Server-1"]
        assert result["synthetic_hop_count"] == 0
        assert any("拓扑不可达" in warning for warning in result["warnings"])

    def test_unknown_ips_still_return_partial(self):
        result = normalize_flow_analysis(
            load_fixture("netcon_flow_analysis_completed.json"),
            "10.9.9.9",
            "10.9.9.10",
            {},
        )
        # 原始 NC hop 仍在，但首尾不是指定 IP → partial
        assert result["trace_status"] == "partial"
        assert len(result["hops"]) >= 2


class TestPathTraceService:
    def test_success_and_two_minute_cache(self):
        driver = FakeDriver(
            {
                "ok": True,
                "flow_analysis_id": "FLOW-1",
                "raw": load_fixture("netcon_flow_analysis_completed.json"),
                "error": None,
            }
        )
        service = PathTraceService(driver, Settings(path_trace_cache_seconds=120))
        first = run_async(service.trace("192.168.20.10", "192.168.50.10", make_snapshot()))
        assert first["ok"] is True
        assert first["cached"] is False
        assert first["trace_status"] == "complete"
        assert first["synthetic_hop_count"] == 1
        assert first["flow_analysis_id"] == "FLOW-1"
        assert first["elapsed_ms"] >= 0
        second = run_async(service.trace("192.168.20.10", "192.168.50.10", make_snapshot()))
        assert second["cached"] is True
        assert len(driver.calls) == 1  # 命中缓存，不重复打 NC
        assert service.cache_size == 1

    def test_unsupported_driver_returns_chinese_error(self):
        service = PathTraceService(UnsupportedDriver(), Settings())
        result = run_async(service.trace("192.168.20.10", "192.168.50.10", {}))
        assert result["ok"] is False
        assert result["trace_status"] == "failed"
        assert "不支持路径追踪" in result["error"]

    def test_nc_error_is_passed_through(self):
        driver = FakeDriver(
            {"ok": False, "flow_analysis_id": None, "raw": None, "error": "NC 路径追踪超时（>30s）"}
        )
        service = PathTraceService(driver, Settings())
        result = run_async(service.trace("192.168.20.10", "192.168.50.10", make_snapshot()))
        assert result["ok"] is False
        assert "超时" in result["error"]

    def test_driver_exception_degrades(self):
        class RaisingDriver:
            async def flow_analysis(self, source_ip, dest_ip, **kwargs):
                raise NetconEndpointError("/flow-analysis -> HTTP 404")

        service = PathTraceService(RaisingDriver(), Settings())
        result = run_async(service.trace("192.168.20.10", "192.168.50.10", make_snapshot()))
        assert result["ok"] is False
        assert result["error"]

    def test_same_ip_rejected(self):
        service = PathTraceService(FakeDriver({}), Settings())
        result = run_async(service.trace("1.1.1.1", "1.1.1.1", {}))
        assert result["ok"] is False
        assert "相同" in result["error"]


class TestNetconFlowAnalysis:
    def _make_driver(self, handler):
        settings = Settings(
            nc_base_url="http://127.0.0.1:58000/api/v1",
            nc_timeout=1.0,
            path_trace_timeout=2.0,
            path_trace_poll_interval=0.01,
        )
        return NetconDriver(settings, transport=httpx.MockTransport(handler))

    def test_post_then_poll_returns_completed(self):
        calls = {"ticket": 0, "post": 0, "get": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/ticket"):
                calls["ticket"] += 1
                return httpx.Response(
                    201,
                    json={
                        "response": {"serviceTicket": "T-1", "idleTimeout": 900, "sessionTimeout": 3600},
                        "version": "1.0",
                    },
                )
            if path.endswith("/flow-analysis"):
                calls["post"] += 1
                assert request.method == "POST"
                body = json.loads(request.content.decode("utf-8"))
                assert body == {"sourceIP": "192.168.20.10", "destIP": "192.168.50.10"}
                return httpx.Response(
                    200,
                    json={
                        "response": {"flowAnalysisId": "FLOW-1", "url": "/flow-analysis/FLOW-1"},
                        "version": "1",
                    },
                )
            if path.endswith("/flow-analysis/FLOW-1"):
                calls["get"] += 1
                return httpx.Response(200, json=load_fixture("netcon_flow_analysis_completed.json"))
            raise AssertionError("unexpected path " + path)

        async def scenario():
            driver = self._make_driver(handler)
            try:
                return await driver.flow_analysis("192.168.20.10", "192.168.50.10")
            finally:
                await driver.close()

        result = run_async(scenario())
        assert result["ok"] is True
        assert result["flow_analysis_id"] == "FLOW-1"
        assert calls["post"] == 1
        assert calls["get"] >= 1

    def test_timeout_returns_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ticket"):
                return httpx.Response(
                    201,
                    json={"response": {"serviceTicket": "T-1"}, "version": "1.0"},
                )
            if request.url.path.endswith("/flow-analysis"):
                return httpx.Response(200, json={"response": {"flowAnalysisId": "FLOW-SLOW"}})
            return httpx.Response(
                200, json=load_fixture("netcon_flow_analysis_in_progress.json")
            )

        async def scenario():
            driver = self._make_driver(handler)
            try:
                return await driver.flow_analysis(
                    "192.168.20.10", "192.168.50.10", timeout=0.15, poll_interval=0.01
                )
            finally:
                await driver.close()

        result = run_async(scenario())
        assert result["ok"] is False
        assert "超时" in result["error"]

    def test_unsupported_endpoint_returns_chinese_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ticket"):
                return httpx.Response(
                    201, json={"response": {"serviceTicket": "T-1"}, "version": "1.0"}
                )
            return httpx.Response(404, json={"response": {"detail": "not found"}})

        async def scenario():
            driver = self._make_driver(handler)
            try:
                return await driver.flow_analysis("192.168.20.10", "192.168.50.10")
            finally:
                await driver.close()

        result = run_async(scenario())
        assert result["ok"] is False
        assert "不支持路径追踪" in result["error"]


class TestPathTraceApi:
    def make_settings(self, tmp_path):
        return Settings(
            db_path=str(tmp_path / "path_trace.db"),
            driver="simulator",
            poll_interval=3600.0,
            simulate_metrics=False,
            log_level="WARNING",
            nc_timeout=0.5,
        )

    def test_simulator_driver_returns_graceful_error(self, tmp_path):
        app = create_app(self.make_settings(tmp_path))
        with TestClient(app) as client:
            response = client.post(
                "/api/path-trace",
                json={"source_ip": "192.168.20.10", "dest_ip": "192.168.50.10"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is False
            assert body["trace_status"] == "failed"
            assert "不支持路径追踪" in body["error"]

    def test_endpoint_returns_normalized_trace_with_fake_driver(self, tmp_path):
        app = create_app(self.make_settings(tmp_path))
        with TestClient(app) as client:
            fake = FakeDriver(
                {
                    "ok": True,
                    "flow_analysis_id": "FLOW-API",
                    "raw": load_fixture("netcon_flow_analysis_completed.json"),
                    "error": None,
                }
            )
            app.state.path_trace = PathTraceService(fake, app.state.settings)
            # 用真实 NC 形态快照（含 hosts 接入关系 + topology links），验证补全契约
            app.state.pipeline.snapshot_data = lambda: make_snapshot()
            response = client.post(
                "/api/path-trace",
                json={"source_ip": "192.168.20.10", "dest_ip": "192.168.50.10"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is True
            assert body["trace_status"] == "complete"
            assert [hop["device_name"] for hop in body["hops"]] == [
                "PC-JX1",
                "SW-JX-2960",
                "Core-3650",
                "Server-1",
            ]
            assert body["synthetic_hop_count"] == 1
            assert body["elapsed_ms"] >= 0
            # 请求体校验
            invalid = client.post("/api/path-trace", json={"source_ip": ""})
            assert invalid.status_code == 422
