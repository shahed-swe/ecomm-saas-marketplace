"""Bulk product import from CSV (vendor tool). Rows group into products by `handle`; each row is a
variant. Upsert by SKU (price/stock/options update; content fields only on create). Row errors are
collected, never fatal to the batch. Runs in a worker; inline in dev/test."""

import csv
import io
import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, ValidationError
from sqlalchemy import text

from app.core import audit
from app.core.cache_keys import object_key
from app.core.deps import Tenant, TenantDB, require_vendor_role
from app.core.errors import AppError, NotFound
from app.core.security import Principal
from app.modules.catalog import products as P
from app.modules.catalog.taxonomy import attributes_for_category, validate_attributes

router = APIRouter(prefix="/api/v1/vendor/imports", tags=["vendor:imports"])
VendorCatalog = Annotated[Principal, Depends(require_vendor_role("catalog.write", approved=True))]

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
REQUIRED = {"handle", "title_en", "category", "sku", "price"}
TEMPLATE = (
    "handle,title_en,title_bn,description,category,brand,sku,option1_name,option1_value,"
    "option2_name,option2_value,price,compare_at_price,stock,weight_grams\n"
)


class ImportOut(BaseModel):
    id: uuid.UUID
    status: str
    total_rows: int
    succeeded: int
    failed: int
    errors: list


def _dec(v: str | None) -> Decimal | None:
    if v is None or v.strip() == "":
        return None
    try:
        return Decimal(v.strip().replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("not a number") from exc


async def run_import(db, private_storage, revalidator, *, tenant_id: str, job_id: str) -> dict:
    job = (
        (
            await db.execute(
                text("SELECT * FROM import_jobs WHERE id = :j AND tenant_id = :t FOR UPDATE"),
                {"j": job_id, "t": tenant_id},
            )
        )
        .mappings()
        .first()
    )
    if job is None or job["status"] not in ("queued",):
        return {"status": job["status"] if job else "missing"}
    vendor_id = str(job["vendor_id"])
    await db.execute(text("UPDATE import_jobs SET status = 'running' WHERE id = :j"), {"j": job_id})
    raw = private_storage.path(job["source_key"]).read_bytes()
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error):
        await db.execute(
            text(
                "UPDATE import_jobs SET status='failed', errors = :e, finished_at = now() WHERE id = :j"
            ),
            {"e": '[{"row": 0, "error": "File must be UTF-8 CSV"}]', "j": job_id},
        )
        return {"status": "failed"}
    missing = REQUIRED - set(reader.fieldnames or [])
    if missing or len(rows) > MAX_ROWS:
        err = (
            f"Missing columns: {', '.join(sorted(missing))}"
            if missing
            else f"At most {MAX_ROWS} rows"
        )
        import json

        await db.execute(
            text(
                "UPDATE import_jobs SET status='failed', errors = CAST(:e AS jsonb), finished_at = now() WHERE id = :j"
            ),
            {"e": json.dumps([{"row": 0, "error": err}]), "j": job_id},
        )
        return {"status": "failed"}

    cats = {
        r.slug: r.id
        for r in (
            await db.execute(
                text("SELECT slug, id FROM categories WHERE tenant_id = :t AND is_active"),
                {"t": tenant_id},
            )
        )
    }
    brands = {
        r.slug: r.id
        for r in (
            await db.execute(
                text("SELECT slug, id FROM brands WHERE tenant_id = :t"), {"t": tenant_id}
            )
        )
    }
    errors, ok = [], 0
    touched: set[str] = set()
    for n, r in enumerate(rows, start=2):  # header is line 1
        try:
            options = {}
            for i in (1, 2, 3):
                name, value = (
                    (r.get(f"option{i}_name") or "").strip(),
                    (r.get(f"option{i}_value") or "").strip(),
                )
                if name and value:
                    options[name] = value
            variant = P.VariantIn(
                sku=(r["sku"] or "").strip(),
                options=options,
                price=_dec(r["price"]),
                compare_at_price=_dec(r.get("compare_at_price")),
                stock=int((r.get("stock") or "0").strip() or 0),
                weight_grams=int(r["weight_grams"])
                if (r.get("weight_grams") or "").strip()
                else None,
            )
            handle = (r["handle"] or "").strip().lower()
            async with db.begin_nested():
                existing = (
                    await db.execute(
                        text(
                            """SELECT id, product_id, stock_on_hand FROM product_variants
                       WHERE tenant_id = :t AND vendor_id = :v AND sku = :s FOR UPDATE"""
                        ),
                        {"t": tenant_id, "v": vendor_id, "s": variant.sku},
                    )
                ).first()
                if existing:
                    await db.execute(
                        text(
                            """UPDATE product_variants SET price = :p, compare_at_price = :c, updated_at = now()
                           WHERE id = :i AND tenant_id = :t"""
                        ),
                        {
                            "p": variant.price,
                            "c": variant.compare_at_price,
                            "i": existing.id,
                            "t": tenant_id,
                        },
                    )
                    delta = variant.stock - existing.stock_on_hand
                    if delta:
                        await P.adjust_stock(
                            db,
                            tenant_id=tenant_id,
                            vendor_id=vendor_id,
                            variant_id=existing.id,
                            delta=delta,
                            reason="adjustment",
                            actor=job["created_by"],
                            ref=f"import:{job_id}",
                        )
                    product_id = existing.product_id
                else:
                    product = (
                        await db.execute(
                            text(
                                "SELECT id, vendor_id FROM products WHERE tenant_id = :t AND slug = :s"
                            ),
                            {"t": tenant_id, "s": handle},
                        )
                    ).first()
                    if product and str(product.vendor_id) != vendor_id:
                        raise ValueError("handle unavailable")
                    if product is None:
                        cat = cats.get((r["category"] or "").strip().lower())
                        if cat is None:
                            raise ValueError("unknown category")
                        brand = (
                            brands.get((r.get("brand") or "").strip().lower())
                            if r.get("brand")
                            else None
                        )
                        count = (
                            await db.execute(
                                text(
                                    "SELECT count(*) FROM products WHERE tenant_id = :t AND status <> 'archived'"
                                ),
                                {"t": tenant_id},
                            )
                        ).scalar()
                        from app.modules.billing.service import require_quota

                        await require_quota(db, tenant_id, "products", count)
                        body = P.ProductIn(
                            slug=handle,
                            title_en=r["title_en"],
                            title_bn=r.get("title_bn") or None,
                            description=r.get("description") or "",
                            category_id=cat,
                            brand_id=brand,
                            variants=[variant],
                        )
                        validate_attributes(
                            await attributes_for_category(db, tenant_id, cat),
                            {},
                            require_complete=False,
                        )
                        product_id = (
                            await db.execute(
                                text(
                                    """INSERT INTO products (tenant_id, vendor_id, category_id, brand_id, slug, title_en, title_bn,
                                   description) VALUES (:t, :v, :c, :b, :s, :en, :bn, :d) RETURNING id"""
                                ),
                                {
                                    "t": tenant_id,
                                    "v": vendor_id,
                                    "c": cat,
                                    "b": brand,
                                    "s": body.slug,
                                    "en": body.title_en,
                                    "bn": body.title_bn,
                                    "d": body.description,
                                },
                            )
                        ).scalar()
                    else:
                        product_id = product.id
                    await P._insert_variant(
                        db, tenant_id, vendor_id, product_id, variant, job["created_by"]
                    )
                await P._refresh_rollups(db, tenant_id, product_id)
            touched.add(handle)
            ok += 1
        except (ValidationError, ValueError, KeyError, AppError) as exc:
            msg = (
                exc.detail
                if isinstance(exc, AppError)
                else (
                    "; ".join(e["msg"] for e in exc.errors())
                    if isinstance(exc, ValidationError)
                    else str(exc)
                )
            )
            if len(errors) < 200:
                errors.append({"row": n, "sku": r.get("sku"), "error": msg[:200]})
        except Exception as exc:  # noqa: BLE001 - e.g. unique violation on a slug
            if len(errors) < 200:
                errors.append({"row": n, "sku": r.get("sku"), "error": "row could not be saved"})
            _ = exc
    import json

    await db.execute(
        text(
            """UPDATE import_jobs SET status = 'done', total_rows = :n, succeeded = :ok, failed = :f,
               errors = CAST(:e AS jsonb), finished_at = now() WHERE id = :j"""
        ),
        {"n": len(rows), "ok": ok, "f": len(rows) - ok, "e": json.dumps(errors), "j": job_id},
    )
    if touched:
        await revalidator.revalidate(tenant_id, [f"t:{tenant_id}:products"])
    return {"status": "done", "succeeded": ok}


@router.get("/template")
async def template(_: VendorCatalog):
    from fastapi.responses import Response

    return Response(
        TEMPLATE,
        media_type="text/csv",
        headers={"content-disposition": 'attachment; filename="products-template.csv"'},
    )


@router.post("", response_model=ImportOut, status_code=202)
async def start_import(
    request: Request, p: VendorCatalog, tenant: Tenant, db: TenantDB, file: UploadFile = File(...)
):
    data = await file.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise AppError("CSV must be 5 MB or smaller", status=422, code="invalid_file")
    if b"\x00" in data[:4096]:
        raise AppError("Upload a CSV file", status=422, code="invalid_file")
    key = object_key(tenant.id, "imports", p.vid, f"{uuid.uuid4().hex}.csv")
    storage = request.app.state.private_storage
    path = storage.path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    jid = (
        await db.execute(
            text(
                "INSERT INTO import_jobs (tenant_id, vendor_id, source_key, created_by) VALUES (:t, :v, :k, :a) RETURNING id"
            ),
            {"t": tenant.id, "v": p.vid, "k": key, "a": p.sub},
        )
    ).scalar()
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="import.start",
        entity="import_job",
        entity_id=jid,
        request=request,
    )
    await request.app.state.import_queue.enqueue(db, tenant_id=tenant.id, job_id=str(jid))
    return await get_import(jid, p, tenant, db)


@router.get("/{job_id}", response_model=ImportOut)
async def get_import(job_id: uuid.UUID, p: VendorCatalog, tenant: Tenant, db: TenantDB):
    row = (
        (
            await db.execute(
                text(
                    "SELECT * FROM import_jobs WHERE id = :j AND tenant_id = :t AND vendor_id = :v"
                ),
                {"j": job_id, "t": tenant.id, "v": p.vid},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    return ImportOut(**{k: row[k] for k in ImportOut.model_fields})


class InlineImportQueue:
    def __init__(self, app):
        self.app = app

    async def enqueue(self, db, *, tenant_id: str, job_id: str) -> None:
        await run_import(
            db,
            self.app.state.private_storage,
            self.app.state.revalidator,
            tenant_id=tenant_id,
            job_id=job_id,
        )
