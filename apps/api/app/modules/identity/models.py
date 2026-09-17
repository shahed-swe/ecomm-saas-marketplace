import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_pk = {"primary_key": True, "server_default": text("uuid_generate_v7()")}
_ts = {"server_default": text("now()")}


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_pk)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    email: Mapped[str | None] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String)
    password_hash: Mapped[str | None] = mapped_column(String)
    full_name: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, server_default="active")
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_ts)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_pk)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    token_hash: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OtpChallenge(Base):
    __tablename__ = "otp_challenges"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_pk)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    phone: Mapped[str] = mapped_column(String)
    purpose: Mapped[str] = mapped_column(String)
    code_hash: Mapped[str] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_ts)


class StaffRole(Base):
    __tablename__ = "staff_roles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_pk)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    key: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(String), server_default="{}")
    is_system: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class StaffMember(Base):
    __tablename__ = "staff_members"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_pk)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String, server_default="active")


class VendorUser(Base):
    __tablename__ = "vendor_users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_pk)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    vendor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    role: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, server_default="active")


class PlatformUser(Base):
    __tablename__ = "platform_users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), **_pk)
    email: Mapped[str] = mapped_column(String)
    password_hash: Mapped[str] = mapped_column(String)
    totp_secret: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, server_default="active")
