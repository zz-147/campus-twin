# -*- coding: utf-8 -*-
"""设备接入知识库：型号规格 / 楼栋规划 / CLI 配置模板（纯静态，无网络）。

onboarding Agent 用它生成：PT 型号选择、端口级连线表、完整 CLI 配置、验证命令、撤销命令；
recon/scene Agent 用它做楼栋/命名/地址规划。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 设备型号
# --------------------------------------------------------------------------- #
DEVICE_SPECS: Dict[str, Dict[str, Any]] = {
    "2960": {
        "key": "2960",
        "label": "Cisco Catalyst 2960-24TT",
        "model": "WS-C2960-24TT-L",
        "kind": "switch",
        "scene_type": "switch",
        "layer3": False,
        "access_port_template": "Fa0/%d",
        "access_port_count": 24,
        "uplink_ports": ["Gi0/1", "Gi0/2"],
        "vlan_name": "USERS",
        "ios": "IOS 15.0(2)SE4",
    },
    "3650": {
        "key": "3650",
        "label": "Cisco Catalyst 3650-24PS",
        "model": "WS-C3650-24PS",
        "kind": "switch",
        "scene_type": "switch",
        "layer3": True,
        "access_port_template": "Gi1/0/%d",
        "access_port_count": 24,
        "uplink_ports": ["Gi1/0/7", "Gi1/0/8"],
        "vlan_name": "USERS",
        "ios": "IOS-XE 16.3.2",
    },
    "4331": {
        "key": "4331",
        "label": "Cisco ISR 4331",
        "model": "ISR4331/K9",
        "kind": "router",
        "scene_type": "router",
        "layer3": True,
        "access_port_template": None,
        "access_port_count": 0,
        "uplink_ports": ["Gi0/0/0", "Gi0/0/1"],
        "vlan_name": None,
        "ios": "IOS-XE 16.6.4",
    },
    "PC": {
        "key": "PC",
        "label": "PC-PT 终端",
        "model": "PC-PT",
        "kind": "pc",
        "scene_type": "pc",
        "layer3": False,
        "access_port_template": "Fa0",
        "access_port_count": 1,
        "uplink_ports": ["Fa0"],
        "vlan_name": "USERS",
        "ios": None,
    },
    "Server": {
        "key": "Server",
        "label": "Server-PT 服务器",
        "model": "Server-PT",
        "kind": "server",
        "scene_type": "server",
        "layer3": False,
        "access_port_template": "Fa0",
        "access_port_count": 1,
        "uplink_ports": ["Fa0"],
        "vlan_name": "SERVER",
        "ios": None,
    },
}

#: 从自然语言里识别型号（顺序敏感：先具体型号，再泛化词）
_DEVICE_DETECT_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("3650", ("3650",)),
    ("4331", ("4331", "isr4331", "isr 4331")),
    ("2960", ("2960",)),
    ("Server", ("服务器", "server")),
    ("PC", ("pc", "电脑", "终端机")),
    ("2960", ("交换机", "switch")),
    ("4331", ("路由器", "router")),
)


def detect_device_kind(text: str) -> Optional[str]:
    """从用户问题中识别要接入的设备型号。"""
    lowered = (text or "").lower()
    for kind, keywords in _DEVICE_DETECT_RULES:
        for keyword in keywords:
            if keyword in lowered:
                return kind
    return None


# --------------------------------------------------------------------------- #
# 楼栋规划（VLAN / 网段 / 命名规则）
# --------------------------------------------------------------------------- #
BUILDING_PROFILES: Dict[str, Dict[str, Any]] = {
    "中心机房": {
        "vlan": 10,
        "vlan_name": "MGMT",
        "gateway": "192.168.10.1",
        "mgmt_segment": "192.168.10.",
        "hostname_prefix": "CORE",
        "keywords": ("中心机房", "机房", "核心", "core"),
    },
    "教学楼": {
        "vlan": 20,
        "vlan_name": "TEACHING",
        "gateway": "192.168.20.1",
        "mgmt_segment": "192.168.10.",
        "hostname_prefix": "SW-JX",
        "keywords": ("教学楼", "教学", "jx"),
    },
    "实训楼": {
        "vlan": 30,
        "vlan_name": "TRAINING",
        "gateway": "192.168.30.1",
        "mgmt_segment": "192.168.10.",
        "hostname_prefix": "SW-SX",
        "keywords": ("实训楼", "实训", "sx"),
    },
    "宿舍楼": {
        "vlan": 40,
        "vlan_name": "DORM",
        "gateway": "192.168.40.1",
        "mgmt_segment": "192.168.10.",
        "hostname_prefix": "SW-SS",
        "keywords": ("宿舍楼", "宿舍", "ss"),
    },
}

#: 新楼栋命名/配色（scene Agent 生成楼栋时使用）
NEW_BUILDING_PRESETS: Dict[str, Dict[str, Any]] = {
    "图书馆": {"slug": "lib", "hostname_prefix": "SW-LIB", "vlan": 60, "color": "#2f5d50"},
    "体育馆": {"slug": "gym", "hostname_prefix": "SW-GYM", "vlan": 70, "color": "#5d4b2f"},
    "实验楼": {"slug": "lab", "hostname_prefix": "SW-LAB", "vlan": 80, "color": "#3d4f6b"},
    "综合楼": {"slug": "zh", "hostname_prefix": "SW-ZH", "vlan": 90, "color": "#4b3d5d"},
}

#: 常见中文楼栋名 → 真实后端 scene 的楼栋 id（与后端默认 seed 对齐）
BUILDING_ID_MAP: Dict[str, str] = {
    "中心机房": "core",
    "教学楼": "teach",
    "实训楼": "train",
    "宿舍楼": "dorm",
    "图书馆": "bld-lib",
    "体育馆": "bld-gym",
}

#: 场景里的型号展示名（与后端 seed 的 model 字段风格一致）
SCENE_MODEL_BY_KIND: Dict[str, str] = {
    "2960": "Catalyst 2960",
    "3650": "Catalyst 3650",
    "4331": "ISR 4331",
    "PC": "PC-PT",
    "Server": "Server-PT",
}


def scene_model(kind_key: str) -> str:
    return SCENE_MODEL_BY_KIND.get(kind_key, str(DEVICE_SPECS.get(kind_key, {}).get("model") or "unknown"))

_SCENE_TYPE_LABELS = {
    "switch": "交换机",
    "router": "路由器",
    "server": "服务器",
    "pc": "终端",
    "nc": "网络控制器",
}


def scene_type_label(scene_type: str) -> str:
    return _SCENE_TYPE_LABELS.get(scene_type, scene_type or "设备")


def detect_building_name(text: str) -> Optional[str]:
    """从问题里识别目标楼栋（含"新建一栋 X"的新楼栋名）。"""
    lowered = (text or "").lower()
    for name, profile in BUILDING_PROFILES.items():
        for keyword in profile.get("keywords") or ():
            if keyword and keyword in lowered:
                return name
    for name in NEW_BUILDING_PRESETS:
        if name in (text or ""):
            return name
    # "新建一栋图书馆 / 在校园东侧新建体育馆" → 抓取楼/馆/中心名
    match = re.search(
        r"(?:新建|新增|加|建)(?:一栋|一个|1栋|一座)?\s*([\u4e00-\u9fa5]{2,8}?(?:楼|馆|中心|厂房))",
        text or "",
    )
    if match:
        candidate = match.group(1)
        candidate = re.sub(r"^(?:一栋|一个|一座)", "", candidate)
        return candidate
    return None


def building_profile(name: str) -> Dict[str, Any]:
    """已知楼栋返回固定规划；新楼栋按预设/推导生成规划。"""
    if name in BUILDING_PROFILES:
        profile = dict(BUILDING_PROFILES[name])
        profile.update({"name": name, "is_new": False})
        profile["building_id"] = BUILDING_ID_MAP.get(name, "bld-" + profile["hostname_prefix"].lower())
        return profile
    preset = NEW_BUILDING_PRESETS.get(name, {})
    vlan = int(preset.get("vlan") or 60)
    prefix = preset.get("hostname_prefix") or ("SW-" + _short_ascii(name, "NEW"))
    profile = {
        "name": name,
        "is_new": True,
        "vlan": vlan,
        "vlan_name": "USERS",
        "gateway": "192.168.%d.1" % vlan,
        "mgmt_segment": "192.168.10.",
        "hostname_prefix": prefix,
        "building_id": BUILDING_ID_MAP.get(name) or ("bld-" + _short_ascii(name, "new").lower()),
        "color": preset.get("color") or "#2a4b62",
    }
    return profile

def _short_ascii(name: str, fallback: str) -> str:
    letters = "".join(char for char in name if char.isascii() and char.isalnum())
    return letters.upper() if letters else fallback


# --------------------------------------------------------------------------- #
# 地址 / 命名 / 端口规划
# --------------------------------------------------------------------------- #
def next_hostname(existing_hostnames: List[str], prefix: str) -> str:
    """生成下一个可用 hostname：SW-SS-2961 / Core-3651 这类递增规则。"""
    pattern = re.compile(r"^%s-(\d+)$" % re.escape(prefix), re.IGNORECASE)
    numbers = [
        int(match.group(1))
        for match in (pattern.match(str(name)) for name in existing_hostnames)
        if match
    ]
    if numbers:
        return "%s-%d" % (prefix, max(numbers) + 1)
    if prefix.upper().startswith("CORE"):
        return "%s-1" % prefix
    return "%s-1" % prefix if "-" in prefix else "%s-1" % prefix


def next_management_ip(existing_ips: List[str], segment: str = "192.168.10.", start: int = 14) -> str:
    used = {str(ip) for ip in existing_ips if str(ip).startswith(segment)}
    for octet in range(start, 100):
        candidate = "%s%d" % (segment, octet)
        if candidate not in used:
            return candidate
    return "%s%d" % (segment, 99)


def used_uplink_ports(topology_links: List[Dict[str, Any]], device_name: str) -> List[str]:
    used: List[str] = []
    for link in topology_links or []:
        if str(link.get("source_label") or "") == device_name:
            used.append(str(link.get("start_port_name") or ""))
        if str(link.get("target_label") or "") == device_name:
            used.append(str(link.get("end_port_name") or ""))
    return [port for port in used if port]


def choose_uplink_port(used: List[str], candidate_ports: List[str]) -> Tuple[str, bool]:
    """选择空闲上联端口；返回 (端口, 是否仍有空闲)。"""
    for port in candidate_ports:
        if port not in used:
            return port, True
    return candidate_ports[0], False


def next_remote_port(
    existing_links: List[Dict[str, Any]], remote_device: str, remote_ports: List[str]
) -> Tuple[str, bool]:
    used: List[str] = []
    for link in existing_links or []:
        if str(link.get("source_label") or "") == remote_device:
            used.append(str(link.get("start_port_name") or ""))
        if str(link.get("target_label") or "") == remote_device:
            used.append(str(link.get("end_port_name") or ""))
    for port in remote_ports:
        if port not in used:
            return port, True
    return remote_ports[0], False


# --------------------------------------------------------------------------- #
# CLI 配置模板
# --------------------------------------------------------------------------- #
def render_access_port_block(port: str, vlan: int) -> str:
    return "\n".join(
        [
            "interface %s" % port,
            " switchport mode access",
            " switchport access vlan %d" % vlan,
            " spanning-tree portfast",
            " no shutdown",
            "exit",
        ]
    )


def render_cli(
    kind_key: str,
    hostname: str,
    vlan: int,
    vlan_name: str,
    mgmt_ip: str,
    gateway: str,
    uplink_local_port: str,
    access_ports: Optional[List[str]] = None,
) -> str:
    """生成完整 Cisco IOS 配置（按型号分支）。"""
    spec = DEVICE_SPECS.get(kind_key) or DEVICE_SPECS["2960"]
    lines: List[str] = ["enable", "configure terminal", "hostname %s" % hostname, "!"]

    if spec["kind"] == "switch" and not spec.get("layer3"):
        lines.extend(
            [
                "vlan %d" % vlan,
                " name %s" % vlan_name,
                "exit",
                "!",
                "interface Vlan%d" % vlan,
                " ip address %s 255.255.255.0" % mgmt_ip,
                " no shutdown",
                "exit",
                "ip default-gateway %s" % gateway,
                "!",
            ]
        )
    elif spec["kind"] == "switch" and spec.get("layer3"):
        lines.extend(
            [
                "ip routing",
                "!",
                "vlan %d" % vlan,
                " name %s" % vlan_name,
                "exit",
                "!",
                "interface Vlan%d" % vlan,
                " ip address %s 255.255.255.0" % mgmt_ip,
                " no shutdown",
                "exit",
                "!",
            ]
        )
    elif spec["kind"] == "router":
        lines.extend(
            [
                "interface %s" % uplink_local_port,
                " no shutdown",
                "exit",
                "!",
            ]
        )

    lines.extend(
        [
            "interface %s" % uplink_local_port,
            " description Uplink-to-%s" % ("core" if spec["kind"] != "router" else "core"),
        ]
    )
    if spec["kind"] == "switch":
        lines.extend(
            [
                " switchport mode trunk",
                " switchport trunk allowed vlan %d" % vlan,
                " no shutdown",
                "exit",
                "!",
            ]
        )
    else:
        lines.extend([" no shutdown", "exit", "!"])

    for port in access_ports or []:
        lines.append(render_access_port_block(port, vlan))
        lines.append("!")

    if spec.get("layer3"):
        lines.extend(
            [
                "ip route 0.0.0.0 0.0.0.0 %s" % gateway,
                "!",
            ]
        )

    lines.extend(
        [
            "line vty 0 4",
            " login local",
            " transport input ssh",
            "exit",
            "!",
            "end",
            "write memory",
        ]
    )
    return "\n".join(lines)


def render_verification(kind_key: str, hostname: str) -> List[str]:
    spec = DEVICE_SPECS.get(kind_key) or DEVICE_SPECS["2960"]
    commands = [
        "show running-config | include hostname|vlan|ip address",
        "show ip interface brief",
        "show interfaces status",
    ]
    if spec["kind"] in ("switch", "router"):
        commands.extend(["show cdp neighbors", "show logging | include LINK|UPDOWN|CHANGED"])
    commands.append("ping %s" % ("192.168.10.1" if kind_key != "4331" else "10.0.0.1"))
    return commands


def render_undo(kind_key: str, hostname: str, vlan: int) -> List[str]:
    return [
        "conf t",
        " no vlan %d" % vlan if kind_key in ("2960", "3650") else " no ip route 0.0.0.0 0.0.0.0",
        " end",
        "write memory",
        "# 若设备已接入 PT，删除前先在场景里执行 remove_scene_device action 撤销 3D 位置",
    ]


def render_device_access_steps(hostname: str, building_name: str) -> List[str]:
    """PC/Server 这类终端在 PT 中的配置步骤（GUI）。"""
    return [
        "1. 在 PT 中拖入 %s，并用直通线连接到接入交换机的空闲 Access 端口。" % hostname,
        "2. 打开设备 Config → FastEthernet0 → IP Configuration，按楼栋网段填写 IP/掩码/网关。",
        "3. 在接入交换机上确认端口 VLAN 与端口状态：show vlan brief / show interface status。",
        "4. 从终端 ping 网关验证连通性，再到大屏确认设备自动发现和 3D 场景位置。",
        "5. 若归属楼栋推断错误，可在对话里说“把 %s 挪到 X 楼”或使用场景确认/撤销按钮。" % hostname,
    ]


def format_cabling_markdown(cabling: List[Dict[str, Any]]) -> str:
    header = "| 本端设备 | 本端端口 | 对端设备 | 对端端口 | 线缆类型 | 用途 |\n|---|---|---|---|---|---|"
    rows = [
        "| %s | %s | %s | %s | %s | %s |"
        % (
            item.get("local_device"),
            item.get("local_port"),
            item.get("peer_device"),
            item.get("peer_port"),
            item.get("cable"),
            item.get("purpose"),
        )
        for item in cabling
    ]
    return "\n".join([header] + rows)


#: 规划服务网段（供 onboarding 生成终端地址）
def terminal_segment(building_name: str) -> str:
    profile = BUILDING_PROFILES.get(building_name)
    if profile:
        gateway = profile["gateway"]
        return gateway.rsplit(".", 1)[0] + "."
    p = building_profile(building_name)
    return p["gateway"].rsplit(".", 1)[0] + "."


def available_scene_type(kind_key: str) -> str:
    spec = DEVICE_SPECS.get(kind_key) or DEVICE_SPECS["2960"]
    return str(spec.get("scene_type") or "switch")
