"""
Chapter CRUD for multi-chapter "book" presentations.

Chapters are stored as an array on the parent presentation document
under the `chapters` field. Each chapter is identified by an ObjectId
in `chapter_id`.

After mutating content, callers should schedule reindex_presentation()
via FastAPI BackgroundTasks so the KB stays in sync with chapters[].
"""
import asyncio
import hashlib
import logging
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from bson.errors import InvalidId
from slugify import slugify

from app.db import get_db
from app.models.presentation import (
    ChapterCreate,
    ChapterDetail,
    ChapterResponse,
    ChapterUpdate,
)
from app.services import document_converter, kb_service
from app.services.document_converter import (
    ConversionError,
    FileTooLargeError,
    UnsupportedFormatError,
)
from app.services.html_converter import _fallback_strip_tags, html_to_markdown
from app.services.md_renderer import render_markdown

logger = logging.getLogger(__name__)

COLLECTION = "content_presentations"


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _unique_chapter_slug(base: str, existing_slugs: set[str]) -> str:
    """Return a slug not already in existing_slugs."""
    slug = base or "chapter"
    if slug not in existing_slugs:
        return slug
    while True:
        candidate = f"{slug}-{secrets.token_hex(2)}"
        if candidate not in existing_slugs:
            return candidate


def _render_chapter_content(
    content_type: str,
    markdown_content: str | None,
    html_content: str | None,
    *,
    chapter_label: str,
) -> tuple[str, str, str]:
    """Return (content_type, markdown_to_store, html_to_store, hash_source).

    For markdown chapters: render html from markdown, hash the markdown.
    For html chapters: convert html -> markdown for KB, keep html as-is,
    hash the html.

    Raises ValueError if the chapter has no usable content.
    """
    content_type = content_type or "markdown"

    if content_type == "html":
        if not html_content or not html_content.strip():
            raise ValueError(f"Chapter '{chapter_label}': HTML content is required")
        kb_md = html_to_markdown(html_content)
        if not kb_md.strip():
            logger.warning(
                "Chapter '%s': html_to_markdown returned empty, using stripped text fallback",
                chapter_label,
            )
            kb_md = _fallback_strip_tags(html_content)
        return content_type, kb_md, html_content, html_content

    # markdown
    md = markdown_content or ""
    if not md.strip():
        raise ValueError(f"Chapter '{chapter_label}': content cannot be empty")
    rendered = render_markdown(md)
    return "markdown", md, rendered, md


def _build_chapter_doc(
    data: ChapterCreate,
    *,
    order: int,
    existing_slugs: set[str],
) -> dict:
    content_type, md_text, html_text, hash_source = _render_chapter_content(
        data.content_type,
        data.markdown_content,
        data.html_content,
        chapter_label=data.title,
    )
    base_slug = slugify(data.slug or data.title) or "chapter"
    slug = _unique_chapter_slug(base_slug, existing_slugs)

    return {
        "chapter_id": ObjectId(),
        "order": order,
        "title": data.title.strip(),
        "slug": slug,
        "section": data.section,
        "content_type": content_type,
        "markdown_content": md_text,
        "html_content": html_text,
        "content_hash": _content_hash(hash_source),
        "indexed_at": None,
    }


def _chapter_to_response(chapter: dict) -> ChapterResponse:
    return ChapterResponse(
        chapter_id=str(chapter["chapter_id"]),
        order=int(chapter.get("order", 0)),
        title=chapter.get("title", ""),
        slug=chapter.get("slug", ""),
        section=chapter.get("section"),
        content_type=chapter.get("content_type", "markdown"),
        indexed_at=chapter.get("indexed_at"),
    )


def _chapter_to_detail(chapter: dict) -> ChapterDetail:
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


async def _load_presentation(presentation_id: str, tenant_id: str) -> dict:
    """Load a presentation, raising ValueError if absent."""
    try:
        oid = ObjectId(presentation_id)
    except (InvalidId, TypeError) as exc:
        raise ValueError(f"Invalid presentation id: {presentation_id}") from exc

    coll = get_db()[COLLECTION]
    doc = await coll.find_one({"_id": oid, "tenant_id": tenant_id})
    if not doc:
        raise ValueError("Presentation not found")
    return doc


def _find_chapter(doc: dict, chapter_id: str) -> tuple[int, dict]:
    """Return (index, chapter_dict) for chapter_id in doc, or raise ValueError."""
    try:
        target = ObjectId(chapter_id)
    except (InvalidId, TypeError) as exc:
        raise ValueError(f"Invalid chapter id: {chapter_id}") from exc

    for idx, ch in enumerate(doc.get("chapters") or []):
        if ch.get("chapter_id") == target:
            return idx, ch
    raise ValueError("Chapter not found")


async def list_chapters(presentation_id: str, tenant_id: str) -> list[ChapterResponse]:
    doc = await _load_presentation(presentation_id, tenant_id)
    chapters = sorted(doc.get("chapters") or [], key=lambda c: c.get("order", 0))
    return [_chapter_to_response(c) for c in chapters]


async def get_chapter(
    presentation_id: str, chapter_id: str, tenant_id: str
) -> ChapterDetail:
    doc = await _load_presentation(presentation_id, tenant_id)
    _, chapter = _find_chapter(doc, chapter_id)
    return _chapter_to_detail(chapter)


async def add_chapter(
    presentation_id: str, tenant_id: str, data: ChapterCreate
) -> ChapterDetail:
    doc = await _load_presentation(presentation_id, tenant_id)

    existing = list(doc.get("chapters") or [])
    existing_slugs = {c.get("slug") for c in existing if c.get("slug")}

    if data.order is None:
        order = max((c.get("order", 0) for c in existing), default=-1) + 1
    else:
        order = int(data.order)

    chapter = _build_chapter_doc(data, order=order, existing_slugs=existing_slugs)

    now = datetime.now(timezone.utc)
    coll = get_db()[COLLECTION]
    result = await coll.update_one(
        {"_id": doc["_id"], "tenant_id": tenant_id},
        {
            "$push": {"chapters": chapter},
            "$set": {"updated_at": now, "layout": doc.get("layout", "single")},
        },
    )
    if result.modified_count != 1:
        # Race condition or missing doc — re-check
        logger.warning(
            "add_chapter modified_count=%s for presentation=%s",
            result.modified_count,
            presentation_id,
        )
        raise ValueError("Failed to add chapter — presentation may have been deleted")

    logger.info(
        "Added chapter '%s' (slug=%s, order=%s) to presentation %s",
        chapter["title"],
        chapter["slug"],
        order,
        presentation_id,
    )
    return _chapter_to_detail(chapter)


async def update_chapter(
    presentation_id: str,
    chapter_id: str,
    tenant_id: str,
    data: ChapterUpdate,
) -> ChapterDetail:
    doc = await _load_presentation(presentation_id, tenant_id)
    idx, existing = _find_chapter(doc, chapter_id)

    new_chapter = dict(existing)  # copy
    content_changed = False

    if data.title is not None:
        new_chapter["title"] = data.title.strip()

    if data.section is not None:
        new_chapter["section"] = data.section

    if data.order is not None:
        new_chapter["order"] = int(data.order)

    # Slug change must remain unique within presentation
    if data.slug is not None:
        base = slugify(data.slug) or "chapter"
        siblings = {
            c.get("slug")
            for i, c in enumerate(doc.get("chapters") or [])
            if i != idx and c.get("slug")
        }
        new_chapter["slug"] = _unique_chapter_slug(base, siblings)

    # Content / type
    type_changing = data.content_type is not None and data.content_type != existing.get(
        "content_type", "markdown"
    )
    md_changing = (
        data.markdown_content is not None
        and data.markdown_content != existing.get("markdown_content")
    )
    html_changing = (
        data.html_content is not None
        and data.html_content != existing.get("html_content")
    )

    if type_changing or md_changing or html_changing:
        content_type = data.content_type or existing.get("content_type", "markdown")
        md_in = (
            data.markdown_content
            if data.markdown_content is not None
            else existing.get("markdown_content", "")
        )
        html_in = (
            data.html_content
            if data.html_content is not None
            else existing.get("html_content")
        )
        ct, md_out, html_out, hash_source = _render_chapter_content(
            content_type,
            md_in,
            html_in,
            chapter_label=new_chapter["title"],
        )
        new_chapter["content_type"] = ct
        new_chapter["markdown_content"] = md_out
        new_chapter["html_content"] = html_out
        new_chapter["content_hash"] = _content_hash(hash_source)
        new_chapter["indexed_at"] = None
        content_changed = True

    if new_chapter == existing:
        return _chapter_to_detail(existing)

    now = datetime.now(timezone.utc)
    coll = get_db()[COLLECTION]
    result = await coll.update_one(
        {
            "_id": doc["_id"],
            "tenant_id": tenant_id,
            "chapters.chapter_id": existing["chapter_id"],
        },
        {
            "$set": {
                "chapters.$": new_chapter,
                "updated_at": now,
            }
        },
    )
    if result.matched_count != 1:
        logger.warning(
            "update_chapter matched_count=%s for presentation=%s chapter=%s",
            result.matched_count,
            presentation_id,
            chapter_id,
        )
        raise ValueError("Chapter update failed — record may have been modified")

    logger.info(
        "Updated chapter %s (title=%s, content_changed=%s) on presentation %s",
        chapter_id,
        new_chapter["title"],
        content_changed,
        presentation_id,
    )
    return _chapter_to_detail(new_chapter)


async def delete_chapter(
    presentation_id: str, chapter_id: str, tenant_id: str
) -> bool:
    doc = await _load_presentation(presentation_id, tenant_id)
    _, existing = _find_chapter(doc, chapter_id)

    if len(doc.get("chapters") or []) <= 1:
        raise ValueError("Cannot delete the last chapter — a presentation must have at least one chapter")

    now = datetime.now(timezone.utc)
    coll = get_db()[COLLECTION]
    result = await coll.update_one(
        {"_id": doc["_id"], "tenant_id": tenant_id},
        {
            "$pull": {"chapters": {"chapter_id": existing["chapter_id"]}},
            "$set": {"updated_at": now},
        },
    )
    if result.modified_count != 1:
        logger.warning(
            "delete_chapter modified_count=%s for presentation=%s chapter=%s",
            result.modified_count,
            presentation_id,
            chapter_id,
        )
        raise ValueError("Chapter delete failed — record may have been modified")

    logger.info("Deleted chapter %s from presentation %s", chapter_id, presentation_id)
    return True


async def delete_all_chapters(
    presentation_id: str, tenant_id: str, user_id: str
) -> dict:
    """Delete the KB collection first, then remove every chapter.

    Order matters: we drop the VectorDB collection up front so the
    presentation's knowledge base is cleaned out before the chapter
    content that backs it disappears. The KB delete is best-effort — a
    missing or already-deleted collection should not block clearing the
    chapters array, so we log and continue.

    Returns a summary {deleted: <count>, kb_deleted: <bool>}.
    """
    doc = await _load_presentation(presentation_id, tenant_id)
    chapters = list(doc.get("chapters") or [])
    count = len(chapters)

    # 1. Clean the KB first so vectors don't outlive the source content.
    kb_name = doc.get("kb_name")
    kb_deleted = False
    if kb_name:
        async with _kb_lock(kb_name):
            try:
                await kb_service.delete_kb(kb_name, tenant_id, user_id)
                kb_deleted = True
                logger.info(
                    "Deleted KB %s before clearing all chapters of %s",
                    kb_name,
                    presentation_id,
                )
            except Exception:
                logger.exception(
                    "delete_kb failed for %s during delete-all; "
                    "clearing chapters anyway",
                    kb_name,
                )

    if not chapters:
        return {"deleted": 0, "kb_deleted": kb_deleted}

    # 2. Clear the chapters array.
    now = datetime.now(timezone.utc)
    coll = get_db()[COLLECTION]
    result = await coll.update_one(
        {"_id": doc["_id"], "tenant_id": tenant_id},
        {"$set": {"chapters": [], "updated_at": now}},
    )
    if result.matched_count != 1:
        logger.warning(
            "delete_all_chapters matched_count=%s for presentation=%s",
            result.matched_count,
            presentation_id,
        )
        raise ValueError("Delete failed — presentation may have been deleted")

    logger.info(
        "Deleted all %d chapters from presentation %s (kb_deleted=%s)",
        count,
        presentation_id,
        kb_deleted,
    )
    return {"deleted": count, "kb_deleted": kb_deleted}


# ---------------------------------------------------------------------------
# Bulk import
# ---------------------------------------------------------------------------


_NATURAL_KEY_RE = re.compile(r"(\d+)")


def _natural_sort_key(filename: str) -> tuple:
    """Natural sort: UG-001 < UG-002 < UG-010 (not UG-1 < UG-10 < UG-2)."""
    parts = _NATURAL_KEY_RE.split(filename)
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


async def bulk_import_chapters(
    presentation_id: str,
    tenant_id: str,
    files: list[tuple[str, bytes]],
) -> dict:
    """Convert files to markdown and create or update chapters.

    Chapters are matched against existing ones by slug derived from the
    full relative path stem (so "section1/intro.md" and
    "section2/intro.md" don't collide):
      - existing slug match → update content + title and refresh
        content_hash. chapter_id, slug, and order are preserved.
        Section is preserved if the chapter already has one; otherwise
        the section derived from the upload path (if any) is set.
      - no match → append in natural-sort filename order after the
        current last chapter. Section is set from the first real
        subfolder name when the path has 3+ parts.

    Files are converted in input order but persisted in natural-sort
    filename order, so e.g. UG-001 lands before UG-002 regardless of
    upload order.

    Returns a summary {created: [...], updated: [...], failed: [...]}.
    The reindex is NOT triggered here — the calling route should
    schedule a single background reindex covering the full set.
    """
    doc = await _load_presentation(presentation_id, tenant_id)

    summary: dict = {"created": [], "updated": [], "failed": []}

    # Order files by natural-sort filename so chapter order is sensible.
    files_sorted = sorted(files, key=lambda f: _natural_sort_key(f[0] or ""))

    # 1. Convert everything first. Per-file failures don't poison the batch.
    converted: list[tuple[str, document_converter.ConvertedDocument]] = []
    for filename, data in files_sorted:
        try:
            doc_md = await document_converter.convert_file(data, filename)
            converted.append((filename, doc_md))
        except (UnsupportedFormatError, FileTooLargeError, ConversionError) as exc:
            logger.warning("Bulk import skipped %s: %s", filename, exc)
            summary["failed"].append({"filename": filename, "error": str(exc)})

    if not converted:
        logger.info(
            "Bulk import on %s produced no convertible files (failed=%d)",
            presentation_id,
            len(summary["failed"]),
        )
        return summary

    # 2. Plan updates vs. inserts. Re-read existing chapters for a fresh
    #    view (the convert loop above is async and may have yielded).
    existing = list(doc.get("chapters") or [])
    existing_by_slug: dict[str, dict] = {
        c.get("slug"): c for c in existing if c.get("slug")
    }
    used_slugs: set[str] = set(existing_by_slug.keys())
    next_order = max((c.get("order", 0) for c in existing), default=-1) + 1

    update_ops: list[tuple[ObjectId, dict]] = []
    new_chapters: list[dict] = []

    for filename, conv in converted:
        # Slug uses the full path minus extension so that
        # "section1/intro.md" and "section2/intro.md" don't collide.
        path_obj = Path(filename)
        path_no_ext = str(path_obj.with_suffix("")) if path_obj.suffix else filename
        base_slug = slugify(path_no_ext) or "chapter"

        # When uploads include real subfolders (e.g. via
        # webkitdirectory with section dirs), derive a section label
        # from the first subfolder inside the picked root. A single
        # picked folder with files directly inside has only 2 path
        # parts and produces no section so we don't bloat the list
        # with a label that matches the book itself.
        section_label: str | None = None
        parts = path_obj.parts
        if len(parts) >= 3:
            section_label = re.sub(
                r"[-_]+", " ", parts[1]
            ).strip().title() or None

        try:
            content_type, md_out, html_out, hash_source = _render_chapter_content(
                "markdown",
                conv.markdown,
                None,
                chapter_label=conv.suggested_title,
            )
        except ValueError as exc:
            summary["failed"].append({"filename": filename, "error": str(exc)})
            continue

        if base_slug in existing_by_slug:
            ex = existing_by_slug[base_slug]
            updated = dict(ex)
            updated.update(
                {
                    "title": conv.suggested_title,
                    "content_type": content_type,
                    "markdown_content": md_out,
                    "html_content": html_out,
                    "content_hash": _content_hash(hash_source),
                    "indexed_at": None,
                }
            )
            # Only overwrite section if we have a derived one and the
            # existing chapter doesn't already have a manual section.
            if section_label and not ex.get("section"):
                updated["section"] = section_label
            update_ops.append((ex["chapter_id"], updated))
            summary["updated"].append({"filename": filename, "slug": base_slug})
        else:
            slug = _unique_chapter_slug(base_slug, used_slugs)
            used_slugs.add(slug)
            new_chapters.append(
                {
                    "chapter_id": ObjectId(),
                    "order": next_order,
                    "title": conv.suggested_title,
                    "slug": slug,
                    "section": section_label,
                    "content_type": content_type,
                    "markdown_content": md_out,
                    "html_content": html_out,
                    "content_hash": _content_hash(hash_source),
                    "indexed_at": None,
                }
            )
            summary["created"].append({"filename": filename, "slug": slug})
            next_order += 1

    # 3. Persist.
    now = datetime.now(timezone.utc)
    coll = get_db()[COLLECTION]

    for chapter_id, ch in update_ops:
        await coll.update_one(
            {
                "_id": doc["_id"],
                "tenant_id": tenant_id,
                "chapters.chapter_id": chapter_id,
            },
            {"$set": {"chapters.$": ch, "updated_at": now}},
        )

    if new_chapters:
        await coll.update_one(
            {"_id": doc["_id"], "tenant_id": tenant_id},
            {
                "$push": {"chapters": {"$each": new_chapters}},
                "$set": {"updated_at": now, "layout": "book"},
            },
        )

    logger.info(
        "Bulk import on %s: created=%d updated=%d failed=%d",
        presentation_id,
        len(summary["created"]),
        len(summary["updated"]),
        len(summary["failed"]),
    )
    return summary


# ---------------------------------------------------------------------------
# KB re-indexing
# ---------------------------------------------------------------------------

_kb_locks: dict[str, asyncio.Lock] = {}


def _kb_lock(kb_name: str) -> asyncio.Lock:
    lock = _kb_locks.get(kb_name)
    if lock is None:
        lock = asyncio.Lock()
        _kb_locks[kb_name] = lock
    return lock


async def reindex_presentation(
    presentation_id: str, tenant_id: str, user_id: str, *, force_recreate: bool = False
) -> dict:
    """Truncate the KB and re-index every chapter as a separate document.

    Each chapter is indexed under its title as `display_file_name`, so
    VectorDB answers can carry per-chapter source attribution.

    Designed to be called from FastAPI BackgroundTasks. Per-KB lock
    serializes concurrent reindexes within a single process. Returns a
    summary dict; never raises (errors are logged so background callers
    don't crash the request lifecycle).

    When `force_recreate=True`, the KB collection is deleted and
    recreated from scratch instead of truncated — necessary when the
    collection's embedding dimensions are stale (e.g. VectorDB switched
    embedding models since the collection was first created).
    """
    summary = {"presentation_id": presentation_id, "indexed": 0, "skipped": 0, "failed": 0, "force_recreate": force_recreate}

    try:
        doc = await _load_presentation(presentation_id, tenant_id)
    except ValueError as exc:
        logger.warning("reindex aborted: %s", exc)
        return summary

    kb_name = doc.get("kb_name")
    if not kb_name:
        logger.warning(
            "reindex aborted: presentation %s has no kb_name", presentation_id
        )
        return summary

    chapters = sorted(doc.get("chapters") or [], key=lambda c: c.get("order", 0))
    if not chapters:
        logger.warning(
            "reindex aborted: presentation %s has no chapters", presentation_id
        )
        return summary

    async with _kb_lock(kb_name):
        # 1. Reset the collection.
        #
        # Truncate only clears vectors and keeps the collection's
        # schema (including embedding dimension). When the embedding
        # model has changed since the collection was created, truncate
        # is not enough — even a subsequent delete+create can be wrong
        # because VectorDB's create_kb currently hardcodes vector_size
        # to 1536 (qdrant_service.py:858) while the indexer now uses
        # voyage-3-large (1024-dim). So in force_recreate mode we
        # delete the collection and SKIP the create — letting
        # VectorDB's index_text path auto-create the collection on
        # first insert using the embed model's true dimensions.
        if force_recreate:
            try:
                await kb_service.delete_kb(kb_name, tenant_id, user_id)
                logger.info(
                    "Force-recreate: deleted KB %s; first index_text "
                    "will auto-create with current embedder's dim",
                    kb_name,
                )
            except Exception:
                logger.exception(
                    "delete_kb failed for %s during force-recreate; "
                    "attempting indexing anyway",
                    kb_name,
                )
        else:
            try:
                await kb_service.truncate_kb(kb_name, tenant_id, user_id)
            except Exception:
                logger.warning(
                    "truncate_kb failed for %s — falling back to delete (no create)",
                    kb_name,
                )
                try:
                    await kb_service.delete_kb(kb_name, tenant_id, user_id)
                except Exception:
                    logger.exception(
                        "delete_kb fallback failed for %s; continuing", kb_name
                    )

        # 2. Index each chapter
        now_iso = datetime.now(timezone.utc).isoformat()
        successful_ids: list[ObjectId] = []
        for ch in chapters:
            text = ch.get("markdown_content") or ""
            if not text.strip():
                logger.warning(
                    "Skipping empty chapter '%s' on reindex of %s",
                    ch.get("title"),
                    kb_name,
                )
                summary["skipped"] += 1
                continue
            display_name = ch.get("title") or "Chapter"
            try:
                await kb_service.index_text(
                    kb_name=kb_name,
                    text=text,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    display_file_name=display_name,
                )
                successful_ids.append(ch["chapter_id"])
                summary["indexed"] += 1
            except Exception:
                logger.exception(
                    "index_text failed for chapter '%s' in %s",
                    display_name,
                    kb_name,
                )
                summary["failed"] += 1

        # 3. Mark indexed_at on successful chapters
        if successful_ids:
            coll = get_db()[COLLECTION]
            try:
                await coll.update_one(
                    {"_id": doc["_id"]},
                    {
                        "$set": {
                            "chapters.$[ch].indexed_at": now_iso,
                        }
                    },
                    array_filters=[{"ch.chapter_id": {"$in": successful_ids}}],
                )
            except Exception:
                logger.exception(
                    "Failed to persist indexed_at for presentation %s", presentation_id
                )

    log_fn = logger.error if summary["failed"] else logger.info
    log_fn(
        "Reindex of kb=%s presentation=%s: indexed=%d skipped=%d failed=%d",
        kb_name,
        presentation_id,
        summary["indexed"],
        summary["skipped"],
        summary["failed"],
    )
    return summary


async def reorder_chapters(
    presentation_id: str, tenant_id: str, ordered_chapter_ids: list[str]
) -> list[ChapterResponse]:
    doc = await _load_presentation(presentation_id, tenant_id)
    chapters = list(doc.get("chapters") or [])

    if len(ordered_chapter_ids) != len(chapters):
        raise ValueError(
            f"Reorder requires all {len(chapters)} chapter ids; "
            f"got {len(ordered_chapter_ids)}"
        )

    try:
        ordered_oids = [ObjectId(cid) for cid in ordered_chapter_ids]
    except (InvalidId, TypeError) as exc:
        raise ValueError(f"Invalid chapter id in reorder list: {exc}") from exc

    existing_ids = {c["chapter_id"] for c in chapters}
    if set(ordered_oids) != existing_ids:
        raise ValueError("Reorder list must contain exactly the existing chapter ids")

    by_id = {c["chapter_id"]: c for c in chapters}
    new_chapters = []
    for new_order, oid in enumerate(ordered_oids):
        ch = dict(by_id[oid])
        ch["order"] = new_order
        new_chapters.append(ch)

    now = datetime.now(timezone.utc)
    coll = get_db()[COLLECTION]
    await coll.update_one(
        {"_id": doc["_id"], "tenant_id": tenant_id},
        {"$set": {"chapters": new_chapters, "updated_at": now}},
    )
    logger.info(
        "Reordered %d chapters on presentation %s", len(new_chapters), presentation_id
    )
    return [_chapter_to_response(c) for c in new_chapters]
