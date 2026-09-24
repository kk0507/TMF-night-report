# python grid_trader.py -> 網格(蛛網)模擬交易(影子模式)，每筆買賣記到 grid_trades.csv
# 需要環境變數：FINMIND_TOKEN, DISCORD_WEBHOOK_URL
"""微台(TMF)只做多網格模擬，期間 2026-09-25 ~ 10月合約結算(10/21)，跟 paper_trader 分開帳戶。

規則(開跑前定案，期間不改)：
- 啟動：日盤收盤後，上升趨勢總開關(收>MA60且MA20>MA60)通過 且 20日趨勢效率<=0.30 才開網。
- 排網：基準=當天收盤；格距S=round(近20日平均真實區間ATR20×1/4, 10點)，上限260點。
  買單：基準-S、基準-2S 各1口；每口買到後在「買價+S」賣出，賣掉後該格重新掛買單。
- 整體停損：價格碰到 基準-3S，所有持倉平倉、收網，冷卻3個交易日。
  (兩格都成交再停損最壞=3S點=最多780點≈7,800元+手續費，守住8,000元)
- 每天日盤收盤：手上沒持倉就用新收盤價重排(條件不成立就收網)；有持倉就維持原網格。
- 10/14起不開新網；10/20日盤收盤強制平倉；本金160,000；手續費每口每邊20元＋期交稅十萬分之2。

成交模擬(排程約10~20分鐘才跑一次，看得到的是各session累計最高/最低)：
- 只算「網格啟動之後」新創的高低點；新session開盤跳空越過價位，用開盤價成交。
- 同一次檢查同時碰到停損跟其他價位：先成交途中經過的買單、再停損(保守)。
- 這次檢查才買到的那口，不會在同一次檢查就賣掉(看不出先後，保守少算)。
"""
import csv
import json
import os
from datetime import date, datetime, time as dtime, timedelta

import paper_trader as pt

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "grid_state.json")
LEDGER_FILE = os.path.join(HERE, "grid_trades.csv")
LEVELS, MAX_SPACING, COOLDOWN = 2, 260, 3
FIELDS = ["time", "action", "grid_id", "level", "price", "lots", "reason", "pnl_points", "pnl_ntd", "fees", "equity"]


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"equity": pt.CAPITAL, "grid": None, "next_grid": 1, "last_eval": None, "cooldown": 0,
                "seen": {}, "stats": {"round_trips": 0, "stops": 0}}


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


def notify(text):
    pt.notify(text)


def stamp(now):
    return now.strftime("%Y-%m-%d %H:%M")


def buy(st, lv, price, now):
    g = st["grid"]
    fee = pt.fees(price, 1)
    st["equity"] -= fee
    lv["filled"] = {"price": price, "fee": fee, "time": now.isoformat()}
    ledger({"time": stamp(now), "action": "BUY", "grid_id": g["id"], "level": f"{lv['level']:.0f}",
            "price": f"{price:.0f}", "lots": 1, "reason": f"網格買單，目標{lv['level'] + g['S']:.0f}",
            "fees": f"{fee:.0f}", "equity": f"{st['equity']:.0f}"})
    return f"買 1口 @{price:,.0f}"


def sell(st, lv, price, now, reason):
    g = st["grid"]
    f = lv["filled"]
    fee = pt.fees(price, 1)
    pts = price - f["price"]
    net = pts * pt.POINT_VALUE - fee - f["fee"]
    st["equity"] += pts * pt.POINT_VALUE - fee
    ledger({"time": stamp(now), "action": "SELL", "grid_id": g["id"], "level": f"{lv['level']:.0f}",
            "price": f"{price:.0f}", "lots": 1, "reason": reason, "pnl_points": f"{pts:+.0f}",
            "pnl_ntd": f"{net:+.0f}", "fees": f"{f['fee'] + fee:.0f}", "equity": f"{st['equity']:.0f}"})
    lv["filled"] = None
    return f"賣 1口 @{price:,.0f}（{pts:+,.0f}點，{net:+,.0f}元）{reason}"


def close_grid(st, now, reason, price=None):
    g = st["grid"]
    msgs = []
    for lv in g["levels"]:
        if lv["filled"]:
            msgs.append(sell(st, lv, price, now, reason))
    ledger({"time": stamp(now), "action": "GRID_OFF", "grid_id": g["id"], "reason": reason,
            "equity": f"{st['equity']:.0f}"})
    st["grid"] = None
    return msgs


def new_extremes(st, now):
    """回傳(新低, 新高, 新低所屬session開盤, 新高所屬session開盤, 目前價)。只算網格啟動後。"""
    g = st["grid"]
    armed = datetime.fromisoformat(g["armed_at"])
    lo = hi = lo_open = hi_open = last = None
    for mt in ("0", "1"):
        q = pt.fetch_contract(mt, g["contract"])
        if not q or q["high"] is None:
            continue
        ss = pt.session_start(q)
        key = ss.isoformat()
        if ss < armed and key not in st["seen"]:
            continue
        seen = st["seen"].get(key)
        if seen is None:
            nl, nh, op = q["low"], q["high"], q["open"]
        else:
            nl = q["low"] if q["low"] < seen["lo"] else None
            nh = q["high"] if q["high"] > seen["hi"] else None
            op = None
        st["seen"][key] = {"lo": q["low"], "hi": q["high"]}
        if nl is not None and (lo is None or nl < lo):
            lo, lo_open = nl, op
        if nh is not None and (hi is None or nh > hi):
            hi, hi_open = nh, op
        if last is None or q["ts"] > last[1]:
            last = (q["price"], q["ts"])
    cutoff = (now - timedelta(days=3)).isoformat()
    st["seen"] = {k: v for k, v in st["seen"].items() if k >= cutoff}
    return lo, hi, lo_open, hi_open


def check_fills(st, now):
    g = st["grid"]
    lo, hi, lo_open, hi_open = new_extremes(st, now)
    msgs = []
    just_bought = set()
    if lo is not None:
        for lv in g["levels"]:
            if not lv["filled"] and lo <= lv["level"] and lv["level"] > g["stop"]:
                px = lo_open if lo_open is not None and lo_open <= lv["level"] else lv["level"]
                msgs.append(buy(st, lv, px, now))
                just_bought.add(lv["level"])
        if lo <= g["stop"]:
            px = lo_open if lo_open is not None and lo_open <= g["stop"] else g["stop"]
            msgs += close_grid(st, now, "跌破網格下緣，整體停損", px)
            st["cooldown"] = COOLDOWN
            st["stats"]["stops"] += 1
            return msgs, "stop"
    if hi is not None:
        for lv in g["levels"]:
            tgt = lv["level"] + g["S"]
            if lv["filled"] and lv["level"] not in just_bought and hi >= tgt:
                px = hi_open if hi_open is not None and hi_open >= tgt else tgt
                msgs.append(sell(st, lv, px, now, "網格停利"))
                st["stats"]["round_trips"] += 1
    return msgs, None


def atr20(bars):
    trs = []
    for i in range(len(bars) - 20, len(bars)):
        b, pc = bars[i], bars[i - 1]["close"]
        trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    return sum(trs) / 20


def day_eval(st, now):
    tb = pt.today_bar(now)
    if not tb:
        return
    today = now.date()
    bars = [b for b in pt.history_bars() if b["date"] < tb["date"]] + [tb]
    c = [b["close"] for b in bars]
    i = len(c) - 1
    ma20, ma60 = sum(c[i - 19:i + 1]) / 20, sum(c[i - 59:i + 1]) / 60
    regime = c[i] > ma60 and ma20 > ma60
    path = sum(abs(c[j] - c[j - 1]) for j in range(i - 19, i + 1))
    te = abs(c[i] - c[i - 20]) / path if path else 1
    atr = atr20(bars)
    S = min(MAX_SPACING, round(atr / 4 / 10) * 10)
    lines = [f"**【網格模擬日報】{tb['date']}**",
             f"收{c[i]:,.0f}｜MA20 {ma20:,.0f}｜MA60 {ma60:,.0f}｜總開關{'✅' if regime else '❌'}｜趨勢效率 {te:.2f}｜ATR20 {atr:,.0f}"]
    g = st["grid"]
    if st["cooldown"] > 0:
        st["cooldown"] -= 1
    if g and today >= pt.FORCE_CLOSE_DATE:
        lines += close_grid(st, now, "結算前強制平倉", tb["close"])
        g = None
    held = g and any(lv["filled"] for lv in g["levels"])
    if held:
        lots = [lv for lv in g["levels"] if lv["filled"]]
        flo = sum((tb["close"] - lv["filled"]["price"]) * pt.POINT_VALUE for lv in lots)
        lines.append(f"網格#{g['id']}維持(有持倉{len(lots)}口，收盤浮動{flo:+,.0f}元)｜"
                     "買單" + "/".join(format(lv["level"], ",.0f") for lv in g["levels"]) + f"｜停損{g['stop']:,.0f}")
    else:
        if g:
            ledger({"time": stamp(now), "action": "GRID_OFF", "grid_id": g["id"], "reason": "收盤無持倉，重新評估",
                    "equity": f"{st['equity']:.0f}"})
            st["grid"] = None
        why = None
        if today > pt.LAST_ENTRY_SIGNAL:
            why = "10/14起不開新網"
        elif st["cooldown"] > 0:
            why = f"停損後冷卻中(剩{st['cooldown']}天)"
        elif not regime:
            why = "總開關未通過"
        elif te > 0.30:
            why = f"趨勢效率{te:.2f}>0.30，不像盤整"
        if why:
            lines.append(f"不開網：{why}")
        else:
            gid = st["next_grid"]
            st["next_grid"] += 1
            anchor = tb["close"]
            st["grid"] = {"id": gid, "contract": tb["contract"], "anchor": anchor, "S": S,
                          "stop": anchor - (LEVELS + 1) * S, "armed_at": now.isoformat(),
                          "levels": [{"level": anchor - k * S, "filled": None} for k in range(1, LEVELS + 1)]}
            g = st["grid"]
            for mt in ("0", "1"):
                q = pt.fetch_contract(mt, g["contract"])
                if q and q["high"] is not None:
                    st["seen"][pt.session_start(q).isoformat()] = {"lo": q["low"], "hi": q["high"]}
            ledger({"time": stamp(now), "action": "GRID_ON", "grid_id": gid, "level": f"{anchor:.0f}",
                    "reason": f"格距{S}點，買" + "/".join(format(lv["level"], ".0f") for lv in g["levels"]) + f"，停損{g['stop']:.0f}",
                    "equity": f"{st['equity']:.0f}"})
            lines.append(f"開網#{gid}：基準{anchor:,.0f}，格距{S}點｜買 "
                         + "、".join(f"{lv['level']:,.0f}(賣{lv['level'] + S:,.0f})" for lv in g["levels"])
                         + f"｜整體停損{g['stop']:,.0f}（最壞約{3 * S * pt.POINT_VALUE:,.0f}元）")
    s = st["stats"]
    lines.append(f"累計完成來回{s['round_trips']}次、停損{s['stops']}次｜網格權益 {st['equity']:,.0f}")
    notify("\n".join(lines))
    ledger({"time": stamp(now), "action": "EVAL", "price": f"{tb['close']:.0f}", "reason": lines[-2],
            "equity": f"{st['equity']:.0f}"})
    st["last_eval"] = today.isoformat()


def main(now=None):
    now = now or datetime.now(pt.TAIPEI)
    if now.date() < pt.START_DATE or now.date() > pt.END_DATE:
        print("不在模擬期間")
        return
    st = load_state()
    if st["grid"]:
        gid = st["grid"]["id"]
        msgs, _ = check_fills(st, now)
        if msgs:
            notify(f"**【網格模擬】#{gid}**\n" + "\n".join(msgs) + f"\n網格權益 {st['equity']:,.0f}")
    if now.weekday() < 5 and now.time() >= pt.DAY_CLOSE and st["last_eval"] != now.date().isoformat():
        day_eval(st, now)
    save_state(st)


if __name__ == "__main__":
    main()
