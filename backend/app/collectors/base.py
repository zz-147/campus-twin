# -*- coding: utf-8 -*-
"""采集驱动契约（可插拔驱动）。

驱动只需要实现三个异步方法：

- ``name``    驱动名（写入日志/API，便于区分数据来源）
- ``probe()`` 轻量连通性探测，返回 bool，禁止抛异常
- ``collect()`` 拉取一轮全量快照，返回 :class:`CollectResult`；网络/解析错误
  必须被驱动内部消化（降级为空数据 + ``ok=False``），不能把异常抛给 pipeline

这样 pipeline / REST / WebSocket 与具体数据源完全解耦：以后加 Replay（回放）、
Config（show run 解析）等驱动，只要实现同一契约即可。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, runtime_checkable

from app.models import Device, HealthIssue, Host, MetricPoint, NetworkHealth, SourceType, Topology


@dataclass
class CollectResult:
    """一轮采集结果（驱动输出、pipeline 输入）。"""

    devices: List[Device] = field(default_factory=list)
    topology: Topology = field(default_factory=Topology)
    hosts: List[Host] = field(default_factory=list)
    issues: List[HealthIssue] = field(default_factory=list)
    metrics: List[MetricPoint] = field(default_factory=list)
    network_health: Optional[NetworkHealth] = None
    source: SourceType = "netcon"
    ok: bool = True
    error: Optional[str] = None
    # 单个端点失败但其余成功时的细粒度错误（端点 -> 错误摘要）
    errors: Dict[str, str] = field(default_factory=dict)
    collected_at: float = field(default_factory=time.time)
    # 主机列表本轮的采集时间（宿主机 epoch）：用于证明 /api/hosts 新鲜度，
    # 检测引擎也用它作为 host_down / 恢复判定的 observed_at。
    hosts_observed_at: Optional[float] = None

    @classmethod
    def unavailable(cls, source: SourceType, error: str) -> "CollectResult":
        """数据源不可用时的空结果（优雅降级，服务继续运行）。"""
        return cls(
            devices=[],
            topology=Topology(nodes=[], links=[], captured_at=time.time(), source=source),
            hosts=[],
            issues=[],
            metrics=[],
            source=source,
            ok=False,
            error=error,
            collected_at=time.time(),
        )


@runtime_checkable
class Driver(Protocol):
    """采集驱动协议（netcon / simulator 都实现它）。"""

    name: str

    async def probe(self) -> bool:
        """轻量探测数据源是否可用；任何异常都在内部捕获，返回 False。"""
        ...

    async def collect(self) -> CollectResult:
        """采集一轮全量快照；错误降级为空数据，不抛异常。"""
        ...

    async def close(self) -> None:
        """释放底层资源（HTTP 连接等）。"""
        ...


class MetricGenerator(Protocol):
    """指标生成器协议。

    PT 的 REST API 不暴露接口流量计数器，指标只能由行为仿真引擎生成。
    该协议与 :class:`Driver` 分开，是因为指标生成器需要“跟随当前拓扑”生成，
    而不是自己决定拓扑；且所有输出必须 ``source="simulator"``。
    """

    name: str

    def generate_metrics(
        self,
        devices: List[Device],
        topology: Topology,
        at: Optional[float] = None,
    ) -> List[MetricPoint]:
        """按当前设备/拓扑生成一个时间片的指标点列表。"""
        ...
