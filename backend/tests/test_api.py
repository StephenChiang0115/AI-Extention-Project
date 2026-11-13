import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import create_app
from app.phash import dhash, hamming
from app.providers import MockProvider, VisionProvider
from app.schemas import Character, Script
from app.store import MemoryStore

H = {"X-Client-Id": "client-abcdefgh"}


def b64(img: Image.Image, fmt="PNG") -> str:
    buf = io.BytesIO()
    img.save(buf, fmt)
    return base64.b64encode(buf.getvalue()).decode()


def tower() -> Image.Image:
    """垂直結構明顯的圖，模擬一座塔。"""
    img = Image.new("RGB", (240, 240), (200, 220, 255))
    d = ImageDraw.Draw(img)
    for i, x in enumerate(range(20, 220, 24)):
        d.rectangle([x, 240 - 40 - i * 14, x + 14, 240], fill=(40 + i * 10, 40, 90))
    d.polygon([(120, 10), (100, 60), (140, 60)], fill=(250, 200, 0))
    return img


def bridge() -> Image.Image:
    """水平結構明顯的圖，模擬一座橋，與 tower 差異大。"""
    img = Image.new("RGB", (240, 240), (255, 230, 200))
    d = ImageDraw.Draw(img)
    for i, y in enumerate(range(20, 220, 24)):
        d.rectangle([0, y, 60 + i * 15, y + 12], fill=(30, 100 + i * 8, 60))
    return img


class CountingProvider(VisionProvider):
    def __init__(self, inner=None, fail=False, conf=0.93, name="台北 101"):
        self.calls, self.fail, self.conf, self.name = 0, fail, conf, name
        self.inner = inner or MockProvider()

    async def identify(self, image_bytes, page_url, page_title=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        s = await self.inner.identify(image_bytes, page_url, page_title)
        return s.model_copy(update={"confidence": self.conf, "landmark_name": self.name})


def make(limit=3, **kw):
    prov = CountingProvider(**kw)
    store = MemoryStore(daily_limit=limit)
    return TestClient(create_app(prov, store)), prov, store


# ---------- 基本 ----------
def test_health():
    c, _, _ = make()
    assert c.get("/health").json() == {"status": "ok"}


def test_requires_client_id():
    c, _, _ = make()
    assert c.post("/api/identify", json={"image": b64(tower())}).status_code == 400
    assert c.post("/api/identify", json={"image": b64(tower())}, headers={"X-Client-Id": "x"}).status_code == 400


def test_identify_ok_and_schema():
    c, _, _ = make()
    r = c.post("/api/identify", json={"image": b64(tower()), "pageUrl": "https://youtube.com/x"}, headers=H)
    assert r.status_code == 200
    b = r.json()
    assert b["landmark_name"] == "台北 101" and b["cache_hit"] is False
    assert len(b["image_hash"]) == 16
    assert b["quota"]["limit"] == 3 and b["quota"]["remaining"] == 2


def test_accepts_data_url_and_rejects_bad_input():
    c, _, _ = make()
    assert c.post("/api/identify", json={"image": "data:image/png;base64," + b64(tower())}, headers=H).status_code == 200
    assert c.post("/api/identify", json={"image": "!!!"}, headers=H).status_code == 422
    big = base64.b64encode(b"a" * (2 * 1024 * 1024 + 1)).decode()
    assert c.post("/api/identify", json={"image": big}, headers=H).status_code == 413


# ---------- 感知雜湊 ----------
def test_phash_similar_vs_different():
    a = dhash(_raw(tower()))
    resized = tower().resize((160, 160))
    jpeg = Image.open(io.BytesIO(_raw(resized, "JPEG", q=70)))
    assert hamming(a, dhash(_raw(jpeg))) <= 6, "同一張圖縮放並壓縮後應仍接近"
    assert hamming(a, dhash(_raw(bridge()))) > 6, "不同圖應明顯不同"


def _raw(img, fmt="PNG", q=None):
    buf = io.BytesIO()
    img.save(buf, fmt, **({"quality": q} if q else {}))
    return buf.getvalue()


# ---------- 快取 ----------
def test_cache_hit_for_similar_image_skips_provider_and_quota():
    c, prov, _ = make(limit=3)
    c.post("/api/identify", json={"image": b64(tower())}, headers=H)
    similar = Image.open(io.BytesIO(_raw(tower().resize((180, 180)), "JPEG", q=75)))
    r = c.post("/api/identify", json={"image": b64(similar, "JPEG")}, headers=H).json()
    assert r["cache_hit"] is True
    assert prov.calls == 1
    assert r["quota"]["remaining"] == 2, "命中快取不應扣額度"


def test_different_image_misses_cache():
    c, prov, _ = make()
    c.post("/api/identify", json={"image": b64(tower())}, headers=H)
    r = c.post("/api/identify", json={"image": b64(bridge())}, headers=H).json()
    assert r["cache_hit"] is False and prov.calls == 2


def test_low_confidence_and_unknown_not_cached():
    for kw in ({"conf": 0.3}, {"name": None}):
        c, prov, store = make(**kw)
        c.post("/api/identify", json={"image": b64(tower())}, headers=H)
        c.post("/api/identify", json={"image": b64(tower())}, headers=H)
        assert prov.calls == 2, f"{kw} 不應被快取"
        assert store.stats()["cached_landmarks"] == 0


def test_cache_expires():
    now = [1_000_000.0]
    prov = CountingProvider()
    store = MemoryStore(daily_limit=9, cache_ttl_s=100, clock=lambda: now[0])
    c = TestClient(create_app(prov, store))
    c.post("/api/identify", json={"image": b64(tower())}, headers=H)
    now[0] += 101
    c.post("/api/identify", json={"image": b64(tower())}, headers=H)
    assert prov.calls == 2


# ---------- 每日額度 ----------
def test_quota_exhausted_returns_429():
    c, prov, _ = make(limit=2)
    for img in (tower(), bridge()):
        assert c.post("/api/identify", json={"image": b64(img)}, headers=H).status_code == 200
    third = Image.new("RGB", (240, 240), (10, 10, 10))
    ImageDraw.Draw(third).ellipse([40, 40, 200, 200], fill=(255, 0, 0))
    r = c.post("/api/identify", json={"image": b64(third)}, headers=H)
    assert r.status_code == 429
    d = r.json()["detail"]
    assert d["message"] == "今日免費額度已用完" and d["quota"]["remaining"] == 0
    assert prov.calls == 2


def test_cache_hit_still_works_after_quota_exhausted():
    c, _, _ = make(limit=1)
    assert c.post("/api/identify", json={"image": b64(tower())}, headers=H).status_code == 200
    assert c.post("/api/identify", json={"image": b64(tower())}, headers=H).status_code == 200  # 命中快取


def test_quota_is_per_client():
    c, _, _ = make(limit=1)
    c.post("/api/identify", json={"image": b64(tower())}, headers=H)
    r = c.post("/api/identify", json={"image": b64(bridge())}, headers={"X-Client-Id": "another-client-1"})
    assert r.status_code == 200


def test_quota_resets_next_day():
    now = [1_700_000_000.0]
    store = MemoryStore(daily_limit=1, clock=lambda: now[0])
    c = TestClient(create_app(CountingProvider(), store))
    c.post("/api/identify", json={"image": b64(tower())}, headers=H)
    assert c.post("/api/identify", json={"image": b64(bridge())}, headers=H).status_code == 429
    now[0] += 24 * 3600
    assert c.post("/api/identify", json={"image": b64(bridge())}, headers=H).status_code == 200


def test_provider_failure_refunds_quota():
    c, _, store = make(limit=2, fail=True)
    r = c.post("/api/identify", json={"image": b64(tower())}, headers=H)
    assert r.status_code == 502
    assert store.quota_remaining("client-abcdefgh") == 2


# ---------- 統計 ----------
def test_stats(monkeypatch):
    c, _, _ = make()
    c.post("/api/identify", json={"image": b64(tower())}, headers=H)
    c.post("/api/identify", json={"image": b64(tower())}, headers=H)
    s = c.get("/api/stats").json()
    assert s["requests"] == 2 and s["cache_hits"] == 1 and s["cache_hit_rate"] == 0.5
    assert s["unique_clients"] == 1 and s["cached_landmarks"] == 1


def test_stats_requires_token_when_set(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    c, _, _ = make()
    assert c.get("/api/stats").status_code == 403
    assert c.get("/api/stats", headers={"X-Admin-Token": "secret"}).status_code == 200


def test_low_texture_detection():
    from app.phash import is_low_texture
    raw = lambda img: base64.b64decode(b64(img))
    assert is_low_texture(raw(Image.new("RGB", (200, 200), (135, 206, 235))))
    assert is_low_texture(raw(Image.new("RGB", (200, 200), (0, 0, 0))))
    assert not is_low_texture(raw(tower())) and not is_low_texture(raw(bridge()))
    assert not is_low_texture(b"not an image")


def test_low_texture_image_skips_cache():
    c, prov, store = make(limit=9)
    sky = b64(Image.new("RGB", (200, 200), (135, 206, 235)))
    black = b64(Image.new("RGB", (200, 200), (0, 0, 0)))
    for img in (sky, sky, black):
        r = c.post("/api/identify", json={"image": img, "page_url": "https://x.test"}, headers=H).json()
        assert r["cache_hit"] is False
    assert prov.calls == 3 and store.stats()["cached_landmarks"] == 0


def test_cors_defaults_to_extension_origins(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    c, _, _ = make()
    ext = "chrome-extension://" + "a" * 32
    assert c.get("/health", headers={"Origin": ext}).headers.get("access-control-allow-origin") == ext
    assert "access-control-allow-origin" not in c.get("/health", headers={"Origin": "https://evil.test"}).headers


def test_cors_uses_allowed_origins_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://a.test, https://b.test")
    c, _, _ = make()
    assert c.get("/health", headers={"Origin": "https://b.test"}).headers.get("access-control-allow-origin") == "https://b.test"
    ext = "chrome-extension://" + "a" * 32
    assert "access-control-allow-origin" not in c.get("/health", headers={"Origin": ext}).headers


# ---------- 並行未命中合併（in-flight coalescing） ----------
import asyncio

import httpx


class SlowProvider(VisionProvider):
    """會等待一段時間的假模型；outcomes 依序指定每次呼叫的結果：'ok'、'fail'、'low'。"""

    def __init__(self, outcomes=("ok",), delay=0.2):
        self.calls, self.outcomes, self.delay = 0, list(outcomes), delay
        self.inner = MockProvider()

    async def identify(self, image_bytes, page_url, page_title=None):
        kind = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        await asyncio.sleep(self.delay)
        if kind == "fail":
            raise RuntimeError("boom")
        s = await self.inner.identify(image_bytes, page_url, page_title)
        return s.model_copy(update={"confidence": 0.3 if kind == "low" else 0.93, "landmark_name": "台北 101"})


def _concurrent(prov, store, n=2, image=None):
    """以 httpx.AsyncClient + ASGITransport 同時送出 n 個相同影像的請求。"""
    app = create_app(prov, store)
    body = {"image": image or b64(tower())}

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await asyncio.gather(*[c.post("/api/identify", json=body, headers=H) for _ in range(n)])

    return asyncio.run(run())


def test_concurrent_same_image_calls_model_once():
    prov, store = SlowProvider(["ok"]), MemoryStore(daily_limit=5)
    rs = _concurrent(prov, store)
    assert [r.status_code for r in rs] == [200, 200]
    assert prov.calls == 1, "同一張圖同時未命中，只應呼叫一次模型"
    assert sorted(r.json()["cache_hit"] for r in rs) == [False, True]
    assert store.quota_remaining("client-abcdefgh") == 4, "只應扣一次額度"


def test_concurrent_first_fails_second_processes_itself():
    prov, store = SlowProvider(["fail", "ok"]), MemoryStore(daily_limit=5)
    rs = _concurrent(prov, store)
    assert sorted(r.status_code for r in rs) == [200, 502]
    assert prov.calls == 2, "第一個失敗後，等待者應自行呼叫模型"
    ok = next(r for r in rs if r.status_code == 200).json()
    assert ok["cache_hit"] is False
    assert store.quota_remaining("client-abcdefgh") == 4, "失敗的那次應退還額度，只扣成功的一次"


def test_concurrent_low_confidence_second_calls_model_itself():
    prov, store = SlowProvider(["low", "ok"]), MemoryStore(daily_limit=5)
    rs = _concurrent(prov, store)
    assert [r.status_code for r in rs] == [200, 200]
    assert prov.calls == 2, "信心不足不寫入快取，等待者應自行呼叫模型"
    assert [r.json()["cache_hit"] for r in rs] == [False, False]
    assert store.quota_remaining("client-abcdefgh") == 3


def test_concurrent_low_texture_not_coalesced():
    prov, store = SlowProvider(["ok"]), MemoryStore(daily_limit=5)
    sky = b64(Image.new("RGB", (200, 200), (135, 206, 235)))
    rs = _concurrent(prov, store, image=sky)
    assert prov.calls == 2 and all(r.json()["cache_hit"] is False for r in rs)


def test_inflight_cleaned_up_after_exception():
    """擁有者發生例外後，之後的請求不應卡住或被錯誤合併。"""
    prov, store = SlowProvider(["fail", "fail", "ok"], delay=0.05), MemoryStore(daily_limit=5)
    app = create_app(prov, store)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            body = {"image": b64(tower())}
            r1 = await c.post("/api/identify", json=body, headers=H)
            r2 = await asyncio.wait_for(c.post("/api/identify", json=body, headers=H), timeout=5)
            r3 = await asyncio.wait_for(c.post("/api/identify", json=body, headers=H), timeout=5)
            return r1, r2, r3

    r1, r2, r3 = asyncio.run(run())
    assert (r1.status_code, r2.status_code, r3.status_code) == (502, 502, 200)
    assert store.quota_remaining("client-abcdefgh") == 4


# ---------- 儲存層選擇 ----------
def test_store_postgres_requires_database_url(monkeypatch):
    monkeypatch.setenv("STORE", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        create_app(MockProvider())


def test_store_defaults_to_memory(monkeypatch):
    from app.main import store_from_env
    monkeypatch.delenv("STORE", raising=False)
    monkeypatch.setenv("DAILY_FREE_LIMIT", "7")
    s = store_from_env()
    assert isinstance(s, MemoryStore) and s.daily_limit == 7


def test_store_unknown_value_rejected(monkeypatch):
    monkeypatch.setenv("STORE", "postgress")
    with pytest.raises(RuntimeError, match="STORE"):
        create_app(MockProvider())


def test_cancelled_model_call_refunds_quota():
    """請求在模型呼叫途中被取消（例如使用者關掉分頁），額度要退還。"""
    import asyncio

    import httpx

    class Slow(CountingProvider):
        async def identify(self, image_bytes, page_url, page_title=None):
            self.calls += 1
            await asyncio.sleep(10)

    prov, store = Slow(), MemoryStore(daily_limit=3)
    app = create_app(prov, store)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            task = asyncio.create_task(c.post("/api/identify", json={"image": b64(tower())}, headers=H))
            while prov.calls == 0:
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(go())
    assert prov.calls == 1 and store.quota_remaining(H["X-Client-Id"]) == 3
