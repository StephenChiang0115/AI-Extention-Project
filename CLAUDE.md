# 專案背景（給之後接手的人與 Claude）

## 這是什麼
AI 景點導覽精靈：Chrome 擴充功能（Manifest V3）。使用者在任何網頁框選地標，後端辨識後，由頁面上的 AI 角色用對話導覽。
這是資管系畢業專題的最小可行版本，同時作為「雲端運算與實作」課程的期中報告。溝通與文件一律使用繁體中文。

## 目前進度
- [x] 階段 1：後端骨架（FastAPI、JSON Schema 驗證、mock 模式）
- [x] 階段 2：Chrome Extension（框選截圖、Shadow DOM 角色、對話與選項）
- [x] 階段 3：影像感知雜湊（dHash）快取、每日免費額度、`/api/stats`
- [~] 階段 4：免費部署（Render + Neon）：`PostgresStore`、`render.yaml`、部署指南 `docs/deploy.md`、擴充功能冷啟動處理都已完成，並在本機 Postgres 16 端到端驗證；**尚未實際部署**（使用者要先註冊 Render、Neon、Google AI Studio），Docker 映像也還沒實際建置過
- [~] 階段 5：壓測與成本統計（期中報告要用的數據）：`scripts/loadtest.py` 已完成，在本機 mock 與本機 Postgres 驗證過，待部署後量測
- [~] 階段 6：接上真實模型：`LMStudioProvider` 同時支援本機 LM Studio 與 Gemini 的 OpenAI 相容端點（只改設定），評估工具 `scripts/eval_landmarks.py` 已備好；尚未用真實模型實測

## 目錄
- `backend/app/main.py`：API 與流程（先查快取，未命中才扣額度並呼叫模型）。CPU 運算與儲存層呼叫都放到 threadpool；同一張圖同時未命中會合併成一次模型呼叫；`STORE` 環境變數選儲存層
- `backend/app/providers.py`：模型抽象層。`MockProvider`（假資料）、`LMStudioProvider`（OpenAI 相容 API，送圖片並要求 JSON Schema 結構化輸出，`parse_script` 負責容錯與清理；也用來接 Gemini）；`GeminiProvider` 只有介面，不需要實作
- `backend/app/store.py`：儲存層介面 `Store` 與記憶體實作 `MemoryStore`
- `backend/app/store_pg.py`：`PostgresStore`（Neon 用）：漢明距離用 `bit_count` 查、原子額度 UPSERT、連線池與斷線重試、每小時自動清理與彙總紀錄
- `backend/app/phash.py`：dHash、漢明距離、低紋理判斷
- `backend/app/schemas.py`：請求／回應與劇本 JSON 結構
- `backend/tests/test_api.py`：API、快取、額度、低紋理、CORS、並行合併、取消退額度、儲存層選擇（29 項）
- `backend/tests/test_lmstudio.py`：LM Studio Provider，用假的 HTTP 傳輸層，不需開模型（20 項）
- `backend/tests/test_store_pg.py`：`PostgresStore`（19 項；其中 15 項要設 `TEST_DATABASE_URL` 才會跑，沒設就跳過）
- `scripts/eval_landmarks.py`：批次辨識評估（檔名即正確答案，輸出 CSV 與準確率、信心值、延遲）
- `scripts/loadtest.py`：壓測與成本統計（合成影像、Zipf 熱門度、命中率、延遲、額度、節省費用）
- `extension/`：`background.js`（截圖、呼叫後端、喚醒 Render）、`content.js`（框選、裁切、Shadow DOM 角色）、`options.*`（後端網址設定）
- `render.yaml`：Render Blueprint（免費方案、新加坡）；`docs/deploy.md`：免費部署步驟

## 常用指令（Windows）
- 啟動後端：雙擊 `start-backend.bat`（第一次會建立虛擬環境並安裝套件，預設 mock 模式、記憶體儲存、不需要金鑰）
- 執行測試：雙擊 `run-tests.bat`（自動化執行加 `/nopause`）。執行期套件在 `requirements.txt`，測試用套件在 `requirements-dev.txt`
- 需要 Python 3.11 以上（釘住的 Pillow、Starlette 版本要求）
- 評估與壓測：見 `scripts/README.md`（後端要先啟動）
- 部署：見 `docs/deploy.md`
- 載入擴充功能：`chrome://extensions` → 開啟開發人員模式 → 載入未封裝項目 → 選 `extension` 資料夾
- 接 LM Studio：在 LM Studio 載入支援圖片的模型 → Developer 分頁 Start Server（port 1234）→ `backend/.env` 設 `PROVIDER=lmstudio` → 重新啟動後端。模型錯誤會印在後端視窗（`模型辨識失敗`）
- 接 Gemini：同樣 `PROVIDER=lmstudio`，把 `LMSTUDIO_BASE_URL`、`LMSTUDIO_MODEL`、`LMSTUDIO_API_KEY` 改成 Gemini 的值（見 `.env.example`）

## 開發規則
- **工作計畫與開發日誌一律放在 `devlog/`**，用 Markdown，檔名 `YYYY-MM-DD-主題.md`（例如 `2026-11-12-work-plan.md`）。
- 每次開工先讀 `devlog/` 裡最新的一份，確認上次做到哪、下一步是什麼。
- 每次收工前，新增或更新當天的 devlog：已完成、測試基線、未完成事項、卡住的地方、下一步（要能讓換電腦的人直接接手）。
- 計畫裡的待辦用 `- [ ]` / `- [x]` 勾選；階段進度有變動時，同步更新本檔「目前進度」與 `README.md`。
- 本專案採**子代理模式**：工作拆成檔案不重疊的獨立任務交給子代理並行，主對話負責整合、跑完整測試與寫入 devlog。
- **只用免費、不需綁信用卡的服務**（使用者 2026-11-14 決定），付費雲端不列入規劃。
- 溝通與文件一律使用繁體中文。
- `.bat` 必須是 CRLF；其他檔案維持 LF。Windows 上用 Python 改檔請用 `read_bytes`／`write_bytes`，避免換行被改掉。

## 重要設計決定（改動前請先想過）
- **只有未命中快取的請求才扣額度**；命中不扣；辨識失敗或請求被取消都退還額度。這是商業模式的核心。
- 信心值 < 0.6 或無法辨識的結果不寫入快取，角色改用不確定的語氣。
- 所有網路請求由 Extension 的背景腳本發出，不在網頁內容腳本裡發出。
- 角色 UI 放在閉合的 Shadow DOM；模型輸出一律用 `textContent` 渲染，避免 XSS。
- 用戶身分是安裝時產生的隨機 UUID（`X-Client-Id`），不含個人資料；資料庫的 `client_id` 欄位是 `text`。
- 模型呼叫經 `VisionProvider` 抽象層；OpenAI 相容的服務直接用 `LMStudioProvider` 改設定即可。
- **API 金鑰只放後端 `.env` 或 Render 控制台**，不放進擴充功能、不提交版本控制（`.gitignore` 已排除 `.env`）。
- 資料庫只存雜湊與劇本，不存影像；原始紀錄保留 `LOG_RETENTION_DAYS`（預設 14）天，之前的彙總成 `daily_stats`，守住 Neon 1 GB。
- 雜湊以二補數存成有號 bigint（不能「減 2^63」，會破壞 XOR）。

## 已知問題與限制
- dHash 抗縮放與壓縮，但對「裁切範圍不同」很敏感：同圖裁掉 10% 邊緣，差異 13（門檻 6）就不會命中。真實命中率未知；門檻可用 `HAMMING_THRESHOLD` 調整，必要時改用影像向量。壓測的 90% 命中率是最佳情況。
- `STORE=memory`（預設）時快取與額度存在記憶體，重啟即清空；部署用 `STORE=postgres`。
- mock 模式不論框選什麼都回傳「台北 101」；辨識正確率沒有數據。
- CORS 由 `ALLOWED_ORIGINS`（逗號分隔）控制；未設定時只允許 `chrome-extension://` 來源。用一般網頁（例如模擬測試頁）呼叫後端時，要把該網頁來源加進 `ALLOWED_ORIGINS`。
- `manifest.json` 的 `host_permissions` 只包含 localhost 與 `*.onrender.com`；改用通道（ngrok 等）直連後端時要加網域。
- Render 免費方案閒置 15 分鐘休眠，喚醒約 1 分鐘；Chrome 會在背景腳本約 30 秒沒回應時終止它，所以 `background.js` 用多次短的 `/health` 呼叫等伺服器醒來（最多 90 秒）。辨識本身超過約 30 秒（例如經通道接慢的本機模型）仍可能被中斷，`content.js` 會顯示中文說明。
- 模型回傳的 JSON 格式不完整時，`parse_script` 會修正；修不了就回 502 並退還額度。
- 不支援 `response_format` 的模型會自動退回純提示詞模式；只有純提示詞重試成功才永久關閉（Gemini 金鑰錯誤也回 400，不會誤關）。
- Gemini 可能會「思考」而吃掉 `max_tokens`；可設 `LMSTUDIO_REASONING_EFFORT=none`，尚未實測。Gemini 免費層資料會被用來改進產品（截圖不含個資）。
- 辨識時會把網頁標題與「解碼後」的網址一起給模型當脈絡。實測 gemma-4-e4b 只看圖片時，把圓山大飯店認成紫禁城。
- 亮度標準差低於 `LOW_TEXTURE_STDDEV`（預設 8）的影像不查也不寫快取（每次都會扣額度）；純雜訊大圖也會被判為低紋理。
- 同一張圖同時未命中的合併只在單一行程內有效，且只合併「雜湊完全相同」的請求。
- `/api/stats` 的伺服器延遲包含 threadpool 等待時間，和改版前的數字不能直接比較；報告要註明引用的是客戶端還是伺服器端延遲。
- `PostgresStore` 遇斷線會重試一次；極少數情況（伺服器已提交但回覆遺失）可能多扣一次額度。
- 只在本機 Postgres 16 測過，**Neon（SSL、喚醒延遲、pooled 連線）尚未實測**。
- 尚未在真實 YouTube 頁面實測。

## 下一步與待確認
1. **使用者**：依 `docs/deploy.md` 註冊 Neon、Google AI Studio、Render，金鑰只填在 Render 控制台。
2. 部署後：測 `/health`、`/api/stats`，用 `eval_landmarks.py` 比較 Gemini 與 LM Studio，用 `loadtest.py` 量雲端數據（排除冷啟動）。
3. 待確認：課程是否規定全程使用 LM Studio（線上用 Gemini、課堂展示切回 LM Studio 是否可以）。
4. 之後會拿到課程講義與實驗，閱讀後再調整。

## 相關文件（線上）
- 專題報告（課堂用）：https://claude.ai/artifact/27VRvfY1xZ3iNuU7vwoJ5x
- 期中簡報：https://claude.ai/artifact/FS4uabVBd9m2hdt3FjbGYY
