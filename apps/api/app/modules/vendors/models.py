import uuid

from sqlalchemy import Boolean, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v7()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    slug: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, server_default="registered")
    is_house: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class VendorStorefront(Base):
    __tablename__ = "vendor_storefronts"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v7()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    vendor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    tagline: Mapped[str | None] = mapped_column(String)
    bio: Mapped[str | None] = mapped_column(String)
    return_policy: Mapped[str | None] = mapped_column(String)
    holiday_mode: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
