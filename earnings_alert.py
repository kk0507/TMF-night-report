# python earnings_alert.py remind|result|test [--dry]
# 財報／法說提醒，發到報價頻道：
#   remind（每天台北 20:30）：未來 24 小時內要公布的財報／法說 → 公布時間、要看什麼、影響到哪些持股；週日另發下週清單。
#   result（每天台北 07:30）：美股公司昨晚公布後的股價反應（盤後價），趕在台股 09:00 開盤前。
# 美股公司的日期每天從 Nasdaq 抓（公司公布＝已確認，否則是推估）；非美股（台積電、三星…）寫在 earnings_events.json。
# 持股只放在 GitHub secret HOLDINGS_JSON（repo 公開，程式和紀錄都不印持股）：{"代號": {"n": "名稱", "t": ["memory", ...]}}
# 需要環境變數：DISCORD_WEBHOOK_URL、HOLDINGS_JSON（沒有就只印出、不列持股）
import json
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

TPE, NY = ZoneInfo("Asia/Taipei"), ZoneInfo("America/New_York")
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
EVENTS_FILE, STATE_FILE = "earnings_events.json", "earnings_state.json"
WEEK = "一二三四五六日"
TAG_NAME = {"memory": "記憶體", "thermal": "散熱", "power": "電源／電網", "passive": "被動元件", "optical": "光通訊",
            "mobile": "手機", "ai": "AI 供應鏈", "all": "全體"}


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def nasdaq_date(sym):
    """回傳 (NY 日期, 'amc'|'bmo'|None, 是否公司已公布)；抓不到回 None。"""
    try:
        r = requests.get(f"https://api.nasdaq.com/api/analyst/{sym}/earnings-date", headers=H, timeout=20)
        txt = r.json()["data"]["reportText"]
    except Exception as e:  # noqa: BLE001
        print(f"nasdaq {sym} 失敗：{e}")
        return None
    m = re.search(r"on\s+(\d{2})/(\d{2})/(\d{4})", txt)
    if not m:
        return None
    d = datetime(int(m[3]), int(m[1]), int(m[2])).date()
    timing = "amc" if "after market close" in txt else "bmo" if "before market open" in txt else None
    return d, timing, "expected" in txt


def release_tw(d, timing):
    """美股公布時間換成台北時間（盤後 16:05、盤前 07:00、不明當盤後）。"""
    hh, mm = (7, 0) if timing == "bmo" else (16, 5)
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).astimezone(TPE)


def all_events(cfg, state):
    evs = []
    for e in cfg["manual"]:
        evs.append({**e, "when": datetime.fromisoformat(e["tw"]).replace(tzinfo=TPE), "key": f"{e['name']}|{e['tw'][:10]}"})
    known = state.setdefault("us_dates", {})
    for sym, info in cfg["us"].items():
        got = nasdaq_date(sym)
        if got:
            d, timing, conf = got
            known[sym] = {"d": d.isoformat(), "timing": timing, "confirmed": conf}
        k = known.get(sym)
        if not k:
            continue
        d = datetime.fromisoformat(k["d"]).date()
        evs.append({**info, "sym": sym, "confirmed": k["confirmed"], "timing": k["timing"],
                    "when": release_tw(d, k["timing"]), "ny_date": k["d"], "key": f"{sym}|{k['d']}"})
    return sorted(evs, key=lambda e: e["when"])


def affected(e, holdings):
    tags = set(e.get("tags", []))
    hit = [h["n"] for c, h in holdings.items() if "all" in tags or tags & set(h.get("t", []))]
    return "、".join(hit)


def fmt_when(w):
    return f"{w.month}/{w.day}（{WEEK[w.weekday()]}）{w:%H:%M}"


def event_lines(e, holdings, detail=True):
    tag = "" if e.get("confirmed", True) else "（日期是推估，公司還沒公布）"
    lines = [f"• **{e['name']}**　台北 {fmt_when(e['when'])}{tag}"]
    if detail:
        if e.get("watch"):
            lines.append(f"　看什麼：{e['watch']}")
        hit = affected(e, holdings)
        if hit:
            lines.append(f"　你的持股：{hit}")
        if e.get("ref"):
            lines.append(f"　歷史參考：{e['ref']}")
    return lines


def yahoo_reaction(sym, e):
    """盤後公布：盤後最新價 vs 當天收盤；盤前公布：當天收盤 vs 前一天收盤。回傳 (漲跌%, 說明) 或 None。"""
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"range": "5d", "interval": "5m", "includePrePost": "true"}, headers=H, timeout=20)
    j = r.json()["chart"]["result"][0]
    q = j["indicators"]["quote"][0]["close"]
    pts = [(datetime.fromtimestamp(t, NY), c) for t, c in zip(j["timestamp"], q) if c]
    day = datetime.fromisoformat(e["ny_date"]).date()
    d = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"range": "1mo", "interval": "1d"}, headers=H, timeout=20).json()["chart"]["result"][0]
    daily = [(datetime.fromtimestamp(t, NY).date(), c) for t, c in zip(d["timestamp"], d["indicators"]["quote"][0]["close"]) if c]
    reg = [c for dd, c in daily if dd == day]
    prev = [c for dd, c in daily if dd < day]
    if not reg or not prev:
        return None
    close, prev_close = reg[-1], prev[-1]  # 官方日收盤（5 分 K 最後一根不等於收盤價）
    if e.get("timing") == "bmo":
        return (close / prev_close - 1) * 100, f"公布當天收盤 {close:,.2f}（前一天 {prev_close:,.2f}）"
    post = [(t, c) for t, c in pts if t.date() == day and t.hour >= 16]
    if not post:
        return None
    return (post[-1][1] / close - 1) * 100, f"盤後 {post[-1][1]:,.2f}（{post[-1][0]:%H:%M} 美東），收盤 {close:,.2f}"


def send(text, dry):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if dry or not url:
        # repo 公開、Actions 紀錄公開：有持股時不印內容，本機測試加 --show 才印
        print(text if ("--show" in sys.argv or not os.environ.get("HOLDINGS_JSON")) else "（未送出；內容含持股不印）")
        return
    requests.post(url, json={"content": text[:1990]}, timeout=20).raise_for_status()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"
    dry = "--dry" in sys.argv
    cfg, state = load(EVENTS_FILE, {"manual": [], "us": {}}), load(STATE_FILE, {})
    holdings = json.loads(os.environ.get("HOLDINGS_JSON") or "{}")
    now = datetime.now(TPE)
    evs = all_events(cfg, state)
    sent = state.setdefault("sent", {})

    if mode == "test":
        for e in evs:
            print(e["key"], fmt_when(e["when"]), "已確認" if e.get("confirmed", True) else "推估")
        print("持股設定：", "有" if holdings else "沒有")
    elif mode == "remind":
        soon = [e for e in evs if now < e["when"] <= now + timedelta(hours=24) and f"remind|{e['key']}" not in sent]
        if soon:
            lines = ["**【財報提醒】接下來 24 小時**"]
            for e in soon:
                lines += event_lines(e, holdings)
                sent[f"remind|{e['key']}"] = now.isoformat()
            lines.append("美股盤後公布的，隔天早上 07:30 會再發股價反應。")
            send("\n".join(lines), dry)
        if now.weekday() == 6 and state.get("week_sent") != now.date().isoformat():
            week = [e for e in evs if now < e["when"] <= now + timedelta(days=8)]
            lines = ["**【下週財報／法說】**"] + [l for e in week for l in event_lines(e, holdings, detail=False)]
            if not week:
                lines.append("沒有排定的重要財報。")
            send("\n".join(lines), dry)
            state["week_sent"] = now.date().isoformat()
    elif mode == "result":
        done = [e for e in evs if e.get("sym") and now - timedelta(hours=20) <= e["when"] < now
                and f"result|{e['key']}" not in sent]
        for e in done:
            try:
                got = yahoo_reaction(e["sym"], e)
            except Exception as ex:  # noqa: BLE001
                print(f"{e['sym']} 股價抓取失敗：{ex}")
                continue
            if not got:
                print(f"{e['sym']} 還沒有盤後資料")
                continue
            pct, how = got
            lines = [f"**【財報反應】{e['name']}　{pct:+.1f}%**", f"　{how}"]
            hit = affected(e, holdings)
            if hit:
                lines.append(f"　今天開盤會受影響的持股：{hit}")
            if e.get("ref"):
                lines.append(f"　歷史參考：{e['ref']}")
            lines.append("　影響大多在台股開盤一次反映，開盤後通常不會再跟著走（2022 年起的統計）。")
            send("\n".join(lines), dry)
            sent[f"result|{e['key']}"] = now.isoformat()

    cutoff = (now - timedelta(days=60)).isoformat()
    state["sent"] = {k: v for k, v in sent.items() if v >= cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
