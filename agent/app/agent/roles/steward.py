# -*- coding: utf-8 -*-
"""数据治理 Agent（steward）：删告警 / 静默 / 删对话历史 / 审计查询。

安全边界（见设计文档 §3.1 / §4.2）：
- active 告警**不可删除**，只能静默（生成 silence_alert action）；
- 所有写操作都只生成 action，用户在界面确认后才由 ActionExecutor 带
  ``X-Operator: agent`` 调用后端接口并写审计；
- 危险操作（清空已恢复告警 / 删除告警 / 删会话）标记 danger=true，需二次确认。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.agent.roles.base import ActionDraft, BaseRole, RoleContext, RoleResult


class StewardRole(BaseRole):
    """数据治理子 Agent：只读告警/审计，生成数据管理 action。"""

    name = "steward"
    label = "数据治理 Agent"
    allowed_tools = ("get_alerts", "get_audit")

    # ------------------------------------------------------------------ #
    # 对话入口
    # ------------------------------------------------------------------ #
    def run(self, ctx: RoleContext) -> RoleResult:
        started = self._start()
        question = ctx.question
        alerts_env = ctx.call_tool(self.toolkit, "get_alerts", {"limit": 100})
        alerts_data = alerts_env.get("data") if alerts_env.get("ok") else None
        alerts = list((alerts_data or {}).get("alerts") or [])
        summary = (alerts_data or {}).get("summary") or self._compute_summary(alerts)
        # W12.2：真实库告警较多时工具结果会被截断（>20000 字符），此时改用 limit=1
        # 只取 summary 计数（动作生成只依赖 cleared/active 数量，不依赖完整列表）
        if alerts_data is not None and (
            alerts_data.get("truncated") or "alerts" not in alerts_data
        ):
            summary_env = ctx.call_tool(self.toolkit, "get_alerts", {"limit": 1})
            summary_data = summary_env.get("data") if summary_env.get("ok") else None
            if isinstance(summary_data, dict) and isinstance(summary_data.get("summary"), dict):
                summary = summary_data["summary"]
            alerts = []
        cleared = [alert for alert in alerts if _alert_status(alert) == "cleared"]
        active = [alert for alert in alerts if _alert_status(alert) == "active"]
        silenced = [alert for alert in alerts if _alert_status(alert) == "silenced"]
        cleared_count = int(summary.get("cleared") or len(cleared))
        active_count = int(summary.get("active") or len(active))
        silenced_count = int(summary.get("silenced") or len(silenced))
        conversation_count = (
            self._conversations.count() if self._conversations is not None else 0
        )
        # W12.2：规则路由 action_hint（写操作确定性生成 pending action）
        route_hint = None
        if isinstance(getattr(ctx, "route", None), dict):
            route_hint = ctx.route.get("action_hint")
            if not route_hint:
                rule_name = str(ctx.route.get("rule") or "")
                if "write_clear_alerts" in rule_name:
                    route_hint = "clear_cleared_alerts"
                elif "write_silence_alerts" in rule_name:
                    route_hint = "silence_alert"
        # 静默类请求在截断后仍需要具体 alert_id：小页重取 active 告警
        if route_hint == "silence_alert" and active_count > 0 and not active:
            for limit in (5, 10, 20):
                small_env = ctx.call_tool(self.toolkit, "get_alerts", {"limit": limit})
                small_data = small_env.get("data") if small_env.get("ok") else None
                if not isinstance(small_data, dict) or small_data.get("truncated"):
                    continue
                active = [
                    alert
                    for alert in (small_data.get("alerts") or [])
                    if _alert_status(alert) == "active"
                ]
                if active:
                    break

        text = question.lower()
        wants_alerts = any(
            keyword in text for keyword in ("告警", "故障记录", "健康问题", "alert")
        )
        wants_conversations = any(
            keyword in text
            for keyword in ("对话", "会话", "聊天记录", "历史记录", "聊天历史", "conversation", "history", "chat")
        )
        wants_delete = any(
            keyword in text
            for keyword in ("删除", "删掉", "清空", "清理", "清除", "删除掉", "移除",
                            "delete", "remove", "clear", "clean")
        )
        wants_silence = any(
            keyword in text for keyword in ("静默", "忽略", "屏蔽", "silence", "mute", "ignore")
        )
        wants_audit = any(
            keyword in text
            for keyword in ("审计", "操作记录", "谁删", "谁改", "谁清", "audit")
        )
        wants_restore = any(keyword in text for keyword in ("撤销", "还原"))

        actions: List[ActionDraft] = []
        lines: List[str] = []

        # ---- 审计查询（只读） ----
        if wants_audit and not wants_delete:
            audit_env = ctx.call_tool(self.toolkit, "get_audit", {"limit": 20})
            entries = ((audit_env.get("data") or {}).get("entries")) or [] if audit_env.get("ok") else []
            lines.append("## 操作审计")
            if entries:
                lines.append("| 时间 | 操作者 | 动作 | 目标 | 说明 |")
                lines.append("|---|---|---|---|---|")
                for entry in entries:
                    lines.append(
                        "| %s | %s | %s | %s | %s |"
                        % (
                            entry.get("created_at_iso")
                            or _format_ts(entry.get("created_at") or entry.get("time")),
                            entry.get("operator") or entry.get("who") or "-",
                            entry.get("action"),
                            entry.get("target"),
                            entry.get("detail"),
                        )
                    )
            else:
                lines.append("- 当前没有审计记录（或后端审计接口暂不可用）。")
            lines.append("")
            lines.append("## 数据现状")
            lines.extend(self._summary_lines(summary, cleared, active, silenced, conversation_count))
            return self._finish(ctx, started, "\n".join(lines), data={"audit": entries})

        # ---- 会话管理 action ----
        if wants_conversations and (wants_delete or wants_restore):
            delete_current_only = any(
                keyword in question
                for keyword in (
                    "删除当前",
                    "删除这个",
                    "删除本",
                    "删掉当前",
                    "删掉这个",
                    "删掉本",
                    "删除这条",
                )
            ) and not any(keyword in question for keyword in ("清空", "所有", "全部", "历史"))
            if ctx.conversation_id and delete_current_only:
                actions.append(
                    ActionDraft(
                        type="delete_conversation",
                        params={"conversation_id": ctx.conversation_id},
                        confirm_text="将删除当前会话及其全部消息（不可恢复），确认执行？",
                        danger=True,
                    )
                )
            else:
                others = max(0, conversation_count - 1) if ctx.conversation_id else conversation_count
                if others <= 0:
                    lines.append("## 结果")
                    lines.append("- 当前没有其他历史会话可清空（当前会话不会被删除）。")
                    return self._finish(
                        ctx,
                        started,
                        "\n".join(lines),
                        data={"conversations": conversation_count},
                    )
                actions.append(
                    ActionDraft(
                        type="clear_conversations",
                        params={"keep_current": ctx.conversation_id},
                        confirm_text=(
                            "将清空 %d 个历史会话%s（删除后不可恢复），确认执行？"
                            % (others, "并保留当前会话" if ctx.conversation_id else "")
                        ),
                        danger=True,
                    )
                )

        # ---- 告警管理 action ----
        if wants_alerts and not wants_audit:
            specific = _find_specific_alert(question, alerts)
            if specific is not None:
                status = _alert_status(specific)
                if status == "active":
                    actions.append(
                        ActionDraft(
                            type="silence_alert",
                            params={
                                "alert_id": str(specific.get("id") or specific.get("alert_id")),
                            },
                            confirm_text=(
                                "将静默活跃告警「%s」（不删除，仅从列表静默），确认执行？"
                                % (specific.get("title") or specific.get("id"))
                            ),
                            danger=False,
                        )
                    )
                else:
                    actions.append(
                        ActionDraft(
                            type="delete_alert",
                            params={
                                "alert_id": str(specific.get("id") or specific.get("alert_id")),
                            },
                            confirm_text=(
                                "将删除告警「%s」（软删除 + 审计，可撤销），确认执行？"
                                % (specific.get("title") or specific.get("id"))
                            ),
                            danger=True,
                        )
                    )
            elif wants_silence:
                for alert in active[:3]:
                    actions.append(
                        ActionDraft(
                            type="silence_alert",
                            params={"alert_id": str(alert.get("id") or alert.get("alert_id"))},
                            confirm_text=(
                                "将静默活跃告警「%s」，确认执行？"
                                % (alert.get("title") or alert.get("id"))
                            ),
                            danger=False,
                        )
                    )
            elif wants_delete and not wants_restore:
                targets_cleared_only = (
                    route_hint == "clear_cleared_alerts" and cleared_count > 0
                ) or any(
                    keyword in question for keyword in ("已恢复", "已清除", "cleared")
                )
                if cleared_count > 0:
                    actions.append(
                        ActionDraft(
                            type="clear_cleared_alerts",
                            params={"scope": "cleared", "before": None},
                            confirm_text=(
                                "将清空 %d 条已恢复告警（软删除 + 审计，10 分钟内可撤销），确认执行？"
                                % cleared_count
                            ),
                            danger=True,
                        )
                    )
                # 设计不变式：active 告警不能删除，只能静默
                if not targets_cleared_only:
                    for alert in active[:3]:
                        actions.append(
                            ActionDraft(
                                type="silence_alert",
                                params={"alert_id": str(alert.get("id") or alert.get("alert_id"))},
                                confirm_text=(
                                    "活跃告警「%s」不可删除，将改为静默（不删除），确认执行？"
                                    % (alert.get("title") or alert.get("id"))
                                ),
                                danger=False,
                            )
                        )
            elif wants_restore:
                lines.append("## 撤销说明")
                lines.append(
                    "- 撤销软删除请使用后端 `POST /api/alerts/{id}/restore`（可由数据管理页面按钮触发）。"
                )
                lines.append("- 我可以在你确认后执行删除类操作，但不会自动恢复历史数据。")

        # ---- W12.2：规则路由 action hint 兜底（确定性生成清空/静默 action） ----
        if cleared_count > 0 and not any(
            action.type == "clear_cleared_alerts" for action in actions
        ) and (route_hint == "clear_cleared_alerts" or (
            wants_alerts and wants_delete and not wants_restore and not wants_audit
            and any(keyword in question for keyword in ("已恢复", "已清除", "cleared"))
        )):
            actions.append(
                ActionDraft(
                    type="clear_cleared_alerts",
                    params={"scope": "cleared", "before": None},
                    confirm_text=(
                        "将清空 %d 条已恢复告警（软删除 + 审计，10 分钟内可撤销），确认执行？"
                        % cleared_count
                    ),
                    danger=True,
                )
            )
        if route_hint == "clear_cleared_alerts" and cleared_count == 0:
            lines.append("## 结果")
            lines.append("- 当前没有已恢复告警可清空（cleared=0），未生成任何 action。")

        # ---- 组织回答 ----
        lines.extend(self._render_answer(question, summary, cleared, active, silenced, conversation_count, actions))
        data = {
            "alert_summary": summary,
            "cleared_count": cleared_count,
            "active_count": active_count,
            "silenced_count": silenced_count,
            "conversation_count": conversation_count,
            "route_action_hint": route_hint,
            "actions": [action.to_dict() for action in actions],
        }
        return self._finish(ctx, started, "\n".join(lines), actions=actions, data=data)

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _compute_summary(alerts: List[Dict[str, Any]]) -> Dict[str, int]:
        summary = {"active": 0, "cleared": 0, "info": 0, "silenced": 0, "total": len(alerts)}
        for alert in alerts:
            status = _alert_status(alert)
            if status in summary:
                summary[status] += 1
            if str(alert.get("severity") or "") == "info":
                summary["info"] += 1
        return summary

    def _summary_lines(
        self,
        summary: Dict[str, int],
        cleared: List[Dict[str, Any]],
        active: List[Dict[str, Any]],
        silenced: List[Dict[str, Any]],
        conversation_count: int,
    ) -> List[str]:
        return [
            "- 告警：共 %d 条（活跃 %d / 已恢复 %d / 已静默 %d）"
            % (
                summary.get("total", len(cleared) + len(active) + len(silenced)),
                summary.get("active", len(active)),
                summary.get("cleared", len(cleared)),
                summary.get("silenced", len(silenced)),
            ),
            "- 会话：共 %d 个" % conversation_count,
        ]

    def _render_answer(
        self,
        question: str,
        summary: Dict[str, int],
        cleared: List[Dict[str, Any]],
        active: List[Dict[str, Any]],
        silenced: List[Dict[str, Any]],
        conversation_count: int,
        actions: List[ActionDraft],
    ) -> List[str]:
        lines: List[str] = []
        lines.append("## 数据现状")
        lines.extend(
            self._summary_lines(summary, cleared, active, silenced, conversation_count)
        )
        if cleared:
            lines.append("")
            lines.append("### 已恢复告警（可安全清理）")
            for alert in cleared[:5]:
                lines.append(
                    "- `%s` [%s] %s（%s）"
                    % (
                        alert.get("id") or alert.get("alert_id") or "-",
                        alert.get("severity") or "-",
                        alert.get("title") or "-",
                        _format_ts(alert.get("created_at")),
                    )
                )
        if active:
            lines.append("")
            lines.append("### 活跃告警（不可删除，只能静默）")
            for alert in active[:5]:
                lines.append(
                    "- `%s` [%s] %s（%s）"
                    % (
                        alert.get("id") or alert.get("alert_id") or "-",
                        alert.get("severity") or "-",
                        alert.get("title") or "-",
                        _format_ts(alert.get("created_at")),
                    )
                )
        lines.append("")
        lines.append("## 待确认操作" if actions else "## 建议操作")
        if actions:
            for index, action in enumerate(actions, start=1):
                danger = "【危险】" if action.danger else ""
                lines.append("%d. %s%s" % (index, danger, action.confirm_text))
            lines.append("")
            lines.append(
                "- 所有操作都需要你在界面点击「确认执行」后才会生效（10 分钟内有效，过期需重新发起）。"
            )
        else:
            if not any(keyword in question for keyword in ("删除", "清空", "静默", "清理")):
                lines.append(
                    "- 我可以帮你：清空已恢复告警、静默活跃告警、删除/清空历史会话、查询操作审计。"
                )
                lines.append("- 请告诉我具体要处理什么，例如「把已恢复的告警清空」。")
            else:
                lines.append("- 当前没有可执行的数据管理操作（例如没有已恢复告警/历史会话）。")
        lines.append("")
        lines.append("## 安全说明")
        lines.append("- active 告警不可删除（避免误删正在发生的故障），只能静默；")
        lines.append("- 删除均为软删除 + 审计记录，可在「操作审计」中追溯；")
        lines.append("- Agent 不会自动执行任何写操作，必须由你确认。")
        return lines


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _alert_status(alert: Dict[str, Any]) -> str:
    state = str(alert.get("state") or "").strip().lower()
    if state == "cleared":
        return "cleared"
    if state == "silenced":
        return "silenced"
    if state == "open":
        return "active"
    if alert.get("resolved_at") is not None:
        return "cleared"
    if alert.get("silenced_at") is not None:
        return "silenced"
    status = str(alert.get("status") or "").strip().lower()
    if status in ("cleared", "resolved"):
        return "cleared"
    if status == "silenced":
        return "silenced"
    if status == "active":
        return "active"
    alert_type = str(alert.get("alert_type") or "")
    title = str(alert.get("title") or "")
    detail = str(alert.get("detail") or "")
    if alert_type.endswith("_cleared") or "已恢复" in title or "已清除" in title or "已恢复" in detail:
        return "cleared"
    return "active"


def _find_specific_alert(
    question: str, alerts: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """识别问题里点名的告警（按 id 数字 / 设备名 / 标题关键字）。"""
    match = re.search(r"(?:告警|alert)\s*[#id:：-]*\s*(\d+)", question, re.IGNORECASE)
    if match:
        wanted = match.group(1)
        for alert in alerts:
            alert_id = str(alert.get("id") or alert.get("alert_id") or "")
            if alert_id == wanted or alert_id.endswith(wanted):
                return alert
    for alert in alerts:
        title = str(alert.get("title") or "")
        device = str(alert.get("device_id") or "")
        for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", title):
            if token and token.lower() in question.lower():
                return alert
        if device and device.lower() in question.lower():
            return alert
    return None


def _format_ts(value: Any) -> str:
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return "-" if value is None else str(value)
    import datetime

    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")
