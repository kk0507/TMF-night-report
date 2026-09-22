# 共用模組：查微台(TMF)近月即時報價，price_alert.py跟scheduled_quote.py都用這個
from datetime import datetime, timedelta, timezone

import requests

MIS_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
TAIPEI = timezone(timedelta(hours=8))
STALE_MINUTES = 20  # 資料時間戳跟現在差超過這個，視為盤已休息
# 原本設10分鐘，實測發現GitHub Actions排程常常晚個10幾分鐘才真的執行(這是GitHub
# 已知的行為，schedule時間只是「最早」不是「保證準時」)，導致夜盤05:00收盤的報價
# 跑到21:17(UTC，該21:05跑)時已經有17分鐘沒更新，被誤判成沒開盤而跳過通知。
# 放寬到20分鐘，蓋過排程延遲，又不會蓋到最短的午休空檔(1小時15分)。


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
    return {
        "price": float(near["CLastPrice"]),
        "contract": near["SymbolID"],
        "open": float(near["COpenPrice"]) if near["COpenPrice"] else None,
        "high": float(near["CHighPrice"]) if near["CHighPrice"] else None,
        "low": float(near["CLowPrice"]) if near["CLowPrice"] else None,
        "diff": float(near["CDiff"]) if near["CDiff"] else 0.0,
        "diff_rate": float(near["CDiffRate"]) if near["CDiffRate"] else 0.0,
        "ts": ts,
    }


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


def sign_emoji(value):
    """台股慣例：漲紅跌綠。用emoji而不是ANSI/diff色塊，因為手機版Discord兩者都顯示不正常。"""
    if value is None:
        return ""
    if value > 0:
        return "🔴"
    if value < 0:
        return "🟢"
    return "⚪"
