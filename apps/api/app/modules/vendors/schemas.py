import uuid

from pydantic import BaseModel, ConfigDict, Field


class VendorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    slug: str
    display_name: str
    status: str
    is_house: bool


class VendorCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
    display_name: str = Field(min_length=2, max_length=120)


class StorefrontOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    vendor_id: uuid.UUID
    tagline: str | None
    bio: str | None
    return_policy: str | None
    holiday_mode: bool


class StorefrontUpdate(BaseModel):
    tagline: str | None = Field(default=None, max_length=160)
    bio: str | None = Field(default=None, max_length=4000)
    return_policy: str | None = Field(default=None, max_length=8000)
    holiday_mode: bool | None = None
