# python price_alert.py -> 每5分鐘跑一次(見price_alert.yml)，做兩件事：
#   1. 比對 alerts.json 裡的目標價，價格穿越就送Discord通知
#   2. 自我修復版的定時報價(day_close/night_check/night_close)，見check_scheduled_quotes()
# 需要環境變數：DISCORD_WEBHOOK_URL
"""微台(TMF)價格穿越通知 ＋ 定時報價自我修復。

原本day_close/night_check/night_close三個定時報價各自獨立排程一天只跑一次，
GitHub Actions的schedule觸發只保證「不早於」不保證準時，實測發現20:50那次被
delay到完全沒觸發過。改法：併進這支已經在跑、頻率高很多的排程裡，用
quote_checkpoints.json記錄「今天這幾個時間點送過了沒」，只要現在時間已經過了
目標時間點、今天還沒送過，下一次這支腳本被執行到就會自動補送，不用等人發現。

day_close/night_close因為是查「那個session收盤當下凍結的資料」，不是查
「現在活著的報價」，所以直接指定session(0=日盤/1=夜盤)去查，收盤後資料還在、
不會因為過了get_live_price()的10~20分鐘新鮮度門檻就查不到，追上的時間窗很寬
(日盤收盤後到隔天日盤開盤前都查得到)。night_check是查盤中活的報價，維持用
get_live_price()。
"""
import json
import os
from datetime import datetime, time as dtime

import requests

from taifex_quote import TAIPEI, fetch_session, get_live_price
from scheduled_quote import build_message

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
ALERTS_FILE = os.path.join(os.path.dirname(__file__), "alerts.json")
CHECKPOINTS_FILE = os.path.join(os.path.dirname(__file__), "quote_checkpoints.json")

CHECKPOINT_TIMES = {
    "day_close": dtime(13, 45),
    "night_check": dtime(20, 50),
    "night_close": dtime(5, 0),
}


def send_discord(text):
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=15)
    resp.raise_for_status()


def side(price, target):
    return 1 if price >= target else -1


def check_price_alerts():
    with open(ALERTS_FILE, "r", encoding="utf-8") as f:
        alerts = json.load(f)

    active = [a for a in alerts if not a.get("fired")]
    if not active:
        print("沒有待觸發的價格通知")
        return

    live = get_live_price()
    if live is None:
        print("[價格通知] 現在不在盤中(或資料太舊)，跳過")
        return

    changed = False
    for a in alerts:
        if a.get("fired"):
            continue
        cur_side = side(live["price"], a["price"])
        if cur_side != a["last_side"]:
            direction = "向上穿越" if cur_side == 1 else "向下穿越"
            note = f"（{a['note']}）" if a.get("note") else ""
            send_discord(
                f"**微台(TMF)價格通知**\n"
                f"{direction} {a['price']:,.0f} {note}\n"
                f"現價: {live['price']:,.0f}　合約{live['contract']}　{live['ts'].strftime('%Y-%m-%d %H:%M:%S')}"
            )
            a["fired"] = True
            a["fired_at"] = live["ts"].isoformat()
            a["fired_price"] = live["price"]
            changed = True
            print(f"觸發: {a['id']} @ {a['price']}")
        else:
            a["last_side"] = cur_side
            changed = True

    if changed:
        with open(ALERTS_FILE, "w", encoding="utf-8") as f:
            json.dump(alerts, f, ensure_ascii=False, indent=2)
            f.write("\n")


def load_checkpoints(today):
    try:
        with open(CHECKPOINTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    if data.get("date") != today:
        data = {"date": today, "day_close": False, "night_check": False, "night_close": False}
    return data


def save_checkpoints(data):
    with open(CHECKPOINTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def check_scheduled_quotes():
    now = datetime.now(TAIPEI)
    today = now.date().isoformat()
    cp = load_checkpoints(today)
    changed = False

    if not cp["day_close"] and now.time() >= CHECKPOINT_TIMES["day_close"]:
        day = fetch_session("0")
        if day and day["ts"].date().isoformat() == today:
            send_discord(build_message("day_close", day))
            cp["day_close"] = True
            changed = True
            print("補送: day_close")

    if not cp["night_check"] and now.time() >= CHECKPOINT_TIMES["night_check"]:
        live = get_live_price()
        if live:
            send_discord(build_message("night_check", live))
            cp["night_check"] = True
            changed = True
            print("補送: night_check")

    if not cp["night_close"] and now.time() >= CHECKPOINT_TIMES["night_close"]:
        night = fetch_session("1")
        # 時間也要卡在清晨(<09:00)，不然晚上新的夜盤一開盤，同一個日期查到的會是
        # 今晚的即時報價，誤標成「夜盤收盤」——一旦錯過這個清晨窗口就是真的錯過，
        # 不會補送錯的資料，等明天日期換掉重新開始判斷
        if night and night["ts"].date().isoformat() == today and night["ts"].time() < dtime(9, 0):
            send_discord(build_message("night_close", night))
            cp["night_close"] = True
            changed = True
            print("補送: night_close")

    if changed:
        save_checkpoints(cp)


def main():
    check_price_alerts()
    check_scheduled_quotes()


if __name__ == "__main__":
    main()
