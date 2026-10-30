# -*- coding: utf-8 -*-
"""新设备归属推断：五级规则（拓扑邻居 → hostname 关键词 → 管理 IP/VLAN 段 → 询问用户 → 新楼栋）。

规则顺序由 docs/W9-Agent扩展设计.md §二 定义：
1. 拓扑邻居：新设备连到的已纳管设备 → 继承其所属楼栋
2. hostname 关键词：JX/教学 → 教学楼、SX/实训 → 实训楼、SS/宿舍 → 宿舍楼、CORE/机房 → 中心机房
3. 管理 IP / VLAN 段映射（192.168.20.0/24 → 教学楼 等）
4. 无法推断 → 返回 ``needs_user=True``，由主 Agent 在对话中询问用户
5. 用户确认新建楼栋 → 由场景构建 Agent 生成楼栋（``propose_new_building``）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.agent.knowledge import BUILDING_PROFILES

#: hostname 关键词（规则 2）
HOSTNAME_BUILDING_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("中心机房", ("CORE", "CENTER", "NC", "机房")),
    ("教学楼", ("JX", "JIAO", "TEACH")),
    ("实训楼", ("SX", "SHIXUN", "TRAIN")),
    ("宿舍楼", ("SS", "SUSHE", "DORM")),
)

#: 管理 IP / 网段映射（规则 3）
IP_BUILDING_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("教学楼", ("192.168.20.",)),
    ("实训楼", ("192.168.30.",)),
    ("宿舍楼", ("192.168.40.",)),
    ("中心机房", ("192.168.10.", "192.168.50.", "10.0.0.")),
)

#: VLAN 映射（规则 3 补充）
VLAN_BUILDING_RULES: Dict[str, str] = {
    "20": "教学楼",
    "30": "实训楼",
    "40": "宿舍楼",
    "10": "中心机房",
    "50": "中心机房",
}


@dataclass
class InferenceResult:
    """归属推断结果。"""

    device_name: str
    building: Optional[str] = None
    building_id: Optional[str] = None
    rule: str = "unknown"
    rule_index: int = 0
    confidence: str = "low"
    reason: str = ""
    needs_user: bool = False
    candidates: List[str] = field(default_factory=list)
    question: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_name": self.device_name,
            "building": self.building,
            "building_id": self.building_id,
            "rule": self.rule,
            "rule_index": self.rule_index,
            "confidence": self.confidence,
            "reason": self.reason,
            "needs_user": self.needs_user,
            "candidates": self.candidates,
            "question": self.question,
        }


# --------------------------------------------------------------------------- #
# 场景数据辅助
# --------------------------------------------------------------------------- #
def scene_buildings(scene: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(scene, dict):
        return []
    return [item for item in scene.get("buildings") or [] if isinstance(item, dict)]


def scene_building_names(scene: Optional[Dict[str, Any]]) -> List[str]:
    return [str(item.get("name")) for item in scene_buildings(scene) if item.get("name")]


def find_building(scene: Optional[Dict[str, Any]], name_or_hint: str) -> Optional[Dict[str, Any]]:
    """按名称/别名/包含关系在场景中找楼栋。"""
    hint = str(name_or_hint or "").strip().lower()
    if not hint:
        return None
    buildings = scene_buildings(scene)
    for building in buildings:
        if str(building.get("name") or "").strip().lower() == hint:
            return building
    # 中文关键词/拼音别名
    for canonical, profile in BUILDING_PROFILES.items():
        keys = [canonical] + list(profile.get("keywords") or ())
        if any(key and key.lower() in hint for key in keys):
            for building in buildings:
                name = str(building.get("name") or "")
                if canonical in name or any(key.lower() in name.lower() for key in keys):
                    return building
    for building in buildings:
        name = str(building.get("name") or "")
        if name and name.lower() in hint:
            return building
    return None


def building_id_for(scene: Optional[Dict[str, Any]], building_name: str) -> Optional[str]:
    building = find_building(scene, building_name)
    if building is not None:
        return str(building.get("id") or "") or None
    mapping = {
        "中心机房": "core",
        "教学楼": "teach",
        "实训楼": "train",
        "宿舍楼": "dorm",
        "图书馆": "bld-lib",
        "体育馆": "bld-gym",
    }
    return mapping.get(building_name)


def building_by_id(
    scene: Optional[Dict[str, Any]], building_id: str
) -> Optional[Dict[str, Any]]:
    needle = str(building_id or "").strip()
    if not needle:
        return None
    for building in scene_buildings(scene):
        if str(building.get("id") or "") == needle:
            return building
    return None


def scene_assignment(scene: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """后端 GET /api/scene 的 device_assignment（hostname_rules / ip_building_map）。"""
    if not isinstance(scene, dict):
        return {}
    assignment = scene.get("device_assignment")
    return assignment if isinstance(assignment, dict) else {}


def _assignment_building_name(
    scene: Optional[Dict[str, Any]], building_id: str
) -> Optional[str]:
    building = building_by_id(scene, building_id)
    if building is not None:
        return str(building.get("name") or "") or None
    known = {
        "core": "中心机房",
        "teach": "教学楼",
        "train": "实训楼",
        "dorm": "宿舍楼",
    }
    return known.get(str(building_id))


def scene_device_map(scene: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """name(lower) → {building, device}。"""
    mapping: Dict[str, Dict[str, Any]] = {}
    for building in scene_buildings(scene):
        for device in building.get("devices") or []:
            if not isinstance(device, dict):
                continue
            name = str(device.get("name") or "").strip()
            if name:
                mapping[name.lower()] = {"building": building, "device": device}
    return mapping


def scene_device_names(scene: Optional[Dict[str, Any]]) -> List[str]:
    return [
        str(device.get("name"))
        for building in scene_buildings(scene)
        for device in building.get("devices") or []
        if isinstance(device, dict) and device.get("name")
    ]


def inventory_new_devices(
    devices: List[Dict[str, Any]], scene: Optional[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """inventory 里有、但场景配置里没有的设备（待加入场景的新设备）。"""
    known = {name.lower() for name in scene_device_names(scene)}
    new_devices: List[Dict[str, Any]] = []
    for device in devices or []:
        name = str(device.get("hostname") or "").strip()
        if not name or name.lower() in known:
            continue
        new_devices.append(device)
    return new_devices


def normalize_alert_building(name: str) -> Optional[str]:
    return name if name in BUILDING_PROFILES else None


# --------------------------------------------------------------------------- #
# 五级推断
# --------------------------------------------------------------------------- #
def infer_building(
    device: Dict[str, Any],
    scene: Optional[Dict[str, Any]],
    topology_links: Optional[List[Dict[str, Any]]] = None,
    device_index: Optional[Dict[str, Dict[str, Any]]] = None,
    allow_new_building: bool = False,
) -> InferenceResult:
    """对单台新设备执行归属推断。

    :param device: inventory 设备对象（hostname/management_ip/vlan_id/id）
    :param scene: GET /api/scene 返回的场景配置
    :param topology_links: GET /api/topology 的 links（含 source_label/target_label 或 source/target）
    :param device_index: 设备 id/name → 设备对象（用于拓扑邻居解析）
    :param allow_new_building: 规则 5：是否允许在推断失败时提出"新建楼栋"建议
    """
    name = str(device.get("hostname") or device.get("name") or device.get("id") or "")
    result = InferenceResult(device_name=name)
    links = topology_links or []
    index = device_index or {}

    # ---------- 规则 0：设备已在场景中 → 直接返回当前归属 ----------
    for building in scene_buildings(scene):
        for scene_device in building.get("devices") or []:
            if str(scene_device.get("name") or "").strip().lower() == name.lower():
                result.building = str(building.get("name") or "") or None
                result.building_id = str(building.get("id") or "") or None
                result.rule = "scene_existing"
                result.rule_index = 0
                result.confidence = "high"
                result.reason = "设备已在场景「%s」中，沿用当前归属" % result.building
                return result

    # ---------- 规则 1：拓扑邻居楼栋 ----------
    neighbor_buildings: List[str] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        source = str(link.get("source_label") or link.get("source") or "")
        target = str(link.get("target_label") or link.get("target") or "")
        if not _same_device(source, name) and not _same_device(target, name) and not _same_device(
            str(link.get("source") or ""), str(device.get("id") or "")
        ):
            continue
        neighbor = target if _same_device(source, name) else source
        neighbor_device = index.get(neighbor.lower()) or index.get(neighbor)
        neighbor_name = str(
            (neighbor_device or {}).get("hostname") or neighbor_device and neighbor_device.get("name") or neighbor
        )
        building = _building_of_scene_device(scene, neighbor_name)
        if building:
            neighbor_buildings.append(building)
    if neighbor_buildings:
        # 取出现次数最多的邻居楼栋
        counts: Dict[str, int] = {}
        for building in neighbor_buildings:
            counts[building] = counts.get(building, 0) + 1
        best = sorted(counts.items(), key=lambda item: item[1], reverse=True)[0][0]
        result.building = best
        result.rule = "topology_neighbor"
        result.rule_index = 1
        result.confidence = "high"
        result.reason = "新设备的拓扑邻居已属于「%s」，按邻居继承归属" % best
        result.building_id = building_id_for(scene, best)
        return result

    # ---------- 规则 2：hostname 关键词（优先使用后端 device_assignment 规则） ----------
    upper = name.upper()
    assignment = scene_assignment(scene)
    for rule in assignment.get("hostname_rules") or []:
        if not isinstance(rule, dict):
            continue
        for keyword in rule.get("keywords") or []:
            if keyword and str(keyword).upper() in upper:
                building_name = _assignment_building_name(scene, str(rule.get("building_id")))
                if building_name:
                    result.building = building_name
                    result.rule = "hostname_keyword"
                    result.rule_index = 2
                    result.confidence = "medium"
                    result.reason = "hostname「%s」命中后端归属规则关键字「%s」→ %s" % (
                        name,
                        keyword,
                        building_name,
                    )
                    result.building_id = building_id_for(scene, building_name)
                    return result
    for building, keywords in HOSTNAME_BUILDING_RULES:
        for keyword in keywords:
            if keyword and keyword in upper:
                result.building = building
                result.rule = "hostname_keyword"
                result.rule_index = 2
                result.confidence = "medium"
                result.reason = "hostname「%s」包含关键字「%s」→ %s" % (name, keyword, building)
                result.building_id = building_id_for(scene, building)
                return result

    # ---------- 规则 3：管理 IP / VLAN 段（优先使用后端 ip_building_map） ----------
    management_ip = str(device.get("management_ip") or device.get("ip") or "")
    for prefix, building_id in (assignment.get("ip_building_map") or {}).items():
        if management_ip.startswith(str(prefix)):
            building_name = _assignment_building_name(scene, str(building_id))
            if building_name:
                result.building = building_name
                result.rule = "ip_segment"
                result.rule_index = 3
                result.confidence = "medium"
                result.reason = "管理 IP %s 命中后端网段映射 %s → %s" % (
                    management_ip,
                    prefix,
                    building_name,
                )
                result.building_id = building_id_for(scene, building_name)
                return result
    for building, prefixes in IP_BUILDING_RULES:
        for prefix in prefixes:
            if management_ip.startswith(prefix):
                result.building = building
                result.rule = "ip_segment"
                result.rule_index = 3
                result.confidence = "medium"
                result.reason = "管理 IP %s 命中网段 %s → %s" % (management_ip, prefix, building)
                result.building_id = building_id_for(scene, building)
                return result
    vlan_id = str(device.get("vlan_id") or device.get("vlan") or "").strip()
    if vlan_id in VLAN_BUILDING_RULES:
        result.building = VLAN_BUILDING_RULES[vlan_id]
        result.rule = "vlan_segment"
        result.rule_index = 3
        result.confidence = "medium"
        result.reason = "VLAN %s 命中楼栋映射 → %s" % (vlan_id, result.building)
        result.building_id = building_id_for(scene, result.building)
        return result

    # ---------- 规则 4：无法推断 → 询问用户 ----------
    result.rule = "need_user"
    result.rule_index = 4
    result.confidence = "low"
    result.needs_user = True
    result.candidates = scene_building_names(scene)
    result.reason = "拓扑邻居/hostname/IP/VLAN 均无法匹配到现有楼栋"
    result.question = (
        "发现新设备 %s（管理 IP：%s），无法自动判断它属于哪栋楼。"
        "请告诉我是%s中的哪一个，或者说“新建一栋 X 楼”。"
        % (name, management_ip or "未知", "、".join(result.candidates) or "现有楼栋")
    )

    # ---------- 规则 5：建议新建楼栋 ----------
    if allow_new_building:
        result.rule = "new_building"
        result.rule_index = 5
        result.reason = "用户确认后可按新楼栋处理（场景构建 Agent 会生成楼栋配置）"
    return result


def propose_new_building(name: str) -> Dict[str, Any]:
    """规则 5：为无法归入现有楼栋的设备生成新楼栋建议（配置由 scene Agent 补全）。"""
    from app.agent.knowledge import building_profile

    profile = building_profile(name)
    return {
        "name": name,
        "vlan": profile["vlan"],
        "hostname_prefix": profile["hostname_prefix"],
        "reason": "现有楼栋无法容纳该设备，建议新建楼栋「%s」并分配 VLAN %s"
        % (name, profile["vlan"]),
    }


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
def _same_device(candidate: str, name: str) -> bool:
    if not candidate or not name:
        return False
    return candidate.strip().lower() == name.strip().lower()


def _building_of_scene_device(
    scene: Optional[Dict[str, Any]], device_name: str
) -> Optional[str]:
    needle = str(device_name or "").strip().lower()
    if not needle:
        return None
    for building in scene_buildings(scene):
        for device in building.get("devices") or []:
            if str(device.get("name") or "").strip().lower() == needle:
                return str(building.get("name") or "") or None
    return None
