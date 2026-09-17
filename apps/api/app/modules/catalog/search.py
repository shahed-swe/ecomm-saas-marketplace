"""Multi-vendor search (architecture §6: < 200 ms p95 @ 20k products / 100 vendors per tenant).

Postgres full text (simple config, prefix matching) + trigram fallback for typos, with query expansion
from a built-in Banglish↔Bangla dictionary and tenant synonyms. Facets are computed from the same
visible, tenant-scoped set, so no facet can reveal another tenant's (or a hidden product's) data.
Two queries per search: results and facets.
"""

import json
import re
import uuid
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core import audit
from app.core.cache_keys import tkey
from app.core.deps import Tenant, TenantDB, require_tenant_staff
from app.core.errors import AppError, Conflict, NotFound
from app.core.security import Principal
from app.modules.catalog.products import VISIBLE

router = APIRouter(prefix="/api/v1/catalog", tags=["search"])
admin = APIRouter(prefix="/api/v1/admin/catalog/synonyms", tags=["admin:search"])
Editor = Annotated[Principal, Depends(require_tenant_staff("catalog.write"))]

_TOKEN = re.compile(r"[a-z0-9ঀ-৿]+")
MAX_TOKENS = 8
PAGE_SIZE = 24

# Common shopping words typed in Banglish mapped to Bangla (and back). Tenants extend with synonyms.
BUILTIN_SYNONYMS: dict[str, list[str]] = {
    "saree": ["শাড়ি", "sari", "shari"],
    "sari": ["saree", "শাড়ি"],
    "shari": ["saree", "শাড়ি"],
    "শাড়ি": ["saree", "sari"],
    "panjabi": ["পাঞ্জাবি", "punjabi", "kurta"],
    "punjabi": ["panjabi", "পাঞ্জাবি"],
    "পাঞ্জাবি": ["panjabi"],
    "kurti": ["কুর্তি", "kurta"],
    "কুর্তি": ["kurti"],
    "kurta": ["kurti", "panjabi"],
    "lungi": ["লুঙ্গি"],
    "লুঙ্গি": ["lungi"],
    "salwar": ["সালোয়ার", "shalwar"],
    "shalwar": ["salwar"],
    "juta": ["জুতা", "shoe", "shoes"],
    "জুতা": ["shoe", "juta"],
    "shoe": ["জুতা", "juta"],
    "mobile": ["মোবাইল", "phone"],
    "মোবাইল": ["mobile", "phone"],
    "phone": ["mobile", "মোবাইল"],
    "ghori": ["ঘড়ি", "watch"],
    "ঘড়ি": ["watch", "ghori"],
    "watch": ["ঘড়ি", "ghori"],
    "bag": ["ব্যাগ", "byag"],
    "ব্যাগ": ["bag"],
    "tel": ["তেল", "oil"],
    "তেল": ["oil", "tel"],
    "oil": ["তেল"],
    "cha": ["চা", "tea"],
    "চা": ["tea", "cha"],
    "tea": ["চা", "cha"],
    "chal": ["চাল", "rice"],
    "চাল": ["rice"],
    "rice": ["চাল", "chal"],
    "mach": ["মাছ", "fish"],
    "মাছ": ["fish"],
    "fish": ["মাছ"],
    "jamdani": ["জামদানি"],
    "জামদানি": ["jamdani"],
    "tant": ["তাঁত", "taant"],
    "তাঁত": ["tant"],
    "baby": ["শিশু", "kids"],
    "shishu": ["শিশু", "baby"],
    "beauty": ["রূপচর্চা", "cosmetics"],
}


def tokens(q: str) -> list[str]:
    return _TOKEN.findall((q or "").lower())[:MAX_TOKENS]


async def tenant_synonyms(db, redis, tenant_id: str) -> dict[str, list[str]]:
    key = tkey(tenant_id, "search", "synonyms")
    cached = await redis.get(key)
    if cached is not None:
        return json.loads(cached)
    rows = (
        await db.execute(
            text("SELECT term, synonyms FROM search_synonyms WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    ).all()
    data = {r.term.lower(): [s.lower() for s in r.synonyms] for r in rows}
    await redis.set(key, json.dumps(data), ex=300)
    return data


def build_tsquery(toks: list[str], synonyms: dict[str, list[str]]) -> str | None:
    """AND across typed words, OR across each word's expansions, prefix match. Tokens are regex-clean,
    so the resulting string cannot inject tsquery operators."""
    if not toks:
        return None
    groups = []
    for tok in toks:
        alts = {tok} | set(synonyms.get(tok, [])) | set(BUILTIN_SYNONYMS.get(tok, []))
        clean = sorted({a for alt in alts for a in _TOKEN.findall(alt.lower())})
        groups.append("(" + " | ".join(f"'{a}':*" for a in clean) + ")")
    return " & ".join(groups)


class Facet(BaseModel):
    value: str
    label: str
    count: int


class SearchOut(BaseModel):
    query: str
    total: int
    page: int
    items: list[dict]
    facets: dict[str, list[Facet]]
    price_range: dict[str, Decimal | None]
    attributes: dict[str, list[Facet]]
    did_you_mean: str | None = None


CANDIDATES = """cand AS MATERIALIZED (
  SELECT * FROM search_candidates(CAST(:tsq AS tsquery), CAST(:cats AS uuid[]), CAST(:brands AS uuid[]),
                                  CAST(:vendors AS uuid[]), CAST(:minp AS numeric), CAST(:maxp AS numeric),
                                  :in_stock, CAST(:attrs AS jsonb)))"""

PAGE_SQL = (
    """WITH """
    + CANDIDATES
    + """
SELECT id, count(*) OVER () AS total FROM cand ORDER BY {order} LIMIT :limit OFFSET :offset"""
)

CARDS_SQL = """SELECT p.id, p.slug, p.title_en, p.title_bn, p.min_price, p.max_price, p.in_stock, p.vendor_id,
       v.display_name AS vendor_name, v.slug AS vendor_slug,
       (SELECT ma.renditions FROM product_media pm JOIN media_assets ma
          ON ma.id = pm.asset_id AND ma.tenant_id = pm.tenant_id
        WHERE pm.tenant_id = p.tenant_id AND pm.product_id = p.id AND ma.status = 'ready'
        ORDER BY pm.position LIMIT 1) AS image
FROM products p JOIN vendors v ON v.id = p.vendor_id AND v.tenant_id = p.tenant_id
WHERE p.tenant_id = :t AND p.id = ANY(CAST(:ids AS uuid[]))"""

FACETS_SQL = (
    """WITH """
    + CANDIDATES
    + """
SELECT
  (SELECT coalesce(json_agg(x ORDER BY x.count DESC), '[]') FROM
     (SELECT c.slug AS value, c.name_en AS label, count(*) AS count FROM cand
      JOIN categories c ON c.id = cand.category_id AND c.tenant_id = :t GROUP BY c.slug, c.name_en LIMIT 30) x) AS categories,
  (SELECT coalesce(json_agg(x ORDER BY x.count DESC), '[]') FROM
     (SELECT b.slug AS value, b.name AS label, count(*) AS count FROM cand
      JOIN brands b ON b.id = cand.brand_id AND b.tenant_id = :t GROUP BY b.slug, b.name LIMIT 30) x) AS brands,
  (SELECT coalesce(json_agg(x ORDER BY x.count DESC), '[]') FROM
     (SELECT vv.slug AS value, vv.display_name AS label, count(*) AS count FROM cand
      JOIN vendors vv ON vv.id = cand.vendor_id AND vv.tenant_id = :t AND NOT vv.is_house
      GROUP BY vv.slug, vv.display_name LIMIT 30) x) AS stores,
  (SELECT min(min_price) FROM cand) AS min_price, (SELECT max(max_price) FROM cand) AS max_price,
  (SELECT coalesce(json_agg(x), '[]') FROM
     (SELECT e.key, e.value, count(*) AS count FROM cand, jsonb_each_text(cand.attributes) e
      WHERE CAST(:filterable AS text[]) IS NOT NULL AND e.key = ANY(CAST(:filterable AS text[]))
      GROUP BY e.key, e.value ORDER BY count(*) DESC LIMIT 100) x) AS attributes"""
)


async def corrections(db, tenant_id: str, toks: list[str]) -> dict[str, list[str]]:
    """Typo correction against the tenant's distinct words (small trigram-indexed table)."""
    rows = (
        await db.execute(
            text(
                """SELECT t.tok, w.word FROM unnest(CAST(:toks AS text[])) AS t(tok)
           CROSS JOIN LATERAL (SELECT word FROM search_terms WHERE tenant_id = :t AND word % t.tok
                               ORDER BY similarity(word, t.tok) DESC LIMIT 2) w"""
            ),
            {"t": tenant_id, "toks": toks},
        )
    ).all()
    out: dict[str, list[str]] = {}
    for tok, word in rows:
        out.setdefault(tok, []).append(word)
    return out


async def index_terms(db, tenant_id: str, product_id) -> None:
    """Called on product writes: add the title words to the tenant's term list (pruned by a nightly job)."""
    await db.execute(
        text(
            """INSERT INTO search_terms (tenant_id, word)
           SELECT :t, lower(w) FROM products p,
                  regexp_split_to_table(coalesce(p.title_en, '') || ' ' || coalesce(p.title_bn, ''), '[^[:alnum:]\u0980-\u09ff]+') w
           WHERE p.id = :p AND p.tenant_id = :t AND length(w) BETWEEN 2 AND 40
           ON CONFLICT DO NOTHING"""
        ),
        {"t": tenant_id, "p": product_id},
    )


@router.get("/search", response_model=SearchOut)
async def search(
    request: Request,
    tenant: Tenant,
    db: TenantDB,
    q: str = Query("", max_length=100),
    category: str | None = Query(None, pattern=r"^[a-z0-9-]{1,80}$"),
    brand: list[str] = Query(default=[], max_length=10),
    store: list[str] = Query(default=[], max_length=10),
    min_price: Decimal | None = Query(None, ge=0),
    max_price: Decimal | None = Query(None, ge=0),
    in_stock: bool = False,
    attr: list[str] = Query(default=[], max_length=10, description="key:value"),
    sort: Literal["relevance", "newest", "price_asc", "price_desc"] = "relevance",
    page: int = Query(1, ge=1, le=50),
):
    toks = tokens(q)
    attrs = []
    for a in attr:
        k, _, v = a.partition(":")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", k) or not v or len(v) > 60:
            raise AppError("Invalid attribute filter", status=422, code="invalid_filter")
        attrs.append((k, v))
    for slug in brand + store:
        if not re.fullmatch(r"[a-z0-9-]{1,80}", slug):
            raise AppError("Invalid filter", status=422, code="invalid_filter")

    # Resolve slugs to ids for THIS tenant under RLS (one small query), so the search function takes ids.
    ref = (
        (
            await db.execute(
                text(
                    """SELECT
             (SELECT array_agg(c.id) FROM categories c WHERE c.tenant_id = :t AND (c.slug = :cat OR
                 (SELECT id FROM categories WHERE tenant_id = :t AND slug = :cat) = ANY(c.path))) AS cats,
             (SELECT array_agg(id) FROM brands WHERE tenant_id = :t AND slug = ANY(CAST(:brands AS text[]))) AS brands,
             (SELECT array_agg(id) FROM vendors WHERE tenant_id = :t AND slug = ANY(CAST(:stores AS text[]))) AS vendors,
             (SELECT array_agg(DISTINCT key) FROM category_attributes WHERE tenant_id = :t AND filterable) AS filterable"""
                ),
                {"t": tenant.id, "cat": category, "brands": brand, "stores": store},
            )
        )
        .mappings()
        .one()
    )
    if (
        (category and not ref["cats"])
        or (brand and not ref["brands"])
        or (store and not ref["vendors"])
    ):
        return SearchOut(
            query=q,
            total=0,
            page=page,
            items=[],
            facets={"categories": [], "brands": [], "stores": []},
            price_range={"min": None, "max": None},
            attributes={},
        )
    attr_filter: dict = {}
    for k, v in attrs:
        attr_filter[k] = v
    params: dict = {
        "t": tenant.id,
        "limit": PAGE_SIZE,
        "offset": (page - 1) * PAGE_SIZE,
        "cats": [str(x) for x in ref["cats"]] if category else None,
        "brands": [str(x) for x in ref["brands"]] if brand else None,
        "vendors": [str(x) for x in ref["vendors"]] if store else None,
        "minp": min_price,
        "maxp": max_price,
        "in_stock": in_stock,
        "attrs": json.dumps(attr_filter) if attr_filter else None,
        "filterable": list(ref["filterable"]) if ref["filterable"] else None,
    }
    synonyms = await tenant_synonyms(db, request.app.state.redis, tenant.id) if toks else {}
    order = {
        "relevance": "rank + quality * 0.1 + CASE WHEN in_stock THEN 0.2 ELSE 0 END DESC, id DESC",
        "newest": "id DESC",
        "price_asc": "min_price ASC NULLS LAST, id DESC",
        "price_desc": "max_price DESC NULLS LAST, id DESC",
    }[sort]

    async def run(extra: dict[str, list[str]]):
        merged = {
            k: list(synonyms.get(k, [])) + extra.get(k, []) for k in set(synonyms) | set(extra)
        }
        params["tsq"] = build_tsquery(toks, merged) if toks else None
        return (await db.execute(text(PAGE_SQL.format(order=order)), params)).all()

    page_rows = await run({})
    did_you_mean = None
    if toks and not page_rows and page == 1:
        fixes = await corrections(db, tenant.id, toks)
        if fixes:
            page_rows = await run(fixes)
            did_you_mean = " ".join(fixes.get(t, [t])[0] for t in toks)
    total = page_rows[0].total if page_rows else 0
    ids = [str(r.id) for r in page_rows]
    rows = []
    if ids:
        cards = (await db.execute(text(CARDS_SQL), {"t": tenant.id, "ids": ids})).mappings().all()
        by = {str(c["id"]): c for c in cards}
        rows = [by[i] for i in ids if i in by]  # RLS re-check: anything not visible here is dropped
        facets_row = (await db.execute(text(FACETS_SQL), params)).mappings().one()
    else:
        facets_row = {
            "categories": [],
            "brands": [],
            "stores": [],
            "min_price": None,
            "max_price": None,
            "attributes": [],
        }
    attributes: dict[str, list[Facet]] = {}
    for a in facets_row["attributes"]:
        attributes.setdefault(a["key"], []).append(
            Facet(value=a["value"], label=a["value"], count=a["count"])
        )
    items = [{k: (str(v) if k in ("id", "vendor_id") else v) for k, v in r.items()} for r in rows]
    return SearchOut(
        query=q,
        total=total,
        page=page,
        items=items,
        did_you_mean=did_you_mean,
        facets={
            "categories": [Facet(**f) for f in facets_row["categories"]],
            "brands": [Facet(**f) for f in facets_row["brands"]],
            "stores": [Facet(**f) for f in facets_row["stores"]],
        },
        price_range={"min": facets_row["min_price"], "max": facets_row["max_price"]},
        attributes=attributes,
    )


@router.get("/suggest")
async def suggest(
    request: Request, tenant: Tenant, db: TenantDB, q: str = Query(..., min_length=1, max_length=60)
):
    """Type-ahead: up to 8 product titles + matching categories, one query."""
    toks = tokens(q)
    if not toks:
        return {"products": [], "categories": []}
    tsq = build_tsquery(toks, await tenant_synonyms(db, request.app.state.redis, tenant.id))
    rows = (
        (
            await db.execute(
                text(
                    f"""(SELECT 'product' AS kind, p.slug, p.title_en AS label FROM products p
             JOIN vendors v ON v.id = p.vendor_id AND v.tenant_id = p.tenant_id
             WHERE {VISIBLE} AND p.search_vector @@ to_tsquery('simple', :tsq)
             ORDER BY ts_rank(p.search_vector, to_tsquery('simple', :tsq)) DESC LIMIT 8)
            UNION ALL
            (SELECT 'category', c.slug, c.name_en FROM categories c WHERE c.tenant_id = :t AND c.is_active
             AND (lower(c.name_en) LIKE :pref OR coalesce(c.name_bn, '') LIKE :pref) LIMIT 4)"""
                ),
                {"t": tenant.id, "tsq": tsq, "pref": toks[0] + "%"},
            )
        )
        .mappings()
        .all()
    )
    return {
        "products": [dict(r) for r in rows if r["kind"] == "product"],
        "categories": [dict(r) for r in rows if r["kind"] == "category"],
    }


class SynonymIn(BaseModel):
    term: str = Field(min_length=1, max_length=60)
    synonyms: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        min_length=1, max_length=20
    )


class SynonymOut(SynonymIn):
    id: uuid.UUID


@admin.get("", response_model=list[SynonymOut])
async def list_synonyms(_: Editor, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT id, term, synonyms FROM search_synonyms WHERE tenant_id = :t ORDER BY term"
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return [SynonymOut(**r) for r in rows]


@admin.post("", response_model=SynonymOut, status_code=201)
async def add_synonym(
    body: SynonymIn, request: Request, actor: Editor, tenant: Tenant, db: TenantDB
):
    try:
        async with db.begin_nested():
            row = (
                (
                    await db.execute(
                        text(
                            "INSERT INTO search_synonyms (tenant_id, term, synonyms) VALUES (:t, :term, :syn) RETURNING id, term, synonyms"
                        ),
                        {
                            "t": tenant.id,
                            "term": body.term.lower().strip(),
                            "syn": [s.lower().strip() for s in body.synonyms],
                        },
                    )
                )
                .mappings()
                .one()
            )
    except IntegrityError as exc:
        raise Conflict("Term already has synonyms") from exc
    await request.app.state.redis.delete(tkey(tenant.id, "search", "synonyms"))
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="search.synonym_add",
        entity="search_synonym",
        entity_id=row["id"],
        data={"term": body.term},
        request=request,
    )
    return SynonymOut(**row)


@admin.delete("/{synonym_id}", status_code=204)
async def delete_synonym(
    synonym_id: uuid.UUID, request: Request, actor: Editor, tenant: Tenant, db: TenantDB
):
    row = (
        await db.execute(
            text("DELETE FROM search_synonyms WHERE id = :i AND tenant_id = :t RETURNING term"),
            {"i": synonym_id, "t": tenant.id},
        )
    ).first()
    if row is None:
        raise NotFound("Not found")
    await request.app.state.redis.delete(tkey(tenant.id, "search", "synonyms"))
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="search.synonym_delete",
        entity="search_synonym",
        entity_id=synonym_id,
        request=request,
    )
