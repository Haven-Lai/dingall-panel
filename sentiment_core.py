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
#    这不是 bug，也不需要"修复"。想要更长的历史只能换源 —— 见下面的同花顺回补。
#
# ⚠️ 同花顺涨停池（limit_up_pool 带 date 参数）实测能回溯 35+ 个交易日，
#    所以 30 天视图 = 东财给近 15 天 + 同花顺补前面那些天。东财优先是刻意的：
#    顶部卡片的涨停家数就是东财口径，最近这段必须跟卡片严丝合缝。
#
# ⚠️ 跌停家数没有第二个源：同花顺没有跌停池接口（dataapi/limit_down/* 全是 404），
#    东财跌停池的历史深度跟涨停池一样只有 ~15 个交易日。所以跌停那条线只能
#    画最近 15 天，更早的日期给 null 让线断开 —— 绝不能用别的口径凑数。

EX_BASE = "https://push2ex.eastmoney.com/"
EX_UT = "7eea3edcaed734bea9cbfc24409ed989"
EX_HEADERS = {"Referer": "https://quote.eastmoney.com/"}

ZT_HIST_FILE = os.path.join(DATA_DIR, "zt_history.json")

# 历史日一整天不会变，但当日那根要跟着盘中跳动，所以拆成两档 TTL。
# 全量重扫一次要打 ~90 个上游请求（东财 45 + 同花顺 30 + 跌停 15），别调太勤；
# 历史日是定值，只有跨天才必须重扫，TTL 只是兜底（东财偶尔补数据慢）。
ZT_FULL_TTL = 10800
ZT_LIVE_TTL = 20

# 要凑满 30 个交易日：last_trading_dates 只按工作日筛，不认节假日，
# 国庆那种长假会白占 5~7 个名额，所以扫 45 个工作日留足余量。
#
# ⚠️ 并发别往上调。本地 10 并发 0.5 秒扫完，但**线上不是一回事**：Render 出海单次
#    往返 550~750ms，10 并发排队就能把一部分请求顶过 timeout，结果是图只剩 7 天
#    而且日期是散的（11-03/04/06/09/17/19/23）—— 看着像上游历史不够，其实是自己超时。
#    串行/低并发实测 18 天全部成功（/api/zt-diag 可复现）。
#    现在 6 并发 + 失败降 3 并发补扫一轮，冷启动约 10 秒，靠磁盘缓存兜住重复请求。
ZT_HIST_DAYS = 45
ZT_HIST_WORKERS = 6
ZT_HIST_RETRY_WORKERS = 3
ZT_HIST_THS_WORKERS = 4

# 磁盘缓存只做加法：没被窗口盖住的老日子留着，跌停家数就能跟着天累积。
# 攒到 ZT_HIST_KEEP 天就从头砍，防止文件无限长。
ZT_HIST_KEEP = 120

_ZT_HIST_LOCK = threading.Lock()


def fetch_pool_count(kind, ymd):
    """查某一天的涨/跌停家数。kind='ZT' 或 'DT'。

    返回 {'tc': 家数, 'qdate': 日期, 'first': 首板数|None}；跌停池没有 'first'。

    'first'＝连板数(lbc) 恰好为 1 的家数，从同一个响应的 pool 里数出来，
    **不额外发请求**。算不出来时给 None 而不是 0 —— 0 会被读成「当天没有首板」，
    跟真实情况正好相反。
    """
    if kind not in ("ZT", "DT"):
        return None
    sort = "fbt%3Aasc" if kind == "ZT" else "fund%3Aasc"
    url = ("%sgetTopic%sPool?ut=%s&dpt=wz.ztzt&Pageindex=0&pagesize=300&sort=%s&date=%s"
           % (EX_BASE, kind, EX_UT, sort, ymd))
    # timeout 给 12 秒：本地 8 秒够，但 Render 出海再回来经常 6~9 秒，
    # 卡在 8 秒会把本来能成的请求判死，读出来就是「历史深度只有 7 天」。
    d = http_json(url, EX_HEADERS, timeout=12, retries=2)
    if not d or d.get("rc") != 0 or not d.get("data"):
        return None
    data = d["data"]
    pool = data.get("pool") or []
    tc = to_num(data.get("tc"))
    if tc is None:
        tc = len(pool)
    out = {"tc": int(tc), "qdate": str(data.get("qdate") or "")}
    if kind == "ZT":
        # pagesize=300 够用（历史峰值 125 家）。真被截断就宁可给 None，
        # 也不要拿半页明细算出一个偏小的首板数。
        truncated = int(tc) > len(pool)
        lbc = [to_num(p.get("lbc")) for p in pool]
        lbc = [v for v in lbc if v is not None]
        out["first"] = (sum(1 for v in lbc if v == 1)
                        if (lbc and not truncated) else None)
    return out


def _zt_hist_zt(dstr):
    """返回 (日期, 涨停家数, 首板数) 或 None。跌停池留给有数据的日期再补。"""
    r = fetch_pool_count("ZT", dstr.replace("-", ""))
    return (dstr, r["tc"], r.get("first")) if r else None


def _zt_hist_dt(dstr):
    """东财跌停家数。失败给 None（= 没数据，线断开），**不能给 0**。"""
    r = fetch_pool_count("DT", dstr.replace("-", ""))
    return (r["tc"] if r else None)


def _ths_first_count(info):
    """从同花顺 high_days 数出「连板数为 1」的家数，对齐东财的 lbc == 1。

    同花顺的标签有两类写法，含义完全不同：
      "首板" / "2天2板" / "5天5板"  -> N 天 N 板，是连续板，连板数 = N
      "5天3板" / "7天5板"           -> N 天 M 板（N≠M），中途断过板，
                                       今天这次是重新起的第一板，连板数 = 1
    只按 "首板" 字面数会漏掉后一类，实测会少 3~15 家（涨停多的大日子漏得最多），
    所以必须补上。补完跟东财 lbc==1 逐日对比：多数日子完全相等，个别日子差 ≤4 只。
    """
    n = 0
    for s in info:
        h = (s.get("high_days") or "").strip()
        if h == "首板":
            n += 1
            continue
        m = re.match(r"^(\d+)天(\d+)板$", h)
        if m and m.group(1) != m.group(2):
            n += 1
    return n


def _zt_hist_ths(dstr):
    """同花顺回补某一天的涨停/首板（东财历史深度外才用）。返回同结构元组或 None。"""
    info, total = fetch_limit_up_pool(dstr.replace("-", ""), limit=200)
    if total <= 0 or not info:
        return None
    zt = int(total)
    # limit 200 是上游硬上限，历史峰值 125 家。真被截断就宁可给 None，
    # 也不要拿半页明细算出一个偏小的首板数（会画成"首板突然塌了"）。
    first = _ths_first_count(info) if zt <= len(info) else None
    return (dstr, zt, first)


def _zt_scan_em(dates, workers):
    """低并发扫东财涨停池，返回 (成功 {日期: (zt, first)}, 失败日期列表)。"""
    ok, bad = {}, []
    if not dates:
        return ok, bad
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for d, r in zip(dates, ex.map(_zt_hist_zt, dates)):
            if r and r[1] > 0:
                ok[d] = (int(r[1]), (int(r[2]) if r[2] is not None else None))
            else:
                bad.append(d)
    return ok, bad


def _dt_scan(dates, workers):
    """扫跌停池，返回 {日期: 家数}；抓失败的日期直接不进字典（= 留 null）。"""
    ok = {}
    if not dates:
        return ok
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for d, v in zip(dates, ex.map(_zt_hist_dt, dates)):
            if v is not None:
                ok[d] = int(v)
    return ok


def _zt_hist_full(days):
    """扫最近 days 个工作日，返回有数据的 [{d, zt, dt, first, src}]（升序）。

    三段式：东财涨停池（失败降并发补一轮）-> 同花顺补东财没覆盖到的日子
    -> 跌停池只问东财认得的日期（深度外问了恒为 0，白送请求还会画错）。
    """
    dates = list(reversed(last_trading_dates(days)))   # 旧 -> 新

    em_ok, em_bad = _zt_scan_em(dates, ZT_HIST_WORKERS)
    if em_bad:
        # 第二轮降并发补扫。线上并发 6 + 12 秒超时偶尔还会丢一两天，
        # 不补这一轮的话图上的日期会变成断断续续的一串，看着像数据丢了。
        em2, em_bad = _zt_scan_em(em_bad, ZT_HIST_RETRY_WORKERS)
        em_ok.update(em2)

    got = {d: (v[0], v[1], "em") for d, v in em_ok.items()}

    missing = [d for d in dates if d not in got]
    if missing:
        with ThreadPoolExecutor(max_workers=ZT_HIST_THS_WORKERS) as ex:
            for r in ex.map(_zt_hist_ths, missing):
                if r and r[1] > 0:
                    got[r[0]] = (int(r[1]),
                                 (int(r[2]) if r[2] is not None else None), "ths")

    if not got:
        return []
    valid = sorted(got)

    dt_dates = [d for d in valid if got[d][2] == "em"]
    dt_map = _dt_scan(dt_dates, ZT_HIST_WORKERS)
    lost = [d for d in dt_dates if d not in dt_map]
    if lost:
        dt_map.update(_dt_scan(lost, ZT_HIST_RETRY_WORKERS))

    return [{"d": d, "zt": got[d][0], "dt": dt_map.get(d),
             "first": got[d][1], "src": got[d][2]} for d in valid]


def zt_hist_diag(days=18):
    """排查用：**串行**逐个问东财涨停池/跌停池，看线上到底哪几天拿不到、各花多久。

    线上跟本地不是一回事（Render 出海、东财对历史查询限流），出问题时不要靠猜，
    直接 curl /api/zt-diag 看每一行的 tc 和耗时。
    """
    rows = []
    for d in reversed(last_trading_dates(days)):
        t0 = time.time()
        zt = fetch_pool_count("ZT", d.replace("-", ""))
        dt = fetch_pool_count("DT", d.replace("-", ""))
        rows.append({
            "d": d,
            "zt": (zt or {}).get("tc"),
            "first": (zt or {}).get("first"),
            "dt": (dt or {}).get("tc"),
            "ms": int((time.time() - t0) * 1000),
        })
    return {"days": len(rows), "rows": rows,
            "hint": "zt/dt 为 null = 该日请求失败；tc=0 = 上游没有这天的数据"}


def _zt_hist_load():
    d = load_json(ZT_HIST_FILE, None)
    if isinstance(d, dict) and isinstance(d.get("days"), list) and d["days"]:
        return {"day": str(d.get("day") or ""), "full_ts": float(d.get("full_ts") or 0),
                "days": d["days"], "live_ts": 0.0}
    return None


def _merge_rows(new_rows, old_rows):
    """新旧历史按日期合并，字段级「有值优先」——磁盘缓存只做加法。

    两个上游池都只留 ~15 个交易日，窗口每往前滚一天，掉出去那天的跌停家数就
    永久查不回来了。所以必须把每天的结果攒进磁盘，跌停线才能随时间自己补齐。
    合并时新扫到的那行不覆盖旧的 dt —— 新扫的行 dt 可能是 None（东财那一下
    抽风），拿 None 盖掉已经攒到的数字等于自毁历史。
    """
    by_day = {}
    for r in old_rows or []:
        if isinstance(r, dict) and r.get("d"):
            by_day[r["d"]] = r
    for r in new_rows or []:
        old = by_day.get(r["d"])
        if old:
            m = dict(old)
            m.update({k: v for k, v in r.items() if v is not None})
            by_day[r["d"]] = m
        else:
            by_day[r["d"]] = r
    return [by_day[d] for d in sorted(by_day)][-ZT_HIST_KEEP:]


def build_zt_history(days=ZT_HIST_DAYS):
    """最近可得的交易日涨跌停家数（升序），内存 + 磁盘双缓存。

    加锁：bootstrap 预热和 /api/zt-kline 的首次请求可能同时进来，
    不锁的话两边会各自并发扫 90 个请求，把东财/同花顺怼到限流。
    """
    with _ZT_HIST_LOCK:
        return _build_zt_history_locked(days)


def _build_zt_history_locked(days):
    now = time.time()
    today_str = datetime.now().strftime("%Y-%m-%d")
    cache = STATE.get("zt_hist")

    # 跨天 / 从没抓过 / 历史缓存太旧 / 旧缓存里没有 first 字段（加首板线之前
    # 存的）/ 旧缓存只有 15 天（加同花顺回补之前存的，上限卡在 ZT_HIST_DAYS
    # 的老值）-> 全量重扫。漏掉这些判据的话，旧缓存会一直生效到跨天，
    # 用户看到的就是「改了代码但还是 15 天」，排查起来极容易误判。
    rows_ready = bool(cache and cache.get("days")
                      and all("first" in r for r in cache["days"]))
    need_full = (not cache or not rows_ready
                 or cache.get("day") != today_str
                 or now - float(cache.get("full_ts") or 0) > ZT_FULL_TTL
                 or len(cache.get("days") or []) < 30)

    if need_full:
        rows = _zt_hist_full(days)
        if rows:
            rows = _merge_rows(rows, (cache or {}).get("days"))
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
                         "dt": _zt_hist_dt(today_str),
                         "first": (int(today_row[2])
                                   if today_row[2] is not None else None),
                         "src": "em"})
            rows = _merge_rows(rows, cache["days"])
            cache["days"] = rows
            save_json(ZT_HIST_FILE, {"day": cache["day"],
                                     "full_ts": cache["full_ts"], "days": rows})
        cache["live_ts"] = now

    return (STATE.get("zt_hist") or {}).get("days") or []


def zt_kline_payload(limit=30):
    """给前端的「每日收盘涨跌停家数 + 首板数」序列（同坐标系三条线）。

    东财涨停池/跌停池的 tc 字段本身就是「该交易日截至当时的家数」：查历史日期
    拿到的是那天收盘的定值，查今天拿到的是实时值。所以这个序列天然就是收盘口径，
    不用再自己做日线合成。

    first（首板数）来自涨停池每条记录的 lbc（连板数）字段，取 lbc==1 的家数。
    它和涨停家数是父子关系（首板 ⊆ 涨停），同轴画出来一眼能看出涨停的成色：
    涨停多但首板少＝资金在抱团高位、新资金不敢接；涨停少但全是首板＝冰点里
    有新资金试探。这是超短最该看的结构信号，单看总数看不出来。

    唯一需要区分的是「今天还没收盘」——那天数字会跟着盘中跳动，标 final=False，
    前端用空心点画出来，免得把盘中数当成收盘数。
    """
    rows = build_zt_history(days=ZT_HIST_DAYS)
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    closed_now = now.strftime("%H:%M") >= "15:00"     # A 股 15:00 收盘

    out = []
    for r in rows:
        is_today = r["d"] == today_str
        dtv = r.get("dt")
        out.append({
            "d": r["d"],
            "zt": int(r["zt"]),
            # 跌停只有东财一个源，历史深度外为 None —— 必须原样透出 null 让前端
            # 断线。兜 0 会画成「那天一家跌停都没有」，跟事实正好相反。
            "dt": (int(dtv) if dtv is not None else None),
            "first": (int(r["first"]) if r.get("first") is not None else None),
            "live": is_today,                        # 兼容旧前端字段
            "final": (not is_today) or closed_now,   # 是否已是收盘定值
        })

    tail = out[-limit:]
    dt_from = next((x["d"] for x in tail if x["dt"] is not None), "")
    return {
        "ready": bool(tail),
        "days": len(tail),
        "today": today_str,
        "closed": closed_now,
        "dt_from": dt_from,       # 跌停家数最早可得的日期（更早的日子上游没有）
        "source": ("东方财富 涨停池/跌停池（与看板卡片同口径；首板＝连板数 1 的家数）；"
                   "东财只有近 15 个交易日，更早的涨停/首板由同花顺涨停池补齐，"
                   "跌停无第二数据源，故仅近 15 日"),
        "series": tail,
        "updated": now.strftime("%H:%M:%S"),
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


# ---------------------------------------------------------------- 涨停个股明细
#
# 「连板数」和「板块」只有东财涨停池有（lbc / hybk），
# 「题材」只有同花顺涨停池有（reason_type，形如 "算力租赁+色纺纱"）。
# 所以这个视图必须两个源合起来，单靠任何一个都做不出来：
#   东财 getTopicZTPool -> 以它为主（连板、板块、封单、首封时间）
#   同花顺 limit_up_pool -> 按代码补一列题材
# 两次请求都是现成的，不额外增加上游压力。

ZT_STOCK_TTL = 25          # 情绪快照 40 秒一刷，这里稍快些，新涨停能先看到
_ZT_STOCK_LOCK = threading.Lock()


def _fbt_str(v):
    """东财 fbt 是 93133 这种整数（HHMMSS）-> '09:31:33'。"""
    try:
        s = str(int(v)).zfill(6)
    except Exception:
        return ""
    return "%s:%s:%s" % (s[0:2], s[2:4], s[4:6])


def _fetch_zt_pool_rich(ymd):
    """东财涨停池明细，返回 (pool, qdate)。

    ⚠️ qdate 是东财回给的真实交易日：盘前、周末、节假日查「今天」时它给的是
    上一个交易日。必须拿它去问同花顺要题材，否则两边日期错位，题材会整列全空。
    """
    url = ("%sgetTopicZTPool?ut=%s&dpt=wz.ztzt&Pageindex=0&pagesize=300&sort=fbt%%3Aasc&date=%s"
           % (EX_BASE, EX_UT, ymd))
    d = http_json(url, EX_HEADERS, timeout=8, retries=2)
    if not d or d.get("rc") != 0 or not d.get("data"):
        return [], ""
    data = d["data"]
    return (data.get("pool") or []), str(data.get("qdate") or "")


def _fetch_ths_reason(ymd):
    """同花顺涨停原因 {代码: '题材A+题材B'}。失败返回 {}，绝不能拖垮整个列表。"""
    info, _total = fetch_limit_up_pool(ymd, limit=200)
    out = {}
    for s in info:
        c = str(s.get("code") or "").strip()
        r = (s.get("reason_type") or "").strip()
        if c and r:
            out[c] = r
    return out


def _build_zt_stocks():
    today_ymd = datetime.now().strftime("%Y%m%d")
    pool, qdate = _fetch_zt_pool_rich(today_ymd)
    if not pool:
        return None
    ymd = qdate or today_ymd
    reasons = _fetch_ths_reason(ymd)

    rows = []
    for p in pool:
        code = str(p.get("c") or "").strip()
        if not code:
            continue
        lbc = to_num(p.get("lbc"))
        lbc = int(lbc) if lbc else 1          # 东财没给就当首板，别让它掉出列表
        zttj = p.get("zttj") or {}
        rows.append({
            "code": code,
            "name": (p.get("n") or "").strip(),
            "lbc": lbc,
            "board": (p.get("hybk") or "").strip(),        # 行业板块
            "reason": reasons.get(code, ""),               # 题材（同花顺）
            "fund": round((to_num(p.get("fund")) or 0) / 1e8, 2),    # 封单(亿)
            "fbt": _fbt_str(p.get("fbt")),                 # 首次封板时间
            "zbc": int(to_num(p.get("zbc")) or 0),         # 炸板次数
            "hs": round(float(to_num(p.get("hs")) or 0), 1),
            "amount": round((to_num(p.get("amount")) or 0) / 1e8, 2),
            "pct": round(float(to_num(p.get("zdp")) or 0), 2),
            "days": int(to_num(zttj.get("days")) or 0),    # 「N天M板」里的 N
            "dct": int(to_num(zttj.get("ct")) or 0),       # 「N天M板」里的 M
        })

    groups = []
    for lbc in sorted({r["lbc"] for r in rows}, reverse=True):
        items = [r for r in rows if r["lbc"] == lbc]
        # 同一梯队里封单大的排前面：封单就是资金决心，超短先看这个
        items.sort(key=lambda x: (-x["fund"], x["fbt"] or "99:99:99"))
        groups.append({
            "lbc": lbc,
            "label": ("首板" if lbc == 1 else "%d连板" % lbc),
            "count": len(items),
            "stocks": items,
        })

    return {
        "ok": True,
        "ymd": ymd,
        "date": ("%s-%s-%s" % (ymd[0:4], ymd[4:6], ymd[6:8])) if len(ymd) == 8 else ymd,
        "live": is_trading_now(),
        "total": len(rows),
        "with_reason": sum(1 for r in rows if r["reason"]),
        "groups": groups,
        "updated": datetime.now().strftime("%H:%M:%S"),
        "source": "东方财富 涨停池（连板/板块/封单）＋ 同花顺 涨停原因（题材）",
    }


def zt_stocks_payload():
    """涨停个股按连板数分组，每只附题材与板块。

    25 秒内存缓存；抓失败退回上一次的结果，不把已经看到的列表清空。
    """
    now = time.time()
    with _ZT_STOCK_LOCK:
        cache = STATE.get("zt_stocks")
        if cache and now - float(cache.get("ts") or 0) < ZT_STOCK_TTL:
            return cache["data"]
        data = _build_zt_stocks()
        if data:
            STATE["zt_stocks"] = {"ts": now, "data": data}
            return data
        return cache["data"] if cache else None


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


