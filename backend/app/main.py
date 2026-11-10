import base64
import binascii
import logging
import os
import re
import time

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .phash import dhash
from .providers import VisionProvider, get_provider
from .schemas import IdentifyRequest, IdentifyResponse, Quota
from .store import LogEntry, MemoryStore, Store, next_reset_iso

load_dotenv()  # 讀取 backend/.env（若存在）；系統環境變數優先
log = logging.getLogger("landmark_guide")

MAX_IMAGE_BYTES = 2 * 1024 * 1024   # 單張框選影像上限 2 MB
CACHE_MIN_CONFIDENCE = 0.6          # 信心不足或無法辨識的結果不寫入快取
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


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


def create_app(provider: VisionProvider | None = None, store: Store | None = None) -> FastAPI:
    provider = provider or get_provider()
    store = store or MemoryStore(
        daily_limit=int(os.getenv("DAILY_FREE_LIMIT", "5")),
        cache_ttl_s=int(os.getenv("CACHE_TTL_S", str(7 * 24 * 3600))),
        hamming_threshold=int(os.getenv("HAMMING_THRESHOLD", "6")),
    )
    app = FastAPI(title="AI Landmark Guide API", version="0.2.0")
    # MVP 階段開放所有來源；上線前應限縮為 Extension 的 chrome-extension://<id>
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
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
        raw = decode_image(req.image)
        t0 = time.perf_counter()
        ph = dhash(raw)

        script = store.cache_get(ph)
        hit = script is not None
        if not hit:
            # 只有「會產生 API 費用」的未命中請求才扣額度；命中快取不扣
            allowed, _ = store.quota_consume(x_client_id)
            if not allowed:
                raise HTTPException(status_code=429, detail={
                    "message": "今日免費額度已用完",
                    "quota": quota_info(x_client_id).model_dump(),
                })
            try:
                script = await provider.identify(raw, req.page_url)
            except Exception:
                log.exception("模型辨識失敗（provider=%s）", type(provider).__name__)
                store.quota_refund(x_client_id)  # 辨識失敗不應吃掉使用者額度
                raise HTTPException(status_code=502, detail="辨識服務暫時無法使用，請稍後再試")
            if script.landmark_name is not None and script.confidence >= CACHE_MIN_CONFIDENCE:
                store.cache_put(ph, script)

        latency = int((time.perf_counter() - t0) * 1000)
        store.log(LogEntry(x_client_id, script.landmark_name, hit, latency, time.time()))
        return IdentifyResponse(**script.model_dump(), cache_hit=hit, image_hash=f"{ph:016x}",
                                quota=quota_info(x_client_id))

    @app.get("/api/stats")
    def stats(x_admin_token: str | None = Header(default=None)):
        token = os.getenv("ADMIN_TOKEN")
        if token and x_admin_token != token:
            raise HTTPException(status_code=403, detail="需要管理者權杖")
        return store.stats()

    return app


app = create_app()
