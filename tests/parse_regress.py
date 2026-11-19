#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析回归：确认「改了某个源的解析逻辑」没有伤到其它源。

用法（两步，中间那次代码修改才是被测对象）：

    python tests/parse_regress.py save     # 抓一份原始 HTML 存缓存 + 记录解析指纹
    # ... 改 dingall_core.py ...
    python tests/parse_regress.py check    # 用同一份缓存重跑，逐源对比指纹

为什么不能直接对比线上内容：上游每次返回的都带新帖，md5 天然会变。
所以必须先把原始 HTML 缓存下来，两次解析跑在**同一份字节**上，对比才有意义。

指纹 = (source, speaker, time, ts, text, images) 全部排序后取 md5。
条数或 md5 任一对不上，就说明这个源被改坏了。

全绿标准：
  - 原有源：基本「一致」（老源里若混进浏览器页面每隔 8 秒的自动刷新，可能会有
    极小抖动，正常情况下应当完全一致）；
  - 新加的源：「新增」且 speakers 里没有空字符串以外的脏值（如「链接」这种正文标签）。
"""
import os
import sys
import json
import time
import hashlib
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 忽略系统代理，直连上游（本机代理会拦截并改写请求）
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

import dingall_core as core  # noqa: E402

CACHE = os.path.join(tempfile.gettempdir(), "dingall_regress")
BASE = os.path.join(CACHE, "base.json")


def grab(url):
    """同一份 HTML 只抓一次，后续 save/check 都复用它。"""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, hashlib.md5(url.encode()).hexdigest()[:10] + ".html")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return open(path, encoding="utf-8", errors="ignore").read()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", "ignore")
    open(path, "w", encoding="utf-8").write(html)
    return html


def fingerprint(name, html):
    posts = core.parse_posts(html, name)
    rows = sorted((p["source"], p["speaker"], p["time"], p["ts"], p["text"],
                   "|".join(p["images"])) for p in posts)
    blob = json.dumps(rows, ensure_ascii=False)
    return {"name": name, "count": len(posts),
            "md5": hashlib.md5(blob.encode()).hexdigest(),
            "speakers": sorted({p["speaker"] for p in posts})}


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    result = {}
    for src in core.SOURCES:
        try:
            result[src["name"]] = fingerprint(src["name"], grab(src["url"]))
            r = result[src["name"]]
            print("  %-8s count=%-4d md5=%s speakers=%s"
                  % (src["name"], r["count"], r["md5"], r["speakers"][:5]))
        except Exception as e:
            print("  %-8s 抓取失败 %r" % (src["name"], e))
            result[src["name"]] = {"name": src["name"], "error": str(e)}

    if mode == "save":
        os.makedirs(CACHE, exist_ok=True)
        with open(BASE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        print("\n基线已保存：%s" % BASE)
        return 0

    if not os.path.exists(BASE):
        print("\n还没有基线，先跑一次 save。")
        return 2
    base = json.load(open(BASE, encoding="utf-8"))
    bad = []
    print("\n=== 对比基线 ===")
    for name, cur in result.items():
        old = base.get(name)
        if old is None:
            print("  %-8s 新增  count=%d speakers=%s" % (name, cur.get("count", 0), cur.get("speakers", [])[:5]))
            continue
        if old.get("md5") == cur.get("md5") and old.get("count") == cur.get("count"):
            print("  %-8s 一致  count=%d" % (name, cur.get("count", 0)))
        else:
            print("  %-8s 变化！%d/%s -> %d/%s"
                  % (name, old.get("count", 0), old.get("md5"), cur.get("count", 0), cur.get("md5")))
            bad.append(name)
    print("\n%s" % ("全部一致" if not bad else "被改坏的源：%s" % "、".join(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
