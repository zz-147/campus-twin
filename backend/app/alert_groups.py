# -*- coding: utf-8 -*-
"""W10 告警聚合与根因分组（展示层，不修改告警状态）。

契约（docs/W10-路径追踪告警聚合设计.md §二）：
``GET /api/alerts/groups?state=active`` →
``{count, groups:[{group_id, root_cause, root_device_id, root_device_name, severity,
title, alert_ids, member_count, suppressed_count, first_seen, last_seen, members[]}]}``

规则
====
1. **归组**：同链路/同设备的告警归入一组；每组根因取成员中最高优先级：
   ``link_down > device_unreachable > device_offline > host_down > health_issue > other``。
2. **抑制**：若某组根因是 link_down/device_unreachable，则拓扑下游（BFS）设备/主机
   的告警组会被该根因组吸收并标记 ``suppressed_by=group_id``（仅在展示层抑制：
   不删除、不改变 active 状态）。
3. 无根因的独立告警单独成组（``suppressed_count=0``）。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Iterable, List, Optional, Set

ROOT_CAUSE_PRIORITY: Dict[str, int] = {
    "link_down": 4,
    "device_unreachable": 3,
    "device_offline": 2,
    "host_down": 1,
    "health_issue": 0,
}
SEVERITY_RANK: Dict[str, int] = {"critical": 3, "warning": 2, "info": 1}
# 会触发“下游抑制”的根因类型
SUPPRESSING_ROOT_CAUSES = ("link_down", "device_unreachable")


def _as_alert_dict(alert: Any) -> Dict[str, Any]:
    if isinstance(alert, dict):
        return dict(alert)
    dump = getattr(alert, "model_dump", None)
    if callable(dump):
        return dump(mode="json") if "mode" in dump.__code__.co_varnames else dump()
    return dict(alert)


def _group_key(alert: Dict[str, Any], device_ids: Set[str]) -> tuple:
    alert_type = str(alert.get("alert_type") or "")
    if alert_type == "link_down":
        return ("link", str(alert.get("link_id") or alert.get("dedup_key") or alert.get("id")))
    if alert_type in ("device_unreachable", "device_offline"):
        return ("device", str(alert.get("device_id") or alert.get("dedup_key")))
    if alert_type == "health_issue":
        device_id = alert.get("device_id")
        if device_id and str(device_id) in device_ids:
            return ("device", str(device_id))
        return ("issue", str(alert.get("issue_id") or alert.get("dedup_key")))
    if alert_type == "host_down":
        device_id = alert.get("device_id")
        if device_id and str(device_id) in device_ids:
            return ("device", str(device_id))
        return ("host", str(alert.get("dedup_key")))
    return ("alert", str(alert.get("dedup_key") or alert.get("id")))


def _max_priority_type(members: List[Dict[str, Any]]) -> str:
    ranked = sorted(
        members,
        key=lambda member: ROOT_CAUSE_PRIORITY.get(
            str(member.get("alert_type") or ""), -1
        ),
        reverse=True,
    )
    return str((ranked[0] if ranked else {}).get("alert_type") or "unknown")


def _group_device_ids(
    members: List[Dict[str, Any]],
    root_device_id: Optional[str],
    device_ids: Set[str],
) -> Set[str]:
    ids: Set[str] = set()
    for member in members:
        device_id = member.get("device_id")
        if device_id and str(device_id) in device_ids:
            ids.add(str(device_id))
    if root_device_id and root_device_id in device_ids:
        ids.add(root_device_id)
    return ids


def build_alert_groups(
    alerts: Iterable[Any],
    snapshot: Optional[Dict[str, Any]],
    state: str = "active",
) -> Dict[str, Any]:
    """把告警列表聚合成根因分组（含下游抑制标记）。"""
    snapshot = snapshot or {}
    devices_raw = snapshot.get("devices") or []
    topology = snapshot.get("topology") or {}
    links_raw = topology.get("links") or []

    devices: Dict[str, Dict[str, Any]] = {}
    name_by_id: Dict[str, str] = {}
    ip_by_id: Dict[str, str] = {}
    for device in devices_raw:
        if not isinstance(device, dict):
            continue
        device_id = str(device.get("id") or "")
        if not device_id:
            continue
        devices[device_id] = device
        name_by_id[device_id] = str(
            device.get("hostname") or device.get("management_ip") or device_id
        )
        if device.get("management_ip"):
            ip_by_id[device_id] = str(device["management_ip"])

    links: Dict[str, Dict[str, Any]] = {}
    adjacency: Dict[str, Set[str]] = {}
    for link in links_raw:
        if not isinstance(link, dict):
            continue
        link_id = str(link.get("id") or "")
        if link_id:
            links[link_id] = link
        source_id = str(link.get("source") or "")
        target_id = str(link.get("target") or "")
        if source_id and target_id:
            adjacency.setdefault(source_id, set()).add(target_id)
            adjacency.setdefault(target_id, set()).add(source_id)

    device_ids = set(devices.keys())
    grouped: Dict[tuple, List[Dict[str, Any]]] = {}
    for raw_alert in alerts:
        alert = _as_alert_dict(raw_alert)
        key = _group_key(alert, device_ids)
        grouped.setdefault(key, []).append(alert)

    # ---------- 1) 初步分组 ----------
    groups: List[Dict[str, Any]] = []
    for key, members in grouped.items():
        root_cause = _max_priority_type(members)
        root_device_id: Optional[str] = None
        root_device_name: Optional[str] = None
        if root_cause == "link_down":
            link_id = str((members[0].get("link_id") or ""))
            link = links.get(link_id) or {}
            root_device_id = str(link.get("source") or members[0].get("device_id") or "") or None
        else:
            ranked = sorted(
                members,
                key=lambda member: ROOT_CAUSE_PRIORITY.get(
                    str(member.get("alert_type") or ""), -1
                ),
                reverse=True,
            )
            for member in ranked:
                device_id = member.get("device_id")
                if device_id and str(device_id) in device_ids:
                    root_device_id = str(device_id)
                    break
        if root_device_id:
            root_device_name = name_by_id.get(root_device_id) or root_device_id

        first_seen = min(
            float(member.get("observed_at") or member.get("created_at") or time.time())
            for member in members
        )
        last_seen = max(
            float(member.get("observed_at") or member.get("created_at") or time.time())
            for member in members
        )
        severity = max(
            (str(member.get("severity") or "warning") for member in members),
            key=lambda value: SEVERITY_RANK.get(value, 0),
        )
        group = {
            "group_id": "g_" + hashlib.sha1(
                "{}:{}".format(root_cause, key).encode("utf-8")
            ).hexdigest()[:12],
            "root_cause": root_cause,
            "root_device_id": root_device_id,
            "root_device_name": root_device_name,
            "severity": severity,
            "title": _title_for(root_cause, root_device_name, members, links),
            "alert_ids": [member.get("id") for member in members],
            "member_count": len(members),
            "suppressed_count": 0,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "members": [
                _member_dict(member, name_by_id, suppressed_by=None)
                for member in members
            ],
            "_key": key,
            "_device_ids": _group_device_ids(members, root_device_id, device_ids),
        }
        groups.append(group)

    # ---------- 2) 下游抑制（仅 active 状态；展示层）----------
    if state == "active":
        groups = _apply_downstream_suppression(groups, adjacency, links, device_ids)
    for group in groups:
        group.pop("_key", None)
        group.pop("_device_ids", None)

    # ---------- 3) 排序：根因优先级 > 严重级 > 最近发生 ----------
    groups.sort(
        key=lambda group: (
            -ROOT_CAUSE_PRIORITY.get(group["root_cause"], -1),
            -SEVERITY_RANK.get(group["severity"], 0),
            -float(group["last_seen"]),
        )
    )
    return {
        "count": len(groups),
        "state": state,
        "generated_at": time.time(),
        "groups": groups,
    }


def _member_dict(
    member: Dict[str, Any],
    name_by_id: Dict[str, str],
    suppressed_by: Optional[str],
) -> Dict[str, Any]:
    device_id = member.get("device_id")
    device_name = name_by_id.get(str(device_id)) if device_id else None
    return {
        "id": member.get("id"),
        "alert_type": member.get("alert_type"),
        "link_id": member.get("link_id"),
        "issue_id": member.get("issue_id"),
        "severity": member.get("severity"),
        "device_id": device_id,
        "device_name": device_name or (str(device_id) if device_id else None),
        "title": member.get("title"),
        "detail": member.get("detail"),
        "observed_at": member.get("observed_at"),
        "dedup_key": member.get("dedup_key"),
        "state": member.get("state"),
        "suppressed_by": suppressed_by,
    }


def _title_for(
    root_cause: str,
    root_device_name: Optional[str],
    members: List[Dict[str, Any]],
    links: Dict[str, Dict[str, Any]],
) -> str:
    name = root_device_name or "未知设备"
    if root_cause == "link_down":
        link_id = str((members[0] or {}).get("link_id") or "")
        link = links.get(link_id) or {}
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if source and target:
            return "{} ↔ {} 链路中断".format(
                name or source, target
            )
        return "{} 链路中断".format(name)
    if root_cause == "device_unreachable":
        return "{} 不可达".format(name)
    if root_cause == "device_offline":
        return "{} 离线".format(name)
    if root_cause == "host_down":
        return "{} 主机离线".format(name)
    if root_cause == "health_issue":
        issue = (members[0] or {}).get("issue_id") if members else None
        return "{} 健康问题（{}）".format(name, issue or "NC issue")
    return "{} 告警组".format(name)


def _apply_downstream_suppression(
    groups: List[Dict[str, Any]],
    adjacency: Dict[str, Set[str]],
    links: Dict[str, Dict[str, Any]],
    device_ids: Set[str],
) -> List[Dict[str, Any]]:
    """根因组抑制其直接下游设备/主机的告警组（仅展示层合并）。

    只按“根因实体”匹配，不做全网 BFS，避免核心链路故障把全校园都判成下游：
    - link_down：受影响设备 = 该链路两端设备；
    - device_unreachable：受影响设备 = 该设备本身。
    主机/终端告警在生成时已带 ``device_id=接入设备``，因此会被自然命中。
    """
    root_groups = [
        group
        for group in groups
        if group["root_cause"] in SUPPRESSING_ROOT_CAUSES
        and group.get("root_device_id")
    ]
    if not root_groups:
        return groups
    root_groups.sort(
        key=lambda group: (
            -ROOT_CAUSE_PRIORITY.get(group["root_cause"], 0),
            float(group["first_seen"]),
        )
    )

    affected: Dict[str, Set[str]] = {}
    for group in root_groups:
        affected_ids: Set[str] = set()
        if group["root_cause"] == "link_down":
            link_ids = {
                str(member.get("link_id"))
                for member in group["members"]
                if member.get("link_id")
            }
            for link_id in link_ids:
                link = links.get(link_id) or {}
                for key in ("source", "target"):
                    value = link.get(key)
                    if value:
                        affected_ids.add(str(value))
        if not affected_ids and group.get("root_device_id"):
            affected_ids.add(str(group["root_device_id"]))
        affected[group["group_id"]] = affected_ids

    claimed: Dict[str, str] = {}
    for group in groups:
        group_devices = _group_device_ids_from_members(group)
        if not group_devices:
            continue
        for root in root_groups:
            # 根因组不能抑制自己；但高优先级根因（排在前面的 link_down）可以
            # 抑制另一个较低优先级根因组（如 device_unreachable）。
            if group["group_id"] == root["group_id"]:
                continue
            if group_devices & affected.get(root["group_id"], set()):
                claimed[group["group_id"]] = root["group_id"]
                break

    if not claimed:
        return groups

    by_id = {group["group_id"]: group for group in groups}
    suppressed_groups: Set[str] = set()
    for suppressed_id, root_id in claimed.items():
        root = by_id[root_id]
        suppressed_group = by_id[suppressed_id]
        if suppressed_id in suppressed_groups:
            continue
        suppressed_groups.add(suppressed_id)
        for member in suppressed_group["members"]:
            member = dict(member)
            member["suppressed_by"] = root_id
            root["members"].append(member)
        root["suppressed_count"] += suppressed_group["member_count"]
        root["last_seen"] = max(root["last_seen"], suppressed_group["last_seen"])
        root["severity"] = max(
            root["severity"],
            suppressed_group["severity"],
            key=lambda value: SEVERITY_RANK.get(value, 0),
        )
    for group in groups:
        if group["suppressed_count"]:
            group["title"] = "{}（{} 项下游告警被抑制）".format(
                group["title"].split("（")[0], group["suppressed_count"]
            )
    return [group for group in groups if group["group_id"] not in suppressed_groups]


def _group_device_ids_from_members(group: Dict[str, Any]) -> Set[str]:
    ids: Set[str] = set()
    for member in group.get("members", []):
        device_id = member.get("device_id")
        if device_id:
            ids.add(str(device_id))
    if group.get("root_device_id"):
        ids.add(str(group["root_device_id"]))
    return ids
