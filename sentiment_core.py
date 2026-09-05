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


# ---------------------------------------------------------------- 全局状态

STATE = {
    "snapshot": None,      # 最近一次成功的完整数据
    "intraday": [],        # 今日情绪分时 [{time, score}]
    "history": {},         # 历史交易日 {date: {c, zt, maxh, yz, approx}}
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
    STATE["history"] = load_json(HISTORY_FILE, {})
    # 分时序列跨进程重启不续接：避免旧公式 / 跨会话残留点污染今日 K 线，
    # 每次启动从当前值起重新累计（当日 K 线由本会话分时点实时构建）。
    STATE["intraday"] = []
    print("正在回补历史情绪日线（约需 10~30 秒）...")
    if backfill_history(STATE["history"], days=30):
        save_json(HISTORY_FILE, STATE["history"])
    print("历史交易日：%d 天" % len(STATE["history"]))


# ---------------------------------------------------------------- HTTP


