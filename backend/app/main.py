import asyncio
import base64
import binascii
import logging
import os
import re
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from .phash import dhash, is_low_texture
from .providers import VisionProvider, get_provider
from .schemas import IdentifyRequest, IdentifyResponse, Quota
from .store import LogEntry, MemoryStore, Store, next_reset_iso

load_dotenv()  # 讀取 backend/.env（若存在）；系統環境變數優先
log = logging.getLogger("landmark_guide")

MAX_IMAGE_BYTES = 2 * 1024 * 1024   # 單張框選影像上限 2 MB
CACHE_MIN_CONFIDENCE = 0.6          # 信心不足或無法辨識的結果不寫入快取
LOW_TEXTURE_STDDEV = float(os.getenv("LOW_TEXTURE_STDDEV", "8"))  # 低於此亮度標準差不使用快取
CLIENT_ID_RE =re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def decode_image(data: str) -> bytes:
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="image 不是有效的 Base64")
    if not raw:
        raise HTTPException(status_code=422, detail="image 為空")
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="影像過大，請縮小框選範圍")
    return raw


def hash_image(raw: bytes) -> tuple[int, bool]:
    """計算 dHash 與是否可使用快取（CPU 密集，應在執行緒池中執行）。"""
    # 低紋理影像的雜湊幾乎都一樣，不查也不寫快取，避免彼此誤命中
    return dhash(raw), not is_low_texture(raw, LOW_TEXTURE_STDDEV)


def store_from_env() -> Store:
    """依環境變數 STORE 選擇儲存層：memory（預設）或 postgres。"""
    kind = os.getenv("STORE", "memory").strip().lower()
    kw = dict(
        daily_limit=int(os.getenv("DAILY_FREE_LIMIT", "5")),
        cache_ttl_s=int(os.getenv("CACHE_TTL_S", str(7 * 24 * 3600))),
        hamming_threshold=int(os.getenv("HAMMING_THRESHOLD", "6")),
    )
    if kind == "postgres":
        kw["log_retention_days"] = int(os.getenv("LOG_RETENTION_DAYS", "14"))
        dsn = os.getenv("DATABASE_URL", "").strip()
        if not dsn:
            # 先檢查設定再匯入：缺設定時給出清楚的錯誤，而不是 psycopg 匯入失敗
            raise RuntimeError("STORE=postgres 需要設定 DATABASE_URL（PostgreSQL 連線字串，例如 postgresql://user:pass@host:5432/db）")
        # 延遲匯入：只有使用 Postgres 時才需要 psycopg，mock／測試環境不必安裝
        from .store_pg import PostgresStore
        return PostgresStore(dsn=dsn, **kw)
    if kind != "memory":
        raise RuntimeError(f"未知的 STORE={kind!r}（可用：memory、postgres）")
    return MemoryStore(**kw)


def create_app(provider: VisionProvider | None = None, store: Store | None = None) -> FastAPI:
    provider = provider or get_provider()
    store = store or store_from_env()
    # 進行中的未命中請求：{phash: asyncio.Event}。同一張圖同時有多個請求未命中時，
    # 只有第一個（擁有者）扣額度並呼叫模型，其餘等它完成後重查快取。
    # 注意：
    # - 這只在「單一行程」內有效；多個執行個體或多個 uvicorn worker 之間不會合併，
    #   仍可能各自呼叫模型（要跨行程合併需要資料庫鎖或分散式鎖，目前不做）。
    # - 以「完全相同」的雜湊值為鍵；雜湊相近但不相等（例如縮放、壓縮後差 1～2 位元）
    #   的同時請求不會合併，只會在其中一個寫入快取後才命中。這是可接受的取捨。
    # - 只有可快取（非低紋理）的影像參與合併。
    inflight: dict[int, asyncio.Event] = {}
    @asynccontextmanager
    async def lifespan(_app):
        yield
        close = getattr(store, "close", None)  # PostgresStore 有連線池要關；MemoryStore 沒有
        if close:
            await run_in_threadpool(close)

    app = FastAPI(title="AI Landmark Guide API", version="0.2.0", lifespan=lifespan)
    # CORS 只是瀏覽器端加固，不是認證（擴充功能背景腳本有 host_permissions，不受 CORS 限制）。
    # ALLOWED_ORIGINS 以逗號分隔，例如 chrome-extension://<擴充功能 ID>；未設定時只允許任一擴充功能來源。
    origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=None if origins else r"^chrome-extension://[a-p]{32}$",
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Client-Id", "X-Admin-Token"],
    )
    limit = getattr(store, "daily_limit", 0)

    def quota_info(client_id: str) -> Quota:
        return Quota(limit=limit, remaining=store.quota_remaining(client_id), resets_at=next_reset_iso(time.time()))

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/identify", response_model=IdentifyResponse)
    async def identify(req: IdentifyRequest, x_client_id: str | None = Header(default=None)):
        if not x_client_id or not CLIENT_ID_RE.match(x_client_id):
            raise HTTPException(status_code=400, detail="缺少或格式錯誤的 X-Client-Id")
        # 這是 async 處理函式：CPU 密集的解碼／雜湊，以及所有 store 呼叫（同步 API，
        # 之後會連到遠端 Postgres）一律丟到執行緒池，避免阻塞事件迴圈、拖慢其他請求。
        # 每次切換執行緒都有成本（排程、GIL 競爭），所以把解碼、雜湊、查快取合併成一次。
        t0 = time.perf_counter()

        def prepare():
            raw = decode_image(req.image)
            ph, cacheable = hash_image(raw)
            return raw, ph, cacheable, (store.cache_get(ph) if cacheable else None)

        raw, ph, cacheable, script = await run_in_threadpool(prepare)
        waited = False
        if script is None and cacheable and (pending := inflight.get(ph)) is not None:
            # 同一張圖正由其他請求辨識中：先等它結束（此時尚未扣本請求的額度），再重查快取。
            # 若對方失敗、信心不足或額度不足而沒有寫入快取，本請求就照一般流程自行處理。
            await pending.wait()
            waited = True
            script = await run_in_threadpool(store.cache_get, ph)
        hit = script is not None

        if not hit:
            owner = None
            if cacheable and ph not in inflight:
                # 「檢查 + 登記」之間沒有 await，在單一事件迴圈中是原子的
                owner = inflight[ph] = asyncio.Event()
            try:
                if owner is not None and not waited:
                    # 雙重檢查：第一次查快取（在執行緒池中）期間，前一個擁有者可能剛寫入快取並離開
                    script = await run_in_threadpool(store.cache_get, ph)
                    hit = script is not None
                if not hit:
                    script = await identify_miss(raw, ph, cacheable, req, x_client_id)
            finally:
                # 不論成功、失敗、例外或取消都要清掉登記並喚醒等待者，避免永久卡住
                if owner is not None:
                    if inflight.get(ph) is owner:
                        del inflight[ph]
                    owner.set()

        latency = int((time.perf_counter() - t0) * 1000)

        def finish() -> Quota:
            store.log(LogEntry(x_client_id, script.landmark_name, hit, latency, time.time()))
            return quota_info(x_client_id)

        quota = await run_in_threadpool(finish)
        return IdentifyResponse(**script.model_dump(), cache_hit=hit, image_hash=f"{ph:016x}", quota=quota)

    async def identify_miss(raw: bytes, ph: int, cacheable: bool, req: IdentifyRequest, client_id: str):
        """未命中快取：扣額度 → 呼叫模型 → 視結果寫入快取。"""
        # 只有「會產生 API 費用」的未命中請求才扣額度；命中快取不扣
        allowed, _ = await run_in_threadpool(store.quota_consume, client_id)
        if not allowed:
            quota = await run_in_threadpool(quota_info, client_id)
            raise HTTPException(status_code=429, detail={
                "message": "今日免費額度已用完",
                "quota": quota.model_dump(),
            })
        try:
            script = await provider.identify(raw, req.page_url, req.page_title)
        except asyncio.CancelledError:
            # 使用者中斷連線等情況：沒有拿到結果，同樣退還額度。已在取消中，直接同步呼叫，不再 await
            store.quota_refund(client_id)
            raise
        except Exception:
            log.exception("模型辨識失敗（provider=%s）", type(provider).__name__)
            await run_in_threadpool(store.quota_refund, client_id)  # 辨識失敗不應吃掉使用者額度
            raise HTTPException(status_code=502, detail="辨識服務暫時無法使用，請稍後再試")
        if cacheable and script.landmark_name is not None and script.confidence >= CACHE_MIN_CONFIDENCE:
            # 必須在喚醒等待者（finally 中的 owner.set()）之前寫入，等待者重查時才會命中
            await run_in_threadpool(store.cache_put, ph, script)
        return script

    @app.get("/api/stats")
    def stats(x_admin_token: str | None = Header(default=None)):
        token = os.getenv("ADMIN_TOKEN")
        if token and x_admin_token != token:
            raise HTTPException(status_code=403, detail="需要管理者權杖")
        return store.stats()

    return app


app = create_app()
