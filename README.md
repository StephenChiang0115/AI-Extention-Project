# AI 景點互動導覽系統（MVP）

在任何網頁框選地標，AI 角色即時辨識並用對話導覽。目前完成階段 1 到 3，詳細進度、設計決定與已知限制請看 `CLAUDE.md`。

## 快速開始（Windows）
1. 雙擊 `start-backend.bat`：第一次會建立虛擬環境、安裝套件，預設 mock 模式，不需要金鑰。看到 `http://localhost:8080` 就代表啟動了。
2. 開啟 Chrome 的 `chrome://extensions`，開啟右上角「開發人員模式」，點「載入未封裝項目」，選擇本資料夾裡的 `extension`。
3. 在任何 http／https 網頁點工具列的擴充功能圖示（或按 Alt+Shift+L），拖曳框選畫面。mock 模式下不論框選什麼，都會回傳「台北 101」，用來驗證流程。
4. 想看統計：瀏覽器打開 `http://localhost:8080/api/stats`。

執行測試：雙擊 `run-tests.bat`（16 項）。

## 設定
複製 `backend/.env.example` 成 `backend/.env`（第一次啟動時會自動建立），可調整每日免費額度、快取存活時間、雜湊門檻等。金鑰只放這個檔案，不要提交到版本控制。

## 目錄
- `backend/`：FastAPI 後端（`app/`）、測試（`tests/`）、`Dockerfile`
- `extension/`：Chrome 擴充功能
- `CLAUDE.md`：專案背景與接續工作的說明

## 進度
- [x] 階段 1：後端骨架
- [x] 階段 2：Chrome Extension
- [x] 階段 3：快取與每日額度
- [ ] 階段 4：部署到雲端
- [ ] 階段 5：壓測與成本統計
- [ ] 階段 6：接上真實模型（LM Studio）
