"""Vendor onboarding (Phase 6): signup, KYC to the private bucket, submission, admin review,
invite codes, commission overrides, payout destination with re-auth + hold."""

import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

import jwt
from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, EmailStr, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core import audit
from app.core.cache_keys import object_key, tkey
from app.core.crypto import Encryptor, keyed_hash, last4
from app.core.deps import Tenant, TenantDB, require_tenant_staff, require_vendor_role
from app.core.errors import AppError, Conflict, Forbidden, NotFound, Unauthorized, problem_response
from app.core.passwords import MIN_LENGTH, hash_password, verify_password
from app.core.phone import normalize_bd_phone
from app.core.ratelimit import hit
from app.core.security import Principal
from app.modules.billing.service import require_quota
from app.modules.identity import service as identity
from app.modules.vendors import lifecycle

public = APIRouter(prefix="/api/v1", tags=["vendor-signup"])
vendor = APIRouter(prefix="/api/v1/vendor", tags=["vendor:onboarding"])
admin = APIRouter(prefix="/api/v1/admin", tags=["admin:vendor-review"])
internal = APIRouter(prefix="/internal", include_in_schema=False)

VendorOwner = Annotated[Principal, Depends(require_vendor_role("*"))]
VendorEditor = Annotated[Principal, Depends(require_vendor_role("storefront.write"))]
Reviewer = Annotated[Principal, Depends(require_tenant_staff("vendors.write"))]
Viewer = Annotated[Principal, Depends(require_tenant_staff("vendors.read"))]

DOC_TYPES = Literal["trade_licence", "nid", "tin", "bin", "bank_proof", "other"]
DOC_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_DOC_BYTES = 10 * 1024 * 1024
HOLD_HOURS = 72
REAUTH_TTL = 300


# ---------------------------------------------------------------------------------------------- signup
class SignupIn(BaseModel):
    display_name: str = Field(min_length=2, max_length=120)
    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
    legal_name: str = Field(min_length=2, max_length=200)
    business_type: Literal["individual", "proprietorship", "partnership", "company"]
    contact_phone: str = Field(max_length=20)
    district: str = Field(min_length=2, max_length=60)
    email: EmailStr
    password: str = Field(min_length=MIN_LENGTH, max_length=200)
    invite_code: str | None = Field(default=None, max_length=40)
    accept_agreement: bool

    @model_validator(mode="after")
    def _agree(self):
        if not self.accept_agreement:
            raise ValueError("You must accept the seller agreement")
        return self


class SignupOut(BaseModel):
    vendor_id: uuid.UUID
    status: str
    access_token: str
    expires_in: int


@public.post("/vendor-signup", response_model=SignupOut, status_code=201)
async def vendor_signup(
    body: SignupIn, request: Request, response: Response, tenant: Tenant, db: TenantDB
):
    await hit(
        request.app.state.redis,
        tkey(tenant.id, "rl", "vendor-signup", request.client.host if request.client else "x"),
        10,
        3600,
    )
    cfg = (
        (
            await db.execute(
                text("""SELECT t.store_mode, s.vendor_signup, s.default_commission_rate
                                    FROM tenants t JOIN tenant_settings s ON s.tenant_id = t.id WHERE t.id = :t"""),
                {"t": tenant.id},
            )
        )
        .mappings()
        .one()
    )
    if cfg["store_mode"] != "multi" or cfg["vendor_signup"] == "closed":
        raise Forbidden("Seller registration is not open")
    phone = normalize_bd_phone(body.contact_phone)
    if phone is None:
        raise AppError("Enter a valid Bangladeshi mobile number", status=422, code="invalid_phone")

    invite = None
    if body.invite_code:
        invite = (
            (
                await db.execute(
                    text(
                        """SELECT * FROM vendor_invite_codes WHERE tenant_id = :t AND code = :c
               AND used_count < max_uses AND (expires_at IS NULL OR expires_at > now()) FOR UPDATE"""
                    ),
                    {"t": tenant.id, "c": body.invite_code.strip().upper()},
                )
            )
            .mappings()
            .first()
        )
        if invite is None:
            raise Conflict("Invite code is invalid or used")
    elif cfg["vendor_signup"] == "invite_only":
        raise Forbidden("An invite code is required")

    used = (
        await db.execute(
            text(
                "SELECT count(*) FROM vendors WHERE tenant_id = :t AND NOT is_house "
                "AND status NOT IN ('closed','rejected')"
            ),
            {"t": tenant.id},
        )
    ).scalar()
    await require_quota(db, tenant.id, "vendors", used)

    user = (
        (
            await db.execute(
                text(
                    "SELECT id, password_hash, status FROM users WHERE tenant_id = :t AND email = :e"
                ),
                {"t": tenant.id, "e": body.email.lower()},
            )
        )
        .mappings()
        .first()
    )
    if user is None:
        uid = (
            await db.execute(
                text("""INSERT INTO users (tenant_id, email, password_hash, phone)
                                        VALUES (:t, :e, :p, :ph) ON CONFLICT DO NOTHING RETURNING id"""),
                {
                    "t": tenant.id,
                    "e": body.email.lower(),
                    "p": hash_password(body.password),
                    "ph": phone,
                },
            )
        ).scalar()
        if uid is None:  # phone already used by another account in this store
            uid = (
                await db.execute(
                    text("""INSERT INTO users (tenant_id, email, password_hash)
                                            VALUES (:t, :e, :p) RETURNING id"""),
                    {"t": tenant.id, "e": body.email.lower(), "p": hash_password(body.password)},
                )
            ).scalar()
    elif user["status"] != "active" or not verify_password(user["password_hash"], body.password):
        raise Unauthorized("Invalid credentials")
    else:
        uid = user["id"]

    try:
        async with db.begin_nested():
            vid = (
                await db.execute(
                    text(
                        """INSERT INTO vendors (tenant_id, slug, display_name, legal_name, business_type, contact_email,
                       contact_phone, district, tier, agreement_accepted_at)
                   VALUES (:t, :slug, :dn, :ln, :bt, :ce, :cp, :d, :tier, now()) RETURNING id"""
                    ),
                    {
                        "t": tenant.id,
                        "slug": body.slug,
                        "dn": body.display_name,
                        "ln": body.legal_name,
                        "bt": body.business_type,
                        "ce": body.email.lower(),
                        "cp": phone,
                        "d": body.district,
                        "tier": invite["tier"] if invite else "standard",
                    },
                )
            ).scalar()
    except IntegrityError as exc:
        raise Conflict("Shop link unavailable") from exc
    await db.execute(
        text("INSERT INTO vendor_storefronts (tenant_id, vendor_id) VALUES (:t, :v)"),
        {"t": tenant.id, "v": vid},
    )
    await db.execute(
        text(
            "INSERT INTO vendor_users (tenant_id, vendor_id, user_id, role) VALUES (:t, :v, :u, 'owner')"
        ),
        {"t": tenant.id, "v": vid, "u": uid},
    )
    if invite:
        await db.execute(
            text("UPDATE vendor_invite_codes SET used_count = used_count + 1 WHERE id = :i"),
            {"i": invite["id"]},
        )
        if invite["commission_rate"] is not None:
            await db.execute(
                text("""INSERT INTO commission_rules (tenant_id, scope, scope_id, rate, expires_at, created_by)
                                     VALUES (:t, 'vendor', :v, :r, :e, 'invite')"""),
                {
                    "t": tenant.id,
                    "v": vid,
                    "r": invite["commission_rate"],
                    "e": invite["commission_expires_at"],
                },
            )
    p = Principal(sub=str(uid), kind="vendor_staff", tid=tenant.id, vid=str(vid), roles=("owner",))
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="vendor.signup",
        entity="vendor",
        entity_id=vid,
        data={"slug": body.slug, "invite": bool(invite)},
        request=request,
    )
    pair, _ = await identity.issue_tokens(db, request.app.state.settings, p)
    response.set_cookie(
        "rt",
        pair.refresh_token,
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.cookie_secure,
        path="/api/v1/auth",
    )
    return SignupOut(
        vendor_id=vid,
        status="registered",
        access_token=pair.access_token,
        expires_in=pair.expires_in,
    )


# ------------------------------------------------------------------------------------------- KYC (vendor)
class UploadUrlIn(BaseModel):
    doc_type: DOC_TYPES
    content_type: str
    byte_size: int = Field(gt=0, le=MAX_DOC_BYTES)


class UploadUrlOut(BaseModel):
    url: str
    method: str
    headers: dict
    storage_key: str
    expires_in: int


class DocumentIn(BaseModel):
    doc_type: DOC_TYPES
    storage_key: str = Field(max_length=300)
    document_number: str | None = Field(default=None, max_length=40)


class DocumentOut(BaseModel):
    id: uuid.UUID
    doc_type: str
    status: str
    number_last4: str | None
    rejection_reason: str | None
    created_at: datetime


class OnboardingOut(BaseModel):
    status: str
    required_documents: list[str]
    missing_documents: list[str]
    documents: list[DocumentOut]
    can_submit: bool
    status_reason: str | None


def _doc_prefix(tenant_id: str, vendor_id: str) -> str:
    return object_key(tenant_id, "kyc", vendor_id) + "/"


async def _onboarding(db, tenant_id: str, vendor_id: str) -> OnboardingOut:
    v = (
        (
            await db.execute(
                text("SELECT status, status_reason FROM vendors WHERE id = :v AND tenant_id = :t"),
                {"v": vendor_id, "t": tenant_id},
            )
        )
        .mappings()
        .one()
    )
    required = (
        await db.execute(
            text("SELECT required_vendor_documents FROM tenant_settings WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    ).scalar()
    docs = (
        (
            await db.execute(
                text(
                    """SELECT id, doc_type, status, number_last4, rejection_reason, created_at FROM vendor_documents
           WHERE tenant_id = :t AND vendor_id = :v ORDER BY created_at DESC"""
                ),
                {"t": tenant_id, "v": vendor_id},
            )
        )
        .mappings()
        .all()
    )
    have = {d["doc_type"] for d in docs if d["status"] in ("pending", "approved")}
    missing = [d for d in required if d not in have]
    return OnboardingOut(
        status=v["status"],
        required_documents=list(required),
        missing_documents=missing,
        documents=[DocumentOut(**d) for d in docs],
        can_submit=not missing and v["status"] in ("registered", "changes_requested"),
        status_reason=v["status_reason"],
    )


@vendor.get("/onboarding", response_model=OnboardingOut)
async def onboarding_status(p: VendorEditor, tenant: Tenant, db: TenantDB):
    return await _onboarding(db, tenant.id, p.vid)


@vendor.post("/documents/upload-url", response_model=UploadUrlOut)
async def document_upload_url(body: UploadUrlIn, request: Request, p: VendorEditor, tenant: Tenant):
    if body.content_type not in DOC_CONTENT_TYPES:
        raise AppError("Upload a JPEG, PNG, WebP or PDF", status=422, code="invalid_document")
    await hit(request.app.state.redis, tkey(tenant.id, "rl", "kyc-upload", p.vid), 30, 3600)
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}[
        body.content_type
    ]
    key = f"{_doc_prefix(tenant.id, p.vid)}{body.doc_type}-{secrets.token_urlsafe(16)}.{ext}"
    up = request.app.state.private_storage.presign_put(
        key, content_type=body.content_type, max_bytes=body.byte_size
    )
    return UploadUrlOut(
        url=up.url, method=up.method, headers=up.headers, storage_key=key, expires_in=up.expires_in
    )


@vendor.post("/documents", response_model=DocumentOut, status_code=201)
async def register_document(
    body: DocumentIn, request: Request, p: VendorEditor, tenant: Tenant, db: TenantDB
):
    if not body.storage_key.startswith(_doc_prefix(tenant.id, p.vid)):
        raise NotFound("Upload not found")  # a key from another vendor/tenant is simply unknown
    storage = request.app.state.private_storage
    size = await storage.head(body.storage_key)
    if size is None:
        raise NotFound("Upload not found")
    status = (
        await db.execute(
            text("SELECT status FROM vendors WHERE id = :v AND tenant_id = :t"),
            {"v": p.vid, "t": tenant.id},
        )
    ).scalar()
    if status not in ("registered", "changes_requested", "approved"):
        raise Conflict("Documents cannot be changed while under review")
    settings = request.app.state.settings
    number_hash = (
        keyed_hash(settings, tenant.id, body.document_number) if body.document_number else None
    )
    ct = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf"}[
        body.storage_key.rsplit(".", 1)[-1]
    ]
    try:
        async with db.begin_nested():
            row = (
                (
                    await db.execute(
                        text(
                            """INSERT INTO vendor_documents (tenant_id, vendor_id, doc_type, storage_key, content_type, byte_size,
                       number_hash, number_last4)
                   VALUES (:t, :v, :dt, :k, :ct, :sz, :nh, :l4)
                   RETURNING id, doc_type, status, number_last4, rejection_reason, created_at"""
                        ),
                        {
                            "t": tenant.id,
                            "v": p.vid,
                            "dt": body.doc_type,
                            "k": body.storage_key,
                            "ct": ct,
                            "sz": size,
                            "nh": number_hash,
                            "l4": last4(body.document_number) if body.document_number else None,
                        },
                    )
                )
                .mappings()
                .one()
            )
    except IntegrityError as exc:
        raise Conflict("Document already registered") from exc
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="vendor.document_add",
        entity="vendor_document",
        entity_id=row["id"],
        data={"doc_type": body.doc_type},
        request=request,
    )
    return DocumentOut(**row)


@vendor.post("/onboarding/submit", response_model=OnboardingOut)
async def submit_onboarding(request: Request, p: VendorOwner, tenant: Tenant, db: TenantDB):
    state = await _onboarding(db, tenant.id, p.vid)
    if not state.can_submit:
        raise Conflict(
            "Upload all required documents first"
            if state.missing_documents
            else "Already submitted"
        )
    await lifecycle.transition(
        db,
        tenant_id=tenant.id,
        vendor_id=p.vid,
        to="documents_submitted",
        actor=p,
        performer="vendor",
        request=request,
    )
    await lifecycle.transition(
        db,
        tenant_id=tenant.id,
        vendor_id=p.vid,
        to="under_review",
        actor=None,
        performer="system",
        request=request,
    )
    return await _onboarding(db, tenant.id, p.vid)


# ------------------------------------------------------------------------------------------ admin review
class ReviewDoc(DocumentOut):
    view_url: str
    content_type: str
    duplicate_of_vendor_ids: list[uuid.UUID]


class ReviewOut(BaseModel):
    id: uuid.UUID
    slug: str
    display_name: str
    legal_name: str | None
    business_type: str | None
    contact_email: str | None
    contact_phone: str | None
    district: str | None
    status: str
    tier: str
    documents: list[ReviewDoc]
    duplicate_signals: list[str]
    events: list[dict]


@admin.get("/vendors/{vendor_id}/review", response_model=ReviewOut)
async def review(vendor_id: uuid.UUID, request: Request, _: Viewer, tenant: Tenant, db: TenantDB):
    v = (
        (
            await db.execute(
                text(
                    """SELECT id, slug, display_name, legal_name, business_type, contact_email, contact_phone, district, status, tier
           FROM vendors WHERE id = :v AND tenant_id = :t AND NOT is_house"""
                ),
                {"v": vendor_id, "t": tenant.id},
            )
        )
        .mappings()
        .first()
    )
    if v is None:
        raise NotFound("Not found")
    storage = request.app.state.private_storage
    docs = (
        (
            await db.execute(
                text(
                    """SELECT d.id, d.doc_type, d.status, d.number_last4, d.rejection_reason, d.created_at, d.storage_key,
                  d.content_type,
                  coalesce(array_agg(DISTINCT o.vendor_id) FILTER (WHERE o.vendor_id IS NOT NULL), '{}') AS dups
           FROM vendor_documents d
           LEFT JOIN vendor_documents o ON o.tenant_id = d.tenant_id AND o.number_hash = d.number_hash
                AND o.vendor_id <> d.vendor_id
           WHERE d.tenant_id = :t AND d.vendor_id = :v
           GROUP BY d.id ORDER BY d.created_at"""
                ),
                {"t": tenant.id, "v": vendor_id},
            )
        )
        .mappings()
        .all()
    )
    signals = []
    if v["contact_phone"]:
        n = (
            await db.execute(
                text(
                    "SELECT count(*) FROM vendors WHERE tenant_id = :t AND contact_phone = :p AND id <> :v"
                ),
                {"t": tenant.id, "p": v["contact_phone"], "v": vendor_id},
            )
        ).scalar()
        if n:
            signals.append(f"phone shared with {n} other vendor(s)")
    if any(d["dups"] for d in docs):
        signals.append("document number matches another vendor")
    events = (
        (
            await db.execute(
                text(
                    """SELECT from_status, to_status, reason, actor_kind, created_at FROM vendor_events
           WHERE tenant_id = :t AND vendor_id = :v ORDER BY created_at"""
                ),
                {"t": tenant.id, "v": vendor_id},
            )
        )
        .mappings()
        .all()
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=_,
        action="vendor.review_view",
        entity="vendor",
        entity_id=vendor_id,
        request=request,
    )
    return ReviewOut(
        **v,
        documents=[
            ReviewDoc(
                **{k: d[k] for k in DocumentOut.model_fields},
                content_type=d["content_type"],
                view_url=storage.presign_get(d["storage_key"]),
                duplicate_of_vendor_ids=list(d["dups"]),
            )
            for d in docs
        ],
        duplicate_signals=signals,
        events=[dict(e) for e in events],
    )


class TransitionIn(BaseModel):
    to: Literal["changes_requested", "approved", "rejected", "suspended", "closed"]
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _reason(self):
        if self.to in ("changes_requested", "rejected", "suspended") and not self.reason:
            raise ValueError("A reason is required")
        return self


@admin.post("/vendors/{vendor_id}/transition")
async def transition_vendor(
    vendor_id: uuid.UUID,
    body: TransitionIn,
    request: Request,
    actor: Reviewer,
    tenant: Tenant,
    db: TenantDB,
):
    house = (
        await db.execute(
            text("SELECT is_house FROM vendors WHERE id = :v AND tenant_id = :t"),
            {"v": vendor_id, "t": tenant.id},
        )
    ).scalar()
    if house is None:
        raise NotFound("Not found")
    if house:
        raise Conflict("The house vendor cannot change status")
    if body.to == "approved":
        pending = (
            await db.execute(
                text(
                    """SELECT count(*) FROM unnest((SELECT required_vendor_documents FROM tenant_settings WHERE tenant_id = :t)) r
               WHERE NOT EXISTS (SELECT 1 FROM vendor_documents d WHERE d.tenant_id = :t AND d.vendor_id = :v
                                 AND d.doc_type = r AND d.status = 'approved')"""
                ),
                {"t": tenant.id, "v": vendor_id},
            )
        ).scalar()
        current = (
            await db.execute(
                text("SELECT status FROM vendors WHERE id = :v AND tenant_id = :t"),
                {"v": vendor_id, "t": tenant.id},
            )
        ).scalar()
        if pending and current != "suspended":
            raise Conflict("Approve all required documents first")
    prev = await lifecycle.transition(
        db,
        tenant_id=tenant.id,
        vendor_id=vendor_id,
        to=body.to,
        actor=actor,
        performer="staff",
        reason=body.reason,
        request=request,
    )
    return {"id": str(vendor_id), "from": prev, "status": body.to}


class DocumentReviewIn(BaseModel):
    status: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=300)


@admin.patch("/vendor-documents/{document_id}", response_model=DocumentOut)
async def review_document(
    document_id: uuid.UUID,
    body: DocumentReviewIn,
    request: Request,
    actor: Reviewer,
    tenant: Tenant,
    db: TenantDB,
):
    if body.status == "rejected" and not body.reason:
        raise AppError("A reason is required", status=422, code="reason_required")
    row = (
        (
            await db.execute(
                text(
                    """UPDATE vendor_documents SET status = :s, rejection_reason = :r, reviewed_by = :a, reviewed_at = now()
           WHERE id = :d AND tenant_id = :t
           RETURNING id, doc_type, status, number_last4, rejection_reason, created_at"""
                ),
                {
                    "s": body.status,
                    "r": body.reason,
                    "a": actor.sub,
                    "d": document_id,
                    "t": tenant.id,
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action=f"vendor.document_{body.status}",
        entity="vendor_document",
        entity_id=document_id,
        data={"reason": body.reason},
        request=request,
    )
    return DocumentOut(**row)


class InviteIn(BaseModel):
    tier: Literal["launch", "standard", "premium"] = "standard"
    commission_rate: Decimal | None = Field(default=None, ge=0, lt=1)
    commission_expires_at: datetime | None = None
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_at: datetime | None = None


class InviteOut(BaseModel):
    id: uuid.UUID
    code: str
    tier: str
    max_uses: int


@admin.post("/vendor-invites", response_model=InviteOut, status_code=201)
async def create_invite(
    body: InviteIn, request: Request, actor: Reviewer, tenant: Tenant, db: TenantDB
):
    code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
    row = (
        (
            await db.execute(
                text(
                    """INSERT INTO vendor_invite_codes (tenant_id, code, tier, commission_rate, commission_expires_at, max_uses,
               expires_at, created_by)
           VALUES (:t, :c, :tier, :r, :ce, :m, :e, :a) RETURNING id, code, tier, max_uses"""
                ),
                {
                    "t": tenant.id,
                    "c": code,
                    "tier": body.tier,
                    "r": body.commission_rate,
                    "ce": body.commission_expires_at,
                    "m": body.max_uses,
                    "e": body.expires_at,
                    "a": actor.sub,
                },
            )
        )
        .mappings()
        .one()
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="vendor.invite_create",
        entity="vendor_invite",
        entity_id=row["id"],
        data=body.model_dump(mode="json"),
        request=request,
    )
    return InviteOut(**row)


class CommissionIn(BaseModel):
    rate: Decimal = Field(ge=0, lt=1)
    expires_at: datetime | None = None


@admin.post("/vendors/{vendor_id}/commission", status_code=201)
async def set_commission(
    vendor_id: uuid.UUID,
    body: CommissionIn,
    request: Request,
    actor: Reviewer,
    tenant: Tenant,
    db: TenantDB,
):
    exists = (
        await db.execute(
            text("SELECT 1 FROM vendors WHERE id = :v AND tenant_id = :t"),
            {"v": vendor_id, "t": tenant.id},
        )
    ).first()
    if not exists:
        raise NotFound("Not found")
    rid = (
        await db.execute(
            text(
                """INSERT INTO commission_rules (tenant_id, scope, scope_id, rate, expires_at, created_by)
           VALUES (:t, 'vendor', :v, :r, :e, :a) RETURNING id"""
            ),
            {"t": tenant.id, "v": vendor_id, "r": body.rate, "e": body.expires_at, "a": actor.sub},
        )
    ).scalar()
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="vendor.commission_override",
        entity="vendor",
        entity_id=vendor_id,
        data={"rate": str(body.rate)},
        request=request,
    )
    return {"id": str(rid), "rate": str(body.rate)}


@admin.post("/payout-holds/{hold_id}/release")
async def release_hold(
    hold_id: uuid.UUID,
    request: Request,
    actor: Annotated[Principal, Depends(require_tenant_staff("payouts.approve"))],
    tenant: Tenant,
    db: TenantDB,
):
    row = (
        await db.execute(
            text(
                """UPDATE payout_holds SET released_at = now(), released_by = :a
           WHERE id = :h AND tenant_id = :t AND released_at IS NULL RETURNING vendor_id"""
            ),
            {"a": actor.sub, "h": hold_id, "t": tenant.id},
        )
    ).first()
    if row is None:
        raise NotFound("Not found")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="payout_hold.release",
        entity="payout_hold",
        entity_id=hold_id,
        data={"vendor_id": str(row.vendor_id)},
        request=request,
    )
    return {"id": str(hold_id), "released": True}


# ----------------------------------------------------------------------------- payout destination (owner)
class ReauthVerifyIn(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class ReauthOut(BaseModel):
    reauth_token: str
    expires_in: int


async def _owner_phone(db, tenant_id: str, user_id: str) -> str:
    phone = (
        await db.execute(
            text("SELECT phone FROM users WHERE id = :u AND tenant_id = :t"),
            {"u": user_id, "t": tenant_id},
        )
    ).scalar()
    if not phone:
        raise Conflict("Add a verified mobile number to your account first")
    return phone


@vendor.post("/reauth/request", status_code=202)
async def reauth_request(request: Request, p: VendorOwner, tenant: Tenant, db: TenantDB):
    phone = await _owner_phone(db, tenant.id, p.sub)
    await hit(request.app.state.redis, tkey(tenant.id, "rl", "reauth", p.sub), 5, 3600)
    code = await identity.create_otp(db, request.app.state.settings, tenant.id, phone, "reauth")
    if code:
        await request.app.state.sms.send(
            tenant_id=tenant.id,
            phone=phone,
            message=f"{code} is your security code to change payout details. "
            "If this wasn't you, contact support now.",
        )
    return {"status": "sent"}


@vendor.post("/reauth/verify", response_model=ReauthOut)
async def reauth_verify(
    body: ReauthVerifyIn, request: Request, p: VendorOwner, tenant: Tenant, db: TenantDB
):
    settings = request.app.state.settings
    phone = await _owner_phone(db, tenant.id, p.sub)
    if not await identity.verify_otp(db, settings, tenant.id, phone, "reauth", body.code):
        return problem_response(request, identity.InvalidOtp("Code is invalid or expired"))
    now = int(time.time())
    token = jwt.encode(
        {
            "typ": "reauth",
            "sub": p.sub,
            "tid": tenant.id,
            "vid": p.vid,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + REAUTH_TTL,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    return ReauthOut(reauth_token=token, expires_in=REAUTH_TTL)


async def _consume_reauth(request: Request, token: str | None, p: Principal) -> None:
    if not token:
        raise Unauthorized("Re-authentication required")
    try:
        data = jwt.decode(token, request.app.state.settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise Unauthorized("Re-authentication required") from exc
    if data.get("typ") != "reauth" or (data.get("sub"), data.get("tid"), data.get("vid")) != (
        p.sub,
        p.tid,
        p.vid,
    ):
        raise Unauthorized("Re-authentication required")
    first = await request.app.state.redis.set(
        tkey(p.tid, "reauth-used", data["jti"]), "1", nx=True, ex=REAUTH_TTL
    )
    if not first:
        raise Unauthorized("Re-authentication required")


class PayoutMethodIn(BaseModel):
    method: Literal["bank", "bkash"]
    account_name: str = Field(min_length=2, max_length=120)
    account_number: str | None = Field(
        default=None, min_length=6, max_length=30, pattern=r"^[0-9 -]+$"
    )
    bank_name: str | None = Field(default=None, max_length=80)
    branch_name: str | None = Field(default=None, max_length=80)
    routing_number: str | None = Field(default=None, pattern=r"^\d{9}$")
    bkash_number: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _shape(self):
        if self.method == "bank" and not (
            self.account_number and self.bank_name and self.routing_number
        ):
            raise ValueError("Bank account number, bank name and routing number are required")
        if self.method == "bkash" and not self.bkash_number:
            raise ValueError("bKash number is required")
        return self


class PayoutMethodOut(BaseModel):
    method: str
    account_name: str
    last4: str
    bank_name: str | None
    branch_name: str | None
    on_hold_until: datetime | None


async def _payout_view(db, tenant_id: str, vendor_id: str) -> PayoutMethodOut | None:
    row = (
        (
            await db.execute(
                text(
                    """SELECT method, account_name, last4, bank_name, branch_name,
                  (SELECT max(hold_until) FROM payout_holds h WHERE h.tenant_id = m.tenant_id AND h.vendor_id = m.vendor_id
                     AND h.released_at IS NULL AND h.hold_until > now()) AS on_hold_until
           FROM vendor_payout_methods m WHERE tenant_id = :t AND vendor_id = :v AND status = 'active'"""
                ),
                {"t": tenant_id, "v": vendor_id},
            )
        )
        .mappings()
        .first()
    )
    return PayoutMethodOut(**row) if row else None


@vendor.get("/payout-method", response_model=PayoutMethodOut | None)
async def get_payout_method(p: VendorOwner, tenant: Tenant, db: TenantDB):
    return await _payout_view(db, tenant.id, p.vid)


@vendor.put("/payout-method", response_model=PayoutMethodOut)
async def set_payout_method(
    body: PayoutMethodIn,
    request: Request,
    p: VendorOwner,
    tenant: Tenant,
    db: TenantDB,
    x_reauth_token: str | None = Header(default=None),
):
    await _consume_reauth(request, x_reauth_token, p)
    settings = request.app.state.settings
    if body.method == "bkash":
        number = normalize_bd_phone(body.bkash_number or "")
        if number is None:
            raise AppError("Enter a valid bKash number", status=422, code="invalid_phone")
        details = {"bkash_number": number}
    else:
        number = "".join(ch for ch in body.account_number if ch.isdigit())
        details = {"account_number": number, "routing_number": body.routing_number}
    enc = Encryptor(settings.data_encryption_key)
    context = f"payout:{tenant.id}:{p.vid}"
    previous = (
        await db.execute(
            text(
                "SELECT id FROM vendor_payout_methods WHERE tenant_id = :t AND vendor_id = :v AND status = 'active' FOR UPDATE"
            ),
            {"t": tenant.id, "v": p.vid},
        )
    ).first()
    if previous:
        await db.execute(
            text("UPDATE vendor_payout_methods SET status = 'replaced' WHERE id = :i"),
            {"i": previous.id},
        )
    await db.execute(
        text(
            """INSERT INTO vendor_payout_methods (tenant_id, vendor_id, method, account_name, details_ciphertext,
               account_hash, last4, bank_name, branch_name, routing_number, created_by)
           VALUES (:t, :v, :m, :n, :c, :h, :l4, :b, :br, :r, :a)"""
        ),
        {
            "t": tenant.id,
            "v": p.vid,
            "m": body.method,
            "n": body.account_name,
            "c": enc.encrypt(details, context=context),
            "h": keyed_hash(settings, tenant.id, number),
            "l4": last4(number),
            "b": body.bank_name,
            "br": body.branch_name,
            "r": body.routing_number,
            "a": p.sub,
        },
    )
    if previous:
        await db.execute(
            text(
                """INSERT INTO payout_holds (tenant_id, vendor_id, reason, hold_until, created_by)
               VALUES (:t, :v, 'destination_change', :u, :a)"""
            ),
            {
                "t": tenant.id,
                "v": p.vid,
                "u": datetime.now(UTC) + timedelta(hours=HOLD_HOURS),
                "a": p.sub,
            },
        )
    contacts = (
        (
            await db.execute(
                text(
                    """SELECT u.phone AS owner_phone, v.contact_phone FROM users u, vendors v
           WHERE u.id = :u AND u.tenant_id = :t AND v.id = :v AND v.tenant_id = :t"""
                ),
                {"u": p.sub, "t": tenant.id, "v": p.vid},
            )
        )
        .mappings()
        .one()
    )
    msg = (
        f"Payout details for your shop were {'changed' if previous else 'added'} (ending {last4(number)}). "
        + (f"Payouts are held for {HOLD_HOURS} hours. " if previous else "")
        + "If this wasn't you, contact the marketplace immediately."
    )
    for phone in {contacts["owner_phone"], contacts["contact_phone"]} - {None}:
        await request.app.state.sms.send(tenant_id=tenant.id, phone=phone, message=msg)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="vendor.payout_method_set",
        entity="vendor",
        entity_id=p.vid,
        data={"method": body.method, "last4": last4(number), "changed": bool(previous)},
        request=request,
    )
    return await _payout_view(db, tenant.id, p.vid)


class ProfilePatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=120)
    legal_name: str | None = Field(default=None, min_length=2, max_length=200)
    district: str | None = Field(default=None, min_length=2, max_length=60)


@vendor.patch("/profile")
async def patch_profile(
    body: ProfilePatch, request: Request, p: VendorEditor, tenant: Tenant, db: TenantDB
):
    data = body.model_dump(exclude_unset=True)
    statements = {
        "display_name": "UPDATE vendors SET display_name = :val, updated_at = now() WHERE id = :v AND tenant_id = :t",
        "legal_name": "UPDATE vendors SET legal_name = :val, updated_at = now() WHERE id = :v AND tenant_id = :t",
        "district": "UPDATE vendors SET district = :val, updated_at = now() WHERE id = :v AND tenant_id = :t",
    }
    for col, val in data.items():
        await db.execute(text(statements[col]), {"val": val, "v": p.vid, "t": tenant.id})
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="vendor.profile_update",
        entity="vendor",
        entity_id=p.vid,
        data=data,
        request=request,
    )
    return {"updated": sorted(data)}


# -------------------------------------------------------------------- local private storage endpoint (dev/test)
@internal.put("/private/{token}", status_code=201)
async def private_put(token: str, request: Request):
    storage = request.app.state.private_storage
    if not hasattr(storage, "decode"):
        raise NotFound("Not found")
    try:
        claims = storage.decode(token)
    except jwt.PyJWTError as exc:
        raise NotFound("Not found") from exc
    if claims.get("typ") != "priv_put" or request.headers.get("content-type") != claims["ct"]:
        raise NotFound("Not found")
    body = await request.body()
    if not body or len(body) > claims["max"]:
        raise AppError("File too large", status=413, code="too_large")
    path = storage.path(claims["key"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return {"stored": True}


@internal.get("/private/{token}")
async def private_get(token: str, request: Request):
    storage = request.app.state.private_storage
    if not hasattr(storage, "decode"):
        raise NotFound("Not found")
    try:
        claims = storage.decode(token)
    except jwt.PyJWTError as exc:
        raise NotFound("Not found") from exc
    if claims.get("typ") != "priv_get":
        raise NotFound("Not found")
    path = storage.path(claims["key"])
    if not path.exists():
        raise NotFound("Not found")
    return Response(path.read_bytes(), headers={"cache-control": "private, no-store"})
