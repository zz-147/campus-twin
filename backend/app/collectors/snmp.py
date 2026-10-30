# -*- coding: utf-8 -*-
"""W10 SNMP 真实设备驱动（MIB-II，v1/v2c，pysnmp）。

契约（docs/W10-路径追踪告警聚合设计.md §四）：

- ``DRIVER=snmp`` 时启用，实现既有 ``Driver`` 协议（name/probe/collect/close）；
- 配置 ``SNMP_DEVICES``（JSON 数组）::

      [{"name": "SW-A", "ip": "127.0.0.1", "community": "public",
        "version": "2c", "building": "core"}]

- 采集 MIB-II：``sysDescr.0`` / ``sysUpTime.0`` / ``sysName.0``、
  ``ifOperStatus`` / ``ifInOctets|ifHCOutOctets`` / ``ifSpeed|ifHighSpeed``；
- 归一化为统一 ``Device`` + ``MetricPoint``（``source="snmp"``，
  ``if_*_bps`` 带 ``speed_bps`` 并按端口速率钳制，``bps <= speed_bps``）；
- 容错：单设备超时/失败不影响其它设备；全部失败 ``ok=False`` +
  ``reachable=false``，由 pipeline 优雅降级。

pysnmp 4.4.12 是同步 API，驱动通过 ``asyncio.to_thread`` 把阻塞 SNMP 调用放到
线程池，避免阻塞采集事件循环；单测通过注入 ``client=MockSnmpClient`` 不打真实网络。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.collectors.base import CollectResult
from app.config import Settings
from app.models import Device, MetricPoint, NetworkHealth, Topology, TopologyNode

logger = logging.getLogger("campus_twin.snmp")

# ---- MIB-II OIDs ----
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"
OID_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"
OID_IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"
OID_IF_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"

# ifOperStatus: 1=up, 2=down, 3=testing, ... 其余视为非 up
IF_OPER_STATUS_TEXT = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}


@dataclass
class SnmpInterfaceSample:
    index: int
    name: str = ""
    oper_status: int = 0
    speed_bps: Optional[float] = None
    in_octets: Optional[int] = None
    out_octets: Optional[int] = None


@dataclass
class SnmpDeviceSample:
    """一台 SNMP 设备的一次采样结果。"""

    sys_descr: str = ""
    sys_name: str = ""
    sys_up_time: Optional[float] = None
    interfaces: List[SnmpInterfaceSample] = field(default_factory=list)


class SnmpClientProtocol:  # pragma: no cover - 仅用于类型说明
    async def collect_device(
        self,
        ip: str,
        community: str = "public",
        version: str = "2c",
        timeout: float = 2.0,
        retries: int = 1,
    ) -> SnmpDeviceSample:
        raise NotImplementedError


class PysnmpClient:
    """pysnmp 4.4.12 实现（同步 hlapi + to_thread）。"""

    def _collect_sync(
        self,
        ip: str,
        community: str,
        version: str,
        timeout: float,
        retries: int,
    ) -> SnmpDeviceSample:
        from pysnmp.hlapi import (  # 延迟 import：未安装 pysnmp 时只在使用 snmp 驱动时报错
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            bulkCmd,
            getCmd,
        )

        mp_model = 0 if str(version).strip().lower() in ("1", "v1") else 1
        transport = UdpTransportTarget((ip, 161), timeout=timeout, retries=retries)
        engine = SnmpEngine()

        def _get(oid: str) -> Any:
            iterator = getCmd(
                engine,
                CommunityData(community, mpModel=mp_model),
                transport,
                ContextData(),
                ObjectType(ObjectIdentity(oid)),
            )
            try:
                error_indication, error_status, error_index, var_binds = next(iterator)
            except StopIteration:
                raise RuntimeError("SNMP get 未返回数据：{}".format(oid))
            if error_indication:
                raise RuntimeError("SNMP 错误：{}".format(error_indication))
            if error_status:
                raise RuntimeError(
                    "SNMP 错误：{} at {}".format(
                        error_status.prettyPrint(), error_index and var_binds[int(error_index) - 1][0]
                    )
                )
            return var_binds[0][1] if var_binds else None

        def _walk(base_oid: str, max_rows: int = 64) -> Dict[int, Any]:
            result: Dict[int, Any] = {}
            iterator = bulkCmd(
                engine,
                CommunityData(community, mpModel=mp_model),
                transport,
                ContextData(),
                0,
                25,
                ObjectType(ObjectIdentity(base_oid)),
                lexicographicMode=False,
            )
            for row_index, (error_indication, error_status, _error_index, var_binds) in enumerate(iterator):
                if row_index > max_rows:
                    break
                if error_indication:
                    raise RuntimeError("SNMP 错误：{}".format(error_indication))
                if error_status:
                    raise RuntimeError("SNMP 错误：{}".format(error_status.prettyPrint()))
                for oid_obj, value in var_binds:
                    if value.__class__.__name__.lower() == "endofmibview":
                        continue
                    try:
                        suffix = oid_obj.asTuple()[-1]
                    except Exception:  # noqa: BLE001 - 非标准 OID 直接跳过
                        continue
                    result[int(suffix)] = value
                if not var_binds:
                    break
            return result

        sample = SnmpDeviceSample(
            sys_descr=str(_get(OID_SYS_DESCR) or ""),
            sys_name=str(_get(OID_SYS_NAME) or ""),
        )
        try:
            sample.sys_up_time = float(int(_get(OID_SYS_UPTIME))) / 100.0
        except Exception:  # noqa: BLE001 - upTime 缺失不影响主流程
            sample.sys_up_time = None

        descr = _walk(OID_IF_DESCR)
        oper = _walk(OID_IF_OPER_STATUS)
        speed = _walk(OID_IF_SPEED)
        high_speed = _walk(OID_IF_HIGH_SPEED)
        hc_in = _walk(OID_IF_HC_IN_OCTETS)
        hc_out = _walk(OID_IF_HC_OUT_OCTETS)
        in_octets = _walk(OID_IF_IN_OCTETS)
        out_octets = _walk(OID_IF_OUT_OCTETS)

        for index in sorted(set(descr) | set(oper) | set(speed) | set(high_speed)):
            speed_bps: Optional[float] = None
            if index in high_speed:
                try:
                    speed_bps = float(int(high_speed[index])) * 1_000_000.0
                except (TypeError, ValueError):
                    speed_bps = None
            if (speed_bps is None or speed_bps <= 0) and index in speed:
                try:
                    speed_bps = float(int(speed[index]))
                except (TypeError, ValueError):
                    speed_bps = None
            in_value: Optional[int] = None
            out_value: Optional[int] = None
            if index in hc_in:
                try:
                    in_value = int(hc_in[index])
                except (TypeError, ValueError):
                    in_value = None
            if in_value is None and index in in_octets:
                try:
                    in_value = int(in_octets[index])
                except (TypeError, ValueError):
                    in_value = None
            if index in hc_out:
                try:
                    out_value = int(hc_out[index])
                except (TypeError, ValueError):
                    out_value = None
            if out_value is None and index in out_octets:
                try:
                    out_value = int(out_octets[index])
                except (TypeError, ValueError):
                    out_value = None
            sample.interfaces.append(
                SnmpInterfaceSample(
                    index=int(index),
                    name=str(descr.get(index) or "if{}".format(index)),
                    oper_status=(
                        int(oper[index]) if index in oper else 0
                    ),
                    speed_bps=speed_bps,
                    in_octets=in_value,
                    out_octets=out_value,
                )
            )
        return sample

    async def collect_device(
        self,
        ip: str,
        community: str = "public",
        version: str = "2c",
        timeout: float = 2.0,
        retries: int = 1,
    ) -> SnmpDeviceSample:
        return await asyncio.to_thread(
            self._collect_sync, ip, community, version, timeout, retries
        )

    async def close(self) -> None:
        return None


class SnmpDriver:
    """SNMP MIB-II 采集驱动（DRIVER=snmp）。"""

    name = "snmp"

    def __init__(
        self,
        settings: Settings,
        client: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.device_configs: List[Dict[str, Any]] = [
            dict(item) for item in (settings.snmp_devices or []) if isinstance(item, dict)
        ]
        self.client = client if client is not None else PysnmpClient()
        # (ip, ifindex) -> (sample_ts, in_octets, out_octets)
        self._last_counters: Dict[Tuple[str, int], Tuple[float, int, int]] = {}
        self.reachable: bool = False
        self.last_ok_at: Optional[float] = None
        self.device_errors: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # 驱动契约
    # ------------------------------------------------------------------ #
    async def probe(self) -> bool:
        if not self.device_configs:
            logger.info("SNMP 驱动未配置 SNMP_DEVICES，probe=false（优雅降级）")
            return False
        config = self.device_configs[0]
        try:
            await asyncio.wait_for(
                self.client.collect_device(
                    ip=str(config.get("ip") or ""),
                    community=str(config.get("community") or "public"),
                    version=str(config.get("version") or "2c"),
                    timeout=self.settings.snmp_timeout,
                    retries=self.settings.snmp_retries,
                ),
                timeout=max(self.settings.snmp_timeout * 2.0, 3.0),
            )
            self.reachable = True
            return True
        except Exception as exc:  # noqa: BLE001 - probe 不抛异常
            logger.info("SNMP probe 失败（%s）：%s", config.get("ip"), exc)
            self.reachable = False
            return False

    async def collect(self) -> CollectResult:
        if not self.device_configs:
            # 未配置设备：返回空数据集（ok=True），服务照常运行
            return CollectResult(
                devices=[],
                topology=Topology(nodes=[], links=[], captured_at=time.time(), source="snmp"),
                hosts=[],
                issues=[],
                metrics=[],
                network_health=NetworkHealth.compute_from_devices([], source="snmp"),
                source="snmp",
                ok=True,
                error="SNMP_DEVICES 未配置（空数据集）",
                collected_at=time.time(),
            )

        now = time.time()
        results = await asyncio.gather(
            *(self._collect_one(config, now) for config in self.device_configs),
            return_exceptions=True,
        )
        devices: List[Device] = []
        metrics: List[MetricPoint] = []
        errors: Dict[str, str] = {}
        for config, result in zip(self.device_configs, results):
            ip = str(config.get("ip") or "")
            if isinstance(result, Exception):
                errors[ip or str(config.get("name"))] = "{}: {}".format(
                    type(result).__name__, result
                )
                logger.warning("SNMP 设备采集失败（%s）：%s", ip, result)
                continue
            device, device_metrics = result
            devices.append(device)
            metrics.extend(device_metrics)

        self.device_errors = errors
        if not devices:
            self.reachable = False
            return CollectResult.unavailable(
                "snmp",
                "全部 SNMP 设备采集失败：{}".format(
                    "; ".join("{}={}".format(key, value) for key, value in errors.items())
                )[:500],
            )

        self.reachable = True
        self.last_ok_at = now
        nodes = [
            TopologyNode(id=device.id, label=device.hostname, role="snmp", source="snmp")
            for device in devices
        ]
        return CollectResult(
            devices=devices,
            topology=Topology(nodes=nodes, links=[], captured_at=now, source="snmp"),
            hosts=[],
            issues=[],
            metrics=metrics,
            network_health=NetworkHealth.compute_from_devices(devices, source="snmp"),
            source="snmp",
            ok=True,
            error=(
                None
                if not errors
                else "; ".join(sorted(errors.values()))[:500]
            ),
            errors=errors,
            collected_at=now,
        )

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                logger.debug("SNMP 客户端关闭异常", exc_info=True)

    # ------------------------------------------------------------------ #
    # 单设备采集
    # ------------------------------------------------------------------ #
    async def _collect_one(
        self, config: Dict[str, Any], now: float
    ) -> Tuple[Device, List[MetricPoint]]:
        ip = str(config.get("ip") or "").strip()
        if not ip:
            raise RuntimeError("SNMP_DEVICES 条目缺少 ip")
        sample = await asyncio.wait_for(
            self.client.collect_device(
                ip=ip,
                community=str(config.get("community") or "public"),
                version=str(config.get("version") or "2c"),
                timeout=self.settings.snmp_timeout,
                retries=self.settings.snmp_retries,
            ),
            timeout=max(self.settings.snmp_timeout * 2.0, 3.0),
        )
        device = self._device_from_sample(config, sample, now)
        metrics = self._metrics_from_sample(device, sample, now)
        return device, metrics

    @staticmethod
    def _device_from_sample(
        config: Dict[str, Any], sample: SnmpDeviceSample, now: float
    ) -> Device:
        ip = str(config.get("ip") or "").strip()
        name = str(sample.sys_name or config.get("name") or ip or "snmp-device").strip()
        platform = _platform_from_sys_descr(sample.sys_descr)
        return Device(
            id="snmp-{}".format(ip.replace(".", "-")),
            hostname=name,
            management_ip=ip,
            platform_id=platform,
            product_id=platform,
            device_type="SNMP MIB-II",
            family="SNMP",
            software_version=None,
            up_time=(
                "{:.0f}s".format(sample.sys_up_time)
                if sample.sys_up_time is not None
                else None
            ),
            reachability="Reachable",
            collection_status="Managed",
            interface_count=len(sample.interfaces),
            last_updated=now,
            source="snmp",
            last_seen=now,
        )

    def _metrics_from_sample(
        self, device: Device, sample: SnmpDeviceSample, now: float
    ) -> List[MetricPoint]:
        metrics: List[MetricPoint] = []
        max_utilization = 0.0
        for interface in sample.interfaces:
            speed_bps = interface.speed_bps
            key = (str(device.management_ip or ""), interface.index)
            previous = self._last_counters.get(key)
            in_bps = 0.0
            out_bps = 0.0
            if (
                previous is not None
                and interface.in_octets is not None
                and interface.out_octets is not None
            ):
                previous_ts, previous_in, previous_out = previous
                elapsed = max(now - previous_ts, 1e-3)
                delta_in = max(interface.in_octets - previous_in, 0)
                delta_out = max(interface.out_octets - previous_out, 0)
                in_bps = delta_in * 8.0 / elapsed
                out_bps = delta_out * 8.0 / elapsed
            if interface.in_octets is not None and interface.out_octets is not None:
                self._last_counters[key] = (
                    now,
                    interface.in_octets,
                    interface.out_octets,
                )

            tags = {
                "interface": interface.name,
                "if_index": interface.index,
                "oper_status": IF_OPER_STATUS_TEXT.get(
                    interface.oper_status, "unknown"
                ),
                "model": "snmp-mib2",
                "device_ip": device.management_ip,
            }
            if speed_bps is not None and speed_bps > 0:
                in_value = min(max(in_bps, 0.0), speed_bps)
                out_value = min(max(out_bps, 0.0), speed_bps)
                in_clamped = in_bps > speed_bps
                out_clamped = out_bps > speed_bps
                utilization = min(
                    max(max(in_value, out_value) / speed_bps * 100.0, 0.0), 100.0
                )
                max_utilization = max(max_utilization, utilization)
                speed_field: Optional[float] = speed_bps
            else:
                in_value, out_value = max(in_bps, 0.0), max(out_bps, 0.0)
                in_clamped = out_clamped = False
                utilization = 0.0
                speed_field = None
            metrics.append(
                MetricPoint(
                    device_id=device.id,
                    metric="if_in_bps",
                    value=round(in_value, 2),
                    unit="bps",
                    interface=interface.name,
                    ts=now,
                    source="snmp",
                    speed_bps=speed_field,
                    clamped=in_clamped,
                    tags=dict(tags),
                )
            )
            metrics.append(
                MetricPoint(
                    device_id=device.id,
                    metric="if_out_bps",
                    value=round(out_value, 2),
                    unit="bps",
                    interface=interface.name,
                    ts=now,
                    source="snmp",
                    speed_bps=speed_field,
                    clamped=out_clamped,
                    tags=dict(tags),
                )
            )
            if speed_field is not None:
                metrics.append(
                    MetricPoint(
                        device_id=device.id,
                        metric="if_utilization_pct",
                        value=round(utilization, 2),
                        unit="%",
                        interface=interface.name,
                        ts=now,
                        source="snmp",
                        speed_bps=speed_field,
                        clamped=False,
                        tags=dict(tags),
                    )
                )
        metrics.append(
            MetricPoint(
                device_id=device.id,
                metric="device_utilization_pct",
                value=round(max_utilization, 2),
                unit="%",
                ts=now,
                source="snmp",
                tags={"model": "snmp-mib2", "device_ip": device.management_ip},
            )
        )
        return metrics

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        return {
            "driver": self.name,
            "devices_configured": len(self.device_configs),
            "reachable": self.reachable,
            "last_ok_at": self.last_ok_at,
            "errors": dict(self.device_errors),
            "source": "snmp",
        }


def _platform_from_sys_descr(sys_descr: str) -> Optional[str]:
    """从 sysDescr 里粗解析设备型号（例如 Cisco IOS 的 C2960/C3650/ISR4331）。"""
    if not sys_descr:
        return None
    text = str(sys_descr)
    patterns = (
        r"Cisco\s+([A-Z]{2,}[0-9][A-Za-z0-9/\-]*)",
        r"Cisco\s+IOS.*?,\s*([A-Z0-9\-]+)\s+Software",
        r"Model:\s*([A-Za-z0-9\-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return text[:80] or None
