# python price_alert.py -> 查一次微台(TMF)近月即時價，比對 alerts.json 裡的目標價，
# 價格從目標價其中一側穿越到另一側就送Discord通知，並把該筆標記成已觸發（不會重複發）。
# 需要環境變數：DISCORD_WEBHOOK_URL
"""微台(TMF)價格穿越通知。

每次執行都是無狀態查一次即時報價；「有沒有觸發」的狀態存在 alerts.json 裡，
執行完如果有變動，交給GitHub Actions那邊的workflow負責commit回repo。
"""
import json
import os
from datetime import datetime, timedelta, timezone

import requests

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
MIS_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
ALERTS_FILE = os.path.join(os.path.dirname(__file__), "alerts.json")
TAIPEI = timezone(timedelta(hours=8))
STALE_MINUTES = 10  # 資料時間戳跟現在差超過這個，視為盤已休息，不判斷


def fetch_session(market_type):
    resp = requests.post(
        MIS_URL,
        json={
            "MarketType": market_type,
            "SymbolType": "F",
            "KindID": "1",
            "CID": "TMF",
            "ExpireMonth": "",
            "rowSize": "全部",
            "PageNo": "",
            "SortColumn": "",
            "AscDesc": "A",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()["RtData"]["QuoteList"]
    contracts = [q for q in data if q["SymbolID"].endswith(("-F", "-M")) and q["CTotalVolume"]]
    if not contracts:
        return None
    near = max(contracts, key=lambda q: int(q["CTotalVolume"]))
    if not near["CLastPrice"] or not near["CTime"]:
        return None
    ts = datetime.strptime(near["CDate"] + near["CTime"], "%Y%m%d%H%M%S").replace(tzinfo=TAIPEI)
    return {"price": float(near["CLastPrice"]), "contract": near["SymbolID"], "ts": ts}


def get_live_price():
    """兩個session都查，取時間戳比較新的那個當現在的價格，太舊代表現在沒開盤。"""
    candidates = [s for s in (fetch_session("0"), fetch_session("1")) if s]
    if not candidates:
        return None
    latest = max(candidates, key=lambda s: s["ts"])
    now = datetime.now(TAIPEI)
    if now - latest["ts"] > timedelta(minutes=STALE_MINUTES):
        return None
    return latest


def send_discord(text):
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=15)
    resp.raise_for_status()


def side(price, target):
    return 1 if price >= target else -1


def main():
    with open(ALERTS_FILE, "r", encoding="utf-8") as f:
        alerts = json.load(f)

    active = [a for a in alerts if not a.get("fired")]
    if not active:
        print("沒有待觸發的通知")
        return

    live = get_live_price()
    if live is None:
        print("現在不在盤中(或資料太舊)，跳過")
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


if __name__ == "__main__":
    main()
