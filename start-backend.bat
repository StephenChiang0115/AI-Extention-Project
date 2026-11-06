@echo off
chcp 65001 >nul
cd /d "%~dp0backend"

where py >nul 2>nul
if errorlevel 1 (
  echo [錯誤] 找不到 Python。請先安裝 Python 3.10 以上版本，並勾選 Add to PATH。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 第一次執行：建立虛擬環境...
  py -m venv .venv
)

call ".venv\Scripts\activate.bat"
echo 安裝或更新套件...
python -m pip install -q -r requirements.txt

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo 已建立 backend\.env（預設 mock 模式，不需要金鑰）。
)

echo.
echo 後端啟動中：http://localhost:8080  （按 Ctrl+C 停止）
python -m uvicorn app.main:app --port 8080 --reload
pause
