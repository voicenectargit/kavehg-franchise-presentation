"""Cache-busting version tokens for static assets.

Appended as ?v=<hash> on <script>/<link> tags so browsers fetch a fresh
copy whenever a static file changes (e.g. after a deploy) instead of
serving a stale cached version against newly rendered HTML.
"""
import hashlib
import os

_STATIC_DIR = "app/static"


def asset_version(*rel_paths: str) -> str:
    """Short hash of the given static files' modification times."""
    h = hashlib.md5()
    for rel in rel_paths:
        try:
            h.update(str(os.path.getmtime(os.path.join(_STATIC_DIR, rel))).encode())
        except OSError:
            pass
    return h.hexdigest()[:8]
