# -*- coding: utf-8 -*-
"""会话持久化（SQLite）：conversations / messages。

设计（见 docs/W9-Agent扩展设计.md §4.1）：
- ``conversations(id, title, created_at, updated_at)``
- ``messages(id, conversation_id, role, content, meta, created_at)``

- 每次操作独立建连接（低并发、WAL 模式），避免线程/进程间共享连接问题；
- 消息 ``meta`` 存 JSON（provider/model/actions/trace 摘要等），便于前端回放；
- 会话标题默认取首条用户问题（截断 40 字）。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_HISTORY_MESSAGES = 16


def new_conversation_id() -> str:
    return "conv_" + uuid.uuid4().hex[:12]


def title_from_question(question: str, limit: int = 40) -> str:
    text = " ".join((question or "").split())
    if not text:
        return "新会话"
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ConversationStore:
    """SQLite 会话存储（无外部依赖）。"""

    def __init__(self, db_path: Path) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------ #
    # 建表 / 连接
    # ------------------------------------------------------------------ #
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
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    meta TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, id);
                """
            )

    @property
    def db_path(self) -> str:
        return self._path

    # ------------------------------------------------------------------ #
    # 会话 CRUD
    # ------------------------------------------------------------------ #
    def create(self, title: str = "", conversation_id: Optional[str] = None) -> Dict[str, Any]:
        now = time.time()
        conversation_id = conversation_id or new_conversation_id()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?,?,?,?)",
                (conversation_id, title or "", now, now),
            )
        return {
            "id": conversation_id,
            "title": title or "",
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }

    def exists(self, conversation_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        return row is not None

    def get(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                return None
            messages = connection.execute(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY id ASC",
                (conversation_id,),
            ).fetchall()
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": len(messages),
            "messages": [_message_to_dict(item) for item in messages],
        }

    def list(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            summaries: List[Dict[str, Any]] = []
            for row in rows:
                count_row = connection.execute(
                    "SELECT COUNT(*) AS c FROM messages WHERE conversation_id=?",
                    (row["id"],),
                ).fetchone()
                last_row = connection.execute(
                    "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                preview = ""
                if last_row is not None:
                    preview = " ".join(str(last_row["content"] or "").split())[:80]
                summaries.append(
                    {
                        "id": row["id"],
                        "title": row["title"] or "新会话",
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "message_count": int(count_row["c"]) if count_row else 0,
                        "preview": preview,
                    }
                )
        return summaries

    def delete(self, conversation_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE id=?", (conversation_id,)
            )
            connection.execute(
                "DELETE FROM messages WHERE conversation_id=?", (conversation_id,)
            )
        return cursor.rowcount > 0

    def clear(self, keep_id: Optional[str] = None) -> int:
        """清空全部会话历史；``keep_id`` 指定的会话保留（前端「保留当前会话」）。"""
        with self._connect() as connection:
            if keep_id:
                ids = [
                    row["id"]
                    for row in connection.execute(
                        "SELECT id FROM conversations WHERE id != ?", (keep_id,)
                    ).fetchall()
                ]
            else:
                ids = [
                    row["id"]
                    for row in connection.execute("SELECT id FROM conversations").fetchall()
                ]
            for conversation_id in ids:
                connection.execute(
                    "DELETE FROM messages WHERE conversation_id=?", (conversation_id,)
                )
                connection.execute(
                    "DELETE FROM conversations WHERE id=?", (conversation_id,)
                )
        return len(ids)

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS c FROM conversations").fetchone()
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------ #
    # 消息
    # ------------------------------------------------------------------ #
    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        meta: Optional[Dict[str, Any]] = None,
        auto_title: bool = False,
    ) -> Dict[str, Any]:
        now = time.time()
        meta_json = json.dumps(meta or {}, ensure_ascii=False, default=str)
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO messages(conversation_id, role, content, meta, created_at)"
                " VALUES (?,?,?,?,?)",
                (conversation_id, role, content or "", meta_json, now),
            )
            if auto_title:
                row = connection.execute(
                    "SELECT title FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone()
                if row is not None and not row["title"] and role == "user":
                    connection.execute(
                        "UPDATE conversations SET title=? WHERE id=?",
                        (title_from_question(content), conversation_id),
                    )
            connection.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id)
            )
        return {
            "id": cursor.lastrowid,
            "conversation_id": conversation_id,
            "role": role,
            "content": content or "",
            "meta": meta or {},
            "created_at": now,
        }

    def recent_messages(
        self, conversation_id: str, limit: int = MAX_HISTORY_MESSAGES
    ) -> List[Dict[str, Any]]:
        """取最近 N 条消息（user/assistant），用于续聊上下文。"""
        limit = max(1, min(int(limit), 100))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE conversation_id=? AND role IN ('user','assistant')"
                " ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        messages = [_message_to_dict(item) for item in reversed(rows)]
        return [
            {"role": item["role"], "content": item["content"]}
            for item in messages
            if item["content"]
        ]

    def history_for_llm(
        self, conversation_id: str, limit: int = 8
    ) -> List[Dict[str, str]]:
        """转换为 LLM 消息格式（最近若干轮，去掉当前问题之外的历史）。"""
        return self.recent_messages(conversation_id, limit=limit)


def _message_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        meta = json.loads(row["meta"] or "{}")
    except ValueError:
        meta = {}
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "role": row["role"],
        "content": row["content"],
        "meta": meta if isinstance(meta, dict) else {},
        "created_at": row["created_at"],
    }
