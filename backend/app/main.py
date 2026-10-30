# -*- coding: utf-8 -*-
"""FastAPI 应用：REST 查询 + WebSocket 实时推送 + 后台采集任务生命周期。

接口一览
========
- ``GET  /api/health``               服务健康 + 采集状态（NC 是否可达、轮次、错误）
- ``GET  /api/devices``              设备快照
- ``GET  /api/topology``             拓扑节点 + 链路
- ``GET  /api/hosts``                主机/终端清单
- ``GET  /api/issues``               当前活跃健康问题
- ``GET  /api/alerts?limit=100&state=all`` 历史告警（active/cleared/info）
- ``GET  /api/metrics/{device_id}``  设备指标时序（source=simulator）
- ``GET  /api/network-health``       网络健康总览
- ``GET  /api/drivers``              可用/当前驱动信息
- ``GET  /api/detection/rules``      W3 检测规则目录 + 告警生命周期统计
- ``DELETE /api/alerts/{id}``         软删除已恢复告警（active 只能静默，不可删除）
- ``POST /api/alerts/clear``          批量清空已恢复告警（scope=cleared|all_cleared）
- ``POST /api/alerts/{id}/silence``   静默活跃告警；``POST /api/alerts/{id}/restore`` 撤销软删
- ``GET  /api/audit``                 操作审计（operator/action/target/detail/time）
- ``POST /api/path-trace``           W10 路径追踪（NC flow-analysis，2 分钟缓存）
- ``GET  /api/alerts/groups``         W10 告警根因分组 + 下游抑制（展示层）
- ``POST /api/alerts/groups/{id}/ack`` 整组确认（审计）
- ``GET  /api/notify/status`` / ``POST /api/notify/test``  W10 Webhook 通知状态/测试
- ``GET  /api/scene`` / ``/api/scene/pending``  场景配置（4 楼栋/12 设备，配置驱动）
- ``POST /api/scene/buildings`` 等    场景写操作（X-Operator + 审计 + version++）
- ``POST /api/debug/simulator/*``    仿真故障注入（仅仿真模式，用于演示/联调）
- ``WS   /ws``                       连接即推一份全量快照，之后每轮推快照 + 增量告警

启动：``uvicorn app.main:app --host 127.0.0.1 --port 8000``
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.alert_groups import build_alert_groups
from app.collectors.simulator import SimulatorDriver
from app.config import Settings
from app.notify import NotifyService
from app.path_trace import PathTraceService
from app.pipeline import CollectorPipeline, build_drivers, iso_time
from app.schemas import (
    AlertClearRequest,
    BuildingCreate,
    DeviceUpsert,
    PathTraceRequest,
)
from app.scene import SceneError, SceneService
from app.store import Store

logger = logging.getLogger("campus_twin.api")


def setup_logging(level: str = "INFO") -> None:
    """统一日志格式（uvicorn 启动后 root handler 可能已存在，避免重复添加）。"""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        )
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # httpx 每个请求一条 INFO，5 秒轮询下太吵，压到 WARNING。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def normalize_operator(value: Optional[str]) -> str:
    """X-Operator 头规范化：默认 web-ui，最长 64 字符，用于审计。"""
    text = str(value or "").strip()
    return (text or "web-ui")[:64]


class ConnectionManager:
    """WebSocket 连接管理：单个发送锁保证快照/告警不会交错写坏帧。"""

    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        self._send_locks: Dict[WebSocket, asyncio.Lock] = {}
        # Python 3.9 的 asyncio.Lock 在“无当前事件循环”时创建会报错；应用工厂
        # 可能在 asyncio.run() 之后被调用（如测试），因此延迟到首次异步使用再创建。
        self._registry_lock: Optional[asyncio.Lock] = None

    def _lock(self) -> asyncio.Lock:
        if self._registry_lock is None:
            self._registry_lock = asyncio.Lock()
        return self._registry_lock

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock():
            self._connections.add(websocket)
            self._send_locks[websocket] = asyncio.Lock()
        logger.info("WebSocket 客户端接入（当前 %d 个连接）", len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock():
            self._connections.discard(websocket)
            self._send_locks.pop(websocket, None)
        logger.info("WebSocket 客户端断开（当前 %d 个连接）", len(self._connections))

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def send(self, websocket: WebSocket, message: Dict[str, Any]) -> None:
        lock = self._send_locks.get(websocket)
        if lock is None:
            return
        async with lock:
            await websocket.send_json(message)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        async with self._lock():
            targets = list(self._connections)
        dead: List[WebSocket] = []
        for websocket in targets:
            try:
                await self.send(websocket, message)
            except Exception:  # noqa: BLE001 - 单个客户端写失败不影响其它客户端
                dead.append(websocket)
        for websocket in dead:
            await self.disconnect(websocket)


# --------------------------------------------------------------------------- #
# 应用工厂（测试可传入独立 Settings：临时 DB / 仿真驱动 / 超时）
# --------------------------------------------------------------------------- #
def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    setup_logging(settings.log_level)
    manager = ConnectionManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.db_path)
        app.state.store = store
        app.state.settings = settings
        app.state.manager = manager
        # W9：场景配置服务（首次访问自动 seed 默认 4 楼栋/12 设备）
        scene = SceneService(store)
        app.state.scene = scene
        driver, metrics_driver = await build_drivers(settings)
        # W10：路径追踪服务（NC flow-analysis + 缓存）与 Webhook 通知服务
        app.state.path_trace = PathTraceService(driver, settings)
        notify = NotifyService(settings)
        app.state.notify = notify
        await notify.start()
        pipeline = CollectorPipeline(
            settings=settings,
            store=store,
            driver=driver,
            metrics_driver=metrics_driver,
            broadcast=manager.broadcast,
            scene=scene,
            notify=notify,
        )
        app.state.pipeline = pipeline
        await pipeline.start()
        try:
            yield
        finally:
            await pipeline.stop()
            await notify.stop()
            logger.info("HTTP 服务已关闭")

    app = FastAPI(
        title="Campus Twin API",
        version=__version__,
        description=(
            "校园网数字孪生运维平台后端：采集 Packet Tracer Network Controller 的"
            "真实 REST 数据，指标由行为仿真引擎补齐（source=simulator）。"
        ),
        lifespan=lifespan,
    )
    app.state.manager = manager
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _pipeline() -> CollectorPipeline:
        pipeline = getattr(app.state, "pipeline", None)
        if pipeline is None:  # lifespan 未执行（例如直接 import 后调用端点）
            raise HTTPException(status_code=503, detail="采集流水线尚未启动")
        return pipeline

    def _simulator() -> Optional[SimulatorDriver]:
        pipeline = _pipeline()
        for candidate in (pipeline.metrics_driver, pipeline.driver):
            if isinstance(candidate, SimulatorDriver):
                return candidate
        return None

    # ------------------------------------------------------------------ #
    # REST
    # ------------------------------------------------------------------ #
    @app.get("/", tags=["meta"])
    def root() -> Dict[str, Any]:
        return {
            "name": "Campus Twin API",
            "version": __version__,
            "docs": "/docs",
            "websocket": "/ws",
            "endpoints": [
                "/api/health",
                "/api/devices",
                "/api/topology",
                "/api/hosts",
                "/api/issues",
                "/api/alerts",
                "/api/metrics/{device_id}",
                "/api/network-health",
                "/api/drivers",
                "/api/detection/rules",
                "/api/alerts/clear",
                "/api/audit",
                "/api/scene",
                "/api/scene/pending",
                "/api/path-trace",
                "/api/alerts/groups",
                "/api/notify/status",
                "/api/notify/test",
            ],
        }

    @app.get("/api/health", tags=["meta"])
    def health() -> Dict[str, Any]:
        """服务健康 + 采集状态。无论 NC 是否在线，本服务自身返回 status=ok。"""
        pipeline = _pipeline()
        payload: Dict[str, Any] = {
            "status": "ok",
            "version": __version__,
            "server_time": iso_time(time.time()),
        }
        payload.update(pipeline.status())
        try:
            payload["db"] = app.state.store.stats()
        except Exception:  # noqa: BLE001 - 诊断信息缺失不影响健康检查
            logger.exception("读取 DB 统计失败")
            payload["db"] = {"error": "unavailable"}
        payload["websocket_clients"] = manager.connection_count
        try:
            payload["scene_version"] = app.state.scene.current_version()
        except Exception:  # noqa: BLE001 - 诊断字段缺失不影响健康检查
            payload["scene_version"] = None
        return payload

    @app.get("/api/devices", tags=["data"])
    def list_devices() -> Dict[str, Any]:
        data = _pipeline().snapshot_data()
        return {
            "source": data["source"],
            "stale": data["stale"],
            "captured_at": data["captured_at"],
            "captured_at_iso": data["captured_at_iso"],
            "count": len(data["devices"]),
            "devices": data["devices"],
        }

    @app.get("/api/topology", tags=["data"])
    def get_topology() -> Dict[str, Any]:
        data = _pipeline().snapshot_data()
        topology = data["topology"]
        return {
            "source": topology["source"],
            "stale": data["stale"],
            "captured_at": topology["captured_at"],
            "captured_at_iso": iso_time(topology["captured_at"]),
            "counts": {
                "nodes": len(topology["nodes"]),
                "links": len(topology["links"]),
            },
            "nodes": topology["nodes"],
            "links": topology["links"],
        }

    @app.get("/api/hosts", tags=["data"])
    def list_hosts() -> Dict[str, Any]:
        """主机快照（含 ping_status 与新鲜度时间）。

        W3-A：netcon 驱动每轮都会重新拉取 /host 并用 seen_at=now 重建 host 模型，
        响应里的 ``observed_at`` / ``hosts_observed_at`` 可用于确认不是旧缓存。
        """
        data = _pipeline().snapshot_data()
        observed_at = data.get("hosts_observed_at") or data.get("captured_at")
        return {
            "source": data["source"],
            "stale": data["stale"],
            "count": len(data["hosts"]),
            "observed_at": observed_at,
            "observed_at_iso": iso_time(observed_at),
            "snapshot_age_seconds": (
                round(max(time.time() - observed_at, 0.0), 1) if observed_at else None
            ),
            "hosts": data["hosts"],
        }

    @app.get("/api/issues", tags=["data"])
    def list_issues(
        limit: int = Query(default=200, ge=1, le=1000)
    ) -> Dict[str, Any]:
        data = _pipeline().snapshot_data()
        issues = data["issues"][:limit]
        return {
            "source": data["source"],
            "stale": data["stale"],
            "observed_at": data.get("observed_at"),
            "observed_at_iso": iso_time(data.get("observed_at")),
            "count": len(issues),
            "issues": issues,
        }

    @app.get("/api/network-health", tags=["data"])
    def network_health() -> Dict[str, Any]:
        """网络健康总览（W3.1 字段自洽口径）。

        ``network_health.device_*`` / ``health_percentage`` 由当前设备列表自算
        （Managed 且非 Unreachable 视为健康，``source_of_truth=computed_from_devices``）；
        NC 原始汇总字段保留在 ``nc_*`` 中用于审计对比；``device_count`` 便于与
        ``/api/devices`` 交叉校验。
        """
        data = _pipeline().snapshot_data()
        health = data["network_health"]
        return {
            "source": data["source"],
            "stale": data["stale"],
            "device_count": len(data["devices"]),
            "source_of_truth": (health or {}).get("source_of_truth"),
            "network_health": health,
        }

    @app.get("/api/alerts", tags=["data"])
    def list_alerts(
        limit: int = Query(default=min(settings.max_alerts, 1000), ge=1, le=1000),
        severity: Optional[str] = Query(default=None),
        state: str = Query(
            default="all",
            pattern="^(all|active|cleared|info)$",
            description="告警生命周期过滤：active/cleared/info/all；默认 all",
        ),
        include_deleted: bool = Query(
            default=False,
            description="是否包含已软删除告警（默认 False；审计/内部用）",
        ),
        status: Optional[str] = Query(
            default=None,
            pattern="^(open|silenced|all)$",
            description="W9 静默状态过滤：open/silenced/all",
        ),
    ) -> Dict[str, Any]:
        """告警历史（W3-C：一个 dedup_key 同时最多一条 active，恢复只改状态）。

        - ``state=active``：当前进行中的告警；
        - ``state=cleared``：已恢复的历史（含 stale_source 标记）；
        - ``state=info``：一次性信息事件（设备上线/新增链路）；
        - "已清除"通知不再逐条入库，历史查询与 WebSocket 恢复摘要均不刷屏。
        """
        store = app.state.store
        alerts = store.load_alerts(
            limit=limit,
            severity=severity,
            state=state,
            include_deleted=include_deleted,
            status=status,
        )
        return {
            "count": len(alerts),
            "state": state,
            "include_deleted": include_deleted,
            "summary": store.alert_stats(),
            "alerts": alerts,
        }

    @app.get("/api/alerts/summary", tags=["data"])
    def alerts_summary() -> Dict[str, Any]:
        """告警生命周期统计（active/cleared/info）。"""
        return app.state.store.alert_stats()

    # ------------------------------------------------------------------ #
    # W9 告警数据管理（软删除 / 清空 / 静默 / 恢复 + 审计）
    # 不变式：active 告警不可删除（只能 silence），避免误删进行中的故障；
    # 硬删除仅由 TTL 后台清理执行。
    # ------------------------------------------------------------------ #
    @app.delete("/api/alerts/{alert_id}", tags=["alerts-manage"])
    def delete_alert(
        alert_id: int,
        x_operator: str = Header(default="web-ui", alias="X-Operator"),
    ) -> Dict[str, Any]:
        """软删除一条已恢复告警（deleted_at）；active 告警返回 409。"""
        store = app.state.store
        operator = normalize_operator(x_operator)
        row = store.get_alert(alert_id)
        if row is None or row.get("deleted_at"):
            store.add_audit(
                operator, "alert.delete", str(alert_id), {"reason": "not_found"},
                result="rejected",
            )
            raise HTTPException(status_code=404, detail="告警不存在或已删除")
        if row.get("state") == "active":
            store.add_audit(
                operator, "alert.delete", str(alert_id), {"state": "active"},
                result="rejected",
            )
            raise HTTPException(
                status_code=409, detail="active 告警不可删除，请使用 silence 静默接口"
            )
        updated = store.soft_delete_alert(alert_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="告警不存在或已删除")
        store.add_audit(
            operator,
            "alert.delete",
            str(alert_id),
            {"alert_type": row.get("alert_type"), "state": row.get("state")},
        )
        logger.info("告警软删除 id=%s operator=%s", alert_id, operator)
        return {"deleted": 1, "id": alert_id, "deleted_at": updated.get("deleted_at")}

    @app.post("/api/alerts/clear", tags=["alerts-manage"])
    def clear_alerts(
        request: AlertClearRequest,
        x_operator: str = Header(default="web-ui", alias="X-Operator"),
    ) -> Dict[str, Any]:
        """批量清空已恢复告警（软删除，可 restore 撤销；active 永不删除）。"""
        store = app.state.store
        operator = normalize_operator(x_operator)
        deleted = store.clear_alerts(scope=request.scope, before=request.before)
        store.add_audit(
            operator,
            "alert.clear",
            "alerts",
            {"scope": request.scope, "before": request.before, "deleted": deleted},
        )
        logger.info(
            "清空告警 scope=%s deleted=%d operator=%s",
            request.scope, deleted, operator,
        )
        return {"deleted": deleted, "scope": request.scope, "before": request.before}

    @app.post("/api/alerts/{alert_id}/silence", tags=["alerts-manage"])
    def silence_alert(
        alert_id: int,
        x_operator: str = Header(default="web-ui", alias="X-Operator"),
    ) -> Dict[str, Any]:
        """静默一条活跃告警（status=silenced，不删除、不改 state）。"""
        store = app.state.store
        operator = normalize_operator(x_operator)
        row = store.get_alert(alert_id)
        if row is None or row.get("deleted_at"):
            store.add_audit(
                operator, "alert.silence", str(alert_id), {"reason": "not_found"},
                result="rejected",
            )
            raise HTTPException(status_code=404, detail="告警不存在或已删除")
        if row.get("state") != "active":
            store.add_audit(
                operator,
                "alert.silence",
                str(alert_id),
                {"state": row.get("state")},
                result="rejected",
            )
            raise HTTPException(status_code=409, detail="只能静默 active 告警")
        updated = store.silence_alert(alert_id)
        store.add_audit(
            operator, "alert.silence", str(alert_id),
            {"alert_type": row.get("alert_type")},
        )
        logger.info("告警静默 id=%s operator=%s", alert_id, operator)
        return {
            "silenced": 1,
            "id": alert_id,
            "state": (updated or {}).get("state"),
            "status": (updated or {}).get("status"),
            "silenced_at": (updated or {}).get("silenced_at"),
        }

    @app.post("/api/alerts/{alert_id}/restore", tags=["alerts-manage"])
    def restore_alert(
        alert_id: int,
        x_operator: str = Header(default="web-ui", alias="X-Operator"),
    ) -> Dict[str, Any]:
        """撤销软删除（deleted_at 置空）。"""
        store = app.state.store
        operator = normalize_operator(x_operator)
        row = store.get_alert(alert_id)
        if row is None:
            store.add_audit(
                operator, "alert.restore", str(alert_id), {"reason": "not_found"},
                result="rejected",
            )
            raise HTTPException(status_code=404, detail="告警不存在")
        if not row.get("deleted_at"):
            store.add_audit(
                operator, "alert.restore", str(alert_id), {"reason": "not_deleted"},
                result="rejected",
            )
            raise HTTPException(status_code=409, detail="告警未被删除，无需恢复")
        store.restore_alert(alert_id)
        store.add_audit(
            operator, "alert.restore", str(alert_id),
            {"alert_type": row.get("alert_type")},
        )
        logger.info("告警恢复 id=%s operator=%s", alert_id, operator)
        return {"restored": 1, "id": alert_id, "deleted_at": None}

    @app.get("/api/audit", tags=["audit"])
    @app.get("/api/alerts/audit", tags=["audit"], include_in_schema=False)
    def list_audit(
        limit: int = Query(default=100, ge=1, le=1000),
        action: Optional[str] = Query(default=None, description="按 action 过滤"),
        operator: Optional[str] = Query(default=None, description="按 operator 过滤"),
    ) -> Dict[str, Any]:
        """操作审计列表（所有告警管理/场景写操作，时间倒序）。"""
        items = app.state.store.load_audit(
            limit=limit, action=action, operator=operator
        )
        for item in items:
            item["created_at_iso"] = iso_time(item.get("created_at"))
        return {"count": len(items), "items": items}

    @app.get("/api/detection/rules", tags=["meta"])
    def detection_rules() -> Dict[str, Any]:
        """W3 检测规则目录 + 引擎状态（规则结构化，便于文档/评审核对）。"""
        pipeline = _pipeline()
        status = pipeline.status()
        return {
            "engine": status.get("detection", {}).get("engine"),
            "rules": pipeline.detector.rule_catalog(),  # type: ignore[attr-defined]
            "alert_states": app.state.store.alert_stats(),
            "notes": [
                "恢复判定优先使用 host.ping_status / device.reachabilityStatus 实际状态，",
                "不等待 NC /assurance/health-issues 清除（NC 实测延迟 >3 分钟）。",
                "NC issue 仍在但数据源已恢复时，告警 cleared 会带 stale_source=true。",
                "observed_at=宿主机采集时间（展示用）；source_ts=NC 原始时间（审计用）。",
            ],
        }

    @app.get("/api/metrics/{device_id}", tags=["data"])
    def device_metrics(
        device_id: str,
        limit: int = Query(default=240, ge=1, le=5000),
        metric: Optional[str] = Query(default=None, description="过滤单个指标名，如 if_utilization_pct"),
    ) -> Dict[str, Any]:
        pipeline = _pipeline()
        data = pipeline.snapshot_data()
        known = any(device["id"] == device_id for device in data["devices"])
        points = app.state.store.load_metrics(device_id=device_id, limit=limit, metric=metric)
        return {
            "device_id": device_id,
            "known_device": known,
            "source": "simulator",  # PT 不暴露计数器，指标恒为行为仿真数据
            "physical_constraint": "if_*_bps <= speed_bps（超出会被钳制并标记 clamped）",
            "count": len(points),
            "metrics": points,
        }

    @app.get("/api/drivers", tags=["meta"])
    def drivers_info() -> Dict[str, Any]:
        pipeline = _pipeline()
        return {
            "active": getattr(pipeline.driver, "name", "unknown"),
            "metrics_generator": getattr(pipeline.metrics_driver, "name", None),
            "available": ["netcon", "simulator"],
            "selection": settings.driver,
            "driver_status": pipeline.status().get("driver_status"),
            "signature": {
                "netcon": "GET /network-device, /topology/physical-topology, /host, /assurance/health-issues, /network-health",
                "simulator": "行为模型：分时段负载 + 故障后流量重分布（source=simulator）",
            },
        }

    # ------------------------------------------------------------------ #
    # W9 场景配置（配置驱动 3D 场景；写操作 X-Operator + 审计 + version++）
    # ------------------------------------------------------------------ #
    @app.get("/api/scene", tags=["scene"])
    def get_scene() -> Dict[str, Any]:
        """场景配置（4 楼栋 / 12 设备默认 seed，配置驱动前端渲染）。"""
        return app.state.scene.get_scene()

    @app.get("/api/scene/pending", tags=["scene"])
    def get_scene_pending() -> Dict[str, Any]:
        """待确认新设备：inventory 中存在但 scene_config 未纳管的设备（每轮采集后刷新）。"""
        service = app.state.scene
        payload = service.pending_devices()
        payload["scene_version"] = service.current_version()
        payload["note"] = (
            "inventory 中尚未纳入 scene_config 的设备；含管理 IP/型号/可达性/拓扑连接关系"
            "与五级归属推断前三级结果（topology_neighbor→hostname_keyword→ip_prefix）"
        )
        return payload

    def _scene_http_error(exc: SceneError) -> HTTPException:
        return HTTPException(status_code=getattr(exc, "status_code", 400), detail=str(exc))

    @app.post("/api/scene/buildings", tags=["scene"])
    def add_scene_building(
        request: BuildingCreate,
        x_operator: str = Header(default="web-ui", alias="X-Operator"),
    ) -> Dict[str, Any]:
        """新增楼栋（默认 status=pending，确认后才算正式场景）。"""
        operator = normalize_operator(x_operator)
        try:
            return app.state.scene.add_building(
                operator,
                name=request.name,
                position=request.position.model_dump() if request.position else None,
                size=request.size.model_dump() if request.size else None,
                style=request.style,
                status=request.status,
                building_id=request.id,
            )
        except SceneError as exc:
            raise _scene_http_error(exc) from exc

    @app.post("/api/scene/buildings/{building_id}/devices", tags=["scene"])
    def upsert_scene_device(
        building_id: str,
        request: DeviceUpsert,
        x_operator: str = Header(default="web-ui", alias="X-Operator"),
    ) -> Dict[str, Any]:
        """新增/更新设备位置（upsert；同楼坐标冲突自动偏移并在响应提示）。"""
        operator = normalize_operator(x_operator)
        try:
            return app.state.scene.upsert_device(
                operator,
                building_id=building_id,
                name=request.name,
                device_type=request.type,
                position=request.position.model_dump() if request.position else None,
                status=request.status,
                source=request.source,
                model=request.model,
                mgmt_ip=request.mgmt_ip,
            )
        except SceneError as exc:
            raise _scene_http_error(exc) from exc

    @app.post("/api/scene/devices/{name}/confirm", tags=["scene"])
    def confirm_scene_device(
        name: str,
        x_operator: str = Header(default="web-ui", alias="X-Operator"),
    ) -> Dict[str, Any]:
        """确认新设备（pending → confirmed）。"""
        try:
            return app.state.scene.confirm_device(normalize_operator(x_operator), name)
        except SceneError as exc:
            raise _scene_http_error(exc) from exc

    @app.delete("/api/scene/devices/{name}", tags=["scene"])
    def delete_scene_device(
        name: str,
        x_operator: str = Header(default="web-ui", alias="X-Operator"),
    ) -> Dict[str, Any]:
        """删除场景设备位置（版本化 tombstone + 审计）。"""
        try:
            return app.state.scene.delete_device(normalize_operator(x_operator), name)
        except SceneError as exc:
            raise _scene_http_error(exc) from exc

    @app.post("/api/scene/reset", tags=["scene"])
    def reset_scene(
        x_operator: str = Header(default="web-ui", alias="X-Operator"),
    ) -> Dict[str, Any]:
        """重置为默认 4 楼栋 / 12 设备（审计 + version++）。"""
        return app.state.scene.reset(normalize_operator(x_operator))

    # ------------------------------------------------------------------ #
    # 仿真故障注入（仅 DRIVER=simulator 或启用仿真指标时可用）
    # ------------------------------------------------------------------ #
    @app.post("/api/debug/simulator/link-failure", tags=["debug"])
    def inject_link_failure(payload: Dict[str, Any]) -> Dict[str, Any]:
        simulator = _simulator()
        if simulator is None:
            raise HTTPException(
                status_code=409,
                detail="当前未启用仿真驱动/仿真指标（DRIVER=simulator 或 SIMULATE_METRICS=1 时可用）",
            )
        link_id = str(payload.get("link_id") or "")
        if not link_id:
            raise HTTPException(status_code=422, detail="缺少 link_id")
        ok = simulator.inject_link_failure(link_id)
        if not ok:
            raise HTTPException(status_code=404, detail="未知仿真链路：" + link_id)
        logger.info("仿真故障注入：链路 %s 中断（下一轮采集生效）", link_id)
        return {"ok": True, "failed_links": sorted(simulator.failed_links)}

    @app.post("/api/debug/simulator/device-failure", tags=["debug"])
    def inject_device_failure(payload: Dict[str, Any]) -> Dict[str, Any]:
        simulator = _simulator()
        if simulator is None:
            raise HTTPException(status_code=409, detail="当前未启用仿真驱动")
        device_id = str(payload.get("device_id") or "")
        if not device_id:
            raise HTTPException(status_code=422, detail="缺少 device_id")
        ok = simulator.inject_device_failure(device_id)
        if not ok:
            raise HTTPException(status_code=404, detail="未知仿真设备：" + device_id)
        logger.info("仿真故障注入：设备 %s 不可达（下一轮采集生效）", device_id)
        return {"ok": True, "failed_devices": sorted(simulator.failed_devices)}

    @app.post("/api/debug/simulator/reset", tags=["debug"])
    def reset_simulator_failures() -> Dict[str, Any]:
        simulator = _simulator()
        if simulator is None:
            raise HTTPException(status_code=409, detail="当前未启用仿真驱动")
        simulator.clear_failures()
        logger.info("仿真故障已全部清除（下一轮采集生效）")
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # W10 路径追踪（NC flow-analysis；缓存 2 分钟）
    # ------------------------------------------------------------------ #
    @app.post("/api/path-trace", tags=["path-trace"])
    async def path_trace(request: PathTraceRequest) -> Dict[str, Any]:
        """追踪 source_ip → dest_ip 的转发路径（NC flow-analysis 实时结果）。"""
        pipeline = _pipeline()
        return await app.state.path_trace.trace(
            request.source_ip, request.dest_ip, pipeline.snapshot_data()
        )

    # ------------------------------------------------------------------ #
    # W10 告警聚合与根因分组（展示层，不修改告警状态）
    # ------------------------------------------------------------------ #
    @app.get("/api/alerts/groups", tags=["alerts-manage"])
    def list_alert_groups(
        state: str = Query(
            default="active",
            pattern="^(active|cleared|info|all)$",
            description="按告警生命周期过滤：active（默认）/cleared/info/all",
        ),
    ) -> Dict[str, Any]:
        """告警根因分组：同链路/同设备归组，根因优先级
        link_down > device_unreachable > device_offline > host_down；下游
        host/device 告警标记 ``suppressed_by``（仅展示层抑制）。"""
        snapshot = _pipeline().snapshot_data()
        alerts = app.state.store.load_alerts(limit=1000, state=state)
        return build_alert_groups(alerts, snapshot, state=state)

    @app.post("/api/alerts/groups/{group_id}/ack", tags=["alerts-manage"])
    def ack_alert_group(
        group_id: str,
        x_operator: str = Header(default="web-ui", alias="X-Operator"),
    ) -> Dict[str, Any]:
        """整组确认：把该分组内所有成员告警 acknowledged=1（写审计）。"""
        store = app.state.store
        operator = normalize_operator(x_operator)
        snapshot = _pipeline().snapshot_data()
        alerts = store.load_alerts(limit=1000, state="active")
        groups = build_alert_groups(alerts, snapshot, state="active")
        group = next(
            (item for item in groups["groups"] if item["group_id"] == group_id), None
        )
        if group is None:
            store.add_audit(
                operator, "alert.group_ack", group_id,
                {"reason": "not_found"}, result="rejected",
            )
            raise HTTPException(status_code=404, detail="告警分组不存在或已过期")
        member_ids = [
            member.get("id")
            for member in group.get("members", [])
            if member.get("id") is not None
        ]
        acknowledged = store.ack_alerts(member_ids)
        store.add_audit(
            operator,
            "alert.group_ack",
            group_id,
            {
                "root_cause": group.get("root_cause"),
                "root_device_id": group.get("root_device_id"),
                "acknowledged": acknowledged,
            },
        )
        return {
            "ok": True,
            "group_id": group_id,
            "acknowledged": acknowledged,
            "root_cause": group.get("root_cause"),
        }

    # ------------------------------------------------------------------ #
    # W10 Webhook 通知状态 / 测试
    # ------------------------------------------------------------------ #
    @app.get("/api/notify/status", tags=["notify"])
    def notify_status() -> Dict[str, Any]:
        """Webhook 通知配置与发送状态（enabled/type/min_severity/统计）。"""
        return app.state.notify.status()

    @app.post("/api/notify/test", tags=["notify"])
    async def notify_test() -> Dict[str, Any]:
        """立即发送一条测试 Webhook 消息（用于前端「发送测试通知」按钮）。"""
        result = await app.state.notify.send_test()
        result["status"] = app.state.notify.status()
        return result

    # ------------------------------------------------------------------ #
    # WebSocket：连接即推快照，之后由采集流水线广播
    # ------------------------------------------------------------------ #
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await manager.connect(websocket)
        try:
            pipeline = getattr(app.state, "pipeline", None)
            if pipeline is not None:
                await manager.send(
                    websocket, {"type": "snapshot", "data": pipeline.snapshot_data()}
                )
            while True:
                message = await websocket.receive_text()
                # 客户端心跳；也用来让服务端及时发现断连。
                if message.strip().lower() in ("ping", "heartbeat"):
                    await manager.send(websocket, {"type": "pong", "ts": time.time()})
        except WebSocketDisconnect:
            await manager.disconnect(websocket)
        except Exception:  # noqa: BLE001 - 单连接异常不能影响服务
            logger.exception("WebSocket 连接异常")
            await manager.disconnect(websocket)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        log_level=Settings.from_env().log_level.lower(),
    )
