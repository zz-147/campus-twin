# -*- coding: utf-8 -*-
"""pytest 公共配置：把 backend/ 加入 sys.path，并提供 JSON fixture 读取工具。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> Any:
    """读取 tests/fixtures 下固定 JSON 样本（不依赖 PT 在线）。"""
    path = FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture()
def fixture_loader():
    return load_fixture
