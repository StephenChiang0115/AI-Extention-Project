"""地標辨識批次評估工具（階段 6：用真實模型實測辨識品質）。

用法（在專案根目錄執行，後端需先以 PROVIDER=lmstudio 啟動）：
    backend\\.venv\\Scripts\\python scripts\\eval_landmarks.py <圖片資料夾>
        [--api http://localhost:8080] [--out results.csv] [--client-id ...]
        [--page-title-from-filename] [--twice] [--max-side 1280] [--timeout 180]

正確答案取自檔名：`台北101__01.jpg` → 預期為「台北101」（取 `__` 前的文字；沒有 `__` 就用整個主檔名）。
多個可接受名稱用「=」分隔：`台北101=Taipei 101__01.jpg`。
支援 jpg / jpeg / png / webp。結果寫成 CSV（utf-8-sig，Excel 可直接開啟中文），並印出統計摘要。

只使用標準函式庫 + httpx + Pillow（backend/.venv 已安裝）。
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import statistics
import sys
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 2 * 1024 * 1024        # 與後端 MAX_IMAGE_BYTES 相同
TARGET_BYTES = MAX_IMAGE_BYTES - 64 * 1024  # 留一點餘裕
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Windows 主控台預設可能是 cp950，避免印中文或特殊字元時當掉
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


@dataclass
class Result:
    file: str
    expected: str
    pass_no: int = 1
    landmark_name: str = ""
    confidence: float | None = None
    correct: bool | None = None
    cache_hit: bool | None = None
    latency_ms: int = 0
    status: int = 0
    error: str = ""
    image_bytes: int = 0
    extra: dict = field(default_factory=dict, repr=False)


def expected_name(path: Path) -> str:
    stem = path.stem
    return stem.split("__", 1)[0].strip() if "__" in stem else stem.strip()


def normalize(text: str) -> str:
    """移除空白與標點、轉小寫（含全形轉半形），用於寬鬆比對。"""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(ch for ch in text if not (ch.isspace() or unicodedata.category(ch)[0] in "PSZ"))


def is_correct(expected: str, predicted: str | None) -> bool:
    """寬鬆比對；expected 可用「=」列出多個可接受名稱，例如「台北101=Taipei 101」。"""
    if not predicted:
        return False
    p = normalize(predicted)
    for name in expected.split("="):
        e = normalize(name)
        if e and p and (e in p or p in e):
            return True
    return False


def encode_image(path: Path, max_side: int) -> bytes:
    """讀圖並視需要縮小／轉檔，確保低於後端 2 MB 上限。
    JPEG/PNG 且尺寸與大小都在範圍內時直接送原檔；其他情況轉成 JPEG。"""
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as img:
        fmt = (img.format or "").upper()
        if fmt in ("JPEG", "PNG") and max(img.size) <= max_side and len(raw) <= TARGET_BYTES:
            return raw
        img = ImageOps.exif_transpose(img).convert("RGB")
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.LANCZOS)
        quality = 90
        while True:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            data = buf.getvalue()
            if len(data) <= TARGET_BYTES:
                return data
            if quality > 50:
                quality -= 10
            else:  # 品質已經很低仍太大，就再縮小尺寸
                img = img.resize((max(1, img.width * 3 // 4), max(1, img.height * 3 // 4)), Image.LANCZOS)


def identify(client: httpx.Client, api: str, client_id: str, path: Path, image: bytes,
             page_title: str | None, pass_no: int) -> Result:
    res = Result(file=path.name, expected=expected_name(path), pass_no=pass_no, image_bytes=len(image))
    payload = {"image": base64.b64encode(image).decode("ascii")}
    if page_title:
        payload["page_title"] = page_title
    t0 = time.perf_counter()
    try:
        r = client.post(f"{api}/api/identify", json=payload, headers={"X-Client-Id": client_id})
    except httpx.HTTPError as exc:
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        res.error = f"連線失敗：{type(exc).__name__} {exc}"
        return res
    res.latency_ms = int((time.perf_counter() - t0) * 1000)
    res.status = r.status_code
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text[:200]}
    if r.status_code != 200:
        detail = body.get("detail") if isinstance(body, dict) else body
        if isinstance(detail, dict):
            detail = detail.get("message", detail)
        res.error = str(detail)
        return res
    res.landmark_name = body.get("landmark_name") or ""
    res.confidence = body.get("confidence")
    res.cache_hit = bool(body.get("cache_hit"))
    res.correct = is_correct(res.expected, body.get("landmark_name"))
    return res


def percentile(values: list[int], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(title: str, results: list[Result]) -> None:
    ok = [r for r in results if r.status == 200]
    errors = [r for r in results if r.status != 200]
    n429 = sum(1 for r in results if r.status == 429)
    confs = [r.confidence for r in ok if r.confidence is not None]
    lat = [r.latency_ms for r in ok]
    correct = sum(1 for r in ok if r.correct)
    unknown = sum(1 for r in ok if not r.landmark_name)
    hits = sum(1 for r in ok if r.cache_hit)

    def fmt(v: float, unit: str = "") -> str:
        return "—" if v != v else f"{v:.0f}{unit}"  # NaN 顯示為「—」

    rows = [
        ("圖片張數", f"{len(results)}"),
        ("成功回應", f"{len(ok)}"),
        ("正確（寬鬆比對）", f"{correct}"),
        ("準確率（正確／成功回應）", f"{correct / len(ok):.1%}" if ok else "—"),
        ("準確率（正確／圖片張數）", f"{correct / len(results):.1%}" if results else "—"),
        ("無法辨識（landmark_name 為空）", f"{unknown}"),
        ("平均信心值", f"{statistics.mean(confs):.3f}" if confs else "—"),
        ("快取命中", f"{hits}／{len(ok)}" + (f"（{hits / len(ok):.1%}）" if ok else "")),
        ("延遲 p50", fmt(percentile(lat, 0.5), " ms")),
        ("延遲 p95", fmt(percentile(lat, 0.95), " ms")),
        ("延遲 平均", fmt(statistics.mean(lat), " ms") if lat else "—"),
        ("錯誤數（非 200）", f"{len(errors)}"),
        ("其中 429（額度用完）", f"{n429}"),
    ]
    width = max(_disp_width(k) for k, _ in rows)
    print(f"\n=== {title} ===")
    for k, v in rows:
        print(f"  {k}{' ' * (width - _disp_width(k))}  {v}")


def _disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="地標辨識批次評估：把資料夾內的照片逐張送到後端 /api/identify，統計準確率、信心值與延遲。")
    ap.add_argument("image_dir", type=Path, help="圖片資料夾（檔名格式：<地標名稱>__<編號>.jpg）")
    ap.add_argument("--api", default="http://localhost:8080", help="後端網址（預設 http://localhost:8080）")
    ap.add_argument("--out", type=Path, default=Path("results.csv"), help="輸出 CSV 路徑（預設 results.csv）")
    ap.add_argument("--client-id", help="X-Client-Id（預設每次執行產生新的隨機值，避免受先前快取／額度影響）")
    ap.add_argument("--page-title-from-filename", action="store_true",
                    help="把預期名稱當成 page_title 送出（模擬網頁標題含地名的情境；注意這會洩漏答案）")
    ap.add_argument("--twice", action="store_true", help="每張圖送兩次，第二輪用來統計快取命中率")
    ap.add_argument("--max-side", type=int, default=1280, help="影像最長邊上限（像素，預設 1280），超過會縮小")
    ap.add_argument("--timeout", type=float, default=180.0, help="單次請求逾時秒數（預設 180，本機模型可能很慢）")
    args = ap.parse_args(argv)

    if not args.image_dir.is_dir():
        print(f"找不到資料夾：{args.image_dir}", file=sys.stderr)
        return 2
    images = sorted(p for p in args.image_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    if not images:
        print(f"資料夾內沒有 jpg/jpeg/png/webp 圖片：{args.image_dir}", file=sys.stderr)
        return 2

    client_id = args.client_id or f"eval-{uuid.uuid4().hex[:16]}"
    if not (8 <= len(client_id) <= 64 and all(c.isalnum() and c.isascii() or c in "-_" for c in client_id)):
        print("--client-id 格式錯誤：需為 8～64 個英數字、底線或連字號", file=sys.stderr)
        return 2
    api = args.api.rstrip("/")

    print(f"後端：{api}　Client-Id：{client_id}　圖片：{len(images)} 張" + ("　（送兩輪）" if args.twice else ""))
    with httpx.Client(timeout=args.timeout) as client:
        try:
            client.get(f"{api}/health", timeout=5).raise_for_status()
        except httpx.HTTPError as exc:
            print(f"無法連線到後端 {api}/health：{exc}\n請先執行 start-backend.bat（並確認 PROVIDER 設定）。", file=sys.stderr)
            return 1

        encoded: dict[Path, bytes] = {}
        results: list[Result] = []
        passes = 2 if args.twice else 1
        for pass_no in range(1, passes + 1):
            if args.twice:
                print(f"\n--- 第 {pass_no} 輪 ---")
            for i, path in enumerate(images, 1):
                if path not in encoded:
                    try:
                        encoded[path] = encode_image(path, args.max_side)
                    except Exception as exc:  # 壞檔或 Pillow 讀不了
                        msg = ("無法辨識的圖片格式或檔案損毀" if isinstance(exc, UnidentifiedImageError)
                               else f"{type(exc).__name__} {exc}")
                        r = Result(file=path.name, expected=expected_name(path), pass_no=pass_no,
                                   error=f"讀取圖片失敗：{msg}")
                        results.append(r)
                        print(f"[{i}/{len(images)}] ! {path.name}：{r.error}")
                        continue
                title = expected_name(path).split("=")[0] if args.page_title_from_filename else None
                r = identify(client, api, client_id, path, encoded[path], title, pass_no)
                results.append(r)
                if r.status == 200:
                    mark = "✔" if r.correct else "✘"
                    conf = f"{r.confidence:.2f}" if r.confidence is not None else "—"
                    hit = "（快取）" if r.cache_hit else ""
                    print(f"[{i}/{len(images)}] {mark} {path.name}：預期「{r.expected}」→「{r.landmark_name or '無法辨識'}」"
                          f" 信心 {conf}　{r.latency_ms} ms{hit}")
                else:
                    print(f"[{i}/{len(images)}] ! {path.name}：HTTP {r.status or '—'} {r.error}")

    fields = ["file", "expected", "pass_no", "landmark_name", "confidence", "correct", "cache_hit",
              "latency_ms", "status", "error", "image_bytes"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({k: ("" if v is None else v) for k, v in asdict(r).items()})

    first = [r for r in results if r.pass_no == 1]
    summarize("第 1 輪（辨識品質）" if args.twice else "摘要", first)
    if args.twice:
        second = [r for r in results if r.pass_no == 2]
        summarize("第 2 輪（快取行為）", second)
        ok2 = [r for r in second if r.status == 200]
        if ok2:
            print(f"  → 第 2 輪快取命中率：{sum(r.cache_hit for r in ok2) / len(ok2):.1%}"
                  "（第 1 輪信心 < 0.6、無法辨識或低紋理的圖不會寫入快取，第 2 輪也不會命中）")
    print(f"\n逐張結果已寫入：{args.out.resolve()}")

    if any(r.status == 429 for r in results):
        print("\n⚠ 有請求回傳 429（今日免費額度已用完）。請在 backend/.env 調高 DAILY_FREE_LIMIT"
              "（例如 DAILY_FREE_LIMIT=500），重新啟動後端後再跑一次。"
              "\n  （只有未命中快取的請求會扣額度；額度存在記憶體，重啟後端也會歸零。）")
    if any(r.status == 502 for r in results):
        print("\n⚠ 有請求回傳 502（模型辨識失敗）。請看後端視窗的「模型辨識失敗」錯誤訊息，"
              "並確認 LM Studio 的 Server 已啟動且載入支援圖片的模型。")
    if any(r.status == 0 and r.error.startswith("連線失敗") for r in results):
        print("\n⚠ 有請求連線失敗或逾時；本機模型較慢時可加大 --timeout（後端另有 LMSTUDIO_TIMEOUT_S）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
