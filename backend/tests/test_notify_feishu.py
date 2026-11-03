# -*- coding: utf-8 -*-
"""W15：飞书富卡片 / 恢复通知 / 运行时配置测试。

全部离线：用 ``httpx.MockTransport`` 截获 Webhook 请求（不联网、不依赖飞书），
pipeline 部分用 ``ScriptedDriver`` 回放「正常 → 故障 → 恢复」三轮采集。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from app.config import Settings
from app.models import Alert
from app.notify import (
    FEISHU_NOTE,
    NotifyConfigError,
    NotifyService,
    PAYLOAD_ALERT,
    PAYLOAD_RECOVERY,
    PAYLOAD_TEST,
)
from app.pipeline import CollectorPipeline
from app.store import Store
from tests.helpers import ScriptedDriver, make_host, make_result, run_async

# 固定宿主机采集时间：2026-10-30T06:00:00Z = 北京时间 2026-10-30 14:00:00
OBSERVED_AT = 1793340000.0
BEIJING_TEXT = "2026-10-30 14:00:00"
BEIJING_RECOVERY_TEXT = "2026-10-30 14:05:00"


# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #
class MockWebhook:
    """httpx.MockTransport 封装：记录请求 URL / payload，可注入 HTTP 或业务码。"""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.status_codes: List[int] = []
        self.business_codes: List[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.requests.append({"url": str(request.url), "payload": body})
        status = self.status_codes.pop(0) if self.status_codes else 200
        business = self.business_codes.pop(0) if self.business_codes else None
        payload: Dict[str, Any] = {}
        if business is not None:
            payload = {"code": business, "msg": "mock business error"}
        return httpx.Response(status, json=payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def payloads(self) -> List[Dict[str, Any]]:
        return [item["payload"] for item in self.requests]


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """飞书通知默认配置（URL 指向 mock，不会真的发出去）。"""
    defaults: Dict[str, Any] = dict(
        db_path=str(tmp_path / "notify_feishu.db"),
        driver="simulator",
        poll_interval=3600.0,
        simulate_metrics=False,
        log_level="WARNING",
        nc_timeout=0.5,
        notify_enabled=True,
        notify_webhook_url="http://127.0.0.1:9999/webhook",
        notify_webhook_type="feishu",
        notify_min_severity="critical",
        notify_dedup_seconds=300.0,
        notify_timeout=2.0,
        notify_retry_delays=(0.01, 0.01),
        notify_on_recovery=True,
        notify_link_url="",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_alert(**overrides: Any) -> Alert:
    data: Dict[str, Any] = dict(
        alert_type="host_down",
        severity="critical",
        title="主机离线：PC-JX1",
        detail="NC /host pingStatus=FAILURE",
        dedup_key="host_down:h1",
        device_id="PC-JX1",
        observed_at=OBSERVED_AT,
    )
    data.update(overrides)
    return Alert(**data)


def recovery_row(**overrides: Any) -> Dict[str, Any]:
    """模拟 store.resolve_alert() 返回的 DB row（pipeline 传入的就是它）。"""
    row: Dict[str, Any] = dict(
        id=11,
        alert_type="host_down",
        severity="critical",
        title="主机恢复：PC-JX1",
        detail="pingStatus FAILURE -> SUCCESS（按数据源实际状态判定恢复，不等 NC issue 清除）",
        dedup_key="host_down:h1",
        device_id="PC-JX1",
        created_at=OBSERVED_AT,
        observed_at=OBSERVED_AT,
        resolved_at=OBSERVED_AT + 300.0,
        clear_reason="pingStatus FAILURE -> SUCCESS（按数据源实际状态判定恢复，不等 NC issue 清除）",
        stale_source=False,
        state="cleared",
        deleted_at=None,
        status="open",
    )
    row.update(overrides)
    return row


def card_of(payload: Dict[str, Any]) -> Dict[str, Any]:
    assert payload["msg_type"] == "interactive"
    return payload["card"]


def fields_of(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    for element in card["elements"]:
        if element.get("tag") == "div" and "fields" in element:
            return element["fields"]
    raise AssertionError("卡片缺少字段区 div.fields")


def field_map(card: Dict[str, Any]) -> Dict[str, str]:
    """字段区 -> {label: value}（用于断言字段内容）。"""
    mapping: Dict[str, str] = {}
    for item in fields_of(card):
        content = item["text"]["content"]
        label, _, value = content.partition("\n")
        mapping[label.strip("*")] = value
    return mapping


def notes_of(card: Dict[str, Any]) -> List[str]:
    return [
        element["elements"][0]["content"]
        for element in card["elements"]
        if element.get("tag") == "note"
    ]


def actions_of(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for element in card["elements"]:
        if element.get("tag") == "action":
            actions.extend(element.get("actions") or [])
    return actions


def run_service(
    tmp_path: Path,
    scenario,
    mock: MockWebhook,
    **settings_overrides: Any,
):
    """构建 NotifyService → 启动 → 执行 scenario(service) → 停止。"""

    async def runner():
        service = NotifyService(
            make_settings(tmp_path, **settings_overrides), transport=mock.transport
        )
        await service.start()
        try:
            return await scenario(service)
        finally:
            await service.stop()

    return run_async(runner())


# --------------------------------------------------------------------------- #
# 1) 飞书 interactive 卡片
# --------------------------------------------------------------------------- #
class TestFeishuAlertCard:
    def test_interactive_card_structure(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            assert service.enqueue(make_alert()) is True
            await asyncio.wait_for(service._queue.join(), timeout=5)
            return service.status()

        status = run_service(tmp_path, scenario, mock)
        assert len(mock.payloads) == 1
        card = card_of(mock.payloads[0])
        # header：颜色按 severity + 标题前缀
        assert card["header"]["template"] == "red"
        assert card["header"]["title"]["content"] == "【告警】主机离线：PC-JX1"
        # 字段区：级别/设备/规则/时间（北京时间 observed_at）/详情
        fields = fields_of(card)
        assert len(fields) == 5
        mapping = field_map(card)
        assert mapping["级别"] == "critical"
        assert mapping["设备"] == "PC-JX1"
        assert mapping["规则"] == "host_down"
        assert mapping["时间"] == BEIJING_TEXT
        assert mapping["详情"] == "NC /host pingStatus=FAILURE"
        # 前 4 项短字段两列排版，详情独占整行
        assert [item["is_short"] for item in fields] == [True, True, True, True, False]
        # 固定备注行
        assert FEISHU_NOTE in notes_of(card)
        # 未配置 NOTIFY_LINK_URL → 不出现按钮
        assert actions_of(card) == []
        # status 扩展字段
        assert status["last_payload_kind"] == PAYLOAD_ALERT
        assert status["on_recovery"] is True
        assert status["card_type"] == "feishu_interactive"
        assert status["sent_count"] == 1

    @pytest.mark.parametrize(
        "severity,min_severity,template",
        [
            ("critical", "critical", "red"),
            ("warning", "warning", "orange"),
            ("info", "info", "blue"),
        ],
    )
    def test_header_color_by_severity(
        self, tmp_path, severity, min_severity, template
    ):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            assert service.enqueue(
                make_alert(severity=severity, dedup_key="sev:{}".format(severity))
            )
            await asyncio.wait_for(service._queue.join(), timeout=5)

        run_service(
            tmp_path,
            scenario,
            mock,
            notify_min_severity=min_severity,
        )
        assert card_of(mock.payloads[0])["header"]["template"] == template

    def test_link_button_only_when_configured(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            assert service.enqueue(make_alert())
            await asyncio.wait_for(service._queue.join(), timeout=5)

        run_service(
            tmp_path,
            scenario,
            mock,
            notify_link_url="http://127.0.0.1:5173/",
        )
        buttons = actions_of(card_of(mock.payloads[0]))
        assert len(buttons) == 1
        assert buttons[0]["tag"] == "button"
        assert buttons[0]["text"]["content"] == "打开 3D 大屏"
        assert buttons[0]["url"] == "http://127.0.0.1:5173/"

    def test_test_message_is_green_card(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            result = await service.send_test()
            return result, service.status()

        result, status = run_service(tmp_path, scenario, mock)
        assert result["ok"] is True and result["sent"] is True
        assert result["kind"] == PAYLOAD_TEST
        card = card_of(mock.payloads[0])
        # 测试消息 header 绿色（severity=test → green）
        assert card["header"]["template"] == "green"
        assert card["header"]["title"]["content"].startswith("【测试】")
        assert status["last_payload_kind"] == PAYLOAD_TEST

    def test_other_webhook_types_not_regressed(self, tmp_path):
        """generic / dingtalk / wecom 的告警 payload 结构保持 W10 原样。"""
        expected = {
            "generic": ["title", "severity", "device", "time", "detail", "alert_id"],
            "dingtalk": ["msgtype", "markdown"],
            "wecom": ["msgtype", "markdown"],
        }
        for webhook_type, keys in expected.items():
            mock = MockWebhook()

            async def scenario(service: NotifyService):
                assert service.enqueue(make_alert())
                await asyncio.wait_for(service._queue.join(), timeout=5)

            run_service(
                tmp_path, scenario, mock, notify_webhook_type=webhook_type
            )
            payload = mock.payloads[0]
            assert sorted(payload.keys()) == sorted(keys), webhook_type
            if webhook_type == "generic":
                assert payload["title"] == "主机离线：PC-JX1"
                assert payload["severity"] == "critical"
                assert payload["device"] == "PC-JX1"
                assert payload["alert_id"] is None
            elif webhook_type == "dingtalk":
                assert payload["markdown"]["title"] == "主机离线：PC-JX1"
                assert "### 主机离线：PC-JX1" in payload["markdown"]["text"]
            else:
                assert "### 主机离线：PC-JX1" in payload["markdown"]["content"]


# --------------------------------------------------------------------------- #
# 2) 恢复通知
# --------------------------------------------------------------------------- #
class TestFeishuRecoveryCard:
    def test_recovery_card_structure(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            assert service.enqueue_recovery(recovery_row()) is True
            await asyncio.wait_for(service._queue.join(), timeout=5)
            return service.status()

        status = run_service(tmp_path, scenario, mock)
        card = card_of(mock.payloads[0])
        assert card["header"]["template"] == "green"
        assert card["header"]["title"]["content"] == "【恢复】主机恢复：PC-JX1"
        mapping = field_map(card)
        assert len(fields_of(card)) == 5
        assert mapping["级别"] == "已恢复"
        assert mapping["设备"] == "PC-JX1"
        assert mapping["规则"] == "host_down"
        assert mapping["恢复时间"] == BEIJING_RECOVERY_TEXT
        assert "pingStatus FAILURE -> SUCCESS" in mapping["恢复原因"]
        assert FEISHU_NOTE in notes_of(card)
        assert status["last_payload_kind"] == PAYLOAD_RECOVERY

    def test_stale_source_recovery_note(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            assert service.enqueue_recovery(
                recovery_row(stale_source=True, alert_type="health_issue")
            )
            await asyncio.wait_for(service._queue.join(), timeout=5)

        run_service(tmp_path, scenario, mock)
        card = card_of(mock.payloads[0])
        assert any("NC 问题记录滞后" in note for note in notes_of(card))

    def test_recovery_disabled_by_config(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            queued = service.enqueue_recovery(recovery_row())
            assert service.notify_recoveries([recovery_row(), recovery_row(id=12)]) == 0
            await asyncio.sleep(0.05)
            return queued, service.status()

        queued, status = run_service(
            tmp_path, scenario, mock, notify_on_recovery=False
        )
        assert queued is False
        assert status["on_recovery"] is False
        assert mock.requests == []

    def test_bulk_recovery_merged_into_one_card(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            rows = [
                recovery_row(id=100 + index, dedup_key="host_down:h{}".format(index))
                for index in range(6)
            ]
            queued = service.notify_recoveries(rows)
            await asyncio.wait_for(service._queue.join(), timeout=5)
            return queued

        queued = run_service(tmp_path, scenario, mock)
        assert queued == 1
        assert len(mock.payloads) == 1
        card = card_of(mock.payloads[0])
        assert card["header"]["template"] == "green"
        assert "批量恢复" in card["header"]["title"]["content"]
        mapping = field_map(card)
        assert "6" in mapping["恢复原因"]

    def test_small_recovery_batch_sends_each_card(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            rows = [
                recovery_row(id=200 + index, dedup_key="host_down:h{}".format(index))
                for index in range(3)
            ]
            queued = service.notify_recoveries(rows)
            await asyncio.wait_for(service._queue.join(), timeout=5)
            return queued

        queued = run_service(tmp_path, scenario, mock)
        assert queued == 3
        assert len(mock.payloads) == 3
        assert all(card_of(item)["header"]["template"] == "green" for item in mock.payloads)

    def test_send_test_recovery_card(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            result = await service.send_test(kind="recovery")
            return result, service.status()

        result, status = run_service(tmp_path, scenario, mock)
        assert result["ok"] is True
        assert result["kind"] == PAYLOAD_RECOVERY
        card = card_of(mock.payloads[0])
        assert card["header"]["template"] == "green"
        assert card["header"]["title"]["content"].startswith("【恢复】")
        assert status["last_payload_kind"] == PAYLOAD_RECOVERY

    def test_recovery_payload_for_other_types(self, tmp_path):
        for webhook_type in ("generic", "dingtalk", "wecom"):
            mock = MockWebhook()

            async def scenario(service: NotifyService):
                assert service.enqueue_recovery(recovery_row())
                await asyncio.wait_for(service._queue.join(), timeout=5)

            run_service(tmp_path, scenario, mock, notify_webhook_type=webhook_type)
            payload = mock.payloads[0]
            if webhook_type == "generic":
                assert payload["kind"] == PAYLOAD_RECOVERY
                assert payload["state"] == "cleared"
                assert payload["device"] == "PC-JX1"
                assert "pingStatus FAILURE -> SUCCESS" in payload["clear_reason"]
            else:
                text = json.dumps(payload, ensure_ascii=False)
                assert "【恢复】主机恢复：PC-JX1" in text
                assert "恢复原因" in text


# --------------------------------------------------------------------------- #
# 3) 运行时配置 apply_config
# --------------------------------------------------------------------------- #
class TestApplyConfig:
    def test_switch_type_and_url_without_restart(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            old_queue = service._queue
            assert service.enqueue(
                make_alert(dedup_key="before:1", severity="critical")
            )
            await asyncio.wait_for(service._queue.join(), timeout=5)
            status = await service.apply_config(
                webhook_url="http://127.0.0.1:9100/webhook",
                webhook_type="feishu",
            )
            # 重建了 httpx client / worker（queue 是新对象，后端不用重启）
            assert service._queue is not old_queue
            assert service._worker is not None
            assert service.enqueue(
                make_alert(dedup_key="after:1", title="设备不可达：SW-JX")
            )
            await asyncio.wait_for(service._queue.join(), timeout=5)
            return status, service.status()

        status, latest = run_service(tmp_path, scenario, mock, notify_webhook_type="generic")
        assert status["type"] == "feishu"
        assert status["config_source"] == "runtime"
        assert latest["sent_count"] == 2
        # 第 1 条走旧 URL 的 generic payload；第 2 条走新 URL 的飞书卡片
        assert mock.requests[0]["url"] == "http://127.0.0.1:9999/webhook"
        assert "title" in mock.payloads[0]
        assert mock.requests[1]["url"] == "http://127.0.0.1:9100/webhook"
        assert card_of(mock.payloads[1])["header"]["template"] == "red"
        # env 初始配置仍保留，可用于“恢复默认”
        assert latest["env_defaults"]["webhook_type"] == "generic"

    def test_invalid_type_rejected_without_state_change(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            with pytest.raises(NotifyConfigError):
                await service.apply_config(webhook_type="slack")
            with pytest.raises(NotifyConfigError):
                await service.apply_config(min_severity="fatal")
            # 校验失败不改状态：仍是可用状态且能正常发送
            assert service.enabled is True
            assert service.webhook_type == "feishu"
            assert service.min_severity == "critical"
            assert service.webhook_url == "http://127.0.0.1:9999/webhook"
            return service.status()

        status = run_service(tmp_path, scenario, mock)
        assert status["type"] == "feishu"
        assert status["config_source"] == "env"

    def test_enable_disable_runtime(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            assert service.enabled is False
            assert service.enqueue(make_alert()) is False
            enabled_status = await service.apply_config(
                enabled=True, webhook_url="http://127.0.0.1:9200/hook"
            )
            assert enabled_status["enabled"] is True
            assert enabled_status["enabled_requested"] is True
            assert enabled_status["configured"] is True
            assert service.enqueue(make_alert(dedup_key="on:1"))
            await asyncio.wait_for(service._queue.join(), timeout=5)
            disabled_status = await service.apply_config(enabled=False)
            assert disabled_status["enabled"] is False
            assert service._worker is None
            assert service.enqueue(make_alert(dedup_key="off:1")) is False
            return disabled_status

        status = run_service(
            tmp_path, scenario, mock, notify_enabled=False, notify_webhook_url=""
        )
        assert status["enabled"] is False
        assert len(mock.payloads) == 1

    def test_min_severity_and_recovery_toggle_runtime(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            old_worker = service._worker
            status = await service.apply_config(
                min_severity="warning", notify_on_recovery=False
            )
            # 仅阈值/恢复开关变化，不需要重建 worker
            assert service._worker is old_worker
            assert service.enqueue(make_alert(severity="warning", dedup_key="w:1"))
            assert service.enqueue_recovery(recovery_row()) is False
            await asyncio.wait_for(service._queue.join(), timeout=5)
            status2 = await service.apply_config(notify_on_recovery=True)
            assert status2["on_recovery"] is True
            return status, service.status()
        status, latest = run_service(tmp_path, scenario, mock)
        assert status["min_severity"] == "warning"
        assert status["on_recovery"] is False
        assert len(mock.payloads) == 1  # 只有 warning 告警，没有恢复通知
        assert latest["on_recovery"] is True
        assert latest["last_payload_kind"] == PAYLOAD_ALERT

    def test_disabled_service_apply_config_does_not_start_worker(self, tmp_path):
        mock = MockWebhook()

        async def scenario(service: NotifyService):
            status = await service.apply_config(min_severity="warning")
            assert status["enabled"] is False
            assert service._worker is None

        run_service(
            tmp_path, scenario, mock, notify_enabled=False, notify_webhook_url=""
        )
        assert mock.requests == []


# --------------------------------------------------------------------------- #
# 4) pipeline 恢复通知钩子
# --------------------------------------------------------------------------- #
class TestPipelineRecoveryNotify:
    def test_pipeline_sends_alert_then_recovery_card(self, tmp_path):
        mock = MockWebhook()
        settings = make_settings(tmp_path, driver="netcon")
        store = Store(settings.db_path)
        store.init_db()
        healthy = make_result(
            hosts=[make_host("h1", hostname="PC-JX1", ping_status="SUCCESS")]
        )
        fault = make_result(
            hosts=[make_host("h1", hostname="PC-JX1", ping_status="FAILURE")]
        )
        recovered = make_result(
            hosts=[make_host("h1", hostname="PC-JX1", ping_status="SUCCESS")]
        )
        driver = ScriptedDriver([healthy, fault, recovered])

        async def scenario():
            service = NotifyService(settings, transport=mock.transport)
            await service.start()
            pipeline = CollectorPipeline(
                settings=settings,
                store=store,
                driver=driver,
                metrics_driver=None,
                notify=service,
            )
            try:
                await pipeline.poll_once()  # 基线
                await pipeline.poll_once()  # 故障 → 告警卡片
                await pipeline.poll_once()  # 恢复 → 恢复卡片
                await asyncio.wait_for(service._queue.join(), timeout=10)
                return service.status()
            finally:
                await service.stop()

        status = run_async(scenario())
        assert len(mock.payloads) == 2
        alert_card = card_of(mock.payloads[0])
        recovery_card = card_of(mock.payloads[1])
        assert alert_card["header"]["template"] == "red"
        assert alert_card["header"]["title"]["content"] == "【告警】主机离线：PC-JX1"
        assert recovery_card["header"]["template"] == "green"
        assert recovery_card["header"]["title"]["content"] == "【恢复】主机恢复：PC-JX1"
        recovery_map = field_map(recovery_card)
        assert recovery_map["设备"] == "PC-JX1"
        assert "FAILURE -> SUCCESS" in recovery_map["恢复原因"]
        assert status["sent_count"] == 2
        assert status["last_payload_kind"] == PAYLOAD_RECOVERY
        # 库里该告警已 cleared 且记录了 clear_reason（恢复通知数据源一致）
        rows = store.load_alerts(limit=10, state="cleared")
        assert len(rows) == 1
        assert "FAILURE -> SUCCESS" in (rows[0].get("clear_reason") or "")

    def test_pipeline_skips_recovery_when_disabled(self, tmp_path):
        mock = MockWebhook()
        settings = make_settings(tmp_path, driver="netcon", notify_on_recovery=False)
        store = Store(settings.db_path)
        store.init_db()
        driver = ScriptedDriver(
            [
                make_result(hosts=[make_host("h1", hostname="PC-JX1")]),
                make_result(
                    hosts=[make_host("h1", hostname="PC-JX1", ping_status="FAILURE")]
                ),
                make_result(hosts=[make_host("h1", hostname="PC-JX1")]),
            ]
        )

        async def scenario():
            service = NotifyService(settings, transport=mock.transport)
            await service.start()
            pipeline = CollectorPipeline(
                settings=settings,
                store=store,
                driver=driver,
                metrics_driver=None,
                notify=service,
            )
            try:
                await pipeline.poll_once()
                await pipeline.poll_once()
                await pipeline.poll_once()
                await asyncio.wait_for(service._queue.join(), timeout=10)
                return service.status()
            finally:
                await service.stop()

        status = run_async(scenario())
        assert len(mock.payloads) == 1  # 只有告警卡片，没有恢复卡片
        assert status["sent_count"] == 1
        assert status["on_recovery"] is False
        # 告警本身仍正常 cleared（通知开关不影响告警生命周期）
        assert len(store.load_alerts(limit=10, state="cleared")) == 1


# --------------------------------------------------------------------------- #
# 5) 环境变量 / 业务错误码
# --------------------------------------------------------------------------- #
class TestEnvAndErrors:
    def test_settings_env_fields(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_ON_RECOVERY", "false")
        monkeypatch.setenv("NOTIFY_LINK_URL", "http://127.0.0.1:5173/")
        settings = Settings.from_env()
        assert settings.notify_on_recovery is False
        assert settings.notify_link_url == "http://127.0.0.1:5173/"

        monkeypatch.setenv("NOTIFY_ON_RECOVERY", "1")
        assert Settings.from_env().notify_on_recovery is True

    def test_business_code_error_is_retried(self, tmp_path):
        """飞书 HTTP 200 但业务码 19021（签名错误）也要算失败并重试。"""
        mock = MockWebhook()
        mock.business_codes = [19021, 19021, 0]

        async def scenario(service: NotifyService):
            assert service.enqueue(make_alert())
            await asyncio.wait_for(service._queue.join(), timeout=10)
            return service.status()

        status = run_service(tmp_path, scenario, mock)
        assert len(mock.requests) == 3  # 初次 + 2 次重试
        assert status["sent_count"] == 1
        assert status["failed_count"] == 2
        assert status["last_error"] is None
