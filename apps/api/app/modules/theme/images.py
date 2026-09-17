"""Brand image intake: the first slice of the media pipeline (full pipeline in Phase 7).

Magic-byte check (never trust filename/MIME) -> decompression-bomb guard -> re-encode (strips
EXIF/GPS and polyglot payloads) -> bounded size. Runs in a thread, never on the event loop.
"""

import io
import secrets

from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool

from app.core.errors import AppError

MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 25_000_000
_MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpeg", b"RIFF": "webp"}


class InvalidImage(AppError):
    status = 422
    code = "invalid_image"


def _sniff(data: bytes) -> str | None:
    for magic, kind in _MAGIC.items():
        if data.startswith(magic):
            if kind == "webp" and data[8:12] != b"WEBP":
                return None
            return kind
    return None


def _process(data: bytes, max_side: int) -> tuple[bytes, bytes, tuple[int, int]]:
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            raise InvalidImage("Image is too large")
        img = ImageOps.exif_transpose(img)
        img.load()
    except (Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise InvalidImage("File is not a supported image") from exc
    img = img.convert("RGBA")
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    webp, png = io.BytesIO(), io.BytesIO()
    img.save(webp, "WEBP", quality=90, method=6)
    img.save(png, "PNG", optimize=True)
    return webp.getvalue(), png.getvalue(), img.size


async def store_brand_image(storage, tenant_id: str, data: bytes, *, max_side: int = 512) -> dict:
    from app.core.cache_keys import object_key

    if len(data) > MAX_BYTES:
        raise InvalidImage("Image must be 2 MB or smaller")
    if _sniff(data) is None:
        raise InvalidImage("Use a PNG, JPEG or WebP image")
    webp, png, size = await run_in_threadpool(_process, data, max_side)
    stem = secrets.token_urlsafe(12)
    cache = "public, max-age=31536000, immutable"
    k_webp = object_key(tenant_id, "brand", f"{stem}.webp")
    k_png = object_key(tenant_id, "brand", f"{stem}.png")
    await storage.put(k_webp, webp, content_type="image/webp", cache_control=cache)
    await storage.put(k_png, png, content_type="image/png", cache_control=cache)
    return {
        "url": storage.public_path(k_webp),
        "fallback_url": storage.public_path(k_png),
        "width": size[0],
        "height": size[1],
    }
