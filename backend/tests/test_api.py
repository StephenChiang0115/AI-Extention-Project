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
