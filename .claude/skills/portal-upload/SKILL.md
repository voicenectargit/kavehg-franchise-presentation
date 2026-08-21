---
name: portal-upload
description: Update an existing Ambivo Content Portal presentation from a file, folder, or subfolder, then reindex its chat knowledge base. Use when the user wants to upload/sync/refresh docs into an EXISTING presentation identified by its URL (…/p/<slug>) or id, and has (or can provide) a JWT. Handles slug→id resolution, bulk chapter import (update-in-place by filename slug, append new), reindex, and verification.
---

# portal-upload

Sync a local file or folder into an **existing** Content Portal presentation and
reindex its chat KB. Wraps `upload.py` (stdlib-only) in this skill directory,
which calls the same API the portal UI uses.

## Inputs (collect these before running)

1. **Path** — a single file, a folder, or a subfolder to upload. Folders are
   walked recursively; files are natural-sorted (so `UG-002` precedes `UG-010`).
   Supported extensions: `md, txt, html, htm, pdf, docx, pptx` (converted
   server-side via markitdown). Anything else is ignored.
2. **Target** — the presentation's URL (`https://content.ambivo.com/p/<slug>`
   or a `/dashboard/edit/<id>` URL) or a raw 24-hex presentation id.
3. **JWT** — a Bearer token for the tenant that OWNS the presentation. Prefer a
   token **file** over passing the token inline so it never lands in shell
   history. If the user has not provided one, ask for it — do not invent it.

## How it behaves

- Chapters are matched by slug derived from the filename stem:
  existing slug → **content updated in place** (chapter_id/slug/order kept);
  new slug → **appended** after the current last chapter.
  So re-running is safe and idempotent — it refreshes, it does not duplicate.
- Reindex runs by default (background job on the server). Pass `--no-reindex`
  to skip, or `--force-reindex` to delete+recreate the KB collection (use when
  chat reports an embedding-dimension mismatch — a stale vector size).
- The URL's tenant must match the JWT's tenant, or slug resolution fails and
  the script prints the slugs that ARE available for that token.

## Run it

```bash
# Folder → presentation, with token in a file (recommended)
python3 .claude/skills/portal-upload/upload.py <PATH> \
    --url <PRESENTATION_URL_OR_ID> \
    --token-file <TOKEN_FILE>

# Single file, inline token, force a clean KB rebuild
python3 .claude/skills/portal-upload/upload.py ./one-file.md \
    --url https://content.ambivo.com/p/ambivo-user-guide \
    --token "$JWT" --force-reindex

# Preview which files would be sent (no token needed, no writes)
python3 .claude/skills/portal-upload/upload.py <PATH> --url <URL> --dry-run
```

Useful flags: `--dry-run`, `--no-reindex`, `--force-reindex`,
`--ext md,pdf` (restrict types), `--base-url` (only when `--url` is a bare id).

## Steps for the assistant

1. Confirm the three inputs. If the JWT is missing, ask for it (or a token
   file path). Never fabricate a token.
2. Run a `--dry-run` first and show the file list so the user can confirm the
   right files and order before any writes.
3. Run for real. Report the `created/updated/failed` counts, the final chapter
   count, and the KB line (`status/points/vector_size`).
4. If any file is in `failed`, surface the filename + error (usually an
   unsupported format or an oversized file).
5. The reindex is a background job; if the KB check reports "could not confirm
   yet", that is not a failure — tell the user it will finish shortly and can
   be re-checked via the `kb-info` endpoint.

## Notes

- This skill **updates an existing** presentation. It does not create a new one
  and does not delete anything.
- kb-info can be slow (VectorDB); the script retries briefly and never fails the
  run on a KB-check timeout — the import itself has already succeeded by then.
