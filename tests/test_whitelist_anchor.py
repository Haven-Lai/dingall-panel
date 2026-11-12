# -*- coding: utf-8 -*-
"""白名单改名自动锚定的回归测试（合成数据，不联网）。

跑法： python tests/test_whitelist_anchor.py

它验证的是：群主改昵称后，服务能靠「旧昵称沉默 + 新昵称接手」自动认出新人，
历史发言不断档、其他人继续被挡在外面，而且重启后依然认得。
"""
import sys, os, time, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "speaker_whitelist.json")
if os.path.exists(ST):
    os.remove(ST)

import dingall_core as d
import urllib.request

OWNER = "嘉兴可爱范"
NEW = "金牛九号"
SRC = "金牌竞价"
NOW = time.time()


def stamp(minutes_ago):
    return datetime.datetime.fromtimestamp(NOW - minutes_ago * 60).strftime("%Y-%m-%d %H:%M:%S")


def block(minutes_ago, speaker, text):
    ts = stamp(minutes_ago)
    inner = ts                                 # 内层与块头一致，解析就会用这个时间
    return ('%s 【<b>%s <a href="http://x/dingall?a=1" target="_blank" '
            'style="font-size:12px;">只看Ta</a></b>】 \n\n %s\n\n\n\n%s   %s\n\n\n\n%s<br>\n'
            % (ts, SRC, inner, speaker, inner, text))


def build(rename_before_min=40):
    """群主 120~45 分钟前用旧名，40 分钟前改名后用新名；其他人一直在聊。"""
    out = []
    for i in range(30):                        # 群主改名前的发言（旧昵称）
        out.append(block(120 - i, OWNER, "改名前的第%d条" % i))
    for i in range(20):                        # 改名后的发言（新昵称）
        m = 40 - i * 1.5
        out.append(block(m, NEW if rename_before_min else OWNER, "改名后的第%d条" % i))
    for i in range(20):                        # 其他群友，一直在说话
        out.append(block(100 - i * 5, ["卡西米尔", "呱呱呱", "玉姐", "小马达"][i % 4], "闲聊%d" % i))
    return "\n".join(out)


class FakeResp:
    def __init__(self, body, etag):
        self._b = body.encode("utf-8")
        self.headers = {"Etag": etag}

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


STATE = {"body": "", "n": 0}


def fake_urlopen(req, timeout=None):
    STATE["n"] += 1
    return FakeResp(STATE["body"], "etag-%d" % STATE["n"])


urllib.request.urlopen = fake_urlopen
d.WL_CONFIRM_SEC = 0        # 线上是 90 秒（由多个 200 观测凑满），这里直接放行
d._src_posts.clear(); d._src_status.clear(); d._src_etag.clear()

src = [s for s in d.SOURCES if s["name"] == SRC][0]

# —— 第一轮：他还叫旧名字 ——
STATE["body"] = build(rename_before_min=None).replace(NEW, OWNER)
d._poll_source_once(src)
kept = d._src_posts[SRC]
print("第一轮  白名单:", d._wl_summary()[SRC])
print("        保留 %d 条，发言人: %s" % (len(kept), set(p["speaker"] for p in kept)))
assert set(p["speaker"] for p in kept) == {OWNER}, set(p["speaker"] for p in kept)
assert len(kept) == 50, len(kept)

# —— 第二轮：40 分钟前改了名，旧名下的历史发言还在 ——
STATE["body"] = build(rename_before_min=40)
d._poll_source_once(src)
s2 = d._wl_summary()[SRC]
print("第二轮  白名单:", s2)
assert s2["pending"] == NEW, "应出现改名候选，实际: %s" % s2

# —— 第三轮：确认（线上靠 90 秒把关，这里设为 0）——
d._poll_source_once(src)
s3 = d._wl_summary()[SRC]
kept = d._src_posts[SRC]
print("第三轮  白名单:", s3)
print("        保留 %d 条，发言人: %s" % (len(kept), set(p["speaker"] for p in kept)))
assert NEW in d._wl_alias[SRC]
speakers = set(p["speaker"] for p in kept)
assert speakers == {OWNER, NEW}, speakers        # 改名前的历史不该丢
assert len(kept) == 50, len(kept)                # 一个群友都不该混进来
assert s3["unresolved"] is False

# —— 第四轮：落盘 ——
import json
print("磁盘别名表:", json.load(open(ST, encoding="utf-8"))["alias"])

# —— 第五轮：重启后仍认识（模拟容器重启）——
for m in ("dingall_core",):
    sys.modules.pop(m, None)
import dingall_core as d2
d2._wl_alias.clear(); d2._wl_known.clear(); d2._wl_state.clear(); d2._wl_load()
kept2 = d2._filter_speakers(SRC, kept)
print("重启后  保留 %d 条，发言人: %s" % (len(kept2), set(p["speaker"] for p in kept2)))
assert set(p["speaker"] for p in kept2) == {OWNER, NEW}

os.path.exists(ST) and os.remove(ST)
print("\n端到端通过：改名被自动识别，历史不断档，其他人仍被挡在外面。")
