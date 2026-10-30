# -*- coding: utf-8 -*-
"""W3 检测引擎：自建规则 + 恢复判定 + 告警降噪基础。

为什么自建规则而不是只信 NC issue？
=================================
2026-10-29 故障注入实测：PT NC 的 ``/assurance/health-issues`` 清除有 **>3 分钟延迟**
（host ping 早已 SUCCESS、健康度回 100%，issue 仍在），只依赖 NC issue 生命周期会
导致告警永远不消失。本引擎直接基于 NC 的**原始数据源状态**做判定：

规则清单（结构化，可单测）：:

    host_down             host.ping_status == FAILURE                  critical
    device_unreachable    device.reachabilityStatus == Unreachable      critical
    device_credential     collectionStatus in (Wrong credential, ...)   warning
    link_down             link.link_status == down / 链路从拓扑消失     warning
    device_offline        设备从采集结果消失（端点未失败时）            critical
    health_issue          NC 原生 issue 新增（保留 NC 视角）            critical/warning
    device_online/link_up 一次性信息事件                                info

恢复判定（不依赖 NC issue 清除）：
- host_down：host pingStatus 恢复 SUCCESS → cleared；
- device_unreachable / device_credential：设备恢复可达 → cleared；
- link_down：链路状态恢复 up / 链路重新出现 → cleared；
- device_offline：设备重新出现 → cleared；
- health_issue：
  1) NC issue 从列表消失 → cleared；
  2) **NC issue 仍存在，但 issue 指向的 host/device 实际状态已恢复** →
     cleared 且 ``stale_source=True``，detail 注明“NC 问题记录滞后，数据源已恢复”。

降噪语义：
- 引擎只输出“新的 active 告警”与“本轮恢复事件”；同一 ``dedup_key`` 的 active 告警
  由 pipeline/store 保证只保留一条（重复出现不再入库/推送）；
- 恢复事件不逐条生成 info 告警行，而由 pipeline 聚合成一条 ``alerts_cleared`` 摘要。

时间基准：
- 所有输出告警的 ``observed_at`` 使用宿主机采集时间 ``current.collected_at``；
- NC 原始时间（issueTimestamp / lastUpdated）写入 ``source_ts`` 供审计；
  PT 仿真时钟与宿主机实测差约 10 分钟，两者不混用。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Set

from app.collectors.base import CollectResult
from app.models import (
    Alert,
    Device,
    HealthIssue,
    Host,
    Link,
    is_ping_failure,
    is_unreachable,
)

logger = logging.getLogger("campus_twin.detection")

# 规则目录（/api/detection/rules 与 README 共用，保证“规则结构化、可核对”）
RULE_CATALOG: List[Dict[str, Any]] = [
    {
        "rule_id": "host_down",
        "source": "NC GET /host -> pingStatus",
        "trigger": "ping_status == FAILURE",
        "severity": "critical",
        "recovery": "pingStatus 恢复 SUCCESS（不依赖 NC issue 清除）",
        "dedup_key": "host_down:<host_id>",
    },
    {
        "rule_id": "device_unreachable",
        "source": "NC GET /network-device -> reachabilityStatus",
        "trigger": "reachabilityStatus == Unreachable",
        "severity": "critical",
        "recovery": "reachabilityStatus 恢复可达",
        "dedup_key": "device_unreachable:<device_id>",
    },
    {
        "rule_id": "device_credential",
        "source": "NC GET /network-device -> collectionStatus",
        "trigger": "collectionStatus == Wrong credential / Unreachable",
        "severity": "warning",
        "recovery": "collectionStatus 恢复正常",
        "dedup_key": "device_unreachable:<device_id>",
    },
    {
        "rule_id": "link_down",
        "source": "NC GET /topology/physical-topology -> linkStatus",
        "trigger": "linkStatus == down 或链路从拓扑消失",
        "severity": "warning",
        "recovery": "linkStatus 恢复 up / 链路重新出现",
        "dedup_key": "link_down:<link_id>",
    },
    {
        "rule_id": "device_offline",
        "source": "NC GET /network-device（设备列表变化）",
        "trigger": "上一轮存在的设备本轮消失（设备端点未报错）",
        "severity": "critical",
        "recovery": "设备重新出现在采集结果",
        "dedup_key": "device_offline:<device_id>",
    },
    {
        "rule_id": "health_issue",
        "source": "NC GET /assurance/health-issues",
        "trigger": "出现新的 NC 原生健康问题",
        "severity": "critical/warning",
        "recovery": "issue 消失；或 issue 仍在但 host/device 实际状态已恢复（stale_source=True）",
        "dedup_key": "health_issue:<issue_id>",
    },
    {
        "rule_id": "device_online / link_up",
        "source": "NC GET /network-device、/topology",
        "trigger": "新增设备 / 新增链路",
        "severity": "info",
        "recovery": "一次性事件（state=info，无 active 生命周期）",
        "dedup_key": "device_online:<id> / link_up:<id>",
    },
]

# 判断 collectionStatus 是否代表采集/凭证异常
_CREDENTIAL_STATUS = ("wrong credential", "unreachable", "wrong credentials")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{1,}")


class PreviousState(Protocol):
    """检测引擎需要的“上一轮状态”结构（避免与 pipeline 循环 import）。"""

    devices: Dict[str, Device]
    hosts: Dict[str, Host]
    links: Dict[str, Link]
    issues: Dict[str, HealthIssue]


@dataclass
class DetectionConfig:
    """规则阈值/级别配置（默认值即生产配置，测试可覆盖）。"""

    host_down_severity: str = "critical"
    device_unreachable_severity: str = "critical"
    device_credential_severity: str = "warning"
    link_down_severity: str = "warning"
    max_alerts_per_poll: int = 200
    # 单条 issue detail 最大长度（防止 NC 描述过长写爆告警列表）
    max_detail_length: int = 500


@dataclass
class ClearedEvent:
    """一条告警的恢复事件（pipeline 会把它聚合为一条摘要，不逐条入库）。"""

    dedup_key: str
    alert_type: str
    title: str
    detail: str
    observed_at: float
    severity: str = "info"
    device_id: Optional[str] = None
    link_id: Optional[str] = None
    issue_id: Optional[str] = None
    source_ts: Optional[str] = None
    # True = NC 记录滞后，实际数据源已恢复
    stale_source: bool = False

    def summary_item(self) -> Dict[str, Any]:
        return {
            "dedup_key": self.dedup_key,
            "alert_type": self.alert_type,
            "title": self.title,
            "device_id": self.device_id,
            "link_id": self.link_id,
            "issue_id": self.issue_id,
            "observed_at": self.observed_at,
            "source_ts": self.source_ts,
            "stale_source": self.stale_source,
        }


@dataclass
class DetectionOutcome:
    """一轮检测结果：新告警 + 恢复事件 + 被标记 stale_source 的 issue id。"""

    alerts: List[Alert] = field(default_factory=list)
    cleared: List[ClearedEvent] = field(default_factory=list)
    stale_issue_ids: Set[str] = field(default_factory=set)
    baseline: bool = False

    @property
    def active_alerts(self) -> List[Alert]:
        return [alert for alert in self.alerts if alert.state == "active"]

    @property
    def info_events(self) -> List[Alert]:
        return [alert for alert in self.alerts if alert.state == "info"]

    def cleared_summary(self) -> Optional[Dict[str, Any]]:
        """把本轮多条恢复事件聚合成一条摘要（解决 11 条集中清除的噪声）。"""
        if not self.cleared:
            return None
        count = len(self.cleared)
        stale_count = sum(1 for event in self.cleared if event.stale_source)
        if stale_count:
            summary = (
                "本轮清除 {} 条告警（其中 {} 条因 NC 问题记录滞后，"
                "已按 host/device 实际状态判定恢复）"
            ).format(count, stale_count)
        else:
            summary = "本轮清除 {} 条告警".format(count)
        latest = max(event.observed_at for event in self.cleared)
        return {
            "count": count,
            "stale_source_count": stale_count,
            "summary": summary,
            "observed_at": latest,
            "items": [event.summary_item() for event in self.cleared[:20]],
        }


class DetectionEngine:
    """基于 NC 原始状态的自建规则检测引擎（无状态；状态由 pipeline/store 提供）。"""

    name = "self-rules-v1"

    def __init__(self, config: Optional[DetectionConfig] = None) -> None:
        self.config = config or DetectionConfig()

    # ------------------------------------------------------------------ #
    # 对外主入口
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        previous: Optional[PreviousState],
        current: CollectResult,
        active_alerts: Iterable[Alert] = (),
        baseline: bool = False,
        suppressed_keys: Iterable[str] = (),
    ) -> DetectionOutcome:
        """对一轮采集结果执行全部规则。

        - ``previous``：上一轮成功采集的状态（首轮为 None）；
        - ``current``：本轮采集结果；
        - ``active_alerts``：当前仍在 active 的告警（来自 store，用于去重与恢复判定）；
        - ``baseline``：是否首轮基线（仅作为标记，是否广播由 pipeline 决定）；
        - ``suppressed_keys``：已按 stale_source 恢复、但 NC 记录仍在的 dedup_key，
          用于抑制 NC issue 持续存在导致的反复告警。
        """
        observed_at = float(current.collected_at or time.time())
        active = self._index_active(active_alerts)
        emitted: Set[str] = set()
        outcome = DetectionOutcome(baseline=baseline)

        self._rule_hosts(outcome, previous, current, active, emitted, observed_at)
        self._rule_devices(outcome, current, active, emitted, observed_at)
        self._rule_links(outcome, previous, current, active, emitted, observed_at)
        self._rule_device_presence(outcome, previous, current, active, emitted, observed_at)
        suppressed = set(suppressed_keys)
        self._rule_health_issues(
            outcome, current, active, emitted, observed_at, suppressed
        )

        if len(outcome.alerts) > self.config.max_alerts_per_poll:
            logger.warning(
                "单轮检测到 %d 条新告警，截断为 %d 条",
                len(outcome.alerts),
                self.config.max_alerts_per_poll,
            )
            outcome.alerts = outcome.alerts[: self.config.max_alerts_per_poll]
        if outcome.cleared:
            logger.info(
                "检测到 %d 条告警恢复（其中 %d 条 stale_source）",
                len(outcome.cleared),
                sum(1 for event in outcome.cleared if event.stale_source),
            )
        return outcome

    def rule_catalog(self) -> List[Dict[str, Any]]:
        """规则目录（供 /api/detection/rules 与文档展示）。"""
        return [dict(item) for item in RULE_CATALOG]

    # ------------------------------------------------------------------ #
    # 规则实现
    # ------------------------------------------------------------------ #
    def _rule_hosts(
        self,
        outcome: DetectionOutcome,
        previous: Optional[PreviousState],
        current: CollectResult,
        active: Dict[str, Alert],
        emitted: Set[str],
        observed_at: float,
    ) -> None:
        """host_down：NC /host 的 pingStatus 直接判定，恢复即清除。"""
        # /host 端点失败且无数据：本轮不能判断主机状态，跳过（避免误报全部恢复）
        if "hosts" in (current.errors or {}) and not current.hosts:
            logger.debug("本轮 /host 采集失败，跳过 host_down 规则")
            return

        current_hosts = {host.id: host for host in current.hosts}
        previous_hosts: Dict[str, Host] = getattr(previous, "hosts", {}) if previous else {}
        for host in current.hosts:
            if not is_ping_failure(host.ping_status):
                continue
            key = self._key("host_down", host.id)
            if key in active or key in emitted:
                continue
            name = host.hostname or host.ip or host.id
            self._emit(
                outcome,
                emitted,
                self._alert(
                    rule_id="host_down",
                    severity=self.config.host_down_severity,
                    title="主机离线：{}".format(name),
                    detail="NC /host pingStatus={}；主机 {}({}) 与网络失联{}".format(
                        host.ping_status,
                        name,
                        host.ip or "无 IP",
                        "（接入交换机 {} {}）".format(
                            host.connected_device_name, host.connected_interface
                        )
                        if host.connected_device_name
                        else "",
                    ),
                    dedup_key=key,
                    observed_at=observed_at,
                    source_ts=_str_or_none(host.last_updated),
                    # 告警尽量关联到接入设备 id：优先 connectedDeviceId，
                    # 否则用 connectedNetworkDeviceName 在当前设备列表里反查。
                    device_id=host.connected_device_id
                    or self._device_id_by_name(host.connected_device_name, current.devices),
                ),
            )

        # 恢复：active 的 host_down 告警，对应主机本轮 pingStatus 已非 FAILURE
        for key, incident in active.items():
            if not key.startswith("host_down:"):
                continue
            host_id = key.split(":", 1)[1]
            host = current_hosts.get(host_id)
            if host is None or is_ping_failure(host.ping_status):
                continue
            if not host.ping_status:  # 无明确状态时不做恢复判定
                continue
            previous_status = (
                previous_hosts[host_id].ping_status
                if host_id in previous_hosts
                else incident.title
            )
            outcome.cleared.append(
                ClearedEvent(
                    dedup_key=key,
                    alert_type="host_down",
                    title="主机恢复：{}".format(host.hostname or host.ip or host_id),
                    detail="pingStatus {} -> {}（按数据源实际状态判定恢复，不等 NC issue 清除）".format(
                        previous_status or "FAILURE", host.ping_status
                    ),
                    observed_at=observed_at,
                    device_id=incident.device_id or host.connected_device_id,
                )
            )

    def _rule_devices(
        self,
        outcome: DetectionOutcome,
        current: CollectResult,
        active: Dict[str, Alert],
        emitted: Set[str],
        observed_at: float,
    ) -> None:
        """device_unreachable / device_credential：基于 reachabilityStatus + collectionStatus。"""
        if "devices" in (current.errors or {}) and not current.devices:
            logger.debug("本轮 /network-device 采集失败，跳过设备可达性规则")
            return

        current_devices = {device.id: device for device in current.devices}
        for device in current.devices:
            unreachable = is_unreachable(device.reachability)
            credential_issue = self._has_credential_issue(device)
            if not unreachable and not credential_issue:
                continue
            key = self._key("device_unreachable", device.id)
            if key in active or key in emitted:
                continue
            name = device.hostname or device.management_ip or device.id
            severity = (
                self.config.device_unreachable_severity
                if unreachable
                else self.config.device_credential_severity
            )
            title = (
                "设备不可达：{}".format(name)
                if unreachable
                else "设备采集异常：{}".format(name)
            )
            self._emit(
                outcome,
                emitted,
                self._alert(
                    rule_id="device_unreachable",
                    severity=severity,
                    title=title,
                    detail="reachabilityStatus={}，collectionStatus={}（{}）".format(
                        device.reachability or "unknown",
                        device.collection_status or "unknown",
                        device.management_ip or "无管理 IP",
                    ),
                    dedup_key=key,
                    observed_at=observed_at,
                    source_ts=_str_or_none(device.last_updated),
                    device_id=device.id,
                ),
            )

        # 恢复：设备重新可达/采集正常
        for key, incident in active.items():
            if not key.startswith("device_unreachable:"):
                continue
            device_id = key.split(":", 1)[1]
            device = current_devices.get(device_id)
            if device is None:
                continue
            if is_unreachable(device.reachability) or self._has_credential_issue(device):
                continue
            outcome.cleared.append(
                ClearedEvent(
                    dedup_key=key,
                    alert_type="device_unreachable",
                    title="设备恢复可达：{}".format(
                        device.hostname or device.management_ip or device_id
                    ),
                    detail="reachabilityStatus={}，collectionStatus={}（数据源实际状态已恢复）".format(
                        device.reachability or "unknown",
                        device.collection_status or "unknown",
                    ),
                    observed_at=observed_at,
                    device_id=device_id,
                )
            )

    def _rule_links(
        self,
        outcome: DetectionOutcome,
        previous: Optional[PreviousState],
        current: CollectResult,
        active: Dict[str, Alert],
        emitted: Set[str],
        observed_at: float,
    ) -> None:
        """link_down：linkStatus down / 链路从拓扑消失；恢复 up / 重新出现。"""
        if "topology" in (current.errors or {}) and not current.topology.links:
            logger.debug("本轮 topology 采集失败，跳过 link_down 规则")
            return

        current_links = {link.id: link for link in current.topology.links}
        for link in current.topology.links:
            if not _is_link_down(link.link_status):
                continue
            key = self._key("link_down", link.id)
            if key in active or key in emitted:
                continue
            self._emit(
                outcome,
                emitted,
                self._alert(
                    rule_id="link_down",
                    severity=self.config.link_down_severity,
                    title="链路故障：{} <-> {}".format(link.source, link.target),
                    detail="linkStatus={}（{}:{} <-> {}:{}）".format(
                        link.link_status,
                        link.source,
                        link.start_port_name or "?",
                        link.target,
                        link.end_port_name or "?",
                    ),
                    dedup_key=key,
                    observed_at=observed_at,
                    link_id=link.id,
                ),
            )

        # 恢复：链路重新 up
        for key, incident in active.items():
            if not key.startswith("link_down:"):
                continue
            link_id = key.split(":", 1)[1]
            link = current_links.get(link_id)
            if link is None or _is_link_down(link.link_status):
                continue
            outcome.cleared.append(
                ClearedEvent(
                    dedup_key=key,
                    alert_type="link_down",
                    title="链路恢复：{} <-> {}".format(link.source, link.target),
                    detail="linkStatus 恢复 {}".format(link.link_status or "up"),
                    observed_at=observed_at,
                    link_id=link_id,
                    device_id=incident.device_id,
                )
            )

        if previous is None:
            return
        previous_links = getattr(previous, "links", {}) or {}
        # 链路从拓扑中消失：按中断处理（若已有 active 则不重复）
        for link_id in sorted(set(previous_links) - set(current_links)):
            key = self._key("link_down", link_id)
            if key in active or key in emitted:
                continue
            old_link = previous_links[link_id]
            self._emit(
                outcome,
                emitted,
                self._alert(
                    rule_id="link_down",
                    severity=self.config.link_down_severity,
                    title="链路中断：{} <-> {}".format(old_link.source, old_link.target),
                    detail="链路 {} 从拓扑中消失（{}:{} <-> {}:{}）".format(
                        link_id,
                        old_link.source,
                        old_link.start_port_name or "?",
                        old_link.target,
                        old_link.end_port_name or "?",
                    ),
                    dedup_key=key,
                    observed_at=observed_at,
                    link_id=link_id,
                ),
            )
        # 新增链路：一次性 info 事件
        for link_id in sorted(set(current_links) - set(previous_links)):
            key = self._key("link_up", link_id)
            if key in emitted:
                continue
            link = current_links[link_id]
            self._emit(
                outcome,
                emitted,
                self._alert(
                    rule_id="link_up",
                    severity="info",
                    title="新增链路：{} <-> {}".format(link.source, link.target),
                    detail="拓扑新增链路 {}（{}:{} <-> {}:{}）".format(
                        link_id,
                        link.source,
                        link.start_port_name or "?",
                        link.target,
                        link.end_port_name or "?",
                    ),
                    dedup_key=key,
                    observed_at=observed_at,
                    link_id=link_id,
                    state="info",
                ),
            )

    def _rule_device_presence(
        self,
        outcome: DetectionOutcome,
        previous: Optional[PreviousState],
        current: CollectResult,
        active: Dict[str, Alert],
        emitted: Set[str],
        observed_at: float,
    ) -> None:
        """device_offline：设备从采集结果消失；device_online 为一次性 info 事件。"""
        if "devices" in (current.errors or {}) and not current.devices:
            logger.debug("本轮 /network-device 采集失败，跳过设备存在性规则")
            return

        current_devices = {device.id: device for device in current.devices}
        if previous is not None:
            previous_devices = getattr(previous, "devices", {}) or {}
            for device_id in sorted(set(previous_devices) - set(current_devices)):
                key = self._key("device_offline", device_id)
                if key in active or key in emitted:
                    continue
                device = previous_devices[device_id]
                self._emit(
                    outcome,
                    emitted,
                    self._alert(
                        rule_id="device_offline",
                        severity="critical",
                        title="设备离线：{}".format(device.hostname or device_id),
                        detail="设备 {}（{}）从采集结果中消失".format(
                            device.hostname or device_id,
                            device.management_ip or "无管理 IP",
                        ),
                        dedup_key=key,
                        observed_at=observed_at,
                        device_id=device_id,
                    ),
                )
            for device_id in sorted(set(current_devices) - set(previous_devices)):
                key = self._key("device_online", device_id)
                if key in emitted:
                    continue
                device = current_devices[device_id]
                self._emit(
                    outcome,
                    emitted,
                    self._alert(
                        rule_id="device_online",
                        severity="info",
                        title="设备上线：{}".format(device.hostname or device_id),
                        detail="新采集到设备 {}（{}）".format(
                            device.hostname or device_id,
                            device.management_ip or "无管理 IP",
                        ),
                        dedup_key=key,
                        observed_at=observed_at,
                        device_id=device_id,
                        state="info",
                    ),
                )

        # 恢复：active 的 device_offline 告警，设备重新出现
        for key, incident in active.items():
            if not key.startswith("device_offline:"):
                continue
            device_id = key.split(":", 1)[1]
            device = current_devices.get(device_id)
            if device is None:
                continue
            outcome.cleared.append(
                ClearedEvent(
                    dedup_key=key,
                    alert_type="device_offline",
                    title="设备恢复在线：{}".format(
                        device.hostname or device.management_ip or device_id
                    ),
                    detail="设备重新出现在采集结果中",
                    observed_at=observed_at,
                    device_id=device_id,
                )
            )

    def _rule_health_issues(
        self,
        outcome: DetectionOutcome,
        current: CollectResult,
        active: Dict[str, Alert],
        emitted: Set[str],
        observed_at: float,
        suppressed_keys: Set[str],
    ) -> None:
        """health_issue：保留 NC 原生 issue；恢复判定以 host/device 实际状态为准。"""
        current_issues = {issue.issue_id: issue for issue in current.issues}

        for issue in current.issues:
            key = self._key("health_issue", issue.issue_id)
            if key in active or key in emitted:
                continue
            # 该 issue 之前已按 stale_source 判定恢复，但 NC 记录仍在：
            # 只要目标 host/device 没有再次故障（False），就不再重复告警。
            if key in suppressed_keys:
                if self._issue_target_recovered(issue, current) is not False:
                    # 已按 stale_source 恢复且 NC 记录仍滞后：抑制重复告警，但继续在
                    # 快照里标记 stale_source（前端/接口仍能看到“NC 记录滞后”）。
                    outcome.stale_issue_ids.add(issue.issue_id)
                    logger.debug(
                        "issue %s 已按 stale_source 恢复且 NC 记录仍滞后，本轮抑制重复告警",
                        issue.issue_id,
                    )
                    continue
            name = issue.issue_name or issue.issue_id
            self._emit(
                outcome,
                emitted,
                self._alert(
                    rule_id="health_issue",
                    severity=issue.severity,
                    title="网络健康问题：{}".format(name),
                    detail=(issue.issue_description or "")[: self.config.max_detail_length],
                    dedup_key=key,
                    observed_at=observed_at,
                    source_ts=issue.source_ts,
                    issue_id=issue.issue_id,
                    device_id=issue.issue_source,
                ),
            )

        # /assurance/health-issues 端点失败且无数据：本轮不能判断 issue 消失/恢复
        if "issues" in (current.errors or {}) and not current.issues:
            logger.debug("本轮 health-issues 采集失败，跳过 issue 恢复判定")
            return

        for key, incident in active.items():
            if not key.startswith("health_issue:"):
                continue
            issue_id = key.split(":", 1)[1]
            issue = current_issues.get(issue_id)
            if issue is None:
                outcome.cleared.append(
                    ClearedEvent(
                        dedup_key=key,
                        alert_type="health_issue",
                        title="健康问题已清除：{}".format(
                            incident.title.replace("网络健康问题：", "") or issue_id
                        ),
                        detail="NC 健康问题 {} 已从列表中消失".format(issue_id),
                        observed_at=observed_at,
                        issue_id=issue_id,
                        device_id=incident.device_id,
                    )
                )
                continue

            # NC issue 仍在：交叉校验 host/device 实际状态，支持 stale_source 恢复
            recovered = self._issue_target_recovered(issue, current)
            if recovered is not True:
                continue
            outcome.stale_issue_ids.add(issue_id)
            outcome.cleared.append(
                ClearedEvent(
                    dedup_key=key,
                    alert_type="health_issue",
                    title="健康问题已恢复（NC 记录滞后）：{}".format(
                        issue.issue_name or issue_id
                    ),
                    detail=(
                        "NC issue {} 仍存在，但数据源实际状态已恢复"
                        "（host pingStatus=SUCCESS / device reachable），"
                        "按数据源状态判定 cleared；source_ts={}。"
                        "NC 问题记录滞后，数据源已恢复。"
                    ).format(issue_id, issue.source_ts or "unknown"),
                    observed_at=observed_at,
                    issue_id=issue_id,
                    source_ts=issue.source_ts,
                    device_id=issue.issue_source,
                    stale_source=True,
                )
            )

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _index_active(active_alerts: Iterable[Alert]) -> Dict[str, Alert]:
        index: Dict[str, Alert] = {}
        for alert in active_alerts:
            if alert.dedup_key and alert.dedup_key not in index:
                index[alert.dedup_key] = alert
        return index

    @staticmethod
    def _key(rule_id: str, target_id: Any) -> str:
        return "{}:{}".format(rule_id, target_id)

    @staticmethod
    def _has_credential_issue(device: Device) -> bool:
        status = (device.collection_status or "").strip().lower()
        return status in _CREDENTIAL_STATUS

    def _alert(
        self,
        *,
        rule_id: str,
        severity: str,
        title: str,
        detail: str,
        dedup_key: str,
        observed_at: float,
        device_id: Optional[str] = None,
        link_id: Optional[str] = None,
        issue_id: Optional[str] = None,
        source_ts: Optional[str] = None,
        state: str = "active",
    ) -> Alert:
        """构造统一 Alert（时间基准：observed_at=宿主机时间，source_ts=NC 原始时间）。"""
        return Alert(
            alert_type=rule_id,
            severity=severity,
            title=title,
            detail=detail[: self.config.max_detail_length],
            dedup_key=dedup_key,
            device_id=device_id,
            link_id=link_id,
            issue_id=issue_id,
            created_at=observed_at,
            observed_at=observed_at,
            source_ts=source_ts,
            state=state,
            source="netcon",
        )

    @staticmethod
    def _emit(
        outcome: DetectionOutcome, emitted: Set[str], alert: Alert
    ) -> None:
        if alert.dedup_key in emitted:
            return
        emitted.add(alert.dedup_key)
        outcome.alerts.append(alert)

    # ---- NC issue -> 目标实体状态交叉校验（stale_source 恢复判定核心）---- #
    def _issue_target_recovered(
        self, issue: HealthIssue, current: CollectResult
    ) -> Optional[bool]:
        """判断 NC issue 指向的 host/device 是否已恢复。

        返回 True=已恢复 / False=仍故障 / None=无法映射到已知实体。
        """
        host_lookup = self._host_lookup(current.hosts)
        device_lookup = self._device_lookup(current.devices)

        # 候选匹配顺序：issueSource 优先，其次 issueName/description 中的 token
        candidates: List[str] = []
        source = _normalize_token(issue.issue_source)
        if source:
            candidates.append(source)
        for text in (issue.issue_name, issue.issue_description):
            for token in _tokens(text):
                key = _normalize_token(token)
                if key:
                    candidates.append(key)
        for candidate in candidates:
            host = host_lookup.get(candidate)
            if host is not None:
                if not host.ping_status:
                    return None
                return not is_ping_failure(host.ping_status)
            device = device_lookup.get(candidate)
            if device is not None:
                return not (
                    is_unreachable(device.reachability) or self._has_credential_issue(device)
                )
        return None

    @staticmethod
    def _device_id_by_name(
        name: Optional[str], devices: Sequence[Device]
    ) -> Optional[str]:
        """按设备 hostname/id 反查设备 id（host 记录只有接入设备名时用）。"""
        key = _normalize_token(name)
        if not key:
            return None
        for device in devices:
            if _normalize_token(device.hostname) == key or _normalize_token(device.id) == key:
                return device.id
        return None

    @staticmethod
    def _host_lookup(hosts: Sequence[Host]) -> Dict[str, Host]:
        lookup: Dict[str, Host] = {}
        for host in hosts:
            for value in (host.id, host.hostname, host.ip, host.mac):
                key = _normalize_token(value)
                if key and key not in lookup:
                    lookup[key] = host
        return lookup

    @staticmethod
    def _device_lookup(devices: Sequence[Device]) -> Dict[str, Device]:
        lookup: Dict[str, Device] = {}
        for device in devices:
            for value in (device.id, device.hostname, device.management_ip):
                key = _normalize_token(value)
                if key and key not in lookup:
                    lookup[key] = device
        return lookup


# --------------------------------------------------------------------------- #
# 模块级小工具
# --------------------------------------------------------------------------- #
def _is_link_down(status: Optional[str]) -> bool:
    if not status:
        return False
    return "down" in str(status).strip().lower()


def _normalize_token(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _tokens(text: Optional[str]) -> List[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(str(text))


def _str_or_none(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)
