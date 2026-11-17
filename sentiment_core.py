# -*- coding: utf-8 -*-
"""
A 股超短情绪量化监控看板 —— 后端服务（纯 Python 标准库，无需 pip 安装任何包）

数据来源
  1. 东方财富全市场行情（clist 接口，并发分页）  -> 涨停/跌停/炸板/一字/涨跌家数/成交额
  2. 同花顺涨停池（limit_up_pool）             -> 连板高度/梯队/一字板/涨停原因/封单
  3. 腾讯行情（qt.gtimg.cn）                   -> 上证/深证/创业板/科创50 指数

启动
  python server.py              # 默认端口 8890
  python server.py --port 9000  # 自定义端口
"""

import json
import os
import re
import socket
import sys
import time
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import functools
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print = functools.partial(print, flush=True)   # 后台运行时也即时输出日志

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "public")  # 本地与 Vercel(public/) 共用同一份前端
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

HISTORY_FILE = os.path.join(DATA_DIR, "history.json")     # 历史交易日情绪分
INTRADAY_FILE = os.path.join(DATA_DIR, "intraday.json")  # 今日情绪分时序列

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 基础抓取


def http_get(url, headers=None, timeout=15, retries=3, encoding="utf-8"):
    """带重试的 GET，返回文本；全部失败返回 None。"""
    hdr = {"User-Agent": UA, "Connection": "close"}
    if headers:
        hdr.update(headers)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode(encoding, "ignore")
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(0.6 * (i + 1))
    return None


def http_json(url, headers=None, timeout=15, retries=3):
    txt = http_get(url, headers, timeout, retries)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


# ---------------------------------------------------------------- 行情快照

EM_HOSTS = ["push2.eastmoney.com", "push2delay.eastmoney.com"]
EM_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"          # 沪深主板/创业板/科创板
EM_FIELDS = "f2,f3,f12,f13,f14,f15,f16,f17,f18,f6"   # 现价/涨跌幅/代码/市场/名称/最高/最低/开盘/昨收/成交额
EM_HEADERS = {"Referer": "https://quote.eastmoney.com/"}

PAGE_SIZE = 100
MAX_WORKERS = 8


def _fetch_page(host, pn):
    url = "https://%s/api/qt/clist/get?%s" % (host, urllib.parse.urlencode({
        "pn": str(pn), "pz": str(PAGE_SIZE), "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": EM_FS, "fields": EM_FIELDS,
    }))
    return http_json(url, EM_HEADERS, timeout=20, retries=2)


def fetch_market_snapshot():
    """并发拉取全市场行情，返回 {code: {...}}；失败返回空 dict。"""
    first = None
    host = None
    for h in EM_HOSTS:
        first = _fetch_page(h, 1)
        if first and first.get("data"):
            host = h
            break
        first = None
    if not first:
        return None

    total = (first.get("data") or {}).get("total") or 0
    pages = max(1, min(80, (total + PAGE_SIZE - 1) // PAGE_SIZE))
    rows = list((first.get("data") or {}).get("diff") or [])

    if pages > 1:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for res in ex.map(lambda p: _fetch_page(host, p), range(2, pages + 1)):
                if res and res.get("data"):
                    rows.extend(res["data"].get("diff") or [])

    snap = {}
    for r in rows:
        code = r.get("f12")
        if not code:
            continue
        snap[str(code)] = {
            "name": r.get("f14") or "",
            "cur": r.get("f2"),
            "pct": r.get("f3"),
            "high": r.get("f15"),
            "low": r.get("f16"),
            "open": r.get("f17"),
            "pre": r.get("f18"),
            "amount": r.get("f6") or 0,
        }
    return snap


# ---------------------------------------------------------------- 同花顺涨停池

THS_HEADERS = {"Referer": "https://data.10jqka.com.cn/funds/ztb/"}
THS_FIELDS = "199112,10,9001,330329,330325,133971,133970,1968584,3475914,9002,9003,9004"


def fetch_limit_up_pool(date_str, limit=200):
    """date_str 形如 20260904；返回 (股票列表, 总数)；失败返回 ([], 0)。"""
    params = {
        "page": "1", "limit": str(limit),
        "field": THS_FIELDS, "filter": "HS,GEM2STAR",
        "order_field": "330324", "order_type": "0", "date": date_str,
    }
    url = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool?" + urllib.parse.urlencode(params)
    d = http_json(url, THS_HEADERS, timeout=12, retries=2)
    if not d or d.get("status_code") != 0 or not d.get("data"):
        return [], 0
    info = d["data"].get("info") or []
    total = (d["data"].get("page") or {}).get("total") or len(info)
    return info, total


def parse_board_height(high_days):
    """'首板'->1, '3连板'->3, '5板'->5"""
    if not high_days:
        return 1
    s = str(high_days)
    if "首" in s:
        return 1
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else 1


# ---------------------------------------------------------------- 指数

TX_INDEX_CODES = [("sh000001", "上证指数"), ("sz399001", "深证成指"),
                  ("sz399006", "创业板指"), ("sh000688", "科创50")]


def fetch_indices():
    q = ",".join(c for c, _ in TX_INDEX_CODES)
    txt = http_get("http://qt.gtimg.cn/q=" + q, timeout=10, retries=2, encoding="gbk")
    out = []
    if not txt:
        return out
    for line in txt.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        body = line.split('="', 1)[-1].strip('"')
        f = body.split("~")
        if len(f) < 6:
            continue
        try:
            cur = float(f[3]); pre = float(f[4])
        except Exception:
            continue
        pct = round((cur - pre) / pre * 100, 2) if pre else 0.0
        out.append({"code": f[2], "name": f[1], "cur": round(cur, 2),
                    "pct": pct, "pre": round(pre, 2)})
    return out


# ---------------------------------------------------------------- 指标计算


def limit_ratio(code, name):
    """返回涨停倍数：ST 1.05，创业板/科创板 1.20，其余 1.10"""
    n = (name or "").upper().replace(" ", "")
    if "ST" in n:
        return 1.05
    if code.startswith(("300", "301", "688", "689")):
        return 1.20
    return 1.10


def to_num(v):
    """行情字段偶发为 '-' 或字符串，统一转成 float；无法转换返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except Exception:
            return None
    return None


def calc_market_stats(snap):
    """从全市场行情快照统计涨跌停/炸板/一字/涨跌家数/成交额。"""
    zt = dt = zb = yz_up = yz_down = 0
    up = down = flat = 0
    up5 = down5 = 0
    amount = 0.0
    zt_list = []

    for code, s in snap.items():
        cur, pre = to_num(s.get("cur")), to_num(s.get("pre"))
        hi, lo, op = to_num(s.get("high")), to_num(s.get("low")), to_num(s.get("open"))
        pct = to_num(s.get("pct"))
        amt = to_num(s.get("amount"))
        if amt:
            amount += amt

        if pct is not None:
            if pct > 0:
                up += 1
            elif pct < 0:
                down += 1
            else:
                flat += 1
            if pct >= 5:
                up5 += 1
            elif pct <= -5:
                down5 += 1

        if not all(isinstance(x, (int, float)) for x in (cur, pre, hi, lo, op)) or pre <= 0:
            continue

        # ST 股为 5% 板，行业惯例（同花顺/东财涨停池）不计入涨停家数，故跳过
        name = s.get("name", "") or ""
        if "ST" in name.upper().replace(" ", ""):
            continue

        ratio = limit_ratio(code, name)
        up_price = round(pre * ratio, 2)
        down_price = round(pre * (2 - ratio), 2)

        if hi >= up_price - 0.01:
            if cur >= up_price - 0.01:
                zt += 1
                zt_list.append(code)
                if op >= up_price - 0.01 and lo >= up_price - 0.01:
                    yz_up += 1
            else:
                zb += 1
        if lo <= down_price + 0.01 and cur <= down_price + 0.01:
            dt += 1
            if op <= down_price + 0.01 and hi <= down_price + 0.01:
                yz_down += 1

    total_try = zt + zb
    broken_ratio = round(zb / total_try * 100, 1) if total_try else 0.0
    return {
        "limit_up": zt, "limit_down": dt, "broken": zb,
        "broken_ratio": broken_ratio,
        "one_word_up": yz_up, "one_word_down": yz_down,
        "up": up, "down": down, "flat": flat,
        "up5": up5, "down5": down5,
        "amount": round(amount / 1e8, 1),  # 亿元
        "zt_codes": zt_list,
    }


# 权重按 A 股实际分布标定：涨停中枢约 50 家、空间高度中枢约 5 板、一字板中枢约 4 只。
# 涨停/高度/一字设上限，避免极端值把分数打满。
Z_T_CAP, HEIGHT_CAP, YZ_CAP = 120, 9, 12


def calc_core_score(zt, max_height, one_word_up):
    """核心情绪分：只用「涨停家数 + 空间高度 + 一字板」三项。

    这三项可以逐日回溯（同花顺涨停池支持历史日期），因此历史与当日完全同口径，
    用于绘制情绪日线 K 图。跌停/炸板/溢价等项无历史来源，只进入实时综合分。
    """
    s = 50.0
    s += (min(zt, Z_T_CAP) - 50) * 0.35
    s += (min(max_height, HEIGHT_CAP) - 5) * 4
    s += (min(one_word_up, YZ_CAP) - 4) * 1.2
    return max(5.0, min(95.0, round(s, 1)))


def calc_score(m):
    """情绪综合分 0~100。在 Gemini 原版公式基础上补入"昨日涨停溢价"与"连板晋级率"。"""
    score = 50.0
    score += (min(m["limit_up"], Z_T_CAP) - 50) * 0.35      # 涨停家数
    score -= m["limit_down"] * 1.5                          # 跌停家数
    score -= m["one_word_down"] * 3.0                       # 一字跌停
    score += (min(m["max_height"], HEIGHT_CAP) - 5) * 4     # 空间板高度
    score -= (m["broken_ratio"] - 30) * 0.6                 # 炸板率
    score += (min(m["one_word_up"], YZ_CAP) - 4) * 1.2      # 一字涨停
    score += m.get("premium", 0.0) * 3                      # 昨日涨停今日溢价
    score += (m.get("promo_rate", 25.0) - 25) * 0.5         # 连板晋级率
    return max(5.0, min(95.0, round(score, 1)))


STATUS_TABLE = [
    (25, "情绪冰点", "#22c55e", "关注恐慌释放后的首板试错，回避高位补跌标的"),
    (50, "混沌震荡", "#eab308", "资金轮动加速，控仓防守，切忌盘中追高打板"),
    (80, "主升回暖", "#ef4444", "聚焦核心主线龙头，大胆做连板接力与强趋势中军"),
    (100, "极度亢奋", "#b91c1c", "警惕次日严重分化，锁仓不追高，去弱留强"),
]


def judge(score, broken_ratio):
    for cap, name, color, advice in STATUS_TABLE:
        if score <= cap:
            status, col, adv = name, color, advice
            break
    # 混沌期特殊判定：分数中枢拉锯 + 炸板率居高不下
    if 35 <= score <= 60 and broken_ratio > 35:
        status, col, adv = "混沌震荡", "#eab308", "题材分散、炸板率高企，控仓防守，切忌盘中追高打板"
    return status, col, adv


# ---------------------------------------------------------------- 历史与分时


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def last_trading_dates(n=30):
    """往前找 n 个自然周内的交易日（周一至周五），最新在前。"""
    days = []
    d = datetime.now()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return days


def backfill_history(history, days=30):
    """用同花顺涨停池回补历史交易日的收盘情绪分（涨停家数/连板高度/一字板均为真实数据）。"""
    changed = False
    for dstr in reversed(last_trading_dates(days)):
        ymd = dstr.replace("-", "")
        if dstr in history and isinstance(history[dstr], dict):
            continue
        info, total = fetch_limit_up_pool(ymd, limit=200)
        if total <= 0:
            continue
        zt = len(info)
        maxh = 1
        yz = 0
        for s in info:
            maxh = max(maxh, parse_board_height(s.get("high_days")))
            if s.get("limit_up_type") == "一字板":
                yz += 1
        sc = calc_core_score(zt, maxh, yz)
        history[dstr] = {"c": sc, "zt": zt, "maxh": maxh, "yz": yz, "approx": True}
        changed = True
        time.sleep(0.25)
    return changed


def build_kline(history, today_str, today_ohlc, limit=30):
    """生成 ECharts candlestick 数据 [ [date, open, close, low, high], ... ]

    历史交易日只有收盘口径（O 取前一日收盘分，无影线）；当日为实时完整 OHLC。
    """
    days = sorted(history.keys())
    if today_str not in days and today_ohlc:
        days.append(today_str)
    days = days[-limit:]

    out = []
    prev_close = None
    for d in days:
        if d == today_str and today_ohlc:
            o, c, l, h = today_ohlc
            out.append([d, o, c, l, h])
            continue
        rec = history.get(d) or {}
        c = rec.get("c")
        if c is None:
            continue
        o = prev_close if prev_close is not None else c
        out.append([d, round(o, 1), round(c, 1), round(min(o, c), 1), round(max(o, c), 1)])
        prev_close = c
    return out


# ---------------------------------------------------------------- 涨跌停家数 K 线
#
# 情绪看板前端（static/sentiment.html）的数字来自东方财富的涨停池 / 跌停池（data.tc），
# 这里用**同一个接口**回补历史，保证「今日卡片上的 42 家」和「K 线最后一根 42」是同一口径。
# 别换成同花顺涨停池 —— 那个口径的涨停家数会跟卡片对不上，用户一眼就看出矛盾。
#
# ⚠️ 两个池的排序字段不通用：涨停池认 fbt（首次封板时间），
#    跌停池如果也传 sort=fbt:asc，会返回 rc=0 / tc 正常但 pool 为空 ——
#    看着像「跌停池没数据」，实际是排序字段不被接受。跌停池必须用 fund:asc。
#
# ⚠️ 历史深度只有约 15 个交易日：再往前 tc 一律返回 0、pool 为空。
#    这不是 bug，也不需要"修复"，多扫几天拿到多少算多少。

EX_BASE = "https://push2ex.eastmoney.com/"
EX_UT = "7eea3edcaed734bea9cbfc24409ed989"
EX_HEADERS = {"Referer": "https://quote.eastmoney.com/"}

ZT_HIST_FILE = os.path.join(DATA_DIR, "zt_history.json")

# 历史日一整天不会变，但当日那根要跟着盘中跳动，所以拆成两档 TTL
ZT_FULL_TTL = 1800
ZT_LIVE_TTL = 20

# 历史深度 ~15 个交易日，扫 22 个工作日刚好盖住；并发 10 实测 0.5 秒内扫完。
# 这两个数一起决定冷启动时 K 线图多久能出来，别随手调大。
ZT_HIST_DAYS = 22
ZT_HIST_WORKERS = 10

_ZT_HIST_LOCK = threading.Lock()


def fetch_pool_count(kind, ymd):
    """查某一天的涨/跌停家数。kind='ZT' 或 'DT'；返回 {'tc':家数,'qdate':日期} 或 None。"""
    if kind not in ("ZT", "DT"):
        return None
    sort = "fbt%3Aasc" if kind == "ZT" else "fund%3Aasc"
    url = ("%sgetTopic%sPool?ut=%s&dpt=wz.ztzt&Pageindex=0&pagesize=300&sort=%s&date=%s"
           % (EX_BASE, kind, EX_UT, sort, ymd))
    d = http_json(url, EX_HEADERS, timeout=8, retries=2)
    if not d or d.get("rc") != 0 or not d.get("data"):
        return None
    data = d["data"]
    tc = to_num(data.get("tc"))
    if tc is None:
        tc = len(data.get("pool") or [])
    return {"tc": int(tc), "qdate": str(data.get("qdate") or "")}


def _zt_hist_zt(dstr):
    """返回 (日期, 涨停家数) 或 None。先只抓涨停池，跌停池留给有数据的日期。"""
    r = fetch_pool_count("ZT", dstr.replace("-", ""))
    return (dstr, r["tc"]) if r else None


def _zt_hist_dt(dstr):
    r = fetch_pool_count("DT", dstr.replace("-", ""))
    return (r["tc"] if r else 0)


def _zt_hist_full(days):
    """并发扫最近 days 个工作日，返回有数据的 [{d, zt, dt}]（升序）。

    东财池的历史深度实测只有 ~15 个交易日，再往前 tc 恒为 0。所以这里默认只扫
    22 个工作日（刚好盖住 + 一点缓冲）：扫太多纯属给东财送了 30 个必然返回 0
    的请求，白等半分钟。真要更长历史，把 days 调大即可，逻辑不用动。
    """
    dates = list(reversed(last_trading_dates(days)))   # 旧 -> 新

    # 两段并发：先筛涨停池，再只对有效日期补跌停池（省一半请求）
    with ThreadPoolExecutor(max_workers=ZT_HIST_WORKERS) as ex:
        zt_rows = [x for x in ex.map(_zt_hist_zt, dates) if x]
    valid = [(d, zt) for d, zt in zt_rows if zt > 0]
    if not valid:
        return []
    with ThreadPoolExecutor(max_workers=ZT_HIST_WORKERS) as ex:
        dt_vals = list(ex.map(lambda x: _zt_hist_dt(x[0]), valid))

    rows = [{"d": d, "zt": int(zt), "dt": int(dt)}
            for (d, zt), dt in zip(valid, dt_vals)]
    rows.sort(key=lambda x: x["d"])
    return rows


def _zt_hist_load():
    d = load_json(ZT_HIST_FILE, None)
    if isinstance(d, dict) and isinstance(d.get("days"), list) and d["days"]:
        return {"day": str(d.get("day") or ""), "full_ts": float(d.get("full_ts") or 0),
                "days": d["days"], "live_ts": 0.0}
    return None


def build_zt_history(days=ZT_HIST_DAYS):
    """最近可得的交易日涨跌停家数（升序），内存 + 磁盘双缓存。

    加锁：bootstrap 预热和 /api/zt-kline 的首次请求可能同时进来，
    不锁的话两边会各自并发扫 45 个请求，把东财怼到限流。
    """
    with _ZT_HIST_LOCK:
        return _build_zt_history_locked(days)


def _build_zt_history_locked(days):
    now = time.time()
    today_str = datetime.now().strftime("%Y-%m-%d")
    cache = STATE.get("zt_hist")

    # 跨天 / 从没抓过 / 历史缓存太旧 -> 全量重扫
    need_full = (not cache
                 or cache.get("day") != today_str
                 or now - float(cache.get("full_ts") or 0) > ZT_FULL_TTL)

    if need_full:
        rows = _zt_hist_full(days)
        if rows:
            cache = {"day": today_str, "full_ts": now, "days": rows, "live_ts": now}
            STATE["zt_hist"] = cache
            save_json(ZT_HIST_FILE, {"day": today_str, "full_ts": now, "days": rows})
        elif cache:
            # 全量扫失败（东财限流/断网）就继续用旧缓存，别把图清空
            cache["full_ts"] = now
        else:
            return []
    elif now - float(cache.get("live_ts") or 0) > ZT_LIVE_TTL:
        # 同一天内：历史日不会变，只刷新当日那根
        today_row = _zt_hist_zt(today_str)
        if today_row and today_row[1] > 0:
            rows = [r for r in cache["days"] if r["d"] != today_str]
            rows.append({"d": today_str, "zt": int(today_row[1]),
                         "dt": int(_zt_hist_dt(today_str))})
            rows.sort(key=lambda x: x["d"])
            cache["days"] = rows
            save_json(ZT_HIST_FILE, {"day": cache["day"],
                                     "full_ts": cache["full_ts"], "days": rows})
        cache["live_ts"] = now

    return (STATE.get("zt_hist") or {}).get("days") or []


def zt_kline_payload(limit=30):
    """给前端的涨跌停 K 线数据。OHLC 口径：O=前一交易日收盘，C=当日收盘，无影线。

    「家数」这种日频单值序列本来就没有真实开高低收，硬造影线反而误导 ——
    所以 O 取前一日收盘，蜡烛实体长度＝家数的增减幅度，颜色＝方向。
    当日那根是活的：盘中每 20 秒跟着最新涨停数跳动。
    """
    rows = build_zt_history(days=ZT_HIST_DAYS)
    today_str = datetime.now().strftime("%Y-%m-%d")

    out = []
    prev_zc = prev_dc = None
    for r in rows:
        zc, dc = int(r["zt"]), int(r["dt"])
        zo = prev_zc if prev_zc is not None else zc
        do_ = prev_dc if prev_dc is not None else dc
        out.append({
            "d": r["d"], "zt": zc, "dt": dc,
            "live": r["d"] == today_str,
            "zo": zo, "zc": zc, "zl": min(zo, zc), "zh": max(zo, zc),
            "do": do_, "dc": dc, "dl": min(do_, dc), "dh": max(do_, dc),
        })
        prev_zc, prev_dc = zc, dc

    tail = out[-limit:]
    return {
        "ready": bool(tail),
        "days": len(tail),
        "today": today_str,
        "source": "东方财富 涨停池/跌停池（与看板卡片同口径）",
        "series": tail,
        "updated": datetime.now().strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------- 全局状态

STATE = {
    "snapshot": None,      # 最近一次成功的完整数据
    "intraday": [],        # 今日情绪分时 [{time, score}]
    "history": {},         # 历史交易日 {date: {c, zt, maxh, yz, approx}}
    "zt_hist": None,       # 涨跌停家数历史缓存（见 build_zt_history）
    "last_ok": 0,
    "last_error": "",
    "refresh_count": 0,
}
LOCK = threading.Lock()


def is_trading_now(now=None):
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= t <= (15 * 60 + 5)


def previous_trading_day():
    d = datetime.now() - timedelta(days=1)
    for _ in range(10):
        if d.weekday() < 5:
            info, total = fetch_limit_up_pool(d.strftime("%Y%m%d"), limit=1)
            if total > 0:
                return d.strftime("%Y-%m-%d"), d.strftime("%Y%m%d")
        d -= timedelta(days=1)
    return None, None


def do_refresh():
    """执行一次完整数据刷新。"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_ymd = today_str.replace("-", "")
    ok = True
    err = ""

    snap = fetch_market_snapshot()
    if not snap:
        return False, "全市场行情抓取失败"

    stats = calc_market_stats(snap)

    # 同花顺涨停池：连板高度 / 梯队 / 一字板 / 涨停原因
    pool, pool_total = fetch_limit_up_pool(today_ymd, limit=200)
    heights = {}
    max_height = 1
    one_word_up = 0
    board_list = []
    for s in pool:
        h = parse_board_height(s.get("high_days"))
        heights[h] = heights.get(h, 0) + 1
        max_height = max(max_height, h)
        if s.get("limit_up_type") == "一字板":
            one_word_up += 1
        board_list.append({
            "code": s.get("code"), "name": s.get("name"),
            "h": h, "type": s.get("limit_up_type"),
            "reason": s.get("reason_type") or "",
            "order": round((s.get("order_amount") or 0) / 1e8, 2),  # 封单(亿)
            "open_num": s.get("open_num") or 0,
        })
    board_list.sort(key=lambda x: (-x["h"], -x["order"]))
    stats["max_height"] = max_height
    stats["one_word_up"] = one_word_up if one_word_up else stats["one_word_up"]
    stats["heights"] = heights
    stats["ladder"] = [{"h": h, "n": n} for h, n in sorted(heights.items(), reverse=True)]
    stats["boards"] = board_list[:20]

    # 昨日涨停今日溢价 + 连板晋级率
    prev_str, prev_ymd = previous_trading_day()
    stats["prev_day"] = prev_str or ""
    premium = 0.0
    promo_rate = 25.0
    prev_pool, prev_total = [], 0
    if prev_ymd:
        prev_pool, prev_total = fetch_limit_up_pool(prev_ymd, limit=200)
        if prev_total > 0:
            vals = []
            for s in prev_pool:
                q = snap.get(str(s.get("code")))
                if q and isinstance(q.get("pct"), (int, float)):
                    vals.append(q["pct"])
            if vals:
                premium = round(sum(vals) / len(vals), 2)
            advanced = sum(1 for s in pool if parse_board_height(s.get("high_days")) >= 2)
            promo_rate = round(advanced / prev_total * 100, 1) if prev_total else 25.0
    stats["premium"] = premium
    stats["promo_rate"] = promo_rate
    stats["prev_zt_total"] = len(prev_pool) if prev_ymd else 0

    stats["score"] = calc_score(stats)
    stats["core"] = calc_core_score(stats["limit_up"], max_height, one_word_up)
    status, color, advice = judge(stats["score"], stats["broken_ratio"])
    stats["status"] = status
    stats["color"] = color
    stats["advice"] = advice

    stats["indices"] = fetch_indices()
    stats["time"] = datetime.now().strftime("%H:%M:%S")
    stats["date"] = today_str
    stats["trading"] = is_trading_now()

    with LOCK:
        STATE["snapshot"] = stats
        STATE["last_ok"] = time.time()
        STATE["last_error"] = err
        STATE["refresh_count"] += 1

        # 今日情绪分时：仅交易时段记录，对齐到交易分钟轴（9:30-11:30 / 13:00-15:00），
        # 非交易时段（午休、盘后、周末）不追加，避免污染分时图。同分钟只保留最后一个点。
        tl = STATE["intraday"]
        if is_trading_now():
            now_min = stats["time"][:5]  # HH:MM
            if not tl or tl[-1].get("min") != now_min:
                tl.append({"time": stats["time"], "min": now_min,
                           "score": stats["score"], "core": stats["core"]})
                if len(tl) > 480:
                    tl.pop(0)

        # 今日日 K：核心分与历史同口径，取真实日内开/收/最低/最高
        if tl:
            cs = [x.get("core", x["score"]) for x in tl]
            STATE["history"][today_str] = {
                "o": cs[0], "c": cs[-1], "l": min(cs), "h": max(cs),
                "zt": stats["limit_up"], "maxh": max_height,
                "yz": one_word_up, "full": stats["score"],
            }
        save_json(INTRADAY_FILE, {"date": today_str, "series": tl})

    return True, ""


def refresh_worker():
    while True:
        try:
            ok, err = do_refresh()
            with LOCK:
                if not ok and not STATE["snapshot"]:
                    STATE["last_error"] = err
                    STATE["last_ok"] = 0
                elif not ok:
                    STATE["last_error"] = err
            if not ok:
                print("[warn] 刷新失败：%s" % err)
            else:
                with LOCK:
                    s = STATE["snapshot"]
                print("[ok] %s 情绪分 %s (%s) 涨停%s 跌停%s 炸板%s%% 高度%s板"
                      % (s["time"], s["score"], s["status"], s["limit_up"],
                         s["limit_down"], s["broken_ratio"], s["max_height"]))
        except Exception as e:
            print("[error] 刷新异常：%r" % (e,))
        time.sleep(10 if is_trading_now() else 60)


def bootstrap():
    # 涨跌停 K 线先读盘：容器重启后不用等东财重扫就能立刻出图
    try:
        STATE["zt_hist"] = _zt_hist_load()
    except Exception:
        STATE["zt_hist"] = None
    STATE["history"] = load_json(HISTORY_FILE, {})
    # 分时序列跨进程重启不续接：避免旧公式 / 跨会话残留点污染今日 K 线，
    # 每次启动从当前值起重新累计（当日 K 线由本会话分时点实时构建）。
    STATE["intraday"] = []
    print("正在回补历史情绪日线（约需 10~30 秒）...")
    if backfill_history(STATE["history"], days=30):
        save_json(HISTORY_FILE, STATE["history"])
    print("历史交易日：%d 天" % len(STATE["history"]))

    # 涨跌停家数历史放最后，就算东财限流也不该拖垮上面的情绪日线
    try:
        rows = build_zt_history(days=ZT_HIST_DAYS)
        print("涨跌停家数历史：%d 天（%s ~ %s）"
              % (len(rows), rows[0]["d"] if rows else "-", rows[-1]["d"] if rows else "-"))
    except Exception as e:
        print("[warn] 涨跌停家数历史拉取失败：%r" % (e,))


# ---------------------------------------------------------------- HTTP


