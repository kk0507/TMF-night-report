# 微台(TMF)夜盤快報

改寫自 [stock-strategies-only](https://github.com/zxc0978563770/stock-strategies-only) 的夜盤快報功能，把固定門檻分五級的判斷換成跟歷史資料算百分位，並加入均線乖離、量能趨勢、外資期貨籌碼三項。平日台灣時間07:00（夜盤05:00收盤後）自動送一則Discord訊息。

（原本考慮用LINE Notify，但這個服務已經在2025年3月被LINE官方關掉了；Telegram KK不想用；最後選Discord，設定最簡單，只要一組Webhook網址，不用申請bot。）

**這是資料整理工具，不是自動下單、也不做買賣建議。**

## 這份報告會告訴你什麼

- 昨晚夜盤本身漲跌幅
- 近3日/5日累計漲跌幅，以及這個漲跌幅在過去(預設抓400天)歷史資料裡排第幾百分位
- 現價對10日/20日均線的乖離率
- 近3日成交量是放大還是縮小
- 外資期貨(TX)淨未平倉部位方向與較前一日的變化

## 部署步驟（這幾步要你自己做，我沒辦法代勞）

1. **開一個GitHub帳號**（如果還沒有），建一個新的**private repo**
2. **把這個資料夾（`night_session_report.py`、`requirements.txt`、`.github/workflows/premarket_tmf.yml`）整個推上去那個repo**，資料夾結構要維持原樣（`.github/workflows/` 這個路徑GitHub才認得出來是排程設定）
3. **建一個Discord Webhook**：
   - 打開Discord，選一個伺服器（沒有的話自己建一個，免費，只有你自己看得到也沒關係）
   - 選一個文字頻道 → 頻道設定（齒輪圖示）→ 整合(Integrations) → Webhooks → New Webhook
   - 取個名字（例如「微台快報」），複製那組 **Webhook URL**，這就是 `DISCORD_WEBHOOK_URL`
4. **把兩個密鑰存進repo設定**：repo頁面 → Settings → Secrets and variables → Actions → New repository secret，依序新增：
   - `FINMIND_TOKEN`
   - `DISCORD_WEBHOOK_URL`
5. 到repo的 Actions 分頁，找到「微台夜盤快報」這個workflow，點 **Run workflow** 手動觸發一次，測試會不會在Discord頻道收到訊息
6. 測試沒問題後就不用再管它，平日早上07:00會自動跑

## 之後要調整什麼

- `LOOKBACK_DAYS`（目前400天）：抓越長歷史，百分位算得越準，但FinMind免費版有速率限制(600次/小時)，這個排程一天只跑一次不會超額，不用擔心
- 近月合約換月是FinMind資料裡`成交量最大合約`自動判斷的，不用手動改
- 這份報告目前**不會**判斷「該不該進場」，只給資料整理——要不要進場的判斷，還是回來跟AI助理討論套原本那套checklist（技術面/消息面/賠率/動能）
