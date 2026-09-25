# python reversal_watch.py [--demo]
# 微台反轉監控（規則跟 reversal_timing.py 回測的一樣，門檻 2%，只看日盤收盤）：
#   開始往上＝收盤比「最近最低的收盤」漲回 2% 以上；漲完了＝收盤比「最近最高的收盤」跌下來 2% 以上。
# 每個交易日 13:45 收盤後判斷一次：發【微台收盤狀態】（趨勢＋波動溫度計），有反轉就在同一則最上面加【反轉訊號】。
# 盤中（日盤、夜盤）：價格越過反轉線先發一次【反轉預警】，只看即時價，不抓歷史資料。
# --demo：不發 Discord、不寫狀態，印出目前狀態＋最近一次「開始往上」「漲完了」當天會收到的訊息
# 需要環境變數：FINMIND_TOKEN、DISCORD_WEBHOOK_URL（demo 不用）
import json
import os
import statistics
import sys
from datetime import datetime, timedelta

import requests

from backtest_confirm_entry import load_days
from taifex_quote import TAIPEI, fetch_session, get_live_price

TH = 0.02
STATE_FILE = "reversal_state.json"
MONTH_CODE = "ABCDEFGHIJKL"  # 期交所月份代碼 A=1月 ... L=12月

# 波動溫度計（big_move.py，台指期 2009~2026）：近 20 日平均振幅佔價格 % 的分布（每 5% 一格），
# 以及分成 5 組後「未來 10 天漲跌超過 4%」的比例、10 天高低差中位數。改資料要重跑 big_move.py 更新。
ATR_PCTL = [0.76, 0.852, 0.918, 0.974, 1.034, 1.093, 1.166, 1.221, 1.278, 1.337,
            1.401, 1.467, 1.556, 1.642, 1.743, 1.867, 2.012, 2.23, 2.707]
ATR_GROUPS = [  # (上限%, 名稱, 大波段機率%, 10天高低差中位%)
    (0.974, "很平靜", 6, 3.1), (1.221, "偏平靜", 13, 3.9), (1.467, "普通", 25, 4.6),
    (1.867, "偏大", 26, 5.4), (99.0, "很大", 37, 6.7)]
BIG_BASE = 21


def mis_contract_to_month(symbol):
    """TMFJ6-M → 202610（年份只給個位數，取離現在最近的那個十年）。"""
    code = symbol[3:5]
    month = MONTH_CODE.index(code[0]) + 1
    now_year = datetime.now(TAIPEI).year
    year = now_year - now_year % 10 + int(code[1])
    if year < now_year - 1:
        year += 10
    return f"{year}{month:02d}"


def load_bars():
    start = (datetime.now(TAIPEI) - timedelta(days=500)).strftime("%Y-%m-%d")
    days = load_days("TMF", start)
    return days, days[-1]["c"]


def today_close(now):
    """日盤收盤後，行情網(MIS)的日盤最後成交＝今天收盤（FinMind 晚上才有）。不是今天的就回 None。"""
    if now.weekday() >= 5 or (now.hour, now.minute) < (13, 45):
        return None
    s = fetch_session("0")
    if not s or s["ts"].strftime("%Y-%m-%d") != now.strftime("%Y-%m-%d") or (s["ts"].hour, s["ts"].minute) < (13, 44):
        return None
    return s


def append_today(days, contract, s):
    """把今天的K棒接上去；換月當天合約對不上就不接（隔天 FinMind 有資料再算）。"""
    today = s["ts"].strftime("%Y-%m-%d")
    if days[-1]["date"] >= today or mis_contract_to_month(s["contract"]) != contract:
        return False
    # 今天的K棒＝昨晚夜盤＋今天日盤（跟 load_days 一樣的合併方式），夜盤高低用行情網的夜盤欄位
    n = fetch_session("1")
    hs = [x for x in (s["high"], n and n["high"]) if x]
    ls = [x for x in (s["low"], n and n["low"]) if x]
    days.append({"date": today, "c": contract, "open": s["open"], "high": max(hs), "low": min(ls), "close": s["price"]})
    return True


def run_state(closes):
    """跟回測 sar() 同一套狀態機，回傳目前狀態＋每次反轉的 index。"""
    d, ext, ext_i, flips = 0, None, None, []
    hi = lo = closes[0]
    hi_i = lo_i = 0
    for i in range(1, len(closes)):
        c = closes[i]
        if d == 0:
            if c > hi:
                hi, hi_i = c, i
            if c < lo:
                lo, lo_i = c, i
            if c >= lo * (1 + TH):
                flips.append({"i": i, "dir": 1, "from_i": lo_i})
                d, ext, ext_i = 1, c, i
            elif c <= hi * (1 - TH):
                flips.append({"i": i, "dir": -1, "from_i": hi_i})
                d, ext, ext_i = -1, c, i
            continue
        if (d == 1 and c > ext) or (d == -1 and c < ext):
            ext, ext_i = c, i
        if (d == 1 and c <= ext * (1 - TH)) or (d == -1 and c >= ext * (1 + TH)):
            flips.append({"i": i, "dir": -d, "from_i": ext_i})
            d, ext, ext_i = -d, c, i
    line = ext * (1 - TH) if d == 1 else ext * (1 + TH)
    return {"dir": d, "ext": ext, "ext_i": ext_i, "line": line, "flips": flips}


def fmt(x):
    return f"{x:,.0f}"


def md(date):
    return f"{int(date[5:7])}/{int(date[8:])}"


def thermometer(days, i=None):
    i = len(days) - 1 if i is None else i
    tr = [max(days[k]["high"], days[k - 1]["close"]) - min(days[k]["low"], days[k - 1]["close"]) for k in range(i - 19, i + 1)]
    atr = statistics.mean(tr)
    pct = atr / days[i]["close"] * 100
    rank = sum(1 for x in ATR_PCTL if x <= pct) * 5
    name, big, rng = next((n, b, r) for up, n, b, r in ATR_GROUPS if pct <= up)
    return (f"🌡️ 波動：{name}（平均一天動 {fmt(atr)} 點，比過去約 {rank}% 的日子大）\n"
            f"　→ 未來 10 天大漲或大跌超過 1,900 點的機會約 {big}%（平常 {BIG_BASE}%），這 10 天高低差通常約 {fmt(days[i]['close'] * rng / 100)} 點。只講會動多大，不講往哪邊。")


def trend_line(days, st, i=None):
    i = len(days) - 1 if i is None else i
    last = st["flips"][-1] if st["flips"] else None
    since = f"（{md(days[last['i']]['date'])} 開始）" if last else ""
    if st["dir"] == 1:
        return f"📈 趨勢：往上走{since}。收盤跌破 {fmt(st['line'])} 才算漲完（今天收 {fmt(days[i]['close'])}，還差 {fmt(days[i]['close'] - st['line'])} 點）"
    if st["dir"] == -1:
        return f"📉 趨勢：往下走{since}。收盤漲回 {fmt(st['line'])} 以上才算開始往上（今天收 {fmt(days[i]['close'])}，還差 {fmt(st['line'] - days[i]['close'])} 點）"
    return "趨勢：資料還不夠，判斷不出來"


def up_message(days, f):
    i, j = f["i"], f["from_i"]
    c, low = days[i]["close"], days[j]["close"]
    exit_line = c * (1 - TH)
    risk = c - exit_line
    return (f"🔔 **【反轉訊號】微台開始往上了**\n"
            f"今天收 {fmt(c)}，比 {md(days[j]['date'])} 的低點 {fmt(low)} 漲回 {fmt(c - low)} 點（超過 2%）。\n"
            f"👉 想做多的參考：\n"
            f"・進場：約 {fmt(c)}（今晚夜盤起）\n"
            f"・出場：收盤跌破 {fmt(exit_line)} 就走；之後創新高，這條線會跟著往上\n"
            f"・1 口大約最多賠 {fmt(risk * 10)} 元（跳空可能更多）\n"
            f"💡 賺 1,000 點先出一半，剩下的照規則抱\n"
            f"📊 過去 17 年這種訊號：一半賺、一半賠；賠的通常賠 1,000 點左右，賺的有時賺很多\n"
            f"⚠️ 只是照規則提醒，不是保證會漲，要不要進場你決定")


def down_message(days, f):
    i, j = f["i"], f["from_i"]
    c, high = days[i]["close"], days[j]["close"]
    return (f"🔔 **【反轉訊號】微台漲完了？（手上有多單注意）**\n"
            f"今天收 {fmt(c)}，比 {md(days[j]['date'])} 的高點 {fmt(high)} 跌了 {fmt(high - c)} 點（超過 2%）。\n"
            f"👉 手上有多單：照規則這裡該出場\n"
            f"👉 不建議反手做空：過去 17 年照這樣做空是賠錢的\n"
            f"⚠️ 只是照規則提醒，要不要出場你決定")


def daily_message(days, st, signal=None):
    parts = [signal] if signal else []
    parts.append(f"**【微台收盤狀態】{md(days[-1]['date'])}**\n{trend_line(days, st)}\n{thermometer(days)}")
    return "\n━━━━━━━━━━━━\n".join(parts)


def warn_message(line, direction, price, session):
    if direction == 1:
        head, need = f"盤中跌破 {fmt(line)}", f"收在 {fmt(line)} 以下，才算「漲完了」"
    else:
        head, need = f"盤中漲過 {fmt(line)}", f"收在 {fmt(line)} 以上，才算「開始往上」"
    return (f"⏰ **【反轉預警】微台{head}**\n"
            f"現在 {fmt(price)}（{session}）。還不算數，要看下一個日盤 13:45 收盤有沒有{need}。")


def send(msg):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if url:
        requests.post(url, json={"content": msg[:1990]}, timeout=15).raise_for_status()
    print(msg)


def demo():
    now = datetime.now(TAIPEI)
    days, contract = load_bars()
    s = today_close(now)
    added = append_today(days, contract, s) if s else False
    closes = [d["close"] for d in days]
    st = run_state(closes)
    print(f"（資料到 {days[-1]['date']}{'，今天收盤取自期交所行情網' if added else ''}；價格已換月調整成目前合約 {contract}）\n")
    print("===== 每天 13:45 收盤後會收到（用最新資料） =====")
    print(daily_message(days, st), "\n")
    last_up = next((f for f in reversed(st["flips"]) if f["dir"] == 1), None)
    last_dn = next((f for f in reversed(st["flips"]) if f["dir"] == -1), None)
    for f, title, fn in ((last_up, "開始往上", up_message), (last_dn, "漲完了", down_message)):
        if not f:
            continue
        sub = days[:f["i"] + 1]
        print(f"===== 最近一次「{title}」（{sub[-1]['date']}）那天會收到 =====")
        print(daily_message(sub, run_state([d["close"] for d in sub]), fn(days, f)), "\n")
    print("===== 盤中越過反轉線時會收到（用目前的線示範） =====")
    print(warn_message(st["line"], st["dir"], st["line"] - 20 if st["dir"] == 1 else st["line"] + 20, "日盤"))


def main():
    if "--demo" in sys.argv:
        demo()
        return
    now = datetime.now(TAIPEI)
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    today = now.strftime("%Y-%m-%d")

    s = today_close(now) if state.get("eval_date") != today else None
    if s or "line" not in state:
        # 每天收盤後算一次（或第一次跑）：抓歷史、接上今天、判斷反轉、發收盤狀態
        days, contract = load_bars()
        if s:
            append_today(days, contract, s)
        st = run_state([d["close"] for d in days])
        last = st["flips"][-1] if st["flips"] else None
        is_new = bool(last and last["i"] == len(days) - 1 and days[-1]["date"] == today
                      and state.get("last_signal_date") != today and "line" in state)
        signal = (up_message(days, last) if last["dir"] == 1 else down_message(days, last)) if is_new else None
        if s and days[-1]["date"] == today:
            send(daily_message(days, st, signal))
            state["eval_date"] = today
        if is_new:
            state["last_signal_date"] = today
        state.update({"dir": st["dir"], "line": round(st["line"]), "ext": round(st["ext"]), "data_date": days[-1]["date"]})
    elif state.get("dir"):
        # 盤中：只看即時價有沒有越過反轉線，每條線只提醒一次
        live = get_live_price()
        if live:
            crossed = live["price"] <= state["line"] if state["dir"] == 1 else live["price"] >= state["line"]
            key = f"{state['dir']}@{state['line']}"
            if crossed and state.get("warned") != key:
                session = "日盤" if 8 <= live["ts"].hour < 14 else "夜盤"
                send(warn_message(state["line"], state["dir"], live["price"], session))
                state["warned"] = key
    state["updated"] = now.isoformat(timespec="seconds")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
