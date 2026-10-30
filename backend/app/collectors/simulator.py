# -*- coding: utf-8 -*-
"""行为仿真驱动（无 PT 环境开发/演示用，也可为真实拓扑补齐指标）。

为什么需要它
============
Packet Tracer 的 NC REST API **不暴露接口流量计数器**，而"过载/流量重分布"类
检测与前端时序图都需要端口级指标。因此本项目用一个**确定性的行为模型**生成
指标，并强制所有指标点 ``source="simulator"``——真实拓扑/设备/健康问题仍由
netcon 驱动提供，仿真数据绝不冒充真实采集。

行为模型（不是裸随机数）
========================
1. **分时段基础负载**：按一天 24 小时的锚点曲线（凌晨低谷、上午/下午高峰、
   午间回落、夜间衰减）线性插值，再乘周末系数。
2. **拓扑感知的流量分摊**：总流量需求按「链路容量 × 确定性权重」分摊到各活跃
   链路（类似 ECMP/按容量选路），因此核心链路负载天然高于接入链路。
3. **故障后流量重分布**：注入链路/设备故障后，故障链路退出流量分摊，其份额按
   权重转移给剩余活跃链路（活跃链路利用率上升），故障端口指标归零。
4. **确定性扰动**：用链路 id 的 CRC32 相位 + 时间桶的正弦波产生轻微波动，
   同一时刻多次计算结果完全一致，便于测试与回放；代码中不使用任何
   ``random.uniform`` 之类的裸随机。
"""
from __future__ import annotations

import math
import time
import zlib
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from app.collectors.base import CollectResult
from app.config import Settings
from app.models import (
    Device,
    HealthIssue,
    Host,
    Link,
    MetricPoint,
    NetworkHealth,
    Topology,
    TopologyNode,
)

# 一天的负载锚点（小时 -> 基础利用率 0~1）。工作日晚间回落、凌晨最低。
DAILY_LOAD_ANCHORS: Tuple[Tuple[float, float], ...] = (
    (0.0, 0.12),
    (6.0, 0.12),
    (7.0, 0.22),
    (8.5, 0.46),
    (11.5, 0.62),
    (12.5, 0.38),
    (14.0, 0.58),
    (17.0, 0.70),
    (19.0, 0.52),
    (21.5, 0.30),
    (23.0, 0.16),
    (24.0, 0.12),
)

# 仿真校园网：2 台核心路由器 + 2 台汇聚交换机 + 2 台接入交换机。
# hostname 统一带 "SIM-" 前缀，配合 source=simulator，避免与真实数据混淆。
SIM_DEVICES: Tuple[Dict[str, Any], ...] = (
    {
        "id": "sim-core-r1",
        "hostname": "SIM-CORE-R1",
        "management_ip": "10.10.0.1",
        "role": "CORE",
        "device_type": "Cisco ISR 4331",
        "product_id": "ISR4331/K9",
        "family": "Routers",
        "software_version": "17.09.04a",
        "interface_count": 4,
        "x": -2.0,
        "y": 2.0,
    },
    {
        "id": "sim-core-r2",
        "hostname": "SIM-CORE-R2",
        "management_ip": "10.10.0.2",
        "role": "CORE",
        "device_type": "Cisco ISR 4331",
        "product_id": "ISR4331/K9",
        "family": "Routers",
        "software_version": "17.09.04a",
        "interface_count": 4,
        "x": 2.0,
        "y": 2.0,
    },
    {
        "id": "sim-dist-sw1",
        "hostname": "SIM-DIST-SW1",
        "management_ip": "10.10.1.11",
        "role": "DISTRIBUTION",
        "device_type": "Cisco Catalyst 9300",
        "product_id": "C9300-48P",
        "family": "Switches and Hubs",
        "software_version": "17.09.04a",
        "interface_count": 48,
        "x": -2.0,
        "y": 0.0,
    },
    {
        "id": "sim-dist-sw2",
        "hostname": "SIM-DIST-SW2",
        "management_ip": "10.10.1.12",
        "role": "DISTRIBUTION",
        "device_type": "Cisco Catalyst 9300",
        "product_id": "C9300-48P",
        "family": "Switches and Hubs",
        "software_version": "17.09.04a",
        "interface_count": 48,
        "x": 2.0,
        "y": 0.0,
    },
    {
        "id": "sim-acc-b1",
        "hostname": "SIM-ACC-B1-SW1",
        "management_ip": "10.10.2.21",
        "role": "ACCESS",
        "device_type": "Cisco Catalyst 2960",
        "product_id": "WS-C2960-24TC-L",
        "family": "Switches and Hubs",
        "software_version": "15.2(7)E",
        "interface_count": 24,
        "x": -3.0,
        "y": -2.0,
    },
    {
        "id": "sim-acc-b2",
        "hostname": "SIM-ACC-B2-SW1",
        "management_ip": "10.10.2.22",
        "role": "ACCESS",
        "device_type": "Cisco Catalyst 2960",
        "product_id": "WS-C2960-24TC-L",
        "family": "Switches and Hubs",
        "software_version": "15.2(7)E",
        "interface_count": 24,
        "x": 3.0,
        "y": -2.0,
    },
)

SIM_LINKS: Tuple[Dict[str, Any], ...] = (
    {"id": "sim-l-core", "source": "sim-core-r1", "target": "sim-core-r2",
     "start_port_name": "GigabitEthernet0/0/1", "end_port_name": "GigabitEthernet0/0/1",
     "start_port_speed": "10Gbps", "end_port_speed": "10Gbps"},
    {"id": "sim-l-r1-d1", "source": "sim-core-r1", "target": "sim-dist-sw1",
     "start_port_name": "GigabitEthernet0/0/2", "end_port_name": "TenGigabitEthernet1/0/1",
     "start_port_speed": "10Gbps", "end_port_speed": "10Gbps"},
    {"id": "sim-l-r2-d2", "source": "sim-core-r2", "target": "sim-dist-sw2",
     "start_port_name": "GigabitEthernet0/0/2", "end_port_name": "TenGigabitEthernet1/0/1",
     "start_port_speed": "10Gbps", "end_port_speed": "10Gbps"},
    {"id": "sim-l-r1-d2", "source": "sim-core-r1", "target": "sim-dist-sw2",
     "start_port_name": "GigabitEthernet0/0/3", "end_port_name": "TenGigabitEthernet1/0/2",
     "start_port_speed": "10Gbps", "end_port_speed": "10Gbps"},
    {"id": "sim-l-r2-d1", "source": "sim-core-r2", "target": "sim-dist-sw1",
     "start_port_name": "GigabitEthernet0/0/3", "end_port_name": "TenGigabitEthernet1/0/2",
     "start_port_speed": "10Gbps", "end_port_speed": "10Gbps"},
    {"id": "sim-l-d1-acc1", "source": "sim-dist-sw1", "target": "sim-acc-b1",
     "start_port_name": "GigabitEthernet1/0/1", "end_port_name": "GigabitEthernet0/1",
     "start_port_speed": "1Gbps", "end_port_speed": "1Gbps"},
    {"id": "sim-l-d1-acc2", "source": "sim-dist-sw1", "target": "sim-acc-b2",
     "start_port_name": "GigabitEthernet1/0/2", "end_port_name": "GigabitEthernet0/1",
     "start_port_speed": "1Gbps", "end_port_speed": "1Gbps"},
    {"id": "sim-l-d2-acc1", "source": "sim-dist-sw2", "target": "sim-acc-b1",
     "start_port_name": "GigabitEthernet1/0/2", "end_port_name": "GigabitEthernet0/2",
     "start_port_speed": "1Gbps", "end_port_speed": "1Gbps"},
    {"id": "sim-l-d2-acc2", "source": "sim-dist-sw2", "target": "sim-acc-b2",
     "start_port_name": "GigabitEthernet1/0/1", "end_port_name": "GigabitEthernet0/2",
     "start_port_speed": "1Gbps", "end_port_speed": "1Gbps"},
)

SIM_HOSTS: Tuple[Dict[str, Any], ...] = (
    {"id": "sim-host-b1-01", "hostname": "SIM-PC-B1-01", "ip": "10.10.20.11",
     "mac": "00:0c:29:aa:00:01", "connected_device_id": "sim-acc-b1",
     "connected_interface": "FastEthernet0/1", "host_type": "Wired"},
    {"id": "sim-host-b1-02", "hostname": "SIM-PC-B1-02", "ip": "10.10.20.12",
     "mac": "00:0c:29:aa:00:02", "connected_device_id": "sim-acc-b1",
     "connected_interface": "FastEthernet0/2", "host_type": "Wired"},
    {"id": "sim-host-b2-01", "hostname": "SIM-PC-B2-01", "ip": "10.10.20.31",
     "mac": "00:0c:29:aa:00:03", "connected_device_id": "sim-acc-b2",
     "connected_interface": "FastEthernet0/3", "host_type": "Wired"},
    {"id": "sim-host-b2-02", "hostname": "SIM-PC-B2-02", "ip": "10.10.20.32",
     "mac": "00:0c:29:aa:00:04", "connected_device_id": "sim-acc-b2",
     "connected_interface": "FastEthernet0/4", "host_type": "Wired"},
)


class SimulatorDriver:
    """行为模型驱动的仿真采集/指标生成器。"""

    name = "simulator"

    # 校园网链路平均负载系数：基础利用率曲线 × 该系数 ≈ 全网目标负载
    UTILIZATION_SCALE = 0.62
    # 指标点的时间粒度（秒）：同一桶内扰动一致，便于回放/测试
    TIME_BUCKET_SECONDS = 5.0

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings
        self._failed_links: Set[str] = set()
        self._failed_devices: Set[str] = set()
        self._phase_cache: Dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # 故障注入（演示/测试用；pipeline 不会自动调用）
    # ------------------------------------------------------------------ #
    @property
    def failed_links(self) -> Set[str]:
        return set(self._failed_links)

    @property
    def failed_devices(self) -> Set[str]:
        return set(self._failed_devices)

    def inject_link_failure(self, link_id: str) -> bool:
        """把指定链路置为故障；返回是否命中已知链路。"""
        known = {link["id"] for link in SIM_LINKS}
        if link_id not in known:
            return False
        self._failed_links.add(link_id)
        return True

    def clear_link_failure(self, link_id: str) -> bool:
        if link_id in self._failed_links:
            self._failed_links.discard(link_id)
            return True
        return False

    def inject_device_failure(self, device_id: str) -> bool:
        known = {device["id"] for device in SIM_DEVICES}
        if device_id not in known:
            return False
        self._failed_devices.add(device_id)
        return True

    def clear_device_failure(self, device_id: str) -> bool:
        if device_id in self._failed_devices:
            self._failed_devices.discard(device_id)
            return True
        return False

    def clear_failures(self) -> None:
        self._failed_links.clear()
        self._failed_devices.clear()

    # ------------------------------------------------------------------ #
    # 驱动契约
    # ------------------------------------------------------------------ #
    async def probe(self) -> bool:
        """仿真驱动永远可用。"""
        return True

    async def collect(self) -> CollectResult:
        now = time.time()
        devices = self._build_devices(now)
        topology = self._build_topology(now)
        hosts = self._build_hosts(now)
        issues = self._build_issues(now)
        metrics = self.generate_metrics(devices, topology, at=now)
        return CollectResult(
            devices=devices,
            topology=topology,
            hosts=hosts,
            issues=issues,
            metrics=metrics,
            network_health=NetworkHealth.from_devices(devices, source="simulator"),
            source="simulator",
            ok=True,
            collected_at=now,
        )

    async def close(self) -> None:
        # 无外部资源需要释放
        return None

    # ------------------------------------------------------------------ #
    # 指标生成
    # ------------------------------------------------------------------ #
    def generate_metrics(
        self,
        devices: List[Device],
        topology: Topology,
        at: Optional[float] = None,
    ) -> List[MetricPoint]:
        """按当前设备/拓扑 + 行为模型生成一个时间片的指标点。

        每一步都是确定性计算：同一 ``at``、同一注入状态下结果完全一致。
        """
        if not devices:
            return []
        ts = at if at is not None else time.time()
        device_ids = {device.id for device in devices}
        # 只对两端设备都存在（或已知）的链路生成指标——真实拓扑里 NC 的链路端
        # 可能是未在设备列表里的节点，跳过即可，不影响其它链路。
        links = [
            link for link in topology.links
            if link.source in device_ids and link.target in device_ids
        ]

        active_links = [
            link for link in links if self._link_active(link, device_ids)
        ]
        util_by_link: Dict[str, float] = {}
        if active_links:
            # 重要：总需求按“全部链路的标称容量”计算，而不是当前活跃容量。
            # 这样链路故障后需求不缩水，只是改由剩余链路承载 —— 才能体现
            # “故障后流量重分布”（其余链路利用率上升）。
            nominal_capacity = sum(link.capacity_mbps() for link in links)
            weights = {
                link.id: link.capacity_mbps() * self._flow_weight(link.id)
                for link in active_links
            }
            weight_sum = sum(weights.values()) or 1.0
            base_load = self._daily_load(datetime.fromtimestamp(ts))
            demand_mbps = base_load * nominal_capacity * self.UTILIZATION_SCALE
            for link in active_links:
                capacity = max(link.capacity_mbps(), 1.0)
                share = demand_mbps * weights[link.id] / weight_sum
                utilization = share / capacity * self._temporal_jitter(link.id, ts)
                util_by_link[link.id] = min(max(utilization, 0.0), 0.99)

        metrics: List[MetricPoint] = []
        # device_id -> [(链路利用率, 端口名, 对端设备 id)]
        incident: Dict[str, List[Tuple[float, str, str]]] = defaultdict(list)

        for link in links:
            utilization = util_by_link.get(link.id, 0.0)
            failed = not self._link_active(link, device_ids)
            # W3.1 物理约束：端口速率是 bps 的物理上限。两端端口速率可能不同，
            # 分别作为该端口的 speed_bps；NC 数值速率单位是 Kbps，已在
            # parse_speed_mbps 中换算（100000 -> 100Mbps -> 1e8 bps）。
            start_speed_bps = link.port_speed_mbps(start=True) * 1_000_000.0
            end_speed_bps = link.port_speed_mbps(start=False) * 1_000_000.0
            phase = self._phase(link.id)
            # 方向不对称：不同链路上下行比例不同，但同一时刻固定
            out_ratio = 0.5 + 0.25 * math.sin(phase)
            for device_id, interface, is_source, peer_id in (
                (link.source, link.start_port_name or "unknown", True, link.target),
                (link.target, link.end_port_name or "unknown", False, link.source),
            ):
                speed_bps = start_speed_bps if is_source else end_speed_bps
                ratio_out = out_ratio if is_source else 1.0 - out_ratio
                ratio_in = (1.0 - out_ratio) if is_source else out_ratio
                raw_out_bps = utilization * speed_bps * ratio_out
                raw_in_bps = utilization * speed_bps * ratio_in
                out_bps, out_clamped = self._clamp_bps(raw_out_bps, speed_bps)
                in_bps, in_clamped = self._clamp_bps(raw_in_bps, speed_bps)
                # 利用率同样不得超过 100%（防御式，正常模型已在 99% 内）
                utilization_pct = round(min(max(utilization, 0.0), 1.0) * 100.0, 2)
                common_tags = {
                    "link_id": link.id,
                    "peer_device_id": peer_id,
                    "failed": failed,
                    "model": "behavioral-v1",
                    "speed_bps": round(speed_bps, 2),
                }
                speed_field = round(speed_bps, 2)
                metrics.append(
                    MetricPoint(
                        device_id=device_id,
                        metric="if_utilization_pct",
                        value=utilization_pct,
                        unit="%",
                        interface=interface,
                        ts=ts,
                        source="simulator",
                        speed_bps=speed_field,
                        clamped=utilization > 1.0,
                        tags=dict(common_tags),
                    )
                )
                metrics.append(
                    MetricPoint(
                        device_id=device_id,
                        metric="if_in_bps",
                        value=round(in_bps, 2),
                        unit="bps",
                        interface=interface,
                        ts=ts,
                        source="simulator",
                        speed_bps=speed_field,
                        clamped=in_clamped,
                        tags=dict(common_tags),
                    )
                )
                metrics.append(
                    MetricPoint(
                        device_id=device_id,
                        metric="if_out_bps",
                        value=round(out_bps, 2),
                        unit="bps",
                        interface=interface,
                        ts=ts,
                        source="simulator",
                        speed_bps=speed_field,
                        clamped=out_clamped,
                        tags=dict(common_tags),
                    )
                )
                incident[device_id].append((utilization, interface, peer_id))

        # 设备级指标：端口利用率聚合出 CPU/内存/设备负载
        for device in devices:
            ports = incident.get(device.id, [])
            device_utilization = max((item[0] for item in ports), default=0.0)
            if device.id in self._failed_devices:
                device_utilization = 0.0
            phase = self._phase(device.id)
            bucket = ts / self.TIME_BUCKET_SECONDS
            cpu = 8.0 + 62.0 * device_utilization + 3.0 * math.sin(bucket / 9.0 + phase)
            memory = (
                35.0
                + 25.0 * device_utilization
                + 2.0 * math.sin(bucket / 17.0 + 2.0 * phase)
            )
            device_tags = {"failed": device.id in self._failed_devices, "model": "behavioral-v1"}
            for metric_name, value, unit in (
                ("device_utilization_pct", device_utilization * 100.0, "%"),
                ("cpu_pct", cpu, "%"),
                ("mem_pct", memory, "%"),
            ):
                metrics.append(
                    MetricPoint(
                        device_id=device.id,
                        metric=metric_name,
                        value=round(min(max(value, 0.0), 100.0), 2),
                        unit=unit,
                        ts=ts,
                        source="simulator",
                        tags=dict(device_tags),
                    )
                )
        return metrics

    # ------------------------------------------------------------------ #
    # 行为模型内部实现
    # ------------------------------------------------------------------ #
    def _build_devices(self, now: float) -> List[Device]:
        devices: List[Device] = []
        for spec in SIM_DEVICES:
            failed = spec["id"] in self._failed_devices
            devices.append(
                Device(
                    id=spec["id"],
                    hostname=spec["hostname"],
                    management_ip=spec["management_ip"],
                    mac_address=self._fake_mac(spec["id"]),
                    serial_number=self._fake_serial(spec["id"]),
                    platform_id=spec["device_type"],
                    product_id=spec["product_id"],
                    device_type=spec["device_type"],
                    role=spec["role"],
                    family=spec["family"],
                    software_version=spec["software_version"],
                    up_time="12 days, 04:21:09" if not failed else "00:00:07",
                    reachability="UNREACHABLE" if failed else "REACHABLE",
                    collection_status="Unreachable" if failed else "Managed",
                    interface_count=spec["interface_count"],
                    last_updated=now,
                    source="simulator",
                    last_seen=now,
                )
            )
        return devices

    def _build_topology(self, now: float) -> Topology:
        nodes = [
            TopologyNode(
                id=spec["id"],
                x=spec.get("x"),
                y=spec.get("y"),
                label=spec["hostname"],
                role=spec["role"],
                source="simulator",
            )
            for spec in SIM_DEVICES
        ]
        links: List[Link] = []
        for spec in SIM_LINKS:
            active = spec["id"] not in self._failed_links and not (
                spec["source"] in self._failed_devices
                or spec["target"] in self._failed_devices
            )
            links.append(
                Link(
                    id=spec["id"],
                    source=spec["source"],
                    target=spec["target"],
                    start_port_name=spec["start_port_name"],
                    end_port_name=spec["end_port_name"],
                    start_port_speed=spec["start_port_speed"],
                    end_port_speed=spec["end_port_speed"],
                    link_status="up" if active else "down",
                    data_source="simulator",
                )
            )
        return Topology(
            nodes=nodes, links=links, captured_at=now, source="simulator"
        )

    def _build_hosts(self, now: float) -> List[Host]:
        return [
            Host(
                id=spec["id"],
                hostname=spec["hostname"],
                ip=spec["ip"],
                mac=spec["mac"],
                connected_device_id=spec["connected_device_id"],
                connected_interface=spec["connected_interface"],
                host_type=spec["host_type"],
                last_updated=now,
                source="simulator",
                last_seen=now,
            )
            for spec in SIM_HOSTS
        ]

    def _build_issues(self, now: float) -> List[HealthIssue]:
        """仿真模式下的健康问题只由注入的故障产生（不凭空造告警）。"""
        issues: List[HealthIssue] = []
        for link_id in sorted(self._failed_links):
            spec = next(link for link in SIM_LINKS if link["id"] == link_id)
            issues.append(
                HealthIssue(
                    issue_id="sim-issue-{}".format(link_id),
                    issue_name="Link Down",
                    issue_description="仿真链路 {} <-> {} 中断（注入故障）".format(
                        spec["source"], spec["target"]
                    ),
                    issue_timestamp=now,
                    issue_source=spec["source"],
                    severity="critical",
                    status="active",
                    source="simulator",
                    first_seen=now,
                    last_seen=now,
                )
            )
        for device_id in sorted(self._failed_devices):
            spec = next(device for device in SIM_DEVICES if device["id"] == device_id)
            issues.append(
                HealthIssue(
                    issue_id="sim-issue-device-{}".format(device_id),
                    issue_name="Device Unreachable",
                    issue_description="仿真设备 {} 不可达（注入故障）".format(
                        spec["hostname"]
                    ),
                    issue_timestamp=now,
                    issue_source=device_id,
                    severity="critical",
                    status="active",
                    source="simulator",
                    first_seen=now,
                    last_seen=now,
                )
            )
        return issues

    @staticmethod
    def _clamp_bps(raw_bps: float, speed_bps: Optional[float]) -> Tuple[float, bool]:
        """把仿真流量钳制到端口物理速率上限内，返回 ``(value, clamped)``。

        物理意义：任何接口的瞬时 bps 不可能超过其端口速率；行为模型推导出的
        理论值若越界（例如速率单位解析错误/极端负载），在这里兜底钳制并标记
        ``clamped=True``，保证 ``/api/metrics`` 的数据物理自洽。
        """
        value = max(float(raw_bps), 0.0)
        if not speed_bps or speed_bps <= 0:
            return value, False
        if value > speed_bps:
            return float(speed_bps), True
        return value, False

    @staticmethod
    def _fake_mac(key: str) -> str:
        """确定性伪 MAC（仅仿真设备使用，前缀 02 表示本地管理地址）。"""
        digest = zlib.crc32(key.encode("utf-8")) & 0xFFFFFF
        return "02:00:{:02x}:{:02x}:{:02x}:{:02x}".format(
            (digest >> 16) & 0xFF,
            (digest >> 8) & 0xFF,
            digest & 0xFF,
            (digest >> 4) & 0xFF,
        )

    @staticmethod
    def _fake_serial(key: str) -> str:
        digest = zlib.crc32(key.encode("utf-8")) & 0xFFFFFF
        return "SIM{:07d}".format(digest)

    @staticmethod
    def _interpolate_anchors(hour: float) -> float:
        """在 DAILY_LOAD_ANCHORS 上线性插值。"""
        for (h0, v0), (h1, v1) in zip(DAILY_LOAD_ANCHORS, DAILY_LOAD_ANCHORS[1:]):
            if h0 <= hour <= h1:
                ratio = (hour - h0) / (h1 - h0) if h1 > h0 else 0.0
                return v0 + (v1 - v0) * ratio
        return DAILY_LOAD_ANCHORS[-1][1]

    def _daily_load(self, at: datetime) -> float:
        """分时段基础负载：工作日曲线 + 周末衰减。"""
        hour = at.hour + at.minute / 60.0 + at.second / 3600.0
        load = self._interpolate_anchors(hour)
        weekday = at.weekday()
        if weekday == 5:  # Saturday
            load *= 0.62
        elif weekday == 6:  # Sunday
            load *= 0.48
        return min(max(load, 0.03), 0.95)

    def _phase(self, key: str) -> float:
        cached = self._phase_cache.get(key)
        if cached is None:
            cached = (zlib.crc32(key.encode("utf-8")) / 2 ** 32) * 2.0 * math.pi
            self._phase_cache[key] = cached
        return cached

    def _flow_weight(self, link_id: str) -> float:
        """确定性链路权重：0.8~1.2，模拟不同链路承担的流量份额差异。"""
        return 1.0 + 0.2 * math.sin(self._phase(link_id))

    def _temporal_jitter(self, link_id: str, ts: float) -> float:
        """确定性时间扰动（±4.5%），不使用随机数。"""
        bucket = ts / self.TIME_BUCKET_SECONDS
        phase = self._phase(link_id)
        return (
            1.0
            + 0.03 * math.sin(2.0 * math.pi * bucket / 24.0 + phase)
            + 0.015 * math.sin(2.0 * math.pi * bucket / 7.0 + 2.0 * phase)
        )

    def _link_active(self, link: Link, device_ids: Set[str]) -> bool:
        if link.id in self._failed_links:
            return False
        if link.source in self._failed_devices or link.target in self._failed_devices:
            return False
        return link.source in device_ids and link.target in device_ids

    # ------------------------------------------------------------------ #
    # 观测信息
    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        return {
            "driver": self.name,
            "failed_links": sorted(self._failed_links),
            "failed_devices": sorted(self._failed_devices),
        }


# 兜底说明：NC 给出的端口速率无论是 "10Gbps" 还是 1000，LegacyLink.capacity_mbps()
# 都会统一解析成 Mbps 后再参与流量分摊。
__all__ = ["SimulatorDriver", "SIM_DEVICES", "SIM_LINKS"]
