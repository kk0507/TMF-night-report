# python earnings_alert.py remind|result|test [--dry]
# 財報／法說提醒，發到報價頻道：
#   remind（每天台北 20:30）：未來 24 小時內要公布的財報／法說 → 公布時間、要看什麼、影響到哪些持股；週日另發下週清單。
#   result（每天台北 07:30）：美股公司昨晚公布後的股價反應（盤後價），趕在台股 09:00 開盤前。
# 美股公司的日期每天從 Nasdaq 抓（公司公布＝已確認，否則是推估）；非美股（台積電、三星…）寫在 earnings_events.json。
# 持股只放在 GitHub secret HOLDINGS_JSON（repo 公開，程式和紀錄都不印持股）：{"代號": {"n": "名稱", "t": ["memory", ...]}}
# 持股自己的財報／法說（2026-10-15 加）：
#   法說會：公開資訊觀測站（mopsov）全市場這個月＋下個月的法人說明會一覽表，存成 tw_conferences.json（全市場，看不出持股）；
#           程式裡再挑出持股、只留「講剛結束那一季成績」或公司自辦的場次；已發送的紀錄用 HMAC 編碼（看不出是哪一檔）。
#           （poorstock 轉載站在 GitHub 機器上會 403，不用。）
#   財報公布：證交所／櫃買 openapi 的「最新一季累計損益」，全市場存成 tw_releases_latest.json，季別變了＝剛公布，
#           寫進 tw_releases.json（全市場最近 10 天的新公布，雲端財報整理排程讀它，自己對照戰情室持股）；持股有公布就發 Discord。
# 需要環境變數：DISCORD_WEBHOOK_URL、HOLDINGS_JSON（沒有就只印出、不列持股）
import hashlib
import hmac
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

TPE, NY = ZoneInfo("Asia/Taipei"), ZoneInfo("America/New_York")
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml", "Accept-Language": "zh-TW,zh;q=0.9"}
EVENTS_FILE, STATE_FILE = "earnings_events.json", "earnings_state.json"
REL_FILE, REL_LATEST, CONF_FILE = "tw_releases.json", "tw_releases_latest.json", "tw_conferences.json"
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


def secret_key(name):
    """持股相關的已發送紀錄：用 webhook 當 HMAC 金鑰，公開 repo 看不出是哪一檔。"""
    k = (os.environ.get("DISCORD_WEBHOOK_URL") or "local").encode()
    return "h:" + hmac.new(k, name.encode(), hashlib.sha256).hexdigest()[:16]


def results_call(when, desc):
    """這場法說是不是在講「剛結束那一季」的成績（或公司自辦）；券商論壇只講營運概況的不算。"""
    if "自辦" in desc or "召開法人說明會" in desc:
        return True
    rq = (when.month - 1) // 3 or 4  # 10～12 月講 Q3、1～3 月講去年 Q4
    qs = {"一": 1, "二": 2, "三": 3, "四": 4}
    found = {int(x) if x.isdigit() else qs[x] for x in re.findall(r"第\s*([1-4一二三四])\s*季", desc)}
    for a, b in re.findall(r"([1-4])Q\d\d|Q([1-4])", desc):
        found.add(int(a or b))
    return rq in found or (rq == 4 and "年度" in desc)


def fetch_conferences(now):
    """公開資訊觀測站「法人說明會一覽表」：全市場（上市＋上櫃）這個月和下個月。回傳 [{code,n,d,t,loc,desc}]。"""
    out = []
    months = {(now.year, now.month), ((now + timedelta(days=32)).year, (now + timedelta(days=32)).month)}
    for y, m in sorted(months):
        for kind in ("sii", "otc"):
            r = requests.post("https://mopsov.twse.com.tw/mops/web/ajax_t100sb02_1", headers=BROWSER, timeout=40,
                              data={"encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1, "TYPEK": kind,
                                    "year": y - 1911, "month": f"{m:02d}", "co_id": ""})
            r.raise_for_status()
            r.encoding = "utf-8"
            if "法人說明會" not in r.text:
                raise ValueError("回傳內容不是法說會表格")
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, flags=re.S):
                tds = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", td))).strip()
                       for td in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)]
                if len(tds) < 6 or not re.fullmatch(r"\d{4,6}", tds[0]):
                    continue
                dm = re.match(r"(\d{2,3})/(\d{2})/(\d{2})", tds[2])
                tm = re.match(r"(\d{1,2}):(\d{2})", tds[3])
                if not dm:
                    continue
                out.append({"code": tds[0], "n": tds[1], "d": f"{int(dm[1]) + 1911}-{dm[2]}-{dm[3]}",
                            "t": f"{int(tm[1]):02d}:{tm[2]}" if tm else "00:00", "loc": tds[4][:40], "desc": tds[5][:200]})
    return out


def update_conferences(now):
    """全市場法說會存成 tw_conferences.json（留最近 120 天＋未來；全市場資料，看不出持股）。回傳 (清單, 錯誤或 None)。"""
    old = load(CONF_FILE, {}).get("items", [])
    try:
        cur = fetch_conferences(now)
    except Exception as ex:  # noqa: BLE001
        return old, f"公開資訊觀測站法說會資料抓取失敗：{type(ex).__name__} {str(ex)[:60]}"
    keep = (now - timedelta(days=120)).date().isoformat()
    merged = {(x["code"], x["d"], x["t"]): x for x in old + cur if x["d"] >= keep}
    items = sorted(merged.values(), key=lambda x: (x["d"], x["t"], x["code"]))
    with open(CONF_FILE, "w", encoding="utf-8") as f:
        json.dump({"_說明": "公開資訊觀測站法人說明會一覽表（上市＋上櫃，全市場；d 是開始日期，台北時間）。", "asOf": now.isoformat(timespec="minutes"),
                   "items": items}, f, ensure_ascii=False, indent=0)
    return items, None


def holding_conferences(holdings, items, now):
    """持股的法說會（不印代號）：每檔每一季只留第一場講該季成績的（之後的券商場次通常是重講同一份財報）。"""
    evs = []
    for code, h in holdings.items():
        first = {}
        for x in items:
            if x["code"] != code:
                continue
            when = datetime.fromisoformat(f"{x['d']}T{x['t']}").replace(tzinfo=TPE)
            if not results_call(when, x["desc"]):
                continue
            rq = (when.month - 1) // 3 or 4
            q = (when.year - (rq == 4 and when.month <= 3), rq)
            if q not in first or when < first[q][0]:
                first[q] = (when, x)
        for when, x in first.values():
            if when >= now - timedelta(days=2):
                evs.append({"name": f"{h['n']} 法說會（{x['loc'][:20]}）", "when": when, "confirmed": True,
                            "watch": x["desc"][:120], "own": h["n"], "key": secret_key(f"{code}|{x['d']}|{x['t']}")})
    return evs


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def fetch_releases():
    """全市場（上市＋上櫃）最新一季的累計損益；營收、利益單位是千元。"""
    out = {}
    for x in requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap14_L", headers=H, timeout=60).json():
        out[x["公司代號"]] = {"n": x["公司名稱"], "q": f"{x['年度']}Q{x['季別']}", "eps": num(x["基本每股盈餘(元)"]),
                            "rev": num(x["營業收入"]), "op": num(x["營業利益"]), "ni": num(x["稅後淨利"])}
    for x in requests.get("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap14_O", headers=H, timeout=60).json():
        out[x["SecuritiesCompanyCode"]] = {"n": x["CompanyName"], "q": f"{x['Year']}Q{x['季別']}", "eps": num(x["基本每股盈餘"]),
                                           "rev": num(x["營業收入"]), "op": num(x["營業利益"]), "ni": num(x["稅後淨利"])}
    return out


def qkey(q):
    y, n = q.split("Q")
    return int(y), int(n)


def update_releases(now):
    """更新全市場快照，回傳 (這次新公布的清單, 錯誤訊息或 None)。第一次跑只建快照、不算新公布。"""
    latest = load(REL_LATEST, {}).get("latest", {})
    try:
        cur = fetch_releases()
    except Exception as ex:  # noqa: BLE001
        return [], f"證交所／櫃買財報資料抓取失敗：{ex}"
    if len(cur) < 1500:
        return [], f"證交所／櫃買財報資料只有 {len(cur)} 家，可能改版"
    fresh = []
    for code, x in cur.items():
        old = latest.get(code)
        if not old or qkey(x["q"]) <= qkey(old["q"]):
            if old and x["q"] == old["q"]:
                x["epsQ"] = old.get("epsQ")
            continue
        (y, n), (oy, on) = qkey(x["q"]), qkey(old["q"])
        item = {"code": code, "n": x["n"], "q": x["q"], "seen": now.isoformat(timespec="minutes"), "eps": x["eps"]}
        if n == 1 or (y == oy and n == on + 1):  # 單季＝累計相減（第 1 季就是累計本身）
            base = {"eps": 0, "rev": 0, "op": 0, "ni": 0} if n == 1 else old
            for k in ("eps", "rev", "op", "ni"):
                if x[k] is not None and base.get(k) is not None:
                    item[k + "Q"] = round(x[k] - base[k], 2)
            x["epsQ"] = item.get("epsQ")
        item["prevEpsQ"] = old.get("epsQ")
        fresh.append(item)
    if not latest:
        fresh = []
    rel = load(REL_FILE, {"new": []})
    cutoff = (now - timedelta(days=10)).isoformat(timespec="minutes")
    rel = {"_說明": "全市場（上市＋上櫃）最近 10 天新公布的季報（證交所 t187ap14_L、櫃買 mopsfin_t187ap14_O；seen＝程式第一次看到的台北時間，"
                    "eps＝年初累計，epsQ／revQ／opQ／niQ＝單季（營收與利益單位千元），prevEpsQ＝上一季單季）。",
           "asOf": now.isoformat(timespec="minutes"),
           "new": [i for i in rel.get("new", []) if i["seen"] >= cutoff] + fresh}
    with open(REL_FILE, "w", encoding="utf-8") as f:
        json.dump(rel, f, ensure_ascii=False, indent=1)
    with open(REL_LATEST, "w", encoding="utf-8") as f:
        json.dump({"asOf": now.isoformat(timespec="minutes"), "latest": {**latest, **cur}}, f, ensure_ascii=False, separators=(",", ":"))
    return fresh, None


def expected_q(now):
    """法定期限過了之後，每家公司至少該有的季別（一般業：Q1 5/15、Q2 8/14、Q3 11/14、年報 3/31）。"""
    y, md = now.year - 1911, (now.month, now.day)
    if md > (11, 14):
        return f"{y}Q3"
    if md > (8, 14):
        return f"{y}Q2"
    if md > (5, 15):
        return f"{y}Q1"
    return f"{y - 1}Q4" if md > (3, 31) else f"{y - 1}Q3"


def release_lines(item):
    y, n = qkey(item["q"])
    lines = [f"**【持股財報公布】{item['n']}　{y} 年第 {n} 季**"]
    if item.get("epsQ") is not None:
        prev = f"（上一季 {item['prevEpsQ']:g}）" if item.get("prevEpsQ") is not None else ""
        lines.append(f"　單季 EPS {item['epsQ']:g}{prev}；年初累計 {item['eps']:g}")
    else:
        lines.append(f"　年初累計 EPS {item['eps']:g}")
    if item.get("revQ"):
        op = f"、營益率 {item['opQ'] / item['revQ'] * 100:.1f}%" if item.get("opQ") is not None else ""
        lines.append(f"　單季營收 {item['revQ'] / 1e5:,.1f} 億{op}")
    lines.append("　數字是公司申報的合併報表（證交所／櫃買）；戰情室的財報整理由早上 07:30 的排程寫。")
    return lines


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
    if e.get("own"):
        return e["own"]
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


def regular_session(sym, e):
    """盤後公布的財報：隔天美股正式交易收盤 vs 公布前收盤。回傳 (漲跌%, 說明) 或 None（還沒收盤）。"""
    d = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"range": "1mo", "interval": "1d"}, headers=H, timeout=20).json()["chart"]["result"][0]
    daily = [(datetime.fromtimestamp(t, NY).date(), c) for t, c in zip(d["timestamp"], d["indicators"]["quote"][0]["close"]) if c]
    day = datetime.fromisoformat(e["ny_date"]).date()
    before = [c for dd, c in daily if dd <= day]
    after = [(dd, c) for dd, c in daily if dd > day]
    if not before or not after or datetime.now(NY) < datetime.combine(after[0][0], datetime.min.time(), NY).replace(hour=16, minute=5):
        return None
    return (after[0][1] / before[-1] - 1) * 100, f"{after[0][0].month}/{after[0][0].day} 收盤 {after[0][1]:,.2f}（公布前 {before[-1]:,.2f}）"


FOLLOW = ("財報後的節奏（2022 年起美光、輝達、Vertiv、SanDisk 共 53 次）：美股本身隔天開盤就反映約 86%，之後沒有固定方向；"
          "台股相關股慢一拍——第 1 天開盤只反映一部分，第 2 天（美股正式交易之後）才跟上，大約第 5 天後就沒有固定方向。")


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
    items, conf_err = update_conferences(now)
    own = holding_conferences(holdings, items, now)
    evs = sorted(evs + own, key=lambda e: e["when"])
    sent = state.setdefault("sent", {})
    show = "--show" in sys.argv or not os.environ.get("HOLDINGS_JSON")
    warns = []
    stocks = [c for c in holdings if not c.startswith("00")]
    if conf_err:
        warns.append(conf_err)
    fresh, err = update_releases(now)
    if err:
        warns.append(err)
    for item in fresh:
        k = secret_key(f"rel|{item['code']}|{item['q']}")
        if item["code"] in holdings and k not in sent:
            send("\n".join(release_lines({**item, "n": holdings[item["code"]]["n"]})), dry)
            sent[k] = now.isoformat()
    exp = expected_q(now)
    latest = load(REL_LATEST, {}).get("latest", {})
    late = [holdings[c]["n"] for c in stocks if latest and (c not in latest or qkey(latest[c]["q"]) < qkey(exp))]
    if late and secret_key(f"late|{exp}") not in sent and mode != "test":
        warns.append(f"法定期限過了，這幾檔在證交所／櫃買資料裡還沒有 {exp}：{'、'.join(late)}（可能是資料源沒更新，偵測可能失靈）")
        sent[secret_key(f"late|{exp}")] = now.isoformat()
    if warns and mode != "test" and f"warn|{now.date()}" not in sent:
        send("**【財報提醒程式】要注意**\n" + "\n".join(f"• {w}" for w in warns), dry)
        sent[f"warn|{now.date()}"] = now.isoformat()

    if mode == "test":
        for e in evs:
            label = e["key"] if show or not e.get("own") else "（持股法說，不印）"
            print(label, fmt_when(e["when"]), "已確認" if e.get("confirmed", True) else "推估")
        print("持股設定：", "有" if holdings else "沒有", "；持股法說", len(own), "場；全市場法說", len(items), "筆")
        print("財報快照：新公布", len(fresh), "家；警告", len(warns), "則")
        if show:
            for w in warns:
                print(" ", w)
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
                lines.append(f"　下一個台股開盤會受影響的持股：{hit}")
            if e.get("ref"):
                lines.append(f"　歷史參考：{e['ref']}")
            if e.get("timing") != "bmo":
                lines.append("　這是盤後價，美股今晚正式交易後可能再變，明天早上 07:30 會再報一次收盤反應。")
            lines.append(f"　{FOLLOW}")
            send("\n".join(lines), dry)
            sent[f"result|{e['key']}"] = now.isoformat()
        # 盤後公布的：隔天美股正式交易收盤後再報一次（台股第 2 天才會跟上這一段）
        later = [e for e in evs if e.get("sym") and e.get("timing") != "bmo" and now - timedelta(hours=48) <= e["when"] < now - timedelta(hours=20)
                 and f"result|{e['key']}" in sent and f"result2|{e['key']}" not in sent]
        for e in later:
            try:
                got = regular_session(e["sym"], e)
            except Exception as ex:  # noqa: BLE001
                print(f"{e['sym']} 收盤抓取失敗：{ex}")
                continue
            if not got:
                continue
            pct, how = got
            lines = [f"**【財報反應・第 2 天】{e['name']}　正式交易收盤 {pct:+.1f}%**", f"　{how}"]
            hit = affected(e, holdings)
            if hit:
                lines.append(f"　下一個台股交易日會跟上這一段的持股：{hit}")
            send("\n".join(lines), dry)
            sent[f"result2|{e['key']}"] = now.isoformat()

    cutoff = (now - timedelta(days=60)).isoformat()
    state["sent"] = {k: v for k, v in sent.items() if v >= cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
