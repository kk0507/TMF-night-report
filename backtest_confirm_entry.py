# python backtest_confirm_entry.py -> 回測「等回落/回彈k%才進場」vs「在極值當天收盤進場」
# 需要環境變數：FINMIND_TOKEN
"""KK的操作習慣：漲到高峰回落一點點才賣(空)、跌到低谷回彈一點點才買(多)。

用微台全歷史日K(日盤+夜盤合併、換月做back-adjust)檢驗這個習慣：
極值日 = 出現10日新高(做空測試)或10日新低(做多測試)，去重(5天內只算第一次)。
策略A：極值日收盤就進場。
策略B_k：從極值日起最多等5天(含極值日)，第一次「收盤價」比目前已知極值低/高至少k%
就在那天收盤進場(只用收盤價判斷確認，避開日K看不出盤中先後順序的問題)。
出場一律相同：目標+1%(約500點)、停損=已知極值外側0.15%、最多持有5天。
遇到同一天停損跟目標都碰到一律當停損先(保守)。
"""
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

import requests

TOKEN = os.environ["FINMIND_TOKEN"]
URL = "https://api.finmindtrade.com/api/v4/data"
N, W, H = 10, 5, 5
TARGET, BUF = 0.01, 0.0015
KS = [0.005, 0.01, 0.015, 0.02]


def load_days(data_id="TMF", start="2024-07-25"):
    rows = requests.get(
        URL,
        params={"dataset": "TaiwanFuturesDaily", "data_id": data_id, "start_date": start,
                "end_date": (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")},
        headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60,
    ).json()["data"]
    g = defaultdict(dict)
    for r in rows:
        g[(r["date"], r["contract_date"])][r["trading_session"]] = r
    by_date = defaultdict(dict)
    for (d, c), s in g.items():
        by_date[d][c] = s
    days = []
    for d in sorted(by_date):
        best = max(by_date[d].items(), key=lambda kv: sum(x["volume"] for x in kv[1].values()))
        c, s = best
        pos, am = s.get("position"), s.get("after_market")
        if not pos:
            continue
        hs = [x["max"] for x in (pos, am) if x]
        ls = [x["min"] for x in (pos, am) if x]
        days.append({"date": d, "c": c, "open": (am or pos)["open"], "high": max(hs), "low": min(ls),
                     "close": pos["close"], "all": by_date[d]})
    # 換月ratio back-adjust：換月當天用「前一天新合約收盤 / 舊合約收盤」當比例，往前乘
    # (比例調整才不會在18年歷史裡把早期價格累積墊歪，報酬率%完全不受影響)
    n = len(days)
    mult = [1.0] * n
    for r in range(1, n):
        if days[r]["c"] != days[r - 1]["c"]:
            prev = days[r - 1]["all"]
            new, old = prev.get(days[r]["c"]), prev.get(days[r - 1]["c"])
            if new and old and new.get("position") and old.get("position"):
                f = new["position"]["close"] / old["position"]["close"]
                for i in range(r):
                    mult[i] *= f
    for i, dd in enumerate(days):
        for k in ("open", "high", "low", "close"):
            dd[k] *= mult[i]
    return days


def simulate(days, d, entry, entry_day, stop, first_check_day, same_day_target):
    tgt = entry * (1 + d * TARGET)
    end = entry_day + H
    for day in range(first_check_day, end + 1):
        o, h, l = days[day]["open"], days[day]["high"], days[day]["low"]
        if day == entry_day:
            if same_day_target and ((h >= tgt) if d == 1 else (l <= tgt)):
                return "T", tgt
            continue
        gap_stop = (o <= stop) if d == 1 else (o >= stop)
        hit_stop = (l <= stop) if d == 1 else (h >= stop)
        gap_tgt = (o >= tgt) if d == 1 else (o <= tgt)
        hit_tgt = (h >= tgt) if d == 1 else (l <= tgt)
        if gap_stop:
            return "S", o
        if hit_stop:
            return "S", stop
        if gap_tgt:
            return "T", o
        if hit_tgt:
            return "T", tgt
    return "X", days[end]["close"]


def run(days, d):
    n = len(days)
    highs = [x["high"] for x in days]
    lows = [x["low"] for x in days]
    events, last = [], -999
    for t in range(N - 1, n - 1 - (W + H)):
        ext = highs[t] >= max(highs[t - N + 1:t + 1]) if d == -1 else lows[t] <= min(lows[t - N + 1:t + 1])
        if ext and t - last >= 5:
            events.append(t)
            last = t
    res = {}
    ext_more = 0
    ext_pct = []
    for t in events:
        if d == -1:
            m = max(highs[t + 1:t + W + 1])
            if m > highs[t]:
                ext_more += 1
                ext_pct.append((m / highs[t] - 1) * 100)
        else:
            m = min(lows[t + 1:t + W + 1])
            if m < lows[t]:
                ext_more += 1
                ext_pct.append((1 - m / lows[t]) * 100)
        # 策略A
        entry = days[t]["close"]
        stop = highs[t] * (1 + BUF) if d == -1 else lows[t] * (1 - BUF)
        out, px = simulate(days, d, entry, t, stop, t + 1, False)
        res.setdefault("A", []).append((out, d * (px - entry) / entry * 100, abs(stop - entry) / entry * 100))
        # 策略B_k：收盤價確認
        for k in KS:
            key = f"B{k*100:.1f}%"
            prev = highs[t] if d == -1 else lows[t]
            fill = None
            for j in range(t, t + W + 1):
                prev = max(prev, highs[j]) if d == -1 else min(prev, lows[j])
                c = days[j]["close"]
                ok = (c <= prev * (1 - k)) if d == -1 else (c >= prev * (1 + k))
                if ok:
                    fill = (j, c)
                    break
            if not fill:
                res.setdefault(key, []).append(None)
                continue
            j, entry = fill
            stop = prev * (1 + BUF) if d == -1 else prev * (1 - BUF)
            out, px = simulate(days, d, entry, j, stop, j + 1, False)
            res.setdefault(key, []).append((out, d * (px - entry) / entry * 100, abs(stop - entry) / entry * 100))
    return events, ext_more, ext_pct, res


def report(name, events, ext_more, ext_pct, res):
    print(f"\n===== {name}：極值事件 {len(events)} 次 =====")
    print(f"極值日之後{W}天內又創更極端價位: {ext_more}/{len(events)} = {ext_more/len(events)*100:.0f}%"
          + (f"，平均再延伸 {statistics.mean(ext_pct):.2f}%" if ext_pct else ""))
    print(f"{'策略':<9}{'成交':>8}{'勝(達標)':>10}{'停損':>7}{'到期':>7}{'每筆均損益':>11}{'每事件均損益':>12}{'平均停損距離':>12}")
    for key, v in res.items():
        tr = [x for x in v if x]
        if not tr:
            continue
        w = sum(1 for x in tr if x[0] == "T")
        s = sum(1 for x in tr if x[0] == "S")
        xx = sum(1 for x in tr if x[0] == "X")
        pnl = [x[1] for x in tr]
        print(f"{key:<9}{len(tr):>4}/{len(v):<3}{w/len(tr)*100:>9.0f}%{s/len(tr)*100:>6.0f}%{xx/len(tr)*100:>6.0f}%"
              f"{statistics.mean(pnl):>+10.2f}%{sum(pnl)/len(v):>+11.2f}%{statistics.mean(x[2] for x in tr):>11.2f}%")


if __name__ == "__main__":
    days = load_days()
    print(f"日K {len(days)} 根 {days[0]['date']} ~ {days[-1]['date']}；目標+{TARGET*100:.1f}%、停損=極值外側{BUF*100:.2f}%、最多持有{H}天")
    for d, name in ((-1, "做空(10日新高後)"), (1, "做多(10日新低後)")):
        report(name, *run(days, d))
