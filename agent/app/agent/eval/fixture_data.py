# -*- coding: utf-8 -*-
"""离线评测数据构建器：基线快照 + 场景故障注入 + W9 多 Agent 状态注入。

设计目标（任务要求：30 场景可离线、可重复运行）：
- ``fixtures/base_snapshot.json`` 是**静态基线快照**（字段对齐后端 REST API，
  拓扑/设备/主机取自项目 docs 的真实落地记录，另含 4 楼栋场景配置与空审计）；
- 每个评测场景在 JSON 里只声明 ``fault``（排障类故障）或 ``w9``（W9 多 Agent 状态：
  新设备/待确认设备/告警历史/审计），由本模块在基线快照上**确定性地**注入；
- 指标时序由固定公式生成（source=simulator），同一场景多次运行结果完全一致；
- 故障注入只修改目标对象及其直接关联数据，不做全网级联推演（已在 README 说明）。

这样既保留了「真实拓扑结构」，又让评测可以脱离 PT/NC/后端独立重跑。
"""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
BASE_SNAPSHOT_PATH = FIXTURES_DIR / "base_snapshot.json"
SCENARIOS_PATH = Path(__file__).resolve().parent / "scenarios.json"

#: 指标时序参数（确定性，无随机）
METRIC_POINTS = 30
METRIC_INTERVAL_S = 5.0

#: 设备链路容量（用于生成/覆盖 bps 指标）
CAPACITY_MBPS: Dict[str, float] = {
    "dev-core-3650": 1000.0,
    "dev-sw-jx-2960": 100.0,
    "dev-sw-sx-2960": 100.0,
    "dev-sw-ss-2960": 100.0,
    "dev-router-4331": 1000.0,
}

#: 支持的故障类型（排障类场景，scenarios.json 校验用）
FAULT_TYPES: Tuple[str, ...] = (
    "host_down",
    "device_unreachable",
    "link_down",
    "port_shutdown",
    "routing_missing",
    "vlan_misconfig",
    "broadcast_storm",
    "high_cpu",
    "bandwidth_congestion",
)

#: W9 场景类别（多 Agent 能力，无 fault，只有 w9 状态）
W9_CATEGORIES: Tuple[str, ...] = (
    "onboarding",
    "recon",
    "steward",
    "new_building",
)

#: W10 场景类别（只读查询：路径追踪 / 告警根因分组 / 通知状态；expected_actions 必须为空）
W10_CATEGORIES: Tuple[str, ...] = (
    "path_trace",
    "alert_groups",
    "notify",
)

_TZ = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------- #
# 基线快照 / 场景加载
# --------------------------------------------------------------------------- #
def load_base_snapshot(path: Optional[Path] = None) -> Dict[str, Any]:
    """加载静态基线快照，并生成确定性的指标时序数据。"""
    snapshot = _read_json(Path(path) if path else BASE_SNAPSHOT_PATH)
    snapshot = copy.deepcopy(snapshot)
    base_time = float((snapshot.get("meta") or {}).get("base_time") or 0.0)
    specs = snapshot.pop("metrics_fixtures", {}) or {}
    snapshot["metrics"] = _generate_metrics(snapshot, specs, base_time)
    return snapshot


def load_scenarios(path: Optional[Path] = None) -> Dict[str, Any]:
    """加载 35 场景评测集（返回 {"meta":..., "scenarios":[...]}）。"""
    payload = _read_json(Path(path) if path else SCENARIOS_PATH)
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        raise ValueError("scenarios.json 缺少 scenarios 数组：%s" % (path or SCENARIOS_PATH))
    payload["scenarios"] = scenarios
    return payload


def build_scenario_snapshot(
    snapshot: Dict[str, Any],
    scenario: Dict[str, Any],
) -> Dict[str, Any]:
    """在基线快照上注入场景状态，返回新的快照（不修改入参）。

    - 排障类场景（s01-s20）：注入 ``fault``；
    - W9 场景（s21-s30）：``fault`` 可省略，注入 ``w9``（新设备/告警/场景 pending 等）；
    - W10 场景（s31-s35）：额外支持 ``w10``（通知状态等只读接口状态）。
    """
    result = copy.deepcopy(snapshot)
    fault = scenario.get("fault") or {}
    fault_type = str(fault.get("type") or "")
    if fault_type:
        if fault_type not in FAULT_TYPES:
            raise ValueError("未知故障类型：%r（场景 %s）" % (fault_type, scenario.get("id")))
        _FAULT_HANDLERS[fault_type](result, fault)
    w9_state = scenario.get("w9") or {}
    if isinstance(w9_state, dict) and w9_state:
        _apply_w9_state(result, w9_state, str(scenario.get("id") or ""))
    w10_state = scenario.get("w10") or {}
    if isinstance(w10_state, dict) and w10_state:
        _apply_w10_state(result, w10_state)
    return result


# --------------------------------------------------------------------------- #
# 指标时序生成
# --------------------------------------------------------------------------- #
def _generate_metrics(
    snapshot: Dict[str, Any],
    specs: Dict[str, Any],
    base_time: float,
) -> Dict[str, Dict[str, Any]]:
    device_ids = {
        str(device.get("id"))
        for device in (snapshot.get("devices") or {}).get("devices") or []
    }
    metrics: Dict[str, Dict[str, Any]] = {}
    for device_id, spec in specs.items():
        phase = float(spec.get("phase") or 0.0)
        cpu_base = float(spec.get("cpu_base") or 15.0)
        mem_base = float(spec.get("mem_base") or 30.0)
        util_base = float(spec.get("util_base") or 12.0)
        capacity = float(spec.get("capacity_mbps") or CAPACITY_MBPS.get(device_id, 100.0))
        interface = str(spec.get("uplink_interface") or "")
        points: List[Dict[str, Any]] = []
        for index in range(METRIC_POINTS):
            ts = base_time - (METRIC_POINTS - 1 - index) * METRIC_INTERVAL_S
            cpu = _clamp(
                cpu_base + 4.2 * math.sin(index / 4.0 + phase) + 2.6 * math.cos(index / 7.0 + phase / 2.0),
                0.5,
                99.0,
            )
            mem = _clamp(
                mem_base + 2.2 * math.sin(index / 9.0 + phase) + 1.4 * math.cos(index / 13.0),
                0.5,
                99.0,
            )
            util = _clamp(
                util_base
                + 5.0 * math.sin(index / 5.0 + phase)
                + 2.2 * math.cos(index / 11.0 + phase / 3.0),
                0.3,
                99.0,
            )
            in_bps = util / 100.0 * capacity * 1_000_000.0
            out_bps = in_bps * 0.73
            points.extend(
                [
                    _metric_point("cpu_pct", cpu, "%", None, ts),
                    _metric_point("mem_pct", mem, "%", None, ts),
                    _metric_point("if_utilization_pct", util, "%", interface, ts),
                    _metric_point("if_in_bps", in_bps, "bps", interface, ts),
                    _metric_point("if_out_bps", out_bps, "bps", interface, ts),
                ]
            )
        metrics[device_id] = {
            "device_id": device_id,
            "known_device": device_id in device_ids,
            "source": "simulator",
            "count": len(points),
            "metrics": points,
        }
    return metrics


def _metric_point(
    metric: str,
    value: float,
    unit: str,
    interface: Optional[str],
    ts: float,
) -> Dict[str, Any]:
    return {
        "metric": metric,
        "value": round(float(value), 2),
        "unit": unit,
        "interface": interface,
        "ts": ts,
        "source": "simulator",
        "tags": {},
    }


# --------------------------------------------------------------------------- #
# 故障注入实现
# --------------------------------------------------------------------------- #
def _fault_host_down(snapshot: Dict[str, Any], fault: Dict[str, Any]) -> None:
    host = _require_host(snapshot, fault.get("hostname"))
    _fail_host(snapshot, host)
    switch = host.get("connected_device_name")
    port = host.get("connected_interface")
    _append_issue(
        snapshot,
        fault_type="host_down",
        issue_name="%s / Connectivity" % host.get("hostname"),
        description="Device is down（终端 %s 不可达，接入 %s %s）"
        % (host.get("hostname"), switch, port),
        source=str(host.get("hostname")),
        severity="critical",
    )
    _append_alert(
        snapshot,
        fault_type="host_down",
        alert_type="host_down",
        severity="critical",
        title="终端离线：%s（%s）" % (host.get("hostname"), host.get("ip")),
        detail="主机 %s（%s）ping=FAILURE，接入 %s %s，VLAN %s；NC 健康问题：Connectivity/Device is down"
        % (host.get("hostname"), host.get("ip"), switch, port, host.get("vlan_id")),
        device_id=str(host.get("connected_device_id") or ""),
        dedup_key="host_down:%s" % host.get("hostname"),
    )
    _refresh_client_health(snapshot)


def _fault_device_unreachable(snapshot: Dict[str, Any], fault: Dict[str, Any]) -> None:
    device = _require_device(snapshot, fault.get("device"))
    device["reachability"] = "Unreachable"
    device["collection_status"] = "Unreachable"
    device["last_seen"] = _base_time(snapshot) - 30.0
    _append_issue(
        snapshot,
        fault_type="device_unreachable",
        issue_name="%s / Reachability" % device.get("hostname"),
        description="Network device is unreachable（设备 %s 不可达，管理 IP %s）"
        % (device.get("hostname"), device.get("management_ip")),
        source=str(device.get("hostname")),
        severity="critical",
    )
    _append_alert(
        snapshot,
        fault_type="device_unreachable",
        alert_type="device_offline",
        severity="critical",
        title="设备离线：%s（%s）" % (device.get("hostname"), device.get("management_ip")),
        detail="设备 %s（%s）reachability=Unreachable，collectionStatus=Unreachable，采集不到该设备数据"
        % (device.get("hostname"), device.get("management_ip")),
        device_id=str(device.get("id")),
        dedup_key="device_offline:%s" % device.get("id"),
    )
    total = len(_devices(snapshot))
    unhealthy = len([item for item in _devices(snapshot) if _device_is_unreachable(item)])
    healthy = max(0, total - unhealthy)
    _set_device_health(snapshot, total, healthy, unhealthy)


def _fault_link_down(snapshot: Dict[str, Any], fault: Dict[str, Any]) -> None:
    link = _require_link(snapshot, fault.get("link_id"))
    link["link_status"] = "down"
    source_label = _device_label(snapshot, str(link.get("source") or ""))
    target_label = _device_label(snapshot, str(link.get("target") or ""))
    _append_issue(
        snapshot,
        fault_type="link_down",
        issue_name="Link Down / %s" % link.get("start_port_name"),
        description="链路 %s %s ↔ %s %s 中断（link status down）"
        % (
            source_label,
            link.get("start_port_name"),
            target_label,
            link.get("end_port_name"),
        ),
        source=source_label,
        severity="critical",
    )
    _append_alert(
        snapshot,
        fault_type="link_down",
        alert_type="link_down",
        severity="critical",
        title="链路中断：%s %s ↔ %s %s"
        % (
            source_label,
            link.get("start_port_name"),
            target_label,
            link.get("end_port_name"),
        ),
        detail="拓扑链路 %s 状态由 up 变为 down（%s %s ↔ %s %s），对端设备可能整体离线"
        % (
            link.get("id"),
            source_label,
            link.get("start_port_name"),
            target_label,
            link.get("end_port_name"),
        ),
        device_id=str(link.get("source") or ""),
        link_id=str(link.get("id") or ""),
        dedup_key="link_down:%s" % link.get("id"),
    )
    # 若中断的是核心 → 接入交换机链路，级联标记该交换机下的终端离线
    target_device = _find_device(snapshot, str(link.get("target") or ""))
    if target_device and str(target_device.get("role") or "").upper() == "ACCESS":
        for host in _hosts(snapshot):
            if str(host.get("connected_device_id") or "") == str(target_device.get("id")):
                _fail_host(snapshot, host)
        _refresh_client_health(snapshot)


def _fault_port_shutdown(snapshot: Dict[str, Any], fault: Dict[str, Any]) -> None:
    switch = _require_device(snapshot, fault.get("switch"))
    port = str(fault.get("port") or "")
    host = _require_host(snapshot, fault.get("hostname"))
    if str(host.get("connected_device_id") or "") != str(switch.get("id")):
        raise ValueError(
            "端口故障数据不一致：%s 不接在 %s 上"
            % (host.get("hostname"), switch.get("hostname"))
        )
    _fail_host(snapshot, host)
    _append_issue(
        snapshot,
        fault_type="port_shutdown",
        issue_name="%s / Connectivity" % host.get("hostname"),
        description="Port %s on %s is shutdown（端口 shutdown，终端 %s 不可达）"
        % (port, switch.get("hostname"), host.get("hostname")),
        source=str(host.get("hostname")),
        severity="critical",
    )
    _append_alert(
        snapshot,
        fault_type="port_shutdown",
        alert_type="port_shutdown",
        severity="critical",
        title="端口 shutdown：%s %s" % (switch.get("hostname"), port),
        detail="%s %s 被 shutdown，终端 %s（%s）ping=FAILURE；需在接入交换机执行 no shutdown 恢复"
        % (switch.get("hostname"), port, host.get("hostname"), host.get("ip")),
        device_id=str(switch.get("id")),
        dedup_key="port_shutdown:%s:%s" % (switch.get("id"), port),
    )
    _refresh_client_health(snapshot)


def _fault_routing_missing(snapshot: Dict[str, Any], fault: Dict[str, Any]) -> None:
    device = _require_device(snapshot, fault.get("device"))
    _append_issue(
        snapshot,
        fault_type="routing_missing",
        issue_name="Routing / Missing default route",
        description="%s（%s）缺少默认路由（缺省路由缺失），跨网段流量中断，回程路由不可达"
        % (device.get("hostname"), device.get("management_ip")),
        source=str(device.get("hostname")),
        severity="critical",
    )
    _append_alert(
        snapshot,
        fault_type="routing_missing",
        alert_type="routing_missing",
        severity="critical",
        title="路由缺失：%s 缺省路由丢失" % device.get("hostname"),
        detail="%s（%s）路由表中默认路由/回程路由缺失，教学楼与服务器网段互通中断；请检查 show ip route 与 OSPF 配置"
        % (device.get("hostname"), device.get("management_ip")),
        device_id=str(device.get("id")),
        dedup_key="routing_missing:%s" % device.get("id"),
    )


def _fault_vlan_misconfig(snapshot: Dict[str, Any], fault: Dict[str, Any]) -> None:
    switch = _require_device(snapshot, fault.get("switch"))
    port = str(fault.get("port") or "")
    host = _require_host(snapshot, fault.get("hostname"))
    expected_vlan = str(fault.get("expected_vlan") or host.get("vlan_id") or "20")
    actual_vlan = str(fault.get("actual_vlan") or "30")
    _fail_host(snapshot, host)
    host["vlan_id"] = actual_vlan
    _append_issue(
        snapshot,
        fault_type="vlan_misconfig",
        issue_name="VLAN / Port VLAN mismatch",
        description="VLAN 配置错误：%s %s 应属 VLAN %s，实际 VLAN %s；终端 %s（%s）无法获取网关"
        % (
            switch.get("hostname"),
            port,
            expected_vlan,
            actual_vlan,
            host.get("hostname"),
            host.get("ip"),
        ),
        source=str(switch.get("hostname")),
        severity="warning",
    )
    _append_alert(
        snapshot,
        fault_type="vlan_misconfig",
        alert_type="vlan_misconfig",
        severity="warning",
        title="VLAN 配置错误：%s %s" % (switch.get("hostname"), port),
        detail="%s %s VLAN %s 与终端 %s 网段 VLAN %s 不一致（VLAN mismatch），%s ping=FAILURE"
        % (
            switch.get("hostname"),
            port,
            actual_vlan,
            host.get("hostname"),
            expected_vlan,
            host.get("hostname"),
        ),
        device_id=str(switch.get("id")),
        dedup_key="vlan_misconfig:%s:%s" % (switch.get("id"), port),
    )
    _refresh_client_health(snapshot)


def _fault_broadcast_storm(snapshot: Dict[str, Any], fault: Dict[str, Any]) -> None:
    switch = _require_device(snapshot, fault.get("switch"))
    ports = [str(port) for port in fault.get("ports") or []]
    port_text = " 与 ".join(ports) if ports else "接入端口"
    _append_issue(
        snapshot,
        fault_type="broadcast_storm",
        issue_name="Broadcast / Storm detected",
        description="%s %s 形成二层环路，广播风暴导致 CPU 与端口利用率异常升高（broadcast storm）"
        % (switch.get("hostname"), port_text),
        source=str(switch.get("hostname")),
        severity="critical",
    )
    _append_alert(
        snapshot,
        fault_type="broadcast_storm",
        alert_type="broadcast_storm",
        severity="critical",
        title="广播风暴：%s 检测到二层环路" % switch.get("hostname"),
        detail="%s %s 出现广播风暴（loop/broadcast storm），CPU 利用率升高、用户网络卡顿；建议检查 STP 与端口连线"
        % (switch.get("hostname"), port_text),
        device_id=str(switch.get("id")),
        dedup_key="broadcast_storm:%s" % switch.get("id"),
    )
    _override_metrics(
        snapshot,
        str(switch.get("id")),
        {"cpu_pct": 93.5, "if_utilization_pct": 90.0},
        interface=str((ports[0] if ports else "Fa0/24")),
    )


def _fault_high_cpu(snapshot: Dict[str, Any], fault: Dict[str, Any]) -> None:
    device = _require_device(snapshot, fault.get("device"))
    _append_issue(
        snapshot,
        fault_type="high_cpu",
        issue_name="High CPU utilization",
        description="%s CPU 利用率持续高于 90%%（约 94%%），可能出现异常流量或进程"
        % device.get("hostname"),
        source=str(device.get("hostname")),
        severity="warning",
    )
    _append_alert(
        snapshot,
        fault_type="high_cpu",
        alert_type="cpu_high",
        severity="warning",
        title="CPU 负载过高：%s" % device.get("hostname"),
        detail="%s CPU 利用率 94%%，超过阈值 80%%，需排查异常流量/环路或高负载进程"
        % device.get("hostname"),
        device_id=str(device.get("id")),
        dedup_key="cpu_high:%s" % device.get("id"),
    )
    _override_metrics(snapshot, str(device.get("id")), {"cpu_pct": 94.0})


def _fault_bandwidth_congestion(snapshot: Dict[str, Any], fault: Dict[str, Any]) -> None:
    device = _require_device(snapshot, fault.get("device"))
    interface = str(fault.get("interface") or "")
    _append_issue(
        snapshot,
        fault_type="bandwidth_congestion",
        issue_name="Congestion / Interface utilization high",
        description="%s %s 带宽利用率持续高于 90%%，链路拥塞（congestion，带宽瓶颈）"
        % (device.get("hostname"), interface),
        source=str(device.get("hostname")),
        severity="warning",
    )
    _append_alert(
        snapshot,
        fault_type="bandwidth_congestion",
        alert_type="bandwidth_high",
        severity="warning",
        title="链路带宽拥塞：%s %s" % (device.get("hostname"), interface),
        detail="%s %s 接口利用率 92%%，超过阈值 80%%；大流量可能导致业务卡顿，需排查流量来源或扩容"
        % (device.get("hostname"), interface),
        device_id=str(device.get("id")),
        dedup_key="bandwidth_high:%s:%s" % (device.get("id"), interface),
    )
    _override_metrics(
        snapshot,
        str(device.get("id")),
        {"if_utilization_pct": 92.0},
        interface=interface or None,
    )


_FAULT_HANDLERS = {
    "host_down": _fault_host_down,
    "device_unreachable": _fault_device_unreachable,
    "link_down": _fault_link_down,
    "port_shutdown": _fault_port_shutdown,
    "routing_missing": _fault_routing_missing,
    "vlan_misconfig": _fault_vlan_misconfig,
    "broadcast_storm": _fault_broadcast_storm,
    "high_cpu": _fault_high_cpu,
    "bandwidth_congestion": _fault_bandwidth_congestion,
}


# --------------------------------------------------------------------------- #
# 快照操作辅助
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> Dict[str, Any]:
    with open(str(path), "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层必须是对象：%s" % path)
    return payload


def _base_time(snapshot: Dict[str, Any]) -> float:
    return float((snapshot.get("meta") or {}).get("base_time") or 0.0)


def _devices(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    return (snapshot.get("devices") or {}).setdefault("devices", [])


def _hosts(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    return (snapshot.get("hosts") or {}).setdefault("hosts", [])


def _issues(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    return (snapshot.get("issues") or {}).setdefault("issues", [])


def _alerts(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    return (snapshot.get("alerts") or {}).setdefault("alerts", [])


def _device_is_unreachable(device: Dict[str, Any]) -> bool:
    reachability = str(device.get("reachability") or "").strip().lower()
    collection = str(device.get("collection_status") or "").strip().lower()
    return reachability.startswith("unreach") or collection in ("unreachable", "failed")


def _find_device(snapshot: Dict[str, Any], query: str) -> Optional[Dict[str, Any]]:
    needle = str(query or "").strip().lower()
    if not needle:
        return None
    for device in _devices(snapshot):
        if str(device.get("id") or "").lower() == needle:
            return device
    for device in _devices(snapshot):
        if str(device.get("hostname") or "").lower() == needle:
            return device
    return None


def _require_device(snapshot: Dict[str, Any], query: Any) -> Dict[str, Any]:
    device = _find_device(snapshot, str(query or ""))
    if device is None:
        raise ValueError("场景引用了不存在的设备：%r" % query)
    return device


def _require_host(snapshot: Dict[str, Any], query: Any) -> Dict[str, Any]:
    needle = str(query or "").strip().lower()
    for host in _hosts(snapshot):
        if str(host.get("hostname") or "").lower() == needle:
            return host
    raise ValueError("场景引用了不存在的主机：%r" % query)


def _require_link(snapshot: Dict[str, Any], query: Any) -> Dict[str, Any]:
    needle = str(query or "").strip()
    for link in (snapshot.get("topology") or {}).get("links") or []:
        if str(link.get("id") or "") == needle:
            return link
    raise ValueError("场景引用了不存在的链路：%r" % query)


def _device_label(snapshot: Dict[str, Any], device_id: str) -> str:
    for node in (snapshot.get("topology") or {}).get("nodes") or []:
        if str(node.get("id") or "") == device_id:
            return str(node.get("label") or device_id)
    device = _find_device(snapshot, device_id)
    return str((device or {}).get("hostname") or device_id)


def _fail_host(snapshot: Dict[str, Any], host: Dict[str, Any]) -> None:
    host["ping_status"] = "FAILURE"
    host["last_updated"] = _iso(_base_time(snapshot))
    host["last_seen"] = _base_time(snapshot)


def _refresh_client_health(snapshot: Dict[str, Any]) -> None:
    hosts = _hosts(snapshot)
    if not hosts:
        return
    failed = sum(1 for host in hosts if str(host.get("ping_status") or "").upper() == "FAILURE")
    percentage = round((len(hosts) - failed) / float(len(hosts)) * 100.0, 2)
    _network_health(snapshot)["healthy_clients_percentage"] = percentage


def _set_device_health(
    snapshot: Dict[str, Any],
    total: int,
    healthy: int,
    unhealthy: int,
) -> None:
    health = _network_health(snapshot)
    health["total_devices"] = total
    health["healthy_devices"] = healthy
    health["unhealthy_devices"] = unhealthy
    health["unreachable_devices"] = unhealthy
    percentage = round(healthy / float(total) * 100.0, 2) if total else 0.0
    health["health_percentage"] = percentage
    health["healthy_devices_percentage"] = percentage
    health["timestamp"] = _iso(_base_time(snapshot))


def _network_health(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    block = snapshot.setdefault("network_health", {})
    health = block.get("network_health")
    if not isinstance(health, dict):
        health = {}
        block["network_health"] = health
    return health


def _append_issue(
    snapshot: Dict[str, Any],
    fault_type: str,
    issue_name: str,
    description: str,
    source: str,
    severity: str,
) -> Dict[str, Any]:
    base_time = _base_time(snapshot)
    issues = _issues(snapshot)
    issue = {
        "issue_id": "fixture-%s-%02d" % (fault_type, len(issues) + 1),
        "issue_name": issue_name,
        "issue_description": description,
        "issue_source": source,
        "severity": severity,
        "status": "active",
        "observed_at": base_time,
        "source_ts": _iso(base_time - 600.0),  # NC 仿真设备时钟约慢 10 分钟
        "issue_timestamp": _iso(base_time - 600.0),
        "first_seen": base_time,
        "last_seen": base_time,
        "source": "netcon",
    }
    issues.append(issue)
    (snapshot.get("issues") or {})["count"] = len(issues)
    return issue


def _append_alert(
    snapshot: Dict[str, Any],
    fault_type: str,
    alert_type: str,
    severity: str,
    title: str,
    detail: str,
    device_id: str = "",
    dedup_key: str = "",
    link_id: str = "",
) -> Dict[str, Any]:
    base_time = _base_time(snapshot)
    alerts = _alerts(snapshot)
    alert = {
        "id": len(alerts) + 1,
        "alert_type": alert_type,
        "severity": severity,
        "title": title,
        "detail": detail,
        "dedup_key": dedup_key or ("%s:%s" % (fault_type, len(alerts) + 1)),
        "device_id": device_id or None,
        "link_id": link_id or None,
        "issue_id": None,
        "created_at": base_time,
        "observed_at": base_time,
        "source_ts": _iso(base_time - 600.0),
        "source": "netcon",
        "acknowledged": False,
    }
    alerts.append(alert)
    (snapshot.get("alerts") or {})["count"] = len(alerts)
    return alert


def _override_metrics(
    snapshot: Dict[str, Any],
    device_id: str,
    overrides: Dict[str, float],
    interface: Optional[str] = None,
    points: int = 6,
) -> None:
    """把指定设备最后 N 个指标点覆盖为异常值（确定性，无随机）。"""
    payload = (snapshot.get("metrics") or {}).get(device_id)
    if payload is None:
        raise ValueError("指标数据缺失：%s" % device_id)
    counters = {name: 0 for name in overrides}
    for point in reversed(payload.get("metrics") or []):
        name = point.get("metric")
        if name not in overrides or counters[name] >= points:
            continue
        value = float(overrides[name]) - (counters[name] % 3) * 0.7
        point["value"] = round(max(0.0, min(99.9, value)), 2)
        if name == "if_utilization_pct" and interface:
            point["interface"] = interface
        counters[name] += 1
        if all(value >= points for value in counters.values()):
            break


def _iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):  # pragma: no cover - 防御性
        return ""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# W9 场景状态注入（新设备 / 待确认场景设备 / 告警历史 / 审计）
# --------------------------------------------------------------------------- #
def _apply_w9_state(snapshot: Dict[str, Any], w9: Dict[str, Any], scene_id: str) -> None:
    base_time = _base_time(snapshot)
    for alert_spec in w9.get("alerts") or []:
        if isinstance(alert_spec, dict):
            _inject_alert(snapshot, alert_spec, base_time)
    for device_spec in w9.get("new_devices") or []:
        if isinstance(device_spec, dict):
            _inject_new_device(snapshot, device_spec, base_time)
    for entry in w9.get("audit") or []:
        if isinstance(entry, dict):
            _inject_audit(snapshot, entry, base_time)


# --------------------------------------------------------------------------- #
# W10 场景状态注入（通知状态只读接口）
# --------------------------------------------------------------------------- #
def _apply_w10_state(snapshot: Dict[str, Any], w10: Dict[str, Any]) -> None:
    notify = snapshot.setdefault("notify", {})
    if not isinstance(notify, dict):
        notify = {}
        snapshot["notify"] = notify
    override = w10.get("notify")
    if isinstance(override, dict):
        notify.update(override)
    payload = w10.get("alert_groups")
    if isinstance(payload, list):
        snapshot["alert_groups_override"] = payload


def _inject_alert(snapshot: Dict[str, Any], spec: Dict[str, Any], base_time: float) -> None:
    alerts = _alerts(snapshot)
    index = len(alerts) + 1
    created_at = float(spec.get("created_at") or base_time)
    state = str(spec.get("state") or "cleared")
    status = str(spec.get("status") or ("cleared" if state == "cleared" else "open"))
    alert = {
        "id": spec.get("id") or (100 + index),
        "alert_id": spec.get("id") or (100 + index),
        "alert_type": spec.get("alert_type") or "health_issue_cleared",
        "severity": spec.get("severity") or "warning",
        "title": spec.get("title") or "历史告警 %02d" % index,
        "detail": spec.get("detail") or "",
        "state": state,
        "status": "open",
        "device_id": spec.get("device_id"),
        "dedup_key": spec.get("dedup_key") or ("w9:%02d" % index),
        "created_at": created_at,
        "observed_at": spec.get("observed_at") or created_at,
        "source_ts": spec.get("source_ts") or _iso(created_at - 600.0),
        "source": "netcon",
        "acknowledged": False,
        "stale_source": False,
        "deleted_at": None,
        "silenced_at": None,
    }
    if state == "cleared":
        alert["resolved_at"] = float(spec.get("resolved_at") or (created_at + 5.0))
        alert["clear_reason"] = spec.get("clear_reason") or "数据源实际状态已恢复"
    alerts.append(alert)
    (snapshot.get("alerts") or {})["count"] = len(alerts)


def _inject_new_device(snapshot: Dict[str, Any], spec: Dict[str, Any], base_time: float) -> None:
    hostname = str(spec.get("hostname") or "").strip()
    if not hostname:
        raise ValueError("W9 新设备缺少 hostname")
    devices = _devices(snapshot)
    existing = {str(item.get("hostname") or "").lower() for item in devices}
    if hostname.lower() not in existing:
        index = len(devices) + 1
        device_id = str(spec.get("id") or "dev-new-%02d" % index)
        platform = str(spec.get("platform_id") or spec.get("model") or "2960")
        device = {
            "id": device_id,
            "hostname": hostname,
            "management_ip": spec.get("management_ip") or "192.168.10.%d" % (20 + index),
            "mac_address": spec.get("mac_address") or "00:1B:0C:99:00:%02X" % index,
            "serial_number": "FIXTURE-%s" % hostname,
            "platform_id": platform,
            "product_id": spec.get("product_id") or "WS-C2960-24TT-L",
            "device_type": spec.get("device_type") or "Cisco Catalyst 2960",
            "role": spec.get("role") or "ACCESS",
            "family": "Switches and Hubs",
            "software_version": "IOS 15.0(2)SE4",
            "up_time": "0 days, 0 hours, 3 minutes",
            "reachability": spec.get("reachability") or "Reachable",
            "collection_status": spec.get("collection_status") or "Managed",
            "interface_count": 24,
            "last_updated": _iso(base_time),
            "source": "netcon",
            "last_seen": base_time,
        }
        devices.append(device)
        (snapshot.get("devices") or {})["count"] = len(devices)

        topology = snapshot.setdefault("topology", {})
        nodes = topology.setdefault("nodes", [])
        nodes.append(
            {
                "id": device_id,
                "x": float(spec.get("x") or (200.0 + index * 10)),
                "y": float(spec.get("y") or 300.0),
                "label": hostname,
                "role": spec.get("role") or "ACCESS",
                "source": "netcon",
            }
        )
        neighbor_name = spec.get("neighbor")
        if neighbor_name:
            neighbor = next(
                (item for item in _devices(snapshot) if str(item.get("hostname")) == str(neighbor_name)),
                None,
            )
            if neighbor is not None:
                links = topology.setdefault("links", [])
                links.append(
                    {
                        "id": "link-fixture-new-%02d" % index,
                        "source": str(neighbor.get("id")),
                        "target": device_id,
                        "start_port_name": spec.get("neighbor_port") or "Gi1/0/9",
                        "end_port_name": spec.get("local_port") or "Fa0/24",
                        "start_port_speed": "1Gbps",
                        "end_port_speed": "100Mbps",
                        "link_status": "up",
                        "data_source": "netcon",
                    }
                )
                topology.setdefault("counts", {})["links"] = len(links)
        topology.setdefault("counts", {})["nodes"] = len(nodes)

    # 已在后端 pending 队列（自动发现后待确认）→ 写入场景
    if spec.get("in_scene"):
        scene = snapshot.setdefault("scene", {"version": 1, "buildings": []})
        buildings = scene.setdefault("buildings", [])
        building_name = str(spec.get("scene_building") or "")
        building = next(
            (item for item in buildings if str(item.get("name")) == building_name),
            buildings[0] if buildings else None,
        )
        if building is not None:
            scene_type = str(spec.get("scene_type") or "switch")
            position = building.get("position") or {}
            size = building.get("size") or {}
            base_x = float(position.get("x") or 0.0)
            base_z = float(position.get("z") or 0.0)
            height = float(size.get("height") or 24.0)
            device_position = spec.get("position") or {
                "x": base_x,
                "y": round(height / 2.0 + 3.8, 1),
                "z": base_z,
            }
            devices_in_building = building.setdefault("devices", [])
            if not any(str(item.get("name")) == hostname for item in devices_in_building):
                devices_in_building.append(
                    {
                        "name": hostname,
                        "type": scene_type,
                        "position": device_position,
                        "status": "pending",
                        "source": "netcon",
                        "model": spec.get("model"),
                        "mgmt_ip": spec.get("management_ip"),
                    }
                )
                scene["version"] = int(scene.get("version") or 1) + 1


def _inject_audit(snapshot: Dict[str, Any], spec: Dict[str, Any], base_time: float) -> None:
    audit = snapshot.setdefault("audit", {"count": 0, "entries": []})
    entries = audit.setdefault("entries", [])
    created_at = float(spec.get("created_at") or spec.get("time") or (base_time - 120.0))
    entries.append(
        {
            "id": spec.get("id") or (len(entries) + 1),
            "created_at": created_at,
            "created_at_iso": spec.get("created_at_iso") or _iso(created_at),
            "operator": spec.get("operator") or spec.get("who") or "web-ui",
            "action": spec.get("action") or "alert.delete",
            "target": spec.get("target") or "19",
            "detail": spec.get("detail") or "",
            "result": spec.get("result") or "ok",
        }
    )
    audit["count"] = len(entries)
