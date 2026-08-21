"""
Document → markdown conversion using Microsoft's markitdown.

Used by chapter upload routes to accept PDF / DOCX / PPTX / HTML files
and produce markdown that flows through the existing chapter pipeline.
Pure markdown / text input is normalized (UTF-8 decode + frontmatter
strip) without going through markitdown to avoid round-tripping.

Conversion runs in a threadpool because markitdown is synchronous and
can take several seconds for large PDFs.
"""
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

# 25 MB cap. Large enough for a normal PDF book chapter; small enough
# that conversion won't tie up the worker for minutes.
MAX_FILE_BYTES = 25 * 1024 * 1024

# Extensions we accept and route to a converter. Lowercased, no dot.
SUPPORTED_EXTENSIONS = {
    "md", "markdown", "txt",
    "html", "htm",
    "pdf",
    "docx",
    "pptx",
    "xlsx",
}

# Extensions we pass through as text (no markitdown invocation).
_PASSTHROUGH_EXTENSIONS = {"md", "markdown", "txt"}

# Frontmatter detector: --- ... --- at the very start of the file.
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)

# H1 detector: # Title  (ATX-style only — common in our content)
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


class UnsupportedFormatError(ValueError):
    """Raised when the uploaded file extension is not supported."""


class FileTooLargeError(ValueError):
    """Raised when the uploaded file exceeds MAX_FILE_BYTES."""


class ConversionError(ValueError):
    """Raised when markitdown fails to convert the file."""


@dataclass
class ConvertedDocument:
    markdown: str
    suggested_title: str
    source_extension: str  # e.g. "pdf"


def _extension_of(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".")


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def _extract_title(markdown: str, fallback_filename: str) -> str:
    match = _H1_RE.search(markdown)
    if match:
        return match.group(1).strip()
    stem = Path(fallback_filename).stem
    # Replace hyphens/underscores with spaces and title-case
    return re.sub(r"[-_]+", " ", stem).strip().title() or "Chapter"


def _convert_with_markitdown(path: str) -> str:
    # Imported lazily so the module is importable even when markitdown
    # isn't installed (e.g. during local dev when the heavy deps aren't
    # in the active venv).
    from markitdown import MarkItDown

    md = MarkItDown()
    result = md.convert(path)
    return result.text_content or ""


async def convert_file(file_bytes: bytes, filename: str) -> ConvertedDocument:
    """Convert an uploaded file's bytes to markdown.

    - Validates extension is in SUPPORTED_EXTENSIONS.
    - Validates size is within MAX_FILE_BYTES.
    - For .md / .markdown / .txt: passes through (UTF-8 decode +
      frontmatter strip).
    - For all other supported types: runs markitdown in a threadpool.

    Returns a ConvertedDocument with markdown text and a suggested
    title (extracted H1 or prettified filename stem).
    """
    if not filename:
        raise UnsupportedFormatError("Uploaded file is missing a filename")

    ext = _extension_of(filename)
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"Unsupported file type '.{ext}'. Supported: "
            + ", ".join(sorted(f".{e}" for e in SUPPORTED_EXTENSIONS))
        )

    if not file_bytes:
        raise ConversionError("Uploaded file is empty")

    if len(file_bytes) > MAX_FILE_BYTES:
        raise FileTooLargeError(
            f"File '{filename}' is {len(file_bytes)} bytes; "
            f"max is {MAX_FILE_BYTES} bytes"
        )

    if ext in _PASSTHROUGH_EXTENSIONS:
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = file_bytes.decode("utf-8", errors="replace")
                logger.warning(
                    "Passthrough decode of %s used utf-8 errors=replace fallback",
                    filename,
                )
            except Exception as exc:
                raise ConversionError(
                    f"Could not decode '{filename}' as UTF-8: {exc}"
                ) from exc
        markdown = _strip_frontmatter(text)
        title = _extract_title(markdown, filename)
        return ConvertedDocument(
            markdown=markdown, suggested_title=title, source_extension=ext
        )

    # markitdown path — write to tempfile, convert, clean up.
    # markitdown's API expects a path; passing the suffix preserves
    # extension-based detection.
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=f".{ext}", delete=False
        ) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        try:
            markdown = await run_in_threadpool(_convert_with_markitdown, tmp_path)
        except Exception as exc:
            logger.exception("markitdown conversion failed for %s", filename)
            raise ConversionError(
                f"Could not convert '{filename}': {exc}"
            ) from exc

        if not markdown or not markdown.strip():
            raise ConversionError(
                f"Conversion of '{filename}' produced empty content. "
                "Scanned PDFs require OCR which is not configured."
            )

        title = _extract_title(markdown, filename)
        logger.info(
            "Converted %s (%d bytes) → markdown (%d chars), title=%r",
            filename,
            len(file_bytes),
            len(markdown),
            title,
        )
        return ConvertedDocument(
            markdown=markdown, suggested_title=title, source_extension=ext
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                logger.warning("Failed to clean up temp file %s", tmp_path)
