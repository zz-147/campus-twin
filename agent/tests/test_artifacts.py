# -*- coding: utf-8 -*-
"""W16：设备接入配置文档 artifact（生成/存储/API 下载/SSE）。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from app.agent.artifacts import (
    ArtifactStore,
    build_device_config_markdown,
    markdown_to_text,
    sanitize_filename,
)
from app.api import create_app
from app.config import AgentSettings


def _client(tmp_path: Path) -> Iterator[TestClient]:
    env = {
        "LLM_PROVIDER": "mock",
        "AGENT_DATA_SOURCE": "fixture",
        "AGENT_FIXTURE_SCENARIO": "s21",
        "AGENT_DB_PATH": str(tmp_path / "agent.db"),
        "AGENT_GRAPH_DB_PATH": str(tmp_path / "graph.db"),
    }
    settings = AgentSettings.from_env(env=env)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_artifact_store_save_get_and_txt(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "agent.db")
    descriptor = store.save(
        "conv_1",
        kind="device_config",
        title="SW-TEST 接入配置文档",
        filename="SW-TEST_接入配置_20261103-2200.md",
        content="# SW-TEST\n\n## CLI\n\n```cisco\nhostname SW-TEST\n```\n",
    )
    assert descriptor["id"].startswith("art_")
    assert descriptor["filename"].startswith("SW-TEST_接入配置_")
    assert descriptor["size"] > 0
    record = store.get(descriptor["id"])
    assert record is not None and record["conversation_id"] == "conv_1"
    assert "hostname SW-TEST" in store.content(descriptor["id"])
    assert store.count() == 1
    assert store.list_since("conv_1", 0)[0]["id"] == descriptor["id"]
    assert store.list_since("conv_1", 9999999999) == []
    # 同名文件自动去重（仍保持 .md 后缀）
    second = store.save("conv_1", kind="device_config", title="t", filename="same.md", content="x")
    assert second["filename"].startswith("same")
    assert sanitize_filename("SW JX/测试") == "SW_JX_测试"
    assert "hostname" in markdown_to_text("# T\n```cisco\nhostname SW\n```")


def test_markdown_content_for_switch_and_pc() -> None:
    switch_md = build_device_config_markdown(
        hostname="SW-SX-2961",
        device_label="Cisco Catalyst 2960-24TT",
        device_model="WS-C2960-24TT-L",
        device_kind="switch",
        building_name="实训楼",
        building_id="train",
        mgmt_ip="192.168.10.14",
        vlan=30,
        gateway="192.168.30.1",
        uplink={"peer_device": "Core-3650", "local_port": "Gi0/1", "peer_port": "Gi1/0/7"},
        cabling=[{
            "local_device": "SW-SX-2961", "local_port": "Gi0/1",
            "peer_device": "Core-3650", "peer_port": "Gi1/0/7",
            "cable": "直通线", "purpose": "上联 Trunk",
        }],
        cli="hostname SW-SX-2961\nvlan 30\ninterface Vlan30",
        verification=["show vlan brief", "ping 192.168.30.1"],
        undo=["no vlan 30"],
        scene_type="switch",
        position={"x": 1, "y": 2, "z": 3},
    )
    for keyword in ("设备信息", "端口连线表", "```cisco", "hostname SW-SX-2961", "vlan 30", "验证命令", "撤销", "注意事项"):
        assert keyword in switch_md
    pc_md = build_device_config_markdown(
        hostname="PC-JX3",
        device_label="PC-PT 终端",
        device_model="PC-PT",
        device_kind="pc",
        building_name="教学楼",
        building_id="teach",
        mgmt_ip="192.168.20.20",
        vlan=20,
        gateway="192.168.20.1",
        uplink={"peer_device": "SW-JX-2960", "local_port": "Fa0", "peer_port": "Fa0/3"},
        cabling=[{"local_device": "PC-JX3", "local_port": "Fa0", "peer_device": "SW-JX-2960", "peer_port": "Fa0/3", "cable": "直通线", "purpose": "终端接入"}],
        cli="1. 打开 PT 终端 Config\n2. 配置 IP",
        verification=["ping 192.168.20.1"],
        undo=["删除连线"],
        scene_type="pc",
        position={"x": 1, "y": 2, "z": 3},
    )
    for keyword in ("PT GUI 配置步骤", "IP 地址", "子网掩码", "默认网关", "DNS", "PC-JX3"):
        assert keyword in pc_md


def test_chat_artifact_download_switch(tmp_path: Path) -> None:
    with TestClient(create_app(AgentSettings.from_env(env={
        "LLM_PROVIDER": "mock",
        "AGENT_DATA_SOURCE": "fixture",
        "AGENT_FIXTURE_SCENARIO": "s21",
        "AGENT_DB_PATH": str(tmp_path / "agent.db"),
        "AGENT_GRAPH_DB_PATH": str(tmp_path / "graph.db"),
    }))) as client:
        response = client.post(
            "/api/chat", json={"question": "我想在实训楼加一台 2960 交换机，怎么配置？"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert isinstance(body.get("artifacts"), list)
        assert len(body["artifacts"]) == 1, body.get("trace", {}).get("artifact_error")
        artifact = body["artifacts"][0]
        assert artifact["kind"] == "device_config"
        assert artifact["filename"].startswith("SW-SX-2961_接入配置_")
        assert artifact["filename"].endswith(".md")
        assert artifact["size"] > 100
        download = client.get("/api/artifacts/%s" % artifact["id"])
        assert download.status_code == 200
        assert "text/markdown" in download.headers.get("content-type", "")
        assert "attachment" in download.headers.get("content-disposition", "")
        assert "filename*=UTF-8''" in download.headers.get("content-disposition", "")
        text = download.text
        for keyword in ("hostname SW-SX-2961", "vlan", "interface", "验证命令", "撤销"):
            assert keyword in text, keyword
        txt = client.get("/api/artifacts/%s?format=txt" % artifact["id"])
        assert txt.status_code == 200
        assert "text/plain" in txt.headers.get("content-type", "")
        assert "hostname SW-SX-2961" in txt.text
        assert client.get("/api/artifacts/art_not_found").status_code == 404


def test_chat_artifact_pc_gui_steps(tmp_path: Path) -> None:
    with TestClient(create_app(AgentSettings.from_env(env={
        "LLM_PROVIDER": "mock",
        "AGENT_DATA_SOURCE": "fixture",
        "AGENT_FIXTURE_SCENARIO": "s21",
        "AGENT_DB_PATH": str(tmp_path / "agent.db"),
        "AGENT_GRAPH_DB_PATH": str(tmp_path / "graph.db"),
    }))) as client:
        body = client.post("/api/chat", json={"question": "教学楼加一台 PC"}).json()
        assert body["artifacts"], body.get("trace")
        artifact = body["artifacts"][0]
        download = client.get("/api/artifacts/%s" % artifact["id"])
        assert download.status_code == 200
        assert "PT GUI 配置步骤" in download.text
        assert "IP 地址" in download.text and "默认网关" in download.text
        assert "PC-JX" in download.text
        assert "```cisco" not in download.text


def test_chat_no_artifact_for_non_onboarding(tmp_path: Path) -> None:
    with TestClient(create_app(AgentSettings.from_env(env={
        "LLM_PROVIDER": "mock",
        "AGENT_DATA_SOURCE": "fixture",
        "AGENT_FIXTURE_SCENARIO": "s01",
        "AGENT_DB_PATH": str(tmp_path / "agent.db"),
        "AGENT_GRAPH_DB_PATH": str(tmp_path / "graph.db"),
    }))) as client:
        body = client.post("/api/chat", json={"question": "现在整体网络状态怎么样"}).json()
        assert body["artifacts"] == []


def test_sse_done_carries_artifacts(tmp_path: Path) -> None:
    with TestClient(create_app(AgentSettings.from_env(env={
        "LLM_PROVIDER": "mock",
        "AGENT_DATA_SOURCE": "fixture",
        "AGENT_FIXTURE_SCENARIO": "s21",
        "AGENT_DB_PATH": str(tmp_path / "agent.db"),
        "AGENT_GRAPH_DB_PATH": str(tmp_path / "graph.db"),
    }))) as client:
        with client.stream(
            "POST", "/api/chat/stream",
            json={"question": "我想在实训楼加一台 2960 交换机，怎么配置？"},
        ) as response:
            payload = "".join(response.iter_text())
        events = [json.loads(line[len("data: "):]) for line in payload.splitlines() if line.startswith("data: ")]
        done = next(event for event in events if event.get("type") == "done")
        assert done["artifacts"]
        assert done["artifacts"][0]["kind"] == "device_config"
