# 免費部署方案（2026-11-14，取代原本的 Cloud Run＋Firestore 規劃）

使用者決定：**不使用需要付費或綁卡的雲端服務**。以下由三個研究子代理查證官方頁面後整理；免費條款常變動，實際動手前請再看一次來源頁面。

## 推薦組合（全部免費、不需信用卡）

| 層 | 選擇 | 理由 | 主要代價 |
|---|---|---|---|
| 後端託管 | **Render Free**（Docker，從 GitHub 部署） | 不用綁卡、直接用現有 Dockerfile、算真正的雲端部署 | 閒置 15 分鐘休眠，喚醒約 1 分鐘（展示前先打一次）；無持久磁碟 |
| 資料庫 | **Neon Free**（Postgres，新加坡） | 永久免費、不用綁卡、1 GB、資料不會被暫停；Postgres 原生支援漢明距離（`bit_count`）與原子額度（UPSERT … RETURNING） | 閒置 5 分鐘後運算休眠，喚醒 < 1 秒；1 GB 上限 |
| 模型 | **Gemini 2.5 Flash-Lite**（OpenAI 相容端點） | 地標知識最廣、繁中好、台灣可用、不用綁卡；**沿用 `LMStudioProvider`，只改 `.env`** | 免費層資料會被 Google 用來改進產品（截圖不含個資，報告中註明）；每日次數要在 AI Studio 確認 |
| 模型（課堂展示） | **LM Studio + 通道**（ngrok 固定網域或 Cloudflare 快速通道） | 課程要求用 LM Studio；同樣只改 `LMSTUDIO_BASE_URL` | 電腦要開著；**必須在 LM Studio 開 API 金鑰**，否則任何人都能用你的電腦跑模型 |

模型設定範例（`backend/.env` 或 Render 的環境變數）：
```
PROVIDER=lmstudio
LMSTUDIO_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LMSTUDIO_MODEL=gemini-2.5-flash-lite
LMSTUDIO_API_KEY=<AI Studio 金鑰，只放環境變數>
```
注意：Gemini 3 以上的 thinking 關不掉，可能吃掉 `max_tokens: 1500` 導致截斷，先用 2.5 Flash-Lite。

### 備案
- 託管：Zeabur（中文介面、不用卡，但免費額度官方沒寫清楚）。Cloud Run 延遲最低但**要綁卡**，不符合原則。
- 資料庫：Supabase（同樣是 Postgres，程式通用；但 7 天沒用會整個專案暫停）、TiDB（5 GB，MySQL 語法）。
- 模型：Groq `qwen/qwen3.8-27b`（支援 strict json_schema、只改 `.env`；但每天約 50 次）。

### 已排除
- 託管：Fly.io、Railway（只剩試用）、Koyeb（要卡）、HF Spaces（Docker 據報改付費）、Oracle（要卡、常搶不到）。
- 資料庫：Firestore（不能做位元運算，模糊比對很耗讀取額度）、Upstash（每月 50 萬指令不夠）、CockroachDB、Xata（只有試用）。
- 模型：GitHub Models（2026-07-30 停止服務）、HF Inference（免費帳號已無額度）、Cerebras（要卡）、OpenRouter 免費模型（冷門、每天 50 次）。

## 資料成長策略（守住 Neon 1 GB）
- 只存雜湊與劇本，不存影像；每筆快取約 5 KB，1 GB 約 20 萬筆。
- 快取保留 7 天 TTL，寫入時順手刪過期資料，或每日清理。
- 紀錄成長最快：每晚彙總成 `daily_stats`（請求數、命中、p50、p95、使用者數），原始紀錄只留 7 到 30 天。
- 額度表只留今天和昨天。

## 實作待辦（等使用者選定後再開工）
- [x] `PostgresStore`（`store.py` 新實作）：`client_id` 欄位用 `text`（不是 uuid）；雜湊以二補數存成有號 bigint（不能用「減 2^63」，平移會破壞 XOR），比對用 `bit_count((phash # $1)::bit(64)) <= 門檻`；資料量小時全表掃描即可（4×16 位元分段索引只保證距離 ≤ 3，門檻 6 要 7 段以上）。
- [x] `main.py` 用 `STORE=postgres` + `DATABASE_URL` 切換；`requirements.txt` 加 `psycopg[binary]`。
- [x] 測試：SQL 邏輯需要真的 Postgres，設 `TEST_DATABASE_URL` 時才跑（可連 Neon 的測試分支）；沒設就跳過，現有 37 項不受影響。
- [x] Render 部署設定（`render.yaml`）、環境變數清單、展示前喚醒說明。
- [x] 擴充功能：`host_permissions` 加 `https://*.onrender.com/*`，選項頁預設網址。
- [ ] 用 `eval_landmarks.py` 比較 Gemini 與 LM Studio 的辨識品質（期中報告素材）。
- [ ] 部署後用 `loadtest.py` 量雲端數據（注意 Render 冷啟動要排除）。

## 待使用者確認
1. 接受上面的推薦組合嗎？
2. 模型：線上版用 Gemini、課堂展示切回 LM Studio，可以嗎？還是課程規定一定要 LM Studio？
3. 需要使用者自己做的事：註冊 Render、Neon、Google AI Studio（取得金鑰），金鑰只放在 Render 的環境變數，不要貼給任何人。

## 實作紀錄（同日，第三輪子代理）
使用者同意推薦組合後，三個子代理並行，主對話整合：

- [x] **`PostgresStore`**（`backend/app/store_pg.py`、`tests/test_store_pg.py`；`psycopg[binary]==3.3.9`、`psycopg-pool==3.3.3`）
  - 連線池（1–4）、autocommit、`prepare_threshold=None`（Neon pooled 連線需要）、斷線重試一次。
  - 多了 `logs.day`（台灣日期）與 `clients` 表（`unique_clients` 在刪除舊紀錄後仍準確）。
  - `maintenance()` 每小時最多跑一次：刪過期快取、刪舊額度、彙總舊紀錄到 `daily_stats`、刪超過保留天數的原始紀錄。
  - 同距離時回傳最新的一筆（MemoryStore 回傳最舊的），這是唯一的行為差異。
- [x] **後端效能與合併**（`main.py`）
  - 解碼、dHash、低紋理、所有儲存層呼叫都改用 `run_in_threadpool`。
  - 同雜湊並行未命中合併：只有第一個請求扣額度並呼叫模型，其他等待後重查快取。
  - `store_from_env()`：`STORE=memory|postgres`，缺 `DATABASE_URL` 或未知值會報錯。
  - mock 壓測前後：客戶端 p95 約 386–564 ms → 345–356 ms（雜訊大）；伺服器端 p95 變大是因為現在包含 threadpool 等待，不能直接比。
- [x] **Render 與擴充功能**（`render.yaml`、`docs/deploy.md`、`.env.example`、`extension/*`）
  - `host_permissions`：移除 `*.run.app`、加入 `*.onrender.com`。
  - Chrome 背景腳本約 30 秒沒回應會被終止，所以喚醒用多次短的 `/health`（每次最多 20 秒、總共 90 秒），點圖示時先送暖機請求。
- 主對話整合時另外修正：
  - `providers.py`：400 只有在純提示詞重試成功時才永久關閉結構化輸出（Gemini 金鑰錯誤也回 400）；新增選用的 `LMSTUDIO_REASONING_EFFORT`；找不到模型的錯誤訊息改成通用說法。
  - `main.py`：模型呼叫途中被取消時退還額度（補測試，並確認移除修正後測試會失敗）；關閉時呼叫 `store.close()`；`LOG_RETENTION_DAYS`；刪掉重複的 `import asyncio`。
  - `content.js`：背景腳本被終止的英文錯誤換成中文說明。
  - Dockerfile 註解改成 Render。

## 測試基線（整合後）
- 沒有資料庫：**53 passed、15 skipped**。
- 設 `TEST_DATABASE_URL`（本機可攜式 Postgres 16.2）：**68 passed**。
- 端到端：`STORE=postgres` 啟動後端，跑 `loadtest.py`（150 請求、並行 10）：命中率 90%，資料庫中 15 筆快取、150 筆紀錄、額度合計 15，與客戶端一致。
- 本機 Postgres 是子代理從 pgserver 的 wheel 解出來的（在暫存目錄，不在專案裡）。換電腦要重測資料庫，可直接連 Neon 的測試分支設 `TEST_DATABASE_URL`。

## 還沒驗證
- Neon 本身（SSL、喚醒延遲、pooled 連線）、Render 實際部署、Docker 映像建置（Render 會在雲端建置）。
- Gemini 實測（結構化輸出、`reasoning_effort`、每日次數）。
- 擴充功能在真實 Chrome 的喚醒流程。

## 下一步
1. **使用者**：依 `docs/deploy.md` 註冊 Neon、Google AI Studio、Render，並把專案推上 GitHub；金鑰只填 Render 控制台。
2. 部署後一起驗證：`/health`、`/api/stats`、擴充功能指向 Render 網址、`eval_landmarks.py`（Gemini 與 LM Studio 比較）、`loadtest.py`（先暖機再量）。
3. 待使用者回答：課程是否規定全程使用 LM Studio。
