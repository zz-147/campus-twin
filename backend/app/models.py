# -*- coding: utf-8 -*-
"""统一数据模型。

设计意图
========
1. **所有数据源输出同一套模型**：netcon 驱动把 Cisco DNA Center 风格的
   ``{"response": ...}`` 归一化成这里的 snake_case 模型；simulator 驱动直接
   构造同样的模型。上层 pipeline / REST / WebSocket 只认这套模型。
2. **来源必须显式标注**：每个模型都有 ``source`` 字段（``netcon`` / ``simulator``）。
   PT 不暴露接口计数器，因此指标统一由行为仿真生成并强制 ``source="simulator"``，
   绝不伪装成真实采集；真实设备/拓扑/健康问题则是 ``source="netcon"``。
   例外：``Link`` 的 ``source`` / ``target`` 沿用 NC 的链路语义（两端设备 id），
   数据来源标在 ``Link.data_source`` 上。
3. **对残缺数据零信任**：NC 在 0 设备时返回 ``{"version": "1.0"}``（没有 response
   字段），字段可能缺失/类型漂移，本模块的 ``from_*`` 全部做防御式提取，
   缺失 id 时用内容哈希生成稳定 id，保证空数据集和脏数据都不会让服务崩溃。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# 数据来源。后续若加 replay / config 驱动，在这里扩展即可。
SourceType = Literal["netcon", "simulator", "snmp"]

NumberLike = Union[int, float, str, None]
PortSpeed = Union[int, float, str, None]


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def stable_id(*parts: Any) -> str:
    """由内容生成稳定短 id（用于源数据缺 id 的场景，可复现、不依赖随机数）。"""
    raw = "|".join("" if p is None else str(p) for p in parts)
    return "auto-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _first(raw: Dict[str, Any], *keys: str) -> Any:
    """按顺序取第一个存在且非空的字段（兼容 NC 不同版本/不同端点的命名差异）。"""
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


def _text(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_response(doc: Any) -> Any:
    """从 NC 的 ``{"response": ..., "version": "1.0"}`` 包装里取载荷。

    三种形态：
    - 标准包装 ``{"response": ..., "version": "1.0"}`` -> 取 ``response``；
    - 空数据 ``{"version": "1.0"}``（没有 response 字段）-> None；
    - 少数端点直接返回扁平 JSON（如 ``/network-health`` 没有 version 包装）-> 原样返回。

    调用方统一把 None 当作“空”。
    """
    if doc is None:
        return None
    if isinstance(doc, dict):
        if "response" in doc:
            return doc["response"]
        if "version" in doc:
            # NC 的空响应包装：只有版本号，没有数据载荷。
            return None
        return doc
    return doc


def extract_list(doc: Any) -> List[Dict[str, Any]]:
    """把端点响应归一化成 dict 列表；空响应返回 ``[]``。"""
    payload = extract_response(doc)
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


# NC/Cisco DNA Center 的端口速率数值单位是 Kbps（实测 PT NC：
#   startPortSpeed=100000   -> 100 Mbps
#   startPortSpeed=1000000  -> 1 Gbps
# 所以裸数字必须按 Kbps 解释；带单位字符串（Gbps/Mbps/Kbps/bps）按单位解释。
_SPEED_UNIT_TO_MBPS = (
    ("gbps", 1000.0),
    ("mbps", 1.0),
    ("kbps", 0.001),
    ("bps", 0.000001),
    ("g", 1000.0),
    ("m", 1.0),
    ("k", 0.001),
    ("b", 0.000001),
)
# 裸数字默认单位（NC 实际口径 = Kbps）；测试/仿真可显式传 mbps
DEFAULT_SPEED_UNIT = "kbps"


def parse_speed_mbps(
    speed: PortSpeed, default_unit: str = DEFAULT_SPEED_UNIT
) -> Optional[float]:
    """把 NC 各种端口速率写法解析成 Mbps 数值。

    - 裸数字（``100000``）：按 ``default_unit`` 解释，默认 **Kbps**（NC 实际口径）；
    - 带单位字符串：``"1Gbps"`` / ``"100Mbps"`` / ``"100000Kbps"`` /
      ``"100000000bps"`` 按单位解析；
    - 兼容简写：``"10g"`` / ``"100m"`` / ``"100k"`` / ``"1b"``。

    解析不了返回 None（调用方使用默认容量兜底，不影响服务运行）。
    """
    if speed is None or speed == "":
        return None
    if isinstance(speed, (int, float)):
        value = max(float(speed), 0.0)
        if value <= 0:
            return None
        if default_unit == "mbps":
            return value
        if default_unit == "bps":
            return value / 1_000_000.0
        return value / 1000.0  # 默认 Kbps -> Mbps
    text = str(speed).strip().lower()
    for suffix, factor in _SPEED_UNIT_TO_MBPS:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            multiplier = factor
            break
    else:
        # 没有单位：与裸数字一致，按 default_unit
        if default_unit == "mbps":
            multiplier = 1.0
        elif default_unit == "bps":
            multiplier = 0.000001
        else:
            multiplier = 0.001
    try:
        value = float(text) * multiplier
    except ValueError:
        return None
    return value if value > 0 else None


def is_unreachable(status: Optional[str]) -> bool:
    """判断 NC 的 reachabilityStatus 是否表示不可达。"""
    if not status:
        return False
    lowered = str(status).strip().lower()
    return lowered in ("unreachable", "down", "offline", "not reachable", "false")


def is_ping_failure(ping_status: Optional[str]) -> bool:
    """判断 NC /host 的 pingStatus 是否表示主机不可达。

    NC 实测值：``SUCCESS`` / ``FAILURE``；这里兼容 FAIL/FAILED/DOWN 等写法。
    未提供状态（None）时返回 False（不臆测故障）。
    """
    if not ping_status:
        return False
    lowered = str(ping_status).strip().lower()
    if lowered in ("success", "up", "ok", "true", "reachable"):
        return False
    return (
        ("fail" in lowered)
        or ("down" in lowered)
        or lowered in ("false", "unreachable", "offline")
    )


def _issue_severity(name: Optional[str], description: Optional[str]) -> str:
    """按问题文本粗分严重级别（MVP 阶段没有 NC 的 severity 字段可用）。"""
    text = "{} {}".format(name or "", description or "").lower()
    critical_keywords = (
        "unreachable",
        "down",
        "failed",
        "failure",
        "critical",
        "outage",
        "unresponsive",
        "不可达",
        "中断",
        "宕",
        "故障",
    )
    return "critical" if any(word in text for word in critical_keywords) else "warning"


# --------------------------------------------------------------------------- #
# 设备
# --------------------------------------------------------------------------- #
class Device(BaseModel):
    """统一设备模型（交换机/路由器/AP 等受管网络设备）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    hostname: str
    management_ip: Optional[str] = None
    mac_address: Optional[str] = None
    serial_number: Optional[str] = None
    platform_id: Optional[str] = None
    product_id: Optional[str] = None
    # NC 的 type / role / family（如 "Cisco 2960" / "ACCESS" / "Switches and Hubs"）
    device_type: Optional[str] = None
    role: Optional[str] = None
    family: Optional[str] = None
    software_version: Optional[str] = None
    up_time: Optional[str] = None
    reachability: Optional[str] = None
    collection_status: Optional[str] = None
    interface_count: Optional[int] = None
    last_updated: Optional[Union[str, int, float]] = None
    source: SourceType = "netcon"
    last_seen: float = Field(default_factory=time.time)

    @classmethod
    def from_netcon(cls, raw: Dict[str, Any], seen_at: Optional[float] = None) -> "Device":
        """把 ``GET /network-device`` 的单条记录归一化成统一模型。"""
        if not isinstance(raw, dict):
            raise TypeError("network-device 记录必须是 dict")
        hostname = _text(_first(raw, "hostname", "hostName", "name")) or ""
        ip = _text(_first(raw, "managementIpAddress", "managementIp", "ipAddress"))
        device_id = _text(_first(raw, "id", "instanceUuid", "deviceId"))
        if not device_id:
            device_id = stable_id(hostname, ip, _first(raw, "macAddress", "serialNumber"))
        if not hostname:
            hostname = ip or device_id
        return cls(
            id=device_id,
            hostname=hostname,
            management_ip=ip,
            mac_address=_text(_first(raw, "macAddress", "mac")),
            serial_number=_text(_first(raw, "serialNumber", "serialNo")),
            platform_id=_text(_first(raw, "platformId", "platform")),
            product_id=_text(_first(raw, "productId", "product")),
            device_type=_text(_first(raw, "type", "deviceType")),
            role=_text(_first(raw, "role", "deviceRole")),
            family=_text(_first(raw, "family")),
            software_version=_text(_first(raw, "softwareVersion", "osVersion")),
            up_time=_text(_first(raw, "upTime", "uptime")),
            reachability=_text(_first(raw, "reachabilityStatus", "reachability")),
            collection_status=_text(_first(raw, "collectionStatus")),
            interface_count=_int_or_none(_first(raw, "interfaceCount")),
            last_updated=_first(raw, "lastUpdated", "lastUpdateTime"),
            source="netcon",
            last_seen=seen_at if seen_at is not None else time.time(),
        )


# --------------------------------------------------------------------------- #
# 拓扑
# --------------------------------------------------------------------------- #
class TopologyNode(BaseModel):
    """拓扑节点。x / y 是 NC 给出的画布坐标，前端 3D 大屏可复用做布局。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    x: Optional[float] = None
    y: Optional[float] = None
    label: Optional[str] = None
    role: Optional[str] = None
    source: SourceType = "netcon"


class Link(BaseModel):
    """拓扑链路。

    注意：``source`` / ``target`` 是两端设备 id（沿用 NC 字段名），
    数据来源标记在 ``data_source``。
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    source: str  # 设备 id（起点）
    target: str  # 设备 id（终点）
    start_port_name: Optional[str] = None
    end_port_name: Optional[str] = None
    start_port_speed: PortSpeed = None
    end_port_speed: PortSpeed = None
    link_status: Optional[str] = None
    data_source: SourceType = "netcon"

    def capacity_mbps(self) -> float:
        """链路容量（Mbps）：取两端速率的较大值，解析失败时按 100Mbps 兜底。

        注意 NC 数值端口速率的单位是 Kbps（``100000`` = 100Mbps），换算在
        :func:`parse_speed_mbps` 内完成。
        """
        speeds = [parse_speed_mbps(self.start_port_speed), parse_speed_mbps(self.end_port_speed)]
        valid = [speed for speed in speeds if speed]
        return max(valid) if valid else 100.0

    def port_speed_mbps(self, start: bool = True) -> float:
        """单端端口速率（Mbps）：解析失败时回退到链路容量兜底值。"""
        value = parse_speed_mbps(
            self.start_port_speed if start else self.end_port_speed
        )
        return value if value else self.capacity_mbps()


class Topology(BaseModel):
    """物理拓扑快照。"""

    model_config = ConfigDict(extra="ignore")

    nodes: List[TopologyNode] = Field(default_factory=list)
    links: List[Link] = Field(default_factory=list)
    captured_at: float = Field(default_factory=time.time)
    source: SourceType = "netcon"

    @classmethod
    def from_netcon(
        cls, doc: Any, captured_at: Optional[float] = None
    ) -> "Topology":
        """归一化 ``GET /topology/physical-topology`` 响应。

        空数据形态：``{"response": {"links": [], "nodes": []}}`` 或 ``{"version": "1.0"}``。
        """
        payload = extract_response(doc)
        if not isinstance(payload, dict):
            payload = {}
        raw_nodes = payload.get("nodes") or []
        raw_links = payload.get("links") or []

        nodes: List[TopologyNode] = []
        for item in raw_nodes:
            if not isinstance(item, dict):
                continue
            node_id = _text(_first(item, "id", "nodeId", "deviceId"))
            if not node_id:
                continue
            nodes.append(
                TopologyNode(
                    id=node_id,
                    x=_float_or_none(_first(item, "x", "positionX")),
                    y=_float_or_none(_first(item, "y", "positionY")),
                    label=_text(_first(item, "label", "hostname", "name", "deviceName")),
                    role=_text(_first(item, "role", "deviceRole", "type")),
                    source="netcon",
                )
            )

        links: List[Link] = []
        for item in raw_links:
            if not isinstance(item, dict):
                continue
            source_id = _text(_first(item, "source", "startNodeId", "sourceNodeId", "from"))
            target_id = _text(_first(item, "target", "endNodeId", "targetNodeId", "to"))
            if not source_id or not target_id:
                continue
            link_id = _text(_first(item, "id", "linkId"))
            if not link_id:
                link_id = stable_id(source_id, target_id, item.get("startPortName"), item.get("endPortName"))
            links.append(
                Link(
                    id=link_id,
                    source=source_id,
                    target=target_id,
                    start_port_name=_text(_first(item, "startPortName", "sourcePort")),
                    end_port_name=_text(_first(item, "endPortName", "targetPort")),
                    start_port_speed=item.get("startPortSpeed"),
                    end_port_speed=item.get("endPortSpeed"),
                    link_status=_text(_first(item, "linkStatus", "status")),
                    data_source="netcon",
                )
            )

        return cls(
            nodes=nodes,
            links=links,
            captured_at=captured_at if captured_at is not None else time.time(),
            source="netcon",
        )


# --------------------------------------------------------------------------- #
# 主机 / 终端
# --------------------------------------------------------------------------- #
class Host(BaseModel):
    """终端主机（NC 的 /host 列表，用于“设备—接入端口—主机”的挂载关系）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    hostname: Optional[str] = None
    ip: Optional[str] = None
    mac: Optional[str] = None
    connected_device_id: Optional[str] = None
    connected_device_name: Optional[str] = None
    connected_interface: Optional[str] = None
    host_type: Optional[str] = None
    ping_status: Optional[str] = None
    vlan_id: Optional[str] = None
    last_updated: Optional[Union[str, int, float]] = None
    source: SourceType = "netcon"
    last_seen: float = Field(default_factory=time.time)

    @classmethod
    def from_netcon(cls, raw: Dict[str, Any], seen_at: Optional[float] = None) -> "Host":
        if not isinstance(raw, dict):
            raise TypeError("host 记录必须是 dict")
        hostname = _text(_first(raw, "hostName", "hostname", "name"))
        ip = _text(_first(raw, "hostIp", "ipAddress", "ip"))
        # 注意：PT 实际返回的是 hostMac（不是 swagger 里的 hostMacAddress）。
        mac = _text(_first(raw, "hostMacAddress", "hostMac", "macAddress", "mac"))
        host_id = _text(_first(raw, "id", "hostId"))
        if not host_id:
            host_id = stable_id(hostname, ip, mac)
        return cls(
            id=host_id,
            hostname=hostname,
            ip=ip,
            mac=mac,
            connected_device_id=_text(
                _first(raw, "connectedNetworkDeviceId", "connectedDeviceId", "deviceId")
            ),
            connected_device_name=_text(
                _first(raw, "connectedNetworkDeviceName", "connectedDeviceName")
            ),
            connected_interface=_text(
                _first(raw, "connectedInterfaceName", "interfaceName", "portName")
            ),
            host_type=_text(_first(raw, "hostType", "type")),
            ping_status=_text(_first(raw, "pingStatus", "status")),
            vlan_id=_text(_first(raw, "vlanId", "vlan")),
            last_updated=_first(raw, "lastUpdated", "lastUpdateTime"),
            source="netcon",
            last_seen=seen_at if seen_at is not None else time.time(),
        )


# --------------------------------------------------------------------------- #
# 健康问题（NC assurance）
# --------------------------------------------------------------------------- #
class HealthIssue(BaseModel):
    """网络健康问题（NC assurance）。

    时间基准（重要）：
    - ``observed_at``：宿主机采集到这条 issue 的时间（epoch 秒），用于告警/展示；
    - ``source_ts``：NC 原始时间字符串，仅用于审计。PT 仿真时钟与宿主机实测相差
      约 10 分钟（故障注入实验：NC 21:33:14 vs 宿主机 21:43:42），两者不能混用；
    - ``stale_source``：数据源记录滞后标记。NC issue 仍存在、但对应 host/device
      实际状态已恢复时，检测引擎会标 stale_source，并在告警侧生成“数据源已恢复”
      的 cleared（见 app/detection/rules.py）。
    """

    model_config = ConfigDict(extra="ignore")

    issue_id: str
    issue_name: Optional[str] = None
    issue_description: Optional[str] = None
    issue_timestamp: Optional[Union[str, int, float]] = None
    issue_source: Optional[str] = None
    severity: str = "warning"  # critical / warning
    status: str = "active"  # active / cleared
    # 宿主机采集时间 / NC 原始时间字符串 / 数据源滞后标记（W3 时间基准）
    observed_at: float = Field(default_factory=time.time)
    source_ts: Optional[str] = None
    stale_source: bool = False
    source: SourceType = "netcon"
    first_seen: float = Field(default_factory=time.time)
    last_seen: float = Field(default_factory=time.time)

    @classmethod
    def from_netcon(cls, raw: Dict[str, Any], seen_at: Optional[float] = None) -> "HealthIssue":
        if not isinstance(raw, dict):
            raise TypeError("health-issue 记录必须是 dict")
        name = _text(_first(raw, "issueName", "name"))
        description = _text(_first(raw, "issueDescription", "description"))
        issue_source = _text(_first(raw, "issueSource", "source"))
        issue_id = _text(_first(raw, "issueId", "id"))
        if not issue_id:
            # 部分 NC 版本缺 issueId：用内容哈希生成稳定 id，保证去重/告警可比对。
            issue_id = stable_id(name, description, issue_source)
        now = seen_at if seen_at is not None else time.time()
        raw_ts = _first(raw, "issueTimestamp", "timestamp")
        return cls(
            issue_id=issue_id,
            issue_name=name,
            issue_description=description,
            issue_timestamp=raw_ts,
            issue_source=issue_source,
            severity=_issue_severity(name, description),
            status="active",
            observed_at=now,
            source_ts=None if raw_ts is None else str(raw_ts),
            stale_source=False,
            source="netcon",
            first_seen=now,
            last_seen=now,
        )


# --------------------------------------------------------------------------- #
# 网络健康总览
# --------------------------------------------------------------------------- #
class NetworkHealth(BaseModel):
    """网络健康总览（W3.1：字段自洽 + NC 原始字段并存）。

    数据口径问题（实测）
    ====================
    PT NC 的 ``/network-health`` 只返回 ``healthyNetworkDevice`` 百分比、没有
    ``healthyDevices/unhealthyDevices`` 计数；旧实现直接透传后会出现
    ``healthy_devices=0 / unhealthy_devices=0 / health_percentage=null`` 却同时
    ``healthy_devices_percentage=100.0`` 的自相矛盾结果。

    修复后的口径
    ============
    - **自算字段（权威口径，``source_of_truth="computed_from_devices"``）**：
      从设备列表自行计算 ``device_total / device_healthy / device_unhealthy /
      device_unreachable / health_percentage``。健康设备定义 = ``collectionStatus ==
      "Managed"`` 且 ``reachabilityStatus`` 不是 Unreachable。
    - **兼容旧字段**：``total_devices / healthy_devices / unhealthy_devices /
      unreachable_devices / healthy_devices_percentage`` 与自算字段同值，保证
      旧前端/AI Agent 继续可用。
    - **NC 原始字段**：``nc_total_devices / nc_unreachable_devices /
      nc_healthy_devices_percentage / nc_healthy_clients_percentage /
      nc_num_licensed_* / nc_summary`` 原样保留，用于审计与对比（不参与自算）。
    - 不变式：``device_healthy + device_unhealthy == device_total``；
      ``health_percentage`` 永远是数值（无设备时为 0.0，不为 null）。
    """

    model_config = ConfigDict(extra="ignore")

    # ---- 自算口径（source_of_truth = computed_from_devices）----
    device_total: int = 0
    device_healthy: int = 0
    device_unhealthy: int = 0
    device_unreachable: int = 0
    health_percentage: float = 0.0
    # ---- 兼容旧字段（与上面的 device_* 保持同值）----
    total_devices: int = 0
    healthy_devices: int = 0
    unhealthy_devices: int = 0
    unreachable_devices: int = 0
    healthy_devices_percentage: Optional[float] = 0.0
    # ---- NC 原始字段（只读审计，不参与自算）----
    nc_total_devices: Optional[int] = None
    nc_unreachable_devices: Optional[int] = None
    nc_healthy_devices_percentage: Optional[float] = None
    nc_healthy_clients_percentage: Optional[float] = None
    nc_num_licensed_routers: Optional[int] = None
    nc_num_licensed_switches: Optional[int] = None
    nc_num_application_policies: Optional[int] = None
    nc_summary: Dict[str, Any] = Field(default_factory=dict)
    # ---- 其余兼容字段 ----
    healthy_clients_percentage: Optional[float] = None
    num_licensed_routers: int = 0
    num_licensed_switches: int = 0
    num_application_policies: int = 0
    timestamp: Optional[Union[str, int, float]] = None
    source_of_truth: str = "computed_from_devices"
    source: SourceType = "netcon"

    # ------------------------------------------------------------------ #
    # NC 原始解析（仅作为 nc_* 输入；权威口径用 compute_from_devices）
    # ------------------------------------------------------------------ #
    @classmethod
    def from_netcon(cls, doc: Any) -> "NetworkHealth":
        """解析 NC 原始健康响应（``/assurance/health`` 嵌套 或 ``/network-health`` 扁平）。

        注意：这里保留 NC 的原始数值（含其口径缺陷），字段 ``source_of_truth``
        标记为 ``nc_raw``。真正对外口径必须再调用 :meth:`compute_from_devices`。
        """
        payload = extract_response(doc)
        if not isinstance(payload, dict):
            return cls(source="netcon", source_of_truth="nc_raw")
        model = cls(source="netcon", source_of_truth="nc_raw", nc_summary=dict(payload))
        devices = payload.get("networkDevices")
        if isinstance(devices, dict):
            total = _int_or_none(devices.get("totalDevices"))
            healthy = _int_or_none(devices.get("healthyDevices"))
            unhealthy = _int_or_none(devices.get("unhealthyDevices"))
            model.total_devices = total if total is not None else 0
            model.healthy_devices = healthy if healthy is not None else 0
            model.unhealthy_devices = unhealthy if unhealthy is not None else 0
            model.device_total = model.total_devices
            model.device_healthy = model.healthy_devices
            model.device_unhealthy = model.unhealthy_devices
            model.health_percentage = _float_or_none(devices.get("totalPercentage"))
            model.healthy_devices_percentage = model.health_percentage
            model.timestamp = payload.get("timestamp")
        model.nc_total_devices = _int_or_none(payload.get("numNetworkDevices"))
        model.nc_unreachable_devices = _int_or_none(payload.get("numUnreachable"))
        model.nc_healthy_devices_percentage = _float_or_none(
            payload.get("healthyNetworkDevice")
        )
        model.nc_healthy_clients_percentage = _float_or_none(payload.get("healthyClient"))
        model.nc_num_licensed_routers = _int_or_none(payload.get("numLicensedRouters"))
        model.nc_num_licensed_switches = _int_or_none(payload.get("numLicensedSwitches"))
        model.nc_num_application_policies = _int_or_none(
            payload.get("numApplicationPolicies")
        )
        # 旧字段（扁平响应）：尽量填充，便于兼容/对比
        if model.nc_total_devices is not None:
            model.total_devices = model.nc_total_devices
            model.device_total = model.nc_total_devices
        if model.nc_unreachable_devices is not None:
            model.unreachable_devices = model.nc_unreachable_devices
            model.device_unreachable = model.nc_unreachable_devices
        model.healthy_clients_percentage = model.nc_healthy_clients_percentage
        model.num_licensed_routers = model.nc_num_licensed_routers or 0
        model.num_licensed_switches = model.nc_num_licensed_switches or 0
        model.num_application_policies = model.nc_num_application_policies or 0
        if model.nc_healthy_devices_percentage is not None:
            model.healthy_devices_percentage = model.nc_healthy_devices_percentage
        return model

    # ------------------------------------------------------------------ #
    # 权威口径：从设备列表自算
    # ------------------------------------------------------------------ #
    @classmethod
    def compute_from_devices(
        cls,
        devices: List[Device],
        source: SourceType = "netcon",
        nc_health: Optional["NetworkHealth"] = None,
    ) -> "NetworkHealth":
        """从设备列表自算 health（权威口径），同时保留 NC 原始字段。

        健康定义：``collectionStatus == "Managed"`` 且 ``reachabilityStatus`` 不是
        Unreachable（不区分大小写）。不变式：healthy + unhealthy == total；
        ``health_percentage`` 恒为数值（无设备时为 0.0）。
        """
        total = len(devices)
        unreachable = 0
        healthy = 0
        for device in devices:
            unreachable_flag = is_unreachable(device.reachability)
            if unreachable_flag:
                unreachable += 1
            managed = (device.collection_status or "").strip().lower() == "managed"
            if managed and not unreachable_flag:
                healthy += 1
        unhealthy = total - healthy
        percentage = round(healthy / total * 100.0, 2) if total else 0.0

        nc_summary = dict(nc_health.nc_summary) if nc_health is not None else {}
        nc_total = nc_health.nc_total_devices if nc_health is not None else None
        nc_unreachable = nc_health.nc_unreachable_devices if nc_health is not None else None
        nc_healthy_pct = (
            nc_health.nc_healthy_devices_percentage if nc_health is not None else None
        )
        nc_healthy_client_pct = (
            nc_health.nc_healthy_clients_percentage if nc_health is not None else None
        )
        return cls(
            device_total=total,
            device_healthy=healthy,
            device_unhealthy=unhealthy,
            device_unreachable=unreachable,
            health_percentage=percentage,
            total_devices=total,
            healthy_devices=healthy,
            unhealthy_devices=unhealthy,
            unreachable_devices=unreachable,
            healthy_devices_percentage=percentage,
            nc_total_devices=nc_total,
            nc_unreachable_devices=nc_unreachable,
            nc_healthy_devices_percentage=nc_healthy_pct,
            nc_healthy_clients_percentage=nc_healthy_client_pct,
            nc_num_licensed_routers=(
                nc_health.nc_num_licensed_routers if nc_health is not None else None
            ),
            nc_num_licensed_switches=(
                nc_health.nc_num_licensed_switches if nc_health is not None else None
            ),
            nc_num_application_policies=(
                nc_health.nc_num_application_policies if nc_health is not None else None
            ),
            nc_summary=nc_summary,
            healthy_clients_percentage=nc_healthy_client_pct,
            num_licensed_routers=(
                (nc_health.nc_num_licensed_routers or 0) if nc_health is not None else 0
            ),
            num_licensed_switches=(
                (nc_health.nc_num_licensed_switches or 0) if nc_health is not None else 0
            ),
            num_application_policies=(
                (nc_health.nc_num_application_policies or 0)
                if nc_health is not None
                else 0
            ),
            timestamp=nc_health.timestamp if nc_health is not None else None,
            source_of_truth="computed_from_devices",
            source=source,
        )

    @classmethod
    def from_devices(cls, devices: List[Device], source: SourceType = "simulator") -> "NetworkHealth":
        """兼容旧入口：等价于 ``compute_from_devices(devices, source)``。"""
        return cls.compute_from_devices(devices, source=source)


# --------------------------------------------------------------------------- #
# 告警
# --------------------------------------------------------------------------- #
class Alert(BaseModel):
    """检测引擎输出的统一告警对象。

    生命周期（W3 降噪核心）
    ======================
    - 一个 ``dedup_key`` 同一时刻最多只有一条 ``state="active"`` 的告警；故障持续
      期间不会每轮重复入库/重复推送；
    - 恢复时把该条 active 告警置为 ``state="cleared"`` 并记录 ``resolved_at`` /
      ``clear_reason``，同一轮多条 cleared 由 pipeline 聚合成一条摘要推送，
      不再逐条产生 info 级“已清除”告警（解决 11 条集中清除噪声）；
    - 一次性信息事件（device_online / link_up）用 ``state="info"`` 记录。
    - 若 NC 的 issue 仍存在、但 host/device 实际状态已恢复，该 cleared 会带
      ``stale_source=True``，detail 说明“NC 问题记录滞后，数据源已恢复”。

    时间基准：``observed_at`` 是宿主机采集时间（告警/展示用），``source_ts`` 是
    NC 原始时间字符串（审计用）；``created_at`` 保留为 observed_at 的别名，
    兼容前端既有字段。
    """

    model_config = ConfigDict(extra="ignore")

    # 服务端 DB id（新建告警入库前为 None；从 DB 读取时自动带上，供 Webhook/API 引用）
    id: Optional[int] = None
    alert_type: str  # host_down/device_unreachable/device_offline/link_down/health_issue (+ cleared 事件)
    severity: str  # critical / warning / info
    title: str
    detail: str = ""
    dedup_key: str = ""
    device_id: Optional[str] = None
    link_id: Optional[str] = None
    issue_id: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    # ---- W3 时间基准 ----
    observed_at: float = Field(default_factory=time.time)
    source_ts: Optional[str] = None
    # ---- W3 告警生命周期 ----
    state: str = "active"  # active / cleared / info
    resolved_at: Optional[float] = None
    clear_reason: Optional[str] = None
    stale_source: bool = False
    # ---- W9 数据管理（软删除 / 静默）----
    # deleted_at 非空 = 已软删除（列表接口默认过滤，可 restore 撤销；active 告警禁止删除）
    deleted_at: Optional[float] = None
    # open / silenced：silenced 仅用于活跃告警静默（不删除、不改变 state）
    status: str = "open"
    silenced_at: Optional[float] = None
    source: SourceType = "netcon"
    acknowledged: bool = False


# --------------------------------------------------------------------------- #
# 指标点（PT 不暴露计数器，统一来自行为仿真）
# --------------------------------------------------------------------------- #
class MetricPoint(BaseModel):
    """单个指标点。

    ``source`` 恒为 ``"simulator"``——PT 的 REST API 不提供接口流量计数器，
    这是仿真补充数据，接口/前端据此区分“真实拓扑”与“仿真指标”。

    物理约束（W3.1）：端口流量类指标（``if_in_bps`` / ``if_out_bps`` /
    ``if_utilization_pct``）必须满足 ``value <= speed_bps``，其中 ``speed_bps``
    是该端口速率的物理上限（Mbps 换算成 bps）。生成器推导后仍会做上限钳制，
    被钳制时 ``clamped=True``，用于审计“仿真值是否触顶”。设备级指标
    （cpu_pct / mem_pct / device_utilization_pct）没有端口速率上限，``speed_bps=None``。
    """

    model_config = ConfigDict(extra="ignore")

    device_id: str
    metric: str  # if_in_bps / if_out_bps / if_utilization_pct / cpu_pct / mem_pct
    value: float
    unit: str = ""
    interface: Optional[str] = None
    ts: float = Field(default_factory=time.time)
    source: SourceType = "simulator"
    # ---- W3.1 物理约束 ----
    speed_bps: Optional[float] = None  # 端口物理速率上限（bps）；设备级指标为 None
    clamped: bool = False  # 是否因超过 speed_bps 被钳制
    tags: Dict[str, Any] = Field(default_factory=dict)



