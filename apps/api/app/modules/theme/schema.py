"""Theme document contract (ADR 0012).

Pydantic is the single source; `scripts/export_theme_schema.py` emits JSON Schema into
packages/theme-schema, from which TypeScript and Dart types are generated. Every value is
validated so theme data can never become CSS/HTML injection.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.theme.sections import CustomPage, Layouts, Templates

RGB_PATTERN = r"^(25[0-5]|2[0-4]\d|1?\d?\d) (25[0-5]|2[0-4]\d|1?\d?\d) (25[0-5]|2[0-4]\d|1?\d?\d)$"

# Curated, self-hosted font pairs (Latin + Bangla). No arbitrary font URLs.
FONTS = Literal[
    "inter-hind-siliguri",
    "poppins-noto-sans-bengali",
    "lora-noto-serif-bengali",
    "roboto-baloo-da-2",
    "system",
]

Rgb = str


class Palette(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    primary: Rgb = Field(pattern=RGB_PATTERN)
    primary_fg: Rgb = Field(pattern=RGB_PATTERN, alias="primary-fg")
    secondary: Rgb = Field(pattern=RGB_PATTERN)
    accent: Rgb = Field(pattern=RGB_PATTERN)
    bg: Rgb = Field(pattern=RGB_PATTERN)
    surface: Rgb = Field(pattern=RGB_PATTERN)
    fg: Rgb = Field(pattern=RGB_PATTERN)
    muted: Rgb = Field(pattern=RGB_PATTERN)
    border: Rgb = Field(pattern=RGB_PATTERN)
    danger: Rgb = Field(pattern=RGB_PATTERN)
    success: Rgb = Field(pattern=RGB_PATTERN)


class Tokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    colors: Palette
    dark: Palette
    radius: Literal["0rem", "0.25rem", "0.5rem", "0.75rem", "1rem", "9999px"] = "0.5rem"
    font_body: FONTS = "inter-hind-siliguri"
    font_heading: FONTS = "inter-hind-siliguri"
    button_style: Literal["solid", "outline", "pill"] = "solid"
    density: Literal["compact", "comfortable"] = "comfortable"


class Brand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logo_url: str | None = Field(default=None, max_length=512)
    logo_dark_url: str | None = Field(default=None, max_length=512)
    favicon_url: str | None = Field(default=None, max_length=512)
    logo_height: int = Field(default=40, ge=16, le=120)

    @field_validator("logo_url", "logo_dark_url", "favicon_url")
    @classmethod
    def _only_own_assets(cls, v: str | None) -> str | None:
        # Only URLs minted by our upload endpoint (relative media path); never arbitrary hosts.
        if v is not None and not v.startswith("/media/t/"):
            raise ValueError("upload the image instead of linking it")
        return v


class ThemeDocument(BaseModel):
    """v1: tokens + brand (Phase 4). v2 adds layouts, templates, custom pages, custom CSS (Phase 8).
    v1 documents validate unchanged; missing v2 parts take defaults."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2] = 2
    preset: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")
    tokens: Tokens
    brand: Brand = Brand()
    layouts: Layouts = Layouts()
    templates: Templates = Templates()
    pages: list[CustomPage] = Field(default_factory=list, max_length=30)
    custom_css: str = Field(default="", max_length=50_000)

    @model_validator(mode="after")
    def _unique_pages(self):
        slugs = [p.slug for p in self.pages]
        if len(set(slugs)) != len(slugs):
            raise ValueError("page slugs must be unique")
        return self
