# python night_session_report.py -> 送一則微台夜盤快報到 Telegram，不會產生其他檔案
# 需要環境變數：FINMIND_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""微台(TMF)夜盤快報。

改寫自 stock-strategies-only 的 premarket 夜盤快報，把原本固定門檻分五級的判斷，
換成跟歷史資料算百分位的方法，並加入均線乖離、量能趨勢、外資期貨籌碼三項。
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta

import requests

FINMIND_TOKEN = os.environ["FINMIND_TOKEN"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FUTURES_ID = "TMF"
LOOKBACK_DAYS = 400


def finmind_get(dataset, data_id, start_date, end_date):
    resp = requests.get(
        FINMIND_URL,
        params={
            "dataset": dataset,
            "data_id": data_id,
            "start_date": start_date,
            "end_date": end_date,
        },
        headers={"Authorization": f"Bearer {FINMIND_TOKEN}"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind error: {payload.get('msg')}")
    return payload["data"]


def build_full_session_series(data_id, lookback_days):
    """近月連續全盤(日盤+夜盤合併)序列，跟這幾天分析用的同一套邏輯。"""
    end = datetime.utcnow() + timedelta(hours=8)
    start = end - timedelta(days=lookback_days)
    rows = finmind_get(
        "TaiwanFuturesDaily", data_id, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    )

    groups = defaultdict(dict)
    for row in rows:
        groups[(row["date"], row["contract_date"])][row["trading_session"]] = row

    by_date = defaultdict(list)
    for (date, contract), sessions in groups.items():
        vol = sum(s["volume"] for s in sessions.values())
        by_date[date].append((vol, contract, sessions))

    series = []
    for date in sorted(by_date.keys()):
        _, contract, sessions = max(by_date[date], key=lambda x: x[0])
        pos = sessions.get("position")
        am = sessions.get("after_market")
        if not pos and not am:
            continue
        highs = [s["max"] for s in (pos, am) if s]
        lows = [s["min"] for s in (pos, am) if s]
        series.append(
            {
                "date": date,
                "contract": contract,
                "close": pos["close"] if pos else am["close"],
                "high": max(highs),
                "low": min(lows),
                "volume": (pos["volume"] if pos else 0) + (am["volume"] if am else 0),
                "night_open": am["open"] if am else None,
                "night_close": am["close"] if am else None,
            }
        )
    return series


def percentile_rank(value, population):
    if not population:
        return None
    below = sum(1 for v in population if v <= value)
    return below / len(population) * 100


def rolling_return(closes, n):
    return [(closes[i] - closes[i - n]) / closes[i - n] * 100 for i in range(n, len(closes))]


def moving_average(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def get_institutional_signal(days=10):
    """外資期貨(TX)淨未平倉部位——微台/小台沒有單獨統計，業界慣例用TX當台指期籌碼指標。"""
    end = datetime.utcnow() + timedelta(hours=8)
    start = end - timedelta(days=days)
    rows = finmind_get(
        "TaiwanFuturesInstitutionalInvestors", "TX", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    )
    foreign = sorted(
        (r for r in rows if r["institutional_investors"] == "外資"), key=lambda r: r["date"]
    )
    if len(foreign) < 2:
        return None
    latest, prev = foreign[-1], foreign[-2]
    net_latest = latest["long_open_interest_balance_volume"] - latest["short_open_interest_balance_volume"]
    net_prev = prev["long_open_interest_balance_volume"] - prev["short_open_interest_balance_volume"]
    return {"date": latest["date"], "net_position": net_latest, "change": net_latest - net_prev}


def send_discord(text):
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=15)
    resp.raise_for_status()


def main():
    series = build_full_session_series(FUTURES_ID, LOOKBACK_DAYS)
    if len(series) < 30:
        raise RuntimeError(f"歷史資料太少({len(series)}筆)，無法計算百分位，檢查FinMind回應內容")

    closes = [d["close"] for d in series]
    latest = series[-1]

    night_ret = None
    if latest["night_open"] and latest["night_close"]:
        night_ret = (latest["night_close"] - latest["night_open"]) / latest["night_open"] * 100

    ret3, ret5 = rolling_return(closes, 3), rolling_return(closes, 5)
    cur_ret3 = ret3[-1] if ret3 else None
    cur_ret5 = ret5[-1] if ret5 else None
    pct3 = percentile_rank(cur_ret3, ret3[:-1]) if cur_ret3 is not None else None
    pct5 = percentile_rank(cur_ret5, ret5[:-1]) if cur_ret5 is not None else None

    ma10, ma20 = moving_average(closes, 10), moving_average(closes, 20)
    dev10 = (closes[-1] - ma10) / ma10 * 100 if ma10 else None
    dev20 = (closes[-1] - ma20) / ma20 * 100 if ma20 else None

    vols = [d["volume"] for d in series]
    vol_recent = sum(vols[-3:]) / 3 if len(vols) >= 3 else None
    vol_prior = sum(vols[-6:-3]) / 3 if len(vols) >= 6 else None
    if vol_recent and vol_prior:
        vol_trend = "放大" if vol_recent > vol_prior else "縮小"
    else:
        vol_trend = "資料不足"

    try:
        chip = get_institutional_signal()
    except Exception:
        chip = None

    lines = [f"**微台(TMF)夜盤快報**  {latest['date']}  合約{latest['contract']}"]
    lines.append(f"收盤: {closes[-1]:,.0f}")
    if night_ret is not None:
        lines.append(f"昨晚夜盤: {night_ret:+.2f}%")
    if cur_ret3 is not None and pct3 is not None:
        lines.append(f"近3日累計: {cur_ret3:+.2f}%（歷史第{pct3:.0f}百分位）")
    if cur_ret5 is not None and pct5 is not None:
        lines.append(f"近5日累計: {cur_ret5:+.2f}%（歷史第{pct5:.0f}百分位）")
    if dev10 is not None:
        lines.append(f"乖離10日均線: {dev10:+.2f}%　乖離20日均線: {dev20:+.2f}%")
    lines.append(f"近3日量能: {vol_trend}")
    if chip:
        sign = "淨多" if chip["net_position"] > 0 else "淨空"
        change_word = "增加" if chip["change"] > 0 else "減少"
        lines.append(
            f"外資期貨(TX): {sign}{abs(chip['net_position']):,.0f}口"
            f"（較前一交易日{change_word}{abs(chip['change']):,.0f}口，{chip['date']}）"
        )
    lines.append("")
    lines.append("這是資料整理，不是買賣建議，方向判斷請自行決定。")

    send_discord("\n".join(lines))
    print("已送出Discord報告")


if __name__ == "__main__":
    main()
