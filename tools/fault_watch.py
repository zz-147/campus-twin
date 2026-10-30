#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""故障注入期间的状态监控：每 3 秒采样 NC + 后端，记录所有变化。"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime

NC = "http://127.0.0.1:58000/api/v1"
BE = "http://127.0.0.1:8000/api"
LOG = r"D:\zdsh\campus-twin\logs\fault-watch.log"


def http(url, payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method="POST" if payload else "GET")
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("x-auth-token", token)
    try:
        with urllib.request.urlopen(r, timeout=8) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:  # noqa: BLE001
        return None, str(e)


token = None


def login():
    global token
    st, doc = http(NC + "/ticket", {"username": "admin", "password": "cisco"})
    token = doc["response"]["serviceTicket"] if st in (200, 201) and isinstance(doc, dict) else None
    return token


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


open(LOG, "w", encoding="utf-8").close()
login()
log("=" * 60)
log("故障注入监控开始（8 分钟） NC登录=%s" % bool(token))

last_sig = None
for i in range(160):
    if not token:
        login()
    _, issues_doc = http(NC + "/assurance/health-issues", token=token)
    issues = issues_doc.get("response", []) if isinstance(issues_doc, dict) else []
    _, nh = http(NC + "/network-health", token=token)
    _, devs = http(NC + "/network-device", token=token)
    _, hosts_doc = http(NC + "/host", token=token)
    hosts = hosts_doc.get("response", []) if isinstance(hosts_doc, dict) else []
    _, alerts = http(BE + "/alerts")
    _, be_health = http(BE + "/health")

    dev_states = tuple(sorted(
        (d.get("hostname") or d.get("managementIpAddress"), d.get("collectionStatus"))
        for d in (devs.get("response", []) if isinstance(devs, dict) else [])
    ))
    host_states = tuple(sorted(
        (h.get("hostName") or h.get("hostIp"), h.get("pingStatus"))
        for h in hosts
    ))
    nh_sig = (nh.get("numUnreachable"), nh.get("healthyNetworkDevice"), nh.get("healthyClient")) if isinstance(nh, dict) else None
    a_count = alerts.get("count") if isinstance(alerts, dict) else None
    be_issue_count = (be_health.get("counts", {}).get("issues") if isinstance(be_health, dict) else None)

    sig = (len(issues), nh_sig, a_count, be_issue_count, dev_states, host_states)
    if sig != last_sig:
        log("-" * 50)
        log("变化 => NC问题=%d 健康=%s 后端告警=%s 后端问题=%s" % (len(issues), nh_sig, a_count, be_issue_count))
        for it in issues[:10]:
            log("  NC问题: %-14s %-16s %s" % (str(it.get("issueSource"))[:14], str(it.get("issueName"))[:16], it.get("issueTimestamp")))
        for name, st in host_states:
            if st and str(st).upper() != "SUCCESS":
                log("  主机异常: %-12s ping=%s" % (name, st))
        for a in (alerts.get("alerts", []) if isinstance(alerts, dict) else [])[:6]:
            log("  后端告警: [%s] %s | %s" % (a.get("severity"), a.get("title"), str(a.get("detail"))[:120]))
        last_sig = sig
    time.sleep(3)

log("监控结束")
