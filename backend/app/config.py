# -*- coding: utf-8 -*-
"""运行配置。

所有配置都可以通过环境变量覆盖，默认值面向本机开发：

- Packet Tracer 9.x Network Controller 只监听 localhost:58000（Real World Access）。
- SQLite 默认落在 backend/data/campus_twin.db，随仓库外置数据，不进版本库。

设计意图：
    配置只有一个入口（``Settings.from_env()``），方便测试时构造独立实例
    （Temp DB / 模拟驱动 / 超短超时），生产运行时则完全由环境变量控制，
    不需要改代码。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_json_list(name: str, default: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """读取 JSON 数组型环境变量（如 SNMP_DEVICES）；解析失败回退默认值。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default)
    try:
        value = json.loads(raw)
    except ValueError:
        return list(default)
    if not isinstance(value, list):
        return list(default)
    return [item for item in value if isinstance(item, dict)]


def _validated_choice(name: str, default: str, choices: Tuple[str, ...]) -> str:
    value = os.environ.get(name, "").strip().lower()
    return value if value in choices else default


@dataclass
class Settings:
    """服务运行配置（默认值即本机开发配置）。"""

    # ---- Packet Tracer Network Controller（真数据源）----
    nc_base_url: str = "http://127.0.0.1:58000/api/v1"
    nc_username: str = "admin"
    nc_password: str = "cisco"
    # 单次 HTTP 请求超时（秒）。本地环回 + 小 JSON，5 秒足够；超时即视为不可用。
    nc_timeout: float = 5.0
    # 连接失败后的重试间隔（秒）：失败指数退避，上限 120 秒，避免 NC 离线时
    # 5 秒一次的空转重试把日志刷爆，也保证 NC 恢复后能在 2 分钟内重新接上。
    nc_retry_interval: float = 15.0
    nc_max_retry_interval: float = 120.0

    # ---- 采集调度 ----
    poll_interval: float = 5.0  # 轮询周期（秒）
    collect_timeout: float = 30.0  # 单轮采集硬超时，兜底防止驱动卡死

    # ---- 驱动选择 ----
    # netcon      : 只用真 REST 数据；NC 不可用时降级为空数据集继续运行
    # simulator   : 纯仿真（用于无 PT 环境开发/演示，所有数据 source=simulator）
    # auto        : 启动时探测 NC，可用则 netcon，否则回退 simulator
    driver: str = "netcon"
    # PT 不暴露接口计数器，指标由行为仿真引擎生成，统一标注 source=simulator。
    simulate_metrics: bool = True

    # ---- 存储 ----
    db_path: str = str(BACKEND_DIR / "data" / "campus_twin.db")
    # 指标点保留时长（秒）：默认 1 小时。5 秒一个点、每轮约数十个点，
    # 定期清理可避免 SQLite 无限膨胀。
    metrics_retention_seconds: int = 3600
    # 历史告警 TTL（天）：默认保留 7 天；只清理已恢复/信息类告警，
    # 仍处于 active 的告警不因时间长被删除（它代表持续中的故障）。
    alert_retention_days: float = 7.0

    # ---- W10 路径追踪 ----
    path_trace_cache_seconds: float = 120.0  # 同 source/dest 缓存 2 分钟
    path_trace_timeout: float = 30.0  # NC flow-analysis 轮询总超时
    path_trace_poll_interval: float = 1.0

    # ---- W10 Webhook 告警通知 ----
    notify_enabled: bool = False
    notify_webhook_url: str = ""
    notify_webhook_type: str = "generic"  # generic | dingtalk | feishu | wecom
    notify_min_severity: str = "critical"  # critical | warning（info 永不通知）
    notify_dedup_seconds: float = 300.0  # 同 dedup_key 5 分钟内只发一次
    notify_timeout: float = 5.0
    # 失败重试 2 次，指数退避 1s / 3s（单测可覆盖成 0 加速）
    notify_retry_delays: Tuple[float, ...] = (1.0, 3.0)

    # ---- W10 SNMP 真实设备驱动 ----
    # SNMP_DEVICES='[{"name":"SW-A","ip":"127.0.0.1","community":"public","version":"2c","building":"core"}]'
    snmp_devices: List[Dict[str, Any]] = field(default_factory=list)
    snmp_timeout: float = 2.0
    snmp_retries: int = 1

    # ---- API / 日志 ----
    log_level: str = "INFO"
    cors_origins: Tuple[str, ...] = field(default_factory=lambda: ("*",))
    # 单次 REST 查询返回的告警/指标条数上限
    max_alerts: int = 200

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构造配置；未设置的项使用本机开发默认值。"""
        return cls(
            nc_base_url=_env_str("NC_BASE_URL", cls.nc_base_url),
            nc_username=_env_str("NC_USERNAME", cls.nc_username),
            nc_password=_env_str("NC_PASSWORD", cls.nc_password),
            nc_timeout=_env_float("NC_TIMEOUT", cls.nc_timeout),
            nc_retry_interval=_env_float("NC_RETRY_INTERVAL", cls.nc_retry_interval),
            nc_max_retry_interval=_env_float(
                "NC_MAX_RETRY_INTERVAL", cls.nc_max_retry_interval
            ),
            poll_interval=_env_float("POLL_INTERVAL", cls.poll_interval),
            collect_timeout=_env_float("COLLECT_TIMEOUT", cls.collect_timeout),
            driver=_env_str("DRIVER", cls.driver).lower(),
            simulate_metrics=_env_bool("SIMULATE_METRICS", cls.simulate_metrics),
            db_path=_env_str("DB_PATH", cls.db_path),
            metrics_retention_seconds=int(
                _env_float(
                    "METRICS_RETENTION_SECONDS", float(cls.metrics_retention_seconds)
                )
            ),
            alert_retention_days=_env_float(
                "ALERT_RETENTION_DAYS", cls.alert_retention_days
            ),
            path_trace_cache_seconds=_env_float(
                "PATH_TRACE_CACHE_SECONDS", cls.path_trace_cache_seconds
            ),
            path_trace_timeout=_env_float(
                "PATH_TRACE_TIMEOUT", cls.path_trace_timeout
            ),
            path_trace_poll_interval=_env_float(
                "PATH_TRACE_POLL_INTERVAL", cls.path_trace_poll_interval
            ),
            notify_enabled=_env_bool("NOTIFY_ENABLED", cls.notify_enabled),
            notify_webhook_url=_env_str("NOTIFY_WEBHOOK_URL", cls.notify_webhook_url),
            notify_webhook_type=_validated_choice(
                "NOTIFY_WEBHOOK_TYPE",
                cls.notify_webhook_type,
                ("generic", "dingtalk", "feishu", "wecom"),
            ),
            notify_min_severity=_validated_choice(
                "NOTIFY_MIN_SEVERITY",
                cls.notify_min_severity,
                ("critical", "warning"),
            ),
            notify_dedup_seconds=_env_float(
                "NOTIFY_DEDUP_SECONDS", cls.notify_dedup_seconds
            ),
            notify_timeout=_env_float("NOTIFY_TIMEOUT", cls.notify_timeout),
            notify_retry_delays=(
                _env_float("NOTIFY_RETRY_DELAY_1", cls.notify_retry_delays[0]),
                _env_float("NOTIFY_RETRY_DELAY_2", cls.notify_retry_delays[1]),
            ),
            snmp_devices=_env_json_list("SNMP_DEVICES", []),
            snmp_timeout=_env_float("SNMP_TIMEOUT", cls.snmp_timeout),
            snmp_retries=int(_env_float("SNMP_RETRIES", float(cls.snmp_retries))),
            log_level=_env_str("LOG_LEVEL", cls.log_level).upper(),
            max_alerts=int(_env_float("MAX_ALERTS", float(cls.max_alerts))),
        )

    def ensure_dirs(self) -> None:
        """确保数据目录存在（SQLite 首次创建库文件需要）。"""
        Path(self.db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
