# -*- coding: utf-8 -*-
"""W15 通知运行时配置 API 测试：校验 / 运行时生效 / 持久化 / 启动加载 / masked URL。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make_settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        db_path=str(tmp_path / "notify_config.db"),
        driver="simulator",
        poll_interval=3600.0,
        simulate_metrics=False,
        log_level="WARNING",
        nc_timeout=0.5,
        notify_enabled=False,
        notify_webhook_url="",
        notify_webhook_type="generic",
        notify_min_severity="critical",
        notify_on_recovery=True,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    path = tmp_path / "notify_config.json"
    monkeypatch.setenv("NOTIFY_CONFIG_PATH", str(path))
    return path


LONG_WEBHOOK = (
    "http://127.0.0.1:9/webhook/secret-token-abcdefghijklmnopqrstuvwxyz-0123456789"
)


class TestNotifyConfigValidation:
    def test_invalid_params_return_400_with_chinese_detail(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            before = client.get("/api/notify/status").json()
            cases = [
                ({"webhook_type": "slack"}, "webhook_type"),
                ({"min_severity": "fatal"}, "min_severity"),
                ({"webhook_url": "ftp://example.com/hook"}, "http"),
                ({"webhook_url": ""}, "不能为空"),
                ({}, "至少"),
            ]
            for payload, keyword in cases:
                response = client.post("/api/notify/config", json=payload)
                assert response.status_code == 400, (payload, response.text)
                assert keyword in response.json()["detail"], response.json()
            after = client.get("/api/notify/status").json()
            assert after["enabled"] == before["enabled"]
            assert after["type"] == before["type"]
            assert after["min_severity"] == before["min_severity"]

    def test_invalid_bool_type_is_422(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            response = client.post(
                "/api/notify/config", json={"enabled": "not-a-bool"}
            )
            assert response.status_code == 422


class TestNotifyConfigApply:
    def test_partial_update_applies_without_reboot(self, tmp_path):
        app = create_app(
            make_settings(
                tmp_path,
                notify_enabled=True,
                notify_webhook_url="http://127.0.0.1:8/env-hook",
                notify_min_severity="critical",
                notify_on_recovery=True,
            )
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/notify/config", json={"min_severity": "warning"}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is True
            assert body["persisted"] is True
            status = body["status"]
            assert status["min_severity"] == "warning"
            assert status["type"] == "generic"
            assert status["on_recovery"] is True
            assert status["config_source"] == "runtime"
            # GET 立即反映
            current = client.get("/api/notify/status").json()
            assert current["min_severity"] == "warning"

    def test_full_config_persisted_and_masked(self, tmp_path, isolated_config_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            response = client.post(
                "/api/notify/config",
                json={
                    "enabled": True,
                    "webhook_url": LONG_WEBHOOK,
                    "webhook_type": "feishu",
                    "min_severity": "warning",
                    "on_recovery": False,
                    "link_url": "http://127.0.0.1:5173/",
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is True and body["persisted"] is True
            status = body["status"]
            assert status["enabled"] is True
            assert status["configured"] is True
            assert status["type"] == "feishu"
            assert status["min_severity"] == "warning"
            assert status["on_recovery"] is False
            assert status["link_url"] == "http://127.0.0.1:5173/"
            assert status["link_button"] is True
            assert status["config_source"] == "runtime"
            # URL 必须 masked，响应不能出现完整 token
            assert status["webhook_url"] != LONG_WEBHOOK
            assert status["webhook_url"].endswith("...")
            assert LONG_WEBHOOK not in response.text
            # GET 同样 masked
            current = client.get("/api/notify/status").json()
            assert current["webhook_url"] == status["webhook_url"]
            assert LONG_WEBHOOK not in json.dumps(current)

        # 持久化文件：完整 URL（用于重启恢复），字段齐全
        assert isolated_config_path.is_file()
        persisted = json.loads(isolated_config_path.read_text(encoding="utf-8"))
        assert persisted["enabled"] is True
        assert persisted["webhook_url"] == LONG_WEBHOOK
        assert persisted["webhook_type"] == "feishu"
        assert persisted["min_severity"] == "warning"
        assert persisted["on_recovery"] is False
        assert persisted["link_url"] == "http://127.0.0.1:5173/"
        assert "updated_at" in persisted

    def test_startup_loads_persisted_config_over_env(self, tmp_path, isolated_config_path):
        isolated_config_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_config_path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "webhook_url": LONG_WEBHOOK,
                    "webhook_type": "feishu",
                    "min_severity": "warning",
                    "on_recovery": False,
                    "link_url": "http://127.0.0.1:5173/",
                    "updated_at": 1793695000.0,
                }
            ),
            encoding="utf-8",
        )
        settings = make_settings(
            tmp_path,
            notify_enabled=True,
            notify_webhook_url="http://127.0.0.1:8/env-hook",
            notify_webhook_type="generic",
            notify_min_severity="critical",
            notify_on_recovery=True,
        )
        app = create_app(settings)
        with TestClient(app) as client:
            status = client.get("/api/notify/status").json()
            assert status["enabled"] is True
            assert status["configured"] is True
            assert status["type"] == "feishu"
            assert status["min_severity"] == "warning"
            assert status["on_recovery"] is False
            assert status["link_url"] == "http://127.0.0.1:5173/"
            assert status["config_source"] == "file"
            assert status["webhook_url"].endswith("...")
            # env_defaults 仍保留环境变量快照（供 UI「恢复默认」）
            assert status["env_defaults"]["webhook_type"] == "generic"
            assert status["env_defaults"]["min_severity"] == "critical"

    def test_notify_test_endpoint_kind(self, tmp_path):
        app = create_app(
            make_settings(
                tmp_path,
                notify_enabled=True,
                notify_webhook_url="http://127.0.0.1:9/webhook",
            )
        )
        with TestClient(app) as client:
            default = client.post("/api/notify/test")
            assert default.status_code == 200
            assert default.json()["kind"] == "test"
            recovery = client.post("/api/notify/test", json={"kind": "recovery"})
            assert recovery.status_code == 200
            assert recovery.json()["kind"] == "recovery"
            invalid = client.post("/api/notify/test", json={"kind": "bogus"})
            assert invalid.status_code == 400
            assert "kind" in invalid.json()["detail"]

    def test_link_url_validation(self, tmp_path):
        app = create_app(make_settings(tmp_path))
        with TestClient(app) as client:
            bad = client.post("/api/notify/config", json={"link_url": "ftp://x"})
            assert bad.status_code == 400
            assert "link_url" in bad.json()["detail"]
            clear = client.post("/api/notify/config", json={"link_url": ""})
            assert clear.status_code == 200
            assert clear.json()["status"]["link_url"] == ""

    def test_startup_without_file_keeps_env(self, tmp_path, isolated_config_path):
        settings = make_settings(
            tmp_path,
            notify_enabled=True,
            notify_webhook_url="http://127.0.0.1:8/env-hook",
            notify_webhook_type="wecom",
            notify_min_severity="critical",
            notify_on_recovery=True,
        )
        app = create_app(settings)
        with TestClient(app) as client:
            status = client.get("/api/notify/status").json()
            assert status["enabled"] is True
            assert status["type"] == "wecom"
            assert status["config_source"] == "env"
        assert not isolated_config_path.exists()

    def test_invalid_config_file_ignored_on_startup(self, tmp_path, isolated_config_path):
        isolated_config_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_config_path.write_text("{not-valid-json", encoding="utf-8")
        settings = make_settings(
            tmp_path,
            notify_enabled=True,
            notify_webhook_url="http://127.0.0.1:8/env-hook",
            notify_webhook_type="dingtalk",
            notify_min_severity="critical",
        )
        app = create_app(settings)
        with TestClient(app) as client:
            status = client.get("/api/notify/status").json()
            assert status["enabled"] is True
            assert status["type"] == "dingtalk"
            assert status["config_source"] == "env"
