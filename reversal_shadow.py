# python reversal_shadow.py -> 反轉規則影子模式：A「收盤 2%」vs B「盤中 1.5%」，各 1 口虛擬單，每筆記到 rev_trades.csv
# 需要環境變數：FINMIND_TOKEN, DISCORD_WEBHOOK_URL
"""目的：驗證「盤中碰到就算（B）」在真實盤中價格下，是不是真的比「等收盤（A）」早進場、多賺。只做多。
2026-09-25 起跑、沒有結束日（KK 說停才停）；10/21 十月合約結算時出第一次報告。

規則（開跑前定案，期間不改）：
A 收盤 2%：規則同 reversal_watch.py——日盤收盤比「最近最低收盤」漲回 2% ＝開始往上 → 當晚夜盤開盤價虛擬買進；
          收盤比「進場後最高收盤」跌 2% ＝漲完了 → 當晚夜盤開盤價賣出。（回測假設用收盤價，這裡用實際買得到的價格）
B 盤中 1.5%：價格碰到「最低點×1.015」就在那個價位買進；碰到「進場後最高點×0.985」就在那個價位賣出；
          新盤開盤就跳空越過，用開盤價成交。
- 兩個都從空手開始，只算開跑後出現的新訊號；各 1 口，本金各 160,000；手續費每口每邊 20 元＋期交稅十萬分之 2。
- 結算前一天日盤收盤強制平倉（10 月合約＝10/20），換到下個月合約後 A 照常、B 從頭重新計算。

成交模擬（排程約 10~20 分鐘才跑一次，看得到的是各盤累計最高/最低）：
- B 只算開跑之後新創的高低點＋每次檢查時的成交價；同一次檢查最多換手一次；先用舊的最高/最低點判斷有沒有碰到線，
  沒碰到才更新最高/最低點（跟回測一樣）。兩次檢查之間碰到線又彈回、而且沒創整盤新高/新低的，會漏掉（取樣限制）。
"""
import csv
import json
import math
import os
from datetime import date, datetime, time as dtime, timedelta

import paper_trader as pt
from reversal_watch import run_state

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "rev_state.json")
LEDGER_FILE = os.path.join(HERE, "rev_trades.csv")
START_DATE = date(2026, 9, 25)
TH_A, TH_B = 0.02, 0.015
MONTHS = "ABCDEFGHIJKL"
FIELDS = ["time", "acct", "action", "contract", "price", "ref_price", "lots", "reason", "pnl_points", "pnl_ntd", "fees", "equity"]
NAMES = {"A": "A｜收盤2%", "B": "B｜盤中1.5%"}


def mis_code(cm):
    """202610 → TMFJ6"""
    return "TMF" + MONTHS[int(cm[4:]) - 1] + cm[3]


def roll_date(code):
    """合約結算日＝該月第三個星期三；結算前一天收盤平倉。code 例：TMFJ6。"""
    m = MONTHS.index(code[3]) + 1
    now_y = datetime.now(pt.TAIPEI).year
    y = now_y - now_y % 10 + int(code[4])
    first = date(y, m, 1)
    settle = first + timedelta(days=(2 - first.weekday()) % 7 + 14)
    return settle - timedelta(days=1)


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


def stamp(now):
    return now.strftime("%Y-%m-%d %H:%M")


def fmt(x):
    return f"{x:,.0f}"


# ---------- 買賣 ----------
def buy(st, acct, price, ref, now, reason):
    a = st[acct]
    fee = pt.fees(price, 1)
    a["equity"] -= fee
    a["pos"] = {"price": price, "fee": fee, "time": now.isoformat()}
    ledger({"time": stamp(now), "acct": acct, "action": "BUY", "contract": st["contract"], "price": f"{price:.0f}",
            "ref_price": f"{ref:.0f}", "lots": 1, "reason": reason, "fees": f"{fee:.0f}", "equity": f"{a['equity']:.0f}"})
    return f"虛擬買進 1 口 @{fmt(price)}（{reason}）"


def sell(st, acct, price, ref, now, reason):
    a = st[acct]
    p = a["pos"]
    fee = pt.fees(price, 1)
    pts = price - p["price"]
    net = pts * pt.POINT_VALUE - fee - p["fee"]
    a["equity"] += pts * pt.POINT_VALUE - fee
    a["trades"].append({"pts": pts, "net": net})
    ledger({"time": stamp(now), "acct": acct, "action": "SELL", "contract": st["contract"], "price": f"{price:.0f}",
            "ref_price": f"{ref:.0f}", "lots": 1, "reason": reason, "pnl_points": f"{pts:+.0f}", "pnl_ntd": f"{net:+.0f}",
            "fees": f"{fee + p['fee']:.0f}", "equity": f"{a['equity']:.0f}"})
    a["pos"] = None
    return f"虛擬賣出 1 口 @{fmt(price)}，這筆 {pts:+,.0f} 點＝{net:+,.0f} 元（{reason}）"


def notify(acct, lines, st):
    a = st[acct]
    tr = a["trades"]
    tail = f"帳戶 {fmt(a['equity'])}（已完成 {len(tr)} 筆，賺 {sum(1 for t in tr if t['net'] > 0)} 筆）"
    pt.notify(f"**【反轉影子 {NAMES[acct]}】**\n" + "\n".join(lines) + "\n" + tail)


# ---------- B：盤中觸價 ----------
def b_step(b, lo, hi, op):
    """回傳成交 (side, 成交價, 線) 或 None；會更新 b 的 dir/ext。同一次最多換手一次。"""
    if b["dir"] == 1:
        lvl = b["ext"] * (1 - TH_B)
        if lo is not None and lo <= lvl:
            px = op if op is not None and op < lvl else math.floor(lvl)  # 微台跳動 1 點，賣在線上取整數往下（保守）
            b["dir"], b["ext"] = -1, px
            return ("SELL", px, lvl)
        if hi is not None and hi > b["ext"]:
            b["ext"] = hi
    else:
        lvl = b["ext"] * (1 + TH_B)
        if hi is not None and hi >= lvl:
            px = op if op is not None and op > lvl else math.ceil(lvl)  # 買在線上取整數往上（保守）
            b["dir"], b["ext"] = 1, px
            return ("BUY", px, lvl)
        if lo is not None and lo < b["ext"]:
            b["ext"] = lo
    return None


def b_init(bars):
    """用日K把 B 的狀態（現在是往上還是往下、最高/最低點）算到最新。"""
    b = {"dir": 0}
    hi, lo = bars[0]["high"], bars[0]["low"]
    for x in bars[1:]:
        if b["dir"] == 0:
            if x["high"] >= lo * (1 + TH_B):
                b = {"dir": 1, "ext": max(x["open"], lo * (1 + TH_B))}
            elif x["low"] <= hi * (1 - TH_B):
                b = {"dir": -1, "ext": min(x["open"], hi * (1 - TH_B))}
            else:
                hi, lo = max(hi, x["high"]), min(lo, x["low"])
            continue
        b_step(b, x["low"], x["high"], x["open"])
    return b


def new_extremes(st):
    """上次檢查之後看到的最低/最高價：這一盤新創的高低點，加上目前成交價（行情網只給整盤累計高低，
    同一盤裡「先衝高再跌回來、但沒破整盤低點」只能靠目前成交價看到）。只算這次有新成交的盤；
    新盤第一次看到時附上開盤價供跳空判斷。"""
    lo = hi = lo_open = hi_open = None
    for mt in ("0", "1"):
        q = pt.fetch_contract(mt, st["contract"])
        if not q or q["high"] is None:
            continue
        key = pt.session_start(q).isoformat()
        ts = q["ts"].isoformat()
        seen = st["seen"].get(key)
        if seen is None:
            nl, nh, op = min(q["low"], q["price"]), max(q["high"], q["price"]), q["open"]
        elif ts <= seen.get("ts", ""):
            continue  # 這一盤沒有新成交（例如收盤後、連假中）
        else:
            nl = min(q["price"], q["low"]) if q["low"] < seen["lo"] else q["price"]
            nh = max(q["price"], q["high"]) if q["high"] > seen["hi"] else q["price"]
            op = None
        st["seen"][key] = {"lo": q["low"], "hi": q["high"], "ts": ts}
        if nl is not None and (lo is None or nl < lo):
            lo, lo_open = nl, op
        if nh is not None and (hi is None or nh > hi):
            hi, hi_open = nh, op
    cutoff = (datetime.now(pt.TAIPEI) - timedelta(days=12)).isoformat()  # 連假時行情網會一直顯示假期前最後一盤，不能太早清掉
    st["seen"] = {k: v for k, v in st["seen"].items() if k >= cutoff}
    return lo, hi, lo_open, hi_open


def check_b(st, now):
    b = st["B"]
    if b.get("paused"):
        return
    lo, hi, lo_open, hi_open = new_extremes(st)
    if lo is None and hi is None:
        return
    # 往上狀態先看有沒有跌破出場線；往下狀態先看有沒有漲過進場線（用各自那一邊的開盤價判斷跳空）
    op = lo_open if b["dir"] == 1 else hi_open
    old_ext = b["ext"]
    fill = b_step(b, lo, hi, op)
    if not fill:
        return
    side, px, lvl = fill
    if side == "BUY" and now.date() < roll_date(st["contract"]):
        msg = buy(st, "B", px, lvl, now, f"盤中從低點 {fmt(old_ext)} 漲回 1.5%")
        notify("B", [msg, f"出場線：跌破 {fmt(px * (1 - TH_B))} 就賣（價格創新高，這條線會跟著往上）"], st)
    elif side == "SELL" and st["B"]["pos"]:
        msg = sell(st, "B", px, lvl, now, f"從進場後最高點 {fmt(old_ext)} 跌了 1.5%")
        notify("B", [msg], st)


# ---------- A：收盤判斷，夜盤開盤成交 ----------
def fill_a_pending(st, now):
    p = st["A"].get("pending")
    if not p:
        return
    # 訊號之後第一個開的盤（通常是當晚夜盤；遇到連假可能是之後的日盤），用它的開盤價
    created = datetime.fromisoformat(p["created"])
    qs = [q for q in (pt.fetch_contract(mt, st["contract"]) for mt in ("1", "0"))
          if q and q["open"] and pt.session_start(q) > created]
    if not qs:
        return
    q = min(qs, key=pt.session_start)
    px = q["open"]
    diff = px - p["signal_close"]
    sess = "夜盤" if q["mt"] == "1" else "日盤"
    note = (f"{sess}開盤成交，比訊號收盤 {fmt(p['signal_close'])} {'貴' if diff > 0 else '便宜'} {abs(diff):,.0f} 點"
            if diff else f"{sess}開盤成交，跟訊號收盤同價")
    if p["side"] == "BUY" and not st["A"]["pos"]:
        msg = buy(st, "A", px, p["signal_close"], now, note)
        notify("A", [msg, f"出場規則：之後收盤比進場後最高收盤跌 2% 就賣"], st)
    elif p["side"] == "SELL" and st["A"]["pos"]:
        msg = sell(st, "A", px, p["signal_close"], now, note)
        notify("A", [msg], st)
    st["A"]["pending"] = None


def day_eval(st, now):
    tb = pt.today_bar(now)
    if not tb:
        return
    today = now.date()
    st["last_eval"] = today.isoformat()
    code = st["contract"]
    if today >= roll_date(code):
        # 結算前一天：兩個帳戶都平倉（用自己這個合約的日盤收盤，近月可能已經換成下個月），B 暫停到換月
        q = pt.fetch_contract("0", code)
        close = q["price"] if q and q["ts"].date() == today and q["ts"].time() >= dtime(13, 40) else tb["close"]
        for acct in ("A", "B"):
            if st[acct]["pos"]:
                notify(acct, [sell(st, acct, close, close, now, "結算前一天收盤換月平倉")], st)
            st[acct]["pending"] = None
        st["B"]["paused"] = True
        if tb["contract"] != code:
            # 已經是新合約：換過去，B 從頭算
            st["contract"] = tb["contract"]
            st["B"] = {"dir": 0, "paused": False, "pos": None, "equity": st["B"]["equity"], "trades": st["B"]["trades"],
                       "restart": {"hi": tb["high"], "lo": tb["low"]}}
            st["seen"] = {}
            ledger({"time": stamp(now), "acct": "-", "action": "ROLL", "contract": st["contract"], "reason": f"換到 {st['contract']}"})
        return
    hist = pt.history_bars()
    if mis_code(hist[-1]["c"]) != tb["contract"]:
        return  # 換月交界，歷史跟今天的合約對不上，隔天再算
    bars = [b for b in hist if b["date"] < tb["date"]] + [tb]
    rule = run_state([b["close"] for b in bars])
    last = rule["flips"][-1] if rule["flips"] else None
    if not last or last["i"] != len(bars) - 1:
        return
    a = st["A"]
    if last["dir"] == 1 and not a["pos"] and today < roll_date(code) - timedelta(days=1):
        a["pending"] = {"side": "BUY", "signal_close": tb["close"], "created": now.isoformat()}
        low = bars[last["from_i"]]["close"]
        notify("A", [f"今天收 {fmt(tb['close'])}，比低點收盤 {fmt(low)} 漲回 2%＝開始往上 → 今晚夜盤開盤虛擬買進 1 口"], st)
        ledger({"time": stamp(now), "acct": "A", "action": "SIGNAL", "contract": code, "ref_price": f"{tb['close']:.0f}", "reason": "收盤漲回2%"})
    elif last["dir"] == -1 and a["pos"]:
        a["pending"] = {"side": "SELL", "signal_close": tb["close"], "created": now.isoformat()}
        high = bars[last["from_i"]]["close"]
        notify("A", [f"今天收 {fmt(tb['close'])}，比進場後最高收盤 {fmt(high)} 跌了 2%＝漲完了 → 今晚夜盤開盤虛擬賣出"], st)
        ledger({"time": stamp(now), "acct": "A", "action": "SIGNAL", "contract": code, "ref_price": f"{tb['close']:.0f}", "reason": "收盤跌2%"})


def restart_b(st):
    """換月後 B 從頭算：先記住新合約的最高/最低，漲回 1.5% 或跌 1.5% 才決定方向。"""
    r = st["B"].get("restart")
    lo, hi, _, _ = new_extremes(st)
    if lo is not None:
        r["lo"] = min(r["lo"], lo)
    if hi is not None:
        r["hi"] = max(r["hi"], hi)
    if r["hi"] >= r["lo"] * (1 + TH_B):
        st["B"].update({"dir": 1, "ext": r["hi"]})
    elif r["lo"] <= r["hi"] * (1 - TH_B):
        st["B"].update({"dir": -1, "ext": r["lo"]})
    else:
        return
    st["B"].pop("restart", None)


def init_state(now):
    bars = pt.history_bars()
    code = mis_code(bars[-1]["c"])
    st = {"contract": code, "seen": {}, "last_eval": None, "started": now.isoformat(),
          "A": {"equity": pt.CAPITAL, "pos": None, "pending": None, "trades": []},
          "B": {"equity": pt.CAPITAL, "pos": None, "trades": []}}
    st["B"].update(b_init(bars))
    # 歷史日K還沒包含的盤（例如最新一個夜盤）先算進 B 的狀態，再把目前所有盤記成「已看過」
    last_day_close = datetime.combine(date.fromisoformat(bars[-1]["date"]), dtime(13, 45), pt.TAIPEI)
    for mt in ("0", "1"):
        q = pt.fetch_contract(mt, code)
        if not q or q["high"] is None:
            continue
        if pt.session_start(q) > last_day_close:
            b_step(st["B"], q["low"], q["high"], None)
        st["seen"][pt.session_start(q).isoformat()] = {"lo": q["low"], "hi": q["high"], "ts": q["ts"].isoformat()}
    ledger({"time": stamp(now), "acct": "-", "action": "START", "contract": code,
            "reason": f"B 起始狀態：{'往上' if st['B']['dir'] == 1 else '往下'}，{'最高' if st['B']['dir'] == 1 else '最低'}點 {st['B']['ext']:.0f}"})
    return st


def main(now=None):
    now = now or datetime.now(pt.TAIPEI)
    if now.date() < START_DATE:
        return
    st = load_state() or init_state(now)
    fill_a_pending(st, now)
    if st["B"].get("restart"):
        restart_b(st)
    else:
        check_b(st, now)
    if now.weekday() < 5 and now.time() >= pt.DAY_CLOSE and st["last_eval"] != now.date().isoformat():
        day_eval(st, now)
    save_state(st)


if __name__ == "__main__":
    main()
