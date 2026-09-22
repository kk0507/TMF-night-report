# 微台(TMF)夜盤快報 ＋ 價格通知

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

## 價格通知（price_alert.py）

`price_alert.py` 每5分鐘查一次微台近月即時報價（期交所官方API），設定的目標價被**穿越**（不管從上往下還是從下往上）就送一則Discord通知，觸發過一次就不會再重複發。

- **這個repo要是public**才能免費用每5分鐘的頻率排程，不然private repo的免費額度(2,000分鐘/月)一個月就會超額被收費。程式碼本身不含任何機敏資訊（webhook網址、API金鑰都存在repo secrets，不會因為repo公開而外洩）。
- **目標價清單存在 `alerts.json`**，要新增/清除通知，跟AI助理說一聲，改完會直接push上去生效，不用自己手動編輯這個檔案。
- 判斷邏輯：每次執行記錄現在價格在目標價的「上方還是下方」，跟上一次記錄的方向比對，方向變了代表穿越過去了，觸發通知。剛設定好的當下不會馬上觸發（除非之後真的穿越過去）。
- 跟夜盤快報共用同一組 `DISCORD_WEBHOOK_URL`，不用額外設定。
- 如果現在剛好沒開盤（日盤/夜盤都沒有新資料，或資料超過10分鐘沒更新），這次執行會直接跳過，不會誤判。
