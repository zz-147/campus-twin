# -*- coding: utf-8 -*-
"""W9 场景配置服务 + 新设备检测（配置驱动 3D 场景）。

设计契约：``docs/W9-Agent扩展设计.md`` §三.2 / §三.3。

职责
====
1. **场景配置存储**：SQLite ``scene_config`` 单行 JSON 文档（version 每次写 +1）；
   首次访问自动 seed 默认 4 楼栋 / 12 设备（中心机房、教学楼、实训楼、宿舍楼，
   坐标取自 ``docs/校园网络拓扑设计.md`` §九/§十 与前端低模布局）。
2. **场景 CRUD**：新增楼栋、设备 upsert（含同楼坐标碰撞检测/自动偏移）、
   确认 pending 设备（→ confirmed）、删除设备（版本化 tombstone）、重置默认。
3. **新设备检测**：每轮采集后把 inventory（NC /network-device）与 scene_config
   比对，输出未纳管设备的 pending 列表（含管理 IP / 型号 / 可达性 / 拓扑连接关系），
   并给出五级归属推断中的前三级（拓扑邻居 → hostname 关键词 → 管理 IP/VLAN 段），
   供 Agent 侦察与前端提示使用。
4. **审计**：所有写操作记录 operator/action/target/detail（由 Store.add_audit）。

约定
====
- 坐标：楼栋 ``position={x,z}``；设备 ``position={x,y,z}`` 为 **世界坐标**
  （与前端低模布局一致，y 默认楼顶高度 +3.8），前端可直接使用。
- 设备 ``status``：``pending``（新设备待确认，前端浅色/虚线）或 ``confirmed``。
- ``source``：``seed``（默认场景）/ ``manual``（API 写入）/ ``agent``（Agent 写入）。
"""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, Sequence

from app.models import Device, Topology
from app.store import Store

logger = logging.getLogger("campus_twin.scene")

# 同楼设备标记的最小间距（世界坐标单位≈米）；冲突时自动沿 x 轴偏移
MIN_DEVICE_SPACING = 8.0
MAX_OFFSET_ATTEMPTS = 40
# 前端低模布局参数（与 frontend/src/scene/layout.js 保持一致）
MAX_ITEMS_PER_ROW = 4
ROOF_OFFSET_Y = 3.8

THEME = {
    "name": "low-poly-campus",
    "style": "low-poly",
    "background": "#070b14",
    "accent": "#38bdf8",
}

# 默认 4 楼栋 / 12 设备（来源：docs/校园网络拓扑设计.md §九/§十 最终落地记录）
_DEFAULT_BUILDINGS: Sequence[Dict[str, Any]] = (
    {
        "id": "core",
        "name": "中心机房",
        "position": {"x": 0.0, "z": 0.0},
        "size": {"width": 48.0, "depth": 36.0, "height": 16.0},
        "style": {"color": "#2f6f9e", "opacity": 0.35, "borderColor": "#7dd3fc"},
        "devices": (
            ("Core-3650", "switch", "Catalyst 3650", "192.168.10.1"),
            ("Router-4331", "router", "ISR 4331", "10.0.0.2"),
            ("Server-1", "server", "Server-PT", "192.168.50.10"),
            ("NC", "controller", "Network Controller", "192.168.10.100"),
        ),
    },
    {
        "id": "teach",
        "name": "教学楼",
        "position": {"x": -120.0, "z": 40.0},
        "size": {"width": 78.0, "depth": 30.0, "height": 24.0},
        "style": {"color": "#2a8a7e", "opacity": 0.35, "borderColor": "#7dd3fc"},
        "devices": (
            ("SW-JX-2960", "switch", "Catalyst 2960", "192.168.10.11"),
            ("PC-JX1", "pc", "PC-PT", "192.168.20.10"),
            ("PC-JX2", "pc", "PC-PT", "192.168.20.11"),
        ),
    },
    {
        "id": "train",
        "name": "实训楼",
        "position": {"x": 120.0, "z": 40.0},
        "size": {"width": 62.0, "depth": 30.0, "height": 20.0},
        "style": {"color": "#7a63b6", "opacity": 0.35, "borderColor": "#7dd3fc"},
        "devices": (
            ("SW-SX-2960", "switch", "Catalyst 2960", "192.168.10.12"),
            ("PC-SX1", "pc", "PC-PT", "192.168.30.10"),
        ),
    },
    {
        "id": "dorm",
        "name": "宿舍楼",
        "position": {"x": 0.0, "z": -140.0},
        "size": {"width": 66.0, "depth": 26.0, "height": 21.0},
        "style": {"color": "#9a783f", "opacity": 0.35, "borderColor": "#7dd3fc"},
        "devices": (
            ("SW-SS-2960", "switch", "Catalyst 2960", "192.168.10.13"),
            ("PC-SS1", "pc", "PC-PT", "192.168.40.10"),
            ("PC-SS2", "pc", "PC-PT", "192.168.40.11"),
        ),
    },
)

# 归属推断第 3 级：管理 IP / VLAN 段映射（与 docs IP 规划一致）
DEFAULT_IP_BUILDING_MAP: Dict[str, str] = {
    "192.168.20.": "teach",   # VLAN20 教学楼
    "192.168.30.": "train",   # VLAN30 实训楼
    "192.168.40.": "dorm",    # VLAN40 宿舍楼
    "192.168.50.": "core",    # SERVERS 中心机房
    "192.168.10.": "core",    # MGMT 管理网（设备默认中心机房）
}

# 归属推断第 2 级：hostname 关键词
DEFAULT_HOSTNAME_RULES: List[Dict[str, Any]] = [
    {"keywords": ["JX", "教学"], "building_id": "teach"},
    {"keywords": ["SX", "实训"], "building_id": "train"},
    {"keywords": ["SS", "宿舍"], "building_id": "dorm"},
    {"keywords": ["CORE", "核心", "机房"], "building_id": "core"},
    {"keywords": ["ROUTER", "路由", "NC"], "building_id": "core"},
]

_BUILDING_ID_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class SceneError(Exception):
    """场景配置业务异常基类（main 层映射为 HTTP 状态码）。"""

    status_code = 400


class SceneNotFound(SceneError):
    status_code = 404


class SceneConflict(SceneError):
    status_code = 409


def normalize_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _slot_position(building: Dict[str, Any], index: int, count: int) -> Dict[str, float]:
    """按前端低模布局算法计算第 index 个设备标记的世界坐标。"""
    position = building.get("position") or {"x": 0.0, "z": 0.0}
    size = building.get("size") or {"width": 40.0, "depth": 30.0, "height": 16.0}
    total = max(int(count), 1)
    per_row = min(total, MAX_ITEMS_PER_ROW)
    rows = int(math.ceil(total / float(per_row)))
    row = index // per_row
    col = index % per_row
    items_in_row = min(per_row, total - row * per_row)
    usable_width = float(size.get("width", 40.0)) - 12.0
    spacing = min(15.0, usable_width / (items_in_row - 1)) if items_in_row > 1 else 0.0
    x = float(position.get("x", 0.0)) + (col - (items_in_row - 1) / 2.0) * spacing
    z = float(position.get("z", 0.0)) + (row - (rows - 1) / 2.0) * 12.0
    y = float(size.get("height", 16.0)) + ROOF_OFFSET_Y
    return {"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}


def build_default_scene() -> Dict[str, Any]:
    """生成默认场景文档（4 楼栋 / 12 设备，version=1）。"""
    buildings: List[Dict[str, Any]] = []
    for spec in _DEFAULT_BUILDINGS:
        building = {
            "id": spec["id"],
            "name": spec["name"],
            "position": dict(spec["position"]),
            "size": dict(spec["size"]),
            "style": dict(spec["style"]),
            "status": "confirmed",
            "source": "seed",
            "devices": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        device_specs = list(spec["devices"])
        for index, (name, dtype, model, mgmt_ip) in enumerate(device_specs):
            building["devices"].append(
                {
                    "name": name,
                    "type": dtype,
                    "position": _slot_position(building, index, len(device_specs)),
                    "status": "confirmed",
                    "source": "seed",
                    "model": model,
                    "mgmt_ip": mgmt_ip,
                }
            )
        buildings.append(building)
    return {
        "version": 1,
        "updated_at": time.time(),
        "theme": dict(THEME),
        "device_assignment": {
            "hostname_rules": [dict(rule) for rule in DEFAULT_HOSTNAME_RULES],
            "ip_building_map": dict(DEFAULT_IP_BUILDING_MAP),
        },
        "buildings": buildings,
        "removed_devices": [],
    }


class SceneService:
    """场景配置读写 + 新设备检测（单例，由 app lifespan 创建并共享给 pipeline）。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._pending_devices: List[Dict[str, Any]] = []
        self._pending_updated_at: Optional[float] = None

    # ------------------------------------------------------------------ #
    # 读取 / seed
    # ------------------------------------------------------------------ #
    def get_scene(self) -> Dict[str, Any]:
        """读取场景文档；首次访问自动 seed 默认 4 楼栋 / 12 设备。"""
        document = self.store.load_scene_document()
        if document is None:
            document = build_default_scene()
            document["updated_at"] = time.time()
            self.store.save_scene_document(document)
            logger.info(
                "scene_config 首次初始化：4 楼栋 / %d 设备（version=1）",
                sum(len(b.get("devices", [])) for b in document["buildings"]),
            )
        return document

    def current_version(self) -> int:
        return self.store.scene_version()

    # ------------------------------------------------------------------ #
    # 写操作（每个写操作 version+1 + 审计）
    # ------------------------------------------------------------------ #
    def add_building(
        self,
        operator: str,
        name: str,
        position: Optional[Dict[str, float]] = None,
        size: Optional[Dict[str, float]] = None,
        style: Optional[Dict[str, Any]] = None,
        status: str = "pending",
        building_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        document = self.get_scene()
        clean_name = str(name or "").strip()
        if not clean_name:
            raise SceneConflict("楼栋 name 不能为空")
        if any(
            normalize_name(b.get("name")) == normalize_name(clean_name)
            for b in document["buildings"]
        ):
            raise SceneConflict("楼栋已存在：{}".format(clean_name))
        used_ids = {b.get("id") for b in document["buildings"]}
        if building_id:
            new_id = _BUILDING_ID_RE.sub("-", str(building_id)).strip("-") or None
            if not new_id:
                raise SceneConflict("楼栋 id 非法")
            if new_id in used_ids:
                raise SceneConflict("楼栋 id 已存在：{}".format(new_id))
        else:
            index = len(document["buildings"]) + 1
            new_id = "bld-{}".format(index)
            while new_id in used_ids:
                index += 1
                new_id = "bld-{}".format(index)
        palette = ["#2f6f9e", "#2a8a7e", "#7a63b6", "#9a783f", "#b45f5f", "#4f7d4f"]
        now = time.time()
        building = {
            "id": new_id,
            "name": clean_name,
            "position": {
                "x": float((position or {}).get("x", 0.0)),
                "z": float((position or {}).get("z", 0.0)),
            },
            "size": {
                "width": float((size or {}).get("width", 40.0)),
                "depth": float((size or {}).get("depth", 30.0)),
                "height": float((size or {}).get("height", 16.0)),
            },
            "style": dict(
                style
                or {
                    "color": palette[len(document["buildings"]) % len(palette)],
                    "opacity": 0.35,
                    "borderColor": "#7dd3fc",
                }
            ),
            "status": "confirmed" if status == "confirmed" else "pending",
            "source": "manual",
            "devices": [],
            "created_at": now,
            "updated_at": now,
        }
        document["buildings"].append(building)
        document = self._commit(
            document,
            operator,
            action="scene.building.add",
            target=new_id,
            detail={"name": clean_name, "status": building["status"]},
        )
        logger.info("场景新增楼栋：%s（%s，operator=%s）", clean_name, new_id, operator)
        return {"ok": True, "version": document["version"], "building": building}

    def upsert_device(
        self,
        operator: str,
        building_id: str,
        name: str,
        device_type: Optional[str] = None,
        position: Optional[Dict[str, float]] = None,
        status: Optional[str] = None,
        source: Optional[str] = "manual",
        model: Optional[str] = None,
        mgmt_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        document = self.get_scene()
        building = self._find_building(document, building_id)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise SceneConflict("设备 name 不能为空")

        previous = self._find_device(document, clean_name)
        previous_building_id = previous[0] if previous else None
        previous_device = previous[1] if previous else None

        base_position = self._device_position(
            building, position, fallback_index=len(building.get("devices", []))
        )
        final_position, adjusted, message = self._avoid_collision(
            building, base_position, ignore_name=clean_name
        )
        now = time.time()
        device = {
            "name": clean_name,
            "type": (device_type or (previous_device or {}).get("type") or "unknown"),
            "position": final_position,
            "status": (
                status
                or (previous_device or {}).get("status")
                or "pending"
            ),
            "source": source or (previous_device or {}).get("source") or "manual",
            "model": model if model is not None else (previous_device or {}).get("model"),
            "mgmt_ip": (
                mgmt_ip if mgmt_ip is not None else (previous_device or {}).get("mgmt_ip")
            ),
            "updated_at": now,
        }
        if device["status"] == "confirmed":
            device["confirmed_at"] = now
            device["confirmed_by"] = operator
        if previous_device is None:
            device["created_at"] = now
        else:
            device["created_at"] = previous_device.get("created_at", now)

        # 同名设备全局唯一：先摘掉旧位置，再加入目标楼栋
        for candidate in document["buildings"]:
            candidate["devices"] = [
                item
                for item in candidate.get("devices", [])
                if normalize_name(item.get("name")) != normalize_name(clean_name)
            ]
        building["devices"].append(device)
        building["updated_at"] = now
        # 重新加入时清掉历史 tombstone
        document["removed_devices"] = [
            tombstone
            for tombstone in document.get("removed_devices", [])
            if normalize_name(tombstone.get("name")) != normalize_name(clean_name)
        ]
        document = self._commit(
            document,
            operator,
            action="scene.device.upsert",
            target=clean_name,
            detail={
                "building_id": building_id,
                "previous_building_id": previous_building_id,
                "status": device["status"],
                "adjusted": adjusted,
            },
        )
        logger.info(
            "场景设备 upsert：%s -> %s（status=%s adjusted=%s operator=%s）",
            clean_name,
            building_id,
            device["status"],
            adjusted,
            operator,
        )
        return {
            "ok": True,
            "version": document["version"],
            "building_id": building_id,
            "device": device,
            "adjusted": adjusted,
            "message": message,
        }

    def confirm_device(self, operator: str, name: str) -> Dict[str, Any]:
        document = self.get_scene()
        found = self._find_device(document, name)
        if found is None:
            raise SceneNotFound("场景中不存在设备：{}".format(name))
        building_id, device = found
        if device.get("status") == "confirmed":
            return {
                "ok": True,
                "version": document["version"],
                "changed": False,
                "building_id": building_id,
                "device": device,
            }
        now = time.time()
        device["status"] = "confirmed"
        device["confirmed_at"] = now
        device["confirmed_by"] = operator
        device["updated_at"] = now
        for building in document["buildings"]:
            if building.get("id") == building_id:
                building["updated_at"] = now
                break
        document = self._commit(
            document,
            operator,
            action="scene.device.confirm",
            target=str(device.get("name")),
            detail={"building_id": building_id},
        )
        logger.info("场景设备确认：%s（operator=%s）", device.get("name"), operator)
        return {
            "ok": True,
            "version": document["version"],
            "changed": True,
            "building_id": building_id,
            "device": device,
        }

    def delete_device(self, operator: str, name: str) -> Dict[str, Any]:
        document = self.get_scene()
        found = self._find_device(document, name)
        if found is None:
            raise SceneNotFound("场景中不存在设备：{}".format(name))
        building_id, device = found
        now = time.time()
        for building in document["buildings"]:
            if building.get("id") == building_id:
                building["devices"] = [
                    item
                    for item in building.get("devices", [])
                    if normalize_name(item.get("name")) != normalize_name(name)
                ]
                building["updated_at"] = now
                break
        tombstones = document.setdefault("removed_devices", [])
        tombstones.append(
            {
                "name": device.get("name"),
                "building_id": building_id,
                "removed_at": now,
                "operator": operator,
            }
        )
        # 只保留最近 200 条 tombstone，避免 JSON 无限增长
        document["removed_devices"] = tombstones[-200:]
        document = self._commit(
            document,
            operator,
            action="scene.device.delete",
            target=str(device.get("name")),
            detail={"building_id": building_id},
        )
        logger.info("场景设备删除：%s（operator=%s）", device.get("name"), operator)
        return {
            "ok": True,
            "version": document["version"],
            "deleted": device.get("name"),
            "building_id": building_id,
            "removed_at": now,
        }

    def reset(self, operator: str) -> Dict[str, Any]:
        current = self.get_scene()
        document = build_default_scene()
        document["version"] = int(current.get("version", 1)) + 1
        document["updated_at"] = time.time()
        self.store.save_scene_document(document)
        self.store.add_audit(
            operator=operator,
            action="scene.reset",
            target="scene",
            detail={"restored_version": document["version"]},
        )
        logger.info("场景配置已重置为默认 4 楼栋/12 设备（operator=%s）", operator)
        return {
            "ok": True,
            "version": document["version"],
            "building_count": len(document["buildings"]),
            "device_count": sum(len(b.get("devices", [])) for b in document["buildings"]),
        }

    # ------------------------------------------------------------------ #
    # 新设备检测（每轮采集后由 pipeline 调用）
    # ------------------------------------------------------------------ #
    def refresh_pending(
        self,
        devices: Sequence[Device],
        topology: Optional[Topology] = None,
    ) -> List[Dict[str, Any]]:
        """比对 inventory 与 scene_config，刷新待确认新设备列表（含归属推断）。"""
        document = self.get_scene()
        pending = self.compute_pending(devices, topology, document)
        self._pending_devices = pending
        self._pending_updated_at = time.time()
        if pending:
            logger.info("检测到 %d 台未纳入场景的新设备：%s", len(pending),
                        ", ".join(item["name"] for item in pending[:5]))
        return pending

    def pending_devices(self) -> Dict[str, Any]:
        """返回最近一次采集计算出的待确认新设备列表。"""
        return {
            "count": len(self._pending_devices),
            "updated_at": self._pending_updated_at,
            "updated_at_iso": _iso(self._pending_updated_at),
            "devices": list(self._pending_devices),
        }

    def compute_pending(
        self,
        devices: Sequence[Device],
        topology: Optional[Topology],
        document: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        document = document or self.get_scene()
        known_names = {
            normalize_name(device.get("name"))
            for building in document["buildings"]
            for device in building.get("devices", [])
        }
        by_id = {device.id: device for device in devices}
        result: List[Dict[str, Any]] = []
        for device in devices:
            name = (device.hostname or device.management_ip or device.id).strip()
            if normalize_name(name) in known_names:
                continue
            connected_to = self._connections(device, topology, by_id)
            inferred = self.infer_building(
                name=name,
                mgmt_ip=device.management_ip,
                topology=topology,
                device_id=device.id,
                document=document,
                devices=devices,
            )
            result.append(
                {
                    "name": name,
                    "device_id": device.id,
                    "hostname": device.hostname,
                    "mgmt_ip": device.management_ip,
                    "platform": device.platform_id or device.product_id,
                    "product_id": device.product_id,
                    "role": device.role,
                    "reachability": device.reachability,
                    "collection_status": device.collection_status,
                    "connected_to": connected_to,
                    "inferred_building": inferred,
                    "in_scene": False,
                    "suggested_status": "pending",
                }
            )
        result.sort(key=lambda item: item["name"])
        return result

    def infer_building(
        self,
        *,
        name: str,
        mgmt_ip: Optional[str] = None,
        topology: Optional[Topology] = None,
        device_id: Optional[str] = None,
        document: Optional[Dict[str, Any]] = None,
        devices: Optional[Sequence[Device]] = None,
    ) -> Optional[Dict[str, Any]]:
        """五级归属推断的前三级（供 Agent 侦察 / 前端提示）。

        1. 拓扑邻居：新设备连到的已纳管设备所在楼栋；
        2. hostname 关键词：JX/教学、SX/实训、SS/宿舍、CORE/机房 等；
        3. 管理 IP / VLAN 段映射。
        推断不出返回 None（交由 Agent/用户决定，不自动建楼）。
        """
        document = document or self.get_scene()
        building_by_id = {b["id"]: b for b in document.get("buildings", [])}
        assignment = document.get("device_assignment") or {}

        def _result(building_id: str, rule: str, confidence: float) -> Optional[Dict[str, Any]]:
            building = building_by_id.get(building_id)
            if building is None:
                return None
            return {
                "building_id": building_id,
                "building_name": building.get("name"),
                "rule": rule,
                "confidence": confidence,
            }

        # ---- 1) 拓扑邻居（新设备连到的已纳管设备 → 继承其楼栋）----
        if topology is not None and device_id:
            scene_name_to_building = {}
            for building in document.get("buildings", []):
                for device in building.get("devices", []):
                    scene_name_to_building[normalize_name(device.get("name"))] = building["id"]
            device_by_id = {device.id: device for device in (devices or [])}
            for link in topology.links:
                peer_id = None
                if link.source == device_id:
                    peer_id = link.target
                elif link.target == device_id:
                    peer_id = link.source
                if not peer_id:
                    continue
                peer = device_by_id.get(peer_id)
                peer_name = peer.hostname if peer is not None else peer_id
                building_id = scene_name_to_building.get(normalize_name(peer_name))
                if building_id:
                    return _result(building_id, "topology_neighbor", 0.9)

        # ---- 2) hostname 关键词 ----
        upper_name = str(name or "").upper()
        for rule in assignment.get("hostname_rules") or DEFAULT_HOSTNAME_RULES:
            keywords = [str(keyword).upper() for keyword in rule.get("keywords", [])]
            if any(keyword and keyword in upper_name for keyword in keywords):
                inferred = _result(rule.get("building_id"), "hostname_keyword", 0.8)
                if inferred:
                    return inferred

        # ---- 3) 管理 IP / VLAN 段映射 ----
        ip_map = assignment.get("ip_building_map") or DEFAULT_IP_BUILDING_MAP
        if mgmt_ip:
            for prefix in sorted(ip_map, key=len, reverse=True):
                if str(mgmt_ip).startswith(prefix):
                    inferred = _result(ip_map[prefix], "ip_prefix", 0.6)
                    if inferred:
                        return inferred
        return None

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _find_building(document: Dict[str, Any], building_id: str) -> Dict[str, Any]:
        for building in document.get("buildings", []):
            if building.get("id") == building_id:
                return building
        raise SceneNotFound("场景中不存在楼栋：{}".format(building_id))

    @staticmethod
    def _find_device(document: Dict[str, Any], name: str):
        target = normalize_name(name)
        for building in document.get("buildings", []):
            for device in building.get("devices", []):
                if normalize_name(device.get("name")) == target:
                    return building.get("id"), device
        return None

    @staticmethod
    def _device_position(
        building: Dict[str, Any],
        position: Optional[Dict[str, float]],
        fallback_index: int,
    ) -> Dict[str, float]:
        if position:
            y = position.get("y")
            if y is None:
                y = float((building.get("size") or {}).get("height", 16.0)) + ROOF_OFFSET_Y
            return {
                "x": float(position.get("x", 0.0)),
                "y": float(y),
                "z": float(position.get("z", 0.0)),
            }
        return _slot_position(
            building,
            fallback_index,
            max(fallback_index + 1, len(building.get("devices", [])) + 1),
        )

    @staticmethod
    def _avoid_collision(
        building: Dict[str, Any],
        position: Dict[str, float],
        ignore_name: Optional[str] = None,
    ):
        """同楼坐标碰撞检测：与已有设备间距 < MIN_DEVICE_SPACING 时自动沿 x 偏移。"""
        target = normalize_name(ignore_name) if ignore_name else None
        others = [
            device
            for device in building.get("devices", [])
            if normalize_name(device.get("name")) != target
        ]

        def conflict(candidate: Dict[str, float]) -> bool:
            for other in others:
                other_position = other.get("position") or {}
                if (
                    math.hypot(
                        float(candidate["x"]) - float(other_position.get("x", 0.0)),
                        float(candidate["z"]) - float(other_position.get("z", 0.0)),
                    )
                    < MIN_DEVICE_SPACING
                ):
                    return True
            return False

        candidate = dict(position)
        if not conflict(candidate):
            return candidate, False, ""
        base_x = float(position["x"])
        for attempt in range(1, MAX_OFFSET_ATTEMPTS + 1):
            direction = 1.0 if attempt % 2 == 1 else -1.0
            step = float((attempt + 1) // 2) * MIN_DEVICE_SPACING
            candidate = dict(position)
            candidate["x"] = round(base_x + direction * step, 2)
            if not conflict(candidate):
                return (
                    candidate,
                    True,
                    "坐标与已有设备冲突，已自动偏移 x{:+g}（同楼最小间距 {}）".format(
                        direction * step, MIN_DEVICE_SPACING
                    ),
                )
        # 理论上不会到这里；兜底放到一个远一点的位置并标记
        candidate = dict(position)
        candidate["x"] = round(base_x + MAX_OFFSET_ATTEMPTS * MIN_DEVICE_SPACING, 2)
        return candidate, True, "坐标冲突过多，已移动到楼栋边缘位置"

    @staticmethod
    def _connections(
        device: Device,
        topology: Optional[Topology],
        by_id: Dict[str, Device],
    ) -> List[Dict[str, Any]]:
        connections: List[Dict[str, Any]] = []
        if topology is None:
            return connections
        for link in topology.links:
            if link.source == device.id:
                peer_id, local_port, peer_port = link.target, link.start_port_name, link.end_port_name
            elif link.target == device.id:
                peer_id, local_port, peer_port = link.source, link.end_port_name, link.start_port_name
            else:
                continue
            peer = by_id.get(peer_id)
            connections.append(
                {
                    "device_id": peer_id,
                    "device_name": peer.hostname if peer is not None else peer_id,
                    "local_port": local_port,
                    "peer_port": peer_port,
                    "link_status": link.link_status,
                }
            )
        return connections

    def _commit(
        self,
        document: Dict[str, Any],
        operator: str,
        action: str,
        target: str,
        detail: Any,
    ) -> Dict[str, Any]:
        """版本 +1、落库、写审计（所有写操作统一出口）。"""
        document["version"] = int(document.get("version", 1)) + 1
        document["updated_at"] = time.time()
        self.store.save_scene_document(document)
        self.store.add_audit(operator=operator, action=action, target=target, detail=detail)
        return document


def _iso(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
