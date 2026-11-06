"""儲存層：快取、每日額度、辨識紀錄。

MemoryStore 供開發與測試；階段 4 部署到雲端時，換成 Firestore 實作即可，
main.py 只依賴 Store 介面。
"""
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .phash import hamming
from .schemas import Script

# 以台灣時間的日期計算「每日」額度。台灣沒有日光節約時間，固定 UTC+8 即可；
# 不依賴系統時區資料庫（Windows 沒有內建，會找不到 Asia/Taipei）。
TZ = timezone(timedelta(hours=8), "Asia/Taipei")


def today_key(now: float) -> str:
    return datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%d")


def next_reset_iso(now: float) -> str:
    d = datetime.fromtimestamp(now, TZ)
    nxt = (d + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.isoformat()


@dataclass
class LogEntry:
    client_id: str
    landmark_name: str | None
    cache_hit: bool
    latency_ms: int
    created_at: float


class Store(ABC):
    @abstractmethod
    def cache_get(self, phash: int) -> Script | None: ...
    @abstractmethod
    def cache_put(self, phash: int, script: Script) -> None: ...
    @abstractmethod
    def quota_consume(self, client_id: str) -> tuple[bool, int]:
        """嘗試使用 1 次額度；回傳 (是否允許, 剩餘次數)。"""
    @abstractmethod
    def quota_refund(self, client_id: str) -> None: ...
    @abstractmethod
    def quota_remaining(self, client_id: str) -> int: ...
    @abstractmethod
    def log(self, entry: LogEntry) -> None: ...
    @abstractmethod
    def stats(self) -> dict: ...


@dataclass
class MemoryStore(Store):
    daily_limit: int = 5
    cache_ttl_s: int = 7 * 24 * 3600
    hamming_threshold: int = 6  # 64 位元中最多幾個位元不同，仍視為同一景點
    clock: callable = time.time
    _cache: list = field(default_factory=list)       # [(phash, script, expires_at)]
    _quota: dict = field(default_factory=dict)       # {(client_id, day): used}
    _logs: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def cache_get(self, phash):
        now = self.clock()
        with self._lock:
            self._cache = [c for c in self._cache if c[2] > now]  # 清掉過期項目
            best = min(self._cache, key=lambda c: hamming(c[0], phash), default=None)
            if best and hamming(best[0], phash) <= self.hamming_threshold:
                return best[1].model_copy(deep=True)
        return None

    def cache_put(self, phash, script):
        with self._lock:
            self._cache.append((phash, script.model_copy(deep=True), self.clock() + self.cache_ttl_s))

    def quota_consume(self, client_id):
        key = (client_id, today_key(self.clock()))
        with self._lock:
            used = self._quota.get(key, 0)
            if used >= self.daily_limit:
                return False, 0
            self._quota[key] = used + 1
            return True, self.daily_limit - used - 1

    def quota_refund(self, client_id):
        key = (client_id, today_key(self.clock()))
        with self._lock:
            if self._quota.get(key, 0) > 0:
                self._quota[key] -= 1

    def quota_remaining(self, client_id):
        with self._lock:
            return max(0, self.daily_limit - self._quota.get((client_id, today_key(self.clock())), 0))

    def log(self, entry):
        with self._lock:
            self._logs.append(entry)

    def stats(self):
        with self._lock:
            n = len(self._logs)
            hits = sum(1 for e in self._logs if e.cache_hit)
            lat = sorted(e.latency_ms for e in self._logs)
            p = lambda q: lat[min(n - 1, int(q * n))] if n else None
            return {
                "requests": n,
                "cache_hits": hits,
                "cache_hit_rate": round(hits / n, 3) if n else None,
                "unique_clients": len({e.client_id for e in self._logs}),
                "latency_ms_p50": p(0.50),
                "latency_ms_p95": p(0.95),
                "cached_landmarks": len(self._cache),
            }
