import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

SLUG = r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$"
RESERVED_SLUGS = frozenset(
    {"www", "api", "admin", "app", "platform", "mail", "static", "cdn", "assets", "status", "docs"}
)


class TenantCreate(BaseModel):
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=2, max_length=120)
    store_mode: Literal["single", "multi"] = "single"
    default_locale: Literal["bn", "en"] = "bn"
    owner_email: EmailStr
    owner_password: str | None = Field(default=None, min_length=10, max_length=200)
    owner_name: str | None = Field(default=None, max_length=120)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    public_id: str
    slug: str
    name: str
    status: str
    store_mode: str
    default_locale: str


class TenantCreated(TenantOut):
    primary_host: str
    house_vendor_id: uuid.UUID
    owner_user_id: uuid.UUID


class TenantPatch(BaseModel):
    status: Literal["trial", "active", "past_due", "suspended", "cancelled"] | None = None
    store_mode: Literal["single", "multi"] | None = None
    name: str | None = Field(default=None, min_length=2, max_length=120)
