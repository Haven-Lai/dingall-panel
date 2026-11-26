# -*- coding: utf-8 -*-
"""把攒到的涨跌停 / 炸板历史落成种子文件，用来跨部署保留历史。

为什么需要这个脚本：
  东财跌停池、炸板池的历史深度只有 14~15 个交易日，过了窗口就永久查不回来，
  而同花顺没有这两类池的接口（全是 404）。Render 免费实例**没有持久磁盘**，
  每次部署都从 git 重建文件系统、data/ 清零 —— 部署一次，那两条短线就短一截。
  所以把已经抓到的日子落成 zt_hist_seed.json 提交上去，服务启动时先吃种子。

用法（在 _repo 目录下跑）：
    python tools/dump_zt_seed.py

建议每次推代码上线前跑一次。不跑也不会坏，只是跌停/炸板线没法往前长。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

RUNTIME = os.path.join(BASE, "data", "zt_history.json")
SEED = os.path.join(BASE, "zt_hist_seed.json")
KEEP = 120          # 最多留多少个交易日，别让文件无限长


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        days = d.get("days") if isinstance(d, dict) else None
        return [r for r in (days or []) if isinstance(r, dict) and r.get("d")]
    except Exception:
        return []


def main():
    by_day = {}
    for path in (SEED, RUNTIME):        # 运行时后读，字段级「有值优先」
        for r in _load(path):
            old = by_day.get(r["d"])
            if old:
                m = dict(old)
                m.update({k: v for k, v in r.items() if v is not None})
                by_day[r["d"]] = m
            else:
                by_day[r["d"]] = r

    days = [by_day[d] for d in sorted(by_day)][-KEEP:]
    if not days:
        print("没有可用的历史数据，先让服务跑一遍再执行")
        return 1

    out = {
        "note": ("涨跌停/炸板家数历史种子：东财跌停池、炸板池的历史深度只有 14~15 个交易日，"
                 "过了窗口就永久查不到，而 Render 免费实例每次部署会清空 data/ —— "
                 "所以把抓到的日子提交上来，启动时先吃种子再合并实时结果。"
                 "由 tools/dump_zt_seed.py 生成，别手工改。"),
        "generated": days[-1]["d"],
        "days": days,
    }
    with open(SEED, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, separators=(",", ":")))

    print("种子已更新：%d 天（%s ~ %s），跌停 %d 天、炸板 %d 天，%.1f KB"
          % (len(days), days[0]["d"], days[-1]["d"],
             sum(1 for r in days if r.get("dt") is not None),
             sum(1 for r in days if r.get("zb") is not None),
             os.path.getsize(SEED) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
