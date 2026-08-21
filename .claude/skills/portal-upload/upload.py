#!/usr/bin/env python3
"""
portal-upload — update an existing Content Portal presentation from a file or
folder, then reindex its chat KB.

Given a target presentation URL (or id), a JWT, and a path (a single file, a
folder, or a subfolder), this:
  1. resolves the presentation (slug from /p/<slug>, or a raw 24-hex id),
  2. bulk-imports every supported file as chapters — existing chapters with a
     matching slug (filename stem) are updated in place, new ones are appended,
  3. triggers a KB reindex,
  4. verifies the chapter count and (best-effort) the KB vector state.

Stdlib only — no pip installs. Talks to the same endpoints the portal UI uses:
  GET  /api/presentations                              (resolve slug -> id)
  POST /api/presentations/{id}/chapters/bulk-import    (multipart, field "files")
  POST /api/presentations/{id}/reindex[?force=true]
  GET  /api/presentations/{id}/chapters                (verify)
  GET  /api/presentations/{id}/kb-info                 (verify, may be slow)

Examples:
  ./upload.py ./docs/user-guide \
      --url https://content.ambivo.com/p/ambivo-user-guide \
      --token-file token.txt

  ./upload.py ./one-file.md \
      --url https://content.ambivo.com/p/ambivo-user-guide \
      --token "$JWT" --force-reindex
"""
import argparse
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Server-side converter (markitdown) accepts these; anything else is skipped.
DEFAULT_EXTS = ["md", "txt", "html", "htm", "pdf", "docx", "pptx"]
# Keep each bulk-import request comfortably under proxy/body limits.
MAX_BATCH_BYTES = 20 * 1024 * 1024


def eprint(*a):
    print(*a, file=sys.stderr)


def die(msg, code=1):
    eprint(f"error: {msg}")
    sys.exit(code)


def natural_key(s: str):
    # "UG-002" < "UG-010": split digit runs so numeric chunks compare as ints.
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def parse_target(url_or_id: str):
    """Return (base_url, slug, presentation_id). Exactly one of slug/id is set."""
    s = url_or_id.strip()
    if re.fullmatch(r"[0-9a-fA-F]{24}", s):
        return (None, None, s)  # raw id; base_url must come from --base-url
    m = re.match(r"(https?://[^/]+)", s)
    base = m.group(1) if m else None
    # /p/<slug>[/c/<chapter>]  or  /dashboard/edit/<id>
    mid = re.search(r"/dashboard/edit/([0-9a-fA-F]{24})", s)
    if mid:
        return (base, None, mid.group(1))
    mslug = re.search(r"/p/([^/?#]+)", s)
    if mslug:
        return (base, mslug.group(1), None)
    die(f"could not parse a /p/<slug> or /dashboard/edit/<id> from URL: {s}")


def api(base, token, method, path, *, expect_json=True, timeout=60):
    req = urllib.request.Request(
        base + path, method=method,
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if expect_json and body else None)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {detail}") from None


def resolve_id(base, token, slug, pid):
    if pid:
        return pid
    _, items = api(base, token, "GET", "/api/presentations")
    matches = [p for p in (items or []) if p.get("slug") == slug]
    if not matches:
        avail = ", ".join(sorted(p.get("slug", "?") for p in (items or []))) or "(none)"
        die(f"no presentation with slug '{slug}' for this token.\navailable slugs: {avail}")
    return matches[0]["id"]


def collect_files(path: Path, exts):
    allow = {("." + e.lower().lstrip(".")) for e in exts}
    if path.is_file():
        return [path]
    if not path.is_dir():
        die(f"path not found: {path}")
    files = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in allow]
    files.sort(key=lambda p: natural_key(str(p.relative_to(path))))
    return files


def encode_multipart(files, root: Path):
    """Build a multipart/form-data body. Field name is 'files' (repeated).

    The filename sent is the path RELATIVE to the upload root, so the server's
    slug/section derivation (which splits on subfolders) works as intended.
    """
    boundary = "----portalupload" + str(len(files)) + "b0undary7k2"
    crlf = b"\r\n"
    buf = bytearray()
    for f in files:
        if root.is_dir():
            name = str(f.relative_to(root))
        else:
            name = f.name
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        buf += b"--" + boundary.encode() + crlf
        buf += (
            f'Content-Disposition: form-data; name="files"; filename="{name}"'
        ).encode() + crlf
        buf += ("Content-Type: " + ctype).encode() + crlf + crlf
        buf += f.read_bytes() + crlf
    buf += b"--" + boundary.encode() + b"--" + crlf
    return bytes(buf), boundary


def batch_by_size(files, root: Path, max_bytes):
    batches, cur, cur_bytes = [], [], 0
    for f in files:
        sz = f.stat().st_size
        if cur and cur_bytes + sz > max_bytes:
            batches.append(cur)
            cur, cur_bytes = [], 0
        cur.append(f)
        cur_bytes += sz
    if cur:
        batches.append(cur)
    return batches


def bulk_import(base, token, pid, files, root: Path):
    body, boundary = encode_multipart(files, root)
    req = urllib.request.Request(
        f"{base}/api/presentations/{pid}/chapters/bulk-import",
        data=body, method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {e.code} bulk-import: {detail}") from None


def main():
    ap = argparse.ArgumentParser(description="Update a Content Portal presentation from a file/folder and reindex.")
    ap.add_argument("path", help="File, folder, or subfolder to upload")
    ap.add_argument("--url", help="Presentation URL (…/p/<slug> or …/dashboard/edit/<id>) or a 24-hex id")
    ap.add_argument("--base-url", help="Portal origin, e.g. https://content.ambivo.com (required only if --url is a bare id)")
    ap.add_argument("--token", help="JWT (Bearer)")
    ap.add_argument("--token-file", help="File containing the JWT")
    ap.add_argument("--ext", default=",".join(DEFAULT_EXTS), help="Comma-separated extensions to include")
    ap.add_argument("--no-reindex", action="store_true", help="Skip the reindex step")
    ap.add_argument("--force-reindex", action="store_true", help="Reindex with force=true (delete+recreate the KB collection; use for stale embedding dims)")
    ap.add_argument("--dry-run", action="store_true", help="List the files that would be uploaded and exit")
    args = ap.parse_args()

    if not args.url:
        die("--url is required (presentation URL or id)")
    token = args.token
    if args.token_file:
        token = Path(args.token_file).read_text().strip()
    if not token and not args.dry_run:
        die("provide --token or --token-file")

    base, slug, pid = parse_target(args.url)
    if args.base_url:
        base = args.base_url.rstrip("/")
    if not base and not args.dry_run:
        die("could not determine portal origin; pass --base-url")

    root = Path(args.path).expanduser()
    exts = [e.strip() for e in args.ext.split(",") if e.strip()]
    files = collect_files(root, exts)
    if not files:
        die(f"no matching files ({', '.join(exts)}) under {root}")

    print(f"found {len(files)} file(s):")
    for f in files:
        rel = f.relative_to(root) if root.is_dir() else f.name
        print(f"  {rel}  ({f.stat().st_size:,} bytes)")
    if args.dry_run:
        return

    pid = resolve_id(base, token, slug, pid)
    print(f"\ntarget: {base}  presentation_id={pid}" + (f"  slug={slug}" if slug else ""))

    batches = batch_by_size(files, root, MAX_BATCH_BYTES)
    totals = {"created": 0, "updated": 0, "failed": 0}
    failures = []
    for i, batch in enumerate(batches, 1):
        if len(batches) > 1:
            print(f"\nuploading batch {i}/{len(batches)} ({len(batch)} files)…")
        res = bulk_import(base, token, pid, batch, root)
        for k in totals:
            totals[k] += len(res.get(k, []))
        failures += res.get("failed", [])
    print(f"\nbulk-import: created={totals['created']} updated={totals['updated']} failed={totals['failed']}")
    for fdict in failures:
        print(f"  FAILED {fdict.get('filename')}: {fdict.get('error')}")

    if not args.no_reindex:
        q = "?force=true" if args.force_reindex else ""
        st, res = api(base, token, "POST", f"/api/presentations/{pid}/reindex{q}", timeout=60)
        print(f"\nreindex: {res.get('status') if res else st}"
              + (f" (force_recreate={res.get('force_recreate')})" if res else ""))
    else:
        print("\nreindex: skipped (--no-reindex)")

    # Verify chapter count.
    _, chapters = api(base, token, "GET", f"/api/presentations/{pid}/chapters")
    print(f"\nverify: {len(chapters or [])} chapter(s) now on the presentation")

    # Best-effort KB check (VectorDB can be slow; don't fail the run on timeout).
    if not args.no_reindex:
        for attempt in range(2):
            time.sleep(8 if attempt == 0 else 20)
            try:
                _, kb = api(base, token, "GET", f"/api/presentations/{pid}/kb-info", timeout=90)
                resp = (kb or {}).get("vectordb_envelope", {}).get("response", {})
                if isinstance(resp, dict):
                    print(f"kb: status={resp.get('status')} points={resp.get('points_count')} "
                          f"vector_size={kb.get('vector_size')}")
                break
            except Exception as e:
                if attempt == 1:
                    print(f"kb: could not confirm yet ({e}); reindex runs in the background — recheck kb-info later.")

    print("\ndone.")


if __name__ == "__main__":
    main()
