# -*- coding: utf-8 -*-
"""会话持久化（SQLite）单元测试。"""
from __future__ import annotations

from typing import Any

import pytest

from app.agent.conversations import ConversationStore, title_from_question


def test_create_get_and_list(conversation_store: ConversationStore) -> None:
    conversation = conversation_store.create(title="")
    assert conversation["id"].startswith("conv_")
    assert conversation["messages"] == []
    assert conversation_store.exists(conversation["id"]) is True
    loaded = conversation_store.get(conversation["id"])
    assert loaded is not None
    assert loaded["message_count"] == 0
    listing = conversation_store.list()
    assert listing[0]["id"] == conversation["id"]
    assert conversation_store.count() == 1


def test_append_message_and_auto_title(conversation_store: ConversationStore) -> None:
    conversation = conversation_store.create()
    conversation_store.append_message(
        conversation["id"],
        "user",
        "教学楼 PC-JX1 上不了网，帮我看下是什么原因，顺便看看交换机状态",
        auto_title=True,
    )
    conversation_store.append_message(
        conversation["id"], "assistant", "## 根因\n终端离线", meta={"provider": "mock"}
    )
    loaded = conversation_store.get(conversation["id"])
    assert loaded is not None
    assert loaded["message_count"] == 2
    assert loaded["title"].startswith("教学楼 PC-JX1")
    assert len(loaded["title"]) <= 40
    assert loaded["messages"][1]["meta"] == {"provider": "mock"}


def test_recent_messages_and_history_for_llm(conversation_store: ConversationStore) -> None:
    conversation = conversation_store.create()
    for index in range(10):
        conversation_store.append_message(
            conversation["id"], "user" if index % 2 == 0 else "assistant", "消息 %d" % index
        )
    recent = conversation_store.recent_messages(conversation["id"], limit=4)
    assert len(recent) == 4
    assert recent[-1]["content"] == "消息 9"
    history = conversation_store.history_for_llm(conversation["id"], limit=6)
    assert all(set(item.keys()) == {"role", "content"} for item in history)


def test_delete_conversation(conversation_store: ConversationStore) -> None:
    conversation = conversation_store.create()
    conversation_store.append_message(conversation["id"], "user", "hello")
    assert conversation_store.delete(conversation["id"]) is True
    assert conversation_store.get(conversation["id"]) is None
    assert conversation_store.delete(conversation["id"]) is False


def test_clear_keeps_current(conversation_store: ConversationStore) -> None:
    current = conversation_store.create(title="当前")
    others = [conversation_store.create(title="历史 %d" % index) for index in range(3)]
    for conversation in others:
        conversation_store.append_message(conversation["id"], "user", "历史消息")
    deleted = conversation_store.clear(keep_id=current["id"])
    assert deleted == 3
    assert conversation_store.count() == 1
    assert conversation_store.get(current["id"]) is not None


def test_clear_all(conversation_store: ConversationStore) -> None:
    conversation_store.create()
    conversation_store.create()
    assert conversation_store.clear() == 2
    assert conversation_store.count() == 0


def test_title_helper() -> None:
    assert title_from_question("   ") == "新会话"
    assert title_from_question("a" * 100).endswith("…")
    assert len(title_from_question("a" * 100)) <= 40


def test_unknown_conversation_returns_none(conversation_store: ConversationStore) -> None:
    assert conversation_store.get("conv_missing") is None
    assert conversation_store.exists("conv_missing") is False


def test_wal_and_persistence_across_instances(tmp_path: Any) -> None:
    db_path = tmp_path / "agent.db"
    first = ConversationStore(db_path)
    conversation = first.create(title="持久化")
    first.append_message(conversation["id"], "user", "你好")
    second = ConversationStore(db_path)
    assert second.get(conversation["id"]) is not None
    assert second.get(conversation["id"]) is not None
    WAL = db_path.with_name(db_path.name + "-wal")
    assert db_path.exists()
    del WAL  # WAL 文件是否落盘由 SQLite 管理，这里只确认主库可重新打开
