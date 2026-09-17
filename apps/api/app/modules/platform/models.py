import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_uuid_pk = {"primary_key": True, "server_default": text("uuid_generate_v7()")}


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_uuid_pk)
    public_id: Mapped[str] = mapped_column(
        String, server_default=text("encode(gen_random_bytes(9),'hex')")
    )
    slug: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, server_default="trial")
    store_mode: Mapped[str] = mapped_column(String, server_default="single")
    default_locale: Mapped[str] = mapped_column(String, server_default="bn")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Domain(Base):
    __tablename__ = "domains"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_uuid_pk)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    host: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, server_default="pending")
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    verification_token: Mapped[str] = mapped_column(
        String, server_default=text("encode(gen_random_bytes(16),'hex')")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TenantSettings(Base):
    __tablename__ = "tenant_settings"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    support_email: Mapped[str | None] = mapped_column(String)
    support_phone: Mapped[str | None] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String, server_default="Asia/Dhaka")
    currency: Mapped[str] = mapped_column(String, server_default="BDT")
