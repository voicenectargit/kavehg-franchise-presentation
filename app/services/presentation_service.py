"""
Presentation CRUD + KB lifecycle
"""
import logging
import re
import secrets
import string
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from slugify import slugify

from app.db import get_db
from app.models.presentation import PresentationCreate, PresentationUpdate, PresentationResponse, PresentationDetail, ChapterDetail, HeaderConfig, ThemeConfig, THEME_PRESETS
from app.services import kb_service
from app.services.access_control import default_access_dict_for_creator
from app.services.html_converter import html_to_markdown, _fallback_strip_tags
from app.config import settings

logger = logging.getLogger(__name__)
COLLECTION = "content_presentations"
CHAT_QUERIES_COLLECTION = "content_chat_queries"


_CODE_CHARS = string.ascii_uppercase + string.digits  # A-Z, 0-9
_CODE_RE = re.compile(r'^[A-Z0-9]{3,12}$')


def _validate_access_codes(codes: list[str]) -> list[str]:
    """Normalize to uppercase, deduplicate, and validate access codes."""
    seen = set()
    result = []
    for code in codes:
        code = code.strip().upper()
        if not _CODE_RE.match(code):
            raise ValueError(f"Invalid access code '{code}': must be 3-12 characters, A-Z and 0-9 only")
        if code not in seen:
            seen.add(code)
            result.append(code)
    return sorted(result)


def _generate_access_codes(count: int = 3) -> list[str]:
    """Generate unique 6-char alphanumeric access codes."""
    codes = set()
    while len(codes) < count:
        codes.add("".join(secrets.choice(_CODE_CHARS) for _ in range(6)))
    return sorted(codes)


def _kb_name(tenant_id: str, slug: str) -> str:
    return f"content_{tenant_id[:8]}_{slug}"


def _build_header(doc: dict, base_url: str = "") -> HeaderConfig:
    h = doc.get("header") or {}
    has_logo = doc.get("has_header_logo", False)
    logo_url = f"{base_url}/p/{doc['slug']}/logo" if has_logo else None
    return HeaderConfig(
        enabled=h.get("enabled", False),
        logo_url=logo_url,
        link_url=h.get("link_url"),
        link_text=h.get("link_text"),
        email=h.get("email"),
        phone=h.get("phone"),
        text=h.get("text"),
    )


def _resolve_theme(theme_update) -> dict:
    """Resolve a ThemeUpdate into a complete theme dict, applying preset if specified."""
    if not theme_update:
        return {}
    data = theme_update.model_dump(exclude_none=True) if hasattr(theme_update, 'model_dump') else theme_update
    preset_name = data.get("preset")
    if preset_name and preset_name in THEME_PRESETS:
        merged = dict(THEME_PRESETS[preset_name])
        merged["preset"] = preset_name
        # Individual overrides take precedence over preset
        for key in ["primary_color", "secondary_color", "accent_color", "font_family", "dark_mode", "custom_css"]:
            if key in data and key not in THEME_PRESETS.get(preset_name, {}):
                merged[key] = data[key]
            elif key in data and data[key] != THEME_PRESETS.get(preset_name, {}).get(key):
                # Only override if explicitly different from preset default
                pass
        return merged
    return data


def _effective_access_mode(doc: dict) -> str:
    """Resolve the access mode honoring legacy access_protected."""
    mode = doc.get("access_mode")
    if mode:
        return mode
    if doc.get("access_protected") and doc.get("access_codes"):
        return "access_code"
    return "public"


def _build_theme(doc: dict) -> ThemeConfig:
    t = doc.get("theme") or {}
    return ThemeConfig(
        preset=t.get("preset"),
        primary_color=t.get("primary_color", "#2563eb"),
        secondary_color=t.get("secondary_color", "#4f46e5"),
        accent_color=t.get("accent_color", "#f59e0b"),
        font_family=t.get("font_family", "System Default"),
        dark_mode=t.get("dark_mode", False),
        custom_css=t.get("custom_css", ""),
    )


def _doc_to_response(doc: dict, base_url: str = "", stats: dict | None = None) -> PresentationResponse:
    s = stats or {}
    return PresentationResponse(
        id=str(doc["_id"]),
        tenant_id=doc["tenant_id"],
        title=doc["title"],
        slug=doc["slug"],
        hosted_url=f"{base_url}/p/{doc['slug']}" if base_url else f"/p/{doc['slug']}",
        kb_name=doc["kb_name"],
        content_type=doc.get("content_type", "markdown"),
        is_published=doc.get("is_published", True),
        chat_enabled=doc.get("chat_enabled", True),
        access_protected=doc.get("access_protected", False),
        access_mode=_effective_access_mode(doc),
        access_tenant_only=bool(doc.get("access_tenant_only", False)),
        access_codes=doc.get("access_codes", []),
        header=_build_header(doc, base_url),
        theme=_build_theme(doc),
        description=doc.get("description"),
        tags=doc.get("tags", []),
        created_at=doc["created_at"].isoformat() if isinstance(doc["created_at"], datetime) else str(doc["created_at"]),
        updated_at=doc["updated_at"].isoformat() if isinstance(doc["updated_at"], datetime) else str(doc["updated_at"]),
        num_views=doc.get("num_views", 0),
        total_chat_queries=s.get("total", 0),
        today_chat_queries=s.get("today", 0),
    )


def _doc_to_chapter_detail(chapter: dict) -> ChapterDetail:
    return ChapterDetail(
        chapter_id=str(chapter["chapter_id"]),
        order=int(chapter.get("order", 0)),
        title=chapter.get("title", ""),
        slug=chapter.get("slug", ""),
        section=chapter.get("section"),
        content_type=chapter.get("content_type", "markdown"),
        indexed_at=chapter.get("indexed_at"),
        markdown_content=chapter.get("markdown_content", ""),
        html_content=chapter.get("html_content"),
        content_hash=chapter.get("content_hash", ""),
    )


def _doc_to_detail(doc: dict, base_url: str = "", stats: dict | None = None) -> PresentationDetail:
    s = stats or {}
    chapters = sorted(doc.get("chapters") or [], key=lambda c: c.get("order", 0))
    return PresentationDetail(
        chapters=[_doc_to_chapter_detail(c) for c in chapters],
        id=str(doc["_id"]),
        tenant_id=doc["tenant_id"],
        title=doc["title"],
        slug=doc["slug"],
        hosted_url=f"{base_url}/p/{doc['slug']}" if base_url else f"/p/{doc['slug']}",
        kb_name=doc["kb_name"],
        content_type=doc.get("content_type", "markdown"),
        is_published=doc.get("is_published", True),
        chat_enabled=doc.get("chat_enabled", True),
        access_protected=doc.get("access_protected", False),
        access_mode=_effective_access_mode(doc),
        access_tenant_only=bool(doc.get("access_tenant_only", False)),
        access_codes=doc.get("access_codes", []),
        header=_build_header(doc, base_url),
        theme=_build_theme(doc),
        description=doc.get("description"),
        tags=doc.get("tags", []),
        created_at=doc["created_at"].isoformat() if isinstance(doc["created_at"], datetime) else str(doc["created_at"]),
        updated_at=doc["updated_at"].isoformat() if isinstance(doc["updated_at"], datetime) else str(doc["updated_at"]),
        markdown_content=doc.get("markdown_content", ""),
        html_content=doc.get("html_content"),
        num_views=doc.get("num_views", 0),
        total_chat_queries=s.get("total", 0),
        today_chat_queries=s.get("today", 0),
    )


async def _chat_query_stats(presentation_ids: list[str]) -> dict[str, dict]:
    """Return {presentation_id: {"total": N, "today": N}} for the given IDs."""
    if not presentation_ids:
        return {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    coll = get_db()["content_chat_queries"]

    # Total counts per presentation
    pipeline_total = [
        {"$match": {"presentation_id": {"$in": presentation_ids}}},
        {"$group": {"_id": "$presentation_id", "total": {"$sum": 1}}},
    ]
    # Today counts per presentation
    pipeline_today = [
        {"$match": {"presentation_id": {"$in": presentation_ids}, "date": today}},
        {"$group": {"_id": "$presentation_id", "today": {"$sum": 1}}},
    ]

    totals = {r["_id"]: r["total"] async for r in coll.aggregate(pipeline_total)}
    todays = {r["_id"]: r["today"] async for r in coll.aggregate(pipeline_today)}

    return {
        pid: {"total": totals.get(pid, 0), "today": todays.get(pid, 0)}
        for pid in presentation_ids
    }


async def _ensure_indexes():
    coll = get_db()[COLLECTION]
    await coll.create_index("slug", unique=True)
    await coll.create_index("tenant_id")


async def _unique_slug(base_slug: str) -> str:
    coll = get_db()[COLLECTION]
    slug = base_slug
    while await coll.find_one({"slug": slug}):
        slug = f"{base_slug}-{secrets.token_hex(3)}"
    return slug


async def create(
    tenant_id: str, user_id: str, data: PresentationCreate, base_url: str = ""
) -> PresentationResponse:
    await _ensure_indexes()
    coll = get_db()[COLLECTION]

    slug = await _unique_slug(slugify(data.slug or data.title))
    kb_name = _kb_name(tenant_id, slug)

    content_type = data.content_type or "markdown"

    # Determine the text to index into KB (always clean Markdown, never raw HTML)
    if content_type == "html":
        if not data.html_content or not data.html_content.strip():
            raise ValueError("HTML content is required when content type is 'html'")
        html_content = data.html_content
        kb_text = html_to_markdown(html_content)
        if not kb_text.strip():
            logger.warning("html_to_markdown returned empty for slug=%s, using stripped text fallback", slug)
            kb_text = _fallback_strip_tags(html_content)
        markdown_content = kb_text  # store converted MD for KB reference
    else:
        content_type = "markdown"
        kb_text = data.markdown_content
        markdown_content = data.markdown_content
        html_content = None

    if not kb_text or not kb_text.strip():
        raise ValueError("Content cannot be empty")

    # Create KB + index content
    try:
        await kb_service.create_kb(kb_name, tenant_id, user_id)
        await kb_service.index_text(kb_name, kb_text, tenant_id, user_id, display_file_name=data.title)
    except Exception:
        logger.exception(f"KB creation/indexing failed for {kb_name}")
        raise

    # Resolve initial access mode. Explicit access_mode wins; legacy
    # access_protected boolean is honored when access_mode is omitted.
    if data.access_mode is not None:
        access_mode = data.access_mode
    elif data.access_protected:
        access_mode = "access_code"
    else:
        access_mode = "public"
    access_protected = access_mode == "access_code"
    access_codes = (
        _generate_access_codes(data.num_access_codes) if access_protected else []
    )

    now = datetime.now(timezone.utc)
    doc = {
        "tenant_id": tenant_id,
        "userid": user_id,
        "title": data.title,
        "slug": slug,
        "content_type": content_type,
        "markdown_content": markdown_content,
        "html_content": html_content,
        "kb_name": kb_name,
        "is_published": True,
        "chat_enabled": data.chat_enabled,
        "access_protected": access_protected,
        "access_mode": access_mode,
        "access_tenant_only": bool(data.access_tenant_only),
        "access_codes": access_codes,
        "description": data.description,
        "tags": data.tags,
        "header": data.header.model_dump(exclude_none=True) if data.header else {},
        "theme": _resolve_theme(data.theme),
        # Default: full access for every tenant user. Tighten by editing
        # access_dict to specific userids — see access_control.py.
        "access_dict": default_access_dict_for_creator(user_id),
        "applied_team_ids": [],
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = await coll.insert_one(doc)
    except Exception:
        logger.warning("insert_one failed for slug=%s, cleaning up orphaned KB %s", slug, kb_name)
        try:
            await kb_service.delete_kb(kb_name, tenant_id, user_id)
        except Exception:
            logger.exception("Failed to clean up KB %s after insert_one failure", kb_name)
        raise
    doc["_id"] = result.inserted_id
    logger.info("Created presentation '%s' slug=%s kb=%s content_type=%s", data.title, slug, kb_name, content_type)
    return _doc_to_response(doc, base_url)


async def update(
    presentation_id: str, tenant_id: str, data: PresentationUpdate, base_url: str = ""
) -> PresentationResponse:
    coll = get_db()[COLLECTION]
    doc = await coll.find_one({"_id": ObjectId(presentation_id), "tenant_id": tenant_id})
    if not doc:
        raise ValueError("Presentation not found")

    updates: dict = {}
    if data.title is not None:
        updates["title"] = data.title
    if data.description is not None:
        updates["description"] = data.description
    if data.tags is not None:
        updates["tags"] = data.tags
    if data.chat_enabled is not None:
        updates["chat_enabled"] = data.chat_enabled

    # Content type switch
    if data.content_type is not None:
        updates["content_type"] = data.content_type

    current_content_type = updates.get("content_type", doc.get("content_type", "markdown"))
    previous_content_type = doc.get("content_type", "markdown")

    # In book mode, chapters[] own the content and the KB. The
    # top-level markdown_content / html_content are unused on the read
    # path, so a stray placeholder coming from the edit form must not
    # be persisted or re-indexed — that would wipe out the per-chapter
    # KB entries created by bulk_import / chapter_service.
    is_book = doc.get("layout") == "book"

    # Validate content type switch has matching content (skip in book mode)
    if not is_book and current_content_type != previous_content_type:
        if current_content_type == "html" and (data.html_content is None or not data.html_content.strip()):
            raise ValueError("HTML content is required when switching to HTML content type")
        if current_content_type == "markdown" and (data.markdown_content is None or not data.markdown_content.strip()):
            raise ValueError("Markdown content is required when switching to Markdown content type")

    # Determine if content changed and needs KB re-index
    content_changed = False
    kb_text = None

    if is_book:
        # Ignore content fields entirely in book mode.
        if data.markdown_content is not None or data.html_content is not None:
            logger.info(
                "Ignoring top-level content update for book-layout presentation %s "
                "(chapters own the KB)",
                presentation_id,
            )
    elif current_content_type == "html" and data.html_content is not None:
        if not data.html_content.strip():
            raise ValueError("HTML content cannot be empty")
        if data.html_content != doc.get("html_content"):
            content_changed = True
            updates["html_content"] = data.html_content
            kb_text = html_to_markdown(data.html_content)
            if not kb_text.strip():
                logger.warning("html_to_markdown returned empty on update for %s, using stripped text fallback", presentation_id)
                kb_text = _fallback_strip_tags(data.html_content)
            updates["markdown_content"] = kb_text  # keep MD in sync for KB
    elif current_content_type == "markdown" and data.markdown_content is not None:
        if not data.markdown_content.strip():
            raise ValueError("Markdown content cannot be empty")
        if data.markdown_content != doc.get("markdown_content"):
            content_changed = True
            updates["markdown_content"] = data.markdown_content
            updates["html_content"] = None  # clear HTML when switching to MD
            kb_text = data.markdown_content

    if content_changed and kb_text is not None:
        # Re-index KB with clean text
        kb_name = doc["kb_name"]
        kb_user_id = doc.get("userid", doc.get("user_id", ""))
        try:
            await kb_service.truncate_kb(kb_name, tenant_id, kb_user_id)
        except Exception:
            logger.warning(f"Truncate failed for {kb_name}, falling back to delete+create")
            try:
                await kb_service.delete_kb(kb_name, tenant_id, kb_user_id)
                await kb_service.create_kb(kb_name, tenant_id, kb_user_id)
            except Exception:
                logger.exception(f"Fallback delete+create failed for {kb_name}")
                raise
        try:
            await kb_service.index_text(
                kb_name, kb_text, tenant_id, kb_user_id,
                display_file_name=data.title or doc["title"],
            )
        except Exception:
            logger.exception(f"KB index_text failed for {kb_name}")
            raise

    # Access mode (preferred, drives derived flags below)
    if data.access_mode is not None:
        updates["access_mode"] = data.access_mode
        if data.access_mode == "access_code":
            updates["access_protected"] = True
            # Only auto-generate codes if none exist and caller didn't provide
            if not doc.get("access_codes") and data.access_codes is None:
                updates["access_codes"] = _generate_access_codes(3)
        else:
            updates["access_protected"] = False
            if data.access_codes is None:
                updates["access_codes"] = []
        # access_tenant_only is only meaningful for ambivo_session.
        # Clear it when leaving that mode so the DB doesn't carry a
        # stale flag (mirrors how access_codes are cleared above).
        if data.access_mode != "ambivo_session":
            updates["access_tenant_only"] = False

    if data.access_tenant_only is not None:
        # Caller may explicitly override (e.g. dashboard sends both
        # access_mode and access_tenant_only in the same PUT).
        updates["access_tenant_only"] = bool(data.access_tenant_only)

    # Access protection — direct code management
    if data.access_codes is not None:
        updates["access_codes"] = _validate_access_codes(data.access_codes)
        # If caller explicitly set codes, treat protected as derived
        updates["access_protected"] = len(updates["access_codes"]) > 0
        # Codes implies access_code mode unless caller explicitly set another
        if "access_mode" not in updates and updates["access_protected"]:
            updates["access_mode"] = "access_code"

    # Access protection toggle (only auto-generate when codes weren't explicitly provided)
    # Skip when access_mode already drove the change above.
    if (
        data.access_protected is not None
        and data.access_codes is None
        and "access_protected" not in updates
    ):
        updates["access_protected"] = data.access_protected
        if data.access_protected and not doc.get("access_codes"):
            updates["access_codes"] = _generate_access_codes(3)
        elif not data.access_protected:
            updates["access_codes"] = []

    if data.regenerate_codes is not None and data.regenerate_codes > 0:
        updates["access_codes"] = _generate_access_codes(data.regenerate_codes)
        updates["access_protected"] = True
        updates["access_mode"] = "access_code"

    # Header
    if data.header is not None:
        existing_header = doc.get("header") or {}
        header_update = data.header.model_dump(exclude_none=True)
        existing_header.update(header_update)
        updates["header"] = existing_header

    # Theme
    if data.theme is not None:
        existing_theme = doc.get("theme") or {}
        theme_data = _resolve_theme(data.theme)
        existing_theme.update(theme_data)
        # Validate custom_css
        css = existing_theme.get("custom_css", "")
        if len(css) > 10240:
            raise ValueError("Custom CSS exceeds 10KB limit")
        updates["theme"] = existing_theme

    if not updates:
        return _doc_to_response(doc, base_url)

    updates["updated_at"] = datetime.now(timezone.utc)
    await coll.update_one({"_id": doc["_id"]}, {"$set": updates})
    doc.update(updates)
    return _doc_to_response(doc, base_url)


async def delete(presentation_id: str, tenant_id: str) -> bool:
    coll = get_db()[COLLECTION]
    doc = await coll.find_one({"_id": ObjectId(presentation_id), "tenant_id": tenant_id})
    if not doc:
        raise ValueError("Presentation not found")

    try:
        await kb_service.delete_kb(doc["kb_name"], tenant_id, doc.get("userid", doc.get("user_id", "")))
    except Exception:
        logger.exception(f"KB deletion failed for {doc['kb_name']}")

    await coll.delete_one({"_id": doc["_id"]})

    # Clean up chat queries
    try:
        result = await get_db()[CHAT_QUERIES_COLLECTION].delete_many({"presentation_id": presentation_id})
        if result.deleted_count:
            logger.info(f"Deleted {result.deleted_count} chat queries for presentation {presentation_id}")
    except Exception:
        logger.exception(f"Failed to delete chat queries for presentation {presentation_id}")

    logger.info(f"Deleted presentation {presentation_id} kb={doc['kb_name']}")
    return True


async def toggle_publish(presentation_id: str, tenant_id: str, base_url: str = "") -> PresentationResponse:
    coll = get_db()[COLLECTION]
    doc = await coll.find_one({"_id": ObjectId(presentation_id), "tenant_id": tenant_id})
    if not doc:
        raise ValueError("Presentation not found")

    new_val = not doc.get("is_published", True)
    now = datetime.now(timezone.utc)
    await coll.update_one({"_id": doc["_id"]}, {"$set": {"is_published": new_val, "updated_at": now}})
    doc["is_published"] = new_val
    doc["updated_at"] = now
    return _doc_to_response(doc, base_url)


async def get_by_id(presentation_id: str, tenant_id: str, base_url: str = "") -> PresentationDetail | None:
    coll = get_db()[COLLECTION]
    doc = await coll.find_one({"_id": ObjectId(presentation_id), "tenant_id": tenant_id}, {"header_logo": 0})
    if not doc:
        return None
    stats = await _chat_query_stats([presentation_id])
    return _doc_to_detail(doc, base_url, stats.get(presentation_id))


async def get_by_slug(slug: str) -> dict | None:
    coll = get_db()[COLLECTION]
    return await coll.find_one({"slug": slug})


async def list_by_tenant(tenant_id: str, base_url: str = "") -> list[PresentationResponse]:
    coll = get_db()[COLLECTION]
    docs = await coll.find({"tenant_id": tenant_id}, {"header_logo": 0, "html_content": 0, "markdown_content": 0}).sort("created_at", -1).to_list(500)
    pids = [str(d["_id"]) for d in docs]
    stats = await _chat_query_stats(pids)
    return [_doc_to_response(d, base_url, stats.get(str(d["_id"]))) for d in docs]


MAX_LOGO_SIZE = 1 * 1024 * 1024  # 1 MB
ALLOWED_LOGO_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}


async def upload_logo(
    presentation_id: str, tenant_id: str, file_data: bytes, content_type: str, base_url: str = ""
) -> PresentationResponse:
    if content_type not in ALLOWED_LOGO_TYPES:
        raise ValueError(f"Unsupported image type: {content_type}")
    if len(file_data) > MAX_LOGO_SIZE:
        raise ValueError("Logo must be under 1 MB")

    coll = get_db()[COLLECTION]
    doc = await coll.find_one({"_id": ObjectId(presentation_id), "tenant_id": tenant_id})
    if not doc:
        raise ValueError("Presentation not found")

    import base64
    logo_b64 = base64.b64encode(file_data).decode("ascii")
    now = datetime.now(timezone.utc)
    await coll.update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "header_logo": logo_b64,
            "header_logo_content_type": content_type,
            "has_header_logo": True,
            "updated_at": now,
        }},
    )
    doc["has_header_logo"] = True
    doc["updated_at"] = now
    return _doc_to_response(doc, base_url)


async def delete_logo(presentation_id: str, tenant_id: str, base_url: str = "") -> PresentationResponse:
    coll = get_db()[COLLECTION]
    doc = await coll.find_one({"_id": ObjectId(presentation_id), "tenant_id": tenant_id})
    if not doc:
        raise ValueError("Presentation not found")

    now = datetime.now(timezone.utc)
    await coll.update_one(
        {"_id": doc["_id"]},
        {
            "$unset": {"header_logo": "", "header_logo_content_type": ""},
            "$set": {"has_header_logo": False, "updated_at": now},
        },
    )
    doc["has_header_logo"] = False
    doc["updated_at"] = now
    return _doc_to_response(doc, base_url)


async def list_chat_queries(
    presentation_id: str, tenant_id: str, page: int = 1, page_size: int = 25,
) -> dict:
    """Return paginated chat queries for a presentation."""
    coll_p = get_db()[COLLECTION]
    doc = await coll_p.find_one(
        {"_id": ObjectId(presentation_id), "tenant_id": tenant_id}, {"_id": 1}
    )
    if not doc:
        raise ValueError("Presentation not found")

    coll = get_db()[CHAT_QUERIES_COLLECTION]
    filt = {"presentation_id": presentation_id}
    total = await coll.count_documents(filt)
    skip = (page - 1) * page_size
    cursor = coll.find(filt).sort("created_at", -1).skip(skip).limit(page_size)
    items = []
    async for q in cursor:
        items.append({
            "id": str(q["_id"]),
            "question": q.get("question", ""),
            "client_ip": q.get("client_ip", ""),
            "access_code": q.get("access_code"),
            "session_id": q.get("session_id", ""),
            "date": q.get("date", ""),
            "created_at": (q["created_at"].isoformat() + "Z") if isinstance(q.get("created_at"), datetime) else str(q.get("created_at", "")),
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def delete_chat_query(query_id: str, presentation_id: str, tenant_id: str) -> bool:
    """Delete a single chat query (after verifying ownership)."""
    coll_p = get_db()[COLLECTION]
    doc = await coll_p.find_one(
        {"_id": ObjectId(presentation_id), "tenant_id": tenant_id}, {"_id": 1}
    )
    if not doc:
        raise ValueError("Presentation not found")
    coll = get_db()[CHAT_QUERIES_COLLECTION]
    result = await coll.delete_one({"_id": ObjectId(query_id), "presentation_id": presentation_id})
    return result.deleted_count > 0


async def delete_all_chat_queries(presentation_id: str, tenant_id: str) -> int:
    """Delete all chat queries for a presentation."""
    coll_p = get_db()[COLLECTION]
    doc = await coll_p.find_one(
        {"_id": ObjectId(presentation_id), "tenant_id": tenant_id}, {"_id": 1}
    )
    if not doc:
        raise ValueError("Presentation not found")
    coll = get_db()[CHAT_QUERIES_COLLECTION]
    result = await coll.delete_many({"presentation_id": presentation_id})
    return result.deleted_count


async def get_logo(slug: str) -> tuple[bytes, str] | None:
    """Return (binary_data, content_type) or None."""
    coll = get_db()[COLLECTION]
    doc = await coll.find_one({"slug": slug}, {"header_logo": 1, "header_logo_content_type": 1})
    if not doc or not doc.get("header_logo"):
        return None
    import base64
    return base64.b64decode(doc["header_logo"]), doc.get("header_logo_content_type", "image/png")
