---
name: fastapi-module-scaffold
description: Scaffold a new FastAPI vertical-slice module (router, schemas, models, service, repository, migration, tests) following this project's exact conventions. Use this skill whenever adding any new backend domain or resource — products, orders, coupons, reviews, vendors, payouts — or when asked to "add an API for X", even if the user doesn't mention structure or scaffolding.
---

# FastAPI Vertical-Slice Module

Every backend domain has the same six files. Consistency is the point: a
reviewer should be able to read any module having read one.

## Files

```
app/modules/<domain>/
  __init__.py
  models.py       # SQLAlchemy 2.0, Mapped[] annotations, UUIDv7 pk, timestamps
  schemas.py      # <Domain>Create / <Domain>Update / <Domain>Out / <Domain>List
  repository.py   # pure queries, takes AsyncSession, returns models/rows
  service.py      # business rules, transactions, raises AppError subclasses
  router.py       # APIRouter(prefix="/<plural>", tags=["<domain>"])
```
Then register in `app/api.py` and write `alembic/versions/<rev>_add_<domain>.py`.

## Layer contract

| Layer | May import | May NOT |
|---|---|---|
| router | schemas, service, deps | SQLAlchemy, repository |
| service | repository, schemas, errors | `Request`, `Response`, `HTTPException` |
| repository | models, sqlalchemy | schemas, service |

Breaking this is the single most common drift. Enforce it in review.

## Canonical router

```python
router = APIRouter(prefix="/products", tags=["products"])

@router.get("", response_model=ProductList)
async def list_products(
    q: ProductQuery = Depends(),
    session: AsyncSession = Depends(get_session),
) -> ProductList:
    return await ProductService(session).list(q)

@router.post("", response_model=ProductOut, status_code=201,
             responses={409: {"model": ErrorOut}})
async def create_product(
    payload: ProductCreate,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_role("admin")),
) -> ProductOut:
    return await ProductService(session).create(payload)
```

## Schema rules

- `model_config = ConfigDict(extra="forbid", from_attributes=True)`.
- Separate Create and Update models; Update has all-optional fields and uses
  `model_fields_set` so "set to null" is distinguishable from "not provided".
- Out models are explicit field lists. Never `ProductOut = ProductModel`.
- Money fields are `int` minor units with a `currency: str` sibling.
- Every list response: `{items: [...], next_cursor: str | None}`.

## Query/pagination helper

Keyset, not offset — offset pagination on a large catalogue degrades and skips
rows under concurrent inserts.

```python
class ProductQuery(BaseModel):
    limit: int = Field(20, le=100, ge=1)
    cursor: str | None = None      # base64 of (created_at, id)
    category_id: UUID | None = None
    min_price: int | None = None
    sort: Literal["new", "price_asc", "price_desc"] = "new"
```

## Service rules

- Own the transaction: `async with session.begin():` at the top of any
  multi-write operation.
- Raise domain errors (`NotFound("product", id)`, `Conflict("sku_taken")`) —
  never construct HTTP responses.
- Lock before you decrement: `FOR UPDATE` on the variant row inside the same
  transaction as the order write.

## Tests to write alongside

`tests/modules/test_<domain>.py` — create (201), create duplicate (409), list
pagination across a cursor boundary, get missing (404), update as non-owner
(403), unauthenticated (401), invalid payload (422).

## Checklist before you call it done
- [ ] Registered in `app/api.py`
- [ ] Migration written and reversible
- [ ] OpenAPI shows correct models and error responses
- [ ] No lazy-loaded relationship in any serialiser
- [ ] Audit log entry on every mutation
