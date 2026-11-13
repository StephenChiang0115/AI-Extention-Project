"""Postgres 儲存層（部署用，對應 Neon 免費方案）。

與 MemoryStore 語意相同：快取 TTL、漢明距離門檻內取最接近的一筆、
每日額度以台灣日期計算、退還額度不會低於 0、統計欄位相同。

設計重點：
- 雜湊：dHash 是無號 64 位元整數，Postgres 沒有無號 bigint，
  所以用二補數存成有號 bigint（h >= 2^63 時減 2^64）。二補數對 XOR 保持不變，
  bigint 轉 bit(64) 也是取原始 64 個位元，因此 bit_count((a # b)::bit(64)) 等於漢明距離。
  （注意：不能用「減 2^63」的平移法，平移會破壞 XOR。）
- 額度：用 INSERT ... ON CONFLICT DO UPDATE ... WHERE used < 上限 RETURNING，
  一條 SQL 完成「檢查＋扣除」，多個執行個體同時請求也不會超扣。
- 「今天」由 Python 的 today_key(clock()) 計算，與 MemoryStore 完全一致，也方便測試注入時鐘。
- 所有方法都是同步的（main.py 以 run_in_threadpool 呼叫），連線池本身是執行緒安全的。
- Neon 閒置 5 分鐘後運算會休眠並切斷連線：連線池借出前先檢查連線（check_connection），
  另外遇到連線層錯誤（OperationalError）會重試一次。
- 資料成長控制（守住 Neon 1 GB）：maintenance() 刪除過期快取、舊額度，
  把今天以前的紀錄彙總到 daily_stats，並刪除超過保留天數的原始紀錄。
  由 cache_put / log 順手觸發，最多每小時一次，不需要排程工具。
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .schemas import Script
from .store import LogEntry, Store, today_key

logger = logging.getLogger(__name__)

_U64 = 1 << 64
_I64_MAX = (1 << 63) - 1

MAINTENANCE_INTERVAL_S = 3600  # 順手維護的最短間隔（秒）


def to_signed64(h: int) -> int:
    """無號 64 位元 → 有號 bigint（二補數）。"""
    if not 0 <= h < _U64:
        raise ValueError(f"雜湊必須是 0 到 2^64-1 的整數：{h}")
    return h - _U64 if h > _I64_MAX else h


def to_unsigned64(s: int) -> int:
    """有號 bigint → 無號 64 位元（to_signed64 的反函數）。"""
    return s + _U64 if s < 0 else s


def _day(now: float) -> date:
    return date.fromisoformat(today_key(now))


def _ts(t: float) -> datetime:
    return datetime.fromtimestamp(t, timezone.utc)


# 快取的模糊比對採全表掃描：每筆只算一次 XOR 與 bit_count，幾萬筆內仍是毫秒等級。
# 為什麼不用「把 64 位元切成 4 段 16 位元、任一段完全相同才比對」的分段索引：
# 依鴿籠原理，切成 k 段只保證距離 ≤ k-1 的雜湊至少有一段完全相同。
# 4 段只涵蓋距離 ≤ 3；門檻 6 的差異可能平均散在 4 段裡，會漏掉命中。
# 門檻 6 至少要切 7 段（每段約 9 位元），索引數量與候選筆數都變多，現階段不划算。
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cache (
    id            bigserial PRIMARY KEY,
    phash         bigint      NOT NULL,
    script        jsonb       NOT NULL,
    landmark_name text,
    created_at    timestamptz NOT NULL,
    expires_at    timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS cache_expires_at_idx ON cache (expires_at);

CREATE TABLE IF NOT EXISTS quota (
    client_id text NOT NULL,
    day       date NOT NULL,
    used      int  NOT NULL,
    PRIMARY KEY (client_id, day)
);

-- 原始辨識紀錄；day 為台灣日期，由 Python 計算，與額度的日期定義一致
CREATE TABLE IF NOT EXISTS logs (
    id            bigserial PRIMARY KEY,
    client_id     text        NOT NULL,
    landmark_name text,
    cache_hit     boolean     NOT NULL,
    latency_ms    int         NOT NULL,
    created_at    timestamptz NOT NULL,
    day           date        NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_day_idx ON logs (day);

-- 每日彙總（今天以前的紀錄），原始紀錄刪除後統計仍保留
CREATE TABLE IF NOT EXISTS daily_stats (
    day            date PRIMARY KEY,
    requests       int  NOT NULL,
    cache_hits     int  NOT NULL,
    unique_clients int  NOT NULL,
    p50            int,
    p95            int
);

-- 出現過的用戶（只有隨機 UUID），讓 unique_clients 在原始紀錄刪除後仍是精確的歷來總數
CREATE TABLE IF NOT EXISTS clients (
    client_id  text PRIMARY KEY,
    first_seen timestamptz NOT NULL
);
"""


class PostgresStore(Store):
    def __init__(
        self,
        dsn: str,
        daily_limit: int = 5,
        cache_ttl_s: int = 7 * 24 * 3600,
        hamming_threshold: int = 6,
        log_retention_days: int = 14,
        clock=time.time,
        schema: str | None = None,
        min_size: int = 1,
        max_size: int = 4,
    ):
        """schema：只給測試用，把所有資料表建在獨立的 schema，測完整個刪掉。
        正式環境請留 None（Neon 的連線池模式下 SET search_path 不可靠）。"""
        self.daily_limit = daily_limit
        self.cache_ttl_s = cache_ttl_s
        self.hamming_threshold = hamming_threshold
        self.log_retention_days = log_retention_days
        self.clock = clock
        self.schema = schema
        self._last_maintenance: float | None = None
        self._maint_lock = threading.Lock()

        configure = None
        if schema:
            def configure(conn):
                conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))

        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=15,  # Neon 從休眠喚醒通常 < 1 秒，保留餘裕
            check=ConnectionPool.check_connection,  # 借出前確認連線還活著（Neon 休眠會切斷）
            configure=configure,
            # autocommit：每條 SQL 各自成交易，不會留下閒置中的交易。
            # prepare_threshold=None：關閉伺服器端預備敘述，相容 Neon 的 PgBouncer 連線池網址。
            kwargs={"autocommit": True, "prepare_threshold": None},
            open=True,
        )
        try:
            self._init_schema()
        except Exception:
            self._pool.close()
            raise

    # ---------- 內部工具 ----------

    def _run(self, fn):
        """借一條連線執行 fn(conn)；連線層錯誤（例如 Neon 休眠切斷）重試一次。"""
        for attempt in range(2):
            try:
                with self._pool.connection() as conn:
                    return fn(conn)
            except psycopg.OperationalError:
                if attempt:
                    raise
                logger.warning("資料庫連線中斷，重試一次", exc_info=True)

    def _init_schema(self):
        def go(conn):
            with conn.transaction():
                if self.schema:
                    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
                conn.execute(_SCHEMA_SQL)
        self._run(go)

    def _maybe_maintain(self):
        now = self.clock()
        with self._maint_lock:
            if self._last_maintenance is not None and now - self._last_maintenance < MAINTENANCE_INTERVAL_S:
                return
            self._last_maintenance = now
        try:
            self.maintenance()
        except Exception:  # 維護失敗不應影響使用者請求
            logger.warning("資料庫維護失敗", exc_info=True)

    def close(self):
        self._pool.close()

    # ---------- 快取 ----------

    def cache_get(self, phash):
        q = to_signed64(phash)
        now = _ts(self.clock())

        def go(conn):
            return conn.execute(
                """
                SELECT script FROM cache
                WHERE expires_at > %(now)s
                  AND bit_count((phash # %(h)s)::bit(64)) <= %(t)s
                ORDER BY bit_count((phash # %(h)s)::bit(64)), created_at DESC, id DESC
                LIMIT 1
                """,
                {"now": now, "h": q, "t": self.hamming_threshold},
            ).fetchone()

        row = self._run(go)
        return Script.model_validate(row[0]) if row else None

    def cache_put(self, phash, script):
        now = self.clock()

        def go(conn):
            conn.execute(
                "INSERT INTO cache (phash, script, landmark_name, created_at, expires_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (to_signed64(phash), Jsonb(script.model_dump(mode="json")), script.landmark_name,
                 _ts(now), _ts(now + self.cache_ttl_s)),
            )
        self._run(go)
        self._maybe_maintain()

    # ---------- 額度 ----------

    def quota_consume(self, client_id):
        if self.daily_limit <= 0:
            return False, 0
        day = _day(self.clock())

        def go(conn):
            return conn.execute(
                """
                INSERT INTO quota (client_id, day, used) VALUES (%s, %s, 1)
                ON CONFLICT (client_id, day) DO UPDATE SET used = quota.used + 1
                WHERE quota.used < %s
                RETURNING used
                """,
                (client_id, day, self.daily_limit),
            ).fetchone()

        row = self._run(go)
        if row is None:
            return False, 0
        return True, max(0, self.daily_limit - row[0])

    def quota_refund(self, client_id):
        day = _day(self.clock())
        self._run(lambda conn: conn.execute(
            "UPDATE quota SET used = used - 1 WHERE client_id = %s AND day = %s AND used > 0",
            (client_id, day),
        ))

    def quota_remaining(self, client_id):
        day = _day(self.clock())
        row = self._run(lambda conn: conn.execute(
            "SELECT used FROM quota WHERE client_id = %s AND day = %s", (client_id, day),
        ).fetchone())
        return max(0, self.daily_limit - (row[0] if row else 0))

    # ---------- 紀錄與統計 ----------

    def log(self, entry: LogEntry):
        def go(conn):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO logs (client_id, landmark_name, cache_hit, latency_ms, created_at, day)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (entry.client_id, entry.landmark_name, entry.cache_hit, int(entry.latency_ms),
                     _ts(entry.created_at), _day(entry.created_at)),
                )
                conn.execute(
                    "INSERT INTO clients (client_id, first_seen) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (entry.client_id, _ts(entry.created_at)),
                )
        self._run(go)
        self._maybe_maintain()

    def stats(self):
        """requests、cache_hits：daily_stats 彙總＋尚未彙總的原始紀錄，是精確的歷來總數。
        unique_clients：clients 表，精確的歷來總數。
        latency 百分位數：只用保留中的原始紀錄（近 log_retention_days 天）計算，
        是近期的數值，不是歷來全部（舊紀錄已刪，只剩每日的 p50/p95，無法精確合併）。
        百分位的取法與 MemoryStore 相同：排序後取第 min(n-1, floor(q*n)) 筆。"""
        now = _ts(self.clock())

        def go(conn):
            with conn.transaction():
                rolled = conn.execute(
                    "SELECT coalesce(sum(requests), 0), coalesce(sum(cache_hits), 0) FROM daily_stats"
                ).fetchone()
                raw = conn.execute(
                    "SELECT count(*), count(*) FILTER (WHERE cache_hit) FROM logs"
                    " WHERE day NOT IN (SELECT day FROM daily_stats)"
                ).fetchone()
                n_lat = conn.execute("SELECT count(*) FROM logs").fetchone()[0]

                def pct(q):
                    if not n_lat:
                        return None
                    return conn.execute(
                        "SELECT latency_ms FROM logs ORDER BY latency_ms OFFSET %s LIMIT 1",
                        (min(n_lat - 1, int(q * n_lat)),),
                    ).fetchone()[0]

                return {
                    "rolled": rolled,
                    "raw": raw,
                    "p50": pct(0.50),
                    "p95": pct(0.95),
                    "clients": conn.execute("SELECT count(*) FROM clients").fetchone()[0],
                    "cached": conn.execute(
                        "SELECT count(*) FROM cache WHERE expires_at > %s", (now,)
                    ).fetchone()[0],
                }

        r = self._run(go)
        n = int(r["rolled"][0]) + int(r["raw"][0])
        hits = int(r["rolled"][1]) + int(r["raw"][1])
        return {
            "requests": n,
            "cache_hits": hits,
            "cache_hit_rate": round(hits / n, 3) if n else None,
            "unique_clients": int(r["clients"]),
            "latency_ms_p50": r["p50"],
            "latency_ms_p95": r["p95"],
            "cached_landmarks": int(r["cached"]),
        }

    # ---------- 維護 ----------

    def maintenance(self) -> dict:
        """清理過期資料並彙總紀錄；回傳各步驟影響的筆數（方便測試與記錄）。

        1. 刪除過期快取。
        2. 刪除昨天以前的額度（只留今天和昨天）。
        3. 今天以前、尚未彙總的紀錄日彙總進 daily_stats（已彙總的日子不會重算）。
           p50/p95 用 percentile_disc，與 stats() 的取法略有差異，僅供報告參考。
        4. 刪除早於「今天 - log_retention_days」的原始紀錄。
        """
        now = self.clock()
        today = _day(now)

        def go(conn):
            with conn.transaction():
                out = {}
                out["cache_deleted"] = conn.execute(
                    "DELETE FROM cache WHERE expires_at <= %s", (_ts(now),)
                ).rowcount
                out["quota_deleted"] = conn.execute(
                    "DELETE FROM quota WHERE day < %s", (today - timedelta(days=1),)
                ).rowcount
                out["days_rolled_up"] = conn.execute(
                    """
                    INSERT INTO daily_stats (day, requests, cache_hits, unique_clients, p50, p95)
                    SELECT day, count(*), count(*) FILTER (WHERE cache_hit), count(DISTINCT client_id),
                           percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_ms),
                           percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms)
                    FROM logs
                    WHERE day < %s AND day NOT IN (SELECT day FROM daily_stats)
                    GROUP BY day
                    ON CONFLICT (day) DO NOTHING
                    """,
                    (today,),
                ).rowcount
                out["logs_deleted"] = conn.execute(
                    "DELETE FROM logs WHERE day < %s", (today - timedelta(days=self.log_retention_days),)
                ).rowcount
                return out

        result = self._run(go)
        logger.info("資料庫維護完成：%s", result)
        return result
