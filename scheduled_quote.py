# python scheduled_quote.py <day_close|night_check|night_close> -> 手動送一則簡單報價到Discord
# 需要環境變數：DISCORD_WEBHOOK_URL
"""固定時間點報價：日盤收盤、夜盤20:50、夜盤收盤。

跟night_session_report.py(每天07:00的完整分析快報)是分開的，這支只單純報價，
不算百分位/均線乖離那些。

正式排程已經併進price_alert.py那個每5分鐘跑一次的常駐排程裡自我修復(見該檔案
的check_scheduled_quotes)，這支檔案保留給KK手動測試/補發用，另外把
build_message()共用給price_alert.py。
"""
import os
import sys

import requests

from taifex_quote import get_live_price, sign_emoji

LABELS = {
    "day_close": "日盤收盤",
    "night_check": "夜盤20:50",
    "night_close": "夜盤收盤",
}


def build_message(key, session):
    label = LABELS[key]
    lines = [f"**微台(TMF) {label}**  {session['ts'].strftime('%Y-%m-%d %H:%M:%S')}　合約{session['contract']}"]
    lines.append(
        f"{sign_emoji(session['diff'])} 價格: {session['price']:,.0f}　"
        f"{session['diff']:+,.0f}（{session['diff_rate']:+.2f}%）"
    )
    if session["open"] and session["high"] and session["low"]:
        lines.append(f"開:{session['open']:,.0f}　高:{session['high']:,.0f}　低:{session['low']:,.0f}")
    return "\n".join(lines)


def send_discord(text):
    webhook = os.environ["DISCORD_WEBHOOK_URL"]
    resp = requests.post(webhook, json={"content": text}, timeout=15)
    resp.raise_for_status()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in LABELS:
        raise SystemExit(f"用法: python scheduled_quote.py <{'|'.join(LABELS)}>")
    key = sys.argv[1]

    live = get_live_price()
    if live is None:
        print("現在不在盤中(或資料太舊)，跳過")
        return

    send_discord(build_message(key, live))
    print(f"已送出「{LABELS[key]}」報價")


if __name__ == "__main__":
    main()
