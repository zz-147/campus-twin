# -*- coding: utf-8 -*-
"""场景构建 Agent（scene）：决定新设备/新楼栋在 3D 场景中的位置、尺寸、配色、图标。

数据契约对齐真实后端 ``GET /api/scene``：
- 楼栋：``{id, name, position:{x,z}, size:{width,depth,height}, style:{color,borderColor,opacity}, status, source, devices[]}``
- 设备：``{name, type, position:{x,y,z}(世界坐标), status, source, model, mgmt_ip}``

按设计文档要求，本角色是**纯计算**（只读 get_scene，不写任何数据）：
- 为 onboarding/recon 提供 ``propose_device`` / ``propose_building`` 摆放建议；
- 自身对话入口返回当前场景概览与布局建议。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.agent.knowledge import DEVICE_SPECS, building_profile, scene_type_label
from app.agent.roles.base import BaseRole, RoleContext, RoleResult

#: 低模风格配色（与前端/后端 seed 的 4 楼栋风格一致：半透明 box + 发光描边）
BUILDING_PALETTE = [
    {"color": "#1f3b57", "borderColor": "#7dd3fc", "opacity": 0.35},
    {"color": "#2a8a7e", "borderColor": "#7dd3fc", "opacity": 0.35},
    {"color": "#7a63b6", "borderColor": "#7dd3fc", "opacity": 0.35},
    {"color": "#9a783f", "borderColor": "#7dd3fc", "opacity": 0.35},
    {"color": "#2f5d50", "borderColor": "#7dd3fc", "opacity": 0.35},
    {"color": "#5d4b2f", "borderColor": "#7dd3fc", "opacity": 0.35},
]

#: 设备图标类型（前端按 type 渲染几何体）
SCENE_TYPE_BY_KIND = {
    "2960": "switch",
    "3650": "switch",
    "4331": "router",
    "PC": "pc",
    "Server": "server",
}

#: 新楼栋的默认尺寸（与后端 seed 楼栋比例一致）
NEW_BUILDING_SIZE = {"width": 64.0, "depth": 60.0, "height": 24.0}

#: 默认 4 楼栋的布局参考（新增楼栋依次排在其后）
DEFAULT_BUILDING_POSITIONS = [
    {"x": 0.0, "z": 0.0},
    {"x": -120.0, "z": 40.0},
    {"x": 120.0, "z": 40.0},
    {"x": 0.0, "z": -140.0},
]


def scene_type_for_device_kind(kind_key: str) -> str:
    if kind_key in SCENE_TYPE_BY_KIND:
        return SCENE_TYPE_BY_KIND[kind_key]
    spec = DEVICE_SPECS.get(kind_key)
    return str((spec or {}).get("scene_type") or "switch")


class SceneRole(BaseRole):
    """场景构建子 Agent：纯计算摆放方案，不写场景配置。"""

    name = "scene"
    label = "场景构建 Agent"
    allowed_tools = ("get_scene",)

    # ------------------------------------------------------------------ #
    # 对话入口
    # ------------------------------------------------------------------ #
    def run(self, ctx: RoleContext) -> RoleResult:
        started = self._start()
        scene = self._fetch_scene(ctx)
        buildings = [item for item in scene.get("buildings") or [] if isinstance(item, dict)]
        lines = [
            "## 当前 3D 场景",
            "- 场景版本：v%s；楼栋 %d 个；主题：%s"
            % (
                scene.get("version"),
                len(buildings),
                (scene.get("theme") or {}).get("style", "low-poly"),
            ),
            "",
            "| 楼栋 | 场景 ID | 位置 (x,z) | 尺寸 (w×d×h) | 设备数 | 设备 |",
            "|---|---|---|---|---|---|",
        ]
        for building in buildings:
            devices = [
                str(device.get("name"))
                for device in building.get("devices") or []
                if isinstance(device, dict)
            ]
            position = building.get("position") or {}
            size = building.get("size") or {}
            lines.append(
                "| %s | %s | (%s, %s) | %s×%s×%s | %d | %s |"
                % (
                    building.get("name"),
                    building.get("id"),
                    position.get("x"),
                    position.get("z"),
                    size.get("width"),
                    size.get("depth"),
                    size.get("height"),
                    len(devices),
                    "、".join(devices),
                )
            )
        lines.extend(
            [
                "",
                "## 摆放规则（低模风格）",
                "- 楼栋：半透明 BoxGeometry + 中文标签 Sprite；新楼栋按现有 4 栋的间距依次排在场景外侧。",
                "- 设备：交换机=小盒子、路由器=圆柱、PC=小方块、服务器=机架条；pending 设备浅色/虚线。",
                "- 同楼设备沿楼栋中心线网格摆放（间距 15），避让已有设备；设备坐标为世界坐标（y≈楼高/2+4）。",
                "",
                "如需给某台新设备生成位置，请说「把 <设备名> 加到 <楼栋> 场景里」。",
            ]
        )
        return self._finish(
            ctx,
            started,
            "\n".join(lines),
            data={"version": scene.get("version"), "building_count": len(buildings)},
        )

    # ------------------------------------------------------------------ #
    # 纯计算 API（供 onboarding / recon 调用）
    # ------------------------------------------------------------------ #
    def propose_device(
        self,
        scene: Optional[Dict[str, Any]],
        building: Optional[Dict[str, Any]],
        device_name: str,
        device_type: str,
        device_kind: str = "2960",
    ) -> Dict[str, Any]:
        """为新设备计算场景位置（世界坐标，避让楼内已有设备）。"""
        return {
            "name": device_name,
            "type": device_type or scene_type_for_device_kind(device_kind),
            "position": self.position_in_building(building),
            "status": "pending",
            "source": "agent",
        }

    def propose_building(
        self,
        scene: Optional[Dict[str, Any]],
        name: str,
    ) -> Dict[str, Any]:
        """为未确认的新楼栋生成场景配置（id/位置/尺寸/配色）。"""
        buildings = [
            item for item in (scene or {}).get("buildings") or [] if isinstance(item, dict)
        ]
        profile = building_profile(name)
        index = len(buildings)
        palette = BUILDING_PALETTE[index % len(BUILDING_PALETTE)]
        if index < len(DEFAULT_BUILDING_POSITIONS):
            position = dict(DEFAULT_BUILDING_POSITIONS[index])
        else:
            position = {"x": round(240.0 + 170.0 * (index - 4), 1), "z": -140.0}
        return {
            "id": profile.get("building_id") or ("bld-%d" % (index + 1)),
            "name": name,
            "position": position,
            "size": dict(NEW_BUILDING_SIZE),
            "style": {
                "color": profile.get("color") or palette["color"],
                "borderColor": palette["borderColor"],
                "opacity": palette["opacity"],
            },
            "status": "pending",
            "source": "agent",
        }

    def position_in_building(self, building: Optional[Dict[str, Any]]) -> Dict[str, float]:
        """计算楼栋内下一个空闲设备位置（世界坐标 {x,y,z}）。"""
        building = building or {}
        position = building.get("position") or {}
        size = building.get("size") or {}
        base_x = _to_float(position.get("x"))
        base_z = _to_float(position.get("z"))
        width = _to_float(size.get("width") or 64.0)
        depth = _to_float(size.get("depth") or 60.0)
        height = _to_float(size.get("height") or 24.0)
        y = round(height / 2.0 + 3.8, 1)

        existing: List[tuple] = []
        for device in building.get("devices") or []:
            device_position = (device or {}).get("position")
            if isinstance(device_position, dict):
                existing.append(
                    (
                        _to_float(device_position.get("x")) - base_x,
                        _to_float(device_position.get("z")) - base_z,
                    )
                )

        step = 15.0
        half_width = max(8.0, width / 2.0 - 10.0)
        rows = [0.0, -step, step, -2 * step, 2 * step]
        offsets = round(half_width / step)
        candidates_x = [round(index * step, 1) for index in range(-int(offsets), int(offsets) + 1)]
        for offset_z in rows:
            if abs(offset_z) > max(6.0, depth / 2.0 - 6.0):
                continue
            for offset_x in candidates_x:
                if all(
                    abs(offset_x - existing_x) >= 10.0 or abs(offset_z - existing_z) >= 10.0
                    for existing_x, existing_z in existing
                ):
                    return {
                        "x": round(base_x + offset_x, 1),
                        "y": y,
                        "z": round(base_z + offset_z, 1),
                    }
        return {
            "x": round(base_x + half_width, 1),
            "y": y,
            "z": round(base_z, 1),
        }

    def _fetch_scene(self, ctx: RoleContext) -> Dict[str, Any]:
        envelope = ctx.call_tool(self.toolkit, "get_scene", {})
        if envelope.get("ok") and isinstance(envelope.get("data"), dict):
            return envelope["data"]
        return {"version": None, "buildings": [], "theme": {}}


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
