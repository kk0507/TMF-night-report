# python us_close.py [--force]
# 美股收盤通知：費半、那指、標普收盤＋粗估對微台下次開盤的影響，發到報價頻道。每個美股交易日只發一次。
# 需要環境變數：DISCORD_WEBHOOK_URL（沒有就只印出來）
"""微台跟美股的關係（us_correlation.py，微台上市以來同時段夜盤）：微台夜盤漲跌 ≈ 費半 × 0.36 ≈ 那指 × 0.67，
約四成的波動解釋不到。微台夜盤還在交易時，美股的影響已經反映在夜盤價格裡，就直接報夜盤現價；
台股休市（週末、連假）時，把「微台上次收盤之後」所有美股交易日的漲跌累計起來估下次開盤。"""
import json
import os
import sys
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests

from taifex_quote import TAIPEI, fetch_session

NY = ZoneInfo("America/New_York")
H = {"User-Agent": "Mozilla/5.0"}
STATE_FILE = "us_close_state.json"
INDEXES = [("^SOX", "費城半導體", 0.36), ("^IXIC", "那斯達克", 0.67), ("^GSPC", "標普500", None)]
WEEK = "一二三四五六日"


def daily(sym):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"range": "1mo", "interval": "1d"}, headers=H, timeout=20)
    r.raise_for_status()
    j = r.json()["chart"]["result"][0]
    return [(datetime.fromtimestamp(t, NY).date(), c)
            for t, c in zip(j["timestamp"], j["indicators"]["quote"][0]["close"]) if c]


def us_close_time(d):
    return datetime.combine(d, dtime(16, 0), NY)


def md(d):
    return f"{d.month}/{d.day}"


def main():
    force = "--force" in sys.argv
    now = datetime.now(NY)
    data = {sym: daily(sym) for sym, _, _ in INDEXES}
    last_day = data["^SOX"][-1][0]
    if not force and now < us_close_time(last_day) + timedelta(minutes=5):
        print(f"{last_day} 美股還沒收盤")
        return
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if not force and state.get("last_sent") == last_day.isoformat():
        print(f"{last_day} 已經發過")
        return

    lines = [f"**【美股收盤】{md(last_day)}（{WEEK[last_day.weekday()]}）**"]
    for sym, name, _ in INDEXES:
        (_, prev), (_, close) = data[sym][-2], data[sym][-1]
        lines.append(f"{name} {close:,.0f}（{(close / prev - 1) * 100:+.2f}%）")
    lines.append("━━━━━━━━━━━━")

    # 微台：夜盤還在交易就報現價；休市就累計「微台上次收盤之後」的美股漲跌來估
    sessions = [s for s in (fetch_session("0"), fetch_session("1")) if s]
    tmf = max(sessions, key=lambda s: s["ts"]) if sessions else None
    if tmf and datetime.now(TAIPEI) - tmf["ts"] < timedelta(minutes=20):
        lines.append(f"微台夜盤目前 {tmf['price']:,.0f}（{tmf['diff']:+,.0f}），美股的影響已經反映在夜盤價格裡。")
    elif tmf:
        t_last = tmf["ts"]
        est = []
        parts = []
        n_days = 0
        for sym, name, beta in INDEXES:
            if beta is None:
                continue
            rows = data[sym]
            base = [c for d, c in rows if us_close_time(d) <= t_last]
            after = [(d, c) for d, c in rows if us_close_time(d) > t_last]
            if not base or not after:
                continue
            cum = after[-1][1] / base[-1] - 1
            n_days = len(after)
            parts.append(f"{'費半' if sym == '^SOX' else '那指'} {cum * 100:+.2f}%")
            est.append(beta * cum * tmf["price"])
        sess = "夜盤" if tmf["ts"].hour < 8 or tmf["ts"].hour >= 15 else "日盤"
        lines.append(f"微台上次收盤 {tmf['price']:,.0f}（{sess}，{md(tmf['ts'])} {tmf['ts']:%H:%M}）")
        if est:
            lo, hi = min(est), max(est)
            lines.append(f"之後美股累計（{n_days} 個交易日）：" + "、".join(parts))
            lines.append(f"👉 粗估微台下次開盤影響：{lo:+,.0f}～{hi:+,.0f} 點 → 約 {tmf['price'] + lo:,.0f}～{tmf['price'] + hi:,.0f}")
        lines.append("⚠️ 粗估：微台大約是費半漲跌的 0.36 倍、那指的 0.67 倍，約四成的波動解釋不到；殖利率、油價、中東消息也會影響。")

    msg = "\n".join(lines)
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if url:
        requests.post(url, json={"content": msg}, timeout=15).raise_for_status()
    print(msg)
    state["last_sent"] = last_day.isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
