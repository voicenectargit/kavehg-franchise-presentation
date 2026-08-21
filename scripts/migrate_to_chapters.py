"""
One-shot migration: backfill chapters[] on existing presentations.

For every presentation lacking a `chapters` field, create a single chapter
from the existing top-level `markdown_content` / `html_content` and set
`layout="single"`. Existing top-level content fields are left in place so
the current read path keeps working untouched — a later slice will move
the read path onto chapters[].

Idempotent: re-running this script skips presentations that already have
a chapters array.

Usage:
    python -m scripts.migrate_to_chapters [--dry-run]
"""
import argparse
import asyncio
import hashlib
import sys
from datetime import datetime, timezone

from bson import ObjectId

from app.db import connect_db, close_db, get_db
from app.services.md_renderer import render_markdown


def _slugify(value: str) -> str:
    out = []
    for ch in value.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_":
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "chapter"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_chapter_from_doc(doc: dict) -> dict:
    content_type = doc.get("content_type", "markdown")
    markdown_content = doc.get("markdown_content", "") or ""
    html_content = doc.get("html_content")

    if content_type == "html":
        if not html_content:
            html_content = render_markdown(markdown_content)
        hash_source = html_content
    else:
        if not html_content:
            html_content = render_markdown(markdown_content)
        hash_source = markdown_content

    title = doc.get("title", "Chapter 1")
    return {
        "chapter_id": ObjectId(),
        "order": 0,
        "title": title,
        "slug": _slugify(title),
        "section": None,
        "content_type": content_type,
        "markdown_content": markdown_content,
        "html_content": html_content,
        "indexed_at": None,
        "content_hash": _content_hash(hash_source),
    }


async def migrate(dry_run: bool) -> None:
    await connect_db()
    try:
        coll = get_db()["content_presentations"]
        cursor = coll.find({"chapters": {"$exists": False}})
        scanned = 0
        updated = 0
        skipped = 0

        async for doc in cursor:
            scanned += 1
            if doc.get("chapters"):
                skipped += 1
                continue

            chapter = _build_chapter_from_doc(doc)
            update_doc = {
                "$set": {
                    "chapters": [chapter],
                    "layout": "single",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            }

            # Promote access_protected -> access_mode if missing
            if "access_mode" not in doc:
                if doc.get("access_protected"):
                    update_doc["$set"]["access_mode"] = "access_code"
                else:
                    update_doc["$set"]["access_mode"] = "public"

            print(
                f"  presentation {doc['_id']} slug={doc.get('slug')!r} "
                f"-> 1 chapter, layout=single, "
                f"access_mode={update_doc['$set'].get('access_mode', '(unchanged)')}"
            )

            if not dry_run:
                await coll.update_one({"_id": doc["_id"]}, update_doc)
            updated += 1

        verb = "Would migrate" if dry_run else "Migrated"
        print(
            f"\nScanned {scanned}; {verb} {updated}; skipped {skipped} "
            f"(already had chapters)."
        )
    finally:
        await close_db()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended changes without writing to MongoDB.",
    )
    args = parser.parse_args()
    asyncio.run(migrate(dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
