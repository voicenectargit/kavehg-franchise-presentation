"""
Pydantic models for presentations
"""
from typing import Literal

from pydantic import BaseModel


Layout = Literal["single", "book"]
AccessMode = Literal["public", "access_code", "ambivo_session"]


class ChapterCreate(BaseModel):
    title: str
    slug: str | None = None
    order: int | None = None
    section: str | None = None
    content_type: Literal["markdown", "html"] = "markdown"
    markdown_content: str = ""
    html_content: str | None = None


class ChapterUpdate(BaseModel):
    title: str | None = None
    slug: str | None = None
    order: int | None = None
    section: str | None = None
    content_type: Literal["markdown", "html"] | None = None
    markdown_content: str | None = None
    html_content: str | None = None


class ChapterResponse(BaseModel):
    chapter_id: str
    order: int
    title: str
    slug: str
    section: str | None = None
    content_type: str = "markdown"
    indexed_at: str | None = None


class ChapterDetail(ChapterResponse):
    markdown_content: str
    html_content: str | None = None
    content_hash: str


class ThemeConfig(BaseModel):
    """Visual theme for a hosted page."""
    preset: str | None = None       # "modern", "minimal", "bold", "warm", "nature", "dark"
    primary_color: str = "#2563eb"
    secondary_color: str = "#4f46e5"
    accent_color: str = "#f59e0b"
    font_family: str = "System Default"
    dark_mode: bool = False
    custom_css: str = ""


class ThemeUpdate(BaseModel):
    preset: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    accent_color: str | None = None
    font_family: str | None = None
    dark_mode: bool | None = None
    custom_css: str | None = None


# Theme presets — coordinated palettes
THEME_PRESETS: dict[str, dict] = {
    "modern": {"primary_color": "#2563eb", "secondary_color": "#4f46e5", "accent_color": "#f59e0b", "font_family": "Inter", "dark_mode": False},
    "minimal": {"primary_color": "#475569", "secondary_color": "#64748b", "accent_color": "#94a3b8", "font_family": "DM Sans", "dark_mode": False},
    "bold": {"primary_color": "#7c3aed", "secondary_color": "#ec4899", "accent_color": "#f97316", "font_family": "Space Grotesk", "dark_mode": False},
    "warm": {"primary_color": "#ea580c", "secondary_color": "#d97706", "accent_color": "#dc2626", "font_family": "Nunito", "dark_mode": False},
    "nature": {"primary_color": "#059669", "secondary_color": "#10b981", "accent_color": "#0d9488", "font_family": "Outfit", "dark_mode": False},
    "dark": {"primary_color": "#06b6d4", "secondary_color": "#8b5cf6", "accent_color": "#f59e0b", "font_family": "JetBrains Mono", "dark_mode": True},
}


class HeaderConfig(BaseModel):
    enabled: bool = False
    logo_url: str | None = None  # served via /p/{slug}/logo
    link_url: str | None = None
    link_text: str | None = None
    email: str | None = None
    phone: str | None = None
    text: str | None = None


class HeaderUpdate(BaseModel):
    enabled: bool | None = None
    link_url: str | None = None
    link_text: str | None = None
    email: str | None = None
    phone: str | None = None
    text: str | None = None


class PresentationCreate(BaseModel):
    title: str
    markdown_content: str = ""
    html_content: str | None = None
    content_type: Literal["markdown", "html"] = "markdown"
    slug: str | None = None
    description: str | None = None
    tags: list[str] = []
    chat_enabled: bool = False
    access_protected: bool = False
    access_mode: AccessMode | None = None
    access_tenant_only: bool = False
    num_access_codes: int = 3
    header: HeaderUpdate | None = None
    theme: ThemeUpdate | None = None
    layout: Layout = "single"
    chapters: list[ChapterCreate] | None = None


class PresentationUpdate(BaseModel):
    title: str | None = None
    markdown_content: str | None = None
    html_content: str | None = None
    content_type: Literal["markdown", "html"] | None = None
    description: str | None = None
    tags: list[str] | None = None
    chat_enabled: bool | None = None
    access_protected: bool | None = None
    access_mode: AccessMode | None = None
    access_tenant_only: bool | None = None
    access_codes: list[str] | None = None
    regenerate_codes: int | None = None
    header: HeaderUpdate | None = None
    theme: ThemeUpdate | None = None
    layout: Layout | None = None


class PresentationResponse(BaseModel):
    id: str
    tenant_id: str
    title: str
    slug: str
    hosted_url: str
    kb_name: str
    content_type: str = "markdown"  # "markdown" or "html"
    is_published: bool
    chat_enabled: bool
    access_protected: bool = False
    access_mode: AccessMode = "public"
    access_tenant_only: bool = False
    access_codes: list[str] = []
    header: HeaderConfig = HeaderConfig()
    theme: ThemeConfig = ThemeConfig()
    description: str | None = None
    tags: list[str] = []
    layout: Layout = "single"
    chapters: list[ChapterResponse] = []
    created_at: str
    updated_at: str
    # Stats
    num_views: int = 0
    total_chat_queries: int = 0
    today_chat_queries: int = 0


class PresentationDetail(PresentationResponse):
    markdown_content: str
    html_content: str | None = None
    chapters: list[ChapterDetail] = []  # type: ignore[assignment]


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
