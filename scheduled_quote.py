# python scheduled_quote.py <day_close|night_check|night_close> -> 送一則簡單報價到Discord
# 需要環境變數：DISCORD_WEBHOOK_URL
"""固定時間點報價通知：日盤收盤、夜盤20:50、夜盤收盤。

跟night_session_report.py(每天07:00的完整分析快報)是分開的，這支只單純報價，
不算百分位/均線乖離那些，純粹讓KK在這幾個時間點知道現在多少錢，不用自己開口問。
"""
import os
import sys

import requests

from taifex_quote import get_live_price, sign_emoji

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

LABELS = {
    "day_close": "日盤收盤",
    "night_check": "夜盤20:50",
    "night_close": "夜盤收盤",
}


def send_discord(text):
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=15)
    resp.raise_for_status()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in LABELS:
        raise SystemExit(f"用法: python scheduled_quote.py <{'|'.join(LABELS)}>")
    label = LABELS[sys.argv[1]]

    live = get_live_price()
    if live is None:
        print("現在不在盤中(或資料太舊)，跳過")
        return

    lines = [f"**微台(TMF) {label}**  {live['ts'].strftime('%Y-%m-%d %H:%M:%S')}　合約{live['contract']}"]
    lines.append(
        f"{sign_emoji(live['diff'])} 價格: {live['price']:,.0f}　"
        f"{live['diff']:+,.0f}（{live['diff_rate']:+.2f}%）"
    )
    if live["open"] and live["high"] and live["low"]:
        lines.append(f"開:{live['open']:,.0f}　高:{live['high']:,.0f}　低:{live['low']:,.0f}")

    send_discord("\n".join(lines))
    print(f"已送出「{label}」報價")


if __name__ == "__main__":
    main()
