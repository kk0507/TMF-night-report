# KK 2026-10-08：回測美國 10 年期公債標售對台指夜盤的影響
# 截標＝美東 13:00（台北隔天 01:00，冬令 02:00），結果約 1～2 分鐘後公布。
# 沒有發行前利率（when-issued），算不出「尾差」，改用投標倍數、主要交易商得標比重對前 6 場平均的高低分強弱。
# 用法：python auction_10y.py   （沿用 fomc_minutes 的資料與快取）
import io, contextlib, json, os, statistics as st, requests

with contextlib.redirect_stdout(io.StringIO()):
    import fomc_minutes as f

p = os.path.join(f.CACHE, "ust_notes.json")
if os.path.exists(p):
    J = json.load(open(p))
else:
    J = requests.get("https://www.treasurydirect.gov/TA_WS/securities/search",
                     params={"type": "Note", "format": "json", "startDate": "2016-01-01", "endDate": "2026-10-08",
                             "dateFieldName": "auctionDate"}, headers=f.H, timeout=90).json()
    json.dump(J, open(p, "w"))
A = []
for x in J:
    if not x.get("originalSecurityTerm", "").startswith("10-Year") or x.get("tips") == "Yes" or not x.get("highYield"):
        continue
    A.append(dict(D=x["auctionDate"][:10], y=float(x["highYield"]), btc=float(x["bidToCoverRatio"]),
                  dealer=float(x["primaryDealerAccepted"]) / float(x["competitiveAccepted"]) * 100,
                  indirect=float(x["indirectBidderAccepted"]) / float(x["competitiveAccepted"]) * 100))
A.sort(key=lambda a: a["D"])
for i, a in enumerate(A):
    prev = A[max(0, i - 6):i]
    if len(prev) < 6:
        a["cls"] = None
        continue
    b0, d0 = st.mean(z["btc"] for z in prev), st.mean(z["dealer"] for z in prev)
    a["dbtc"], a["ddealer"] = a["btc"] - b0, a["dealer"] - d0
    a["cls"] = "弱" if a["btc"] < b0 and a["dealer"] > d0 else "強" if a["btc"] > b0 and a["dealer"] < d0 else "普通"
AD = {a["D"]: a for a in A}
print("10 年期標售", len(A), A[0]["D"], "~", A[-1]["D"], "；跟會議紀錄同一天", sum(1 for a in A if a["D"] in f.MD))
print("最近 6 場：", [(a["D"], a["y"], a["btc"], round(a["dealer"], 1), a["cls"]) for a in A[-6:]])
print("投標倍數 近 6 場平均 %.2f；主要交易商比重 近 6 場平均 %.1f%%" % (st.mean(a["btc"] for a in A[-6:]), st.mean(a["dealer"] for a in A[-6:])))

rows = {x["D"]: x for x in f.rows}
M = [dict(rows[d], **AD[d]) for d in AD if d in rows]
O = [x for x in f.rows if x["D"] not in AD]
skipped = [d for d in AD if d not in rows and d >= f.tx[0]["date"]]
print("對不到夜盤而跳過", skipped)

print("\n=== 台指夜盤（前一日盤收→夜盤收）：標售那晚 vs 一般 ===")
for lo in ("2017", "2022", "2025"):
    print(f.line(f"{lo} 起 標售那晚", [x["night"] for x in M if x["D"] >= lo], [x["rng"] for x in M if x["D"] >= lo]))
    print(f.line(f"{lo} 起 其他晚上", [x["night"] for x in O if x["D"] >= lo], [x["rng"] for x in O if x["D"] >= lo]))
print()
for c in ("強", "普通", "弱"):
    g = [x for x in M if x["cls"] == c]
    print(f.line(f"標售結果 {c}", [x["night"] for x in g], [x["rng"] for x in g]))
    t = [x["tnx"] for x in g if x["tnx"] is not None]
    ix = [x["ix"] for x in g if x["ix"] is not None]
    print(f"      當天殖利率平均{st.mean(t):+.1f}bp（升的比例{sum(1 for v in t if v > 0) / len(t) * 100:.0f}%）、那指平均{st.mean(ix):+.2f}%（上漲{sum(1 for v in ix if v > 0) / len(ix) * 100:.0f}%）")
print("\n=== 標售日殖利率（整天，基點）===")
t = [x["tnx"] for x in M if x["tnx"] is not None]
ot = [x["tnx"] for x in O if x["tnx"] is not None]
print(f"標售日 n={len(t)} 平均{st.mean(t):+.1f} 平均絕對值{st.mean(abs(v) for v in t):.1f}；其他日 平均{st.mean(ot):+.1f} 平均絕對值{st.mean(abs(v) for v in ot):.1f}")
for tag, fn in (("當天殖利率升≥5bp", lambda v: v >= 5), ("當天殖利率降≥5bp", lambda v: v <= -5)):
    g = [x for x in M if x["tnx"] is not None and fn(x["tnx"])]
    print(f.line("標售那晚 " + tag, [x["night"] for x in g], [x["rng"] for x in g]))
    g = [x for x in O if x["tnx"] is not None and fn(x["tnx"])]
    print(f.line("對照 其他晚 " + tag, [x["night"] for x in g], [x["rng"] for x in g]))

print("\n=== 隔天日盤（夜盤收→日盤收）===")
print(f.line("標售後的日盤", [x["day"] for x in M]))
for c in ("強", "普通", "弱"):
    print(f.line(f"  結果{c}", [x["day"] for x in M if x["cls"] == c]))
print(f.line("其他日盤", [x["day"] for x in O]))

print("\n=== 最差的 8 個標售夜盤 ===")
for x in sorted(M, key=lambda x: x["night"])[:8]:
    print(f"  {x['D']} 夜盤{x['night']:+.2f}% 低{x['lo']:+.2f}% 結果{x['cls']} 倍數{x['btc']:.2f} 交易商{x['dealer']:.1f}% 殖利率{x['tnx']}bp 那指{x['ix']} 隔天日盤{x['day']:+.2f}%")

# 那指期貨小時線：截標後（美東 13:00→15:00、13:00→16:00）
post2, post3, rng2, pre = {}, {}, {}, {}
for d, hs in f.byd.items():
    if all(h in hs for h in (10, 12, 13, 14, 15)):
        o = hs[13][0]
        post2[d] = (hs[14][3] / o - 1) * 100
        post3[d] = (hs[15][3] / o - 1) * 100
        rng2[d] = (max(hs[13][1], hs[14][1]) - min(hs[13][2], hs[14][2])) / o * 100
        pre[d] = (hs[12][3] / hs[10][0] - 1) * 100
mm = sorted(d for d in post2 if d in AD)
oo = [d for d in post2 if d not in AD]
print("\n=== 那指期貨小時線（", min(post2), "~", max(post2), "）===")
print(f.line("標售日 13:00→15:00", [post2[d] for d in mm], [rng2[d] for d in mm]))
print(f.line("其他日 13:00→15:00", [post2[d] for d in oo], [rng2[d] for d in oo]))
print(f.line("標售日 13:00→16:00", [post3[d] for d in mm]))
print(f.line("其他日 13:00→16:00", [post3[d] for d in oo]))
for c in ("強", "普通", "弱"):
    g = [d for d in mm if AD[d]["cls"] == c]
    print(f.line(f"  結果{c} 13:00→16:00", [post3[d] for d in g]))
for d in mm:
    a = AD[d]
    print(f"  {d} {a['cls']} 倍數{a['btc']:.2f}({a['dbtc']:+.2f}) 交易商{a['dealer']:.1f}%({a['ddealer']:+.1f}) 截標前(10→13){pre[d]:+.2f}% 後2小時{post2[d]:+.2f}% 後3小時{post3[d]:+.2f}% 振幅{rng2[d]:.2f}%{' ←同日會議紀錄' if d in f.MD else ''}")
print("截標前後同方向", sum(1 for d in mm if pre[d] * post3[d] > 0), "/", len(mm))
