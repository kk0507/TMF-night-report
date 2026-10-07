# KK 2026-10-08：回測過去 Fed 會議紀錄公布對台指夜盤的影響
# 公布時間＝美東 14:00（台北隔天 02:00，冬令 03:00），落在「美國日期 D 當晚」的台指夜盤裡。
# 用法：python fomc_minutes.py   （快取放 %TEMP%/bigup，沿用 big_up_after 的 TX 連續序列）
import os, sys, json, re, statistics as st, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

OUT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, OUT)
CACHE = os.path.join(os.environ["TEMP"], "bigup")
sys.argv = [sys.argv[0], CACHE]
import big_up_after as b

H = {"User-Agent": "Mozilla/5.0"}
NY = ZoneInfo("America/New_York")


def minutes_dates():
    p = os.path.join(CACHE, "fomc_minutes_dates.json")
    if os.path.exists(p):
        return json.load(open(p))
    urls = ["https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"] + [
        f"https://www.federalreserve.gov/monetarypolicy/fomchistorical{y}.htm" for y in range(2017, 2021)]
    ds = set()
    for u in urls:
        t = requests.get(u, headers=H, timeout=30).text
        for m in re.findall(r"Released ([A-Z][a-z]+ \d{1,2}, \d{4})", t):
            ds.add(datetime.strptime(m, "%B %d, %Y").strftime("%Y-%m-%d"))
    ds = sorted(ds)
    json.dump(ds, open(p, "w"))
    return ds


def yahoo_daily(sym):
    p = os.path.join(CACHE, "yd_" + sym.replace("^", "").replace("=", "_") + ".json")
    if os.path.exists(p):
        return json.load(open(p))
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/" + sym,
                     params={"interval": "1d", "period1": 1483228800, "period2": 1791500000},
                     headers=H, timeout=30).json()["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    rows = [[datetime.fromtimestamp(t, NY).strftime("%Y-%m-%d"), c] for t, c in zip(r["timestamp"], q["close"]) if c]
    json.dump(rows, open(p, "w"))
    return rows


def yahoo_hourly(sym):
    p = os.path.join(CACHE, "yh_" + sym.replace("^", "").replace("=", "_") + ".json")
    if os.path.exists(p):
        return json.load(open(p))
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/" + sym,
                     params={"interval": "60m", "range": "730d"}, headers=H, timeout=60).json()["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    rows = [[t, o, h, l, c] for t, o, h, l, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]) if o and c]
    json.dump(rows, open(p, "w"))
    return rows


def line(tag, xs, rng=None):
    if not xs:
        return f"{tag:<28s} n=0"
    up = sum(1 for x in xs if x > 0) / len(xs) * 100
    big = sum(1 for x in xs if abs(x) >= 1) / len(xs) * 100
    s = (f"{tag:<28s} n={len(xs):4d} 平均{st.mean(xs):+.2f}% 中位{st.median(xs):+.2f}% 上漲{up:3.0f}% "
         f"平均絕對值{st.mean(abs(x) for x in xs):.2f}% |漲跌|≥1%占{big:3.0f}% 最差{min(xs):+.2f}% 最好{max(xs):+.2f}%")
    if rng:
        s += f" 振幅中位{st.median(rng):.2f}%"
    return s


MD = minutes_dates()
print("會議紀錄公布日", len(MD), MD[0], "~", MD[-1])

tx = b.tx_sessions()
taiex = b.fm("TaiwanStockPrice", "TAIEX", "2005-01-01", "TAIEX_2005")
tdates = sorted({r["date"] for r in taiex if r["close"]})
nxt = {tdates[i]: tdates[i + 1] for i in range(len(tdates) - 1)}
# T 的夜盤＝前一個交易日 D 15:00 開、T 05:00 收
N = {}
for i in range(2, len(tx)):
    if tx[i]["sess"] == "D" and tx[i - 1]["sess"] == "N" and tx[i - 2]["sess"] == "D" and tx[i - 1]["date"] == tx[i]["date"]:
        p = tx[i - 2]["c"]
        n_, d_ = tx[i - 1], tx[i]
        N[tx[i]["date"]] = dict(night=(n_["c"] / p - 1) * 100, rng=(n_["h"] - n_["l"]) / p * 100,
                                lo=(n_["l"] / p - 1) * 100, hi=(n_["h"] / p - 1) * 100,
                                day=(d_["c"] / n_["c"] - 1) * 100, prevD=tx[i - 2]["date"])
print("TX", tx[0]["date"], "~", tx[-1]["date"], "；夜盤樣本", len(N))

ixic = yahoo_daily("^IXIC")
tnx = yahoo_daily("^TNX")
r_ix = {ixic[i][0]: (ixic[i][1] / ixic[i - 1][1] - 1) * 100 for i in range(1, len(ixic))}
d_tnx = {tnx[i][0]: (tnx[i][1] - tnx[i - 1][1]) * 100 for i in range(1, len(tnx))}  # 基點

rows, skipped = [], []
for D in tdates:
    T = nxt.get(D)
    if not T or T not in N or N[T]["prevD"] != D:
        continue
    x = dict(D=D, T=T, wd=datetime.strptime(D, "%Y-%m-%d").weekday(), m=D in MD, **N[T],
             ix=r_ix.get(D), tnx=d_tnx.get(D))
    rows.append(x)
have = {x["D"] for x in rows}
skipped = [d for d in MD if d not in have and d >= tx[0]["date"]]
print("對不到夜盤而跳過的公布日（台灣休市等）", skipped)

M = [x for x in rows if x["m"]]
O = [x for x in rows if not x["m"]]
W = [x for x in O if x["wd"] == 2]
print("\n=== 台指夜盤（前一日盤收→夜盤收）：公布那晚 vs 一般 ===")
for tag, lo in (("2017 起", "2017"), ("2022 起", "2022"), ("2025 起", "2025")):
    for name, g in (("公布那晚", M), ("其他週三晚", W), ("其他所有晚上", O)):
        g2 = [x for x in g if x["D"] >= lo]
        print(line(f"{tag} {name}", [x["night"] for x in g2], [x["rng"] for x in g2]))
    print()

print("=== 升息循環裡的公布那晚 ===")
for tag, a, z in (("2017-05～2018-12", "2017-05", "2019-01"), ("2022-03～2023-08", "2022-03", "2023-09"), ("2026 年", "2026", "2027")):
    g = [x for x in M if a <= x["D"] < z]
    print(line(tag, [x["night"] for x in g], [x["rng"] for x in g]))

print("\n=== 公布當天美股與殖利率（同一時段，不是預測）===")
print(line("那指 公布日", [x["ix"] for x in M if x["ix"] is not None]))
print(line("那指 其他日", [x["ix"] for x in O if x["ix"] is not None]))
mt = [x["tnx"] for x in M if x["tnx"] is not None]
ot = [x["tnx"] for x in O if x["tnx"] is not None]
print(f"10 年期殖利率變動（基點） 公布日 n={len(mt)} 平均{st.mean(mt):+.1f} 平均絕對值{st.mean(abs(v) for v in mt):.1f}；"
      f"其他日 平均{st.mean(ot):+.1f} 平均絕對值{st.mean(abs(v) for v in ot):.1f}")
for tag, f in (("殖利率升≥3bp", lambda v: v >= 3), ("殖利率在±3bp內", lambda v: -3 < v < 3), ("殖利率降≥3bp", lambda v: v <= -3)):
    g = [x for x in M if x["tnx"] is not None and f(x["tnx"])]
    print(line("公布那晚 " + tag, [x["night"] for x in g], [x["rng"] for x in g]))

print("\n=== 公布那晚之後：隔天日盤（夜盤收→日盤收）===")
print(line("公布後的日盤", [x["day"] for x in M]))
print(line("其他日盤", [x["day"] for x in O]))
for tag, f in (("夜盤收跌後的日盤", lambda v: v < 0), ("夜盤收漲後的日盤", lambda v: v > 0)):
    print(line(tag, [x["day"] for x in M if f(x["night"])]))

print("\n=== 像今晚：公布前夜盤已經在跌（用整晚收跌 ≥0.5% 近似）===")
g = [x for x in M if x["night"] <= -0.5]
for x in g:
    print(f"  {x['D']} 夜盤{x['night']:+.2f}% 低{x['lo']:+.2f}% 振幅{x['rng']:.2f}% 隔天日盤{x['day']:+.2f}% 那指{x['ix'] if x['ix'] is None else round(x['ix'], 2)} 殖利率{x['tnx'] if x['tnx'] is None else round(x['tnx'], 1)}bp")

print("\n=== 近 12 次公布 ===")
for x in M[-12:]:
    print(f"  {x['D']} 夜盤{x['night']:+.2f}% 高{x['hi']:+.2f}% 低{x['lo']:+.2f}% 振幅{x['rng']:.2f}% 隔天日盤{x['day']:+.2f}% 那指{x['ix'] if x['ix'] is None else round(x['ix'], 2)} 殖利率{x['tnx'] if x['tnx'] is None else round(x['tnx'], 1)}bp")

# 那指期貨小時線：切出公布後兩小時（美東 14:00→16:00）
hr = yahoo_hourly("NQ=F")
byd = {}
for t, o, h, l, c in hr:
    dt = datetime.fromtimestamp(t, NY)
    byd.setdefault(dt.strftime("%Y-%m-%d"), {})[dt.hour] = (o, h, l, c)
post, pre, prng = {}, {}, {}
for d, hs in byd.items():
    if 14 in hs and 15 in hs and 10 in hs and 13 in hs:
        o = hs[14][0]
        post[d] = (hs[15][3] / o - 1) * 100
        prng[d] = (max(hs[14][1], hs[15][1]) - min(hs[14][2], hs[15][2])) / o * 100
        pre[d] = (hs[13][3] / hs[10][0] - 1) * 100
print("\n=== 那指期貨小時線（", min(post), "~", max(post), "）：美東 14:00→16:00 ===")
mm = [d for d in post if d in MD]
oo = [d for d in post if d not in MD]
print(line("公布日 14:00→16:00", [post[d] for d in mm], [prng[d] for d in mm]))
print(line("其他日 14:00→16:00", [post[d] for d in oo], [prng[d] for d in oo]))
print(line("公布日 10:00→14:00", [pre[d] for d in mm]))
for d in sorted(mm):
    print(f"  {d} 公布前(10→14) {pre[d]:+.2f}% 公布後(14→16) {post[d]:+.2f}% 兩小時振幅 {prng[d]:.2f}%")
same = sum(1 for d in mm if pre[d] * post[d] > 0)
print(f"公布前後同方向 {same}/{len(mm)}")
