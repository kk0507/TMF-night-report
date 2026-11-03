# python overnight_shadow.py -> 隔夜多影子：收盤前虛擬買 1 口微台、隔天 09:00 賣，記到 on_trades.csv
# 需要環境變數：FINMIND_TOKEN, DISCORD_WEBHOOK_URL（影子頻道）
"""KK 2026-10-24 定的「隔夜多」規則（Threads true.liu.92 的策略包，我們 10/24 回測、11/4 AI 策略研究第 2 輪確認
這套規則比各種過濾都好），11/4 KK：「放影子」。跑到 12 月合約結算（12/16），跟其他影子一起出報告。

規則（開跑前定案，期間不改）：
- 決定時間＝每個交易日 13:45 日盤收盤。原版 13:43:30 送單，收盤集合競價成交＝收盤價，影子用當天日盤收盤價。
- 買：週一～三；週五要收在 5 日線上（同一個合約、日盤收盤、含今天）；週四不買；
  下一個交易日離今天超過 4 天不買（連假前）；ATR14 × 0.75 > 1,000 點不買（日盤＋夜盤合併一根，含跳空）。
- 賣：下一個交易日 09:00（開盤後第 3 根 5 分 K 收盤），也記 08:45 開盤價對照。不掛停損。
- 每期（結算到結算）虧損上限 15,000 元：KK 的規則是跟他自己的單共用，影子看不到他的單，只算影子自己的。
- 一次 1 口，成本一趟 78 元（KK 券商明細推的：手續費 40＋期交稅＋滑價各 1 點）。

排程延遲（GitHub 最近常晚好幾個小時）：不用「跑的時候的價格」，一律回頭抓 13:45 收盤價、09:00 那一分鐘的價格；
期交所即時行情的分 K 只保留當盤，晚到隔天開盤後就拿不到 09:00，改用 FinMind 的 08:45 開盤價並註明。
"""
import csv
import json
import os
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta

import requests

import paper_trader as pt
from taifex_quote import MIS_CHART_URL, TAIPEI

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "on_state.json")
LEDGER_FILE = os.path.join(HERE, "on_trades.csv")
LAST_ENTRY = date(2026, 12, 15)  # 12/15 買、12/16 09:00 賣，12/16 是 12 月合約結算
SETTLES = [date(2026, 10, 21), date(2026, 11, 18), date(2026, 12, 16)]
HOLIDAYS = {date(2026, 12, 25), date(2027, 1, 1)}  # 台股休市（週末另外算）
DAY_CLOSE, SELL_AT = dtime(13, 45), dtime(9, 0)
COST_NTD, PV = 78, 10
ATR_LIMIT = 1000
PERIOD_LIMIT = -15000
MONTHS = "ABCDEFGHIJKL"
TAG = "**【隔夜多影子】**"
FIELDS = ["time", "date", "action", "contract", "price", "price_0845", "reason", "atr14", "ma5", "pnl_pts", "pnl_ntd"]


def fmt(x):
    return f"{x:,.0f}"


def is_trading(d):
    return d.weekday() < 5 and d not in HOLIDAYS


def next_trading(d):
    d += timedelta(days=1)
    while not is_trading(d):
        d += timedelta(days=1)
    return d


def period_of(d):
    """d 屬於哪一期（以結算日切，結算日當天收盤後算下一期）"""
    for s in SETTLES:
        if d < s:
            return s.isoformat()
    return SETTLES[-1].isoformat()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.write("\n")


def ledger(row):
    new = not os.path.exists(LEDGER_FILE)
    with open(LEDGER_FILE, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in FIELDS})


# ---------- 資料 ----------
def fm_rows(start):
    """FinMind 免費額度每小時 600 次、跟其他排程共用，用完會回 402：當作沒資料，下一輪再試"""
    try:
        j = requests.get(pt.FINMIND_URL, params={"dataset": "TaiwanFuturesDaily", "data_id": "TMF", "start_date": start},
                         headers={"Authorization": f"Bearer {os.environ['FINMIND_TOKEN']}"}, timeout=60).json()
    except Exception as e:  # noqa: BLE001
        print("FinMind 失敗", e)
        return []
    if "data" not in j:
        print("FinMind 沒給資料", j.get("msg"))
        return []
    return j["data"]


def near_code(d):
    """d 這天 13:45 能買的近月合約代碼，例：2026-11-04 → TMFK6、2026-11-18（結算日）→ TMFL6"""
    s = next(x for x in SETTLES + [date(2027, 1, 20)] if d < x)  # 結算日 13:30 舊約就到期，13:45 要買新的近月
    return "TMF" + MONTHS[s.month - 1] + str(s.year % 10), f"{s.year}{s.month:02d}"


def day_close(d, code, cm):
    """d 那天日盤的開高低收：當天先用期交所即時行情（13:45 後），拿不到再用 FinMind"""
    q = pt.fetch_contract("0", code)
    if q and q["ts"].date() == d and q["ts"].time() >= dtime(13, 44):
        return {"o": q["open"], "h": q["high"], "l": q["low"], "c": q["price"]}
    for r in fm_rows(d.isoformat()):
        if r["date"] == d.isoformat() and r["contract_date"] == cm and r["trading_session"] == "position" and r["close"]:
            return {"o": r["open"], "h": r["max"], "l": r["min"], "c": r["close"]}
    return None


def history(d, cm, today):
    """同一個合約、d 以前的日 K（日盤＋前一晚夜盤合併一根），最後一根換成今天的日盤＋昨晚夜盤"""
    g = defaultdict(dict)
    for r in fm_rows((d - timedelta(days=40)).isoformat()):
        if r["contract_date"] == cm and r["close"]:
            g[r["date"]][r["trading_session"]] = r  # FinMind 夜盤標在結束那天
    bars = []
    for k in sorted(g):
        if k > d.isoformat() or "position" not in g[k] and k != d.isoformat():
            continue
        day = today if k == d.isoformat() else {"o": g[k]["position"]["open"], "h": g[k]["position"]["max"],
                                                  "l": g[k]["position"]["min"], "c": g[k]["position"]["close"]}
        n = g[k].get("after_market")
        bars.append({"d": k, "h": max(day["h"], n["max"]) if n else day["h"], "l": min(day["l"], n["min"]) if n else day["l"], "c": day["c"]})
    if not bars or bars[-1]["d"] != d.isoformat():
        bars.append({"d": d.isoformat(), "h": today["h"], "l": today["l"], "c": today["c"]})
    return bars


def sell_prices(d, code, cm):
    """d 那天 09:00 那一分鐘收盤價、08:45 開盤價；09:00 拿不到時回 (None, 開盤價)"""
    try:
        data = requests.post(MIS_CHART_URL, json={"SymbolID": code + "-F"}, timeout=15).json()["RtData"]
        if data["Quote"]["CDate"] == d.strftime("%Y%m%d"):
            t9 = next((t for t in data["Ticks"] if t[0] == "090000"), None)  # 分 K 標的是結束時間：08:59～09:00 那根
            op = float(data["Quote"]["COpenPrice"]) if data["Quote"]["COpenPrice"] else None
            if t9:
                return float(t9[4]), op
            if op and datetime.now(TAIPEI).time() < dtime(9, 1):
                return None, None  # 還沒到 09:00，下次再看
    except Exception as e:  # noqa: BLE001
        print("MIS 分 K 失敗", e)
    for r in fm_rows(d.isoformat()):
        if r["date"] == d.isoformat() and r["contract_date"] == cm and r["trading_session"] == "position" and r["open"]:
            return None, r["open"]
    return None, None


# ---------- 決定與出場 ----------
def decide(st, d, now):
    code, cm = near_code(d)
    today = day_close(d, code, cm)
    if not today:
        return False  # 還沒有收盤價，下次再試
    bars = history(d, cm, today)
    if len(bars) < 15:
        print("歷史日 K 不夠（FinMind 可能沒回），下一輪再決定", len(bars))
        return False
    trs = [max(b["h"], a["c"]) - min(b["l"], a["c"]) for a, b in zip(bars, bars[1:])]
    atr = sum(trs[-14:]) / len(trs[-14:]) if trs else None
    ma5 = sum(b["c"] for b in bars[-5:]) / len(bars[-5:])
    nd = next_trading(d)
    per = period_of(d)
    used = st["period_pnl"].get(per, 0)
    why = None
    if d.weekday() == 3:
        why = "週四不做"
    elif (nd - d).days > 4:
        why = f"下一個交易日 {nd.month}/{nd.day} 離今天超過 4 天（連假前）"
    elif atr is not None and atr * 0.75 > ATR_LIMIT:
        why = f"波動太大：ATR14 {fmt(atr)} × 0.75 ＝ {fmt(atr * 0.75)} > 1,000 點"
    elif d.weekday() == 4 and today["c"] <= ma5:
        why = f"週五沒站上 5 日線（收 {fmt(today['c'])}，5 日線 {fmt(ma5)}）"
    elif used <= PERIOD_LIMIT:
        why = f"這一期影子已虧 {fmt(used)} 元，到上限 15,000，停到下一期"
    base = {"time": now.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(), "contract": code, "price": f"{today['c']:.0f}",
            "atr14": f"{atr:.0f}" if atr else "", "ma5": f"{ma5:.0f}"}
    info = f"ATR14 {fmt(atr)}（×0.75＝{fmt(atr * 0.75)}）" if atr else "ATR14 算不出來"
    if why:
        ledger({**base, "action": "SKIP", "reason": why})
        pt.notify(f"{TAG} {d.month}/{d.day}（{'一二三四五'[d.weekday()]}）不買：{why}\n收盤 {fmt(today['c'])}｜{info}")
    else:
        st["pos"] = {"date": d.isoformat(), "sell_day": nd.isoformat(), "code": code, "cm": cm, "entry": today["c"]}
        ledger({**base, "action": "BUY", "reason": "13:45 收盤價虛擬買進 1 口"})
        pt.notify(f"{TAG} {d.month}/{d.day}（{'一二三四五'[d.weekday()]}）虛擬買進 {code} 1 口 @{fmt(today['c'])}（日盤收盤）\n"
                  f"{info}｜不掛停損｜{nd.month}/{nd.day} 09:00 賣｜這一期影子累計 {used:+,.0f} 元")
    st["decided"].append(d.isoformat())
    return True


def try_sell(st, now):
    p = st["pos"]
    d = date.fromisoformat(p["sell_day"])
    if now < datetime.combine(d, SELL_AT, TAIPEI):
        return
    px9, op = sell_prices(d, p["code"], p["cm"])
    if px9 is None and op is None:
        return
    px = px9 if px9 is not None else op
    pts = px - p["entry"]
    ntd = pts * PV - COST_NTD
    per = period_of(date.fromisoformat(p["date"]))
    st["period_pnl"][per] = st["period_pnl"].get(per, 0) + ntd
    st["trades"].append({"buy": p["date"], "sell": p["sell_day"], "entry": p["entry"], "exit": px, "ntd": ntd,
                         "open_ntd": (op - p["entry"]) * PV - COST_NTD if op else None})
    note = "" if px9 is not None else "（09:00 那一分鐘沒抓到，用 08:45 開盤價）"
    ledger({"time": now.strftime("%Y-%m-%d %H:%M"), "date": p["sell_day"], "action": "SELL", "contract": p["code"],
            "price": f"{px:.0f}", "price_0845": f"{op:.0f}" if op else "", "reason": "09:00 虛擬賣出" + note,
            "pnl_pts": f"{pts:+.0f}", "pnl_ntd": f"{ntd:+.0f}"})
    tot = sum(t["ntd"] for t in st["trades"])
    alt = f"｜08:45 開盤就賣會是 {(op - p['entry']) * PV - COST_NTD:+,.0f} 元" if op and px9 is not None else ""
    pt.notify(f"{TAG} {d.month}/{d.day} 09:00 虛擬賣出 @{fmt(px)}{note}\n"
              f"這筆 {pts:+,.0f} 點＝{ntd:+,.0f} 元（扣 78 元）{alt}\n影子累計 {len(st['trades'])} 筆 {tot:+,.0f} 元")
    st["pos"] = None


def summary(st, title):
    t = st["trades"]
    if not t:
        return f"{TAG} {title}：還沒有成交"
    eq, peak, dd = 0, 0, 0
    for x in t:
        eq += x["ntd"]; peak = max(peak, eq); dd = min(dd, eq - peak)
    win = sum(1 for x in t if x["ntd"] > 0)
    op = [x["open_ntd"] for x in t if x["open_ntd"] is not None]
    return (f"{TAG} {title}\n{len(t)} 筆、賺 {win} 筆（{win / len(t):.0%}）、累計 {eq:+,.0f} 元、最大回落 {dd:,.0f} 元、"
            f"最好 {max(x['ntd'] for x in t):+,.0f}／最差 {min(x['ntd'] for x in t):+,.0f}\n"
            + (f"如果都在 08:45 開盤賣：{sum(op):+,.0f} 元（{len(op)} 筆）\n" if op else "")
            + f"不買的日子 {st['skips']} 天。回測（2020～2025/10，同規則、45,000 點換算）每年平均約 +5 萬、最大回落約 −6.7 萬；"
            "幾週的樣本只能看實際跟回測有沒有落差，不能當結論。")


def main(now=None):
    now = now or datetime.now(TAIPEI)
    st = load_state()
    if st is None:
        st = {"started": now.isoformat(), "decided": [], "pos": None, "trades": [], "period_pnl": {}, "skips": 0,
              "weekly": [], "final_sent": False}
        pt.notify(f"{TAG} 開跑：每個交易日 13:45 照 KK 的隔夜多規則決定買不買，隔天 09:00 賣；跑到 12/16（12 月合約結算）")
    if st["final_sent"]:
        save_state(st)
        return
    if st["pos"]:
        try_sell(st, now)
    # 決定：今天（13:45 後）或之前漏掉的交易日（排程大延遲時補）。持倉中不重複進場（隔天賣掉才會輪到下一個）
    d = (now - timedelta(hours=13, minutes=45)).date()  # 13:45 以後才算「今天可以決定」
    start = datetime.fromisoformat(st["started"]).date()
    if not st["pos"] and is_trading(d) and d >= start and d <= LAST_ENTRY and d.isoformat() not in st["decided"]:
        before = len(st["decided"])
        if decide(st, d, now) and not st["pos"] and len(st["decided"]) > before:
            st["skips"] += 1
    # 週報：週五的決定做完後發一次
    wk = d.isocalendar()[:2]
    if d.weekday() == 4 and d.isoformat() in st["decided"] and list(wk) not in st["weekly"]:
        pt.notify(summary(st, f"週報（到 {d.month}/{d.day}）"))
        st["weekly"].append(list(wk))
    # 結算報告：最後一筆賣掉之後
    if not st["pos"] and d >= LAST_ENTRY and now.date() > LAST_ENTRY and not st["final_sent"]:
        pt.notify(summary(st, "結算報告（跑到 12/16）"))
        ledger({"time": now.strftime("%Y-%m-%d %H:%M"), "action": "REPORT",
                "pnl_ntd": f"{sum(t['ntd'] for t in st['trades']):+.0f}", "reason": f"{len(st['trades'])} 筆"})
        st["final_sent"] = True
    save_state(st)


if __name__ == "__main__":
    main()
