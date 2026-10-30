# -*- coding: utf-8 -*-
"""异步采集流水线：调度 → 归一化快照 → W3 检测引擎 → 告警持久化/去重 → WebSocket 广播。

设计要点
========
1. **单轮采集有硬超时**：``asyncio.wait_for(driver.collect(), collect_timeout)``，
   驱动即使内部卡住也不会拖死整个服务；失败即降级并继续下一轮。
2. **检测引擎独立成模块**：真正的规则在 ``app/detection/rules.py``（host ping /
   设备可达性 / 链路状态 / NC issue + 数据源交叉校验）；pipeline 只负责执行、
   持久化与广播，便于单测与替换。
3. **告警生命周期去重（W3-C）**：同一 ``dedup_key`` 同时最多一条 active 告警；
   故障持续期间不重复入库/推送；恢复时更新为 cleared（不新增 info 噪声行），
   同一轮多条恢复聚合成一条 ``alerts_cleared`` 摘要推送。
4. **首轮基线不入广播**：服务启动后的第一轮成功采集会静默记录现存 active 告警
   （便于重启后恢复判定/API 查询），但不通过 WebSocket 广播，避免启动瞬间刷屏。
5. **历史告警 TTL**：默认保留 7 天（``ALERT_RETENTION_DAYS``），定期清理已恢复
   与 info 类告警；active 告警不因时间长被删除。
6. **部分端点失败防误报**：若本轮设备/主机/拓扑端点采集失败（而不是实体真的消失），
   检测引擎跳过对应规则，避免网络抖动导致“全部离线/全部恢复”的误报。
7. **指标总是标注来源**：netcon 模式下的指标由 simulator 行为模型生成并带
   ``source="simulator"``；PT 真数据永远标 ``source="netcon"``。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.collectors.base import CollectResult, Driver, MetricGenerator
from app.collectors.netcon import NetconDriver
from app.collectors.simulator import SimulatorDriver
from app.config import Settings
from app.detection.rules import DetectionEngine, DetectionOutcome
from app.models import (
    Device,
    HealthIssue,
    Host,
    Link,
    MetricPoint,
    NetworkHealth,
    SourceType,
    Topology,
)
from app.notify import NotifyService
from app.scene import SceneService
from app.store import Store

logger = logging.getLogger("campus_twin.pipeline")

BroadcastFn = Callable[[Dict[str, Any]], Awaitable[None]]

# 每 N 轮做一次指标/告警 TTL 清理
PRUNE_EVERY_N_POLLS = 60


def iso_time(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class PipelineState:
    """某次成功采集的基线（用于下一轮变更比较）。"""

    devices: Dict[str, Device] = field(default_factory=dict)
    links: Dict[str, Link] = field(default_factory=dict)
    issues: Dict[str, HealthIssue] = field(default_factory=dict)
    hosts: Dict[str, Host] = field(default_factory=dict)
    topology: Topology = field(default_factory=Topology)
    network_health: Optional[NetworkHealth] = None
    captured_at: float = 0.0
    # /host 本轮的采集时间（宿主机 epoch），用于 /api/hosts 新鲜度证明
    hosts_observed_at: Optional[float] = None
    source: SourceType = "netcon"

    @classmethod
    def from_result(cls, result: CollectResult) -> "PipelineState":
        return cls(
            devices={device.id: device for device in result.devices},
            links={link.id: link for link in result.topology.links},
            issues={issue.issue_id: issue for issue in result.issues},
            hosts={host.id: host for host in result.hosts},
            topology=result.topology,
            network_health=result.network_health,
            captured_at=result.collected_at,
            hosts_observed_at=result.hosts_observed_at,
            source=result.source,
        )


class CollectorPipeline:
    """采集调度器（FastAPI 启动时 start，关闭时 stop）。"""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        driver: Driver,
        metrics_driver: Optional[MetricGenerator] = None,
        broadcast: Optional[BroadcastFn] = None,
        scene: Optional[SceneService] = None,
        notify: Optional[NotifyService] = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.driver = driver
        # W9：场景配置服务（与 app lifespan 共享同一实例；单测可独立构造）
        self.scene = scene if scene is not None else SceneService(store)
        # W10：Webhook 通知服务（非阻塞队列；由 lifespan 启动/停止）
        self.notify = notify
        self.metrics_driver = metrics_driver
        self._broadcast_fn = broadcast
        # W3 检测引擎（规则实现见 app/detection/rules.py；可替换为其它引擎）
        self.detector = DetectionEngine()
        self.detection_engine_name = getattr(self.detector, "name", "unknown")

        self._state: Optional[PipelineState] = None
        self._task: Optional[asyncio.Task] = None
        self._polls: int = 0
        self._collect_errors: int = 0
        self._last_poll_at: Optional[float] = None
        self._last_success_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._stale: bool = False
        self._prune_counter: int = 0
        self._last_metrics: List[MetricPoint] = []
        self._last_cleared_summary: Optional[Dict[str, Any]] = None
        self._baseline_logged: bool = False
        # 连续失败轮数 / 失败日志节流（避免 NC 长时间离线时每 5 秒刷一条 warning）
        self._consecutive_failures: int = 0
        self._last_failure_log_at: float = 0.0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """建库、探测数据源、先做一轮采集（应用就绪即有数据），再启动周期任务。"""
        self.store.init_db()
        logger.info(
            "采集流水线启动：driver=%s  metrics=%s  poll_interval=%.1fs  db=%s",
            self.driver.name,
            self.metrics_driver.name if self.metrics_driver else "disabled",
            self.settings.poll_interval,
            self.settings.db_path,
        )
        await self._warmup_probe()
        try:
            await self.poll_once()
        except Exception:  # noqa: BLE001 - 首轮异常也不能阻止服务启动
            logger.exception("首轮采集异常，服务继续启动并进入周期采集")
        self._task = asyncio.create_task(self._run_loop(), name="campus-twin-collector")
        logger.info("采集循环已启动（每 %.1fs 一轮）", self.settings.poll_interval)

    async def _warmup_probe(self) -> None:
        """启动探测：只用于日志/自动选驱动，失败不影响启动。"""
        try:
            ok = await asyncio.wait_for(
                self.driver.probe(), timeout=max(self.settings.nc_timeout * 2, 5.0)
            )
        except asyncio.TimeoutError:
            ok = False
            logger.warning("数据源启动探测超时：%s", self.driver.name)
        except Exception:  # noqa: BLE001 - 探测失败绝不能阻止服务启动
            ok = False
            logger.exception("数据源启动探测异常：%s", self.driver.name)
        if ok:
            logger.info("数据源探测成功：%s", self.driver.name)
        else:
            logger.warning(
                "数据源探测失败：%s。服务将以空数据集启动并持续按退避策略重试（不阻塞启动）。",
                self.driver.name,
            )

    async def _run_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.poll_interval)
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 单轮异常不能让循环退出
                    logger.exception("采集轮次异常，继续下一轮")
        except asyncio.CancelledError:
            logger.info("采集循环收到取消信号，准备退出")
            raise

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.driver.close()
        if self.metrics_driver is not None and self.metrics_driver is not self.driver:
            close = getattr(self.metrics_driver, "close", None)
            if close is not None:
                await close()
        logger.info("采集流水线已停止（累计采集 %d 轮）", self._polls)

    # ------------------------------------------------------------------ #
    # 单轮采集
    # ------------------------------------------------------------------ #
    async def poll_once(self) -> None:
        """执行一轮采集 + 变更检测 + 持久化 + 广播。任何异常都在内部消化。"""
        self._polls += 1
        self._last_poll_at = time.time()
        try:
            result = await asyncio.wait_for(
                self.driver.collect(), timeout=self.settings.collect_timeout
            )
        except asyncio.TimeoutError:
            result = CollectResult.unavailable(
                self.driver.name, "采集超时（>%.0fs）" % self.settings.collect_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单轮失败不允许影响服务
            logger.exception("驱动采集异常：%s", exc)
            result = CollectResult.unavailable(self.driver.name, "驱动采集异常：{}".format(exc))

        if not result.ok:
            self._collect_errors += 1
            self._consecutive_failures += 1
            self._last_error = result.error or "采集失败"
            became_stale = not self._stale
            self._stale = True
            now = time.time()
            # 首次失败立即 warning，之后每 60s 提醒一次（其余降为 debug）：既不丢
            # 可观测性，也避免 NC 长时间离线时日志被 5 秒一轮的 warning 刷爆。
            if became_stale or now - self._last_failure_log_at >= 60.0:
                self._last_failure_log_at = now
                logger.warning(
                    "本轮采集失败（连续 %d 轮）：%s；%s继续服务",
                    self._consecutive_failures,
                    self._last_error,
                    "保留最近一次成功快照并标记 stale " if self._state else "以空数据集",
                )
            else:
                logger.debug(
                    "本轮采集失败（连续 %d 轮）：%s", self._consecutive_failures, self._last_error
                )
            if became_stale and self._broadcast_fn is not None:
                await self._safe_broadcast({"type": "status", "data": self.status()})
            return

        if self._consecutive_failures:
            logger.info("采集已恢复（此前连续失败 %d 轮）", self._consecutive_failures)
        self._consecutive_failures = 0
        self._last_failure_log_at = 0.0

        now = result.collected_at or time.time()
        baseline = self._state is None
        outcome = self._detect(result, baseline=baseline)

        # W3 时间基准：把“NC issue 仍在但数据源已恢复”的 issue 标记出来，
        # snapshot/API 可见 stale_source=true（前端可置灰并提示 NC 记录滞后）。
        for issue in result.issues:
            if issue.issue_id in outcome.stale_issue_ids:
                issue.stale_source = True

        self._state = PipelineState.from_result(result)
        self._last_success_at = now
        self._last_error = None
        self._stale = False

        # W9：每轮采集后比对 inventory 与 scene_config，刷新 pending 新设备（含归属推断）
        try:
            self.scene.refresh_pending(result.devices, result.topology)
        except Exception:  # noqa: BLE001 - 场景检测失败不影响采集主链路
            logger.exception("新设备检测失败（scene_config 比对）")

        metrics = self._resolve_metrics(result)
        self._last_metrics = metrics

        try:
            self.store.save_snapshot(result)
            self.store.save_metrics(metrics)
            self._persist_detection(outcome)
            self._prune_counter += 1
            if self._prune_counter >= PRUNE_EVERY_N_POLLS:
                self._prune_counter = 0
                removed = self.store.prune_metrics(self.settings.metrics_retention_seconds)
                if removed:
                    logger.info("清理过期指标点 %d 条", removed)
                removed_alerts = self.store.prune_alerts(self.settings.alert_retention_days)
                if removed_alerts:
                    logger.info(
                        "清理过期历史告警 %d 条（TTL %.1f 天）",
                        removed_alerts,
                        self.settings.alert_retention_days,
                    )
        except Exception:  # noqa: BLE001 - 持久化失败不应打断实时链路
            logger.exception("持久化失败（实时数据仍可用）")

        if baseline and not self._baseline_logged:
            self._baseline_logged = True
            logger.info(
                "首轮基线：静默记录 %d 条现存 active 告警（不广播，避免启动刷屏）",
                len(outcome.active_alerts),
            )

        if self._broadcast_fn is not None:
            await self._safe_broadcast(
                {"type": "snapshot", "data": self.snapshot_data()}
            )
            if not baseline:
                for alert in outcome.alerts:
                    await self._safe_broadcast(
                        {"type": "alert", "data": alert.model_dump(mode="json")}
                    )
            # 恢复事件统一聚合成一条摘要（W3-C：11 条集中清除不再刷屏）
            cleared_summary = outcome.cleared_summary()
            self._last_cleared_summary = cleared_summary
            if cleared_summary:
                await self._safe_broadcast(
                    {"type": "alerts_cleared", "data": cleared_summary}
                )
        # W10：新产生的 active 告警投递到 Webhook 后台队列（不阻塞采集；基线轮不发）
        # 注意：入库后才能拿到 DB id，这里按 dedup_key 回填，保证 Webhook 消息带 alert_id。
        if not baseline and self.notify is not None:
            for alert in outcome.active_alerts:
                try:
                    if getattr(alert, "id", None) is None and alert.dedup_key:
                        try:
                            row = self.store.active_alert(alert.dedup_key)
                            if row is not None:
                                alert.id = row.get("id")
                        except Exception:  # noqa: BLE001 - id 回填失败不影响通知
                            logger.debug("回填告警 id 失败", exc_info=True)
                    self.notify.enqueue(alert)
                except Exception:  # noqa: BLE001 - 通知失败不能影响采集
                    logger.exception("Webhook 通知入队失败")
        if outcome.alerts:
            logger.info("本轮产生 %d 条新告警", len(outcome.alerts))

    def _detect(self, result: CollectResult, baseline: bool) -> DetectionOutcome:
        """执行 W3 检测引擎；异常时降级为空结果（快照照常更新）。"""
        try:
            active_alerts = self.store.load_active_alerts()
            suppressed_keys = self.store.load_stale_source_keys()
            return self.detector.evaluate(
                self._state,
                result,
                active_alerts=active_alerts,
                baseline=baseline,
                suppressed_keys=suppressed_keys,
            )
        except Exception:  # noqa: BLE001 - 检测失败不能影响采集主链路
            logger.exception("检测引擎异常，本轮跳过告警（采集快照仍更新）")
            return DetectionOutcome(baseline=baseline)

    def _persist_detection(self, outcome: DetectionOutcome) -> None:
        """持久化新告警与恢复事件（active 去重由引擎+store 保证）。"""
        if outcome.alerts:
            self.store.save_alert_events(outcome.alerts)
        for event in outcome.cleared:
            updated = self.store.resolve_alert(
                dedup_key=event.dedup_key,
                observed_at=event.observed_at,
                reason=event.detail,
                stale_source=event.stale_source,
            )
            if updated is None:
                # 例如历史库里没有这条 active（服务重启前已清理），只记录不报错
                logger.debug("恢复事件找不到 active 告警：%s", event.dedup_key)

    def _resolve_metrics(self, result: CollectResult) -> List[MetricPoint]:
        """确定本轮指标：真实拓扑 + 仿真指标（始终标注 source=simulator）。"""
        if self.metrics_driver is None:
            return []
        if result.metrics:
            return result.metrics
        if not result.devices:
            return []
        try:
            # 拓扑可能暂时没有链路（PT 刚加载网络时 links 为空），此时行为模型
            # 仍会为每台设备生成 CPU/内存/设备负载指标；端口指标等链路出现后自动补上。
            return self.metrics_driver.generate_metrics(
                result.devices, result.topology, at=result.collected_at
            )
        except Exception:  # noqa: BLE001 - 指标生成失败不影响采集主链路
            logger.exception("指标生成失败，本轮无指标")
            return []

    async def _safe_broadcast(self, message: Dict[str, Any]) -> None:
        assert self._broadcast_fn is not None
        try:
            await self._broadcast_fn(message)
        except Exception:  # noqa: BLE001 - 广播失败不影响采集
            logger.exception("WebSocket 广播失败")

    # ------------------------------------------------------------------ #
    # 数据视图（REST / WebSocket 共用）
    # ------------------------------------------------------------------ #
    @property
    def has_data(self) -> bool:
        return self._state is not None

    @property
    def state(self) -> Optional[PipelineState]:
        return self._state

    @staticmethod
    def _snapshot_network_health(state: PipelineState) -> Optional[Dict[str, Any]]:
        """W3.1：对外网络健康口径始终由当前设备列表自算（与 /api/devices 自洽）。

        即使驱动透传的 NC 汇总字段缺失/矛盾，API 也保证
        ``device_healthy + device_unhealthy == device_total`` 且
        ``health_percentage`` 为数值；NC 原始值保留在 ``nc_*`` 字段中。
        """
        if state.network_health is None and not state.devices:
            return None
        health = NetworkHealth.compute_from_devices(
            list(state.devices.values()),
            source=state.source,
            nc_health=state.network_health,
        )
        return health.model_dump(mode="json")

    def snapshot_data(self) -> Dict[str, Any]:
        """当前快照的 JSON 友好结构（WebSocket 推送与 REST 共用，避免两套字段）。"""
        state = self._state
        if state is None:
            return {
                "captured_at": None,
                "captured_at_iso": None,
                "driver": getattr(self.driver, "name", "unknown"),
                "source": None,
                "stale": self._stale,
                "counts": {
                    "devices": 0,
                    "links": 0,
                    "hosts": 0,
                    "issues": 0,
                    "metrics": 0,
                },
                "observed_at": None,
                "observed_at_iso": None,
                "hosts_observed_at": None,
                "hosts_observed_at_iso": None,
                "devices": [],
                "topology": {"nodes": [], "links": [], "source": None, "captured_at": None},
                "hosts": [],
                "issues": [],
                "network_health": None,
            }
        return {
            "captured_at": state.captured_at,
            "captured_at_iso": iso_time(state.captured_at),
            # W3 时间基准：observed_at 即本轮宿主机采集时间（与 captured_at 一致，显式命名）
            "observed_at": state.captured_at,
            "observed_at_iso": iso_time(state.captured_at),
            "hosts_observed_at": state.hosts_observed_at,
            "hosts_observed_at_iso": iso_time(state.hosts_observed_at),
            "driver": getattr(self.driver, "name", "unknown"),
            "source": state.source,
            "stale": self._stale,
            "counts": {
                "devices": len(state.devices),
                "links": len(state.links),
                "hosts": len(state.hosts),
                "issues": len(state.issues),
                "metrics": len(self._last_metrics),
            },
            "devices": [device.model_dump(mode="json") for device in state.devices.values()],
            "topology": {
                "source": state.topology.source,
                "captured_at": state.topology.captured_at,
                "nodes": [
                    node.model_dump(mode="json") for node in state.topology.nodes
                ],
                "links": [
                    link.model_dump(mode="json") for link in state.topology.links
                ],
            },
            "hosts": [host.model_dump(mode="json") for host in state.hosts.values()],
            "issues": [issue.model_dump(mode="json") for issue in state.issues.values()],
            "network_health": self._snapshot_network_health(state),
        }

    def status(self) -> Dict[str, Any]:
        """服务/采集状态（GET /api/health 用）。"""
        state = self._state
        status: Dict[str, Any] = {
            "status": "ok",  # 本服务自身健康状态
            "driver": getattr(self.driver, "name", "unknown"),
            "metrics_source": self.metrics_driver.name if self.metrics_driver else None,
            "dataset_source": state.source if state else None,
            "stale": self._stale,
            "polls": self._polls,
            "collect_errors": self._collect_errors,
            "consecutive_failures": self._consecutive_failures,
            "poll_interval": self.settings.poll_interval,
            "last_poll_at": iso_time(self._last_poll_at),
            "last_success_at": iso_time(self._last_success_at),
            "last_error": self._last_error,
            "hosts_observed_at": iso_time(
                state.hosts_observed_at if state else None
            ),
            "detection": self._detection_status(),
            "counts": {
                "devices": len(state.devices) if state else 0,
                "links": len(state.links) if state else 0,
                "hosts": len(state.hosts) if state else 0,
                "issues": len(state.issues) if state else 0,
                "metrics_last_poll": len(self._last_metrics),
            },
            "captured_at": iso_time(state.captured_at) if state else None,
            "last_cleared_summary": (
                self._last_cleared_summary["summary"]
                if self._last_cleared_summary
                else None
            ),
            "scene_version": (
                self.scene.current_version() if getattr(self, "scene", None) else None
            ),
        }
        driver_status = getattr(self.driver, "status", None)
        if callable(driver_status):
            try:
                status["driver_status"] = driver_status()
            except Exception:  # noqa: BLE001
                logger.debug("driver.status() 失败", exc_info=True)
        return status

    def _detection_status(self) -> Dict[str, Any]:
        """检测引擎与告警生命周期统计（/api/health 观测用）。"""
        catalog: List[Dict[str, Any]] = []
        try:
            catalog = self.detector.rule_catalog()
        except Exception:  # noqa: BLE001
            logger.debug("rule_catalog() 失败", exc_info=True)
        alert_states: Dict[str, int] = {}
        try:
            alert_states = self.store.alert_stats()
        except Exception:  # noqa: BLE001
            logger.debug("alert_stats() 失败", exc_info=True)
        return {
            "engine": self.detection_engine_name,
            "rules": [item.get("rule_id") for item in catalog],
            "alert_states": alert_states,
            "last_cleared_summary": (
                self._last_cleared_summary["summary"]
                if self._last_cleared_summary
                else None
            ),
        }


# --------------------------------------------------------------------------- #
# 驱动工厂
# --------------------------------------------------------------------------- #
async def build_drivers(settings: Settings) -> Tuple[Driver, Optional[MetricGenerator]]:
    """按配置构造 (数据源驱动, 指标生成器)。

    - ``netcon``（默认）：真 REST 数据；指标由 simulator 行为模型补齐。
    - ``simulator``：全仿真数据（无 PT 环境开发/演示）。
    - ``auto``：启动探测 NC，可用用 netcon，否则回退 simulator（数据仍标 source）。
    """
    driver_name = (settings.driver or "netcon").lower()
    if driver_name not in ("netcon", "simulator", "auto", "snmp"):
        logger.warning("未知 DRIVER=%r，回退为 netcon", settings.driver)
        driver_name = "netcon"

    if driver_name == "snmp":
        # W10：SNMP 真实设备驱动（指标即真实采集，不需要 simulator 补指标）
        from app.collectors.snmp import SnmpDriver

        return SnmpDriver(settings), None

    if driver_name == "simulator":
        sim = SimulatorDriver(settings)
        return sim, (sim if settings.simulate_metrics else None)

    netcon = NetconDriver(settings)
    if driver_name == "auto":
        reachable = await netcon.probe()
        if not reachable:
            logger.warning("DRIVER=auto 且 NC 不可用，回退到 simulator 驱动（数据 source=simulator）")
            await netcon.close()
            sim = SimulatorDriver(settings)
            return sim, (sim if settings.simulate_metrics else None)

    metrics_driver: Optional[MetricGenerator] = SimulatorDriver(settings) if settings.simulate_metrics else None
    return netcon, metrics_driver
