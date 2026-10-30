# -*- coding: utf-8 -*-
"""Action 确认协议：Agent 只生成 action，用户确认后服务端才执行写操作。

安全设计（docs/W9-Agent扩展设计.md §4.2）：
- Agent 对话过程中**绝不直接执行写操作**，只返回 ``actions[]``（status=pending）；
- 用户在界面点「确认执行」→ ``POST /api/actions/{id}/confirm`` 才真正调用后端写接口；
- ``confirm`` 前校验：action 存在、未过期（默认 10 分钟）、仍为 pending（CAS 防重复执行）；
- 执行时后端写接口统一带 ``X-Operator: agent``，后端负责审计；
- 数据治理类危险操作标记 ``danger=true``（前端红色确认卡片），但同样必须显式确认。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from app.agent.backend import BackendClient, BackendError
from app.agent.conversations import ConversationStore

ACTION_TTL_S = 600  # 10 分钟

#: 设计文档约定的 action 类型
ACTION_TYPES: Tuple[str, ...] = (
    "clear_cleared_alerts",
    "delete_alert",
    "silence_alert",
    "delete_conversation",
    "clear_conversations",
    "apply_scene_device",
    "apply_scene_building",
    "confirm_scene_device",
    "remove_scene_device",
)

#: 数据治理中真正会删除数据的操作（前端红色高亮）
DANGER_ACTION_TYPES = frozenset(
    {
        "clear_cleared_alerts",
        "delete_alert",
        "delete_conversation",
        "clear_conversations",
        "remove_scene_device",
    }
)

ACTION_LABELS: Dict[str, str] = {
    "clear_cleared_alerts": "清空已恢复告警",
    "delete_alert": "删除告警",
    "silence_alert": "静默告警",
    "delete_conversation": "删除会话",
    "clear_conversations": "清空历史会话",
    "apply_scene_device": "场景新增设备",
    "apply_scene_building": "场景新增楼栋",
    "confirm_scene_device": "确认新设备",
    "remove_scene_device": "移除场景设备",
}


class ActionError(Exception):
    """action 协议错误（不存在/已过期/已执行/重复执行/参数非法）。"""

    def __init__(self, code: str, message: str, action: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "action": self.action}


class ActionStore:
    """action 持久化 + 状态机（pending → executing → executed/failed/cancelled/expired）。"""

    def __init__(self, db_path: Path, ttl_s: int = ACTION_TTL_S) -> None:
        self._path = str(db_path)
        self._ttl_s = max(30, int(ttl_s))
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    action_type TEXT NOT NULL,
                    params TEXT NOT NULL DEFAULT '{}',
                    confirm_text TEXT NOT NULL DEFAULT '',
                    danger INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    executed_at REAL,
                    result TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status, created_at);
                """
            )

    @property
    def db_path(self) -> str:
        return self._path

    # ------------------------------------------------------------------ #
    # 创建 / 查询
    # ------------------------------------------------------------------ #
    def create(
        self,
        action_type: str,
        params: Optional[Dict[str, Any]] = None,
        confirm_text: str = "",
        danger: Optional[bool] = None,
        conversation_id: Optional[str] = None,
        ttl_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        if action_type not in ACTION_TYPES:
            raise ActionError("invalid_type", "未知 action 类型：%s" % action_type)
        params = params or {}
        if not isinstance(params, dict):
            raise ActionError("invalid_params", "action params 必须是对象")
        now = time.time()
        ttl = max(30, int(ttl_s)) if ttl_s is not None else self._ttl_s
        action_id = "act_" + uuid.uuid4().hex[:12]
        danger_flag = (
            bool(danger) if danger is not None else action_type in DANGER_ACTION_TYPES
        )
        confirm_text = confirm_text or self._default_confirm_text(action_type, params)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO actions(id, conversation_id, action_type, params, confirm_text,"
                " danger, status, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    action_id,
                    conversation_id,
                    action_type,
                    json.dumps(params, ensure_ascii=False, default=str),
                    confirm_text,
                    1 if danger_flag else 0,
                    "pending",
                    now,
                    now + ttl,
                ),
            )
        return self.get(action_id)  # type: ignore[return-value]

    def get(self, action_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM actions WHERE id=?", (action_id or "",)
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "pending" and time.time() > float(row["expires_at"]):
                connection.execute(
                    "UPDATE actions SET status='expired' WHERE id=? AND status='pending'",
                    (action_id,),
                )
                row = connection.execute(
                    "SELECT * FROM actions WHERE id=?", (action_id,)
                ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def list_pending(self, conversation_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM actions WHERE status='pending'"
        args: Tuple[Any, ...] = ()
        if conversation_id:
            query += " AND conversation_id=?"
            args = (conversation_id,)
        query += " ORDER BY created_at ASC"
        with self._connect() as connection:
            rows = connection.execute(query, args).fetchall()
        return [_row_to_dict(row) for row in rows]

    def count(self, status: Optional[str] = None) -> int:
        with self._connect() as connection:
            if status:
                row = connection.execute(
                    "SELECT COUNT(*) AS c FROM actions WHERE status=?", (status,)
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) AS c FROM actions").fetchone()
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------ #
    # 取消 / 确认执行
    # ------------------------------------------------------------------ #
    def cancel(self, action_id: str) -> Dict[str, Any]:
        action = self.get(action_id)
        if action is None:
            raise ActionError("not_found", "操作不存在或已被清理")
        if action["status"] == "expired":
            raise ActionError("expired", "操作已过期（超过 %.0f 秒），请重新发起" % self._ttl_s, action)
        if action["status"] != "pending":
            raise ActionError(
                "not_pending",
                "操作当前状态为 %s，不能取消" % action["status"],
                action,
            )
        with self._connect() as connection:
            connection.execute(
                "UPDATE actions SET status='cancelled' WHERE id=? AND status='pending'",
                (action_id,),
            )
        return self.get(action_id)  # type: ignore[return-value]

    def confirm(
        self,
        action_id: str,
        executor: "ActionExecutor",
        operator: str = "agent",
    ) -> Dict[str, Any]:
        """校验并执行 action。并发调用时只有第一个能把 pending 改成 executing。"""
        action = self.get(action_id)
        if action is None:
            raise ActionError("not_found", "操作不存在或已被清理")
        if action["status"] == "expired":
            raise ActionError("expired", "操作已过期（超过 %.0f 秒），请重新发起" % self._ttl_s, action)
        if action["status"] != "pending":
            raise ActionError(
                "not_pending",
                "操作当前状态为 %s，不能重复执行" % action["status"],
                action,
            )
        if float(action["expires_at"]) < time.time():
            with self._connect() as connection:
                connection.execute(
                    "UPDATE actions SET status='expired' WHERE id=?", (action_id,)
                )
            raise ActionError("expired", "操作已过期，请重新发起", self.get(action_id))

        # CAS：只有 pending → executing 成功的调用才执行
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE actions SET status='executing', executed_at=? WHERE id=? AND status='pending'",
                (time.time(), action_id),
            )
            claimed = cursor.rowcount == 1
        if not claimed:
            current = self.get(action_id)
            raise ActionError(
                "not_pending", "操作已被处理，不能重复执行", current
            )

        try:
            result = executor.execute(
                action["type"], action.get("params") or {}, operator=operator
            )
            status, error = "executed", None
        except BackendError as exc:
            result = None
            status, error = "failed", "后端执行失败：%s" % exc
        except ActionError as exc:
            result = None
            status, error = "failed", exc.message
        except Exception as exc:  # noqa: BLE001 - 执行失败要落库，不能抛给协议层
            result = None
            status, error = "failed", "执行异常：%s: %s" % (type(exc).__name__, exc)

        with self._connect() as connection:
            connection.execute(
                "UPDATE actions SET status=?, result=?, error=?, executed_at=? WHERE id=?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                    error,
                    time.time(),
                    action_id,
                ),
            )
        return self.get(action_id)  # type: ignore[return-value]

    @staticmethod
    def _default_confirm_text(action_type: str, params: Dict[str, Any]) -> str:
        label = ACTION_LABELS.get(action_type, action_type)
        return "将执行「%s」，确认继续？" % label


class ActionExecutor:
    """把用户已确认的 action 翻译成后端写接口调用（或 Agent 本地会话删除）。

    所有后端写请求都带 ``X-Operator``（默认 agent），由后端记录审计。
    """

    def __init__(
        self,
        backend: BackendClient,
        conversations: ConversationStore,
        default_operator: str = "agent",
    ) -> None:
        self._backend = backend
        self._conversations = conversations
        self._default_operator = default_operator
        self._handlers = {
            "clear_cleared_alerts": self._clear_cleared_alerts,
            "delete_alert": self._delete_alert,
            "silence_alert": self._silence_alert,
            "delete_conversation": self._delete_conversation,
            "clear_conversations": self._clear_conversations,
            "apply_scene_device": self._apply_scene_device,
            "apply_scene_building": self._apply_scene_building,
            "confirm_scene_device": self._confirm_scene_device,
            "remove_scene_device": self._remove_scene_device,
        }

    def execute(
        self,
        action_type: str,
        params: Dict[str, Any],
        operator: Optional[str] = None,
    ) -> Dict[str, Any]:
        handler = self._handlers.get(action_type)
        if handler is None:
            raise ActionError("invalid_type", "未知 action 类型：%s" % action_type)
        return handler(params, operator or self._default_operator)

    # ------------------------------------------------------------------ #
    # 数据治理
    # ------------------------------------------------------------------ #
    def _clear_cleared_alerts(self, params: Dict[str, Any], operator: str) -> Dict[str, Any]:
        body: Dict[str, Any] = {"scope": params.get("scope") or "cleared"}
        if params.get("before") is not None:
            body["before"] = params["before"]
        result = self._backend.post("/api/alerts/clear", body, operator=operator)
        return {"backend": result, "deleted": result.get("deleted")}

    def _delete_alert(self, params: Dict[str, Any], operator: str) -> Dict[str, Any]:
        alert_id = _require_param(params, "alert_id")
        result = self._backend.delete(
            "/api/alerts/%s" % quote(str(alert_id), safe=""), operator=operator
        )
        return {"backend": result, "alert_id": alert_id}

    def _silence_alert(self, params: Dict[str, Any], operator: str) -> Dict[str, Any]:
        alert_id = _require_param(params, "alert_id")
        result = self._backend.post(
            "/api/alerts/%s/silence" % quote(str(alert_id), safe=""),
            {},
            operator=operator,
        )
        return {"backend": result, "alert_id": alert_id}

    # ------------------------------------------------------------------ #
    # 会话管理（Agent 本地 SQLite，不经过后端）
    # ------------------------------------------------------------------ #
    def _delete_conversation(self, params: Dict[str, Any], operator: str) -> Dict[str, Any]:
        conversation_id = _require_param(params, "conversation_id")
        deleted = self._conversations.delete(str(conversation_id))
        return {"deleted": deleted, "conversation_id": conversation_id}

    def _clear_conversations(self, params: Dict[str, Any], operator: str) -> Dict[str, Any]:
        keep = params.get("keep_current") or params.get("keep_id")
        deleted = self._conversations.clear(keep_id=str(keep) if keep else None)
        return {"deleted": deleted, "kept": str(keep) if keep else None}

    # ------------------------------------------------------------------ #
    # 场景配置
    # ------------------------------------------------------------------ #
    def _apply_scene_device(self, params: Dict[str, Any], operator: str) -> Dict[str, Any]:
        building_id = _require_param(params, "building_id")
        device = params.get("device")
        if not isinstance(device, dict):
            raise ActionError("invalid_params", "apply_scene_device 缺少 device 对象")
        payload = _sanitize_device(device)
        result = self._backend.post(
            "/api/scene/buildings/%s/devices" % quote(str(building_id), safe=""),
            payload,
            operator=operator,
        )
        return {"backend": result, "building_id": building_id, "device": payload}

    def _apply_scene_building(self, params: Dict[str, Any], operator: str) -> Dict[str, Any]:
        building = params.get("building")
        if not isinstance(building, dict):
            raise ActionError("invalid_params", "apply_scene_building 缺少 building 对象")
        payload = _sanitize_building(building)
        created = self._backend.post("/api/scene/buildings", payload, operator=operator)
        building_id = _extract_building_id(created) or str(payload.get("id") or "")
        devices_added: List[Dict[str, Any]] = []
        for device in params.get("devices") or []:
            if not isinstance(device, dict):
                continue
            device_payload = _sanitize_device(device)
            devices_added.append(
                self._backend.post(
                    "/api/scene/buildings/%s/devices" % quote(building_id, safe=""),
                    device_payload,
                    operator=operator,
                )
            )
        return {
            "backend": created,
            "building_id": building_id,
            "devices_added": len(devices_added),
        }

    def _confirm_scene_device(self, params: Dict[str, Any], operator: str) -> Dict[str, Any]:
        name = _require_param(params, "device_name")
        result = self._backend.post(
            "/api/scene/devices/%s/confirm" % quote(str(name), safe=""), {}, operator=operator
        )
        return {
            "backend": result,
            "device_name": name,
            "device": result.get("device") if isinstance(result, dict) else None,
        }

    def _remove_scene_device(self, params: Dict[str, Any], operator: str) -> Dict[str, Any]:
        name = _require_param(params, "device_name")
        result = self._backend.delete(
            "/api/scene/devices/%s" % quote(str(name), safe=""), operator=operator
        )
        return {
            "backend": result,
            "device_name": name,
            "deleted": result.get("deleted") if isinstance(result, dict) else None,
        }


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        params = json.loads(row["params"] or "{}")
    except ValueError:
        params = {}
    result: Optional[Any] = None
    if row["result"]:
        try:
            result = json.loads(row["result"])
        except ValueError:
            result = row["result"]
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "type": row["action_type"],
        "label": ACTION_LABELS.get(row["action_type"], row["action_type"]),
        "params": params if isinstance(params, dict) else {},
        "confirm_text": row["confirm_text"],
        "danger": bool(row["danger"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "executed_at": row["executed_at"],
        "result": result,
        "error": row["error"],
    }


def _require_param(params: Dict[str, Any], key: str) -> Any:
    value = params.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ActionError("invalid_params", "action 参数缺少 %s" % key)
    return value


def _extract_building_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    building = payload.get("building")
    if isinstance(building, dict) and building.get("id"):
        return str(building["id"])
    if payload.get("id"):
        return str(payload["id"])
    data = payload.get("data")
    if isinstance(data, dict):
        if data.get("id"):
            return str(data["id"])
        nested = data.get("building")
        if isinstance(nested, dict) and nested.get("id"):
            return str(nested["id"])
    return None


def _sanitize_building(building: Dict[str, Any]) -> Dict[str, Any]:
    """严格对齐后端 BuildingCreate 字段，避免多余键导致 422。"""
    payload: Dict[str, Any] = {"name": str(building.get("name") or "").strip()}
    if not payload["name"]:
        raise ActionError("invalid_params", "楼栋缺少 name")
    for key in ("id", "position", "size", "style", "status"):
        value = building.get(key)
        if value is not None:
            payload[key] = value
    return payload


def _sanitize_device(device: Dict[str, Any]) -> Dict[str, Any]:
    """严格对齐后端 DeviceUpsert 字段，避免多余键导致 422。"""
    payload: Dict[str, Any] = {"name": str(device.get("name") or "").strip()}
    if not payload["name"]:
        raise ActionError("invalid_params", "设备缺少 name")
    for key in ("type", "position", "status", "source", "model", "mgmt_ip"):
        value = device.get(key)
        if value is not None:
            payload[key] = value
    return payload
