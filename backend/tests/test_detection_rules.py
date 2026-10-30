# -*- coding: utf-8 -*-
"""W3 检测规则单测（完全离线）。

覆盖需求 B 的四类自建规则与恢复判定：
- host_down（pingStatus=FAILURE → critical；恢复 SUCCESS → cleared）
- device_unreachable（reachabilityStatus=Unreachable；恢复 → cleared）
- link_down（linkStatus up→down；恢复/链路消失/新增链路）
- health_issue（NC issue 新增；issue 消失 → cleared；**issue 仍在但 host/device 已恢复
  → stale_source=True 的 cleared**）
- device_offline/online（保留 W2 能力）
- 时间基准：observed_at=宿主机采集时间，source_ts=NC 原始时间
"""
from __future__ import annotations

from app.detection.rules import DetectionConfig, DetectionEngine
from app.models import Alert, is_ping_failure
from tests.helpers import (
    OBSERVED_AT,
    SIM_CLOCK_TS,
    evaluate,
    make_device,
    make_host,
    make_issue,
    make_link,
    make_result,
)


def active_of(*alerts: Alert):
    return list(alerts)


class TestRuleCatalog:
    def test_rules_are_structured_catalog(self):
        engine = DetectionEngine()
        catalog = engine.rule_catalog()
        ids = {item["rule_id"] for item in catalog}
        for expected in (
            "host_down",
            "device_unreachable",
            "link_down",
            "health_issue",
            "device_offline",
        ):
            assert expected in ids
        for item in catalog:
            assert item["source"] and item["trigger"] and item["recovery"]
            assert item["severity"] in ("critical", "warning", "info", "critical/warning")

    def test_config_defaults(self):
        config = DetectionConfig()
        assert config.host_down_severity == "critical"
        assert config.link_down_severity == "warning"


class TestHostDownRule:
    def test_ping_failure_creates_critical_alert_with_time_base(self):
        result = make_result(
            devices=[make_device("sw1")],
            hosts=[
                make_host(
                    "PC-JX1",
                    hostname="PC-JX1",
                    ping_status="FAILURE",
                    last_updated="2026-10-29 21:33:14",
                )
            ],
        )
        outcome = evaluate(result)
        assert len(outcome.alerts) == 1
        alert = outcome.alerts[0]
        assert alert.alert_type == "host_down"
        assert alert.severity == "critical"
        assert alert.dedup_key == "host_down:PC-JX1"
        assert alert.state == "active"
        # 时间基准：observed_at 用宿主机采集时间；source_ts 保留 NC 原始时间
        assert alert.observed_at == OBSERVED_AT
        assert alert.created_at == OBSERVED_AT
        assert alert.source_ts == "2026-10-29 21:33:14"
        assert alert.device_id == "sw1"

    def test_active_alert_not_duplicated_while_fault_persists(self):
        failing = make_result(hosts=[make_host("h1", ping_status="FAILURE")])
        first = evaluate(failing)
        assert len(first.alerts) == 1
        previous = failing
        # 第二轮仍然 FAILURE，且第一条告警仍 active → 不重复产生
        second = evaluate(
            make_result(hosts=[make_host("h1", ping_status="FAILURE")]),
            previous_result=previous,
            active_alerts=first.alerts,
        )
        assert second.alerts == []
        assert second.cleared == []

    def test_recovery_to_success_generates_cleared(self):
        failing_result = make_result(hosts=[make_host("h1", ping_status="FAILURE")])
        first = evaluate(failing_result)
        recovered_result = make_result(hosts=[make_host("h1", ping_status="SUCCESS")])
        outcome = evaluate(
            recovered_result,
            previous_result=failing_result,
            active_alerts=first.alerts,
        )
        assert outcome.alerts == []
        assert len(outcome.cleared) == 1
        cleared = outcome.cleared[0]
        assert cleared.dedup_key == "host_down:h1"
        assert cleared.alert_type == "host_down"
        assert cleared.stale_source is False
        assert "SUCCESS" in cleared.detail

    def test_hosts_endpoint_failure_does_not_mass_resolve(self):
        failing = make_result(hosts=[make_host("h1", ping_status="FAILURE")])
        first = evaluate(failing)
        # /host 端点失败且无数据：不能误判为全部恢复
        broken = make_result(hosts=[], errors={"hosts": "/host -> HTTP 500"})
        outcome = evaluate(broken, previous_result=failing, active_alerts=first.alerts)
        assert outcome.cleared == []

    def test_ping_status_helpers(self):
        assert is_ping_failure("FAILURE") is True
        assert is_ping_failure("FAIL") is True
        assert is_ping_failure("SUCCESS") is False
        assert is_ping_failure(None) is False


class TestDeviceUnreachableRule:
    def test_unreachable_device_creates_alert(self):
        result = make_result(
            devices=[make_device("sw1", hostname="SW-JX-2960", reachability="Unreachable")],
        )
        outcome = evaluate(result)
        assert len(outcome.alerts) == 1
        alert = outcome.alerts[0]
        assert alert.alert_type == "device_unreachable"
        assert alert.severity == "critical"
        assert alert.dedup_key == "device_unreachable:sw1"
        assert alert.device_id == "sw1"

    def test_wrong_credential_is_warning(self):
        result = make_result(
            devices=[
                make_device(
                    "sw1",
                    reachability="Reachable",
                    collection_status="Wrong credential",
                )
            ]
        )
        outcome = evaluate(result)
        assert len(outcome.alerts) == 1
        assert outcome.alerts[0].severity == "warning"

    def test_device_recovery_generates_cleared(self):
        unreachable = make_result(
            devices=[make_device("sw1", reachability="Unreachable")]
        )
        first = evaluate(unreachable)
        recovered = make_result(devices=[make_device("sw1", reachability="Reachable")])
        outcome = evaluate(
            recovered, previous_result=unreachable, active_alerts=first.alerts
        )
        assert outcome.alerts == []
        assert len(outcome.cleared) == 1
        assert outcome.cleared[0].alert_type == "device_unreachable"
        assert outcome.cleared[0].stale_source is False


class TestLinkDownRule:
    def test_link_status_change_creates_warning_alert(self):
        previous = make_result(
            devices=[make_device("d1"), make_device("d2")],
            links=[make_link("l1", status="up")],
        )
        current = make_result(
            devices=[make_device("d1"), make_device("d2")],
            links=[make_link("l1", status="down")],
        )
        outcome = evaluate(current, previous_result=previous)
        assert len(outcome.alerts) == 1
        alert = outcome.alerts[0]
        assert alert.alert_type == "link_down"
        assert alert.severity == "warning"
        assert alert.link_id == "l1"
        assert alert.dedup_key == "link_down:l1"

    def test_link_recovery_generates_cleared(self):
        previous = make_result(
            devices=[make_device("d1"), make_device("d2")],
            links=[make_link("l1", status="up")],
        )
        broken = make_result(
            devices=[make_device("d1"), make_device("d2")],
            links=[make_link("l1", status="down")],
        )
        first = evaluate(broken, previous_result=previous)
        restored = make_result(
            devices=[make_device("d1"), make_device("d2")],
            links=[make_link("l1", status="up")],
        )
        outcome = evaluate(restored, previous_result=broken, active_alerts=first.alerts)
        assert outcome.alerts == []
        assert len(outcome.cleared) == 1
        assert outcome.cleared[0].alert_type == "link_down"

    def test_link_removed_from_topology_counts_as_down(self):
        previous = make_result(
            devices=[make_device("d1"), make_device("d2")],
            links=[make_link("l1", status="up")],
        )
        current = make_result(devices=[make_device("d1"), make_device("d2")], links=[])
        outcome = evaluate(current, previous_result=previous)
        assert len(outcome.alerts) == 1
        assert outcome.alerts[0].alert_type == "link_down"
        assert outcome.alerts[0].link_id == "l1"

    def test_new_link_is_info_event(self):
        previous = make_result(devices=[make_device("d1"), make_device("d2")], links=[])
        current = make_result(
            devices=[make_device("d1"), make_device("d2")],
            links=[make_link("l1", status="up")],
        )
        outcome = evaluate(current, previous_result=previous)
        assert len(outcome.alerts) == 1
        alert = outcome.alerts[0]
        assert alert.alert_type == "link_up"
        assert alert.state == "info"

    def test_topology_endpoint_failure_does_not_alert(self):
        previous = make_result(
            devices=[make_device("d1"), make_device("d2")],
            links=[make_link("l1", status="up")],
        )
        broken = make_result(
            devices=[make_device("d1"), make_device("d2")],
            links=[],
            errors={"topology": "/topology -> HTTP 500"},
        )
        outcome = evaluate(broken, previous_result=previous)
        assert outcome.alerts == []
        assert outcome.cleared == []


class TestDevicePresenceRule:
    def test_device_offline_and_recovered(self):
        previous = make_result(devices=[make_device("d1"), make_device("d2")])
        current = make_result(devices=[make_device("d1")])
        first = evaluate(current, previous_result=previous)
        assert len(first.alerts) == 1
        assert first.alerts[0].alert_type == "device_offline"
        assert first.alerts[0].severity == "critical"

        back = make_result(devices=[make_device("d1"), make_device("d2")])
        outcome = evaluate(back, previous_result=current, active_alerts=first.alerts)
        # 设备重新出现：active 的 device_offline 被 cleared，同时产生一次性 device_online info
        assert outcome.active_alerts == []
        assert any(
            alert.alert_type == "device_online" and alert.state == "info"
            for alert in outcome.alerts
        )
        assert len(outcome.cleared) == 1
        assert outcome.cleared[0].alert_type == "device_offline"

    def test_new_device_is_info_event(self):
        previous = make_result(devices=[make_device("d1")])
        current = make_result(devices=[make_device("d1"), make_device("d2")])
        outcome = evaluate(current, previous_result=previous)
        assert len(outcome.alerts) == 1
        assert outcome.alerts[0].alert_type == "device_online"
        assert outcome.alerts[0].state == "info"


class TestHealthIssueRule:
    def test_new_issue_creates_alert_with_both_timestamps(self):
        issue = make_issue(
            "NC-ISSUE-1",
            name="Connectivity",
            description="Device is down",
            issue_source="PC-JX1",
            source_ts=SIM_CLOCK_TS,
        )
        result = make_result(hosts=[make_host("PC-JX1", hostname="PC-JX1")], issues=[issue])
        outcome = evaluate(result)
        assert len(outcome.alerts) == 1
        alert = outcome.alerts[0]
        assert alert.alert_type == "health_issue"
        assert alert.issue_id == "NC-ISSUE-1"
        assert alert.observed_at == OBSERVED_AT  # 宿主机采集时间
        assert alert.source_ts == SIM_CLOCK_TS  # NC 仿真时钟原始值（审计用）

    def test_issue_disappearing_generates_normal_cleared(self):
        issue = make_issue("i-1", issue_source="d1")
        previous = make_result(devices=[make_device("d1")], issues=[issue])
        first = evaluate(previous)
        gone = make_result(devices=[make_device("d1")], issues=[])
        outcome = evaluate(gone, previous_result=previous, active_alerts=first.alerts)
        assert len(outcome.cleared) == 1
        assert outcome.cleared[0].alert_type == "health_issue"
        assert outcome.cleared[0].stale_source is False

    def test_issue_lingering_but_host_recovered_clears_as_stale_source(self):
        """核心场景（故障注入实验）：NC issue 超 3 分钟不清，但 host 已 SUCCESS。"""
        issue = make_issue(
            "NC-ISSUE-JX1",
            name="Connectivity",
            description="Device is down",
            issue_source="PC-JX1",
            source_ts=SIM_CLOCK_TS,
        )
        # 第一轮：host FAILURE + NC issue 同时存在（故障端）
        fault = make_result(
            hosts=[make_host("PC-JX1", hostname="PC-JX1", ping_status="FAILURE")],
            issues=[issue],
        )
        first = evaluate(fault)
        alert_types = {alert.alert_type for alert in first.alerts}
        assert alert_types == {"host_down", "health_issue"}

        # 第二轮：host 已 SUCCESS，但 NC issue 仍在（恢复端 NC 滞后）
        recovered = make_result(
            hosts=[make_host("PC-JX1", hostname="PC-JX1", ping_status="SUCCESS")],
            issues=[issue],
        )
        outcome = evaluate(
            recovered, previous_result=fault, active_alerts=first.alerts
        )
        # host_down 恢复为普通 cleared；health_issue 为 stale_source cleared
        assert outcome.alerts == []
        cleared_by_type = {event.alert_type: event for event in outcome.cleared}
        assert set(cleared_by_type) == {"host_down", "health_issue"}

        issue_cleared = cleared_by_type["health_issue"]
        assert issue_cleared.stale_source is True
        assert issue_cleared.issue_id == "NC-ISSUE-JX1"
        assert "NC 问题记录滞后，数据源已恢复" in issue_cleared.detail
        assert "仍存在" in issue_cleared.detail
        assert issue_cleared.source_ts == SIM_CLOCK_TS

        # issue 被标记 stale_source（snapshot/API 可见）
        assert outcome.stale_issue_ids == {"NC-ISSUE-JX1"}

    def test_issue_matching_by_description_token(self):
        """issueSource 是笼统值（Connectivity）时，从描述里找目标主机名。"""
        issue = make_issue(
            "i-token",
            name="Connectivity",
            description="Device PC-JX1 is down",
            issue_source="Connectivity",
        )
        fault = make_result(
            hosts=[make_host("PC-JX1", hostname="PC-JX1", ping_status="FAILURE")],
            issues=[issue],
        )
        first = evaluate(fault)
        recovered = make_result(
            hosts=[make_host("PC-JX1", hostname="PC-JX1", ping_status="SUCCESS")],
            issues=[issue],
        )
        outcome = evaluate(recovered, previous_result=fault, active_alerts=first.alerts)
        stale = [event for event in outcome.cleared if event.alert_type == "health_issue"]
        assert len(stale) == 1
        assert stale[0].stale_source is True

    def test_stale_source_suppression_prevents_flapping(self):
        """issue 仍存在 + host 已恢复：恢复后继续轮询不得重新告警（防 flap）。"""
        issue = make_issue("i-flap", issue_source="PC-JX1")
        fault = make_result(
            hosts=[make_host("PC-JX1", hostname="PC-JX1", ping_status="FAILURE")],
            issues=[issue],
        )
        first = evaluate(fault)
        recovered = make_result(
            hosts=[make_host("PC-JX1", hostname="PC-JX1", ping_status="SUCCESS")],
            issues=[issue],
        )
        second = evaluate(recovered, previous_result=fault, active_alerts=first.alerts)
        stale_cleared = [
            event for event in second.cleared if event.alert_type == "health_issue"
        ]
        assert len(stale_cleared) == 1 and stale_cleared[0].stale_source is True

        # 下一轮：issue 仍在，但 suppressed_keys 让它不再重新告警
        third = evaluate(
            recovered,
            previous_result=recovered,
            active_alerts=[],
            engine=None,
        )
        # 说明：不带 violated suppression 时会重新告警（这是需要抑制的场景）
        assert third.alerts  # 未抑制时会新告警，证明场景成立
        engine = DetectionEngine()
        from app.pipeline import PipelineState

        suppressed = engine.evaluate(
            PipelineState.from_result(recovered),
            recovered,
            active_alerts=[],
            suppressed_keys={"health_issue:i-flap"},
        )
        assert suppressed.alerts == []
        assert suppressed.cleared == []

    def test_issue_with_unmapped_target_stays_active(self):
        issue = make_issue("i-unknown", issue_source="UNKNOWN-NODE")
        source = make_result(hosts=[make_host("h1")], issues=[issue])
        first = evaluate(source)
        same = make_result(hosts=[make_host("h1")], issues=[issue])
        outcome = evaluate(same, previous_result=source, active_alerts=first.alerts)
        assert outcome.cleared == []
        assert outcome.stale_issue_ids == set()

    def test_issue_endpoint_failure_does_not_mass_clear(self):
        issue = make_issue("i-1", issue_source="h1")
        present = make_result(hosts=[make_host("h1")], issues=[issue])
        first = evaluate(present)
        broken = make_result(
            hosts=[make_host("h1")],
            issues=[],
            errors={"issues": "/assurance/health-issues -> HTTP 500"},
        )
        outcome = evaluate(broken, previous_result=present, active_alerts=first.alerts)
        assert outcome.cleared == []


class TestMultiRuleScenario:
    def test_one_poll_multiple_faults_and_recovery_aggregates(self):
        """一次故障/恢复涉及多类实体时，恢复事件统一交给 pipeline 聚合。"""
        issue = make_issue("i-1", issue_source="PC-JX1")
        fault = make_result(
            devices=[make_device("sw1", reachability="Unreachable")],
            hosts=[make_host("PC-JX1", hostname="PC-JX1", ping_status="FAILURE")],
            links=[make_link("l1", source="sw1", target="d2", status="down")],
            issues=[issue],
        )
        first = evaluate(fault)
        types = {alert.alert_type for alert in first.alerts}
        assert {"device_unreachable", "host_down", "link_down", "health_issue"} <= types

        recovered = make_result(
            devices=[make_device("sw1", reachability="Reachable")],
            hosts=[make_host("PC-JX1", hostname="PC-JX1", ping_status="SUCCESS")],
            links=[make_link("l1", source="sw1", target="d2", status="up")],
            issues=[issue],
        )
        outcome = evaluate(
            recovered, previous_result=fault, active_alerts=first.alerts
        )
        assert outcome.alerts == []
        cleared_types = {event.alert_type for event in outcome.cleared}
        assert {"device_unreachable", "host_down", "link_down", "health_issue"} <= cleared_types
        summary = outcome.cleared_summary()
        assert summary is not None
        assert summary["count"] == len(outcome.cleared)
        assert summary["stale_source_count"] == 1
        assert "NC 问题记录滞后" in summary["summary"]
