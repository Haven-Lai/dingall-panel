#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大V发言整合服务
- 代理 6 个 dingall 接口，解析发言（每个分组内含多位发言人的多条消息），合并成 JSON
- 提供 /api/feed（前端每 5 秒拉一次）与首页 /
- 纯标准库，无需联网安装依赖
"""
import http.server
import socketserver
import urllib.request
import json
import gzip
import re
import time
import datetime
import os
import threading
import concurrent.futures

# 忽略系统代理，直接连接上游（上游为公网 IP，无需代理；避免本机/他机代理设置干扰抓取）
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

HOST = "0.0.0.0"
PORT = 8899

# 6 个大V来源（直接使用原始接口地址）
# 顺序即前端色标 chip 的显示顺序（build_feed 按此顺序返回 sources）：
# 灯神 / 金牌竞价 排在龙头舵主之前，是用户的阅读优先级要求。
SOURCES = [
    {"name": "灯神",      "url": "http://121.41.9.82:4380/dingall?a=%E7%81%AF%E7%A5%9E%20&o=1&r=1"},
    {"name": "金牌竞价",  "url": "http://121.41.9.82:4380/dingall?a=%E9%87%91%E7%89%8C%E7%AB%9E%E4%BB%B7%20&o=1&r=1"},
    {"name": "龙头舵主", "url": "http://121.41.9.82:4380/dingall?a=%E9%BE%99%E5%A4%B4%E8%88%B5%E4%B8%BB%20&o=1&r=1"},
    {"name": "极致短线2", "url": "http://121.41.9.82:4380/dingall?a=%E6%9E%81%E8%87%B4%E7%9F%AD%E7%BA%BF2%20&o=1&r=1"},
    {"name": "极致短线",  "url": "http://121.41.9.82:4380/dingall?a=%E6%9E%81%E8%87%B4%E7%9F%AD%E7%BA%BF%20&o=1&r=1"},
    {"name": "金甲",      "url": "http://121.41.9.82:4380/dingall?a=%E9%87%91%E7%94%B2%20&o=1&r=1"},
]

# 外层：一条记录的头（日期时间 + 分组名）
OUTER_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*【<b>(.*?)\s*<a[^>]*>.*?</b>】',
    re.S,
)
# 内层：发言人 + 时间戳（同一行，可能带正文）
# 两种日期写法都要吃下：
#   - 自定义/极致短线类：`发言人 MM-DD HH:MM:SS`（无年份，靠外层头补）
#   - 金牌竞价群：`哈尔滨恐龙380   2026-11-05 08:55:05`（带完整年份）
HEADER_RE = re.compile(
    r'^(\S{1,24})\s+((?:\d{4}-)?\d{1,2}-\d{1,2} \d{1,2}:\d{2}:\d{2})\s*(.*)$'
)
# 内联发言人：【名字】正文（同一行，用于提取名字；允许行首空格）
INLINE_SPK_RE = re.compile(r'^\s*【\s*(.+?)\s*】\s*(.*)$')
# 角色发言行：短名字后跟冒号，单独成行（如「老师:」「群主:」）
ROLE_RE = re.compile(r'^\s*([\u4e00-\u9fffA-Za-z0-9_]{1,8})\s*[:：]\s*$')
# 内层时间戳：带 4 位年、月/日/时可能不带前导零（如 2026-8-21 12:14:46）
INNER_TS_RE = re.compile(r'^\s*(\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{2}:\d{2})\s*$')
# 这些来源每条消息含两个时间戳：外层头（24 小时制，真实发帖时间）与
# 内层（12 小时制，缺 AM/PM 标记，会比真实时间早 12 小时）。用户明确要求
# 「按前面 24 小时制的时间来」，故这类来源一律采用外层（outer_time）。
OUTER_TIME_SOURCES = {"金牌竞价", "龙头舵主", "灯神"}
# 仅剥离行首的【名字】前缀（不吞掉后续正文）
STRIP_SPK_RE = re.compile(r'^\s*【\s*[^】]*\s*】\s*')
# 纯发言人名（无标点、较短）
NAME_RE = re.compile(r'^[\u4e00-\u9fffA-Za-z0-9_]{1,10}$')
# 群标签最后一段若是这类词，说明是栏目/群名而非发言人（如「系统复盘」「闲聊群」）
NON_PERSON_RE = re.compile(r'(群|计划|复盘|机器人|公告|通知|助手)$')
PUNCT = set("。，、！？：；""''（）【】《》,.!?;: ")
IMG_RE = re.compile(r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|gif)(?:\?[^\s"\'<>]*)?|https?://static\.dingtalk\.com/media/[^\s"\'<>]+', re.I)

_cache = {}
_CACHE_TTL = 4.0

# 每日定时源：只在指定时间窗内高频刷新，窗外「当天已拉到过」就冻结、不再请求上游。
# 金甲每天约 09:27 发一张图，9 点后当天不会再发——没必要整天每 5 秒拉它一次 190KB。
# 窗口留到 09:40 是容错，防止某天略有延迟导致漏抓；冻结后看板仍显示已抓到的那条。
DAILY_SOURCES = {
    "金甲": {"window": ("09:00", "09:40")},
}


def _need_refresh(name, cached):
    """每日定时源是否需要真正去请求上游。返回 False 表示直接复用缓存。"""
    spec = DAILY_SOURCES.get(name)
    if not spec:
        return True
    now = datetime.datetime.now()
    hm = now.strftime("%H:%M")
    lo, hi = spec["window"]
    if lo <= hm <= hi:
        # 发布窗口内：保持高频刷新，确保当天那张图一发出就被抓到
        return True
    if cached:
        d = cached.get("data") or {}
        # 窗外 + 当天已成功拉到过 -> 冻结，避免整天重复请求
        if d.get("ok") and d.get("count") and cached.get("date") == now.strftime("%Y-%m-%d"):
            return False
    # 当天还没拉到过（如服务刚重启）仍需拉一次，否则看板会一整天没有金甲的图
    return True


def parse_ts(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def clean_text(raw):
    if not raw:
        return ""
    s = raw.replace("<br>", "\n").replace("<br/>", "\n").replace("<BR>", "\n")
    s = re.sub(r"!\[\]\(\s*", "", s)            # 去掉 markdown 图片语法 ![](
    s = re.sub(r"\]\(\s*", "", s)               # 去掉 markdown 链接残留 ](（金甲纯图帖只发图，正文会剩这一截）
    s = re.sub(r"<[^>]+>", "", s)               # 去标签
    s = re.sub(r'https?://static\.dingtalk\.com/media/[^\s"\'<>]+', '', s)  # 去钉钉媒体裸链接（已单独作图片提取）
    s = s.replace("【", "").replace("】", "")
    s = s.replace("详情", "").replace("\xa0", " ").replace("\r", "")
    s = re.sub(r"-{3,}", "", s)                 # 去掉 ------- 分隔
    s = re.sub(r"\n{3,}", "\n\n", s)            # 折叠连续空行（上游常多余空行）
    lines = [ln.rstrip() for ln in s.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def parse_ts_flex(s):
    """解析可能不带前导零的日期时间，如 2026-8-21 12:14:46。"""
    try:
        m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2}) (\d{1,2}):(\d{2}):(\d{2})', s)
        if not m:
            return 0.0
        y, mo, d, h, mi, se = (int(x) for x in m.groups())
        return datetime.datetime(y, mo, d, h, mi, se).timestamp()
    except Exception:
        return 0.0


def parse_block(content, outer_time, source_name):
    """把一个外层块的内容拆成多条内层消息。
    不同来源格式不同：
      - 自定义类：每条含 `发言人 MM-DD HH:MM:SS 正文`，多条之间用 ------- 分隔
      - 极致短线类：发言人单独一行（无内时间戳），正文随后，每条是一个外层块
    """
    year = outer_time[:4] if outer_time else str(datetime.datetime.now().year)
    outer_ts = parse_ts(outer_time) if outer_time else 0.0
    msgs = []
    for seg in re.split(r'-{3,}', content):
        seg = seg.strip("\n")
        if not seg.strip():
            continue
        lines = seg.split("\n")
        # 首个非空行的下标（发言人/正文可能不在第 0 行，前面有空行/空格）
        idx = next((k for k, ln in enumerate(lines) if ln.strip()), None)
        if idx is None:
            continue
        first = lines[idx].strip()

        # 内层时间戳（带年、月份/日/时可能无前导零）：龙头舵主类
        inner = outer_time
        inner_ts = 0.0
        for ln in lines:
            m = INNER_TS_RE.match(ln.strip())
            if m:
                inner = m.group(1)
                inner_ts = parse_ts_flex(inner)
                if inner_ts:
                    break

        # 发言人 + 正文
        speaker = ""
        body = ""
        # 有些群（金牌竞价）块内首行是内层时间戳，真正的「发言人 + 时间戳」行在它下面，
        # 所以首行是时间戳时把表头行往下挪一格再匹配。
        hidx = idx
        if INNER_TS_RE.match(first):
            nxt = next((k for k in range(idx + 1, len(lines)) if lines[k].strip()), None)
            if nxt is not None:
                hidx = nxt
        header_match = HEADER_RE.match(lines[hidx].strip())   # 群聊类：发言人 + MM-DD HH:MM:SS / YYYY-MM-DD HH:MM:SS
        if header_match:
            speaker = header_match.group(1)
            stamp = header_match.group(2)
            # 自带年份就直接用，否则（MM-DD HH:MM:SS）用外层头的年份补全
            inner = stamp if re.match(r'^\d{4}-', stamp) else "%s-%s" % (year, stamp)
            inner_ts = parse_ts_flex(inner)
            rest = ([header_match.group(3)] if header_match.group(3) else []) + lines[hidx + 1:]
            body = "\n".join(rest)
        else:
            # 找发言行：【名字】（内联）或 名字:（角色行，如 老师:）
            spk_line = None
            spk_type = None
            for li, ln in enumerate(lines):
                ms = INLINE_SPK_RE.search(ln)
                if ms:
                    speaker = ms.group(1).strip()
                    spk_line, spk_type = li, "inline"
                    break
                mr = ROLE_RE.match(ln.strip())
                if mr:
                    speaker = mr.group(1).strip()
                    spk_line, spk_type = li, "role"
            # 拼接正文：跳过内层时间戳行与发言行
            body_lines = []
            for li, ln in enumerate(lines):
                s = ln.strip()
                if INNER_TS_RE.match(s):
                    continue
                if spk_line is not None and li == spk_line:
                    if spk_type == "inline":
                        after = STRIP_SPK_RE.sub("", ln, count=1).strip()
                        if after:
                            body_lines.append(after)
                    continue
                body_lines.append(ln)
            body = "\n".join(body_lines)
            if not speaker and NAME_RE.match(first) and not any(p in first for p in PUNCT):
                # 首行纯名字（极致短线类），正文取其后的行
                speaker = first
                bl = []
                for li, ln in enumerate(lines):
                    if li == idx:
                        continue
                    if INNER_TS_RE.match(ln.strip()):
                        continue
                    bl.append(ln)
                body = "\n".join(bl)

        speaker = speaker.strip().strip("【】").strip()
        # 群标签类（灯神）：内联标签里塞的是「群名 · MM-DD HH:MM · 发言人」或「群名 · 发言人」，
        # 只认最后一段（那才是发言人的位置）。最后一段不是人名（如「∴」「10-21 09:58」）就留空，
        # 绝不回退去取前面的群名——否则「闲聊群」「系统复盘」会变成发言人，
        # 用户点它以为能看某个人，实际捞出来一堆互不相干的群发言。
        if "·" in speaker:
            tail = [x.strip() for x in speaker.split("·")][-1]
            if NAME_RE.match(tail) and not tail.isdigit() and not NON_PERSON_RE.search(tail):
                speaker = tail
            else:
                speaker = ""
        # 龙头舵主：发言人恒为「老师」，且「老师:」/「老师 」常顶在正文开头，统一强制并剥离，避免正文短句被误当发言人
        if source_name == "龙头舵主":
            speaker = "老师"
            body = re.sub(r'^\s*老师\s*[:：]?\s*', '', body)
        imgs = IMG_RE.findall(body)
        txt = clean_text(body)
        # 时间选取：
        #  - OUTER_TIME_SOURCES（金牌竞价、龙头舵主等）：外层头是 24 小时制的真实发帖时间，
        #    内层是 12 小时制（缺 AM/PM，早 12h），一律取外层（用户明确要求按前面 24h 时间）。
        #  - 其他来源：内嵌时间戳若比外层早 >6h 也判定为残留，改用外层；
        #    否则取内嵌时间（含发言人 MM-DD HH:MM:SS 的自定义类格式由 inner 提供）。
        if source_name in OUTER_TIME_SOURCES and outer_time:
            t = outer_time
            ts = outer_ts
        elif outer_time and inner_ts and (outer_ts - inner_ts > 6 * 3600):
            t = outer_time
            ts = outer_ts
        else:
            t = inner if inner_ts else outer_time
            ts = inner_ts if inner_ts else outer_ts
        if txt or imgs:
            msgs.append({
                "time": t,
                "ts": ts,
                "source": source_name,
                "speaker": speaker,
                "text": txt,
                "images": imgs[:3],
            })
    return msgs


def parse_posts(html, source_name):
    posts = []
    parts = OUTER_RE.split(html)
    i = 1
    while i + 2 < len(parts):
        outer_time = parts[i]
        content = parts[i + 2]
        posts.extend(parse_block(content, outer_time, source_name))
        i += 3
    return posts


def _candidate_urls(src):
    """生成候选地址：原始地址 + 去掉 a 参数尾随空格/%20 的版本，用于回退。"""
    u = src["url"]
    urls = [u]
    no_tail = re.sub(r'(%20|\+|\s)+$', '', u)
    if no_tail and no_tail != u:
        urls.append(no_tail)
    seen, out = set(), []
    for x in urls:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def fetch_source(src, force=False):
    now = time.time()
    cached = _cache.get(src["url"])
    if not force and cached and now - cached["t"] < _CACHE_TTL:
        return cached["data"]
    # 每日定时源：在非发布时段且当天已拉到过，就不再打上游，直接复用上次结果
    if not _need_refresh(src["name"], cached):
        return cached["data"]
    best = None
    tried = 0
    for u in _candidate_urls(src):
        tried += 1
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            posts = parse_posts(raw, src["name"])
            data = {"ok": True, "count": len(posts), "posts": posts,
                    "error": "", "fetched": datetime.datetime.now().strftime("%H:%M:%S")}
        except Exception as e:
            data = {"ok": False, "count": 0, "posts": [],
                    "error": str(e)[:120], "fetched": datetime.datetime.now().strftime("%H:%M:%S")}
        if best is None or data["count"] > best["count"]:
            best = data
        if data["count"] > 0:
            break
    if tried > 1:
        best = dict(best)
        best["note"] = "已自动回退尝试 %d 个候选地址，采用返回最多的" % tried
    _cache[src["url"]] = {"t": now, "data": best,
                          "date": datetime.datetime.now().strftime("%Y-%m-%d")}
    return best


def _collect_all(force=False):
    """并发抓所有源，合并去重排序，返回 (来源状态, 时间倒序的帖子)。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(SOURCES))) as ex:
        results = list(ex.map(lambda s: fetch_source(s, force=force), SOURCES))
    sources_status = []
    all_posts = []
    for src, d in zip(SOURCES, results):
        sources_status.append({
            "name": src["name"], "ok": d["ok"], "count": d["count"],
            "error": d["error"], "fetched": d["fetched"],
        })
        all_posts.extend(d["posts"])
    # 去重（上游偶发重复同一条消息）
    seen = set()
    uniq = []
    for p in all_posts:
        key = (p["source"], p["speaker"], p["time"], p["text"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    uniq.sort(key=lambda p: p["ts"], reverse=True)
    return sources_status, uniq

# ================= 快照与「每源独立轮询」 =================
# 上游 121.41.9.82:4380 每次请求都要现抓一遍钉钉（实测 0.7~1.1 秒、每源约 200KB），
# 但它支持 Etag 条件请求：内容没变时回 304、0 字节。所以真正该压的不是「少抓」，
# 而是「用 304 高频轻量地问」——实测 6 源 × 1.5 秒并发跑 76 秒零失败。
#
# 于是不再有「每 5 秒全量重抓一轮」这种东西：每个源一条独立轮询线程，各自条件请求；
# 哪个源变了就立刻重建快照、版本号 +1，由 /api/stream（SSE）推给浏览器。
# 内容感知延迟从「最长 5 秒（后台）+ 最长 5 秒（前端轮询）」压到 1.5 秒内。
import urllib.error

SOURCE_POLL_INTERVAL = float(os.environ.get("DINGALL_POLL_SEC", "1.5"))
FROZEN_POLL_INTERVAL = 30.0     # 每日定时源在冻结时段的轮询间隔
DISK_SAVE_MIN_GAP = 20.0        # 快照落盘最小间隔（120KB，不必每轮都写）
HTTP_TIMEOUT = 8

SNAPSHOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "feed_snapshot.json.gz")

_src_posts = {}       # 源名 -> 该源最新解析结果
_src_status = {}      # 源名 -> {ok,count,error,fetched}
_src_etag = {}        # 源名 -> 上次响应头里的 Etag
_src_date = {}        # 源名 -> 上次成功抓取的日期（供每日定时源判断冻结）
_src_lock = threading.RLock()
_ver_lock = threading.Condition()
_ver = 0
_last_disk_save = 0.0
_disk_dirty = False

_snap = {"t": 0.0, "posts": [], "sources": [], "updated": ""}


def _now_hm():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _today():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _load_snapshot_from_disk():
    """读上次落盘的快照（只用于冷启动顶一下，失败就当没有）。"""
    try:
        with gzip.open(SNAPSHOT_FILE, "rt", encoding="utf-8") as f:
            d = json.load(f)
        posts = d.get("posts") or []
        if not posts:
            return None
        # 按源拆开回填：某个源首轮请求失败时，它也不会从看板上凭空消失
        for p in posts:
            _src_posts.setdefault(p.get("source") or "", []).append(p)
        print("[info] 已加载磁盘快照：%s（%d 条）" % (d.get("updated", "?"), len(posts)), flush=True)
        return {"t": 0.0, "posts": posts, "sources": d.get("sources") or [],
                "updated": d.get("updated") or "", "from_disk": True}
    except Exception:
        return None


def _save_snapshot_to_disk(snap):
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_FILE), exist_ok=True)
        tmp = SNAPSHOT_FILE + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=1) as f:
            json.dump({"updated": snap["updated"], "sources": snap["sources"],
                       "posts": snap["posts"]}, f, ensure_ascii=False)
        os.replace(tmp, SNAPSHOT_FILE)
    except Exception:
        pass


def _is_frozen(name):
    """每日定时源是否处于「当天已拿到、且不在发布窗口」的冻结状态。"""
    pseudo = {"date": _src_date.get(name), "data": _src_status.get(name) or {}}
    return not _need_refresh(name, pseudo)


def _publish():
    """把所有源的当前结果合并成一份快照，并叫醒所有 SSE 订阅者。"""
    global _snap, _ver, _disk_dirty
    all_posts = []
    for src in SOURCES:
        all_posts.extend(_src_posts.get(src["name"]) or [])
    seen = set()
    uniq = []
    for p in all_posts:
        key = (p["source"], p["speaker"], p["time"], p["text"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    uniq.sort(key=lambda p: p["ts"], reverse=True)
    status = []
    for src in SOURCES:
        m = _src_status.get(src["name"]) or {}
        status.append({"name": src["name"], "ok": bool(m.get("ok")),
                       "count": m.get("count", 0), "error": m.get("error") or "",
                       "fetched": m.get("fetched") or ""})
    snap = {"t": time.time(), "posts": uniq, "sources": status,
            "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    with _ver_lock:
        _snap = snap
        _ver += 1
        _ver_lock.notify_all()
    # 只打脏标记，真正落盘交给 _disk_writer 低频去做：
    # 若在这里按时间节流直接写，会出现「只写进第一个抓完的源、后面的完整数据写不进去」。
    _disk_dirty = True
    return _ver


def _disk_writer():
    """低频落盘线程：有变化才写，且保证最终会写下完整的一份。"""
    global _disk_dirty
    while True:
        time.sleep(DISK_SAVE_MIN_GAP)
        if _disk_dirty:
            _disk_dirty = False
            _save_snapshot_to_disk(_snap)


def current_version():
    return _ver


def wait_version(last, timeout=20.0):
    """SSE 用：等版本号变化；超时返回当前版本（上层据此发心跳）。"""
    with _ver_lock:
        if _ver == last:
            _ver_lock.wait(timeout)
        return _ver


def snapshot_meta():
    """SSE 只推这个轻量摘要，正文仍由前端自己去拉 gzip 后的 /api/feed。"""
    with _ver_lock:
        return {"ver": _ver, "total": len(_snap["posts"]), "updated": _snap["updated"]}


def poll_health():
    """每个源最近一次轮询的状态（排查「为啥不更新」时直接看这个）。"""
    out = {}
    with _src_lock:
        for src in SOURCES:
            n = src["name"]
            m = _src_status.get(n) or {}
            out[n] = {"ok": m.get("ok"), "fetched": m.get("fetched"),
                      "count": m.get("count", 0), "etag": bool(_src_etag.get(n)),
                      "interval": FROZEN_POLL_INTERVAL if _is_frozen(n) else SOURCE_POLL_INTERVAL}
        return {"version": _ver, "posts": len(_snap["posts"]), "sources": out}


def _poll_source_once(src):
    """对单个源做一次条件请求。返回 True 表示状态或内容有变化，需要重建快照。"""
    name = src["name"]
    etag = _src_etag.get(name)
    last_err = ""
    for u in _candidate_urls(src):
        headers = {"User-Agent": "Mozilla/5.0"}
        if etag:
            headers["If-None-Match"] = etag
        try:
            req = urllib.request.Request(u, headers=headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read()
                new_etag = (resp.headers.get("Etag") or "").strip()
        except urllib.error.HTTPError as e:
            if e.code == 304:
                # 绝大多数轮询都走这里：0 字节、不解析、不改快照
                with _src_lock:
                    cur = dict(_src_status.get(name) or {})
                    cur.update({"ok": True, "error": "", "fetched": _now_hm()})
                    _src_status[name] = cur
                    if not _src_posts.get(name):
                        # 内存里没数据却回 304，说明 etag 没意义了，下轮强制全量
                        _src_etag.pop(name, None)
                return False
            last_err = "HTTP %s" % e.code
            continue
        except Exception as e:
            last_err = str(e)[:120]
            continue

        # 200：内容确实变了，解析后替换该源
        txt = raw.decode("utf-8", "ignore")
        posts = parse_posts(txt, name)
        with _src_lock:
            old = _src_posts.get(name)
            changed = True
            if old and posts and len(old) == len(posts):
                # 长度和最新一条都没变（上游忽略 If-None-Match 时会出现），不必惊动快照
                changed = (old[0].get("time") != posts[0].get("time")
                           or old[0].get("text") != posts[0].get("text")
                           or (old[0].get("images") or []) != (posts[0].get("images") or []))
            _src_posts[name] = posts
            if new_etag:
                _src_etag[name] = new_etag
            _src_status[name] = {"ok": True, "count": len(posts), "error": "",
                                 "fetched": _now_hm()}
            _src_date[name] = _today()
        return changed

    # 所有候选地址都失败：保留已有数据，只把状态标红
    with _src_lock:
        prev_ok = (_src_status.get(name) or {}).get("ok")
        _src_etag.pop(name, None)          # etag 可能已失效，下轮强制全量
        cur = dict(_src_status.get(name) or {})
        cur.update({"ok": False, "error": last_err, "fetched": _now_hm(),
                    "count": len(_src_posts.get(name) or [])})
        _src_status[name] = cur
    # 已经在报错就别每 1.5 秒推一次「还是错的」，只在状态翻转时通知前端
    return prev_ok is not False


def _poll_all_once():
    """并发抓一轮所有源（只在冷启动、快照为空时用）。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(SOURCES))) as ex:
        list(ex.map(_poll_source_once, SOURCES))
    return _publish()


def _source_loop(src):
    name = src["name"]
    while True:
        t0 = time.time()
        interval = SOURCE_POLL_INTERVAL
        changed = False
        try:
            if _is_frozen(name):
                interval = FROZEN_POLL_INTERVAL
            else:
                changed = _poll_source_once(src)
        except Exception as e:
            print("[warn] %s 轮询异常：%r" % (name, e), flush=True)
        if changed:
            try:
                _publish()
            except Exception as e:
                print("[warn] 快照重建失败：%r" % (e,), flush=True)
        time.sleep(max(0.2, interval - (time.time() - t0)))


def start_background_refresher():
    """启动每个源的独立轮询线程（幂等）。app.py 启动时调用一次。"""
    if getattr(start_background_refresher, "_started", False):
        return
    start_background_refresher._started = True

    # 磁盘快照已经顶住了内容，先发布一版让 SSE 有个基线版本号
    if _snap["posts"]:
        _publish()

    for src in SOURCES:
        threading.Thread(target=_source_loop, args=(src,), daemon=True,
                         name="poll-" + src["name"]).start()
    threading.Thread(target=_disk_writer, daemon=True, name="snap-disk").start()


def build_feed(hours):
    snap = _snap
    if not snap["posts"]:
        # 冷启动兜底：各源线程正在抓首轮，这里只「等」不「抢」——
        # 再发一轮只会让 12 个请求一起压上游，反而更慢。
        deadline = time.time() + 12
        while time.time() < deadline and not _snap["posts"]:
            time.sleep(0.05)
        snap = _snap
    posts = snap["posts"]
    if hours:
        cutoff = time.time() - hours * 3600
        posts = [p for p in posts if p["ts"] >= cutoff]
    return {
        "updated": snap["updated"],
        "total": len(posts),
        "hours": hours,
        "sources": snap["sources"],
        "posts": posts,
        "version": _ver,
        "snapshot_age": round(time.time() - snap["t"], 1),
    }


# 模块加载时先顶一份上次落盘的数据（进程重启后首屏不用干等）
_loaded = _load_snapshot_from_disk()
if _loaded:
    _snap = _loaded
