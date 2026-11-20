# 每盤收盤後把微台(TMF)近月的1分K存成CSV，累積起來給之後回測用。
# 期交所MIS(getChartData1M)只給「當盤」的1分K，歷史分K拿不到(FinMind要付費)，
# 所以只能自己每盤存。實測收盤後資料還會留十幾個小時(日盤13:45收，隔天03:50
# 還抓得到完整300根)，掛在每5分鐘的price_alert.yml裡，GitHub排程延遲也來得及。
#
# 存法：minute_k/TMF_YYYYMMDD_day.csv、minute_k/TMF_YYYYMMDD_night.csv
#   YYYYMMDD＝盤開始那天(夜盤跨夜也用開盤那天，例如11/20 15:00～11/21 05:00 存成20261120_night)
#   欄位：time(那根K棒的結束時間，台北)、open、high、low、close、volume、contract
# 只在那一盤收完才寫(盤中不寫，避免每5分鐘提交一次)；檔案已存在且根數一樣就不動。
import csv
import os
from datetime import datetime, timedelta

import requests

import taifex_quote as tq

OUT_DIR = "minute_k"
SESSIONS = {"0": "day", "1": "night"}
FIELDS = ["time", "open", "high", "low", "close", "volume", "contract"]


def session_end(market_type, cdate):
    if market_type == "0":
        return cdate.replace(hour=13, minute=45)
    return (cdate + timedelta(days=1)).replace(hour=5, minute=0)


def fetch_bars(market_type):
    s = tq.fetch_session(market_type)
    if not s:
        return None, None, None
    resp = requests.post(tq.MIS_CHART_URL, json={"SymbolID": s["contract"]}, timeout=15)
    resp.raise_for_status()
    data = resp.json()["RtData"]
    cdate = datetime.strptime(data["Quote"]["CDate"], "%Y%m%d").replace(tzinfo=tq.TAIPEI)
    bars = []
    for t in data["Ticks"]:
        ts = cdate.replace(hour=int(t[0][:2]), minute=int(t[0][2:4]))
        if market_type == "1" and ts.hour < 12:
            ts += timedelta(days=1)
        bars.append({
            "time": ts.strftime("%Y-%m-%d %H:%M"),
            "open": t[1], "high": t[2], "low": t[3], "close": t[4], "volume": t[5],
            "contract": s["contract"],
        })
    return cdate, s["contract"], bars


def existing_rows(path):
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return max(sum(1 for _ in f) - 1, 0)


def archive(market_type, now):
    cdate, contract, bars = fetch_bars(market_type)
    if not bars:
        print(f"{SESSIONS[market_type]}：沒有資料")
        return
    end = session_end(market_type, cdate)
    if now < end + timedelta(minutes=3):
        print(f"{SESSIONS[market_type]} {cdate:%Y%m%d}：還沒收盤（{end:%m-%d %H:%M} 收），先不存")
        return
    path = os.path.join(OUT_DIR, f"TMF_{cdate:%Y%m%d}_{SESSIONS[market_type]}.csv")
    if existing_rows(path) >= len(bars):
        print(f"{path}：已經存過（{len(bars)} 根）")
        return
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(bars)
    print(f"{path}：存了 {len(bars)} 根（{contract}，{bars[0]['time']}～{bars[-1]['time']}）")


def main():
    now = datetime.now(tq.TAIPEI)
    for mt in SESSIONS:
        try:
            archive(mt, now)
        except Exception as e:  # 一盤失敗不影響另一盤
            print(f"{SESSIONS[mt]}：失敗 {e!r}")


if __name__ == "__main__":
    main()
