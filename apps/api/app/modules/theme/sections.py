"""Section registry for the page builder (architecture §5.2).

A section is data: {id, type, hidden, settings}. Settings are validated by a per-type Pydantic model
so nothing a tenant types can become markup or a query. Sections reference ids/slugs; the storefront
resolver fetches data through scoped queries with a fixed budget.
"""

import re
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

PAGES = ("home", "category", "product", "store", "custom")
MAX_SECTIONS_PER_PAGE = 25
_SAFE_HREF = re.compile(
    r"^(/[A-Za-z0-9\-._~/?=&%#]*|https://[A-Za-z0-9.\-]+(/[A-Za-z0-9\-._~/?=&%#]*)?)$"
)
_ASSET = re.compile(r"^/media/t/[0-9a-f-]{36}/[A-Za-z0-9/_\-.]+$")
VIDEO_HOSTS = ("www.youtube.com", "youtube.com", "youtu.be", "www.facebook.com", "player.vimeo.com")
SOCIAL_HOSTS = {
    "facebook": ("facebook.com", "www.facebook.com", "m.facebook.com"),
    "instagram": ("instagram.com", "www.instagram.com"),
    "youtube": ("youtube.com", "www.youtube.com"),
    "tiktok": ("tiktok.com", "www.tiktok.com"),
    "linkedin": ("linkedin.com", "www.linkedin.com"),
}


def safe_href(v: str | None) -> str | None:
    if v is None:
        return v
    v = v.strip()
    if not _SAFE_HREF.match(v) or "//" in v.removeprefix("https://"):
        raise ValueError("links must be a path like /c/sarees or an https:// URL")
    return v


def asset_url(v: str | None) -> str | None:
    if v is not None and not _ASSET.match(v):
        raise ValueError("upload the image instead of linking it")
    return v


class Text(BaseModel):
    """Bilingual short text. Plain text only; rendered escaped."""

    model_config = ConfigDict(extra="forbid")
    en: str = Field(default="", max_length=300)
    bn: str = Field(default="", max_length=300)


class LongText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    en: str = Field(default="", max_length=5000)
    bn: str = Field(default="", max_length=5000)


class Link(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Text = Text()
    href: str = Field(max_length=300)
    _v = field_validator("href")(lambda cls, v: safe_href(v))


class Slide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_url: str
    mobile_image_url: str | None = None
    heading: Text = Text()
    subheading: Text = Text()
    cta: Link | None = None
    _img = field_validator("image_url", "mobile_image_url")(lambda cls, v: asset_url(v))


ProductSource = Literal["category", "brand", "manual", "newest", "store"]


class ProductQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: ProductSource = "newest"
    category_slug: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,80}$")
    brand_slug: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,80}$")
    store_slug: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,80}$")
    product_ids: list[uuid.UUID] = Field(default_factory=list, max_length=24)
    limit: int = Field(default=8, ge=1, le=24)

    @model_validator(mode="after")
    def _source(self):
        need = {
            "category": self.category_slug,
            "brand": self.brand_slug,
            "store": self.store_slug,
            "manual": self.product_ids or None,
        }
        if self.source in need and not need[self.source]:
            raise ValueError(f"{self.source} source needs its value")
        return self


# ----------------------------------------------------------------------------- section settings
class AnnouncementBar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Text
    link: Link | None = None
    tone: Literal["primary", "accent", "neutral"] = "primary"


class HeroSlider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slides: list[Slide] = Field(min_length=1, max_length=6)
    autoplay_seconds: int = Field(default=6, ge=0, le=15)
    height: Literal["small", "medium", "large"] = "medium"


class ImageBanner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_url: str
    link: str | None = None
    alt: Text = Text()
    _img = field_validator("image_url")(lambda cls, v: asset_url(v))
    _href = field_validator("link")(lambda cls, v: safe_href(v))


class BannerGrid(BaseModel):
    model_config = ConfigDict(extra="forbid")
    banners: list[ImageBanner] = Field(min_length=2, max_length=6)
    columns: Literal[2, 3] = 2


class CategoryGrid(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Text = Text()
    category_slugs: list[Annotated[str, Field(pattern=r"^[a-z0-9-]{1,80}$")]] = Field(
        default_factory=list, max_length=12
    )
    style: Literal["circle", "card"] = "card"


class ProductCarousel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Text = Text()
    query: ProductQuery = ProductQuery()
    view_all: str | None = None
    _href = field_validator("view_all")(lambda cls, v: safe_href(v))


class ProductGrid(ProductCarousel):
    columns: Literal[2, 3, 4] = 4


class FlashSale(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Text
    ends_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})$")
    query: ProductQuery = ProductQuery()


class CampaignStrip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heading: Text
    link: Link | None = None
    image_url: str | None = None
    _img = field_validator("image_url")(lambda cls, v: asset_url(v))


class BrandStrip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Text = Text()
    brand_slugs: list[Annotated[str, Field(pattern=r"^[a-z0-9-]{1,80}$")]] = Field(
        default_factory=list, max_length=20
    )


class TopVendors(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Text = Text()
    limit: int = Field(default=8, ge=1, le=16)


class FeaturedVendor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store_slug: str = Field(pattern=r"^[a-z0-9-]{1,80}$")
    blurb: Text = Text()


class RichText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heading: Text = Text()
    body: LongText
    align: Literal["left", "center"] = "left"


class ImageWithText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_url: str
    heading: Text = Text()
    body: LongText = LongText()
    image_side: Literal["left", "right"] = "left"
    cta: Link | None = None
    _img = field_validator("image_url")(lambda cls, v: asset_url(v))


class FaqItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: Text
    answer: LongText


class Faq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Text = Text()
    items: list[FaqItem] = Field(min_length=1, max_length=30)


class Testimonial(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote: Text
    name: str = Field(max_length=60)


class Testimonials(BaseModel):
    """Tenant-entered and labelled as such on the storefront."""

    model_config = ConfigDict(extra="forbid")
    title: Text = Text()
    items: list[Testimonial] = Field(min_length=1, max_length=12)


class Newsletter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heading: Text
    show_app_badges: bool = False


class TrustBadges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    badges: list[
        Literal["cod", "easy_return", "original", "fast_delivery", "secure_payment", "support"]
    ] = Field(min_length=1, max_length=6)


class VideoEmbed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: HttpUrl
    title: Text = Text()

    @field_validator("url")
    @classmethod
    def _host(cls, v):
        if v.scheme != "https" or v.host not in VIDEO_HOSTS:
            raise ValueError("only YouTube, Facebook or Vimeo videos can be embedded")
        return v


class Spacer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size: Literal["sm", "md", "lg"] = "md"
    divider: bool = False


# PDP / listing layout blocks (positional, no free-form content)
class ProductLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gallery: Literal["thumbnails_left", "thumbnails_bottom", "grid"] = "thumbnails_bottom"
    blocks: list[
        Literal[
            "title",
            "price",
            "variants",
            "add_to_cart",
            "vendor_card",
            "delivery",
            "description",
            "specs",
            "questions",
            "reviews",
            "related",
        ]
    ] = Field(
        default_factory=lambda: [
            "title",
            "price",
            "variants",
            "add_to_cart",
            "vendor_card",
            "delivery",
            "description",
            "specs",
            "questions",
            "reviews",
            "related",
        ],
        min_length=3,
        max_length=11,
    )

    @model_validator(mode="after")
    def _unique(self):
        if len(set(self.blocks)) != len(self.blocks):
            raise ValueError("each block can appear once")
        for required in ("price", "add_to_cart"):
            if required not in self.blocks:
                raise ValueError(f"{required} block is required")
        return self


class CollectionLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns_desktop: Literal[3, 4, 5] = 4
    columns_mobile: Literal[1, 2] = 2
    filters: Literal["sidebar", "drawer"] = "sidebar"
    default_sort: Literal["newest", "price_asc", "price_desc", "popular"] = "newest"


# type -> (settings model, allowed pages, vendor_allowed, max per page, mobile_supported)
REGISTRY: dict[str, tuple[type[BaseModel], tuple[str, ...], bool, int, bool]] = {
    "announcement_bar": (AnnouncementBar, ("home", "category", "custom"), False, 1, True),
    "hero_slider": (HeroSlider, ("home", "custom", "store"), True, 1, True),
    "image_banner": (ImageBanner, PAGES, True, 5, True),
    "banner_grid": (BannerGrid, ("home", "custom", "category"), False, 4, True),
    "category_grid": (CategoryGrid, ("home", "custom"), False, 3, True),
    "product_carousel": (ProductCarousel, PAGES, True, 8, True),
    "product_grid": (ProductGrid, ("home", "custom", "store"), True, 4, True),
    "flash_sale": (FlashSale, ("home", "custom"), False, 2, True),
    "campaign_strip": (CampaignStrip, ("home", "category", "custom"), False, 4, True),
    "brand_strip": (BrandStrip, ("home", "custom"), False, 2, True),
    "top_vendors": (TopVendors, ("home", "custom"), False, 1, True),
    "featured_vendor": (FeaturedVendor, ("home", "custom"), False, 3, True),
    "rich_text": (RichText, PAGES, True, 10, True),
    "image_with_text": (ImageWithText, PAGES, True, 6, True),
    "faq": (Faq, ("home", "custom", "store", "product"), True, 2, True),
    "testimonials": (Testimonials, ("home", "custom", "store"), True, 2, True),
    "newsletter": (Newsletter, ("home", "custom"), False, 1, False),
    "trust_badges": (TrustBadges, PAGES, True, 2, True),
    "video_embed": (VideoEmbed, ("home", "custom", "store", "product"), True, 3, False),
    "spacer": (Spacer, PAGES, True, 10, True),
}


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9]{6,24}$")
    type: Literal[tuple(REGISTRY)]  # type: ignore[valid-type]
    hidden: bool = False
    settings: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _settings(self):
        model = REGISTRY[self.type][0]
        self.settings = model.model_validate(self.settings).model_dump(mode="json")
        return self


def validate_sections(sections: list[Section], page: str, *, vendor: bool = False) -> None:
    if len(sections) > MAX_SECTIONS_PER_PAGE:
        raise ValueError(f"{page}: at most {MAX_SECTIONS_PER_PAGE} sections")
    ids, counts = set(), {}
    for s in sections:
        _, pages, vendor_allowed, max_per_page, _ = REGISTRY[s.type]
        if page not in pages:
            raise ValueError(f"{s.type} cannot be used on the {page} page")
        if vendor and not vendor_allowed:
            raise ValueError(f"{s.type} is not available for store pages")
        counts[s.type] = counts.get(s.type, 0) + 1
        if counts[s.type] > max_per_page:
            raise ValueError(f"{s.type}: at most {max_per_page} per page")
        if s.id in ids:
            raise ValueError(f"duplicate section id {s.id}")
        ids.add(s.id)


# ------------------------------------------------------------------------------ layouts & pages
class MenuItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Text
    href: str = Field(max_length=300)
    children: list[Link] = Field(default_factory=list, max_length=12)
    _v = field_validator("href")(lambda cls, v: safe_href(v))


class Header(BaseModel):
    model_config = ConfigDict(extra="forbid")
    logo_position: Literal["left", "center"] = "left"
    sticky: bool = True
    show_search: bool = True
    menu: list[MenuItem] = Field(default_factory=list, max_length=12)


class FooterColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Text
    links: list[Link] = Field(default_factory=list, max_length=10)


class Footer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: list[FooterColumn] = Field(default_factory=list, max_length=4)
    social: dict[Literal["facebook", "instagram", "youtube", "tiktok", "linkedin"], HttpUrl] = (
        Field(default_factory=dict)
    )
    show_payment_icons: bool = True
    copyright: Text = Text()

    @field_validator("social")
    @classmethod
    def _social(cls, v):
        for k, url in v.items():
            if url.scheme != "https" or url.host not in SOCIAL_HOSTS[k]:
                raise ValueError(f"{k} link must be an https://{SOCIAL_HOSTS[k][0]} URL")
        return v


class Layouts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header: Header = Header()
    footer: Footer = Footer()


class Templates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    home: list[Section] = Field(default_factory=list)
    category_top: list[Section] = Field(default_factory=list)
    product_bottom: list[Section] = Field(default_factory=list)
    product_layout: ProductLayout = ProductLayout()
    collection_layout: CollectionLayout = CollectionLayout()

    @model_validator(mode="after")
    def _pages(self):
        validate_sections(self.home, "home")
        validate_sections(self.category_top, "category")
        validate_sections(self.product_bottom, "product")
        return self


RESERVED_PAGE_SLUGS = {
    "p",
    "c",
    "store",
    "cart",
    "checkout",
    "account",
    "api",
    "admin",
    "vendor",
    "search",
    "login",
    "media",
    "internal",
    "platform",
    "orders",
    "track",
}


class Seo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Text = Text()
    description: Text = Text()


class CustomPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")
    title: Text
    sections: list[Section] = Field(default_factory=list)
    seo: Seo = Seo()
    published: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.slug in RESERVED_PAGE_SLUGS:
            raise ValueError(f"'{self.slug}' is reserved")
        validate_sections(self.sections, "custom")
        return self


def new_section_id() -> str:
    return uuid.uuid4().hex[:12]
