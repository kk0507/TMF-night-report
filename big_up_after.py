# python big_up_after.py [快取資料夾]
# KK 2026-10-05 問：(1) 像今天這種大漲（微台對前一日盤 +2.64%、收盤新高）之後幾天怎麼走
#                  (2) 這種大漲能不能事先知道（今天＝週五夜盤 +681 ＋週一日盤再 +604）
# 資料：FinMind TaiwanFuturesDaily TX（2017-05 起有夜盤），TaiwanStockPrice TAIEX（2005 起）
# 期貨用「日盤＋夜盤」連續序列，換月用比例往回調整。
import json
import os
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import requests

URL = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.environ.get("FINMIND_TOKEN", "")
CACHE = sys.argv[1] if len(sys.argv) > 1 else "."
TODAY = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")


def fm(dataset, data_id, start, tag):
    p = os.path.join(CACHE, f"cache_{tag}.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    rows = []
    for y in range(int(start[:4]), int(TODAY[:4]) + 1):
        s = max(start, f"{y}-01-01")
        e = min(TODAY, f"{y}-12-31")
        h = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
        r = requests.get(URL, params={"dataset": dataset, "data_id": data_id, "start_date": s, "end_date": e},
                         headers=h, timeout=180).json()
        rows += r.get("data", [])
    json.dump(rows, open(p, "w", encoding="utf-8"))
    return rows


def tx_sessions():
    rows = fm("TaiwanFuturesDaily", "TX", "2017-05-01", "TX_2017")
    by = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        if "/" in r["contract_date"] or not r["open"] or not r["close"]:
            continue
        by[r["date"]][r["contract_date"]]["D" if r["trading_session"] == "position" else "N"] = r
    seq = []
    for d in sorted(by):
        day = {c: s["D"] for c, s in by[d].items() if "D" in s}
        if not day:
            continue
        near = max(day, key=lambda c: day[c]["volume"])
        if "N" in by[d][near]:
            seq.append((d, "N", near))
        seq.append((d, "D", near))
    out = []
    lvl = None
    prev = None
    for d, s, c in seq:
        r = by[d][c][s]
        if prev is None:
            f = 1.0
        else:
            pd_, ps = prev
            pr = by[pd_].get(c, {}).get(ps) or by[pd_][out[-1]["ct"]][ps]
            f = lvl / pr["close"]
        out.append(dict(date=d, sess=s, o=r["open"] * f, h=r["max"] * f, l=r["min"] * f, c=r["close"] * f,
                        raw=r["close"], ct=c))
        lvl = r["close"] * f
        prev = (d, s)
    return out


def q(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def desc(xs):
    if not xs:
        return "n=0"
    up = sum(1 for x in xs if x > 0) / len(xs) * 100
    return (f"n={len(xs):4d} 平均{st.mean(xs):+6.2f}% 中位{st.median(xs):+6.2f}% 上漲{up:3.0f}% "
            f"25分位{q(xs, .25):+6.2f}% 75分位{q(xs, .75):+6.2f}%")


def corr(xs, ys):
    mx, my = st.mean(xs), st.mean(ys)
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / (len(xs) * st.pstdev(xs) * st.pstdev(ys))


def main():
    S = tx_sessions()
    D = [i for i, x in enumerate(S) if x["sess"] == "D"]
    print(f"期貨資料 {S[0]['date']} ~ {S[-1]['date']}，日盤 {len(D)} 個；最後日盤 {S[D[-1]]['date']} 收 {S[D[-1]]['raw']:.0f}")

    rows = []
    for k in range(1, len(D)):
        i, ip = D[k], D[k - 1]
        chg = (S[i]["c"] / S[ip]["c"] - 1) * 100
        night = (S[i - 1]["c"] / S[ip]["c"] - 1) * 100 if (S[i - 1]["sess"] == "N" and i - 1 > ip) else None
        hi = max(S[j]["h"] for j in range(max(0, i - 500), i))
        rec = S[i]["c"] >= hi * 0.995
        fw = {n: (S[D[k + n]]["c"] / S[i]["c"] - 1) * 100 for n in (1, 2, 3, 5, 10, 20) if k + n < len(D)}
        nn = (S[i + 1]["c"] / S[i]["c"] - 1) * 100 if (i + 1 < len(S) and S[i + 1]["sess"] == "N") else None
        path = {}
        for n in (1, 3, 5):
            if k + n < len(D):
                seg = S[i + 1: D[k + n] + 1]
                path[n] = (min(x["l"] for x in seg) / S[i]["c"] * 100 - 100,
                           max(x["h"] for x in seg) / S[i]["c"] * 100 - 100)

        def first(dn, upv, n=5):
            if k + n >= len(D):
                return None
            for x in S[i + 1: D[k + n] + 1]:
                lo = x["l"] / S[i]["c"] * 100 - 100 <= dn
                hi_ = x["h"] / S[i]["c"] * 100 - 100 >= upv
                if lo and hi_:
                    return "both"
                if lo:
                    return "down"
                if hi_:
                    return "up"
            return "none"

        rows.append(dict(date=S[i]["date"], chg=chg, night=night, rec=rec, fw=fw, nn=nn, path=path,
                         t1=first(-0.95, 0.85), t2=first(-0.45, 0.65)))

    def report(name, sel):
        print(f"\n=== {name}（{len(sel)} 次）===")
        if not sel:
            return
        print("  接著的夜盤  ", desc([r["nn"] for r in sel if r["nn"] is not None]))
        for n in (1, 2, 3, 5, 10, 20):
            print(f"  {n:2d} 天後收盤 ", desc([r["fw"][n] for r in sel if n in r["fw"]]))
        for n in (1, 3, 5):
            lo = [r["path"][n][0] for r in sel if n in r["path"]]
            hi = [r["path"][n][1] for r in sel if n in r["path"]]
            if lo:
                print(f"  {n} 天內最低 中位{st.median(lo):+.2f}% 25分位{q(lo, .25):+.2f}% 最差{min(lo):+.2f}%"
                      f"｜最高 中位{st.median(hi):+.2f}% 75分位{q(hi, .75):+.2f}%"
                      f"｜跌破 -0.95% {sum(1 for x in lo if x <= -0.95) / len(lo) * 100:.0f}%"
                      f"｜跌破 -0.45% {sum(1 for x in lo if x <= -0.45) / len(lo) * 100:.0f}%")
        for key, lab in (("t1", "5 天內先碰 -0.95% 或 +0.85%"), ("t2", "5 天內先碰 -0.45% 或 +0.65%")):
            v = [r[key] for r in sel if r[key]]
            if v:
                c = {s: v.count(s) for s in ("down", "up", "both", "none")}
                print(f"  {lab}：先跌 {c['down']}、先漲 {c['up']}、同一盤都碰 {c['both']}、都沒碰 {c['none']}")

    report("全部日盤（基準）", rows)
    big2 = [r for r in rows if r["chg"] >= 2]
    report("日盤收盤 ≥ +2%（對前一日盤）", big2)
    report("≥ +2% 且收在前高附近（今天這型）", [r for r in big2 if r["rec"]])
    report("≥ +2% 但不在前高（多半是跌深反彈）", [r for r in big2 if not r["rec"]])
    report("≥ +2.5%", [r for r in rows if r["chg"] >= 2.5])
    report("≥ +2% 且前一個夜盤已漲 ≥ 1%", [r for r in big2 if r["night"] is not None and r["night"] >= 1])
    report("全部收在前高附近的日盤", [r for r in rows if r["rec"]])
    r25 = [r for r in big2 if r["date"] >= "2025-01-01"]
    report("≥ +2%，只看 2025 年起", r25)
    print("\n2025 年起 ≥+2% 明細（當天、接著夜盤、1/3/5 天、5 天內最低）")
    for r in r25:
        print(f"  {r['date']} {r['chg']:+.2f}% 夜盤{(r['nn'] if r['nn'] is not None else 0):+.2f}% "
              + " ".join(f"{n}d{r['fw'][n]:+.2f}%" for n in (1, 3, 5) if n in r["fw"])
              + (f" 低{r['path'][5][0]:+.2f}%" if 5 in r["path"] else "") + (" 前高" if r["rec"] else ""))

    print("\n\n##### 問題二：夜盤收盤大漲後，接著的日盤（日盤收 vs 夜盤收）#####")
    pairs = []
    for k in range(1, len(D)):
        i, ip = D[k], D[k - 1]
        if S[i - 1]["sess"] != "N" or i - 1 <= ip:
            continue
        gap = (datetime.strptime(S[i]["date"], "%Y-%m-%d") - datetime.strptime(S[ip]["date"], "%Y-%m-%d")).days
        pairs.append(dict(date=S[i]["date"], n=(S[i - 1]["c"] / S[ip]["c"] - 1) * 100,
                          d=(S[i]["c"] / S[i - 1]["c"] - 1) * 100, o=(S[i]["o"] / S[i - 1]["c"] - 1) * 100,
                          mon=gap >= 3))

    def rep2(name, sel):
        print(f"  {name}: {desc([p['d'] for p in sel])}")

    rep2("全部", pairs)
    rep2("夜盤 ≥ +1%", [p for p in pairs if p["n"] >= 1])
    rep2("夜盤 ≥ +1.2%", [p for p in pairs if p["n"] >= 1.2])
    rep2("夜盤 ≤ -1%", [p for p in pairs if p["n"] <= -1])
    rep2("週末／連假後 全部", [p for p in pairs if p["mon"]])
    rep2("週末／連假後 且夜盤 ≥ +1%", [p for p in pairs if p["mon"] and p["n"] >= 1])
    print(f"  夜盤漲跌 vs 接著日盤 相關 {corr([p['n'] for p in pairs], [p['d'] for p in pairs]):+.3f}（n={len(pairs)}）")
    p25 = [p for p in pairs if p["date"] >= "2025-01-01"]
    print(f"  2025 年起相關 {corr([p['n'] for p in p25], [p['d'] for p in p25]):+.3f}（n={len(p25)}）")
    print("  夜盤 ≥ +1% 明細（2025 起）：")
    for p in p25:
        if p["n"] >= 1:
            print(f"    {p['date']} 夜盤{p['n']:+.2f}% → 日盤開{p['o']:+.2f}% 收{p['d']:+.2f}%{' 週末後' if p['mon'] else ''}")

    print("\n\n##### 加權指數 2005 起：單日 ≥ +2% 之後 #####")
    tx = fm("TaiwanStockPrice", "TAIEX", "2005-01-01", "TAIEX_2005")
    tx = sorted({r["date"]: r for r in tx if r["close"]}.values(), key=lambda r: r["date"])
    print("  資料", tx[0]["date"], "~", tx[-1]["date"], len(tx), "天，最後收盤", tx[-1]["close"])
    ev, base = [], []
    runmax = tx[0]["close"]
    for i in range(1, len(tx)):
        c = tx[i]["close"]
        chg = (c / tx[i - 1]["close"] - 1) * 100
        rec = c >= runmax
        runmax = max(runmax, c)
        fw = {n: (tx[i + n]["close"] / c - 1) * 100 for n in (1, 2, 3, 5, 10, 20) if i + n < len(tx)}
        lo5 = (min(r["min"] for r in tx[i + 1:i + 6]) / c - 1) * 100 if i + 5 < len(tx) else None
        d = dict(date=tx[i]["date"], chg=chg, rec=rec, fw=fw, lo5=lo5)
        base.append(d)
        if chg >= 2:
            ev.append(d)

    def rep3(name, sel):
        print(f"\n  -- {name}（{len(sel)} 次）")
        for n in (1, 2, 3, 5, 10, 20):
            print(f"    {n:2d} 天後 ", desc([r["fw"][n] for r in sel if n in r["fw"]]))
        lo = [r["lo5"] for r in sel if r["lo5"] is not None]
        if lo:
            print(f"    5 天內最低 中位{st.median(lo):+.2f}% 25分位{q(lo, .25):+.2f}% 跌破 -1% {sum(1 for x in lo if x <= -1) / len(lo) * 100:.0f}%")

    rep3("全部日子（基準）", base)
    rep3("單日 ≥ +2%", ev)
    rep3("單日 ≥ +2% 且收盤歷史新高（今天這型）", [r for r in ev if r["rec"]])
    rep3("單日 ≥ +2% 但不是新高", [r for r in ev if not r["rec"]])
    rep3("全部收盤歷史新高的日子", [r for r in base if r["rec"]])
    print("\n  ≥+2% 且歷史新高 明細：")
    for r in ev:
        if r["rec"]:
            print(f"    {r['date']} {r['chg']:+.2f}% " + " ".join(f"{n}d{r['fw'][n]:+.2f}%" for n in (1, 3, 5, 20) if n in r["fw"])
                  + (f" 5天內最低{r['lo5']:+.2f}%" if r["lo5"] is not None else ""))


if __name__ == "__main__":
    main()
