# python timed_reminder.py -> 定時提醒：reminders.json 裡時間到、還沒發過的提醒，各發一次到報價頻道（附微台現價與關鍵價位距離）
# 需要環境變數：DISCORD_WEBHOOK_URL（沒有就只印出來）
"""跟著 price_alert.yml 每 5 分鐘跑，GitHub 排程晚到也只是晚幾分鐘，不會漏發；發過就在 reminders.json 標 sent。
repo 是公開的：提醒文字不要寫口數、均價、損益，只放價位與要做的決定。
reminders.json 格式：[{"id", "at": "2026-09-30T18:30:00+08:00", "title", "text", "levels": [{"name", "price"}], "sent": false}]"""
import json
import os
from datetime import datetime

import requests

from taifex_quote import TAIPEI, get_live_price

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")


def main(now=None):
    now = now or datetime.now(TAIPEI)
    try:
        with open(FILE, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return
    due = [r for r in items if not r.get("sent") and datetime.fromisoformat(r["at"]) <= now]
    if not due:
        return
    q = get_live_price()
    for r in due:
        lines = [f"⏰ **【提醒】{r['title']}**", r["text"]]
        if q:
            lines.append(f"微台現價 {q['price']:,.0f}（{q['diff']:+,.0f}），{q['ts']:%H:%M}")
            for lv in r.get("levels", []):
                d = lv["price"] - q["price"]
                lines.append(f"・{lv['name']} {lv['price']:,.0f}（{'要漲' if d > 0 else '要跌'} {abs(d):,.0f} 點）")
        else:
            lines.append("（現在抓不到微台即時價）")
        msg = "\n".join(lines)
        url = os.environ.get("DISCORD_WEBHOOK_URL")
        if url:
            requests.post(url, json={"content": msg}, timeout=15).raise_for_status()
        print(msg)
        r["sent"] = True
        r["sentAt"] = now.isoformat(timespec="minutes")
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
