# -*- coding: utf-8 -*-
"""采集流水线单测：变更检测 + poll_once 端到端（不依赖 PT / 网络）。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.collectors.base import CollectResult
from app.collectors.simulator import SimulatorDriver
from app.config import Settings
from app.models import Device, HealthIssue, Link, Topology, TopologyNode
from app.pipeline import CollectorPipeline
from app.store import Store


def run_async(coro):
    return asyncio.run(coro)


def make_result(
    device_specs: Sequence[Tuple[str, Optional[str]]] = (("d1", "Reachable"), ("d2", "Reachable")),
    link_specs: Sequence[Tuple[str, str, str, Optional[str]]] = (
        ("l1", "d1", "d2", "up"),
    ),
    issues: Sequence[HealthIssue] = (),
    errors: Optional[Dict[str, str]] = None,
) -> CollectResult:
    devices = [
        Device(id=device_id, hostname=device_id.upper(), reachability=reachability, source="netcon")
        for device_id, reachability in device_specs
    ]
    links = [
        Link(
            id=link_id,
            source=source,
            target=target,
            start_port_name="Gi0/1",
            end_port_name="Gi0/2",
            link_status=status,
            data_source="netcon",
        )
        for link_id, source, target, status in link_specs
    ]
    topology = Topology(
        nodes=[TopologyNode(id=device_id, source="netcon") for device_id, _ in device_specs],
        links=links,
        source="netcon",
    )
    return CollectResult(
        devices=devices,
        topology=topology,
        issues=list(issues),
        source="netcon",
        ok=True,
        errors=errors or {},
    )


class TestPipelinePollOnce:
    def test_poll_once_broadcasts_snapshot_and_persists(self, tmp_path):
        settings = Settings(
            db_path=str(tmp_path / "campus.db"),
            driver="simulator",
            poll_interval=3600.0,
            metrics_retention_seconds=3600,
        )
        store = Store(settings.db_path)
        store.init_db()
        simulator = SimulatorDriver(settings)
        messages: List[Dict[str, Any]] = []

        async def broadcast(message: Dict[str, Any]) -> None:
            messages.append(message)

        pipeline = CollectorPipeline(
            settings=settings,
            store=store,
            driver=simulator,
            metrics_driver=simulator,
            broadcast=broadcast,
        )

        async def scenario():
            await pipeline.poll_once()
            first_stats = store.stats()
            await pipeline.poll_once()
            return first_stats

        stats = run_async(scenario())
        assert stats["device_rows"] > 0
        assert stats["metric_rows"] > 0
        snapshots = [message for message in messages if message["type"] == "snapshot"]
        assert len(snapshots) == 2
        assert snapshots[0]["data"]["counts"]["devices"] > 0
        assert snapshots[0]["data"]["source"] == "simulator"
        # 没有故障注入 → 不应该有告警
        assert [message for message in messages if message["type"] == "alert"] == []
        status = pipeline.status()
        assert status["status"] == "ok"
        assert status["polls"] == 2
        assert status["stale"] is False

    def test_failure_transition_marks_stale_and_keeps_last_snapshot(self, tmp_path):
        settings = Settings(
            db_path=str(tmp_path / "campus.db"),
            driver="simulator",
            poll_interval=3600.0,
        )
        store = Store(settings.db_path)
        store.init_db()
        simulator = SimulatorDriver(settings)
        messages: List[Dict[str, Any]] = []

        async def broadcast(message: Dict[str, Any]) -> None:
            messages.append(message)

        pipeline = CollectorPipeline(
            settings=settings,
            store=store,
            driver=simulator,
            metrics_driver=simulator,
            broadcast=broadcast,
        )

        async def scenario():
            await pipeline.poll_once()
            # 模拟数据源不可用：直接替换为返回空结果的驱动行为
            original_collect = simulator.collect

            async def failing_collect():
                return CollectResult.unavailable("simulator", "模拟连接失败")

            simulator.collect = failing_collect  # type: ignore[assignment]
            try:
                await pipeline.poll_once()
            finally:
                simulator.collect = original_collect  # type: ignore[assignment]
            return pipeline.status()

        status = run_async(scenario())
        assert status["status"] == "ok"
        assert status["stale"] is True
        assert status["collect_errors"] == 1
        assert status["counts"]["devices"] > 0  # 保留最近一次成功快照
        assert any(message["type"] == "status" for message in messages)

    def test_baseline_alerts_persisted_silently_without_broadcast(self, tmp_path):
        """首轮基线：静默记录现存 active 告警（可查可恢复），但不通过 WS 广播。"""
        from tests.helpers import ScriptedDriver, make_host, make_result

        settings = Settings(
            db_path=str(tmp_path / "campus.db"),
            driver="netcon",
            poll_interval=3600.0,
            simulate_metrics=False,
        )
        store = Store(settings.db_path)
        store.init_db()
        fault = make_result(hosts=[make_host("h1", hostname="PC-1", ping_status="FAILURE")])
        driver = ScriptedDriver([fault, fault])
        messages: List[Dict[str, Any]] = []

        async def broadcast(message: Dict[str, Any]) -> None:
            messages.append(message)

        pipeline = CollectorPipeline(
            settings=settings, store=store, driver=driver, broadcast=broadcast
        )

        async def scenario():
            await pipeline.poll_once()  # 基线：静默持久化
            baseline_active = store.load_active_alerts()
            await pipeline.poll_once()  # 仍故障：去重
            return baseline_active

        baseline_active = run_async(scenario())
        assert len(baseline_active) == 1
        assert baseline_active[0].alert_type == "host_down"
        # 基线轮不广播 alert 帧；第二轮因去重也不再广播
        assert [msg for msg in messages if msg["type"] == "alert"] == []
        assert len(store.load_active_alerts()) == 1

    def test_resolve_metrics_with_devices_but_no_links(self, tmp_path):
        """真实 NC 刚加载网络时拓扑可能只有节点没有链路：仍应生成设备级仿真指标。"""
        settings = Settings(driver="netcon", simulate_metrics=True)
        pipeline = CollectorPipeline(
            settings=settings,
            store=Store(str(tmp_path / "campus.db")),
            driver=SimulatorDriver(settings),
            metrics_driver=SimulatorDriver(settings),
        )
        result = CollectResult(
            devices=[Device(id="d1", hostname="R1", source="netcon")],
            topology=Topology(nodes=[TopologyNode(id="d1", source="netcon")], source="netcon"),
            source="netcon",
        )
        points = pipeline._resolve_metrics(result)  # noqa: SLF001 - 单测内部方法
        assert {point.metric for point in points} == {
            "device_utilization_pct",
            "cpu_pct",
            "mem_pct",
        }
        assert all(point.source == "simulator" for point in points)

