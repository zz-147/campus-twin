# -*- coding: utf-8 -*-
"""netcon 驱动归一化 + HTTP 行为单测（完全离线，用固定 JSON fixture / MockTransport）。

覆盖：
- 0 设备时 NC 的 ``{"version": "1.0"}`` 空响应 → 空列表
- network-device / topology / host / health-issues / network-health 字段归一化
- 缺 id 记录的稳定兜底 id、缺失 response 包装的容错
- ticket 认证、401 自动重登重试、连接失败优雅降级、单端点失败不影响其它端点
- 连接失败后的退避：不重复打网络（避免服务被卡死/刷屏）
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.collectors.netcon import NetconDriver
from app.config import Settings
from app.models import (
    Device,
    HealthIssue,
    Host,
    NetworkHealth,
    Topology,
    extract_list,
    extract_response,
    is_unreachable,
    parse_speed_mbps,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> Any:
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run_async(coro):
    """不依赖 pytest-asyncio：用 asyncio.run 跑异步用例。"""
    return asyncio.run(coro)


def make_settings(**overrides: Any) -> Settings:
    defaults: Dict[str, Any] = {
        "nc_base_url": "http://127.0.0.1:58000/api/v1",
        "nc_timeout": 1.0,
        "nc_retry_interval": 15.0,
        "nc_max_retry_interval": 120.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


# --------------------------------------------------------------------------- #
# 纯归一化
# --------------------------------------------------------------------------- #
class TestEmptyResponseTolerance:
    def test_extract_list_from_empty_version_only(self):
        self.assert_empty(load_fixture("netcon_empty_response.json"))

    @staticmethod
    def assert_empty(doc: Any) -> None:
        assert extract_response(doc) is None
        assert extract_list(doc) == []

    def test_extract_list_from_various_shapes(self):
        assert extract_list(None) == []
        assert extract_list({}) == []
        assert extract_list({"response": None}) == []
        assert extract_list({"response": []}) == []
        assert extract_list({"response": [{"id": "x"}, "bad"]}) == [{"id": "x"}]
        assert extract_list([{"id": "a"}]) == [{"id": "a"}]

    def test_topology_from_empty_response(self):
        topology = Topology.from_netcon(load_fixture("netcon_empty_response.json"))
        assert topology.nodes == []
        assert topology.links == []
        assert topology.source == "netcon"


class TestDeviceNormalize:
    def test_normalize_fields(self):
        docs = extract_list(load_fixture("netcon_network_device.json"))
        assert len(docs) == 3
        device = Device.from_netcon(docs[0])
        assert device.id == "device-1111"
        assert device.hostname == "CORE-R1"
        assert device.management_ip == "10.10.0.1"
        assert device.mac_address == "00:11:22:33:44:55"
        assert device.serial_number == "FCW1234A0B1"
        assert device.platform_id == "ISR4331"
        assert device.product_id == "ISR4331/K9"
        assert device.device_type == "Cisco ISR 4331"
        assert device.role == "CORE"
        assert device.family == "Routers"
        assert device.software_version == "17.9.1"
        assert device.reachability == "Reachable"
        assert device.collection_status == "Managed"
        assert device.interface_count == 4
        assert device.source == "netcon"  # 真实采集来源标记

    def test_missing_id_gets_stable_fallback_id(self):
        raw = {"hostname": "NO-ID-SW1", "managementIpAddress": "10.10.3.9"}
        first = Device.from_netcon(raw)
        second = Device.from_netcon(raw)
        assert first.id.startswith("auto-")
        assert first.id == second.id  # 稳定可复现，便于去重
        assert first.hostname == "NO-ID-SW1"

    def test_hostname_falls_back_to_ip(self):
        device = Device.from_netcon({"id": "d-1", "managementIpAddress": "10.1.1.1"})
        assert device.hostname == "10.1.1.1"


class TestTopologyNormalize:
    def test_nodes_and_links(self):
        topology = Topology.from_netcon(load_fixture("netcon_topology.json"))
        assert topology.source == "netcon"
        assert [node.id for node in topology.nodes] == ["device-1111", "device-2222", "device-3333"]
        assert topology.nodes[0].x == 120.5
        assert topology.nodes[1].y == 180.25
        assert topology.nodes[0].label == "CORE-R1"
        assert len(topology.links) == 2
        first = topology.links[0]
        assert first.id == "link-1"
        assert first.source == "device-1111"
        assert first.target == "device-2222"
        assert first.start_port_name == "GigabitEthernet0/0/1"
        assert first.link_status == "up"
        assert first.data_source == "netcon"
        # 10Gbps -> 10000 Mbps，供仿真流量分摊使用
        assert first.capacity_mbps() == 10000.0
        # 缺 id 的链路按内容生成稳定 id
        assert topology.links[1].id.startswith("auto-")
        # NC 数值速率单位是 Kbps：100000 Kbps = 100 Mbps
        assert topology.links[1].capacity_mbps() == 100.0
        assert topology.links[1].port_speed_mbps() == 100.0


class TestHostNormalize:
    def test_normalize_host(self):
        docs = extract_list(load_fixture("netcon_host.json"))
        assert len(docs) == 2
        host = Host.from_netcon(docs[0])
        assert host.id == "host-1"
        assert host.hostname == "PC-B1-01"
        assert host.ip == "10.10.20.11"
        assert host.mac == "00:0c:29:aa:00:01"
        assert host.connected_device_id == "device-2222"
        assert host.connected_interface == "FastEthernet0/1"
        assert host.host_type == "Wired"
        assert host.source == "netcon"
        # 缺 id 的记录同样生成稳定 id
        assert Host.from_netcon(docs[1]).id.startswith("auto-")


class TestHealthIssueNormalize:
    def test_normalize_issue(self):
        docs = extract_list(load_fixture("netcon_health_issues.json"))
        assert len(docs) == 2
        issue = HealthIssue.from_netcon(docs[0])
        assert issue.issue_id == "issue-1001"
        assert issue.issue_name == "Device Unreachable"
        assert issue.issue_source == "device-2222"
        assert issue.issue_timestamp == 1761751200000
        assert issue.severity == "critical"
        assert issue.source == "netcon"
        assert issue.status == "active"
        # 缺 issueId：内容哈希生成稳定 id，severity 由文本推断
        fallback = HealthIssue.from_netcon(docs[1])
        assert fallback.issue_id.startswith("auto-")
        assert fallback.severity == "critical"
        assert fallback.issue_id == HealthIssue.from_netcon(docs[1]).issue_id

    def test_empty_issue_response(self):
        assert extract_list(load_fixture("netcon_empty_response.json")) == []


class TestNetworkHealthNormalize:
    def test_flat_string_numbers(self):
        health = NetworkHealth.from_netcon(load_fixture("netcon_network_health.json"))
        assert health.total_devices == 3
        assert health.unreachable_devices == 1
        assert health.num_licensed_routers == 1
        assert health.num_licensed_switches == 2
        assert health.num_application_policies == 3
        assert health.source == "netcon"

    def test_nested_assurance_health(self):
        doc = {
            "response": {
                "timestamp": 1761751200000,
                "networkDevices": {
                    "totalDevices": 6,
                    "totalPercentage": 83.33,
                    "healthyDevices": 5,
                    "unhealthyDevices": 1,
                },
            },
            "version": "1.0",
        }
        health = NetworkHealth.from_netcon(doc)
        assert health.total_devices == 6
        assert health.healthy_devices == 5
        assert health.unhealthy_devices == 1
        assert health.health_percentage == 83.33

    def test_empty_health_response(self):
        health = NetworkHealth.from_netcon(load_fixture("netcon_empty_response.json"))
        assert health.total_devices == 0


class TestSpeedParsing:
    def test_parse_speed(self):
        # W3.1：NC 裸数字单位是 Kbps（实测 100000 -> 100Mbps，1000000 -> 1Gbps）
        assert parse_speed_mbps(100000) == 100.0
        assert parse_speed_mbps("100000") == 100.0
        assert parse_speed_mbps(1000000) == 1000.0
        assert parse_speed_mbps(1000) == 1.0
        assert parse_speed_mbps(1000, default_unit="mbps") == 1000.0
        # 带单位字符串按单位解析
        assert parse_speed_mbps("1Gbps") == 1000.0
        assert parse_speed_mbps("10g") == 10000.0
        assert parse_speed_mbps("100Mbps") == 100.0
        assert parse_speed_mbps("100000Kbps") == 100.0
        assert parse_speed_mbps("100000000bps") == 100.0
        assert parse_speed_mbps(None) is None
        assert parse_speed_mbps("unknown") is None


# --------------------------------------------------------------------------- #
# 基于 PT NC 实际返回形态的回归测试
# （2026-10-29 从本机 NC Real World Access 抓取：interfaceCount 是字符串、
#   platformId/type 为空串、host 用 hostMac、network-health 用 healthyNetworkDevice…）
# --------------------------------------------------------------------------- #
class TestRealNetconSamples:
    def test_string_interface_count_and_empty_strings(self):
        docs = extract_list(load_fixture("netcon_network_device_real_sample.json"))
        assert len(docs) == 3
        device = Device.from_netcon(docs[0])
        assert device.id == "PTT08101YKE-uuid"
        assert device.interface_count == 2  # "2" -> int 2
        assert device.platform_id is None  # "" -> None
        assert device.product_id is None
        assert device.device_type is None
        assert device.serial_number is None
        # 没有 hostname 时用管理 IP 兜底，避免设备列表出现空名字
        assert device.hostname == "192.168.137.3"
        assert device.management_ip == "192.168.137.3"
        assert device.reachability == "Reachable"
        assert device.source == "netcon"

    def test_managed_device_fields(self):
        docs = extract_list(load_fixture("netcon_network_device_real_sample.json"))
        device = Device.from_netcon(docs[1])
        assert device.hostname == "Router"
        assert device.interface_count == 4
        assert device.platform_id == "ISR4300"
        assert device.product_id == "ISR4331"
        assert device.software_version == "15.4"
        assert device.device_type == "Router"
        assert device.up_time == "14 minutes, 37 seconds"
        assert device.reachability == "Reachable"

    def test_unreachable_device_detected(self):
        docs = extract_list(load_fixture("netcon_network_device_real_sample.json"))
        device = Device.from_netcon(docs[2])
        assert is_unreachable(device.reachability) is True
        assert device.collection_status == "Wrong credential"

    def test_real_host_uses_host_mac_field(self):
        docs = extract_list(load_fixture("netcon_host_real_sample.json"))
        host = Host.from_netcon(docs[0])
        assert host.mac == "00E0.A313.7D46"  # PT 实际用 hostMac 字段
        assert host.hostname == "PC0"
        assert host.ip == "1.1.1.2"
        assert host.connected_device_name == "Switch"
        assert host.connected_interface == "FastEthernet0/2"
        assert host.host_type == "Pc"
        assert host.ping_status == "SUCCESS"
        assert host.vlan_id == "1"
        # 空字段容错
        empty = Host.from_netcon(docs[1])
        assert empty.hostname is None
        assert empty.mac is None
        assert empty.ping_status == "FAILURE"

    def test_real_network_health_percentages(self):
        health = NetworkHealth.from_netcon(load_fixture("netcon_network_health_real_sample.json"))
        assert health.total_devices == 7
        assert health.unreachable_devices == 3
        assert health.num_licensed_routers == 1
        assert health.healthy_devices_percentage == 100.0
        assert health.healthy_clients_percentage == 20.0
        assert health.source == "netcon"


# --------------------------------------------------------------------------- #
# NetconDriver（httpx.MockTransport，无需 PT 在线）
# --------------------------------------------------------------------------- #
def _ticket_response(status: int = 201, ticket: str = "T-123") -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "response": {
                "idleTimeout": 900,
                "serviceTicket": ticket,
                "sessionTimeout": 3600,
            },
            "version": "1.0",
        },
    )


def _endpoint_response(path: str) -> Optional[httpx.Response]:
    """按端点返回固定 fixture；未知/空端点返回空响应。"""
    if path.endswith("/network-device"):
        return httpx.Response(200, json=load_fixture("netcon_network_device.json"))
    if path.endswith("/topology/physical-topology"):
        return httpx.Response(200, json=load_fixture("netcon_topology.json"))
    if path.endswith("/host"):
        return httpx.Response(200, json=load_fixture("netcon_host.json"))
    if path.endswith("/assurance/health-issues"):
        return httpx.Response(200, json=load_fixture("netcon_health_issues.json"))
    if path.endswith("/network-health"):
        return httpx.Response(200, json=load_fixture("netcon_network_health.json"))
    return None


class TestNetconDriverHttp:
    def test_full_collect_authenticates_and_normalizes(self):
        calls = {"ticket": 0, "tokens": []}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ticket"):
                calls["ticket"] += 1
                assert request.method == "POST"
                body = json.loads(request.content.decode("utf-8"))
                assert body == {"username": "admin", "password": "cisco"}
                return _ticket_response()
            calls["tokens"].append(request.headers.get("x-auth-token"))
            response = _endpoint_response(request.url.path)
            assert response is not None, "unexpected path: {}".format(request.url.path)
            return response

        async def scenario():
            driver = NetconDriver(make_settings(), transport=httpx.MockTransport(handler))
            try:
                return await driver.collect(), driver
            finally:
                await driver.close()

        result, driver = run_async(scenario())
        assert result.ok is True
        assert result.source == "netcon"
        assert result.error is None
        assert len(result.devices) == 3
        assert len(result.topology.nodes) == 3
        assert len(result.topology.links) == 2
        assert len(result.hosts) == 2
        assert len(result.issues) == 2
        assert result.network_health is not None
        health = result.network_health
        # W3.1：network-health 以设备列表自算为准，字段自洽
        assert health.source_of_truth == "computed_from_devices"
        assert health.total_devices == 3
        assert health.device_total == 3
        assert health.healthy_devices + health.unhealthy_devices == health.total_devices
        assert health.device_healthy + health.device_unhealthy == health.device_total
        assert isinstance(health.health_percentage, float)
        # 归一化 fixture：1 台 Managed+Reachable 健康，2 台不健康（Unreachable/未 Managed）
        assert health.healthy_devices == 1
        assert health.unhealthy_devices == 2
        assert health.health_percentage == 33.33
        # NC 原始字段并存（fixture 是扁平响应：3 台/1 台不可达）
        assert health.nc_total_devices == 3
        assert health.nc_unreachable_devices == 1
        assert calls["ticket"] == 1
        assert calls["tokens"] and all(token == "T-123" for token in calls["tokens"])
        assert driver.login_count == 1
        assert result.metrics == []  # NC 不提供计数器，指标交给 simulator

    def test_empty_dataset_is_not_an_error(self):
        """PT 当前 0 设备：除 ticket/count 外全部返回 {"version":"1.0"}。"""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ticket"):
                return _ticket_response()
            if request.url.path.endswith("/network-device/count"):
                return httpx.Response(200, json={"response": 0, "version": "1.0"})
            return httpx.Response(200, json=load_fixture("netcon_empty_response.json"))

        async def scenario():
            driver = NetconDriver(make_settings(), transport=httpx.MockTransport(handler))
            try:
                return await driver.collect()
            finally:
                await driver.close()

        result = run_async(scenario())
        assert result.ok is True
        assert result.devices == []
        assert result.topology.nodes == []
        assert result.topology.links == []
        assert result.hosts == []
        assert result.issues == []
        assert result.error is None or result.error == ""

    def test_401_triggers_single_relogin_and_retry(self):
        calls = {"ticket": 0, "device": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ticket"):
                calls["ticket"] += 1
                return _ticket_response(ticket="T-{}".format(calls["ticket"]))
            if request.url.path.endswith("/network-device"):
                calls["device"] += 1
                if calls["device"] == 1:
                    return httpx.Response(401, json={"response": {"errorCode": "TOKEN_EXPIRED"}})
                assert request.headers.get("x-auth-token") == "T-2"
                return httpx.Response(200, json=load_fixture("netcon_network_device.json"))
            response = _endpoint_response(request.url.path)
            if response is not None:
                return response
            return httpx.Response(200, json=load_fixture("netcon_empty_response.json"))

        async def scenario():
            driver = NetconDriver(make_settings(), transport=httpx.MockTransport(handler))
            try:
                return await driver.collect()
            finally:
                await driver.close()

        result = run_async(scenario())
        assert result.ok is True
        assert len(result.devices) == 3
        assert calls["ticket"] == 2  # 401 后自动重登一次
        assert calls["device"] == 2

    def test_connect_error_degrades_without_raising(self):
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            raise httpx.ConnectError("connection refused", request=request)

        async def scenario():
            driver = NetconDriver(
                make_settings(nc_timeout=0.2, nc_retry_interval=60.0),
                transport=httpx.MockTransport(handler),
            )
            try:
                first = await driver.collect()
                second = await driver.collect()  # 退避窗口内：不应再打网络
                return first, second, driver
            finally:
                await driver.close()

        first, second, driver = run_async(scenario())
        assert first.ok is False
        assert first.devices == []
        assert first.topology.nodes == []
        assert "失败" in (first.error or "") or "不可用" in (first.error or "")
        assert second.ok is False
        assert attempts["count"] == 1  # 退避生效：5 秒一轮不会反复空转重试
        assert driver.nc_reachable is False
        status = driver.status()
        assert status["reachable"] is False
        assert status["failures"] == 1
        assert status["next_retry_at"] is not None

    def test_ticket_cached_and_proactively_renewed(self):
        """同一轮/跨轮复用 ticket；接近 idleTimeout(900s) 时主动续期。"""
        calls = {"ticket": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ticket"):
                calls["ticket"] += 1
                return _ticket_response(ticket="T-{}".format(calls["ticket"]))
            response = _endpoint_response(request.url.path)
            if response is not None:
                return response
            return httpx.Response(200, json=load_fixture("netcon_empty_response.json"))

        async def scenario():
            driver = NetconDriver(make_settings(), transport=httpx.MockTransport(handler))
            try:
                await driver.collect()
                await driver.collect()
                reused = calls["ticket"]
                driver._ticket_at -= 1000.0  # noqa: SLF001 - 模拟 ticket 接近 idleTimeout
                await driver.collect()
                return reused, calls["ticket"]
            finally:
                await driver.close()

        reused, after_renew = run_async(scenario())
        assert reused == 1  # 第二轮复用，不重复登录
        assert after_renew == 2  # 过期前主动续期

    def test_probe_returns_boolean(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ticket"):
                return _ticket_response()
            return httpx.Response(200, json={"response": 0, "version": "1.0"})

        async def scenario():
            driver = NetconDriver(make_settings(), transport=httpx.MockTransport(handler))
            try:
                return await driver.probe(), driver
            finally:
                await driver.close()

        ok, driver = run_async(scenario())
        assert ok is True
        assert driver.nc_reachable is True

    def test_single_endpoint_failure_does_not_break_others(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ticket"):
                return _ticket_response()
            if request.url.path.endswith("/network-device"):
                return httpx.Response(500, json={"response": {"errorCode": "UNKNOWN_ERROR"}})
            response = _endpoint_response(request.url.path)
            if response is not None:
                return response
            return httpx.Response(200, json=load_fixture("netcon_empty_response.json"))

        async def scenario():
            driver = NetconDriver(make_settings(), transport=httpx.MockTransport(handler))
            try:
                return await driver.collect()
            finally:
                await driver.close()

        result = run_async(scenario())
        # 设备端点失败，但拓扑/主机/健康问题照常返回
        assert result.ok is True
        assert "devices" in result.errors
        assert result.devices == []
        assert len(result.topology.nodes) == 3
        assert result.topology.source == "netcon"
