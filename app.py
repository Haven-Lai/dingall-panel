#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘中作战看板（云端统一版）
把本机两套服务合并为一个进程，完整复刻 http://localhost:8899 的功能：
  /              完整面板（大V发言 + 情绪看板 双 tab）
  /api/feed      大V发言 JSON（服务端代理上游 dingall 接口）
  /api/data      情绪量化数据 JSON
  /sentiment     情绪看板前端（供面板 iframe 同源嵌入）
纯标准库，无第三方依赖。端口由环境变量 PORT 指定（Render 要求），默认 10000。
"""
import os
import sys
import json
import time
import threading
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# 云容器默认 UTC，会把交易时段/日期算错（东八区）→ 统一按北京时间
os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    time.tzset()
except Exception:
    pass  # Windows 无 tzset，忽略（本地开发不受影响）

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
sys.path.insert(0, BASE_DIR)

import dingall_core
import sentiment_core
from sentiment_core import STATE, LOCK, build_kline

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _serve_file(self, name):
        fp = os.path.join(STATIC_DIR, name)
        ext = os.path.splitext(name)[1].lower()
        try:
            with open(fp, "rb") as f:
                return self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))
        except Exception:
            return self._send(404, "not found: %s" % name, "text/plain; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"

        if path in ("/", "/index.html", "/panel.html"):
            return self._serve_file("panel.html")

        if path in ("/sentiment", "/sentiment.html"):
            return self._serve_file("sentiment.html")

        if path == "/echarts.min.js":
            return self._serve_file("echarts.min.js")

        if path == "/api/feed":
            try:
                hours = int(parse_qs(u.query).get("hours", ["24"])[0])
            except Exception:
                hours = 24
            if hours < 0:
                hours = 0
            try:
                data = dingall_core.build_feed(hours)
            except Exception as e:
                data = {"updated": "", "total": 0, "hours": hours,
                        "sources": [], "posts": [], "error": str(e)[:200]}
            return self._send(200, json.dumps(data, ensure_ascii=False))

        if path == "/api/data":
            with LOCK:
                snap = STATE["snapshot"]
                if not snap:
                    return self._send(200, json.dumps({
                        "ready": False,
                        "error": STATE.get("last_error") or "正在初始化数据…",
                    }, ensure_ascii=False))
                today_str = snap["date"]
                cur_hist = STATE["history"].get(today_str) or {}
                today_ohlc = None
                if all(k in cur_hist for k in ("o", "c", "l", "h")):
                    today_ohlc = (cur_hist["o"], cur_hist["c"], cur_hist["l"], cur_hist["h"])
                payload = {
                    "ready": True,
                    "current": {k: v for k, v in snap.items() if k != "zt_codes"},
                    "intraday": STATE["intraday"][-240:],
                    "kline": build_kline(STATE["history"], today_str, today_ohlc),
                    "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "refresh_count": STATE["refresh_count"],
                    "stale": (time.time() - STATE["last_ok"]) > 180,
                }
            return self._send(200, json.dumps(payload, ensure_ascii=False))

        if path in ("/api/health", "/healthz"):
            with LOCK:
                return self._send(200, json.dumps({
                    "ok": bool(STATE["snapshot"]),
                    "last_ok": STATE["last_ok"],
                    "error": STATE["last_error"],
                    "refreshes": STATE["refresh_count"],
                }, ensure_ascii=False))

        return self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))


def worker():
    """后台：启动先回补历史（较慢，放后台避免阻塞端口绑定），然后进入刷新循环。"""
    try:
        sentiment_core.bootstrap()
    except Exception as e:
        print("[error] bootstrap 失败：%r" % (e,), flush=True)
    try:
        sentiment_core.refresh_worker()
    except Exception as e:
        print("[error] refresh_worker 退出：%r" % (e,), flush=True)


def main():
    port = int(os.environ.get("PORT", "10000"))
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("盘中作战看板已启动: 0.0.0.0:%d" % port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
