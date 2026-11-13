@echo off
chcp 65001 >nul
cd /d "%~dp0backend"

where py >nul 2>nul
if errorlevel 1 (
  echo [錯誤] 找不到 Python。請先安裝 Python 3.11 以上版本，並勾選 Add to PATH。
  if /i not "%~1"=="/nopause" pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 第一次執行：建立虛擬環境...
  py -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -m pip install -q -r requirements-dev.txt
python -m pytest -q
set RESULT=%errorlevel%

rem 自動化執行（CI、腳本）時加 /nopause 參數，避免卡在 pause
if /i not "%~1"=="/nopause" pause
exit /b %RESULT%
