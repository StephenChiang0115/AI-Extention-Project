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
- [ ] `PostgresStore`（`store.py` 新實作）：`client_id` 欄位用 `text`（不是 uuid）；雜湊存 bigint（減 2^63），比對用 `bit_count((phash # $1)::bit(64)) <= 門檻`；資料量小時全表掃描即可（4×16 位元分段索引只保證距離 ≤ 3，門檻 6 要 7 段以上）。
- [ ] `main.py` 用 `STORE=postgres` + `DATABASE_URL` 切換；`requirements.txt` 加 `psycopg[binary]`。
- [ ] 測試：SQL 邏輯需要真的 Postgres，設 `TEST_DATABASE_URL` 時才跑（可連 Neon 的測試分支）；沒設就跳過，現有 37 項不受影響。
- [ ] Render 部署設定（`render.yaml`）、環境變數清單、展示前喚醒說明。
- [ ] 擴充功能：`host_permissions` 加 `https://*.onrender.com/*`，選項頁預設網址。
- [ ] 用 `eval_landmarks.py` 比較 Gemini 與 LM Studio 的辨識品質（期中報告素材）。
- [ ] 部署後用 `loadtest.py` 量雲端數據（注意 Render 冷啟動要排除）。

## 待使用者確認
1. 接受上面的推薦組合嗎？
2. 模型：線上版用 Gemini、課堂展示切回 LM Studio，可以嗎？還是課程規定一定要 LM Studio？
3. 需要使用者自己做的事：註冊 Render、Neon、Google AI Studio（取得金鑰），金鑰只放在 Render 的環境變數，不要貼給任何人。
