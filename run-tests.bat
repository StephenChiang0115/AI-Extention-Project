@echo off
chcp 65001 >nul
cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
  echo 第一次執行：建立虛擬環境...
  py -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -m pip install -q -r requirements.txt
python -m pytest -q
pause
