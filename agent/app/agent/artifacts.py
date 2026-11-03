# -*- coding: utf-8 -*-
"""W16：设备接入配置文档 artifact（生成 / 存储 / 下载）。

- onboarding 生成接入方案时，同时生成 **Markdown 配置文档**；
- 文档落盘 ``agent/data/artifacts/``，SQLite ``artifacts`` 表记录元数据；
- Agent 回复携带 ``artifacts:[{id,kind,title,filename,size}]``，前端/API 可下载原文；
- 设计：artifact id 随机（不可猜），无鉴权；仅存本机，不进 git（data/ 已忽略）。
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ARTIFACT_KIND_DEVICE_CONFIG = "device_config"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at REAL NOT NULL,
    content TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_conversation ON artifacts(conversation_id, created_at);
"""

_BAD_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


def sanitize_filename(name: str) -> str:
    cleaned = _BAD_FILENAME_RE.sub("_", str(name or "").strip()).strip("._-")
    return cleaned[:120] or "artifact"


def markdown_to_text(markdown: str) -> str:
    """轻量 Markdown → 纯文本（方便粘贴；保留代码内容）。"""
    lines: List[str] = []
    in_code = False
    for line in str(markdown or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            lines.append(line)
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if all(set(cell) <= set("-: ") for cell in cells):
                continue
            lines.append(" | ".join(cell for cell in cells if cell))
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        line = re.sub(r"`([^`]*)`", r"\1", line)
        lines.append(line.rstrip())
    return "\n".join(lines).strip() + "\n"


def build_device_config_markdown(
    *,
    hostname: str,
    device_label: str,
    device_model: str,
    device_kind: str,
    building_name: str,
    building_id: str,
    mgmt_ip: str,
    vlan: Optional[int],
    gateway: str,
    uplink: Dict[str, Any],
    cabling: List[Dict[str, Any]],
    cli: str,
    verification: List[str],
    undo: List[str],
    scene_type: str,
    position: Any,
    new_building: bool = False,
    generated_at: Optional[datetime] = None,
) -> str:
    """生成可下载的 Markdown 设备接入配置文档。"""
    now = generated_at or datetime.now()
    lines: List[str] = [
        "# %s 接入配置文档" % hostname,
        "",
        "> 生成时间：%s  " % now.strftime("%Y-%m-%d %H:%M"),
        "> 生成工具：campus-twin Agent · 接入引导（onboarding）  ",
        "> 说明：以下配置来自项目知识库模板，接入前请由网络负责人复核；演示环境使用 PT 仿真。",
        "",
        "## 一、设备信息",
        "",
        "| 项目 | 内容 |",
        "|---|---|",
        "| 主机名 | **%s** |" % hostname,
        "| 设备型号 | %s（%s） |" % (device_label, device_model),
        "| 所属楼栋 | %s（场景 ID：%s）%s |"
        % (building_name, building_id or "待后端分配", "（新建）" if new_building else ""),
        "| 管理 IP | %s |" % mgmt_ip,
        "| VLAN / 网关 | %s / %s |"
        % (("VLAN %s" % vlan) if vlan is not None else "不适用（终端）", gateway),
        "| 场景类型 | `%s` |" % scene_type,
        "| 3D 相对位置 | `%s`（pending → 确认后 confirmed） |" % position,
        "",
        "## 二、端口连线表",
        "",
    ]
    if cabling:
        lines.extend(
            [
                "| 本端设备 | 本端端口 | 对端设备 | 对端端口 | 线缆 | 用途 |",
                "|---|---|---|---|---|---|",
            ]
        )
        for item in cabling:
            lines.append(
                "| %s | %s | %s | %s | %s | %s |"
                % (
                    item.get("local_device", ""),
                    item.get("local_port", ""),
                    item.get("peer_device", ""),
                    item.get("peer_port", ""),
                    item.get("cable", ""),
                    item.get("purpose", ""),
                )
            )
    else:
        lines.append("- 无端口连线（请按现场拓扑补充）。")
    lines.append("")

    is_terminal = str(device_kind).lower() in ("pc", "server")
    if is_terminal:
        lines.extend(
            [
                "## 三、PT GUI 配置步骤（PC/服务器无 IOS CLI）",
                "",
                "```text",
                str(cli).strip(),
                "```",
                "",
                "**IP 配置参数**",
                "",
                "| 项目 | 值 |",
                "|---|---|",
                "| IP 地址 | %s |" % mgmt_ip,
                "| 子网掩码 | 255.255.255.0 |",
                "| 默认网关 | %s |" % gateway,
                "| DNS | 按园区实际 DNS 填写（演示可用 223.5.5.5） |",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "## 三、完整 CLI 配置（可直接复制粘贴）",
                "",
                "```cisco",
                str(cli).strip(),
                "```",
                "",
            ]
        )

    lines.extend(
        [
            "## 四、验证命令",
            "",
            *["%d. `%s`" % (index, item) for index, item in enumerate(verification, start=1)],
            "",
            "## 五、撤销 / 回滚命令",
            "",
            "```text",
            *[str(item) for item in undo],
            "```",
            "",
            "## 六、注意事项",
            "",
            "1. 本方案先以 **pending** 状态加入 3D 场景，需在对话下方 action 卡片中确认后才会生效；",
            "2. PT 为仿真环境，设备/端口/网段以本项目现场拓扑为准；接入前请复核上联端口与 VLAN；",
            "3. 配置中的密码/密钥请按现场安全规范替换，不要把生产密码写入文档/仓库；",
            "4. 若设备类型为 PC/服务器，只需按 GUI 步骤配置 IP/掩码/网关，无需粘贴 IOS CLI；",
            "5. 改名/删除场景设备会产生危险操作 action，需人工确认后执行。",
            "",
            "---",
            "*本文档由 campus-twin Agent 自动生成；演示数据请标注“演示样例”。*",
            "",
        ]
    )
    return "\n".join(lines)


class ArtifactStore:
    """配置文档 artifact 的 SQLite + 文件存储。"""

    def __init__(self, db_path: Path | str, artifacts_dir: Optional[Path | str] = None) -> None:
        self.db_path = str(db_path)
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else Path(self.db_path).parent / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def save(
        self,
        conversation_id: Optional[str],
        *,
        kind: str,
        title: str,
        filename: str,
        content: str,
    ) -> Dict[str, Any]:
        """保存 artifact（文件名自动去重），返回不含正文的 descriptor。"""
        safe_name = sanitize_filename(filename)
        if not safe_name.lower().endswith(".md"):
            safe_name += ".md"
        target = self.artifacts_dir / safe_name
        base_name = safe_name[:-3] if safe_name.lower().endswith(".md") else safe_name
        suffix = 2
        while target.exists():
            target = self.artifacts_dir / ("%s-%d.md" % (base_name, suffix))
            suffix += 1
        artifact_id = "art_" + uuid.uuid4().hex[:12]
        payload = str(content or "")
        target.write_text(payload, encoding="utf-8")
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO artifacts
                   (id, conversation_id, kind, title, filename, path, size, created_at, content)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    artifact_id,
                    conversation_id,
                    str(kind),
                    str(title),
                    target.name,
                    str(target),
                    len(payload.encode("utf-8")),
                    now,
                    payload,
                ),
            )
            connection.commit()
        return {
            "id": artifact_id,
            "kind": str(kind),
            "title": str(title),
            "filename": target.name,
            "size": len(payload.encode("utf-8")),
        }

    # ------------------------------------------------------------------ #
    # 查
    # ------------------------------------------------------------------ #
    def get(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (str(artifact_id),)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def content(self, artifact_id: str) -> Optional[str]:
        record = self.get(artifact_id)
        if record is None:
            return None
        content = record.pop("content", None)
        if content:
            return str(content)
        path = Path(str(record.get("path") or ""))
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def descriptor(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        record = self.get(artifact_id)
        if record is None:
            return None
        return {
            "id": record["id"],
            "kind": record["kind"],
            "title": record["title"],
            "filename": record["filename"],
            "size": record["size"],
        }

    def list_since(
        self,
        conversation_id: Optional[str],
        since_ts: float,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM artifacts WHERE created_at>=?"
        args: List[Any] = [float(since_ts)]
        if conversation_id:
            query += " AND conversation_id=?"
            args.append(str(conversation_id))
        query += " ORDER BY created_at ASC LIMIT ?"
        args.append(max(1, min(100, int(limit))))
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, tuple(args)).fetchall()
        descriptors: List[Dict[str, Any]] = []
        for row in rows:
            record = _row_to_dict(row)
            descriptors.append(
                {
                    "id": record["id"],
                    "kind": record["kind"],
                    "title": record["title"],
                    "filename": record["filename"],
                    "size": record["size"],
                }
            )
        return descriptors

    def count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()
        return int(row["n"] or 0)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "kind": row["kind"],
        "title": row["title"],
        "filename": row["filename"],
        "path": row["path"],
        "size": int(row["size"] or 0),
        "created_at": float(row["created_at"] or 0.0),
        "content": row["content"] if "content" in row.keys() else None,
    }
