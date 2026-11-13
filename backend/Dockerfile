# 後端容器映像（預設部署目標：Google Cloud Run，平台會以 $PORT 指定埠號）
FROM python:3.12-slim

# 即時輸出日誌、不產生 .pyc、pip 不保留下載快取以縮小映像
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /srv

# 只安裝執行期套件（不含 pytest）；先複製 requirements.txt 以利用建置快取
COPY requirements.txt .
RUN pip install -r requirements.txt

# 建立非 root 使用者執行服務，降低容器被入侵時的權限
RUN useradd --create-home --uid 10001 appuser

COPY app ./app

USER appuser

# 用 exec 讓 uvicorn 取代 sh 成為主程序，才能正確收到平台送出的 SIGTERM 並優雅關閉
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
