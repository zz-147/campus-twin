#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PT Network Controller 一键初始化 / 设备发现
==========================================
零依赖（仅标准库）。功能：
  1. 登录取 ticket
  2. 检查/创建 CLI 全局凭据（username/password/enablePassword）
  3. 发起 CDP 发现（种子 IP 默认 192.168.10.1 = 核心交换机）
  4. 轮询发现进度直到 Complete
  5. 打印最终设备清单 / 主机 / 拓扑 / 健康问题

用法：
  python tools/netcon_bootstrap.py                      # 自动探测端口，默认种子 192.168.10.1
  python tools/netcon_bootstrap.py --port 58000
  python tools/netcon_bootstrap.py --seed 192.168.10.1 --cdp-level 2
  python tools/netcon_bootstrap.py --no-discovery       # 只登录 + 准备凭据，不发现
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

PORT_SCAN_RANGE = range(58000, 58011)
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "cisco"
DEFAULT_SEED = "192.168.10.1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NC:
    def __init__(self, port, user, password, timeout=10):
        self.base = "http://127.0.0.1:%d/api/v1" % port
        self.user = user
        self.password = password
        self.timeout = timeout
        self.token = None

    def _request(self, method, path, payload=None, auth=True):
        url = self.base + "/" + path.lstrip("/")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if auth and self.token:
            req.add_header("x-auth-token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(body)
                except Exception:  # noqa: BLE001
                    return resp.status, body
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except Exception:  # noqa: BLE001
                return e.code, body
        except urllib.error.URLError as e:
            return None, "URLError: %s" % (e.reason,)

    # ---------- 登录 ----------
    def login(self):
        status, doc = self._request("POST", "ticket", {"username": self.user, "password": self.password}, auth=False)
        if status in (200, 201) and isinstance(doc, dict):
            self.token = doc.get("response", {}).get("serviceTicket")
        return status, doc

    # ---------- CLI 凭据 ----------
    def ensure_cli_credential(self, user, password, enable):
        status, doc = self._request("GET", "global-credential/cli")
        existing = doc.get("response") if isinstance(doc, dict) else None
        if status == 200 and isinstance(existing, list) and existing:
            print("  [凭据] 已存在 %d 个 CLI 凭据，复用第一个: %s" % (len(existing), existing[0].get("id")))
            return existing[0].get("id")
        payload = {"username": user, "password": password, "enablePassword": enable,
                   "description": "campus-twin auto created"}
        status, doc = self._request("POST", "global-credential/cli", payload)
        print("  [凭据] 创建结果: HTTP %s %s" % (status, json.dumps(doc, ensure_ascii=False)[:300]))
        if isinstance(doc, dict):
            resp = doc.get("response")
            if isinstance(resp, str):
                return resp
            if isinstance(resp, dict):
                return resp.get("id") or resp.get("globalCredentialId")
        # 有些版本创建后需要重新 GET
        status, doc = self._request("GET", "global-credential/cli")
        if isinstance(doc, dict) and isinstance(doc.get("response"), list) and doc["response"]:
            return doc["response"][0].get("id")
        return None

    # ---------- 发现 ----------
    def start_discovery(self, seed, cdp_level, credential_id):
        payload = {
            "name": "campus-cdp-%d" % int(time.time()),
            "discoveryType": "CDP",
            "ipAddressList": seed,
            "cdpLevel": cdp_level,
            "retryCount": 2,
            "timeOut": 8,
            "discoveryStatus": "active",
            "discoveryCondition": "In Progress",
        }
        if credential_id:
            payload["globalCredentialIdList"] = [credential_id]
        status, doc = self._request("POST", "discovery", payload)
        print("  [发现] 创建结果: HTTP %s %s" % (status, json.dumps(doc, ensure_ascii=False)[:300]))
        return status, doc

    def wait_discovery(self, timeout_s=180, interval=5):
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            status, doc = self._request("GET", "discovery")
            items = doc.get("response") if isinstance(doc, dict) else None
            if isinstance(items, list):
                states = [(i.get("name"), i.get("discoveryCondition"), i.get("discoveryStatus")) for i in items]
                if states != last:
                    print("  [发现] 进度: %s" % states)
                    last = states
                done = items and all(str(i.get("discoveryCondition", "")).lower() == "complete" for i in items)
                if done:
                    return True
            time.sleep(interval)
        return False

    # ---------- 汇总 ----------
    def snapshot(self):
        out = {}
        for path in ("network-device/count", "network-device", "host", "topology/physical-topology",
                     "assurance/health-issues", "network-health"):
            status, doc = self._request("GET", path)
            out[path] = (status, doc)
        return out


def print_snapshot(snap):
    print("\n" + "=" * 78)
    print("采集结果")
    print("=" * 78)
    dev = snap.get("network-device", (None, {}))[1]
    devices = dev.get("response") if isinstance(dev, dict) else None
    if isinstance(devices, list):
        print("\n设备清单 (%d 台):" % len(devices))
        print("%-16s %-16s %-10s %-12s %-14s %s" % ("hostname", "管理IP", "型号", "类型", "纳管状态", "可达性"))
        print("-" * 90)
        for d in devices:
            print("%-16s %-16s %-10s %-12s %-14s %s" % (
                str(d.get("hostname") or "-")[:16],
                str(d.get("managementIpAddress") or "-")[:16],
                str(d.get("platformId") or "-")[:10],
                str(d.get("type") or "-")[:12],
                str(d.get("collectionStatus") or "-")[:14],
                str(d.get("reachabilityStatus") or "-")))
    else:
        print("设备清单: 空", dev)

    topo = snap.get("topology/physical-topology", (None, {}))[1]
    t = topo.get("response") if isinstance(topo, dict) else {}
    if isinstance(t, dict):
        print("\n拓扑: nodes=%d links=%d" % (len(t.get("nodes") or []), len(t.get("links") or [])))
        for l in (t.get("links") or [])[:20]:
            print("  %s[%s] <-> %s[%s] (%s)" % (l.get("source"), l.get("startPortName"),
                                                l.get("target"), l.get("endPortName"), l.get("linkStatus")))

    host = snap.get("host", (None, {}))[1]
    hosts = host.get("response") if isinstance(host, dict) else None
    if isinstance(hosts, list):
        print("\n主机 (%d):" % len(hosts))
        for h in hosts:
            print("  %-10s %-16s %s" % (h.get("hostName") or "-", h.get("hostIp") or "-",
                                        h.get("connectedNetworkDeviceName") or "-"))

    health = snap.get("network-health", (None, {}))[1]
    print("\n网络健康: %s" % json.dumps(health, ensure_ascii=False))

    issues = snap.get("assurance/health-issues", (None, {}))[1]
    print("健康问题: %s" % json.dumps(issues, ensure_ascii=False)[:600])


def detect_port():
    for p in PORT_SCAN_RANGE:
        try:
            s = socket.create_connection(("127.0.0.1", p), timeout=0.3)
            s.close()
            return p
        except OSError:
            continue
    return None


def main():
    ap = argparse.ArgumentParser(description="PT NC 一键初始化与设备发现")
    ap.add_argument("--port", type=int, default=0, help="NC 端口，0=自动探测")
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("--seed", default=DEFAULT_SEED, help="CDP 发现种子 IP（核心交换机管理 IP）")
    ap.add_argument("--cdp-level", type=int, default=2)
    ap.add_argument("--no-discovery", action="store_true")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    port = args.port or detect_port()
    if not port:
        print("[X] 没找到 NC 的监听端口，请确认 Real World Access 已开启")
        return 1
    print("=" * 78)
    print("NC 一键初始化  ->  http://127.0.0.1:%d/api/v1" % port)
    print("=" * 78)

    nc = NC(port, args.user, args.password)
    status, doc = nc.login()
    print("[1/4] 登录: HTTP %s" % status)
    if not nc.token:
        print("      失败: %s" % json.dumps(doc, ensure_ascii=False)[:400])
        return 1
    print("      ticket: %s..." % nc.token[:20])

    print("[2/4] 准备 CLI 凭据")
    cred_id = nc.ensure_cli_credential(args.user, args.password, args.password)

    if not args.no_discovery:
        print("[3/4] 发起 CDP 发现（种子 %s，cdpLevel=%d）" % (args.seed, args.cdp_level))
        nc.start_discovery(args.seed, args.cdp_level, cred_id)
        ok = nc.wait_discovery(timeout_s=args.timeout)
        print("[3/4] 发现%s" % ("完成" if ok else "超时（继续看已有结果）"))
    else:
        print("[3/4] 跳过发现")

    print("[4/4] 汇总采集结果")
    print_snapshot(nc.snapshot())
    return 0


if __name__ == "__main__":
    sys.exit(main())
