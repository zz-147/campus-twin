#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PT Network Controller (NC) Real World Access 探针
=================================================
零依赖（仅标准库），用于验证 Packet Tracer 9.x 的 Network Controller
北向 REST API 是否能从 Windows 宿主机（localhost）访问，并抓取原始 JSON
样本，作为后端采集驱动的开发依据。

前置条件（在 Packet Tracer 里操作）：
  1. 打开示例工程：
     C:\\Program Files\\Cisco Packet Tracer 9.0.0\\saves\\02 Infrastructure Automation\\Network Controller\\
  2. Options -> Preferences -> 勾选 "Enable External Access for Network Controller REST API"
  3. 点击拓扑里的 Network Controller 设备 -> Config 标签 -> Real World Access
     - [x] Access Enabled
     - HTTP Port: 58000
     - Server Status 显示正在监听
  4. 如果提示初始设置，创建账号 admin / cisco

用法：
  python tools/netcon_probe.py
  python tools/netcon_probe.py --port 58001
  python tools/netcon_probe.py --user admin --password cisco

输出：
  终端打印各端点状态表；原始 JSON 保存到 data/raw/ 下，按端点命名。
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# NC 的 Real World Access 端口由 PT 里的 NC 设备决定，可能在 58000-58010 之间浮动
PORT_SCAN_RANGE = range(58000, 58011)

DEFAULT_PORT = 0  # 0 表示自动探测
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "cisco"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (路径, 说明)  —— 顺序即探测顺序
ENDPOINTS = [
    ("network-device/count",            "设备数量"),
    ("network-device",                  "设备清单（型号/IP/角色/软件版本）"),
    ("host",                            "主机/终端清单"),
    ("topology/physical-topology",      "物理拓扑（nodes + links）"),
    ("discovery",                       "发现任务"),
    ("global-credential/cli",           "CLI 全局凭据"),
    ("assurance/network-health",        "网络健康总览"),
    ("assurance/health",                "健康明细"),
    ("assurance/health-issues",         "健康问题/告警（核心！）"),
    ("network-health",                  "网络健康（备用路径）"),
]


class NCClient:
    def __init__(self, port, user, password, timeout=10):
        self.base = "http://127.0.0.1:%d/api/v1" % port
        self.user = user
        self.password = password
        self.timeout = timeout
        self.token = None

    def request(self, method, path, payload=None):
        url = self.base + "/" + path.lstrip("/")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if self.token:
            req.add_header("x-auth-token", self.token)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), time.time() - t0
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), time.time() - t0
        except urllib.error.URLError as e:
            return None, "URLError: %s" % (e.reason,), time.time() - t0
        except Exception as e:  # noqa: BLE001
            return None, "%s: %s" % (type(e).__name__, e), time.time() - t0

    def login(self):
        status, body, _ = self.request(
            "POST", "ticket", {"username": self.user, "password": self.password}
        )
        if status not in (200, 201):
            return status, body
        try:
            doc = json.loads(body)
            self.token = doc["response"]["serviceTicket"]
            return status, doc
        except Exception as e:  # noqa: BLE001
            return status, "解析 ticket 失败: %s | 原始响应: %s" % (e, body[:500])


def save_json(out_dir, name, body):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name + ".json")
    try:
        doc = json.loads(body)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        return path, len(doc.get("response", doc)) if isinstance(doc, dict) else len(doc)
    except Exception:  # noqa: BLE001
        with open(path + ".txt", "w", encoding="utf-8") as f:
            f.write(body)
        return path + ".txt", len(body)


def preview(body, limit=220):
    s = " ".join(body.split())
    return s[:limit] + ("..." if len(s) > limit else "")


def main():
    ap = argparse.ArgumentParser(description="PT Network Controller Real World Access 探针")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="NC Real World Access HTTP 端口；0=自动探测 58000-58010")
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "raw"))
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args()

    if args.port <= 0:
        print("自动探测 Network Controller 端口 (58000-58010) ...")
        for p in PORT_SCAN_RANGE:
            try:
                with socket.create_connection(("127.0.0.1", p), timeout=0.3):
                    args.port = p
                    print("  发现监听端口: %d" % p)
                    break
            except OSError:
                continue
        if args.port <= 0:
            print("[X] 未发现任何 NC 监听端口。请确认 PT 已打开且 Real World Access 已启用。")
            return 1

    print("=" * 72)
    print("PT Network Controller 探针  ->  http://127.0.0.1:%d/api/v1" % args.port)
    print("=" * 72)

    client = NCClient(args.port, args.user, args.password, args.timeout)
    status, body = client.login()
    if status not in (200, 201):
        print("[X] 获取 ticket 失败，status=%s" % status)
        print("    响应: %s" % preview(str(body), 400))
        print()
        print("请检查：")
        print("  1. Packet Tracer 是否已打开，且 Network Controller 设备存在")
        print("  2. Options -> Preferences 是否勾选 Enable External Access for Network Controller REST API")
        print("  3. NC 设备 Config -> Real World Access: Access Enabled，HTTP Port = %d" % args.port)
        print("  4. 端口是否被占用（netstat -ano | findstr %d）" % args.port)
        return 1

    ticket_path, _ = save_json(args.out, "ticket", json.dumps(body, ensure_ascii=False))
    print("[OK] ticket 获取成功  serviceTicket=%s" % client.token)
    print("     原始响应已保存: %s" % ticket_path)
    print()
    print("%-28s %-6s %-8s %s" % ("端点", "状态", "耗时(ms)", "预览"))
    print("-" * 72)

    ok_count = 0
    for path, label in ENDPOINTS:
        status, body, elapsed = client.request("GET", path)
        name = path.replace("/", "_")
        if status in (200, 201):
            ok_count += 1
            saved, size = save_json(args.out, name, body)
            print("%-28s %-6s %-8.0f size=%-7s %s" % (path, status, elapsed * 1000,
                                                      size if isinstance(size, int) else "?", preview(body, 120)))
        else:
            print("%-28s %-6s %-8.0f %s" % (path, status if status else "ERR", elapsed * 1000,
                                            preview(str(body), 160)))

    print("-" * 72)
    print("成功 %d/%d 个端点，原始 JSON 保存在: %s" % (ok_count, len(ENDPOINTS), args.out))
    if ok_count:
        print()
        print("下一步：把 data/raw/ 目录发给开发侧，即可据此实现采集驱动与数据模型。")
    return 0 if ok_count else 2


if __name__ == "__main__":
    sys.exit(main())
