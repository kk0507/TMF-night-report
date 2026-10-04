# python nq_open.py [--dry]
# 那指期貨週一開盤通知：隔了一個週末後，那指期貨、標普期貨、原油重新開盤的跳空＋粗估對微台開盤的影響，發到報價頻道。每週只發一次。
# 需要環境變數：DISCORD_WEBHOOK_URL（沒有就只印出來）
"""美股期貨、原油週日 18:00（美東）重新開盤＝台北夏令週一 06:00／冬令 07:00；平日只休息一小時、幾乎沒有跳空，所以只做週一。
Yahoo 週末後的第一筆固定是開盤後 10 分鐘（台北 06:10），拿不到真正的開盤價，報價又延遲約 10 分鐘，所以排程排在開盤後 30 分鐘、訊息寫「首筆」不寫「開盤價」。
不用連續月算跳空（換月那週新舊合約混在一起會失真，見 dashboard-prices/fetch_global.py），改找報價對應的那一個合約。
repo 是公開的：訊息不寫部位、口數、損益。--dry：不發送、不記錄，資料裡最近一次週末開盤是哪次就印哪次（給人看樣本用）。"""
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from taifex_quote import TAIPEI, fetch_session

NY = ZoneInfo("America/New_York")
H = {"User-Agent": "Mozilla/5.0"}
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
STATE_FILE = "nq_open_state.json"
MONTHS = "FGHJKMNQUVXZ"
FUTURES = [  # (連續月代號, 合約根, 交易所, 只有季月, 名稱, 小數位)
    ("NQ=F", "NQ", "CME", True, "那指期貨", 0), ("ES=F", "ES", "CME", True, "標普期貨", 0),
    ("CL=F", "CL", "NYM", False, "WTI 原油", 2), ("BZ=F", "BZ", "NYM", False, "布蘭特原油", 2),
]
BETA_NQ = 0.67  # us_correlation.py：微台夜盤 ≈ 那指 × 0.67
WEEK = "一二三四五六日"


def chart(sym, rng, interval):
    r = requests.get(CHART + sym, params={"range": rng, "interval": interval}, headers=H, timeout=20)
    r.raise_for_status()
    j = r.json()["chart"]["result"][0]
    q = j["indicators"]["quote"][0]
    bars = [(datetime.fromtimestamp(t, NY), o, c)
            for t, o, c in zip(j.get("timestamp") or [], q.get("open") or [], q.get("close") or [])
            if o is not None and c is not None]
    return j["meta"]["regularMarketPrice"], bars


def same_contract(cont, root, exch, quarterly, today):
    """找出報價跟連續月一樣的那個合約（往後 13 個月內）；找不到就退回連續月。"""
    price, _ = chart(cont, "1d", "1d")
    for i in range(13):
        m0 = today.month - 1 + i
        y, m = today.year + m0 // 12, m0 % 12 + 1
        if quarterly and m not in (3, 6, 9, 12):
            continue
        sym = f"{root}{MONTHS[m - 1]}{y % 100}.{exch}"
        try:
            p, _ = chart(sym, "1d", "1d")
        except Exception:  # noqa: BLE001 該月沒有合約或抓不到
            continue
        if abs(p - price) <= price * 0.0005:
            return sym
    return cont


def weekend_open(bars):
    """最近一次週末後的開盤：美東週日的第一根、而且跟前一根隔了 40 小時以上。回傳（前一根, 開盤那根的位置）。"""
    for i in range(len(bars) - 1, 0, -1):
        if bars[i][0].weekday() == 6 and bars[i][0] - bars[i - 1][0] > timedelta(hours=40):
            return bars[i - 1], i
    return None, None


def main():
    dry = "--dry" in sys.argv
    now = datetime.now(NY)
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}

    rows, opened = [], None
    for cont, root, exch, quarterly, name, nd in FUTURES:
        try:
            sym = same_contract(cont, root, exch, quarterly, now.date())
            _, bars = chart(sym, "1mo", "5m")
            prev, i = weekend_open(bars)
        except Exception as e:  # noqa: BLE001 一個商品抓不到不能擋住其他的
            print(f"{name} 抓取失敗：", e)
            continue
        if prev is None:
            continue
        t_open = bars[i][0]
        if cont == "NQ=F":
            opened = t_open
        # 正式跑＝報最新一根；樣本模式＝報開盤後 20 分鐘那根，跟實際發送時看到的一樣
        later = [b for b in bars[i:] if dry and b[0] <= t_open + timedelta(minutes=20)] or bars[i:]
        rows.append((cont, name, nd, prev[2], bars[i][1], later[-1][2], sym))

    if opened is None:
        print("那指期貨抓不到週末後的開盤資料")
        return
    if not dry and now - opened > timedelta(hours=6):
        print(f"最近一次週末開盤是 {opened:%Y-%m-%d %H:%M}（美東），這週的還沒開盤")
        return
    if not dry and state.get("last_sent") == opened.date().isoformat():
        print(f"{opened.date()} 這次開盤已經發過")
        return

    tw = opened.astimezone(TAIPEI)
    lines = [f"**【那指期貨週一開盤】{tw.month}/{tw.day}（{WEEK[tw.weekday()]}）**　比上週五收盤",
             f"首筆＝{tw:%H:%M}（開盤後約 10 分鐘，資料來源沒有更早的）"]
    nq_chg = None
    for cont, name, nd, prev, o, cur, sym in rows:
        gap, chg = (o / prev - 1) * 100, (cur / prev - 1) * 100
        lines.append(f"{name} 首筆 {o:,.{nd}f}（{o - prev:+,.{nd}f}、{gap:+.2f}%）→ 目前 {cur:,.{nd}f}（{chg:+.2f}%）")
        if cont == "NQ=F":
            nq_chg = chg / 100
    lines.append("━━━━━━━━━━━━")

    sessions = [s for s in (fetch_session("0"), fetch_session("1")) if s]
    tmf = max(sessions, key=lambda s: s["ts"]) if sessions else None
    if tmf and nq_chg is not None:
        est = BETA_NQ * nq_chg * tmf["price"]
        sess = "夜盤" if tmf["ts"].hour < 8 or tmf["ts"].hour >= 15 else "日盤"
        lines.append(f"微台上次收盤 {tmf['price']:,.0f}（{sess}，{tmf['ts'].month}/{tmf['ts'].day} {tmf['ts']:%H:%M}）")
        lines.append(f"👉 粗估微台開盤影響：那指期貨 {nq_chg * 100:+.2f}% × {BETA_NQ} ≈ {est:+,.0f} 點 → 約 {tmf['price'] + est:,.0f}")
    lines.append("⚠️ 粗估：週末後剛開盤成交量少，到 08:45 台指開盤時可能已經不同；報價延遲約 10 分鐘。"
                 "那指期貨只能預告開高或開低，開盤後的走勢看不出來。")

    msg = "\n".join(lines)
    print(msg)
    if dry:
        return
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if url:
        requests.post(url, json={"content": msg}, timeout=15).raise_for_status()
    state["last_sent"] = opened.date().isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
