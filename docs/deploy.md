# 雲端部署指南（Render + Neon + Gemini，全部免費、不需信用卡）

架構：Chrome 擴充功能 → **Render**（Docker 跑 FastAPI 後端，新加坡）→ **Neon**（Postgres，快取／額度／紀錄）＋ **Gemini 2.5 Flash-Lite**（辨識模型，透過 OpenAI 相容端點）。

> 免費條款常變動，動手前請再看一次各服務的官方頁面。本文寫於 2026-11-14。

---

## 0. 事前準備

- GitHub 帳號（Render 從 GitHub 拉程式碼）
- 一個 Google 帳號（取得 Gemini API 金鑰）
- 本機確認測試通過：雙擊 `run-tests.bat`

**金鑰原則**：所有金鑰與連線字串只放在 Render 控制台（或本機 `backend/.env`）。不要寫進 `render.yaml`、程式碼、截圖、報告或聊天訊息。

---

## 1. 建立 Neon 資料庫

1. 到 <https://neon.tech> 註冊（可用 GitHub 或 Google 登入，不需信用卡）。
2. 建立專案：
   - Region 選 **AWS Asia Pacific (Singapore)**（和 Render 同區，延遲最低）。
   - Postgres 版本用預設即可。
3. 到專案的 **Dashboard → Connect**：
   - 勾選 **Connection pooling**（主機名稱會出現 `-pooler`）。
   - 複製連線字串，格式類似：
     ```
     postgresql://neondb_owner:********@ep-xxxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
     ```
   - 確認結尾有 `sslmode=require`。
4. 先把這串字放在安全的地方，第 4 步要貼到 Render。資料表由後端第一次啟動時自動建立。

---

## 2. 取得 Gemini API 金鑰

1. 到 <https://aistudio.google.com> 用 Google 帳號登入。
2. 左側 **Get API key → Create API key**，複製金鑰。
3. 不需要啟用付費（不要點「Set up billing」）。
4. 在 AI Studio 確認 `gemini-2.5-flash-lite` 的免費每日次數與每分鐘次數。

**隱私說明（報告中請註明）**：Gemini 免費層的輸入資料可能被 Google 用來改進產品。本系統送出的只有使用者框選的網頁畫面片段、網頁標題與網址，不含個人資料；使用者身分是隨機 UUID。

---

## 3. 把程式推到 GitHub

1. 在 GitHub 建立新的儲存庫（建議 Private）。
2. 本機推送：
   ```
   git remote add origin https://github.com/<你的帳號>/landmark-mvp.git
   git push -u origin main
   ```
3. 確認 `backend/.env` **沒有**被推上去（`.gitignore` 已排除）。到 GitHub 網頁檢查一次。

---

## 4. 用 Blueprint 部署到 Render

1. 到 <https://render.com> 用 GitHub 登入（免費方案不需信用卡）。
2. **New → Blueprint**，授權並選擇剛剛的儲存庫。Render 會讀取根目錄的 `render.yaml`，建立 `landmark-backend` 服務（Docker、Free、Singapore）。
3. 畫面會要求填三個機密值（`sync: false` 的變數）：

   | 變數 | 填什麼 |
   |---|---|
   | `DATABASE_URL` | 第 1 步的 Neon pooled 連線字串 |
   | `LMSTUDIO_API_KEY` | 第 2 步的 Gemini API 金鑰 |
   | `ADMIN_TOKEN` | 自己產生一串隨機字（例如在 PowerShell 執行 `[guid]::NewGuid()`），用來保護 `/api/stats` |

   其他變數（`STORE=postgres`、`PROVIDER=lmstudio`、Gemini 端點與模型名稱、`DAILY_FREE_LIMIT`、`LOW_TEXTURE_STDDEV`）已寫在 `render.yaml`。
4. 按 **Apply**，等待建置與部署（第一次約 3 到 5 分鐘）。在服務的 **Logs** 可以看到進度與錯誤。
5. 部署完成後，服務頁面上方會顯示網址，例如 `https://landmark-backend-xxxx.onrender.com`。

> `sync: false` 的值只有「第一次建立 Blueprint」時會詢問。之後要改或新增機密，請到服務的 **Environment** 頁面手動修改，改完服務會自動重新部署。

---

## 5. 驗證部署

把下面的網址換成你的 Render 網址（PowerShell）：

```powershell
# 健康檢查：應回傳 {"status":"ok"}
curl.exe https://landmark-backend-xxxx.onrender.com/health

# 統計（需要管理者權杖）
curl.exe -H "X-Admin-Token: 你的ADMIN_TOKEN" https://landmark-backend-xxxx.onrender.com/api/stats
```

- 第一次呼叫若等很久，是服務正在喚醒，屬正常現象。
- 沒帶權杖呼叫 `/api/stats` 應回 403。

---

## 6. 設定擴充功能

1. `chrome://extensions` → 對「AI 景點導覽精靈」按 **重新載入**（`manifest.json` 已加入 `https://*.onrender.com/*` 權限）。
2. 在擴充功能上按右鍵 → **選項**。
3. 後端 API 位址填 `https://landmark-backend-xxxx.onrender.com`（結尾不加斜線），按 **儲存**。
4. 到任一網頁按擴充功能圖示，框選地標測試。

**CORS**：`ALLOWED_ORIGINS` 不設定即可。未設定時後端只接受 `chrome-extension://` 來源，擴充功能的背景腳本可以正常呼叫。若要再限縮到自己的擴充功能，可在 Render 設 `ALLOWED_ORIGINS=chrome-extension://<擴充功能 ID>`（ID 在 `chrome://extensions` 可看到；未封裝載入時換電腦 ID 可能不同）。

---

## 7. 免費方案限制與因應

| 服務 | 限制 | 影響 |
|---|---|---|
| Render Free | 閒置 **15 分鐘**自動休眠，喚醒約 **1 分鐘** | 第一次辨識會比較慢。擴充功能會先輪詢 `/health` 最多 90 秒等它醒來，逾時會顯示「伺服器喚醒中，請稍候再試」 |
| Render Free | 每個工作區每月 **750 小時**免費執行時間（休眠不計） | 一個服務全天開著約 720 小時，夠用；不要同時開多個免費服務 |
| Render Free | 沒有持久磁碟、重新部署會重啟 | 資料都在 Neon，不受影響 |
| Neon Free | 閒置 **5 分鐘**運算休眠，喚醒通常不到 1 秒；儲存上限約 0.5–1 GB（以官方頁面為準） | 第一個查詢稍慢；只存雜湊與劇本，不存影像 |
| Gemini 免費層 | 每分鐘／每日次數有上限 | 超過會回 429，後端回 502 並**退還使用者額度**；壓測時注意不要耗盡 |

### 展示前喚醒

展示或壓測前 **2 分鐘**先打一次健康檢查，讓 Render 醒來（`/health` 不查資料庫；Neon 會在第一次辨識時自動喚醒，不到 1 秒）：

```powershell
curl.exe https://landmark-backend-xxxx.onrender.com/health
```

或直接在網頁上按一次擴充功能圖示（啟用時會自動送出喚醒請求）。壓測時記得排除第一筆冷啟動的數據。

---

## 8. 課堂展示：改用本機 LM Studio（透過通道）

課程要求使用 LM Studio 時，可以讓雲端後端改呼叫你電腦上的 LM Studio。**電腦必須開著且 LM Studio 伺服器在執行。**

1. **先開啟 LM Studio 的 API 金鑰驗證**（Developer 分頁 → Server Settings → 開啟需要 API Token／Authentication，產生一組金鑰）。
   不開的話，任何知道通道網址的人都能用你的電腦跑模型。
2. 載入支援圖片的模型，Start Server（port 1234）。
3. 建立通道（擇一）：
   - **ngrok 免費固定網域**：註冊 ngrok 後在控制台領取一個免費 static domain，執行
     `ngrok http --url=<你的網域>.ngrok-free.app 1234`
   - **Cloudflare 快速通道**（不用註冊，但每次網址都會變）：
     `cloudflared tunnel --url http://localhost:1234`
4. 到 Render 服務的 **Environment** 修改：
   - `LMSTUDIO_BASE_URL` = `https://<通道網域>/v1`
   - `LMSTUDIO_MODEL` = LM Studio 中的模型名稱
   - `LMSTUDIO_API_KEY` = LM Studio 產生的金鑰（取代 Gemini 金鑰）
   - `LMSTUDIO_TIMEOUT_S` 可調高到 `80`（本機模型較慢，但要低於擴充功能的 90 秒逾時）
5. 儲存後 Render 會自動重新部署。展示結束後改回 Gemini 的三個值。

> 本機模型較慢，若單次辨識超過約 30 秒，Chrome 可能中止擴充功能的背景程序，請選用較小的模型。

---

## 9. 費用確認清單（全部不需信用卡）

- [ ] Neon：Free 方案，註冊時沒有要求付款方式
- [ ] Google AI Studio：只建立 API 金鑰，**沒有**按「Set up billing」，專案顯示為 Free tier
- [ ] Render：服務方案顯示 **Free**，帳戶 Billing 頁面沒有綁定付款方式
- [ ] Render：只建立一個免費 Web Service（避免超過 750 小時）
- [ ] GitHub：Private 儲存庫免費
- [ ] ngrok／Cloudflare 快速通道：免費方案，不需付款方式
- [ ] 任何服務若跳出「升級」或「新增付款方式」提示，一律略過

## 疑難排解

| 現象 | 可能原因 |
|---|---|
| Render 部署失敗、Logs 顯示連不上資料庫 | `DATABASE_URL` 貼錯，或缺少 `sslmode=require` |
| 辨識一直顯示「辨識服務暫時無法使用」 | Gemini 金鑰錯誤、模型名稱錯誤或超過免費次數；看 Render Logs 的「模型辨識失敗」 |
| 擴充功能顯示「連不上伺服器」 | 選項頁網址打錯，或改網址後沒有重新載入擴充功能 |
| 擴充功能顯示「伺服器喚醒中」 | Render 正在冷啟動，等 1 分鐘再試 |
