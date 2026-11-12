# 專案背景（給之後接手的人與 Claude）

## 這是什麼
AI 景點導覽精靈：Chrome 擴充功能（Manifest V3）。使用者在任何網頁框選地標，後端辨識後，由頁面上的 AI 角色用對話導覽。
這是資管系畢業專題的最小可行版本，同時作為「雲端運算與實作」課程的期中報告。溝通與文件一律使用繁體中文。

## 目前進度
- [x] 階段 1：後端骨架（FastAPI、JSON Schema 驗證、mock 模式）
- [x] 階段 2：Chrome Extension（框選截圖、Shadow DOM 角色、對話與選項）
- [x] 階段 3：影像感知雜湊（dHash）快取、每日免費額度、`/api/stats`
- [ ] 階段 4：容器化並部署到雲端；儲存層換成雲端資料庫
- [ ] 階段 5：壓測與成本統計（期中報告要用的數據）
- [~] 階段 6：接上真實模型（課程使用 LM Studio）：`LMStudioProvider` 已完成並有測試，尚未用真實模型實測辨識品質

## 目錄
- `backend/app/main.py`：API 與流程（先查快取，未命中才扣額度並呼叫模型）
- `backend/app/providers.py`：模型抽象層。`MockProvider`（假資料）、`LMStudioProvider`（OpenAI 相容 API，送圖片並要求 JSON Schema 結構化輸出，`parse_script` 負責容錯與清理）；`GeminiProvider` 只有介面
- `backend/app/store.py`：儲存層介面 `Store` 與記憶體實作 `MemoryStore`（快取、額度、紀錄、統計）
- `backend/app/phash.py`：dHash 與漢明距離
- `backend/app/schemas.py`：請求／回應與劇本 JSON 結構
- `backend/tests/test_api.py`：API、快取、額度測試（16 項）
- `backend/tests/test_lmstudio.py`：LM Studio Provider 測試，用假的 HTTP 傳輸層，不需開模型（17 項）
- `extension/`：`background.js`（截圖、呼叫後端）、`content.js`（框選、裁切、Shadow DOM 角色）、`options.*`（後端網址設定）

## 常用指令（Windows）
- 啟動後端：雙擊 `start-backend.bat`（第一次會建立虛擬環境並安裝套件，預設 mock 模式、不需要金鑰）
- 執行測試：雙擊 `run-tests.bat`
- 載入擴充功能：`chrome://extensions` → 開啟開發人員模式 → 載入未封裝項目 → 選 `extension` 資料夾
- 接 LM Studio：在 LM Studio 載入支援圖片的模型 → Developer 分頁 Start Server（port 1234）→ `backend/.env` 設 `PROVIDER=lmstudio` → 重新啟動後端。模型錯誤會印在後端視窗（`模型辨識失敗`）

## 開發規則
- **工作計畫與開發日誌一律放在 `devlog/`**，用 Markdown，檔名 `YYYY-MM-DD-主題.md`（例如 `2026-11-12-work-plan.md`）。
- 每次開工先讀 `devlog/` 裡最新的一份，確認上次做到哪、下一步是什麼。
- 每次收工前，新增或更新當天的 devlog：已完成、測試基線、未完成事項、卡住的地方、下一步（要能讓換電腦的人直接接手）。
- 計畫裡的待辦用 `- [ ]` / `- [x]` 勾選；階段進度有變動時，同步更新本檔「目前進度」與 `README.md`。
- 範圍較大的工作（多個獨立面向）採多代理模式：用子代理並行處理，主對話負責整合與寫入 devlog。
- 溝通與文件一律使用繁體中文。

## 重要設計決定（改動前請先想過）
- **只有未命中快取的請求才扣額度**；命中不扣；辨識失敗退還額度。這是商業模式的核心。
- 信心值 < 0.6 或無法辨識的結果不寫入快取，角色改用不確定的語氣。
- 所有網路請求由 Extension 的背景腳本發出，不在網頁內容腳本裡發出。
- 角色 UI 放在閉合的 Shadow DOM；模型輸出一律用 `textContent` 渲染，避免 XSS。
- 用戶身分是安裝時產生的隨機 UUID（`X-Client-Id`），不含個人資料。
- 模型呼叫經 `VisionProvider` 抽象層，換模型只新增一個 Provider。
- **API 金鑰只放後端 `.env`**，不放進擴充功能、不提交版本控制（`.gitignore` 已排除 `.env`）。

## 已知問題與限制
- dHash 抗縮放與壓縮，但對「裁切範圍不同」很敏感：同圖裁掉 10% 邊緣，差異 13（門檻 6）就不會命中。真實命中率未知；門檻可用 `HAMMING_THRESHOLD` 調整，必要時改用影像向量。
- 快取與額度存在記憶體，重啟即清空（階段 4 處理）。
- mock 模式不論框選什麼都回傳「台北 101」；辨識正確率沒有數據。
- CORS 目前允許所有來源，上線前要限縮為 Extension。
- 擴充功能的 `manifest.json` 的 `host_permissions` 只包含 localhost 與 `*.run.app`；部署到其他網域要加。
- 模型回傳的 JSON 格式不完整時，`parse_script` 會修正（百分比信心值、不存在的 next_step、未知情緒等）；修不了就回 502 並退還額度。
- 不支援 `response_format` 結構化輸出的模型會自動退回純提示詞模式。
- 辨識時會把網頁標題與「解碼後」的網址一起給模型當脈絡（例如 Google 地圖網址含地名）。實測 gemma-4-e4b 只看圖片時，把圓山大飯店認成紫禁城。
- 純色或幾乎沒有紋理的框選（天空、黑畫面）dHash 都接近 0，彼此會誤命中快取。真實模型對這類圖通常回傳無法辨識（不會寫入快取），風險不大，但可考慮對低紋理影像跳過快取。
- 尚未在真實 YouTube 頁面實測；端到端測試是在模擬頁面上做的（Chrome 的本機網路限制讓測試頁不能直接呼叫 localhost，真實的擴充功能背景腳本不受此限）。

## 下一步與待確認
1. 確認課程指定的雲端平台（預設 Google Cloud Run；`backend/Dockerfile` 已備好）。
2. LM Studio 跑在本機，雲端連不到它：部署方式可能要調整（例如後端也跑在本機，或另找可外部連線的做法）。
3. 地標辨識需要支援圖片輸入的模型，要確認 LM Studio 載入的模型支援。
4. 之後會拿到課程講義與實驗，閱讀後再決定階段 4 與 6 的做法。

## 相關文件（線上）
- 專題報告（課堂用）：https://claude.ai/artifact/27VRvfY1xZ3iNuU7vwoJ5x
- 期中簡報：https://claude.ai/artifact/FS4uabVBd9m2hdt3FjbGYY
