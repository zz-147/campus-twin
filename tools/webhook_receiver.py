#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 Webhook 接收器：验证告警通知链路（零依赖标准库）。

用法：
  python tools/webhook_receiver.py            # 默认监听 127.0.0.1:9000，日志写 logs/webhook.log
  python tools/webhook_receiver.py --port 9001

后端启动时配置：
  NOTIFY_ENABLED=true
  NOTIFY_WEBHOOK_URL=http://127.0.0.1:9000/webhook
  NOTIFY_WEBHOOK_TYPE=generic
  NOTIFY_MIN_SEVERITY=critical
"""
import argparse
import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "logs", "webhook.log")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        line = "[%s] %s %s\n%s\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            self.command,
            self.path,
            body,
        )
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "-" * 60 + "\n")
        print(line, flush=True)
        resp = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, fmt, *args):  # noqa: A003
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    args = ap.parse_args()
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    print("Webhook receiver listening on http://%s:%d/webhook  (log: %s)" % (args.host, args.port, LOG), flush=True)
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
