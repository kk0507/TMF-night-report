# python paper_trader.py -> 模擬交易(影子模式)：照固定規則自己下虛擬單，每筆買賣記到 paper_trades.csv
# 需要環境變數：FINMIND_TOKEN, DISCORD_WEBHOOK_URL
"""微台(TMF)模擬交易，期間：2026-09-25 起到 10月合約結算(2026-10-21)。

規則(開跑前定案，期間不改)：
- 總開關：收盤 > MA60 且 MA20 > MA60(上升趨勢)才做多；不做空。
- 進場訊號(日盤收盤後判斷，全盤日K=前一晚夜盤+當天日盤)：
  ①上突破：收盤 > 前20根最高點
  ②盤整低緣：20日趨勢效率<=0.30 且收盤落在前20根區間的下緣20%
- 進場：訊號成立的當晚夜盤開盤後第一次執行時，用當下價格進場。
- 停損：訊號日起近3根最低點 ×(1-0.15%)，且至少在進場價下方0.5%。
- 停利：進場價 + 1.5 × 停損距離，碰到就出場(盤中最高點碰到即算)。
- 時間停損：持有滿5根日K，在第5根的日盤收盤出場。
- 口數：floor(8,000 ÷ (停損點數×10))，最多3口；<1口就跳過(也記錄)。
- 虛擬本金160,000；手續費每口每邊20元＋期交稅十萬分之2。
- 10/14起不開新倉；10/20日盤收盤強制平倉；10/21後不再執行。
同一次檢查同時碰到停損跟停利，保守算停損。GitHub排程約10~20分鐘跑一次，
所以成交價是「觸價的價位」，實際能不能成交在那個價位是假設。
"""
import csv
import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta

import requests

from taifex_quote import MIS_URL, TAIPEI, fetch_session

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "paper_state.json")
LEDGER_FILE = os.path.join(HERE, "paper_trades.csv")
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

START_DATE = date(2026, 9, 25)
LAST_ENTRY_SIGNAL = date(2026, 10, 13)
FORCE_CLOSE_DATE = date(2026, 10, 20)
END_DATE = date(2026, 10, 21)
CAPITAL = 160_000
RISK_BUDGET = 8_000
MAX_LOTS = 3
POINT_VALUE = 10
FEE_PER_SIDE = 20
TAX_RATE = 0.00002
BUF, MIN_STOP, RR, MAX_HOLD = 0.0015, 0.005, 1.5, 5
DAY_CLOSE = dtime(13, 45)
NIGHT_OPEN = dtime(15, 0)

LEDGER_FIELDS = ["time", "action", "trade_id", "contract", "price", "lots", "stop", "target",
                 "reason", "pnl_points", "pnl_ntd", "fees", "equity"]


# ---------- 狀態、帳本、通知 ----------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"equity": CAPITAL, "next_id": 1, "position": None, "pending": None, "last_eval": None}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.write("\n")


def ledger(row):
    new = not os.path.exists(LEDGER_FILE)
    with open(LEDGER_FILE, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in LEDGER_FIELDS})


def notify(text):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("[discord略過]", text)
        return
    requests.post(url, json={"content": text}, timeout=15).raise_for_status()


def fees(price, lots):
    return lots * (FEE_PER_SIDE + price * POINT_VALUE * TAX_RATE)


# ---------- 報價 ----------
def fetch_contract(market_type, code):
    """指定合約(例如TMFJ6)的報價；code+(-F/-M)。"""
    resp = requests.post(MIS_URL, json={
        "MarketType": market_type, "SymbolType": "F", "KindID": "1", "CID": "TMF", "ExpireMonth": "",
        "rowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"}, timeout=15)
    resp.raise_for_status()
    for q in resp.json()["RtData"]["QuoteList"]:
        if q["SymbolID"].startswith(code + "-") and q["CLastPrice"] and q["CTime"]:
            ts = datetime.strptime(q["CDate"] + q["CTime"], "%Y%m%d%H%M%S").replace(tzinfo=TAIPEI)
            if market_type == "1" and ts.hour < 12:
                ts += timedelta(days=1)
            return {"price": float(q["CLastPrice"]), "open": float(q["COpenPrice"] or 0) or None,
                    "high": float(q["CHighPrice"] or 0) or None, "low": float(q["CLowPrice"] or 0) or None,
                    "ts": ts, "mt": market_type}
    return None


def session_start(q):
    """這筆報價所屬session的開盤時間。"""
    ts = q["ts"]
    if q["mt"] == "0":
        return datetime.combine(ts.date(), dtime(8, 45), TAIPEI)
    d = ts.date() if ts.hour >= 12 else ts.date() - timedelta(days=1)
    return datetime.combine(d, NIGHT_OPEN, TAIPEI)


# ---------- 日K序列 ----------
def history_bars():
    rows = requests.get(FINMIND_URL, params={
        "dataset": "TaiwanFuturesDaily", "data_id": "TMF",
        "start_date": (datetime.now(TAIPEI) - timedelta(days=200)).strftime("%Y-%m-%d")},
        headers={"Authorization": f"Bearer {os.environ['FINMIND_TOKEN']}"}, timeout=60).json()["data"]
    g = defaultdict(dict)
    for r in rows:
        if len(r["contract_date"]) == 6:
            g[(r["date"], r["contract_date"])][r["trading_session"]] = r
    by_date = defaultdict(dict)
    for (d, c), s in g.items():
        by_date[d][c] = s
    bars = []
    for d in sorted(by_date):
        c, s = max(by_date[d].items(), key=lambda kv: sum(x["volume"] for x in kv[1].values()))
        pos, am = s.get("position"), s.get("after_market")
        if not pos or not pos["close"]:
            continue
        parts = [x for x in (pos, am) if x and x["max"]]
        bars.append({"date": d, "c": c, "open": (am or pos)["open"], "high": max(x["max"] for x in parts),
                     "low": min(x["min"] for x in parts), "close": pos["close"], "all": by_date[d]})
    mult = [1.0] * len(bars)
    for r in range(1, len(bars)):
        if bars[r]["c"] != bars[r - 1]["c"]:
            prev = bars[r - 1]["all"]
            new, old = prev.get(bars[r]["c"]), prev.get(bars[r - 1]["c"])
            if new and old and new.get("position") and old.get("position") and old["position"]["close"]:
                f = new["position"]["close"] / old["position"]["close"]
                for i in range(r):
                    mult[i] *= f
    for i, b in enumerate(bars):
        for k in ("open", "high", "low", "close"):
            b[k] *= mult[i]
    return bars


def today_bar(now):
    """用期交所即時資料組出今天的全盤日K(昨晚夜盤+今天日盤)。"""
    day = fetch_session("0")
    if not day or day["ts"].date() != now.date() or day["ts"].time() < dtime(13, 40):
        return None
    night = fetch_session("1")
    parts = [day]
    if night and night["contract"][:5] == day["contract"][:5] and \
            now - timedelta(days=4) < night["ts"] < datetime.combine(now.date(), dtime(8, 45), TAIPEI):
        parts.append(night)
    return {"date": now.date().isoformat(), "open": parts[-1]["open"],
            "high": max(p["high"] for p in parts), "low": min(p["low"] for p in parts),
            "close": day["price"], "contract": day["contract"][:5], "has_night": len(parts) == 2}


def evaluate(bars):
    """回傳 (總開關是否通過, 訊號名稱或None, 說明文字, 建議停損)。bars最後一根=今天。"""
    c = [b["close"] for b in bars]
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    i = len(bars) - 1
    ma20 = sum(c[i - 19:i + 1]) / 20
    ma60 = sum(c[i - 59:i + 1]) / 60
    regime = c[i] > ma60 and ma20 > ma60
    hi20, lo20 = max(h[i - 20:i]), min(l[i - 20:i])
    path = sum(abs(c[j] - c[j - 1]) for j in range(i - 19, i + 1))
    te = abs(c[i] - c[i - 20]) / path if path else 1
    pos = (c[i] - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5
    info = (f"收{c[i]:,.0f}｜MA20 {ma20:,.0f}｜MA60 {ma60:,.0f}｜總開關{'✅' if regime else '❌'}\n"
            f"20日高 {hi20:,.0f}｜20日區間位置 {pos * 100:.0f}%｜趨勢效率 {te:.2f}")
    sig = None
    if c[i] > hi20:
        sig = "上突破(收盤>20日高)"
    elif te <= 0.30 and pos <= 0.20:
        sig = "盤整低緣(效率<=0.3、區間下緣20%)"
    stop_ref = min(l[i - 2:i + 1]) * (1 - BUF)
    return regime, sig, info, stop_ref


# ---------- 交易動作 ----------
def close_position(st, price, when, reason):
    p = st["position"]
    pts = price - p["entry"]
    fee = fees(price, p["lots"])
    pnl = pts * POINT_VALUE * p["lots"] - fee
    st["equity"] += pnl
    total = pnl - p["entry_fee"]
    ledger({"time": when.strftime("%Y-%m-%d %H:%M"), "action": "SELL", "trade_id": p["id"], "contract": p["contract"],
            "price": f"{price:.0f}", "lots": p["lots"], "stop": f"{p['stop']:.0f}", "target": f"{p['target']:.0f}",
            "reason": reason, "pnl_points": f"{pts:+.0f}",
            "pnl_ntd": f"{total:+.0f}", "fees": f"{p['entry_fee'] + fee:.0f}", "equity": f"{st['equity']:.0f}"})
    notify(f"**【模擬】平倉 #{p['id']}**　{reason}\n"
           f"{p['contract']} {p['lots']}口　進{p['entry']:,.0f} → 出{price:,.0f}（{pts:+,.0f}點）\n"
           f"損益 {total:+,.0f} 元(已扣進出手續費稅)｜模擬權益 {st['equity']:,.0f}")
    st["position"] = None


def check_exit(st, now):
    p = st["position"]
    best_hi, best_lo, gap_open = None, None, None
    for mt in ("0", "1"):
        q = fetch_contract(mt, p["contract"])
        if not q or q["high"] is None:
            continue
        ss = session_start(q)
        entry_ss = datetime.fromisoformat(p["entry_session"])
        if ss < entry_ss:
            continue
        if ss == entry_ss:
            hi = q["high"] if q["high"] > p["base_hi"] else None
            lo = q["low"] if q["low"] < p["base_lo"] else None
        else:
            hi, lo = q["high"], q["low"]
            if q["open"]:
                gap_open = q["open"]
        if hi is not None:
            best_hi = hi if best_hi is None else max(best_hi, hi)
        if lo is not None:
            best_lo = lo if best_lo is None else min(best_lo, lo)
    if best_lo is not None and best_lo <= p["stop"]:
        px = gap_open if gap_open and gap_open <= p["stop"] else p["stop"]
        close_position(st, px, now, "停損")
        return True
    if best_hi is not None and best_hi >= p["target"]:
        px = gap_open if gap_open and gap_open >= p["target"] else p["target"]
        close_position(st, px, now, "停利")
        return True
    return False


def try_enter(st, now):
    pend = st["pending"]
    q = fetch_session("1")
    if not q or q["ts"] < datetime.fromisoformat(pend["valid_from"]) or now - q["ts"] > timedelta(minutes=20):
        return
    entry = q["price"]
    stop = min(pend["stop_ref"], entry * (1 - MIN_STOP))
    risk_pts = entry - stop
    target = entry + RR * risk_pts
    lots = min(MAX_LOTS, math.floor(RISK_BUDGET / (risk_pts * POINT_VALUE)))
    when = now.strftime("%Y-%m-%d %H:%M")
    if lots < 1:
        ledger({"time": when, "action": "SKIP", "contract": q["contract"][:5], "price": f"{entry:.0f}",
                "stop": f"{stop:.0f}", "reason": f"{pend['signal']}；停損{risk_pts:.0f}點，1口風險超過8,000",
                "equity": f"{st['equity']:.0f}"})
        notify(f"**【模擬】跳過** {pend['signal']}：停損要{risk_pts:,.0f}點，1口就超過8,000元風險上限")
        st["pending"] = None
        return
    tid = st["next_id"]
    st["next_id"] += 1
    fee = fees(entry, lots)
    st["equity"] -= fee
    cp = q["contract"][:5]
    base = fetch_contract("1", cp) or q
    st["position"] = {"id": tid, "contract": cp, "entry": entry, "lots": lots, "stop": stop, "target": target,
                      "entry_time": q["ts"].isoformat(), "entry_session": session_start({**q, "mt": "1"}).isoformat(),
                      "base_hi": base["high"], "base_lo": base["low"], "bars_held": 0, "entry_fee": fee,
                      "signal": pend["signal"]}
    ledger({"time": when, "action": "BUY", "trade_id": tid, "contract": cp, "price": f"{entry:.0f}", "lots": lots,
            "stop": f"{stop:.0f}", "target": f"{target:.0f}", "reason": pend["signal"],
            "fees": f"{fee:.0f}", "equity": f"{st['equity']:.0f}"})
    notify(f"**【模擬】進場 #{tid} 多{lots}口 {cp}**　@{entry:,.0f}\n理由：{pend['signal']}\n"
           f"停損 {stop:,.0f}（{risk_pts:,.0f}點，風險{risk_pts * POINT_VALUE * lots:,.0f}元）｜"
           f"停利 {target:,.0f}（{RR * risk_pts:,.0f}點）｜最多抱5個交易日")
    st["pending"] = None


def day_eval(st, now):
    tb = today_bar(now)
    if not tb:
        return
    today = now.date()
    bars = [b for b in history_bars() if b["date"] < tb["date"]] + [tb]
    regime, sig, info, stop_ref = evaluate(bars)
    lines = [f"**【模擬交易日報】{tb['date']}**", info]
    if not tb["has_night"]:
        lines.append("(昨晚夜盤資料抓不到，今天日K只用日盤)")
    p = st["position"]
    if p:
        p["bars_held"] += 1
        if p["bars_held"] >= MAX_HOLD or today >= FORCE_CLOSE_DATE:
            close_position(st, tb["close"], now, "持滿5日時間出場" if p["bars_held"] >= MAX_HOLD else "結算前強制平倉")
            lines.append("持倉已出場(見上一則)")
        else:
            lines.append(f"持倉 #{p['id']} 多{p['lots']}口 進{p['entry']:,.0f}，收盤浮動 "
                         f"{(tb['close'] - p['entry']) * POINT_VALUE * p['lots']:+,.0f}元｜已抱{p['bars_held']}天")
    if st["position"] is None:
        if today > LAST_ENTRY_SIGNAL:
            lines.append("決定：10/14起不開新倉")
        elif not regime:
            lines.append(f"決定：總開關未通過，不進場{f'（有{sig}訊號但不理會）' if sig else ''}")
        elif not sig:
            lines.append("決定：無訊號，空手")
        else:
            st["pending"] = {"signal": sig, "stop_ref": stop_ref,
                             "valid_from": datetime.combine(today, NIGHT_OPEN, TAIPEI).isoformat()}
            lines.append(f"決定：**{sig}**，今晚夜盤開盤進場多單（停損參考 {stop_ref:,.0f}）")
    lines.append(f"模擬權益 {st['equity']:,.0f}")
    notify("\n".join(lines))
    ledger({"time": now.strftime("%Y-%m-%d %H:%M"), "action": "EVAL", "contract": tb["contract"],
            "price": f"{tb['close']:.0f}", "reason": lines[-2].replace("決定：", "").replace("*", ""),
            "equity": f"{st['equity']:.0f}"})
    st["last_eval"] = today.isoformat()


def main(now=None):
    now = now or datetime.now(TAIPEI)
    if now.date() < START_DATE or now.date() > END_DATE:
        print("不在模擬期間")
        return
    st = load_state()
    if st["pending"] and now.date().isoformat() > st["pending"]["valid_from"][:10] and now.time() >= dtime(8, 45):
        ledger({"time": now.strftime("%Y-%m-%d %H:%M"), "action": "CANCEL", "reason": f"{st['pending']['signal']}：夜盤沒抓到進場時機",
                "equity": f"{st['equity']:.0f}"})
        st["pending"] = None
    if st["position"]:
        check_exit(st, now)
    if st["pending"] and not st["position"] and now.time() >= NIGHT_OPEN:
        try_enter(st, now)
    if now.weekday() < 5 and now.time() >= DAY_CLOSE and st["last_eval"] != now.date().isoformat():
        day_eval(st, now)
    save_state(st)


if __name__ == "__main__":
    main()
