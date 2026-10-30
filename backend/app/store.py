# -*- coding: utf-8 -*-
"""SQLite 持久化层（WAL 模式）。

存什么
======
- 设备/主机快照：每个设备/主机一行，保存最近一次采集结果，便于重启后排查
  以及运维侧直接查库。
- 拓扑快照：只在拓扑内容变化时新增一行（payload_hash 对比），避免 5 秒一次
  轮询把库写爆；未变化时仅更新 captured_at。
- 健康问题：issue_id 去重，记录 first_seen / last_seen / active，已消失的问题
  标 active=0 而不是删除（保留故障时间线）。
- 告警：检测引擎输出的告警。每条告警是一个“事件生命周期”（incident）：
  ``state=active``（进行中）/ ``cleared``（已恢复）/ ``info``（一次性信息），
  同一 dedup_key 同时最多一条 active；恢复只更新状态，不再逐条新增“已清除”行
  （W3 降噪）。带 observed_at（宿主机时间）与 source_ts（NC 原始时间）两个时间基准；
  历史告警默认保留 7 天（可配置），后台定期清理。
- 指标点：(device_id, metric, interface, ts) 时序，定期按保留时长清理。

为什么不用 ORM / 连接池
=======================
MVP 数据量小（单机、5 秒一轮），标准库 sqlite3 + 每次操作短连接最简单可靠；
WAL 模式下读写并发互不阻塞。每次操作新建连接可天然避开 FastAPI 线程池
跨线程使用同一 connection 的问题。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

from app.collectors.base import CollectResult
from app.models import Alert, MetricPoint, Topology, parse_speed_mbps

logger = logging.getLogger("campus_twin.store")

SCHEMA_VERSION = 4

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS device_snapshots (
    device_id   TEXT PRIMARY KEY,
    hostname    TEXT,
    source      TEXT NOT NULL,
    captured_at REAL NOT NULL,
    data        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topology_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at REAL NOT NULL,
    source      TEXT NOT NULL,
    node_count  INTEGER NOT NULL DEFAULT 0,
    link_count  INTEGER NOT NULL DEFAULT 0,
    payload_hash TEXT NOT NULL,
    data        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS host_snapshots (
    host_id     TEXT PRIMARY KEY,
    source      TEXT,
    captured_at REAL,
    data        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_issues (
    issue_id    TEXT PRIMARY KEY,
    name        TEXT,
    severity    TEXT,
    source      TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    data        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type  TEXT NOT NULL,
    severity    TEXT NOT NULL,
    title       TEXT NOT NULL,
    detail      TEXT,
    dedup_key   TEXT,
    device_id   TEXT,
    link_id     TEXT,
    issue_id    TEXT,
    created_at  REAL NOT NULL,
    source      TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    -- W3 时间基准：宿主机采集时间 vs NC 原始时间字符串
    observed_at REAL,
    source_ts   TEXT,
    -- W3 生命周期：active / cleared / info
    state       TEXT NOT NULL DEFAULT 'active',
    resolved_at REAL,
    clear_reason TEXT,
    stale_source INTEGER NOT NULL DEFAULT 0,
    -- W9 数据管理：软删除（deleted_at 非空默认不出现在列表）+ 活跃告警静默
    deleted_at  REAL,
    status      TEXT NOT NULL DEFAULT 'open',
    silenced_at REAL
);

CREATE TABLE IF NOT EXISTS metric_points (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    unit        TEXT,
    interface   TEXT,
    ts          REAL NOT NULL,
    source      TEXT,
    tags        TEXT,
    -- W3.1 物理约束：端口速率上限（bps）与是否被钳制（旧数据为 NULL/0）
    speed_bps   REAL,
    clamped     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

-- W9 场景配置：单行 JSON 文档（version 每次变更 +1，首次 GET 时 seed 默认 4 楼栋/12 设备）
CREATE TABLE IF NOT EXISTS scene_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    version     INTEGER NOT NULL DEFAULT 1,
    data        TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

-- W9 操作审计：所有写操作（告警管理 / 场景配置）记录 operator/action/target/detail
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    operator    TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT,
    detail      TEXT,
    result      TEXT
);

CREATE INDEX IF NOT EXISTS idx_topology_captured ON topology_snapshots(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_issues_active ON health_issues(active, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_device_ts ON metric_points(device_id, ts);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metric_points(ts);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, created_at DESC);
"""


class Store:
    """SQLite 存取封装（同步接口，调用方在事件循环里直接调用，单次写 < 1ms）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).resolve().parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 连接 / 建表
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """建表 + 迁移历史库 + 记录 schema 版本（幂等，可重复调用）。"""
        with self._db() as conn:
            conn.executescript(SCHEMA_SQL)
            self._migrate(conn)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
        logger.info("SQLite 已就绪：%s（WAL，schema v%d）", self.db_path, SCHEMA_VERSION)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """历史库幂等迁移。

        - v1 -> v2：alerts 增加 W3 时间基准/生命周期字段；
        - v2 -> v3：metric_points 增加 W3.1 物理约束字段（speed_bps / clamped）。
        迁移只 ADD COLUMN / 回填，不删除任何历史行。
        """
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()
        }
        additions = (
            ("observed_at", "REAL"),
            ("source_ts", "TEXT"),
            ("state", "TEXT NOT NULL DEFAULT 'active'"),
            ("resolved_at", "REAL"),
            ("clear_reason", "TEXT"),
            ("stale_source", "INTEGER NOT NULL DEFAULT 0"),
        )
        for name, ddl in additions:
            if name not in columns:
                conn.execute("ALTER TABLE alerts ADD COLUMN {} {}".format(name, ddl))
        conn.execute("UPDATE alerts SET observed_at=created_at WHERE observed_at IS NULL")
        conn.execute("UPDATE alerts SET state='active' WHERE state IS NULL")
        # 依赖新列的索引放在迁移之后创建（旧库 ALTER 前不能建）
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_state ON alerts(state, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_dedup ON alerts(dedup_key, state)"
        )

        # v2 -> v3：metric_points 物理约束字段
        metric_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(metric_points)").fetchall()
        }
        metric_additions = (
            ("speed_bps", "REAL"),
            ("clamped", "INTEGER NOT NULL DEFAULT 0"),
        )
        for name, ddl in metric_additions:
            if name not in metric_columns:
                conn.execute(
                    "ALTER TABLE metric_points ADD COLUMN {} {}".format(name, ddl)
                )
        # 旧指标点没有端口速率上下文：尽量用最近一次拓扑快照回填 speed_bps，
        # 并把历史上因“速率单位解析错误”产生的超物理上限值钳制回去（W3.1）。
        self._backfill_metric_speed(conn)

        # v3 -> v4：告警数据管理字段（软删除 / 静默）；scene_config/audit_log 由
        # SCHEMA_SQL 的 CREATE TABLE IF NOT EXISTS 负责创建。迁移只加列不回删数据。
        alert_columns_v4 = {
            row["name"] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()
        }
        alert_additions_v4 = (
            ("deleted_at", "REAL"),
            ("status", "TEXT NOT NULL DEFAULT 'open'"),
            ("silenced_at", "REAL"),
        )
        for name, ddl in alert_additions_v4:
            if name not in alert_columns_v4:
                conn.execute("ALTER TABLE alerts ADD COLUMN {} {}".format(name, ddl))
        conn.execute("UPDATE alerts SET status='open' WHERE status IS NULL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_deleted ON alerts(deleted_at)"
        )

    def _backfill_metric_speed(self, conn: sqlite3.Connection) -> None:
        """迁移回填：用最近拓扑快照为历史指标补 speed_bps，并钳制越界旧值。

        背景：W3.1 之前端口速率单位被误当成 Mbps（NC 实际是 Kbps），历史
        ``if_in_bps`` 可能高达 1.5e10 而端口只有 100Mbps。迁移时按当前拓扑的
        真实端口速率回填 ``speed_bps`` 并把 value 钳制到上限（clamped=1），
        保证 ``/api/metrics`` 历史+新数据都满足 bps <= speed_bps。
        """
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(metric_points)").fetchall()
        }
        if "speed_bps" not in columns:
            return
        row = conn.execute(
            "SELECT data FROM topology_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return
        try:
            topology = Topology.model_validate_json(row["data"])
        except Exception:  # noqa: BLE001 - 旧快照格式异常时跳过回填
            return
        # (link_id, device_id, interface) -> speed_bps
        port_speeds: Dict[Any, float] = {}
        for link in topology.links:
            start = parse_speed_mbps(link.start_port_speed)
            end = parse_speed_mbps(link.end_port_speed)
            if start and link.start_port_name:
                port_speeds[(link.id, link.source, link.start_port_name)] = start * 1_000_000.0
            if end and link.end_port_name:
                port_speeds[(link.id, link.target, link.end_port_name)] = end * 1_000_000.0
        if not port_speeds:
            return

        legacy_rows = conn.execute(
            "SELECT id, device_id, metric, value, interface, tags FROM metric_points "
            "WHERE speed_bps IS NULL AND metric IN "
            "('if_in_bps','if_out_bps','if_utilization_pct')"
        ).fetchall()
        updated = 0
        clamped_count = 0
        for item in legacy_rows:
            try:
                tags = json.loads(item["tags"] or "{}")
            except (TypeError, ValueError):
                tags = {}
            link_id = tags.get("link_id")
            speed_bps = port_speeds.get(
                (link_id, item["device_id"], item["interface"])
            )
            if speed_bps is None and link_id:
                # 端口名对不上时：用同链路同接口名 / 该链路任意端口上限兜底
                candidates = [
                    value
                    for (candidate_link, _device, port), value in port_speeds.items()
                    if candidate_link == link_id and port == item["interface"]
                ]
                if not candidates:
                    candidates = [
                        value
                        for (candidate_link, _device, _port), value in port_speeds.items()
                        if candidate_link == link_id
                    ]
                if candidates:
                    speed_bps = max(candidates)
            if not speed_bps:
                continue
            value = float(item["value"])
            if item["metric"] == "if_utilization_pct":
                new_value = min(max(value, 0.0), 100.0)
                was_clamped = value > 100.0
            else:
                new_value = min(max(value, 0.0), speed_bps)
                was_clamped = value > speed_bps
            conn.execute(
                "UPDATE metric_points SET value=?, speed_bps=?, clamped=? WHERE id=?",
                (
                    new_value,
                    speed_bps,
                    1 if was_clamped else 0,
                    item["id"],
                ),
            )
            updated += 1
            if was_clamped:
                clamped_count += 1
        if updated:
            logger.info(
                "迁移回填历史指标 speed_bps：%d 条（其中 %d 条超物理上限已钳制）",
                updated,
                clamped_count,
            )

    def journal_mode(self) -> str:
        """返回当前 journal_mode（测试用来确认 WAL 生效）。"""
        with self._db() as conn:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower() if row else ""

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #
    def save_snapshot(self, result: CollectResult) -> None:
        """保存一轮采集快照（设备/主机覆盖式，拓扑按内容变化追加）。"""
        captured_at = result.collected_at
        with self._db() as conn:
            # 设备/主机用“快照覆盖”语义：当轮没出现的设备不再留在最新快照里。
            conn.execute("DELETE FROM device_snapshots")
            conn.executemany(
                "INSERT INTO device_snapshots(device_id, hostname, source, captured_at, data) "
                "VALUES(?, ?, ?, ?, ?)",
                [
                    (
                        device.id,
                        device.hostname,
                        device.source,
                        captured_at,
                        device.model_dump_json(),
                    )
                    for device in result.devices
                ],
            )
            conn.execute("DELETE FROM host_snapshots")
            conn.executemany(
                "INSERT INTO host_snapshots(host_id, source, captured_at, data) VALUES(?, ?, ?, ?)",
                [
                    (host.id, host.source, captured_at, host.model_dump_json())
                    for host in result.hosts
                ],
            )

            topology_payload = result.topology.model_dump_json()
            payload_hash = hashlib.sha1(topology_payload.encode("utf-8")).hexdigest()
            last = conn.execute(
                "SELECT id, payload_hash FROM topology_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last is None or last["payload_hash"] != payload_hash:
                conn.execute(
                    "INSERT INTO topology_snapshots"
                    "(captured_at, source, node_count, link_count, payload_hash, data) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        captured_at,
                        result.topology.source,
                        len(result.topology.nodes),
                        len(result.topology.links),
                        payload_hash,
                        topology_payload,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE topology_snapshots SET captured_at=? WHERE id=?",
                    (captured_at, last["id"]),
                )

            # 健康问题：先全部置非活跃，再把本轮出现的置回活跃（保留历史时间线）。
            conn.execute("UPDATE health_issues SET active=0 WHERE active=1")
            conn.executemany(
                "INSERT INTO health_issues"
                "(issue_id, name, severity, source, first_seen, last_seen, active, data) "
                "VALUES(?, ?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(issue_id) DO UPDATE SET "
                "name=excluded.name, severity=excluded.severity, "
                "last_seen=excluded.last_seen, active=1, data=excluded.data",
                [
                    (
                        issue.issue_id,
                        issue.issue_name,
                        issue.severity,
                        issue.source,
                        issue.first_seen,
                        issue.last_seen,
                        issue.model_dump_json(),
                    )
                    for issue in result.issues
                ],
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('last_snapshot_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(captured_at),),
            )
            if result.hosts_observed_at is not None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('last_hosts_at', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(result.hosts_observed_at),),
                )

    def load_latest_devices(self) -> List[Dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT data FROM device_snapshots ORDER BY hostname"
            ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def load_latest_hosts(self) -> List[Dict[str, Any]]:
        """最近一轮主机快照（含 ping_status / last_seen，验证新鲜度用）。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT data, captured_at FROM host_snapshots ORDER BY host_id"
            ).fetchall()
        hosts = []
        for row in rows:
            item = json.loads(row["data"])
            item["snapshot_captured_at"] = row["captured_at"]
            hosts.append(item)
        return hosts

    # ------------------------------------------------------------------ #
    # 告警
    # ------------------------------------------------------------------ #
    # 告警表全部列（读写共用，避免字段漏写）
    _ALERT_COLUMNS = (
        "alert_type, severity, title, detail, dedup_key, device_id, link_id, "
        "issue_id, created_at, source, acknowledged, observed_at, source_ts, "
        "state, resolved_at, clear_reason, stale_source, deleted_at, status, "
        "silenced_at"
    )

    def save_alerts(self, alerts: Iterable[Alert]) -> int:
        """保存告警事件（兼容旧接口；新代码建议用 save_alert_events）。"""
        return self.save_alert_events(alerts)

    def save_alert_events(self, alerts: Iterable[Alert]) -> int:
        """批量写入告警事件（state 由 Alert.state 决定：active/cleared/info）。"""
        items = list(alerts)
        if not items:
            return 0
        with self._db() as conn:
            conn.executemany(
                "INSERT INTO alerts ({}) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)".format(
                    self._ALERT_COLUMNS
                ),
                [
                    (
                        alert.alert_type,
                        alert.severity,
                        alert.title,
                        alert.detail,
                        alert.dedup_key,
                        alert.device_id,
                        alert.link_id,
                        alert.issue_id,
                        alert.observed_at or alert.created_at,
                        alert.source,
                        1 if alert.acknowledged else 0,
                        alert.observed_at or alert.created_at,
                        alert.source_ts,
                        alert.state or "active",
                        alert.resolved_at,
                        alert.clear_reason,
                        1 if alert.stale_source else 0,
                        alert.deleted_at,
                        alert.status or "open",
                        alert.silenced_at,
                    )
                    for alert in items
                ],
            )
        return len(items)

    def load_active_alerts(self) -> List[Alert]:
        """加载当前仍在 active 的告警（检测引擎用于去重与恢复判定）。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE state='active' AND deleted_at IS NULL "
                "ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [Alert.model_validate(dict(row)) for row in rows]

    def load_stale_source_keys(self) -> Set[str]:
        """已按 stale_source 判定恢复、但 NC 记录可能仍在的 dedup_key 集合。

        用于抑制“NC issue 持续存在 → 每轮重新生成 issue 告警”的抖动；只有
        NC issue 消失、或目标 host/device 再次故障时才允许重新告警。
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT dedup_key FROM alerts "
                "WHERE state='cleared' AND stale_source=1 AND dedup_key IS NOT NULL "
                "AND deleted_at IS NULL"
            ).fetchall()
        return {row["dedup_key"] for row in rows}

    def active_alert(self, dedup_key: str) -> Optional[Dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE dedup_key=? AND state='active' "
                "AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
                (dedup_key,),
            ).fetchone()
        return _row_to_alert_dict(row) if row is not None else None

    def resolve_alert(
        self,
        dedup_key: str,
        observed_at: float,
        reason: str = "",
        stale_source: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """把某 dedup_key 的 active 告警置为 cleared（恢复），返回更新后的行。

        同一 dedup_key 同时最多一条 active；恢复只改状态，不新增“已清除”噪声行。
        """
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE dedup_key=? AND state='active' "
                "ORDER BY id DESC LIMIT 1",
                (dedup_key,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE alerts SET state='cleared', resolved_at=?, clear_reason=?, "
                "stale_source=? WHERE id=?",
                (
                    observed_at,
                    reason[:500],
                    1 if stale_source else 0,
                    row["id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM alerts WHERE id=?", (row["id"],)
            ).fetchone()
        return dict(updated) if updated is not None else None

    def get_alert(self, alert_id: int) -> Optional[Dict[str, Any]]:
        """按 id 读取单条告警（含已软删除行，调用方自行判断）。"""
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE id=?", (int(alert_id),)
            ).fetchone()
        return _row_to_alert_dict(row) if row is not None else None

    def soft_delete_alert(
        self, alert_id: int, deleted_at: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """软删除（deleted_at 置为当前时间），返回更新后的行；已删/不存在返回 None。"""
        stamp = time.time() if deleted_at is None else float(deleted_at)
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE id=? AND deleted_at IS NULL",
                (int(alert_id),),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE alerts SET deleted_at=? WHERE id=?", (stamp, int(alert_id))
            )
            updated = conn.execute(
                "SELECT * FROM alerts WHERE id=?", (int(alert_id),)
            ).fetchone()
        return _row_to_alert_dict(updated) if updated is not None else None

    def restore_alert(self, alert_id: int) -> Optional[Dict[str, Any]]:
        """撤销软删除（deleted_at 置空）；未被删除/不存在返回 None。"""
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE id=? AND deleted_at IS NOT NULL",
                (int(alert_id),),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE alerts SET deleted_at=NULL WHERE id=?", (int(alert_id),)
            )
            updated = conn.execute(
                "SELECT * FROM alerts WHERE id=?", (int(alert_id),)
            ).fetchone()
        return _row_to_alert_dict(updated) if updated is not None else None

    def silence_alert(
        self, alert_id: int, silenced_at: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """静默活跃告警（status=silenced，不删除、不改 state）；非 active 返回 None。"""
        stamp = time.time() if silenced_at is None else float(silenced_at)
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE id=? AND deleted_at IS NULL AND state='active'",
                (int(alert_id),),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE alerts SET status='silenced', silenced_at=? WHERE id=?",
                (stamp, int(alert_id)),
            )
            updated = conn.execute(
                "SELECT * FROM alerts WHERE id=?", (int(alert_id),)
            ).fetchone()
        return _row_to_alert_dict(updated) if updated is not None else None

    def clear_alerts(self, scope: str = "cleared", before: Optional[float] = None) -> int:
        """批量软删除（只允许 cleared/info，active 永不删除）。

        scope:
        - ``cleared``      : 仅 state='cleared'（前端“清空已恢复”）
        - ``all_cleared``  : state IN ('cleared','info')（连同 info 一次性事件一起清理）
        ``before``（可选）: 只删除 observed_at < before 的记录。
        """
        states = ["cleared"] if scope == "cleared" else ["cleared", "info"]
        placeholders = ", ".join("?" for _ in states)
        sql = (
            "UPDATE alerts SET deleted_at=? WHERE state IN ({}) "
            "AND deleted_at IS NULL AND state != 'active'"
        ).format(placeholders)
        params: List[Any] = [time.time()] + states
        if before is not None:
            sql += " AND COALESCE(observed_at, created_at) < ?"
            params.append(float(before))
        with self._db() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount or 0

    def ack_alerts(self, alert_ids: List[int]) -> int:
        """整组确认：把给定告警 id 的 acknowledged 置 1（不改变 active/state）。"""
        ids = [int(item) for item in alert_ids if item is not None]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._db() as conn:
            cursor = conn.execute(
                "UPDATE alerts SET acknowledged=1 WHERE id IN ({}) "
                "AND deleted_at IS NULL".format(placeholders),
                ids,
            )
            return cursor.rowcount or 0

    # ------------------------------------------------------------------ #
    # 操作审计（W9）
    # ------------------------------------------------------------------ #
    def add_audit(
        self,
        operator: str,
        action: str,
        target: str = "",
        detail: Any = "",
        result: str = "ok",
    ) -> None:
        """记录一条写操作审计（operator/action/target/detail/result/时间）。"""
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        with self._db() as conn:
            conn.execute(
                "INSERT INTO audit_log(created_at, operator, action, target, detail, result) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (time.time(), str(operator), str(action), str(target), detail, str(result)),
            )

    def load_audit(
        self,
        limit: int = 100,
        action: Optional[str] = None,
        operator: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """审计列表（时间倒序，可按 action/operator 过滤）。"""
        sql = (
            "SELECT id, created_at, operator, action, target, detail, result "
            "FROM audit_log WHERE 1=1"
        )
        params: List[Any] = []
        if action:
            sql += " AND action=?"
            params.append(action)
        if operator:
            sql += " AND operator=?"
            params.append(operator)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # 场景配置存储（W9）
    # ------------------------------------------------------------------ #
    def load_scene_document(self) -> Optional[Dict[str, Any]]:
        """读取 scene_config 单行 JSON 文档；未 seed 返回 None。"""
        with self._db() as conn:
            row = conn.execute(
                "SELECT version, data, updated_at FROM scene_config WHERE id=1"
            ).fetchone()
        if row is None:
            return None
        try:
            document = json.loads(row["data"])
        except (TypeError, ValueError):
            return None
        document["version"] = int(row["version"])
        document["updated_at"] = row["updated_at"]
        return document

    def save_scene_document(self, document: Dict[str, Any]) -> None:
        """写入 scene_config（单行 upsert），version 由调用方维护。"""
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True)
        with self._db() as conn:
            conn.execute(
                "INSERT INTO scene_config(id, version, data, updated_at) VALUES(1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version, "
                "data=excluded.data, updated_at=excluded.updated_at",
                (
                    int(document.get("version", 1)),
                    payload,
                    float(document.get("updated_at", time.time())),
                ),
            )

    def scene_version(self) -> int:
        with self._db() as conn:
            row = conn.execute("SELECT version FROM scene_config WHERE id=1").fetchone()
        return int(row["version"]) if row is not None else 0

    def load_alerts(
        self,
        limit: int = 100,
        severity: Optional[str] = None,
        state: Optional[str] = None,
        include_deleted: bool = False,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询告警历史（时间倒序）。

        - ``state``：``active`` / ``cleared`` / ``info`` / ``all``（None 等价 all）；
        - 默认过滤已软删除记录（``deleted_at IS NULL``），``include_deleted=True``
          时包含（内部/审计用）；
        - ``status``：``open`` / ``silenced``（W9 静默）。
        """
        sql = "SELECT * FROM alerts WHERE 1=1"
        params: List[Any] = []
        if severity:
            sql += " AND severity=?"
            params.append(severity)
        if state and state != "all":
            sql += " AND state=?"
            params.append(state)
        if status and status != "all":
            sql += " AND status=?"
            params.append(status)
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        sql += " ORDER BY observed_at DESC, created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_alert_dict(row) for row in rows]

    def alert_stats(self, include_deleted: bool = False) -> Dict[str, int]:
        """按状态统计告警数量（/api/health、/api/alerts 摘要用；默认不含软删除）。"""
        counts = {"active": 0, "cleared": 0, "info": 0}
        sql = "SELECT state, COUNT(*) AS cnt FROM alerts"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        sql += " GROUP BY state"
        with self._db() as conn:
            rows = conn.execute(sql).fetchall()
        total = 0
        for row in rows:
            counts[row["state"]] = row["cnt"]
            total += row["cnt"]
        counts["total"] = total
        return counts

    def prune_alerts(self, retention_days: float) -> int:
        """清理超过 TTL 的历史告警（默认 7 天）。

        只删除已恢复/信息类告警；``state='active'`` 代表仍在进行的故障，不因时间
        长而删除（避免长故障被“清理”掉）。
        """
        cutoff = time.time() - max(float(retention_days), 0.0) * 86400.0
        with self._db() as conn:
            cursor = conn.execute(
                "DELETE FROM alerts WHERE state IN ('cleared','info') AND created_at < ?",
                (cutoff,),
            )
            return cursor.rowcount or 0

    # ------------------------------------------------------------------ #
    # 健康问题
    # ------------------------------------------------------------------ #
    def load_issues(
        self, active_only: bool = True, limit: int = 200
    ) -> List[Dict[str, Any]]:
        sql = "SELECT issue_id, name, severity, source, first_seen, last_seen, active, data FROM health_issues"
        params: List[Any] = []
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        issues: List[Dict[str, Any]] = []
        for row in rows:
            item = json.loads(row["data"])
            item["active"] = bool(row["active"])
            issues.append(item)
        return issues

    # ------------------------------------------------------------------ #
    # 指标点
    # ------------------------------------------------------------------ #
    def save_metrics(self, points: Iterable[MetricPoint]) -> int:
        items = list(points)
        if not items:
            return 0
        with self._db() as conn:
            conn.executemany(
                "INSERT INTO metric_points"
                "(device_id, metric, value, unit, interface, ts, source, tags, "
                " speed_bps, clamped) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        point.device_id,
                        point.metric,
                        point.value,
                        point.unit,
                        point.interface,
                        point.ts,
                        point.source,
                        json.dumps(point.tags, ensure_ascii=False, sort_keys=True),
                        point.speed_bps,
                        1 if point.clamped else 0,
                    )
                    for point in items
                ],
            )
        return len(items)

    def load_metrics(
        self,
        device_id: str,
        limit: int = 240,
        metric: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按时间升序返回最近 limit 个指标点（便于前端直接画折线）。"""
        sql = (
            "SELECT metric, value, unit, interface, ts, source, tags, "
            "speed_bps, clamped FROM metric_points WHERE device_id=?"
        )
        params: List[Any] = [device_id]
        if metric:
            sql += " AND metric=?"
            params.append(metric)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        points = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = json.loads(item["tags"]) if item["tags"] else {}
            except (TypeError, ValueError):
                item["tags"] = {}
            item["clamped"] = bool(item.get("clamped"))
            item["speed_bps"] = (
                float(item["speed_bps"]) if item.get("speed_bps") is not None else None
            )
            points.append(item)
        points.reverse()
        return points

    def prune_metrics(self, retention_seconds: int) -> int:
        """删除超过保留时长的指标点，返回删除行数。"""
        cutoff = time.time() - max(int(retention_seconds), 60)
        with self._db() as conn:
            cursor = conn.execute("DELETE FROM metric_points WHERE ts < ?", (cutoff,))
            return cursor.rowcount or 0

    # ------------------------------------------------------------------ #
    # 统计（/api/health 诊断用）
    # ------------------------------------------------------------------ #
    def stats(self) -> Dict[str, Any]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM device_snapshots) AS devices, "
                "(SELECT COUNT(*) FROM metric_points) AS metrics, "
                "(SELECT COUNT(*) FROM alerts) AS alerts, "
                "(SELECT COUNT(*) FROM alerts WHERE state='active') AS active_alerts, "
                "(SELECT COUNT(*) FROM health_issues WHERE active=1) AS issues, "
                "(SELECT COUNT(*) FROM audit_log) AS audits, "
                "(SELECT version FROM scene_config WHERE id=1) AS scene_version"
            ).fetchone()
            meta = {
                item["key"]: item["value"]
                for item in conn.execute("SELECT key, value FROM meta").fetchall()
            }
        return {
            "db_path": self.db_path,
            "journal_mode": self.journal_mode(),
            "device_rows": row["devices"] if row else 0,
            "metric_rows": row["metrics"] if row else 0,
            "alert_rows": row["alerts"] if row else 0,
            "active_alert_rows": row["active_alerts"] if row else 0,
            "alert_states": (self.alert_stats() if row else {}),
            "active_issue_rows": row["issues"] if row else 0,
            "audit_rows": row["audits"] if row else 0,
            "scene_version": (row["scene_version"] if row and row["scene_version"] else 0),
            "meta": meta,
        }


def _row_to_alert_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """sqlite3.Row -> 告警 dict（布尔字段规范化，供 API 直接返回）。"""
    item = dict(row)
    item["acknowledged"] = bool(item.get("acknowledged"))
    item["stale_source"] = bool(item.get("stale_source"))
    return item
