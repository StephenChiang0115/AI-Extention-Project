"""影像感知雜湊（dHash）：同一地標、不同裁切或壓縮的影像，雜湊會很接近。"""
import hashlib
import io

from PIL import Image, ImageStat, UnidentifiedImageError


def dhash(image_bytes: bytes) -> int:
    """回傳 64 位元整數。無法解碼成影像時，退回以 SHA-256 取前 64 位元（僅完全相同才會命中）。"""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((9, 8), Image.LANCZOS)
    except (UnidentifiedImageError, OSError, ValueError):
        return int.from_bytes(hashlib.sha256(image_bytes).digest()[:8], "big")
    px = img.tobytes()  # L 模式：每像素 1 byte
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def is_low_texture(image_bytes: bytes, threshold: float = 8.0) -> bool:
    """純色或幾乎沒有紋理的影像（天空、黑畫面）：dHash 都接近 0，彼此會誤命中快取。
    轉灰階縮成 32x32 後，亮度標準差小於 threshold 即視為低紋理。無法解碼時回傳 False。"""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((32, 32), Image.LANCZOS)
    except (UnidentifiedImageError, OSError, ValueError):
        return False
    return ImageStat.Stat(img).stddev[0] < threshold


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()
