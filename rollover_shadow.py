# python rollover_shadow.py -> 期初長抱影子：每期結算當晚夜盤開盤虛擬買進 1 口、停損 -4%、抱到結算，記到 roll_trades.csv
# 需要環境變數：FINMIND_TOKEN, DISCORD_WEBHOOK_URL（影子頻道）
"""KK 2026-09-28：獨立於日常微台操作之外，每一期初固定進場 1 口多單、抱到結算、加停損。
回測（rollover_stop.py，2017~2026 共 112 期）停損 4% 是「不易被洗」的轉折點，先用影子驗證，跑到 12 月合約結算（12/16）。

規則（開跑前定案，期間不改）：
- 進場：結算日當晚夜盤開盤價，買新的近月合約 1 口（排程晚到也用那一盤的開盤價）。
- 停損：進場價 -4%。夜盤、日盤最低點都算；新的一盤開盤就低於停損 → 用開盤價成交（跳空），
  否則碰到就用停損價（取整數往下，保守）。停損後這期不再進場，等下一次結算夜盤。
- 出場：抱到結算日，用到期日日盤收盤（＝最後結算價）。停損掉的期也記結算價，用來判斷「被洗」
  （停損了，但抱到結算其實會賺）。
- 期數：9/16→10/21（開跑前已開始，用 FinMind 實際價格追溯，標「追溯」）、10/21→11/18、11/18→12/16。
- 帳本只記點數；金額同時換算小台（1 點 50 元）與微台（1 點 10 元），未扣手續費
  （小台一趟約 200 元、微台約 60 元）。
成交模擬限制同其他影子：排程約 5~20 分鐘跑一次，看得到的是各盤累計最低點＋當下成交價。
"""
import csv
import json
import math
import os
from datetime import date, datetime, time as dtime, timedelta

import requests

import paper_trader as pt

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "roll_state.json")
LEDGER_FILE = os.path.join(HERE, "roll_trades.csv")
SETTLES = [date(2026, 9, 16), date(2026, 10, 21), date(2026, 11, 18), date(2026, 12, 16)]
STOP = 0.04
MTX, TMF = 50, 10
MONTHS = "ABCDEFGHIJKL"
SETTLE_READY = dtime(13, 35)
FIELDS = ["time", "period", "action", "contract", "price", "stop", "reason", "pnl_points", "mtx_ntd", "tmf_ntd"]
TAG = "**【期初長抱影子】**"


def code_for(d):
    """到期日 → 行情網合約代碼，例：2026-10-21 → TMFJ6"""
    return "TMF" + MONTHS[d.month - 1] + str(d.year % 10)


def fmt(x):
    return f"{x:,.0f}"


def money(pts):
    return f"{pts:+,.0f} 點＝小台 {pts * MTX:+,.0f} 元／微台 {pts * TMF:+,.0f} 元"


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


def finmind_rows(cm, start):
    return [r for r in requests.get(pt.FINMIND_URL, params={
        "dataset": "TaiwanFuturesDaily", "data_id": "TMF", "start_date": start},
        headers={"Authorization": f"Bearer {os.environ['FINMIND_TOKEN']}"}, timeout=60).json()["data"]
        if r["contract_date"] == cm]


# ---------- 進場 ----------
def try_enter(st, p, now):
    start = datetime.combine(date.fromisoformat(p["entry_day"]), pt.NIGHT_OPEN, pt.TAIPEI)
    qs = [q for q in (pt.fetch_contract(mt, p["contract"]) for mt in ("1", "0"))
          if q and q["open"] and pt.session_start(q) >= start]
    if not qs:
        return
    q = min(qs, key=pt.session_start)
    px = q["open"]
    stop = px * (1 - STOP)
    p.update({"status": "open", "entry": px, "stop": stop, "entry_session": pt.session_start(q).isoformat()})
    st["seen"] = {}
    sess = "夜盤" if q["mt"] == "1" else "日盤"
    ledger({"time": stamp(now), "period": p["k"], "action": "BUY", "contract": p["contract"], "price": f"{px:.0f}",
            "stop": f"{stop:.0f}", "reason": f"結算{sess}開盤進場"})
    pt.notify(f"{TAG}\n第 {p['k']} 期：{p['entry_day'][5:].replace('-', '/')} 結算{sess}開盤虛擬買進 {p['contract']} 1 口 @{fmt(px)}\n"
              f"停損 {fmt(stop)}（-4%，{fmt(px - stop)} 點＝小台 {fmt((px - stop) * MTX)} 元／微台 {fmt((px - stop) * TMF)} 元）\n"
              f"沒停損就抱到 {p['settle_day'][5:].replace('-', '/')} 結算")


# ---------- 停損 ----------
def check_stop(st, p, now):
    sessions = []
    for mt in ("1", "0"):
        q = pt.fetch_contract(mt, p["contract"])
        if q and q["low"] and pt.session_start(q).isoformat() >= p["entry_session"]:
            sessions.append(q)
    for q in sorted(sessions, key=pt.session_start):
        key = pt.session_start(q).isoformat()
        ts = q["ts"].isoformat()
        seen = st["seen"].get(key)
        if seen is None:
            lo = min(q["low"], q["price"])
            op = q["open"] if key != p["entry_session"] else None
        elif ts <= seen["ts"]:
            continue
        else:
            lo = min(q["low"], q["price"]) if q["low"] < seen["lo"] else q["price"]
            op = None
        st["seen"][key] = {"lo": q["low"], "ts": ts}
        if op is not None and op <= p["stop"]:
            return close(p, op, now, f"開盤 {fmt(op)} 就跳空跌破停損 {fmt(p['stop'])}，用開盤價成交", "stopped")
        if lo <= p["stop"]:
            return close(p, math.floor(p["stop"]), now, f"跌破停損 {fmt(p['stop'])}", "stopped")


def close(p, px, now, reason, status):
    pts = px - p["entry"]
    p.update({"status": status, "exit": px, "exit_reason": reason})
    ledger({"time": stamp(now), "period": p["k"], "action": "STOP" if status == "stopped" else "SETTLE",
            "contract": p["contract"], "price": f"{px:.0f}", "reason": reason, "pnl_points": f"{pts:+.0f}",
            "mtx_ntd": f"{pts * MTX:+.0f}", "tmf_ntd": f"{pts * TMF:+.0f}"})
    nxt = "等下一次結算夜盤再進場" if p["settle_day"] != SETTLES[-1].isoformat() else "這是最後一期"
    pt.notify(f"{TAG}\n第 {p['k']} 期{'（追溯）' if p.get('retro') else ''}：虛擬賣出 {p['contract']} @{fmt(px)}（{reason}）\n"
              f"這期 {money(pts)}" + (f"\n{nxt}" if status == "stopped" else ""))


# ---------- 結算 ----------
def settle_price(p, now):
    q = pt.fetch_contract("0", p["contract"])
    if q and q["ts"].date().isoformat() == p["settle_day"] and q["ts"].time() >= dtime(13, 25):
        return q["price"]
    cm = f"{p['settle_day'][:4]}{p['settle_day'][5:7]}"
    for r in finmind_rows(cm, p["settle_day"]):
        if r["date"] == p["settle_day"] and r["trading_session"] == "position" and r["close"]:
            return r["close"]
    return None


def try_settle(st, p, now):
    px = settle_price(p, now)
    if px is None:
        return
    p["settle"] = px
    if p["status"] == "open":
        close(p, px, now, "抱到結算，用最後結算價", "settled")
    else:
        hold = px - p["entry"]
        washed = hold > 0
        ledger({"time": stamp(now), "period": p["k"], "action": "SETTLE_REF", "contract": p["contract"], "price": f"{px:.0f}",
                "reason": "已停損，記結算價供對照" + ("（被洗）" if washed else ""), "pnl_points": f"{hold:+.0f}"})
        pt.notify(f"{TAG}\n第 {p['k']} 期結算價 {fmt(px)}。這期先前已停損（{money(p['exit'] - p['entry'])}）；"
                  f"如果沒設停損抱到結算是 {money(hold)} → {'**被洗掉了**' if washed else '停損有保護到'}")


def final_report(st, now):
    lines, tot, tot_hold = [], 0, 0
    for p in st["periods"]:
        pts = p["exit"] - p["entry"]
        hold = p["settle"] - p["entry"]
        tot += pts
        tot_hold += hold
        how = "停損" if p["status"] == "stopped" else "抱到結算"
        lines.append(f"第 {p['k']} 期{'（追溯）' if p.get('retro') else ''} {p['entry_day'][5:].replace('-', '/')}→{p['settle_day'][5:].replace('-', '/')}："
                     f"{fmt(p['entry'])} → {fmt(p['exit'])} {how}，{money(pts)}"
                     + (f"（不停損會是 {hold:+,.0f} 點{'，被洗' if hold > 0 else ''}）" if p["status"] == "stopped" else ""))
    pt.notify(f"{TAG} 結算報告（跑到 12 月合約結算）\n" + "\n".join(lines) +
              f"\n合計 {money(tot)}\n對照：全部不設停損 {money(tot_hold)}\n"
              f"未扣手續費（小台一趟約 200 元、微台約 60 元）。只有 3 期，只能看實盤跟回測有沒有落差，不能當結論。")
    ledger({"time": stamp(now), "period": "-", "action": "REPORT", "pnl_points": f"{tot:+.0f}",
            "mtx_ntd": f"{tot * MTX:+.0f}", "tmf_ntd": f"{tot * TMF:+.0f}", "reason": f"不設停損對照 {tot_hold:+.0f} 點"})
    st["final_sent"] = True


# ---------- 啟動（第 1 期追溯） ----------
def init_state(now):
    periods = [{"k": i + 1, "entry_day": SETTLES[i].isoformat(), "settle_day": SETTLES[i + 1].isoformat(),
                "contract": code_for(SETTLES[i + 1]), "status": "pending"} for i in range(len(SETTLES) - 1)]
    st = {"started": now.isoformat(), "seen": {}, "periods": periods, "final_sent": False}
    p = periods[0]
    cm = f"{p['settle_day'][:4]}{p['settle_day'][5:7]}"
    d1 = (SETTLES[0] + timedelta(days=1)).isoformat()
    rows = sorted(finmind_rows(cm, d1), key=lambda r: (r["date"], r["trading_session"] != "after_market"))
    first = next(r for r in rows if r["date"] >= d1 and r["trading_session"] == "after_market")
    entry = first["open"]
    stop = entry * (1 - STOP)
    p.update({"status": "open", "entry": entry, "stop": stop, "retro": True,
              "entry_session": datetime.combine(SETTLES[0], pt.NIGHT_OPEN, pt.TAIPEI).isoformat()})
    ledger({"time": stamp(now), "period": 1, "action": "BUY", "contract": p["contract"], "price": f"{entry:.0f}",
            "stop": f"{stop:.0f}", "reason": "追溯：9/16 結算夜盤開盤（FinMind）"})
    msg = [f"{TAG} 開跑：每期結算夜盤開盤虛擬買進 1 口、停損 -4%、抱到結算，跑到 12/16（12 月合約結算）",
           f"第 1 期（追溯，9/16 開跑前就開始了）：9/16 結算夜盤開盤 {p['contract']} @{fmt(entry)}，停損 {fmt(stop)}"]
    # 追溯：FinMind 已有的日K（夜盤→日盤依序）檢查有沒有碰過停損
    for r in rows:
        if r["date"] < d1 or not r["min"]:
            continue
        if r["open"] and r["open"] <= stop and not (r["date"] == d1 and r["trading_session"] == "after_market"):
            pt.notify("\n".join(msg))
            close(p, r["open"], now, f"追溯：{r['date']} 開盤跳空跌破停損", "stopped")
            return st
        if r["min"] <= stop:
            pt.notify("\n".join(msg))
            close(p, math.floor(stop), now, f"追溯：{r['date']} 跌破停損", "stopped")
            return st
    low = min(r["min"] for r in rows if r["date"] >= d1 and r["min"])
    msg.append(f"到目前為止最低 {fmt(low)}，還沒碰到停損；之後照常每 5 分鐘檢查")
    msg.append("金額同時換算小台（1 點 50 元）與微台（1 點 10 元）")
    pt.notify("\n".join(msg))
    return st


def main(now=None):
    now = now or datetime.now(pt.TAIPEI)
    st = load_state() or init_state(now)
    if st.get("final_sent"):
        save_state(st)
        return
    for i, p in enumerate(st["periods"]):
        prev_closed = i == 0 or st["periods"][i - 1]["status"] in ("stopped", "settled")
        entry_dt = datetime.combine(date.fromisoformat(p["entry_day"]), pt.NIGHT_OPEN, pt.TAIPEI)
        if p["status"] == "pending" and prev_closed and now >= entry_dt:
            try_enter(st, p, now)
        if p["status"] == "open":
            check_stop(st, p, now)
        settle_dt = datetime.combine(date.fromisoformat(p["settle_day"]), SETTLE_READY, pt.TAIPEI)
        if p["status"] in ("open", "stopped") and p.get("settle") is None and now >= settle_dt:
            try_settle(st, p, now)
    if all(p.get("settle") is not None and p["status"] in ("stopped", "settled") for p in st["periods"]):
        final_report(st, now)
    save_state(st)


if __name__ == "__main__":
    main()
