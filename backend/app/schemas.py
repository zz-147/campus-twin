# -*- coding: utf-8 -*-
"""W9 管理类 API 请求模型（告警数据管理 / 场景配置）。

响应统一用 dict 组装（便于附带 version/审计信息），请求体用 Pydantic 做基础校验。
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class Position2(BaseModel):
    x: float = 0.0
    z: float = 0.0


class Position3(BaseModel):
    x: float = 0.0
    y: Optional[float] = None
    z: float = 0.0


class Size3(BaseModel):
    width: float = 40.0
    depth: float = 30.0
    height: float = 16.0


class BuildingCreate(BaseModel):
    """POST /api/scene/buildings 请求体。"""

    name: str = Field(min_length=1, max_length=80, description="楼栋中文名")
    id: Optional[str] = Field(
        default=None, max_length=64, description="可选自定义 id（默认自动生成 bld-N）"
    )
    position: Optional[Position2] = Field(default=None, description="地面中心坐标 {x,z}")
    size: Optional[Size3] = Field(default=None, description="体块尺寸 {width,depth,height}")
    style: Optional[Dict[str, Any]] = Field(default=None, description="低模风格（颜色/透明度等）")
    status: Literal["pending", "confirmed"] = Field(
        default="pending", description="默认 pending，确认后才算正式场景"
    )


class DeviceUpsert(BaseModel):
    """POST /api/scene/buildings/{id}/devices 请求体（按 name upsert）。"""

    name: str = Field(min_length=1, max_length=120, description="设备名（全局唯一键）")
    type: Optional[str] = Field(
        default="unknown",
        max_length=32,
        description="设备类型：switch/router/server/pc/controller/unknown",
    )
    position: Optional[Position3] = Field(
        default=None, description="世界坐标 {x,y,z}；不传则按楼栋自动落位"
    )
    status: Optional[Literal["pending", "confirmed"]] = Field(
        default=None, description="不传：新设备 pending，已有设备保持原状态"
    )
    source: Optional[str] = Field(default="manual", max_length=32)
    model: Optional[str] = Field(default=None, max_length=120)
    mgmt_ip: Optional[str] = Field(default=None, max_length=64)


class PathTraceRequest(BaseModel):
    """POST /api/path-trace 请求体。"""

    source_ip: str = Field(min_length=1, max_length=64, description="源 IP（如主机/服务器）")
    dest_ip: str = Field(min_length=1, max_length=64, description="目的 IP")


class AlertClearRequest(BaseModel):
    """POST /api/alerts/clear 请求体。"""

    scope: Literal["cleared", "all_cleared"] = Field(
        default="cleared",
        description="cleared=仅已恢复告警；all_cleared=已恢复+info 一次性事件（active 永不删除）",
    )
    before: Optional[float] = Field(
        default=None, description="可选 epoch：仅删除 observed_at < before 的告警"
    )
