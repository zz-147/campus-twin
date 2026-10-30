# -*- coding: utf-8 -*-
"""Action 确认协议 + 执行器单元测试。"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest

from app.agent.actions import (
    ACTION_TYPES,
    ActionError,
    ActionExecutor,
    ActionStore,
)
from app.agent.backend import BackendError, FixtureBackendClient
from app.agent.conversations import ConversationStore
from app.agent.eval.fixture_data import build_scenario_snapshot


def _executor(backend: Any, conversations: ConversationStore) -> ActionExecutor:
    return ActionExecutor(backend, conversations)


# --------------------------------------------------------------------------- #
# 存储与状态机
# --------------------------------------------------------------------------- #
def test_create_defaults(action_store: ActionStore) -> None:
    action = action_store.create(
        "clear_cleared_alerts", {"scope": "cleared"}, "将清空 3 条已恢复告警，确认？"
    )
    assert action["id"].startswith("act_")
    assert action["type"] == "clear_cleared_alerts"
    assert action["status"] == "pending"
    assert action["danger"] is True  # 默认危险类型
    assert action["confirm_text"]
    assert action["expires_at"] > action["created_at"]
    assert action_store.count("pending") == 1
    assert action_store.list_pending() == [action]


def test_create_rejects_unknown_type(action_store: ActionStore) -> None:
    with pytest.raises(ActionError) as exc_info:
        action_store.create("drop_database", {})
    assert exc_info.value.code == "invalid_type"
    assert set(ACTION_TYPES)


def test_confirm_calls_executor_once(action_store: ActionStore, conversation_store: ConversationStore) -> None:
    calls: List[Dict[str, Any]] = []

    class FakeExecutor:
        def execute(self, action_type: str, params: Dict[str, Any], operator: str = "agent"):
            calls.append({"type": action_type, "params": params, "operator": operator})
            return {"deleted": 3}

    action = action_store.create("clear_cleared_alerts", {"scope": "cleared"})
    done = action_store.confirm(action["id"], FakeExecutor())  # type: ignore[arg-type]
    assert done["status"] == "executed"
    assert done["result"] == {"deleted": 3}
    assert calls == [
        {"type": "clear_cleared_alerts", "params": {"scope": "cleared"}, "operator": "agent"}
    ]
    with pytest.raises(ActionError) as exc_info:
        action_store.confirm(action["id"], FakeExecutor())  # type: ignore[arg-type]
    assert exc_info.value.code == "not_pending"


def test_cancel_and_not_found(action_store: ActionStore) -> None:
    action = action_store.create("silence_alert", {"alert_id": "19"})
    cancelled = action_store.cancel(action["id"])
    assert cancelled["status"] == "cancelled"
    with pytest.raises(ActionError) as exc_info:
        action_store.cancel(action["id"])
    assert exc_info.value.code == "not_pending"
    with pytest.raises(ActionError) as exc_info:
        action_store.cancel("act_missing")
    assert exc_info.value.code == "not_found"


def test_expiry(monkeypatch: pytest.MonkeyPatch, action_store: ActionStore) -> None:
    action = action_store.create("delete_alert", {"alert_id": "19"}, ttl_s=30)
    assert action["status"] == "pending"
    real_time = time.time

    class Executor:
        def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
            raise AssertionError("过期 action 不应被执行")

    monkeypatch.setattr("app.agent.actions.time.time", lambda: real_time() + 9999)
    expired = action_store.get(action["id"])
    assert expired is not None and expired["status"] == "expired"
    with pytest.raises(ActionError) as exc_info:
        action_store.confirm(action["id"], Executor())  # type: ignore[arg-type]
    assert exc_info.value.code == "expired"


def test_failed_execution_is_recorded(action_store: ActionStore) -> None:
    class BrokenExecutor:
        def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
            raise BackendError("HTTP 409：active 告警不可删除")

    action = action_store.create("delete_alert", {"alert_id": "19"}, confirm_text="确认？")
    failed = action_store.confirm(action["id"], BrokenExecutor())  # type: ignore[arg-type]
    assert failed["status"] == "failed"
    assert "active 告警不可删除" in failed["error"]


# --------------------------------------------------------------------------- #
# 执行器：数据治理
# --------------------------------------------------------------------------- #
@pytest.fixture()
def steward_backend(base_snapshot: Dict[str, Any], scenario_map: Dict[str, Dict[str, Any]]) -> FixtureBackendClient:
    return FixtureBackendClient(build_scenario_snapshot(base_snapshot, scenario_map["s26"]))


def test_executor_clear_cleared_alerts(
    steward_backend: FixtureBackendClient, conversation_store: ConversationStore
) -> None:
    executor = _executor(steward_backend, conversation_store)
    result = executor.execute("clear_cleared_alerts", {"scope": "cleared"})
    assert result["deleted"] == 3
    assert steward_backend.get("/api/alerts")["summary"]["cleared"] == 0
    audit = steward_backend.get("/api/audit")
    assert audit["count"] == 1
    assert audit["items"][0]["operator"] == "agent"


def test_executor_delete_and_silence(
    steward_backend: FixtureBackendClient, conversation_store: ConversationStore
) -> None:
    executor = _executor(steward_backend, conversation_store)
    deleted = executor.execute("delete_alert", {"alert_id": "101"})
    assert deleted["alert_id"] == "101"
    silenced = executor.execute("silence_alert", {"alert_id": "104"})
    assert silenced["alert_id"] == "104"
    summary = steward_backend.get("/api/alerts")["summary"]
    assert summary["cleared"] == 2
    assert summary["silenced"] == 1


def test_executor_delete_active_alert_rejected(
    steward_backend: FixtureBackendClient, conversation_store: ConversationStore
) -> None:
    executor = _executor(steward_backend, conversation_store)
    with pytest.raises(BackendError) as exc_info:
        executor.execute("delete_alert", {"alert_id": "104"})
    assert "409" in str(exc_info.value)


def test_executor_conversation_actions(
    fixture_backend: FixtureBackendClient, conversation_store: ConversationStore
) -> None:
    keep = conversation_store.create(title="当前")
    old = [conversation_store.create(title="历史 %d" % index) for index in range(2)]
    executor = _executor(fixture_backend, conversation_store)
    result = executor.execute("clear_conversations", {"keep_current": keep["id"]})
    assert result["deleted"] == 2
    assert conversation_store.count() == 1
    second = conversation_store.create(title="待删除")
    removed = executor.execute("delete_conversation", {"conversation_id": second["id"]})
    assert removed["deleted"] is True


# --------------------------------------------------------------------------- #
# 执行器：场景配置
# --------------------------------------------------------------------------- #
def test_executor_apply_scene_device_and_confirm(
    base_snapshot: Dict[str, Any], conversation_store: ConversationStore
) -> None:
    backend = FixtureBackendClient(base_snapshot)
    executor = _executor(backend, conversation_store)
    device = {
        "name": "SW-SS-2961",
        "type": "switch",
        "position": {"x": 0.0, "y": 14.3, "z": -140.0},
        "status": "pending",
        "source": "agent",
        "model": "Catalyst 2960",
        "mgmt_ip": "192.168.10.14",
    }
    result = executor.execute(
        "apply_scene_device", {"building_id": "dorm", "device": device}
    )
    assert result["building_id"] == "dorm"
    scene = backend.get("/api/scene")
    dorm = next(item for item in scene["buildings"] if item["id"] == "dorm")
    assert any(item["name"] == "SW-SS-2961" for item in dorm["devices"])
    confirmed = executor.execute("confirm_scene_device", {"device_name": "SW-SS-2961"})
    assert confirmed["device"]["status"] == "confirmed"
    removed = executor.execute("remove_scene_device", {"device_name": "SW-SS-2961"})
    assert removed["deleted"] == 1


def test_executor_apply_scene_building(
    base_snapshot: Dict[str, Any], conversation_store: ConversationStore
) -> None:
    backend = FixtureBackendClient(base_snapshot)
    executor = _executor(backend, conversation_store)
    building = {
        "id": "bld-lib",
        "name": "图书馆",
        "position": {"x": 240.0, "z": -140.0},
        "size": {"width": 64.0, "depth": 60.0, "height": 24.0},
        "style": {"color": "#2f5d50", "borderColor": "#7dd3fc", "opacity": 0.35},
        "status": "pending",
    }
    device = {
        "name": "SW-LIB-1",
        "type": "switch",
        "position": {"x": 240.0, "y": 15.8, "z": -140.0},
        "status": "pending",
        "source": "agent",
        "model": "Catalyst 2960",
        "mgmt_ip": "192.168.10.14",
    }
    result = executor.execute(
        "apply_scene_building", {"building": building, "devices": [device]}
    )
    assert result["building_id"] == "bld-lib"
    assert result["devices_added"] == 1
    scene = backend.get("/api/scene")
    created = next(item for item in scene["buildings"] if item["name"] == "图书馆")
    assert created["devices"][0]["name"] == "SW-LIB-1"
    assert scene["version"] > base_snapshot["scene"]["version"]


def test_executor_requires_params(
    fixture_backend: FixtureBackendClient, conversation_store: ConversationStore
) -> None:
    executor = _executor(fixture_backend, conversation_store)
    with pytest.raises(ActionError):
        executor.execute("apply_scene_device", {"building_id": "dorm"})
    with pytest.raises(ActionError):
        executor.execute("unknown_type", {})
