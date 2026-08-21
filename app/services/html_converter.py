"""
HTML → Markdown converter for KB indexing.

When content_type is "html", the raw HTML is stored for rendering
but we convert it to clean Markdown for VectorDB indexing so the KB
doesn't ingest HTML noise (tags, styles, scripts, etc.).
"""
import logging
import re

from markdownify import markdownify

logger = logging.getLogger(__name__)


def html_to_markdown(html: str) -> str:
    """Convert HTML to clean Markdown suitable for KB indexing.

    Returns stripped Markdown text. On conversion failure, falls back
    to a plain-text extraction (regex tag stripping) so the KB always
    gets *something* indexable rather than crashing the request.
    """
    if not html or not html.strip():
        return ""

    try:
        cleaned = _strip_non_content(html)
        md = markdownify(cleaned, heading_style="ATX", strip=["img"])
        # Collapse excessive blank lines
        md = re.sub(r"\n{3,}", "\n\n", md)
        return md.strip()
    except Exception:
        logger.warning("markdownify conversion failed, falling back to tag stripping", exc_info=True)
        return _fallback_strip_tags(html)


def _strip_non_content(html: str) -> str:
    """Remove head/style/script/comments and extract body content."""
    html = re.sub(r"<head[\s\S]*?</head>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<!--[\s\S]*?-->", "", html)

    # Extract body if present
    body_match = re.search(r"<body[^>]*>([\s\S]*)</body>", html, re.IGNORECASE)
    if body_match:
        html = body_match.group(1)

    # Strip remaining structural wrappers
    html = re.sub(r"<!DOCTYPE[^>]*>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"</?html[^>]*>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"</?body[^>]*>", "", html, flags=re.IGNORECASE)

    return html


def _fallback_strip_tags(html: str) -> str:
    """Last-resort: strip all HTML tags and return plain text."""
    text = _strip_non_content(html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
