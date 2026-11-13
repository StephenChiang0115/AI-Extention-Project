"""壓測與成本統計腳本（階段 5，期中報告用數據）。

用途
----
模擬多位使用者對 `/api/identify` 送出框選影像，量測：
- 總請求數、成功數、429（每日額度用完）、其他錯誤
- 快取命中率（依回應中的 cache_hit 欄位）
- 延遲 p50 / p95 / p99 / 最大值（整體、命中、未命中分開統計）
- 吞吐量（req/s）與總耗時
- 模型呼叫次數與成本估算：有快取 vs. 沒有快取，節省比例
結束後會呼叫 `/api/stats`，把伺服器端統計一併寫入報告，方便對照。

模擬方式
--------
- 產生 N 張「有紋理」的合成影像（每張圖由不同的隨機矩形、橢圓、線條組成）。
  純色影像會被後端判定為低紋理而不使用快取，所以刻意避開。
- 每次請求依 Zipf 分布挑一張基礎影像（熱門景點被框選的次數多），
  再做小幅變化（JPEG 品質 70–95、縮放 ±5%），模擬使用者重複框選同一地標。
  這些變化應仍落在 dHash 門檻內（預設 6）而命中快取。
- 每位模擬使用者有自己的 X-Client-Id（每次執行都不同），受每日免費額度限制；
  429 視為「額度行為」單獨統計，不算錯誤。
- 執行前會先在本機計算 dHash，印出「不同影像之間的最小距離」與「同一影像變化的最大距離」，
  用來確認合成資料本身合理（不同圖不誤命中、同圖變化會命中）。

使用方式（Windows，在專案根目錄）
-------------------------------
    backend\\.venv\\Scripts\\python scripts\\loadtest.py --api http://localhost:8080 ^
        --requests 200 --concurrency 10 --clients 20 --distinct-images 15

常用參數：
    --api              後端網址（本機或雲端部署後的網址）
    --requests         總請求數（預設 200）
    --concurrency      同時進行的請求數（預設 10）
    --clients          模擬使用者數（預設 20）
    --distinct-images  不同的基礎影像（景點）數量（預設 15）
    --zipf-s           Zipf 分布參數，越大越集中在熱門影像（預設 1.1）
    --cost-per-call    每次模型呼叫的估計成本，美元（預設 0.0005）
    --admin-token      若後端設定了 ADMIN_TOKEN，查詢 /api/stats 時帶上
    --hamming-threshold 本機預檢 dHash 時使用的門檻（應與後端 HAMMING_THRESHOLD 相同，預設 6）
    --seed             亂數種子（預設 42，方便重現）
    --out              JSON 報告輸出路徑（例如 report.json）

注意事項
--------
- 後端的快取與額度存在記憶體中；要得到乾淨的數據，每次壓測前請重新啟動後端。
- 每位使用者每日只能有 DAILY_FREE_LIMIT 次「未命中」請求；使用者數太少時會出現大量 429。
- mock 模式下模型沒有延遲，未命中的延遲只反映後端本身的處理時間；接上真實模型時差異才會明顯。
- 只需要 httpx 與 Pillow（backend/.venv 已安裝）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import random
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx
from PIL import Image, ImageDraw

IMG_W, IMG_H = 320, 240
VARIANTS_PER_IMAGE = 8


# ---------------------------------------------------------------- 影像產生

def make_base_image(image_id: int, seed: int) -> Image.Image:
    """產生有紋理的合成影像：背景漸層 + 隨機矩形、橢圓、線條。不同 image_id 圖樣不同。"""
    rng = random.Random(seed * 1000 + image_id)
    img = Image.new("RGB", (IMG_W, IMG_H))
    d = ImageDraw.Draw(img)
    # 背景：隨機方向的漸層，避免整片純色
    c1 = [rng.randint(0, 255) for _ in range(3)]
    c2 = [rng.randint(0, 255) for _ in range(3)]
    for y in range(IMG_H):
        t = y / (IMG_H - 1)
        d.line([(0, y), (IMG_W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    for _ in range(rng.randint(10, 18)):
        x0, y0 = rng.randint(-40, IMG_W), rng.randint(-40, IMG_H)
        x1, y1 = x0 + rng.randint(30, 160), y0 + rng.randint(30, 140)
        color = tuple(rng.randint(0, 255) for _ in range(3))
        kind = rng.choice(("rect", "ellipse", "line", "poly"))
        if kind == "rect":
            d.rectangle([x0, y0, x1, y1], fill=color)
        elif kind == "ellipse":
            d.ellipse([x0, y0, x1, y1], fill=color)
        elif kind == "line":
            d.line([x0, y0, x1, y1], fill=color, width=rng.randint(4, 14))
        else:
            pts = [(rng.randint(0, IMG_W), rng.randint(0, IMG_H)) for _ in range(rng.randint(3, 6))]
            d.polygon(pts, fill=color)
    return img


def make_variant(base: Image.Image, rng: random.Random, first: bool) -> bytes:
    """模擬使用者重新框選同一地標：JPEG 品質變化、縮放 ±5%。第一個變化保持原尺寸。"""
    img = base
    if not first:
        scale = rng.uniform(0.95, 1.05)
        img = base.resize((max(16, int(IMG_W * scale)), max(16, int(IMG_H * scale))), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85 if first else rng.randint(70, 95))
    return buf.getvalue()


def local_dhash(image_bytes: bytes) -> int:
    """與 backend/app/phash.py 相同的 dHash，僅用於本機預檢（不匯入後端程式碼）。"""
    img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((9, 8), Image.LANCZOS)
    px = img.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def zipf_weights(n: int, s: float) -> list[float]:
    w = [1.0 / (k ** s) for k in range(1, n + 1)]
    total = sum(w)
    return [x / total for x in w]


# ---------------------------------------------------------------- 統計

def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    # 最近秩法（nearest-rank）
    idx = max(0, min(len(v) - 1, int(round(q * len(v) + 0.5)) - 1))
    return round(v[idx], 1)


def latency_summary(values: list[float]) -> dict:
    return {
        "count": len(values),
        "p50_ms": pct(values, 0.50),
        "p95_ms": pct(values, 0.95),
        "p99_ms": pct(values, 0.99),
        "max_ms": round(max(values), 1) if values else None,
        "mean_ms": round(statistics.fmean(values), 1) if values else None,
    }


@dataclass
class Result:
    image_id: int
    client_idx: int
    status: int            # HTTP 狀態碼；0 代表連線錯誤或逾時
    latency_ms: float
    cache_hit: bool | None = None
    error: str | None = None


@dataclass
class Plan:
    image_id: int
    variant_idx: int
    client_idx: int


@dataclass
class Ctx:
    api: str
    payloads: dict[tuple[int, int], str]
    client_ids: list[str]
    results: list[Result] = field(default_factory=list)


# ---------------------------------------------------------------- 壓測主體

async def send_one(client: httpx.AsyncClient, ctx: Ctx, p: Plan) -> Result:
    body = {
        "image": ctx.payloads[(p.image_id, p.variant_idx)],
        "pageUrl": f"https://example.com/landmark/{p.image_id}",
        "pageTitle": f"壓測景點 {p.image_id}",
    }
    headers = {"X-Client-Id": ctx.client_ids[p.client_idx]}
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{ctx.api}/api/identify", json=body, headers=headers)
        ms = (time.perf_counter() - t0) * 1000
    except httpx.HTTPError as e:
        return Result(p.image_id, p.client_idx, 0, (time.perf_counter() - t0) * 1000,
                      error=f"{type(e).__name__}: {e}")
    hit = None
    err = None
    if r.status_code == 200:
        try:
            hit = bool(r.json().get("cache_hit"))
        except ValueError:
            err = "回應不是有效的 JSON"
    elif r.status_code != 429:
        err = r.text[:200]
    return Result(p.image_id, p.client_idx, r.status_code, ms, hit, err)


async def run_load(ctx: Ctx, plans: list[Plan], concurrency: int, timeout: float) -> float:
    queue: asyncio.Queue[Plan] = asyncio.Queue()
    for p in plans:
        queue.put_nowait(p)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        async def worker():
            while True:
                try:
                    p = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                ctx.results.append(await send_one(client, ctx, p))

        t0 = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        return time.perf_counter() - t0


async def fetch_stats(api: str, admin_token: str | None) -> dict:
    headers = {"X-Admin-Token": admin_token} if admin_token else {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{api}/api/stats", headers=headers)
        if r.status_code == 200:
            return r.json()
        return {"error": f"HTTP {r.status_code}", "detail": r.text[:200]}
    except httpx.HTTPError as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 報告

def build_report(args, results: list[Result], wall_s: float, precheck: dict, server_stats: dict) -> dict:
    total = len(results)
    ok = [r for r in results if r.status == 200]
    quota = [r for r in results if r.status == 429]
    bad_gateway = [r for r in results if r.status == 502]
    errors = [r for r in results if r.status not in (200, 429)]
    hits = [r for r in ok if r.cache_hit]
    misses = [r for r in ok if not r.cache_hit]

    model_calls = len(misses) + len(bad_gateway)          # 實際呼叫模型的次數
    calls_without_cache = len(ok) + len(bad_gateway)      # 沒有快取時，每個請求都要呼叫模型
    cost_with = model_calls * args.cost_per_call
    cost_without = calls_without_cache * args.cost_per_call
    savings = (1 - cost_with / cost_without) if cost_without else None

    clients_hit_quota = len({r.client_idx for r in quota})
    error_samples: dict[str, int] = {}
    for r in errors:
        key = f"HTTP {r.status}: {(r.error or '')[:80]}"
        error_samples[key] = error_samples.get(key, 0) + 1

    # 每張基礎影像：請求次數與命中次數（看 Zipf 分布與快取效果）
    per_image: dict[int, dict] = {}
    for r in ok:
        d = per_image.setdefault(r.image_id, {"requests": 0, "hits": 0})
        d["requests"] += 1
        d["hits"] += int(bool(r.cache_hit))

    return {
        "config": {
            "api": args.api, "requests": args.requests, "concurrency": args.concurrency,
            "clients": args.clients, "distinct_images": args.distinct_images, "zipf_s": args.zipf_s,
            "cost_per_call_usd": args.cost_per_call, "seed": args.seed,
            "hamming_threshold_assumed": args.hamming_threshold,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
        "precheck_dhash": precheck,
        "totals": {
            "requests": total,
            "success_200": len(ok),
            "quota_exceeded_429": len(quota),
            "errors": len(errors),
            "errors_502": len(bad_gateway),
            "error_samples": error_samples,
            "wall_time_s": round(wall_s, 3),
            "throughput_rps": round(total / wall_s, 2) if wall_s else None,
            "success_throughput_rps": round(len(ok) / wall_s, 2) if wall_s else None,
        },
        "cache": {
            "hits": len(hits),
            "misses": len(misses),
            "hit_rate": round(len(hits) / len(ok), 4) if ok else None,
        },
        "latency": {
            "all": latency_summary([r.latency_ms for r in results]),
            "success": latency_summary([r.latency_ms for r in ok]),
            "cache_hit": latency_summary([r.latency_ms for r in hits]),
            "cache_miss": latency_summary([r.latency_ms for r in misses]),
            "quota_429": latency_summary([r.latency_ms for r in quota]),
        },
        "quota": {
            "rejected_429": len(quota),
            "clients_rejected": clients_hit_quota,
            "clients_total": args.clients,
        },
        "cost": {
            "model_calls": model_calls,
            "model_calls_without_cache": calls_without_cache,
            "cost_usd": round(cost_with, 6),
            "cost_without_cache_usd": round(cost_without, 6),
            "savings_usd": round(cost_without - cost_with, 6),
            "savings_ratio": round(savings, 4) if savings is not None else None,
            "note": "未計入 429 被拒的請求；若沒有快取，這些請求也會更早耗盡額度。",
        },
        "per_image": {str(k): v for k, v in sorted(per_image.items())},
        "server_stats": server_stats,
    }


def fmt(v, suffix=""):
    return "—" if v is None else f"{v}{suffix}"


def print_summary(rep: dict) -> None:
    t, c, q, cost, lat = rep["totals"], rep["cache"], rep["quota"], rep["cost"], rep["latency"]
    line = "=" * 64
    print(line)
    print(" 壓測結果摘要")
    print(line)
    print(f" 目標後端            {rep['config']['api']}")
    print(f" 總請求數            {t['requests']}")
    print(f" 成功（200）         {t['success_200']}")
    print(f" 額度用完（429）     {t['quota_exceeded_429']}（{q['clients_rejected']}/{q['clients_total']} 位使用者曾被拒）")
    print(f" 其他錯誤            {t['errors']}（其中 502：{t['errors_502']}）")
    print(f" 總耗時              {t['wall_time_s']} 秒")
    print(f" 吞吐量              {t['throughput_rps']} req/s（成功 {t['success_throughput_rps']} req/s）")
    print("-" * 64)
    hr = c["hit_rate"]
    print(f" 快取命中 / 未命中   {c['hits']} / {c['misses']}   命中率 {fmt(None if hr is None else round(hr * 100, 1), '%')}")
    print("-" * 64)
    print(f" {'延遲（毫秒）':<12}{'筆數':>6}{'p50':>9}{'p95':>9}{'p99':>9}{'最大':>9}")
    for key, label in (("all", "全部"), ("cache_hit", "命中"), ("cache_miss", "未命中"), ("quota_429", "429")):
        s = lat[key]
        print(f" {label:<14}{s['count']:>6}{fmt(s['p50_ms']):>9}{fmt(s['p95_ms']):>9}"
              f"{fmt(s['p99_ms']):>9}{fmt(s['max_ms']):>9}")
    print("-" * 64)
    print(f" 模型呼叫次數        {cost['model_calls']}（沒有快取時需 {cost['model_calls_without_cache']} 次）")
    print(f" 估計成本            US$ {cost['cost_usd']:.4f}（沒有快取：US$ {cost['cost_without_cache_usd']:.4f}）")
    sr = cost["savings_ratio"]
    print(f" 快取節省            US$ {cost['savings_usd']:.4f}（{fmt(None if sr is None else round(sr * 100, 1), '%')}）")
    if t["error_samples"]:
        print("-" * 64)
        print(" 錯誤樣本：")
        for k, n in t["error_samples"].items():
            print(f"   {n} 次  {k}")
    print("-" * 64)
    ss = rep["server_stats"]
    if "error" in ss:
        print(f" 伺服器 /api/stats   取得失敗：{ss['error']}")
    else:
        print(f" 伺服器 /api/stats   requests={ss.get('requests')} cache_hits={ss.get('cache_hits')} "
              f"hit_rate={ss.get('cache_hit_rate')}")
        print(f"                     p50={ss.get('latency_ms_p50')}ms p95={ss.get('latency_ms_p95')}ms "
              f"（伺服器端處理時間） cached_landmarks={ss.get('cached_landmarks')}")
        print("                     註：伺服器統計自啟動後累計，429 不會記錄。")
    print(line)


# ---------------------------------------------------------------- 主程式

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="AI 景點導覽後端壓測與成本統計")
    ap.add_argument("--api", default="http://localhost:8080", help="後端網址")
    ap.add_argument("--requests", type=int, default=200, help="總請求數")
    ap.add_argument("--concurrency", type=int, default=10, help="同時請求數")
    ap.add_argument("--clients", type=int, default=20, help="模擬使用者數")
    ap.add_argument("--distinct-images", type=int, default=15, help="不同基礎影像數")
    ap.add_argument("--zipf-s", type=float, default=1.1, help="Zipf 分布參數")
    ap.add_argument("--cost-per-call", type=float, default=0.0005, help="每次模型呼叫成本（美元）")
    ap.add_argument("--admin-token", default=None, help="/api/stats 的 X-Admin-Token")
    ap.add_argument("--hamming-threshold", type=int, default=6, help="本機預檢用的 dHash 門檻")
    ap.add_argument("--timeout", type=float, default=120.0, help="單一請求逾時秒數")
    ap.add_argument("--seed", type=int, default=42, help="亂數種子")
    ap.add_argument("--out", default=None, help="JSON 報告輸出路徑")
    args = ap.parse_args(argv)
    if min(args.requests, args.concurrency, args.clients, args.distinct_images) < 1:
        ap.error("--requests、--concurrency、--clients、--distinct-images 都必須 ≥ 1")
    args.api = args.api.rstrip("/")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    rng = random.Random(args.seed)

    # 1. 準備影像與變化版本
    print(f"產生 {args.distinct_images} 張合成影像，每張 {VARIANTS_PER_IMAGE} 種變化……")
    payloads: dict[tuple[int, int], str] = {}
    base_hashes: list[int] = []
    variant_max: list[int] = []
    for i in range(args.distinct_images):
        base = make_base_image(i, args.seed)
        vrng = random.Random(args.seed * 7919 + i)
        hashes = []
        for v in range(VARIANTS_PER_IMAGE):
            data = make_variant(base, vrng, first=(v == 0))
            payloads[(i, v)] = "data:image/jpeg;base64," + base64.b64encode(data).decode()
            hashes.append(local_dhash(data))
        base_hashes.append(hashes[0])
        variant_max.append(max(hamming(hashes[0], h) for h in hashes))

    inter = [hamming(base_hashes[a], base_hashes[b])
             for a in range(len(base_hashes)) for b in range(a + 1, len(base_hashes))]
    precheck = {
        "min_distance_between_images": min(inter) if inter else None,
        "max_distance_within_variants": max(variant_max),
        "variants_over_threshold": sum(1 for m in variant_max if m > args.hamming_threshold),
        "image_pairs_within_threshold": sum(1 for d in inter if d <= args.hamming_threshold),
    }
    print(f"dHash 預檢：不同影像最小距離 {precheck['min_distance_between_images']}，"
          f"同一影像變化最大距離 {precheck['max_distance_within_variants']}（門檻 {args.hamming_threshold}）")
    if precheck["image_pairs_within_threshold"]:
        print(f"  警告：有 {precheck['image_pairs_within_threshold']} 組不同影像距離在門檻內，可能互相誤命中")
    if precheck["variants_over_threshold"]:
        print(f"  警告：有 {precheck['variants_over_threshold']} 張影像的變化超過門檻，命中率會偏低")

    # 2. 產生請求計畫：Zipf 挑影像、隨機變化、隨機使用者
    run_tag = uuid.uuid4().hex[:8]
    client_ids = [f"lt-{run_tag}-{k:04d}" for k in range(args.clients)]
    weights = zipf_weights(args.distinct_images, args.zipf_s)
    plans = [Plan(rng.choices(range(args.distinct_images), weights)[0],
                  rng.randrange(VARIANTS_PER_IMAGE),
                  rng.randrange(args.clients)) for _ in range(args.requests)]

    # 3. 健康檢查
    try:
        r = httpx.get(f"{args.api}/health", timeout=10)
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"無法連線到後端 {args.api}/health：{e}", file=sys.stderr)
        return 2

    # 4. 執行壓測
    print(f"開始壓測：{args.requests} 個請求、同時 {args.concurrency} 個、{args.clients} 位使用者……")
    ctx = Ctx(args.api, payloads, client_ids)
    wall = asyncio.run(run_load(ctx, plans, args.concurrency, args.timeout))
    server_stats = asyncio.run(fetch_stats(args.api, args.admin_token))

    # 5. 報告
    rep = build_report(args, ctx.results, wall, precheck, server_stats)
    print_summary(rep)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print(f"JSON 報告已寫入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
