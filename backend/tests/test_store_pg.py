"""PostgresStore 測試。

- 不需要資料庫的測試（雜湊有號／無號轉換、XOR 與漢明距離等價）一律執行。
- 需要真的 Postgres 的測試，只有設定環境變數 TEST_DATABASE_URL 時才執行，否則跳過。
  每次測試建立獨立的 schema（test_隨機碼），測完整個刪除，不會動到既有資料。
  例：TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres
  也可以連 Neon 的測試分支（網址需含 sslmode=require）。
"""
import os
import random
import threading
import uuid
from datetime import datetime

import pytest

from app.phash import hamming
from app.schemas import Character, Script, Step
from app.store import TZ, LogEntry, MemoryStore
from app.store_pg import PostgresStore, to_signed64, to_unsigned64

MASK64 = (1 << 64) - 1
EDGE_HASHES = [0, 1, (1 << 63) - 1, 1 << 63, (1 << 63) + 1, MASK64 - 1, MASK64]


# ---------- 不需要資料庫 ----------

def test_module_exports():
    assert PostgresStore.__name__ == "PostgresStore"
    from app.store import Store
    assert issubclass(PostgresStore, Store)


def test_signed_round_trip():
    rng = random.Random(42)
    for h in EDGE_HASHES + [rng.getrandbits(64) for _ in range(2000)]:
        s = to_signed64(h)
        assert -(1 << 63) <= s < (1 << 63)  # 放得進 bigint
        assert to_unsigned64(s) == h


def test_signed_conversion_rejects_out_of_range():
    for bad in (-1, 1 << 64):
        with pytest.raises(ValueError):
            to_signed64(bad)


def test_xor_bitcount_matches_hamming():
    """有號值 XOR 後取低 64 位元，等於無號值 XOR；
    這就是 Postgres 中 bit_count((a # b)::bit(64)) 的計算。"""
    rng = random.Random(7)
    pairs = [(a, b) for a in EDGE_HASHES for b in EDGE_HASHES]
    pairs += [(rng.getrandbits(64), rng.getrandbits(64)) for _ in range(2000)]
    for a, b in pairs:
        x = to_signed64(a) ^ to_signed64(b)
        assert -(1 << 63) <= x < (1 << 63)  # XOR 結果仍是合法 bigint
        assert (x & MASK64) == a ^ b
        assert (x & MASK64).bit_count() == hamming(a, b)


# ---------- 需要真的 Postgres ----------

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="未設定 TEST_DATABASE_URL，跳過真實資料庫測試")


class FakeClock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self):
        return self.t


def script(name="台北 101", confidence=0.9) -> Script:
    return Script(
        landmark_name=name,
        confidence=confidence,
        character=Character(name="小導", avatar_emotion="excited"),
        sequence_steps=[Step(step_id=1, dialogue=f"這是{name}", choices=[])],
    )


def ts(y, m, d, hh=12, mm=0):
    """台灣時間 → epoch 秒。"""
    return datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp()


@pytest.fixture
def make_store():
    created = []
    schema = f"test_{uuid.uuid4().hex[:12]}"

    def factory(**kw):
        kw.setdefault("clock", FakeClock(ts(2026, 11, 14)))
        s = PostgresStore(dsn=DSN, schema=schema, **kw)
        created.append(s)
        return s

    yield factory

    import psycopg
    from psycopg import sql
    for s in created:
        s.close()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@needs_db
def test_init_is_idempotent_and_exposes_limit(make_store):
    s1 = make_store(daily_limit=3)
    s1.cache_put(123, script())
    s2 = make_store(daily_limit=3)  # 同一個 schema 再建一次，不應出錯也不應清資料
    assert s2.daily_limit == 3
    assert s2.cache_get(123).landmark_name == "台北 101"


@needs_db
def test_cache_exact_fuzzy_and_threshold(make_store):
    s = make_store(hamming_threshold=6)
    base = 0xF0F0_F0F0_F0F0_F0F0  # 最高位元為 1，存成負的 bigint
    s.cache_put(base, script("圓山大飯店"))
    assert s.cache_get(base).landmark_name == "圓山大飯店"
    assert s.cache_get(base ^ 0b111111).landmark_name == "圓山大飯店"   # 距離 6：命中
    assert s.cache_get(base ^ 0b1111111) is None                       # 距離 7：未命中
    assert s.cache_get(base ^ (1 << 63)).landmark_name == "圓山大飯店"  # 翻轉正負號位元，距離 1


@needs_db
def test_cache_picks_nearest(make_store):
    s = make_store(hamming_threshold=6)
    s.cache_put(0, script("A"))
    s.cache_put(0b1111, script("B"))
    assert s.cache_get(0b1110).landmark_name == "B"  # 與 B 距離 1、與 A 距離 3
    assert s.cache_get(0b0001).landmark_name == "A"


@needs_db
def test_cache_matches_memory_store_on_random_hashes(make_store):
    rng = random.Random(1)
    clock = FakeClock(ts(2026, 11, 14))
    pg = make_store(clock=clock)
    mem = MemoryStore(clock=clock)
    stored = [rng.getrandbits(64) for _ in range(30)]
    for i, h in enumerate(stored):
        pg.cache_put(h, script(f"L{i}"))
        mem.cache_put(h, script(f"L{i}"))
    queries = [h ^ (1 << rng.randrange(64)) ^ (1 << rng.randrange(64)) for h in stored]
    queries += [rng.getrandbits(64) for _ in range(30)]
    for q in queries:
        a, b = pg.cache_get(q), mem.cache_get(q)
        assert (a is None) == (b is None)
        if a:  # 隨機雜湊彼此距離很遠，不會有平手
            assert a.landmark_name == b.landmark_name


@needs_db
def test_cache_ttl(make_store):
    clock = FakeClock(ts(2026, 11, 14))
    s = make_store(clock=clock, cache_ttl_s=100)
    s.cache_put(42, script())
    clock.t += 99
    assert s.cache_get(42) is not None
    clock.t += 2
    assert s.cache_get(42) is None
    assert s.stats()["cached_landmarks"] == 0


@needs_db
def test_cache_round_trips_script(make_store):
    s = make_store()
    original = script("101")
    s.cache_put(7, original)
    assert s.cache_get(7) == original


@needs_db
def test_quota_consume_refund_remaining(make_store):
    s = make_store(daily_limit=3)
    assert s.quota_remaining("c1") == 3
    assert s.quota_consume("c1") == (True, 2)
    assert s.quota_consume("c1") == (True, 1)
    assert s.quota_consume("c1") == (True, 0)
    assert s.quota_consume("c1") == (False, 0)
    assert s.quota_remaining("c1") == 0
    s.quota_refund("c1")
    assert s.quota_remaining("c1") == 1
    assert s.quota_remaining("c2") == 3  # 不同用戶互不影響


@needs_db
def test_quota_refund_never_below_zero(make_store):
    s = make_store(daily_limit=2)
    s.quota_refund("x")          # 沒有紀錄時退還：不出錯
    s.quota_consume("x")
    s.quota_refund("x")
    s.quota_refund("x")
    assert s.quota_remaining("x") == 2
    assert s.quota_consume("x") == (True, 1)


@needs_db
def test_quota_zero_limit(make_store):
    s = make_store(daily_limit=0)
    assert s.quota_consume("x") == (False, 0)
    assert s.quota_remaining("x") == 0


@needs_db
def test_quota_resets_at_taiwan_midnight(make_store):
    clock = FakeClock(ts(2026, 11, 14, 23, 59))
    s = make_store(daily_limit=1, clock=clock)
    assert s.quota_consume("c") == (True, 0)
    assert s.quota_consume("c") == (False, 0)
    clock.t = ts(2026, 11, 15, 0, 1)  # 台灣時間跨日（UTC 仍是 11/14）
    assert s.quota_remaining("c") == 1
    assert s.quota_consume("c") == (True, 0)


@needs_db
def test_quota_concurrent_consume_never_exceeds_limit(make_store):
    s = make_store(daily_limit=5, max_size=4)
    results = []
    lock = threading.Lock()

    def worker():
        r = s.quota_consume("race")
        with lock:
            results.append(r[0])

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 5
    assert s.quota_remaining("race") == 0


def _sample_logs(base):
    lat = [120, 30, 900, 45, 300, 60, 2000, 15, 75, 500, 33]
    return [
        LogEntry(client_id=f"c{i % 4}", landmark_name="101" if i % 3 else None,
                 cache_hit=(i % 2 == 0), latency_ms=v, created_at=base + i)
        for i, v in enumerate(lat)
    ]


@needs_db
def test_stats_empty(make_store):
    s = make_store()
    assert s.stats() == MemoryStore().stats()


@needs_db
def test_stats_match_memory_store(make_store):
    clock = FakeClock(ts(2026, 11, 14))
    pg = make_store(clock=clock)
    mem = MemoryStore(clock=clock)
    for e in _sample_logs(clock.t):
        pg.log(e)
        mem.log(e)
    for h in (1, 2):
        pg.cache_put(h, script())
        mem.cache_put(h, script())
    assert pg.stats() == mem.stats()


@needs_db
def test_maintenance_rollup_and_cleanup(make_store):
    clock = FakeClock(ts(2026, 11, 1))
    s = make_store(clock=clock, log_retention_days=3, cache_ttl_s=3600)
    mem = MemoryStore()
    for e in _sample_logs(clock.t):
        s.log(e)
        mem.log(e)
    s.cache_put(9, script())
    s.quota_consume("old")
    expected = mem.stats()

    clock.t = ts(2026, 11, 2)  # 隔天：11/1 的紀錄被彙總，但原始紀錄還在保留期內
    r = s.maintenance()
    assert r["days_rolled_up"] == 1 and r["cache_deleted"] == 1 and r["logs_deleted"] == 0
    st = s.stats()
    assert st["requests"] == expected["requests"]       # 不重複計算
    assert st["cache_hits"] == expected["cache_hits"]
    assert st["unique_clients"] == expected["unique_clients"]
    assert s.maintenance()["days_rolled_up"] == 0       # 已彙總的日子不重算

    clock.t = ts(2026, 11, 10)  # 超過保留期：原始紀錄與舊額度都刪掉，總數仍來自彙總
    r = s.maintenance()
    assert r["logs_deleted"] == len(_sample_logs(0)) and r["quota_deleted"] == 1
    st = s.stats()
    assert st["requests"] == expected["requests"]
    assert st["cache_hits"] == expected["cache_hits"]
    assert st["cache_hit_rate"] == expected["cache_hit_rate"]
    assert st["unique_clients"] == expected["unique_clients"]
    assert st["latency_ms_p50"] is None  # 百分位數只看保留中的原始紀錄


@needs_db
def test_opportunistic_maintenance_runs_at_most_hourly(make_store):
    clock = FakeClock(ts(2026, 11, 14))
    s = make_store(clock=clock, cache_ttl_s=10)
    s.cache_put(1, script())        # 第一次寫入就會順手維護
    clock.t += 60
    s.cache_put(2, script())        # 1 分鐘內不會再維護：過期的 1 號仍在表裡
    with s._pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM cache").fetchone()[0] == 2
    clock.t += 3600
    s.log(LogEntry("c", None, False, 10, clock.t))  # 超過 1 小時：再次維護，兩筆都過期被刪
    with s._pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM cache").fetchone()[0] == 0
