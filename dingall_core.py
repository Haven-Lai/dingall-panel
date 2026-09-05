#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大V发言整合服务
- 代理 5 个 dingall 接口，解析发言（每个分组内含多位发言人的多条消息），合并成 JSON
- 提供 /api/feed（前端每 5 秒拉一次）与首页 /
- 纯标准库，无需联网安装依赖
"""
import http.server
import socketserver
import urllib.request
import json
import re
import time
import datetime
import os

# 忽略系统代理，直接连接上游（上游为公网 IP，无需代理；避免本机/他机代理设置干扰抓取）
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

HOST = "0.0.0.0"
PORT = 8899

# 6 个大V来源（直接使用原始接口地址）
SOURCES = [
    {"name": "龙头舵主", "url": "http://121.41.9.82:4380/dingall?a=%E9%BE%99%E5%A4%B4%E8%88%B5%E4%B8%BB%20&o=1&r=1"},
    {"name": "十倍之路", "url": "http://121.41.9.82:4380/dingall?a=%E5%8D%81%E5%80%8D%E4%B9%8B%E8%B7%AF%20&o=1&r=1"},
    {"name": "自定义",   "url": "http://121.41.9.82:4380/dingall?a=%E8%87%AA%E5%AE%9A%E4%B9%89%20&o=1&r=1"},
    {"name": "极致短线2", "url": "http://121.41.9.82:4380/dingall?a=%E6%9E%81%E8%87%B4%E7%9F%AD%E7%BA%BF2%20&o=1&r=1"},
    {"name": "极致短线",  "url": "http://121.41.9.82:4380/dingall?a=%E6%9E%81%E8%87%B4%E7%9F%AD%E7%BA%BF%20&o=1&r=1"},
    {"name": "金牌竞价",  "url": "http://121.41.9.82:4380/dingall?a=%E9%87%91%E7%89%8C%E7%AB%9E%E4%BB%B7%20&o=1&r=1"},
]

# 外层：一条记录的头（日期时间 + 分组名）
OUTER_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*【<b>(.*?)\s*<a[^>]*>.*?</b>】',
    re.S,
)
# 内层：发言人 + 短时间戳 MM-DD HH:MM:SS（同一行，可能带正文）
HEADER_RE = re.compile(r'^(\S{1,20})\s+(\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*(.*)$')
# 内联发言人：【名字】正文（同一行，用于提取名字；允许行首空格）
INLINE_SPK_RE = re.compile(r'^\s*【\s*(.+?)\s*】\s*(.*)$')
# 角色发言行：短名字后跟冒号，单独成行（如「老师:」「群主:」）
ROLE_RE = re.compile(r'^\s*([\u4e00-\u9fffA-Za-z0-9_]{1,8})\s*[:：]\s*$')
# 内层时间戳：带 4 位年、月/日/时可能不带前导零（如 2026-8-21 12:14:46）
INNER_TS_RE = re.compile(r'^\s*(\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{2}:\d{2})\s*$')
# 仅剥离行首的【名字】前缀（不吞掉后续正文）
STRIP_SPK_RE = re.compile(r'^\s*【\s*[^】]*\s*】\s*')
# 纯发言人名（无标点、较短）
NAME_RE = re.compile(r'^[\u4e00-\u9fffA-Za-z0-9_]{1,10}$')
PUNCT = set("。，、！？：；""''（）【】《》,.!?;: ")
IMG_RE = re.compile(r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|gif)(?:\?[^\s"\'<>]*)?|https?://static\.dingtalk\.com/media/[^\s"\'<>]+', re.I)

_cache = {}
_CACHE_TTL = 4.0


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
        header_match = HEADER_RE.match(first)          # 自定义类：发言人 MM-DD HH:MM:SS
        if header_match:
            speaker = header_match.group(1)
            its = parse_ts("%s-%s" % (year, header_match.group(2)))
            rest = ([header_match.group(3)] if header_match.group(3) else []) + lines[idx + 1:]
            body = "\n".join(rest)
            inner = "%s-%s" % (year, header_match.group(2))
            inner_ts = its
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
        # 龙头舵主：发言人恒为「老师」，且「老师:」/「老师 」常顶在正文开头，统一强制并剥离，避免正文短句被误当发言人
        if source_name == "龙头舵主":
            speaker = "老师"
            body = re.sub(r'^\s*老师\s*[:：]?\s*', '', body)
        imgs = IMG_RE.findall(body)
        txt = clean_text(body)
        # 龙头舵主类：内嵌时间戳是钉钉时区残留（比真实发帖时间早约 12h），
        # 用外层真实发帖时间，避免看着像旧数据
        if source_name == "龙头舵主" and outer_time:
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


def fetch_source(src):
    now = time.time()
    cached = _cache.get(src["url"])
    if cached and now - cached["t"] < _CACHE_TTL:
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
    _cache[src["url"]] = {"t": now, "data": best}
    return best


def build_feed(hours):
    all_posts = []
    sources_status = []
    for src in SOURCES:
        d = fetch_source(src)
        sources_status.append({
            "name": src["name"], "ok": d["ok"], "count": d["count"],
            "error": d["error"], "fetched": d["fetched"],
        })
        all_posts.extend(d["posts"])
    if hours:
        cutoff = time.time() - hours * 3600
        all_posts = [p for p in all_posts if p["ts"] >= cutoff]
    # 去重（上游偶发重复同一条消息）
    seen = set()
    uniq = []
    for p in all_posts:
        key = (p["source"], p["speaker"], p["time"], p["text"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    all_posts = uniq
    all_posts.sort(key=lambda p: p["ts"], reverse=True)
    return {
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(all_posts),
        "hours": hours,
        "sources": sources_status,
        "posts": all_posts,
    }


