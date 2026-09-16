---
name: image-optimization-pipeline
description: Build and maintain the backend-driven image optimization pipeline — FastAPI upload interception, Pillow validation and resizing, WebP/AVIF conversion, responsive variant generation, EXIF stripping, blur placeholders, and S3/R2/MinIO upload. Use this skill whenever the work touches uploads, images, media, thumbnails, product photos, KYC or banner uploads, storage keys, or CDN delivery — even if the user just says "let them upload a picture" without mentioning optimization.
---

# Backend-Driven Image Optimization Pipeline

The frontend uploads raw bytes. Nothing raw ever reaches the bucket or the DB.

## Contract

**In:** `multipart/form-data`, field `files[]`, plus `entity_type` and
`entity_id`.
**Out:**
```json
{
  "id": "0192f3...",
  "checksum": "sha256:...",
  "width": 3024, "height": 4032,
  "dominant_color": "#c8b4a2",
  "blur_data_url": "data:image/webp;base64,UklGR...",
  "variants": {
    "thumb":  {"webp": "https://cdn/.../thumb.webp",  "avif": "...", "w": 160},
    "card":   {"webp": "...", "avif": "...", "w": 400},
    "detail": {"webp": "...", "avif": "...", "jpeg": "...", "w": 800},
    "zoom":   {"webp": "...", "avif": "...", "w": 1600}
  }
}
```
The `variants` map is what `next/image` consumes. Keep the shape stable — the
frontend types are generated from it.

## Stage order (never reorder)

1. **Size gate** — reject on `Content-Length` before reading a byte.
2. **Stream** — read in 64 kB chunks into a `BytesIO` with a hard cap.
3. **Identify** — `Image.open` + `verify()`, then reopen (verify consumes the
   file). Trust magic bytes; ignore filename and client MIME.
4. **Bomb guard** — set `Image.MAX_IMAGE_PIXELS`; reject `w*h > MAX_PIXELS`.
5. **Dedupe** — sha256 the original bytes; if `media_assets.checksum` exists,
   return the existing row and skip all work.
6. **Normalise** — `ImageOps.exif_transpose`, convert to sRGB, drop metadata by
   constructing a fresh `Image.new` and pasting (this is what strips GPS/EXIF).
7. **Derive** — Lanczos downscale only, never upscale. If the source is 500px
   wide, the `zoom` variant is 500px, and you say so in the response.
8. **Encode** — AVIF `quality=55, speed=4`; WebP `quality=82, method=6`. JPEG
   `quality=85, progressive=True, optimize=True` for the `detail` fallback only.
9. **Placeholder** — 16px wide WebP, `quality=30`, base64 into `blur_data_url`.
10. **Upload** — async, parallel per variant, key
    `media/{entity}/{uuid7}/{variant}.{ext}`, `Cache-Control: public,
    max-age=31536000, immutable`.
11. **Persist** — one row, inside a transaction, after all uploads succeed.

## Threading rule (the mistake everyone makes)

Pillow is CPU-bound C code holding the GIL for the duration. Encoding four
variants in an `async def` handler stalls the entire event loop and every other
request on that worker.

- Single image, ≤4 variants → `await run_in_threadpool(process, buf)`.
- Bulk upload, or AVIF at high effort → enqueue an ARQ job, return
  `{"status": "processing", "asset_id": ...}`, and let the client poll
  `GET /media/{id}` or listen on SSE.
- Benchmark before choosing. A 4000×3000 JPEG → 8 encodes is roughly 1.5–3s on
  a single core; that is far past an acceptable request budget.

## Security rules

- Re-encoding through Pillow is the sanitisation step — it discards polyglot
  payloads and embedded scripts. Never store the original bytes as-is for
  public serving. (If you must keep originals for print, store them in a private
  bucket with no public read.)
- Random storage keys; never `f"media/{user_filename}"`.
- SVG is not on the allowlist. If a logo upload needs SVG, sanitise it through a
  strict XML allowlist in a separate, explicit code path and serve it with
  `Content-Security-Policy: sandbox`.
- Per-user upload rate limit and a daily byte quota.

## Failure handling

Uploads before DB write means a crash leaves orphan objects, not broken rows —
that's the right direction. A nightly ARQ sweep deletes objects whose
`media_assets` row is missing or unreferenced and older than 24h.

## Frontend consumption

```tsx
<Image
  src={asset.variants.detail.webp}
  width={asset.width} height={asset.height}
  sizes="(max-width: 768px) 100vw, 50vw"
  placeholder="blur" blurDataURL={asset.blur_data_url}
  alt={product.title}
/>
```
Configure `images.formats = ['image/avif','image/webp']` and add the CDN host to
`images.remotePatterns` in `next.config.ts`.

## Verify your work

Report, for one real 4000×3000 photo: original KB, per-variant KB for AVIF and
WebP, total processing ms, and whether it ran on the threadpool or a worker.
A pipeline without measured numbers is not done.
