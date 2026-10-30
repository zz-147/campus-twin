# -*- coding: utf-8 -*-
"""W10 路径追踪服务（基于 NC ``/flow-analysis``）。

契约（docs/W10-路径追踪告警聚合设计.md §一）：
``POST /api/path-trace {source_ip, dest_ip}`` → 200
``{ok, source_ip, dest_ip, trace_status, hops[], links[], elapsed_ms, error}``。

实测 PT 9.0 NC 响应形态（2026-10-30）::

    POST /flow-analysis {"sourceIP","destIP"}
      → {"response":{"flowAnalysisId":"0A8QN...","url":"/flow-analysis/..."},"version":"1"}
    GET /flow-analysis/{id}
      → {"response":{
             "request":{"status":"COMPLETED","sourceIP":...,"destIP":...},
             "networkElementsInfo":[
                {"name":"PC-JX1","id":"PTT...","ip":"192.168.20.10","type":"Pc",
                 "ingressInterface":null,"egressInterface":null},
                {"name":"Core-3650","id":"CAT...","ip":"192.168.20.1","type":"MultiLayerSwitch",
                 "ingressInterface":{"physicalInterface":{"name":"Vlan20"}},
                 "egressInterface":{"physicalInterface":{"name":"GigabitEthernet1/0/5"}}},
                {"name":"Server-1","id":"PTT...","ip":"192.168.50.10","type":"Server",
                 "ingressInterface":{"physicalInterface":{"name":"UNKNOWN"}},"egressInterface":null}
             ]},"version":"1.0"}

PT 的路径追踪有已知限制：只返回部分中间设备/端口（官方文档明确说明），因此
归一化时会把缺失的源/目的端补成合成 hop，链路匹配不到拓扑时给 ``synthetic=true``，
供前端按 hop 顺序画高亮路径。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import time
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple

from app.collectors.netcon import NetconError
from app.config import Settings

logger = logging.getLogger("campus_twin.path_trace")


def _normalize_name(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _port_name(iface: Any) -> Optional[str]:
    """从 NC 的 ingressInterface/egressInterface 结构里取端口名。"""
    if not isinstance(iface, dict):
        return None
    physical = iface.get("physicalInterface")
    if isinstance(physical, dict) and physical.get("name"):
        return str(physical["name"])
    for key in ("name", "interfaceName", "interface", "portName"):
        if iface.get(key):
            return str(iface[key])
    return None


def _extract_elements(raw: Any) -> List[Dict[str, Any]]:
    """从 NC 响应中提取 networkElementsInfo 列表（容忍多种包装）。"""
    if not isinstance(raw, dict):
        return []
    response = raw.get("response")
    if isinstance(response, dict):
        elements = response.get("networkElementsInfo") or response.get(
            "networkElements"
        )
        if isinstance(elements, list):
            return [item for item in elements if isinstance(item, dict)]
        return []
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    elements = raw.get("networkElementsInfo")
    if isinstance(elements, list):
        return [item for item in elements if isinstance(item, dict)]
    return []


def _build_path_graph(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """构图：topology links（设备↔设备）+ hosts 接入关系（host↔接入设备）。

    返回:
    - ``nodes``: node_id -> {name, kind: "device"/"host", ip}
    - ``adjacency``: node_id -> set(neighbor_id)
    - ``edges``: (from_id, to_id) -> {kind, link_id, from_port, to_port}
    - ``name_to_id`` / ``ip_to_id``: 用于把 NC hop 解析成图节点
    """
    devices = snapshot.get("devices") or []
    hosts = snapshot.get("hosts") or []
    topology = snapshot.get("topology") or {}
    topology_links = topology.get("links") or []

    nodes: Dict[str, Dict[str, Any]] = {}
    adjacency: Dict[str, Set[str]] = {}
    edges: Dict[Tuple[str, str], Dict[str, Any]] = {}
    name_to_id: Dict[str, str] = {}
    ip_to_id: Dict[str, str] = {}

    def add_edge(
        from_id: str,
        to_id: str,
        *,
        kind: str,
        link_id: Optional[str],
        from_port: Optional[str],
        to_port: Optional[str],
    ) -> None:
        adjacency.setdefault(from_id, set()).add(to_id)
        adjacency.setdefault(to_id, set()).add(from_id)
        edges[(from_id, to_id)] = {
            "kind": kind,
            "link_id": link_id,
            "from_port": from_port,
            "to_port": to_port,
        }

    for device in devices:
        if not isinstance(device, dict):
            continue
        device_id = str(device.get("id") or "")
        if not device_id:
            continue
        nodes[device_id] = {
            "name": str(device.get("hostname") or device.get("management_ip") or device_id),
            "kind": "device",
            "ip": device.get("management_ip"),
        }
        name = _normalize_name(device.get("hostname"))
        if name:
            name_to_id[name] = device_id
        if device.get("management_ip"):
            ip_to_id[str(device["management_ip"])] = device_id

    device_id_by_name = dict(name_to_id)
    for host in hosts:
        if not isinstance(host, dict):
            continue
        host_id = str(host.get("id") or "")
        if not host_id:
            continue
        nodes[host_id] = {
            "name": str(host.get("hostname") or host.get("ip") or host_id),
            "kind": "host",
            "ip": host.get("ip"),
        }
        name = _normalize_name(host.get("hostname"))
        if name:
            name_to_id.setdefault(name, host_id)
        if host.get("ip"):
            ip_to_id.setdefault(str(host["ip"]), host_id)
        attached_device = None
        connected_device_id = host.get("connected_device_id")
        if connected_device_id and str(connected_device_id) in nodes:
            attached_device = str(connected_device_id)
        elif host.get("connected_device_name"):
            attached_device = device_id_by_name.get(
                _normalize_name(host.get("connected_device_name"))
            )
        if attached_device:
            host_port = str(host.get("connected_interface") or "") or None
            add_edge(
                host_id,
                attached_device,
                kind="host_attachment",
                link_id=None,
                from_port=host_port,
                to_port=None,
            )
            # 反向边：设备侧端口 NC /host 不提供，未知时留空（展示为 UNKNOWN）
            edges[(attached_device, host_id)] = {
                "kind": "host_attachment",
                "link_id": None,
                "from_port": None,
                "to_port": host_port,
            }

    for link in topology_links:
        if not isinstance(link, dict):
            continue
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if source not in nodes or target not in nodes:
            continue
        start_port = str(link.get("start_port_name") or "") or None
        end_port = str(link.get("end_port_name") or "") or None
        link_id = str(link.get("id") or "") or None
        add_edge(
            source,
            target,
            kind="topology",
            link_id=link_id,
            from_port=start_port,
            to_port=end_port,
        )
        edges[(target, source)] = {
            "kind": "topology",
            "link_id": link_id,
            "from_port": end_port,
            "to_port": start_port,
        }
    return {
        "nodes": nodes,
        "adjacency": adjacency,
        "edges": edges,
        "name_to_id": name_to_id,
        "ip_to_id": ip_to_id,
    }


def _resolve_hop_node(hop: Dict[str, Any], graph: Dict[str, Any]) -> Optional[str]:
    """把 hop 解析成图节点 id（优先 device_id，其次设备名，再次 IP）。"""
    nodes = graph["nodes"]
    device_id = hop.get("device_id")
    if device_id and str(device_id) in nodes:
        return str(device_id)
    name = _normalize_name(hop.get("device_name"))
    if name and name in graph["name_to_id"]:
        return graph["name_to_id"][name]
    ip = hop.get("ip")
    if ip and str(ip) in graph["ip_to_id"]:
        return graph["ip_to_id"][str(ip)]
    return None


def _bfs_path(graph: Dict[str, Any], start: str, goal: str) -> Optional[List[str]]:
    """图上 BFS 最短路（确定性：邻居按 id 排序）；找不到返回 None。"""
    if start == goal:
        return [start]
    adjacency = graph["adjacency"]
    visited: Set[str] = {start}
    queue: deque = deque([[start]])
    max_depth = 8  # 校园网场景足够；防止异常拓扑导致长搜索
    while queue:
        path = queue.popleft()
        if len(path) - 1 >= max_depth:
            continue
        for neighbor in sorted(adjacency.get(path[-1], ())):
            if neighbor in visited:
                continue
            new_path = path + [neighbor]
            if neighbor == goal:
                return new_path
            visited.add(neighbor)
            queue.append(new_path)
    return None


def _expand_hops_with_topology(
    hops: List[Dict[str, Any]],
    graph: Dict[str, Any],
    warnings: List[str],
) -> Tuple[List[Dict[str, Any]], int]:
    """对相邻 hops 做拓扑补全：A、B 不直接相邻时用 BFS 最短路插入中间设备。"""
    if not graph["nodes"]:
        return hops, 0
    result: List[Dict[str, Any]] = []
    inserted_count = 0
    for hop in hops:
        hop.setdefault("synthetic", False)
        if not result:
            result.append(hop)
            continue
        previous = result[-1]
        previous_id = _resolve_hop_node(previous, graph)
        current_id = _resolve_hop_node(hop, graph)
        if previous_id is None or current_id is None:
            warnings.append(
                "hop {} → {} 缺少匹配的拓扑节点，无法自动补全中间设备".format(
                    previous.get("device_name"), hop.get("device_name")
                )
            )
            result.append(hop)
            continue
        if current_id in graph["adjacency"].get(previous_id, set()):
            # 已是直接相邻（真实拓扑链路或主机接入），无需补全
            result.append(hop)
            continue
        path = _bfs_path(graph, previous_id, current_id)
        if path is None or len(path) <= 2:
            warnings.append(
                "hop {} → {} 之间拓扑不可达，无法补全中间设备（保持 NC 原始结果）".format(
                    previous.get("device_name"), hop.get("device_name")
                )
            )
            result.append(hop)
            continue
        for path_index in range(1, len(path) - 1):
            node_id = path[path_index]
            node = graph["nodes"].get(node_id) or {}
            ingress_edge = graph["edges"].get((path[path_index - 1], node_id)) or {}
            egress_edge = graph["edges"].get((node_id, path[path_index + 1])) or {}
            inferred_hop = {
                "index": len(result),
                "device_name": node.get("name") or node_id,
                "device_id": node_id,
                "ip": node.get("ip"),
                "ingress_port": ingress_edge.get("to_port") or "UNKNOWN",
                "egress_port": egress_edge.get("from_port") or "UNKNOWN",
                "role": "transit",
                "node_type": node.get("kind") or "device",
                "reachability": None,
                "inferred": True,
                "synthetic": True,
                "source": "topology_inference",
                "topology_rule": "bfs_shortest_path",
            }
            warnings.append(
                "中间设备 {} 由拓扑推断补全（NC 未返回）".format(
                    inferred_hop["device_name"]
                )
            )
            result.append(inferred_hop)
            inserted_count += 1
        result.append(hop)
    return result, inserted_count


def normalize_flow_analysis(
    raw: Any,
    source_ip: str,
    dest_ip: str,
    snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把 NC flow-analysis 结果归一化成 hops/links 契约结构。"""
    snapshot = snapshot or {}
    devices = snapshot.get("devices") or []
    hosts = snapshot.get("hosts") or []
    topology = snapshot.get("topology") or {}
    topology_links = topology.get("links") or []

    host_by_ip: Dict[str, Dict[str, Any]] = {}
    device_by_ip: Dict[str, Dict[str, Any]] = {}
    device_by_id: Dict[str, Dict[str, Any]] = {}
    device_by_name: Dict[str, Dict[str, Any]] = {}
    for host in hosts:
        if isinstance(host, dict):
            if host.get("ip"):
                host_by_ip[str(host["ip"])] = host
    for device in devices:
        if isinstance(device, dict):
            if device.get("management_ip"):
                device_by_ip[str(device["management_ip"])] = device
            if device.get("id"):
                device_by_id[str(device["id"])] = device
            name = _normalize_name(device.get("hostname"))
            if name:
                device_by_name[name] = device

    warnings_seed: List[str] = []
    # ---------- 1) 原始 hop ----------
    hops: List[Dict[str, Any]] = []
    elements = _extract_elements(raw)
    for index, element in enumerate(elements):
        name = (
            element.get("name")
            or element.get("hostname")
            or element.get("deviceName")
            or element.get("ip")
            or element.get("id")
        )
        device_id = element.get("id")
        device = None
        if device_id and str(device_id) in device_by_id:
            device = device_by_id[str(device_id)]
        elif name and _normalize_name(name) in device_by_name:
            device = device_by_name[_normalize_name(name)]
            device_id = device.get("id")
        hops.append(
            {
                "index": index,
                "device_name": str(name or device_id or "unknown"),
                "device_id": str(device_id) if device_id else None,
                "ip": element.get("ip"),
                "ingress_port": _port_name(element.get("ingressInterface")),
                "egress_port": _port_name(element.get("egressInterface")),
                "role": "transit",
                "node_type": element.get("type"),
                "reachability": (device or {}).get("reachability"),
                "synthetic": False,
                "source": "nc_flow_analysis",
            }
        )

    # ---------- 2) 补齐源/目的端（PT 有时不返回终端，或只返回网络设备）----------
    def _host_hop(ip: str, role: str) -> Optional[Dict[str, Any]]:
        host = host_by_ip.get(str(ip))
        if host is not None:
            return {
                "device_name": host.get("hostname") or ip,
                # device_id 用 host 自身 id（与 NC flow-analysis 的终端元素一致），
                # 接入设备由 graph 的 host_attachment 边推断，不能混用。
                "device_id": host.get("id"),
                "attached_device_id": host.get("connected_device_id"),
                "ip": ip,
                "ingress_port": None,
                "egress_port": host.get("connected_interface"),
                "role": role,
                "node_type": "host",
                "reachability": None,
                "synthetic": False,
                "source": "snapshot_host",
            }
        device = device_by_ip.get(str(ip))
        if device is not None:
            return {
                "device_name": device.get("hostname") or ip,
                "device_id": device.get("id"),
                "ip": ip,
                "ingress_port": None,
                "egress_port": None,
                "role": role,
                "node_type": "device",
                "reachability": device.get("reachability"),
                "synthetic": False,
                "source": "snapshot_device",
            }
        return None

    def _hop_matches_ip(hop: Dict[str, Any], ip: str) -> bool:
        return str(hop.get("ip") or "") == str(ip)

    if not hops or not _hop_matches_ip(hops[0], source_ip):
        source_hop = _host_hop(source_ip, "source")
        if source_hop is not None:
            hops.insert(0, source_hop)
    if not hops or not _hop_matches_ip(hops[-1], dest_ip):
        dest_hop = _host_hop(dest_ip, "destination")
        if dest_hop is not None:
            hops.append(dest_hop)

    # ---------- 2.5) 拓扑推断补全（W10.1：NC flow-analysis 会漏中间设备） ----------
    graph = _build_path_graph(snapshot)
    hops, synthetic_hop_count = _expand_hops_with_topology(hops, graph, warnings_seed)
    # 补全后重排 index
    for index, hop in enumerate(hops):
        hop["index"] = index

    # 角色：首跳=source，末跳=destination，其余 transit；再按 IP 精修
    for index, hop in enumerate(hops):
        hop["index"] = index
        if _hop_matches_ip(hop, source_ip):
            hop["role"] = "source"
        elif _hop_matches_ip(hop, dest_ip):
            hop["role"] = "destination"
        elif index == 0:
            hop["role"] = "source"
        elif index == len(hops) - 1:
            hop["role"] = "destination"
        else:
            hop["role"] = "transit"

    # ---------- 3) links（相邻 hop；优先匹配真实拓扑链路）----------
    links: List[Dict[str, Any]] = []
    for index, (previous, current) in enumerate(zip(hops, hops[1:])):
        previous_node = _resolve_hop_node(previous, graph)
        current_node = _resolve_hop_node(current, graph)
        graph_edge = (
            graph["edges"].get((previous_node, current_node))
            if previous_node and current_node
            else None
        )
        previous_ids = {
            value
            for value in (previous.get("device_id"), previous.get("device_name"))
            if value
        }
        current_ids = {
            value
            for value in (current.get("device_id"), current.get("device_name"))
            if value
        }
        matched = None
        for link in topology_links:
            if not isinstance(link, dict):
                continue
            source_id = str(link.get("source") or "")
            target_id = str(link.get("target") or "")
            if (source_id in previous_ids and target_id in current_ids) or (
                source_id in current_ids and target_id in previous_ids
            ):
                matched = link
                break
        if matched is not None:
            reason = "topology"
        elif graph_edge is not None and graph_edge.get("kind") == "host_attachment":
            reason = "host_attachment"
        else:
            reason = "hop_sequence"
        source_port = previous.get("egress_port") or (
            graph_edge.get("from_port") if graph_edge is not None else None
        )
        target_port = current.get("ingress_port") or (
            graph_edge.get("to_port") if graph_edge is not None else None
        )
        if target_port == "UNKNOWN" and graph_edge is not None:
            target_port = graph_edge.get("to_port") or target_port
        links.append(
            {
                "source": previous.get("device_id") or previous.get("device_name"),
                "target": current.get("device_id") or current.get("device_name"),
                "source_name": previous.get("device_name"),
                "target_name": current.get("device_name"),
                "link_id": (
                    matched.get("id") if matched is not None else "trace-hop-{}".format(index)
                ),
                "synthetic": matched is None,
                "reason": reason,
                "source_port": source_port,
                "target_port": target_port,
            }
        )

    # ---------- 4) 状态 ----------
    warnings: List[str] = list(warnings_seed)
    if not elements:
        status = "failed"
        warnings.append("NC 返回的 networkElementsInfo 为空")
    elif len(hops) < 2:
        status = "partial"
        warnings.append("hop 数量不足 2，路径不完整")
    else:
        status = "complete"
    if hops and hops[0].get("role") != "source":
        status = "partial"
        warnings.append("未识别到源端 hop")
    if hops and hops[-1].get("role") != "destination":
        status = "partial"
        warnings.append("未识别到目的端 hop")
    if any(link["synthetic"] for link in links):
        warnings.append(
            "部分 hop 间无真实拓扑链路（主机接入/推断段），前端按 hop 顺序连线"
        )
    if hops and hops[0].get("ip") and str(hops[0]["ip"]) != str(source_ip):
        status = "partial"
        warnings.append("NC 返回的首跳 IP 与请求源 IP 不一致（可能路径不完整）")
    if hops and hops[-1].get("ip") and str(hops[-1]["ip"]) != str(dest_ip):
        status = "partial"
        warnings.append("NC 返回的末跳 IP 与请求目的 IP 不一致（可能路径不完整）")

    return {
        "trace_status": status,
        "hops": hops,
        "links": links,
        "raw_element_count": len(elements),
        "synthetic_hop_count": synthetic_hop_count,
        "warnings": warnings,
    }


class PathTraceService:
    """路径追踪服务：NC flow-analysis + 2 分钟缓存 + 并发去重。

    - 同一 (source,dest) 的并发请求只真正打一次 NC（per-key asyncio.Lock）；
    - 结果缓存 ``path_trace_cache_seconds``（默认 120s）；
    - 不阻塞采集主循环：由 API 请求触发，NC 调用受 ``path_trace_timeout`` 限制。
    """

    def __init__(self, driver: Any, settings: Settings) -> None:
        self.driver = driver
        self.settings = settings
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    @staticmethod
    def _cache_key(source_ip: str, dest_ip: str) -> str:
        return "{}|{}".format(source_ip.strip(), dest_ip.strip())

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def clear_cache(self) -> None:
        self._cache.clear()

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def _failure(
        self, source_ip: str, dest_ip: str, error: str, elapsed_ms: int
    ) -> Dict[str, Any]:
        return {
            "ok": False,
            "source_ip": source_ip,
            "dest_ip": dest_ip,
            "trace_status": "failed",
            "hops": [],
            "links": [],
            "elapsed_ms": elapsed_ms,
            "flow_analysis_id": None,
            "cached": False,
            "error": error,
        }

    async def trace(
        self,
        source_ip: str,
        dest_ip: str,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        source_ip = str(source_ip).strip()
        dest_ip = str(dest_ip).strip()
        if not source_ip or not dest_ip:
            return self._failure(source_ip, dest_ip, "source_ip/dest_ip 不能为空", 0)
        if source_ip == dest_ip:
            return self._failure(
                source_ip, dest_ip, "源 IP 与目的 IP 相同，无需追踪", 0
            )

        key = self._cache_key(source_ip, dest_ip)
        ttl = float(self.settings.path_trace_cache_seconds)
        cached = self._cache.get(key)
        if cached is not None and time.time() - cached["cached_at"] < ttl:
            result = copy.deepcopy(cached["result"])
            result["cached"] = True
            return result

        flow_analysis = getattr(self.driver, "flow_analysis", None)
        if not callable(flow_analysis):
            return self._failure(
                source_ip,
                dest_ip,
                "当前驱动不支持路径追踪（需要 DRIVER=netcon 且 PT NC 在线）",
                0,
            )

        lock = self._lock(key)
        async with lock:
            # 等锁期间可能已被其它请求填充缓存
            cached = self._cache.get(key)
            if cached is not None and time.time() - cached["cached_at"] < ttl:
                result = copy.deepcopy(cached["result"])
                result["cached"] = True
                return result

            started = time.monotonic()
            try:
                flow_result = await flow_analysis(source_ip, dest_ip)
            except NetconError as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return self._failure(
                    source_ip, dest_ip, "NC 路径追踪失败：{}".format(exc), elapsed_ms
                )
            except Exception as exc:  # noqa: BLE001 - 路径追踪不能影响服务主流程
                logger.exception("路径追踪异常")
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return self._failure(
                    source_ip, dest_ip, "路径追踪异常：{}".format(exc), elapsed_ms
                )

            elapsed_ms = int((time.monotonic() - started) * 1000)
            if not isinstance(flow_result, dict) or not flow_result.get("ok"):
                return self._failure(
                    source_ip,
                    dest_ip,
                    str((flow_result or {}).get("error") or "NC 路径追踪失败"),
                    elapsed_ms,
                )

            normalized = normalize_flow_analysis(
                flow_result.get("raw"), source_ip, dest_ip, snapshot
            )
            result = {
                "ok": True,
                "source_ip": source_ip,
                "dest_ip": dest_ip,
                "trace_status": normalized["trace_status"],
                "hops": normalized["hops"],
                "links": normalized["links"],
                "raw_element_count": normalized["raw_element_count"],
                "synthetic_hop_count": normalized.get("synthetic_hop_count", 0),
                "warnings": normalized["warnings"],
                "elapsed_ms": elapsed_ms,
                "flow_analysis_id": flow_result.get("flow_analysis_id"),
                "cached": False,
                "error": None,
            }
            self._cache[key] = {"cached_at": time.time(), "result": result}
            # 仅保留最近 64 条缓存，避免长期运行内存膨胀
            if len(self._cache) > 64:
                oldest = sorted(
                    self._cache.items(), key=lambda item: item[1]["cached_at"]
                )[: len(self._cache) - 64]
                for old_key, _ in oldest:
                    self._cache.pop(old_key, None)
            logger.info(
                "路径追踪完成：%s -> %s（%s，%d hops，%dms）",
                source_ip,
                dest_ip,
                result["trace_status"],
                len(result["hops"]),
                elapsed_ms,
            )
            return result


def result_digest(result: Dict[str, Any]) -> str:
    """调试用：返回路径结果的短摘要。"""
    return hashlib.sha1(
        "{}".format([(hop.get("device_name"), hop.get("role")) for hop in result.get("hops", [])]).encode(
            "utf-8"
        )
    ).hexdigest()[:10]
