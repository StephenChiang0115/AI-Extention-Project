# 開發日誌：2026-11-14（測試基線與 A 區修補）

接續 `2026-11-12-work-plan.md`。

## 環境
- Python 3.14.7（`C:\Python314`，有 `py` 啟動器）。
- 已在 `backend/.venv` 建立虛擬環境並安裝 `requirements.txt`（依賴仍未釘版本）。

## 測試基線
- 修改前：**33 passed**（test_api 16 + test_lmstudio 17），1 個警告（Starlette 對 `httpx` 的棄用提醒，不影響結果）。
- 修改後：**37 passed**（test_api 20 + test_lmstudio 17）。
- 執行方式：`run-tests.bat`，或自動化執行 `run-tests.bat /nopause`；也可 `cd backend && .venv\Scripts\python -m pytest -q`。

## 已完成（工作計畫 A 區）
- [x] **低紋理影像跳過快取**
  - `phash.py` 新增 `is_low_texture(raw, threshold)`：灰階、縮成 32x32，亮度標準差 < 門檻即為低紋理；無法解碼的資料回傳 False。
  - `main.py`：算 `cacheable`，不可快取時跳過 `cache_get`，寫入條件加上 `cacheable`。門檻由環境變數 `LOW_TEXTURE_STDDEV`（預設 8）調整。
  - 取捨：低紋理影像每次都會呼叫模型並扣額度。
  - 測試：`test_low_texture_detection`、`test_low_texture_image_skips_cache`。
  - `test_lmstudio.py` 的 `jpeg()` 原本是純色圖，會被判為低紋理而不走快取，已加上一個矩形讓它有紋理。
- [x] **CORS 限縮**
  - `ALLOWED_ORIGINS`（逗號分隔）設定時只允許清單中的來源；未設定時只允許 `chrome-extension://<32 碼 ID>`（正規表示式）。
  - 方法限 GET、POST；標頭限 Content-Type、X-Client-Id、X-Admin-Token。
  - **注意**：之前用一般網頁（模擬測試頁）呼叫 localhost 後端的做法，現在需要把該頁面的來源加入 `ALLOWED_ORIGINS`。擴充功能背景腳本有 `host_permissions`，不受 CORS 影響。
  - 測試：`test_cors_defaults_to_extension_origins`、`test_cors_uses_allowed_origins_env`。
- [x] **`run-tests.bat`**：加入 `where py` 檢查；支援 `/nopause` 參數；回傳 pytest 的結束碼。
- [x] `.env.example` 加入 `ALLOWED_ORIGINS`、`LOW_TEXTURE_STDDEV` 說明；`CLAUDE.md`、`README.md` 同步更新。

## 踩到的坑
- `.bat` 必須是 CRLF 換行；用 LF 存的 UTF-8 批次檔，cmd 會把中文字切壞而亂執行。
- Windows 上 Python 的 `write_text` 會把 LF 轉成 CRLF，改既有 `.py`／`.md` 要用 `write_bytes` 或編輯器，避免整檔變動。

## 第二部分：子代理並行（使用者要求本專案改用子代理模式）
三個子代理並行，各自只改自己的檔案；主對話整合、跑測試、寫文件。

- [x] **Docker 與依賴**（`backend/Dockerfile`、`backend/.dockerignore`、`requirements.txt`、`requirements-dev.txt`、兩個 `.bat`）
  - 非 root（uid 10001）、`PYTHONUNBUFFERED` 等環境變數、`exec uvicorn` 讓程序收得到 SIGTERM、只裝執行期套件。
  - 釘版本：fastapi 0.162.0、uvicorn[standard] 0.54.0、httpx 0.28.1、pydantic 2.14.3、Pillow 13.0.0、python-dotenv 1.2.4；dev 另加 pytest 9.2.0。
  - 這些版本需要 **Python 3.11 以上**，`.bat` 的提示已改。
  - **未驗證**：本機沒有 Docker，映像沒有實際建置過。間接依賴（starlette、pydantic_core 等）沒釘。
- [x] **辨識評估工具** `scripts/eval_landmarks.py`（階段 6 用）
  - 檔名即答案（`台北101__01.jpg`；`=` 列別名，例如 `台北101=Taipei 101__01.jpg`），輸出 utf-8-sig CSV 與摘要（準確率、信心值、延遲、429）；`--twice` 量第二輪快取命中。
  - 已在 mock 後端驗證（大圖縮圖、壞檔、429 提示、連不到後端的結束碼）。說明在 `scripts/README.md`、`scripts/eval_samples/README.md`。
- [x] **壓測工具** `scripts/loadtest.py`（階段 5 用）
  - 合成有紋理影像、每張 8 種變化（JPEG 品質、±5% 縮放）、Zipf 熱門度、每位模擬使用者獨立 Client-Id；送出前先在本機算 dHash 預檢。
  - mock 實測（150 請求、並行 10、20 位使用者、15 張圖）：命中率 90%，模型呼叫 15 次（無快取需 150 次），估計省 90%；吞吐量約 113 req/s。額度測試：2 位使用者各 10 次未命中後回 429，符合設計。
- 整合後測試：**37 passed**；兩個腳本可編譯、`--help` 正常。

## 子代理回報的問題（已寫入 CLAUDE.md「已知問題」）
- 客戶端 p95 約 300 ms，伺服器端 `/api/stats` 約 3 ms：推測 dHash 等 CPU 運算阻塞 async 事件迴圈（未證實）。
- 同一張圖同時兩個未命中 → 兩次模型呼叫、兩次扣額度。
- 壓測命中率 90% 是最佳情況（變化只有壓縮與縮放，沒有不同裁切）。
- 評估工具的寬鬆比對可能誤判（名稱太短、臺／台），要人工複核 CSV；同一批重跑會命中上次快取，統計模型表現只看 `cache_hit=False` 或重啟後端。
- 測試時兩個子代理曾打到彼此的測試 server，壓測的伺服器端數字被多算約 4 筆；正式量測要用專用 port 並重啟後端。

## 未完成／卡住
- 階段 4：平台未定；Docker 映像未建置；雲端 `Store` 未做。
- 階段 5：等部署後在雲端跑 `loadtest.py`。
- 階段 6：需要使用者在 LM Studio 載入支援圖片的模型，再用 `eval_landmarks.py` 實測。
- CORS 固定擴充功能 ID：尚未在 `manifest.json` 設 `key`。

## 下一步
1. **（使用者）階段 6 實測**：照片放 `scripts/eval_samples/`，`backend/.env` 設 `PROVIDER=lmstudio`、調高 `DAILY_FREE_LIMIT`，啟動後端後跑 `eval_landmarks.py --twice`。
2. **（可交給子代理）後端效能修補**：dHash／低紋理改到 threadpool、同雜湊未命中請求合併，並補測試；之後重跑壓測比較延遲。
3. 釘住 starlette 或改用 lock 檔；有 Docker 時建置映像並測 `/health`。
4. 拿到課程資訊後：雲端 `Store`、部署、雲端壓測。

## 待使用者確認（同上次）
1. 課程指定的雲端平台？
2. 部署時模型從哪來（LM Studio 在本機的問題）？
3. 課程講義與實驗何時能拿到？
