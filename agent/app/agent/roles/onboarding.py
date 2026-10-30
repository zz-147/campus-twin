# -*- coding: utf-8 -*-
"""接入引导 Agent（onboarding）：用户想加设备时给出完整接入方案。

输出：
- PT 型号建议、端口级连线表、完整 Cisco IOS CLI、验证命令、撤销命令；
- 3D 场景摆放建议（调用 scene Agent 纯计算）；
- 一个 ``apply_scene_device`` / ``apply_scene_building`` 操作草案（需用户确认后执行）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.agent.inference import find_building
from app.agent.knowledge import (
    BUILDING_PROFILES,
    DEVICE_SPECS,
    building_profile,
    detect_building_name,
    detect_device_kind,
    format_cabling_markdown,
    next_hostname,
    next_management_ip,
    render_cli,
    render_device_access_steps,
    render_undo,
    render_verification,
    scene_model,
    scene_type_label,
    terminal_segment,
)
from app.agent.roles.base import ActionDraft, BaseRole, RoleContext, RoleResult
from app.agent.roles.scene import SceneRole, scene_type_for_device_kind

_NEW_BUILDING_KEYWORDS = ("新建", "新楼", "新盖", "再建", "建一栋", "新增一栋", "建个", "建一栋楼", "加一栋")


class OnboardingRole(BaseRole):
    """接入引导子 Agent：只读工具 + 内置模板库（不写配置）。"""

    name = "onboarding"
    label = "接入引导 Agent"
    allowed_tools = (
        "get_devices",
        "get_topology",
        "get_hosts",
        "get_scene",
        "get_device_config_template",
        "get_new_devices",
        "mcp_list_tools",
        "mcp_list_dir",
        "mcp_read_file",
        "mcp_search_files",
        "mcp_memory_search",
    )

    def run(self, ctx: RoleContext) -> RoleResult:
        started = self._start()
        question = ctx.question
        kind = detect_device_kind(question) or "2960"
        building_name = detect_building_name(question)
        if not building_name:
            answer = (
                "## 需要确认目标楼栋\n"
                "- 我可以帮你完成设备接入（型号选型 / 端口连线 / CLI 配置 / 3D 场景摆放），"
                "但需要先知道设备放在哪栋楼。\n"
                "- 现有楼栋：%s。\n"
                "- 请回复例如：「我想在宿舍楼加一台 2960 交换机」。"
                % "、".join(BUILDING_PROFILES.keys())
            )
            return self._finish(
                ctx, started, answer, data={"needs_building": True, "device_kind": kind}
            )

        # ---- 只读数据 ----
        scene = _tool_data(ctx.call_tool(self.toolkit, "get_scene", {})) or {}
        devices = list((_tool_data(ctx.call_tool(self.toolkit, "get_devices", {})) or {}).get("devices") or [])
        topology = _tool_data(ctx.call_tool(self.toolkit, "get_topology", {})) or {}
        links = list(topology.get("links") or [])
        building_obj = find_building(scene, building_name)
        is_new_request = any(keyword in question for keyword in _NEW_BUILDING_KEYWORDS)
        known_building = building_name in BUILDING_PROFILES
        new_building = is_new_request or (not known_building and building_obj is None)

        scene_role = SceneRole(self.full_toolkit)
        profile = building_profile(building_name)

        # ---- 命名 / 地址 / 端口规划 ----
        existing_hostnames = [
            str(item.get("name") or item.get("hostname") or "")
            for item in _all_scene_devices(scene)
        ] + [str(item.get("hostname") or "") for item in devices]
        hostname = next_hostname(
            [name for name in existing_hostnames if name], str(profile["hostname_prefix"])
        )
        management_ips = [
            str(item.get("management_ip") or "") for item in devices if item.get("management_ip")
        ]
        mgmt_ip = next_management_ip(
            management_ips, str(profile.get("mgmt_segment") or "192.168.10.")
        )
        vlan = int(profile["vlan"])
        gateway = str(profile["gateway"])
        spec = DEVICE_SPECS.get(kind) or DEVICE_SPECS["2960"]
        scene_type = scene_type_for_device_kind(kind)

        if new_building:
            building_cfg = scene_role.propose_building(scene, building_name)
            building_id = str(building_cfg.get("id"))
            placement = {
                "name": hostname,
                "type": scene_type,
                "position": scene_role.position_in_building(building_cfg),
                "status": "pending",
                "source": "agent",
            }
            building_position = building_cfg.get("position")
        else:
            building_cfg = None
            building_id = str(
                (building_obj or {}).get("id") or profile.get("building_id") or ""
            )
            placement = scene_role.propose_device(
                scene, building_obj, hostname, scene_type, device_kind=kind
            )
            building_position = (building_obj or {}).get("position")

        # 场景设备字段对齐后端 DeviceUpsert：补充 model / mgmt_ip
        placement["model"] = scene_model(kind)
        if spec["kind"] in ("pc", "server"):
            placement["mgmt_ip"] = terminal_segment(building_name) + "20"
        else:
            placement["mgmt_ip"] = mgmt_ip

        uplink = self._plan_uplink(
            kind=kind,
            scene=scene,
            devices=devices,
            links=links,
            building_name=building_name,
        )

        # ---- 模板 / CLI ----
        if spec["kind"] in ("pc", "server"):
            cabling = [
                {
                    "local_device": hostname,
                    "local_port": "Fa0",
                    "peer_device": uplink["peer_device"],
                    "peer_port": uplink["local_port"],
                    "cable": "直通线（Copper Straight-Through）",
                    "purpose": "终端接入 Access（VLAN %d）" % vlan,
                }
            ]
            cli = "\n".join(render_device_access_steps(hostname, building_name))
            verification = [
                "在 PT 终端里 ping %s（网关）" % gateway,
                "在接入交换机执行 show vlan brief / show interface status",
            ]
            undo = ["在 PT 中删除终端连线；如已加入场景，确认撤销场景设备操作"]
        else:
            cabling = [
                {
                    "local_device": hostname,
                    "local_port": uplink["local_port"],
                    "peer_device": uplink["peer_device"],
                    "peer_port": uplink["peer_port"],
                    "cable": "直通线（Copper Straight-Through）",
                    "purpose": "上联 Trunk（VLAN %d）" % vlan,
                }
            ]
            access_ports = (
                [spec["access_port_template"] % 1, spec["access_port_template"] % 2]
                if spec["kind"] == "switch" and spec.get("access_port_template")
                else []
            )
            cli = render_cli(
                kind,
                hostname,
                vlan,
                str(profile.get("vlan_name") or "USERS"),
                mgmt_ip,
                gateway,
                uplink["local_port"],
                access_ports,
            )
            verification = render_verification(kind, hostname)
            undo = render_undo(kind, hostname, vlan)

        # ---- 操作草案 ----
        actions: List[ActionDraft] = []
        if new_building and building_cfg is not None:
            actions.append(
                ActionDraft(
                    type="apply_scene_building",
                    params={
                        "building": building_cfg,
                        "devices": [placement],
                        "building_name": building_name,
                    },
                    confirm_text=(
                        "将新建楼栋「%s」并把设备 %s 以 pending 状态加入 3D 场景，确认执行？"
                        % (building_name, hostname)
                    ),
                    danger=False,
                )
            )
        else:
            actions.append(
                ActionDraft(
                    type="apply_scene_device",
                    params={
                        "building_id": building_id,
                        "building_name": building_name,
                        "device": placement,
                    },
                    confirm_text=(
                        "将在 3D 场景「%s」中新增设备 %s（pending 状态，可在场景里确认/撤销），确认执行？"
                        % (building_name, hostname)
                    ),
                    danger=False,
                )
            )

        # ---- 回答 ----
        lines = self._render_answer(
            question=question,
            kind=kind,
            spec=spec,
            building_name=building_name,
            profile=profile,
            building_id=building_id,
            building_position=building_position,
            hostname=hostname,
            mgmt_ip=mgmt_ip,
            vlan=vlan,
            gateway=gateway,
            uplink=uplink,
            cabling=cabling,
            cli=cli,
            verification=verification,
            undo=undo,
            placement=placement,
            new_building=new_building,
        )
        data = {
            "device_kind": kind,
            "building": building_name,
            "building_id": building_id,
            "new_building": new_building,
            "hostname": hostname,
            "management_ip": mgmt_ip,
            "vlan": vlan,
            "gateway": gateway,
            "uplink": uplink,
            "placement": placement,
            "actions": [action.to_dict() for action in actions],
        }
        return self._finish(ctx, started, "\n".join(lines), actions=actions, data=data)

    # ------------------------------------------------------------------ #
    # 规划辅助
    # ------------------------------------------------------------------ #
    def _plan_uplink(
        self,
        kind: str,
        scene: Dict[str, Any],
        devices: List[Dict[str, Any]],
        links: List[Dict[str, Any]],
        building_name: str,
    ) -> Dict[str, Any]:
        spec = DEVICE_SPECS.get(kind) or DEVICE_SPECS["2960"]
        local_port = str((spec.get("uplink_ports") or ["Gi0/1"])[0])
        device_names = {str(item.get("hostname") or "") for item in devices}
        scene_names = {str(item.get("name") or "") for item in _all_scene_devices(scene)}
        candidates = ["Core-3650", "Router-4331"]
        peer_device = next(
            (name for name in candidates if name in device_names or name in scene_names),
            "Core-3650",
        )
        if kind == "4331":
            peer_device = "Core-3650" if "Core-3650" in device_names else "Core-3650"
            local_port = "Gi0/0/1"
        used_remote = _used_ports(links, peer_device)
        if peer_device.startswith("Core"):
            remote_candidates = ["Gi1/0/%d" % index for index in range(7, 25)]
        elif "Router" in peer_device:
            remote_candidates = ["Gi0/0/1", "Gi0/0/2"]
        else:
            remote_candidates = ["Fa0/%d" % index for index in range(20, 25)]
        remote_port = next(
            (port for port in remote_candidates if port not in used_remote),
            remote_candidates[0],
        )
        return {
            "peer_device": peer_device,
            "local_port": local_port,
            "peer_port": remote_port,
            "mode": "trunk" if spec["kind"] == "switch" else "routed",
        }

    def _render_answer(
        self,
        question: str,
        kind: str,
        spec: Dict[str, Any],
        building_name: str,
        profile: Dict[str, Any],
        building_id: str,
        building_position: Any,
        hostname: str,
        mgmt_ip: str,
        vlan: int,
        gateway: str,
        uplink: Dict[str, Any],
        cabling: List[Dict[str, Any]],
        cli: str,
        verification: List[str],
        undo: List[str],
        placement: Dict[str, Any],
        new_building: bool,
    ) -> List[str]:
        lines: List[str] = []
        lines.append("## 接入方案")
        position_text = "-"
        if isinstance(building_position, dict):
            position_text = "x=%s, z=%s" % (
                building_position.get("x"),
                building_position.get("z"),
            )
        elif building_position:
            position_text = str(building_position)
        lines.append(
            "- 目标楼栋：**%s**%s（场景 ID：%s，位置：%s）"
            % (
                building_name,
                "（新建）" if new_building else "",
                building_id or "待后端分配",
                position_text,
            )
        )
        lines.append(
            "- 设备型号：**%s**（%s，%s）"
            % (spec.get("label"), spec.get("model"), spec.get("ios") or "终端设备")
        )
        lines.append(
            "- 建议 hostname：**%s**；管理 IP：**%s**（VLAN %d / 网关 %s）"
            % (hostname, mgmt_ip, vlan, gateway)
        )
        if spec["kind"] in ("switch", "router"):
            lines.append(
                "- 上联：%s %s ↔ %s %s（%s）"
                % (
                    hostname,
                    uplink["local_port"],
                    uplink["peer_device"],
                    uplink["peer_port"],
                    "Trunk" if uplink["mode"] == "trunk" else "三层互联",
                )
            )
        lines.append(
            "- 场景类型：`%s`（前端按低模风格渲染为 %s）"
            % (placement.get("type"), scene_type_label(str(placement.get("type"))))
        )
        lines.append("")
        lines.append("## 端口连线表")
        lines.append(format_cabling_markdown(cabling))
        lines.append("")
        lines.append("## 完整 CLI 配置" if spec["kind"] not in ("pc", "server") else "## PT 终端配置步骤")
        lines.append("```")
        lines.append(cli)
        lines.append("```")
        lines.append("")
        lines.append("## 3D 场景位置（低模风格）")
        lines.append(
            "- 相对楼栋坐标：`%s`；状态：`pending`（确认后转 `confirmed`）；来源：`agent`"
            % placement.get("position")
        )
        if new_building:
            lines.append(
                "- 新楼栋：`%s` 将按现有 4 栋的间距排在场景外侧，使用半透明 box + 中文标签。"
                % building_name
            )
        lines.append("")
        lines.append("## 验证命令")
        for index, command in enumerate(verification, start=1):
            lines.append("%d. `%s`" % (index, command))
        lines.append("")
        lines.append("## 撤销命令（回滚）")
        lines.append("```")
        lines.extend(undo)
        lines.append("```")
        lines.append("")
        lines.append("## 下一步")
        if new_building:
            lines.append(
                "1. 我已在对话下方生成「新建楼栋 + 新增设备」操作卡片，点确认后场景才会变更（需用户确认，10 分钟内有效）。"
            )
        else:
            lines.append(
                "1. 我已在对话下方生成「场景新增设备」操作卡片，点确认后设备以 pending 状态出现在 3D 场景。"
            )
        lines.append("2. 到 PT 里按上面的布线表连线、粘贴 CLI，再用验证命令确认。")
        lines.append("3. 大屏会自动发现新设备；如归属楼栋/位置不对，可在场景里确认或撤销。")
        return lines


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _tool_data(envelope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if envelope.get("ok") and isinstance(envelope.get("data"), dict):
        return envelope["data"]
    return None


def _all_scene_devices(scene: Dict[str, Any]) -> List[Dict[str, Any]]:
    devices: List[Dict[str, Any]] = []
    for building in scene.get("buildings") or []:
        for device in building.get("devices") or []:
            if isinstance(device, dict):
                devices.append(device)
    return devices


def _used_ports(links: List[Dict[str, Any]], device_name: str) -> List[str]:
    used: List[str] = []
    for link in links:
        if str(link.get("source_label") or "") == device_name:
            used.append(str(link.get("start_port_name") or ""))
        if str(link.get("target_label") or "") == device_name:
            used.append(str(link.get("end_port_name") or ""))
    return [port for port in used if port]
