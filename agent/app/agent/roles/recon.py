# -*- coding: utf-8 -*-
"""接入侦察 Agent（recon）：发现新设备并推断归属楼栋（五级规则）。

输出：
- 待加入场景的新设备清单（来自 /api/scene/pending + inventory 差集）；
- 每台设备的归属推断结果（规则/置信度/原因）；
- 用户要求"帮我加到场景"时，生成 apply_scene_device / confirm_scene_device 操作草案；
- 无法推断时在对话中询问用户，不擅自建楼。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.agent.inference import find_building
from app.agent.knowledge import DEVICE_SPECS, detect_device_kind, scene_type_label
from app.agent.roles.base import ActionDraft, BaseRole, RoleContext, RoleResult
from app.agent.roles.scene import SceneRole, scene_type_for_device_kind

#: 表示"帮我把新设备加入场景"的意图关键词
ADD_KEYWORDS = ("加到场景", "加入场景", "加进场景", "放进场景", "帮我加", "加入", "收录", "确认加入", "加到 3d", "加到3d")

RULE_LABELS = {
    "topology_neighbor": "拓扑邻居",
    "hostname_keyword": "hostname 关键词",
    "ip_segment": "管理 IP 段",
    "vlan_segment": "VLAN 段",
    "scene_pending": "后端 pending 队列",
    "need_user": "无法自动推断",
    "new_building": "建议新建楼栋",
    "unknown": "未知",
}


class ReconRole(BaseRole):
    """接入侦察子 Agent：只读 inventory/拓扑/场景，不写配置。"""

    name = "recon"
    label = "接入侦察 Agent"
    allowed_tools = ("get_devices", "get_topology", "get_hosts", "get_scene", "get_new_devices")

    def run(self, ctx: RoleContext) -> RoleResult:
        started = self._start()
        question = ctx.question
        new_env = ctx.call_tool(self.toolkit, "get_new_devices", {})
        if not new_env.get("ok"):
            return self._finish(
                ctx,
                started,
                "## 侦察结果\n- 无法读取新设备数据：%s\n- 请确认后端已提供 GET /api/scene/pending 或 inventory 接口。"
                % new_env.get("error"),
                error=str(new_env.get("error") or ""),
            )
        data = new_env.get("data") or {}
        new_devices = list(data.get("new_devices") or [])
        scene_env = ctx.call_tool(self.toolkit, "get_scene", {})
        scene = scene_env.get("data") if scene_env.get("ok") else {}
        scene = scene if isinstance(scene, dict) else {}
        wants_add = any(keyword in question for keyword in ADD_KEYWORDS)
        scene_role = SceneRole(self.full_toolkit)

        if not new_devices:
            answer = "\n".join(
                [
                    "## 侦察结果",
                    "- 当前没有发现待加入场景的新设备：inventory 中的设备都已在场景配置里。",
                    "- 自动发现流程：在 PT 里加好设备 → 后端采集（约 5 秒）→ 我会在下一轮发现它并推断楼栋。",
                    "",
                    "## 下一步",
                    "- 加好设备后问我「发现新设备了吗？帮我加到场景里」。",
                ]
            )
            return self._finish(
                ctx, started, answer, data={"new_devices": [], "wanted_add": wants_add}
            )

        actions: List[ActionDraft] = []
        lines: List[str] = ["## 侦察结果"]
        lines.append(
            "发现 **%d** 台待处理新设备（数据源：%s）。"
            % (len(new_devices), "后端 pending 队列 + inventory 差集")
        )
        lines.append("")
        lines.append("| 设备 | 管理 IP | 推断楼栋 | 依据规则 | 置信度 | 场景状态 |")
        lines.append("|---|---|---|---|---|---|")
        questions: List[str] = []
        placements: List[Dict[str, Any]] = []

        for item in new_devices[:5]:
            name = str(item.get("name") or "")
            ip = str(item.get("management_ip") or "未知")
            inference = item.get("inference") or {}
            building_name = inference.get("building") or ""
            rule_label = RULE_LABELS.get(str(inference.get("rule") or ""), str(inference.get("rule")))
            confidence = str(inference.get("confidence") or "-")
            status_label = "已在场景 pending" if item.get("source") == "scene_pending" else "未加入场景"
            lines.append(
                "| %s | %s | %s | %s | %s | %s |"
                % (name, ip, building_name or "待确认", rule_label, confidence, status_label)
            )
            if inference.get("needs_user") or not building_name:
                questions.append(str(inference.get("question") or ("设备 %s 属于哪栋楼？" % name)))
                continue
            if not wants_add:
                continue
            scene_building = find_building(scene, building_name)
            if item.get("source") == "scene_pending":
                actions.append(
                    ActionDraft(
                        type="confirm_scene_device",
                        params={"device_name": name, "building_name": building_name},
                        confirm_text=(
                            "将确认新设备 %s 正式加入「%s」场景（pending → confirmed），确认执行？"
                            % (name, building_name)
                        ),
                        danger=False,
                    )
                )
                placements.append({"device": name, "action": "confirm_scene_device"})
            else:
                kind = detect_device_kind(str(item.get("type") or "")) or "2960"
                scene_type = scene_type_for_device_kind(kind)
                placement = scene_role.propose_device(
                    scene, scene_building, name, scene_type, device_kind=kind
                )
                building_id = str(
                    (scene_building or {}).get("id") or inference.get("building_id") or ""
                )
                actions.append(
                    ActionDraft(
                        type="apply_scene_device",
                        params={
                            "building_id": building_id,
                            "building_name": building_name,
                            "device": placement,
                        },
                        confirm_text=(
                            "将把新设备 %s 加入「%s」场景（pending 状态，可再确认/撤销），确认执行？"
                            % (name, building_name)
                        ),
                        danger=False,
                    )
                )
                placements.append({"device": name, "action": "apply_scene_device"})

        lines.append("")
        lines.append("## 推断说明")
        lines.append(
            "- 推断顺序：拓扑邻居楼栋 → hostname 关键词（JX/SX/SS/CORE）→ 管理 IP/VLAN 段映射 → 询问用户 → 新建楼栋。"
        )
        for item in new_devices[:5]:
            inference = item.get("inference") or {}
            if inference.get("reason"):
                lines.append(
                    "- %s：%s（置信度 %s）"
                    % (item.get("name"), inference.get("reason"), inference.get("confidence"))
                )

        lines.append("")
        lines.append("## 建议操作")
        if questions:
            lines.append("以下设备无法自动归属，需要你确认：")
            for question in questions:
                lines.append("- %s" % question)
        if actions:
            lines.append("我已生成 %d 个场景操作卡片（需你确认后执行，10 分钟内有效）：" % len(actions))
            for action in actions:
                lines.append("- %s：%s" % (action.type, action.confirm_text))
        elif not questions:
            lines.append(
                "- 如需把新设备加入 3D 场景，请回复「帮我加到场景里」，我会生成确认卡片。"
            )

        lines.append("")
        lines.append("## 安全说明")
        lines.append("- 所有场景写入都先变成 pending 状态并等待你确认，不会自动修改配置。")
        lines.append("- 若归属推断错误，可告诉我正确楼栋（如「它属于教学楼」），我会重新生成操作。")

        result_data = {
            "new_devices": new_devices,
            "wanted_add": wants_add,
            "actions": [action.to_dict() for action in actions],
            "placements": placements,
            "needs_user": questions,
        }
        return self._finish(ctx, started, "\n".join(lines), actions=actions, data=result_data)
