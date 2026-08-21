# Ambivo Content Portal

Markdown content hosting microservice with per-page knowledge base chat. Submit Markdown via API, get a clean hosted web page with a floating AI chat widget that answers visitor questions about the content using streaming RAG.

## Features

- **Markdown hosting** — paste Markdown, get a styled public page at `/p/{slug}`
- **AI chat widget** — floating chat bubble on every page, powered by VectorDB RAG streaming
- **Access code protection** — optionally require an access code to view a page
- **Header configuration** — customizable header with logo upload, contact info, and external links
- **View tracking** — tracks `num_views`, `last_view_date`, and viewer IP per page
- **Chat query management** — list, delete, and bulk-delete recorded chat queries per presentation
- **GenAI quota enforcement** — checks quota via ambivo_api `/genai/check_quota` (fails open on connection error)
- **Daily per-presentation chat limit** — configurable via `DAILY_CHAT_LIMIT` (default 20)

## Architecture

```
Author (Browser)                                     VectorDB API
  │                                                       │
  ├── /login ──> proxy to ambivo_api /user/login          │
  │              ──> JWT token stored in localStorage      │
  │                                                       │
  ├── /dashboard ──> list presentations (MongoDB)         │
  │                                                       │
  ├── /dashboard/create ──POST──> Content Portal          │
  │       paste Markdown, tags, chat toggle                │
  │       ──> insert into MongoDB                         │
  │       ──> create KB + index content ──────────────────>│
  │                                                       │
  ├── /dashboard/edit/{id} ──PUT──> Content Portal        │
  │       edit Markdown, tags, chat toggle                 │
  │       ──> update MongoDB                              │
  │       ──> re-index KB (truncate + index) ─────────────>│
  │                                                       │
Visitor (Browser)                                         │
  ├── /p/{slug} ──> Rendered HTML + Chat Widget           │
  │                        │                              │
  │    chat message ───> Portal ──proxy──> /kh/get_answer │
  │         ↑                              (SSE stream)   │
  │    SSE stream <───────────────────────────────────────┘
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env with your credentials

# 3. Run
uvicorn app.main:app --reload --port 8003
```

The app will be available at `http://localhost:8003`.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `JWT_SECRET_KEY` | Yes | — | Must match ambivo_api `cookie_secret` |
| `JWT_ALGORITHM` | No | `HS256` | JWT signing algorithm |
| `MONGODB_URL` | Yes | — | MongoDB connection string |
| `MONGODB_DATABASE` | No | `omnilonely` | Database name |
| `VECTORDB_API_URL` | No | `https://vectordbapi.ambivo.com` | VectorDB API base URL |
| `AMBIVO_INTERNAL_SECRET` | No | `""` | Service-to-service auth for VectorDB |
| `AMBIVO_API_URL` | No | `https://goferapi.ambivo.com` | Login proxy target (ambivo_api) |
| `DAILY_CHAT_LIMIT` | No | `20` | Per-presentation daily chat limit |
| `HOST` | No | `0.0.0.0` | Server bind address |
| `PORT` | No | `8003` | Server port |
| `DEBUG` | No | `false` | Enable debug mode |
| `LOG_LEVEL` | No | `INFO` | Logging level |

## API Reference

### Authenticated Endpoints

All require `Authorization: Bearer <jwt_token>` header.

#### Create Presentation

```
POST /api/presentations
```

```json
{
  "title": "My Presentation",
  "markdown_content": "# Hello\n\nThis is **Markdown** content.",
  "slug": "my-presentation",       // optional, auto-generated from title
  "description": "A short summary", // optional
  "tags": ["demo", "docs"],         // optional
  "chat_enabled": true               // optional, default true
}
```

Response `201`:
```json
{
  "id": "665f...",
  "tenant_id": "t123",
  "title": "My Presentation",
  "slug": "my-presentation",
  "hosted_url": "https://portal.ambivo.com/p/my-presentation",
  "kb_name": "content_t123abcd_my-presentation",
  "is_published": true,
  "chat_enabled": true,
  "description": "A short summary",
  "tags": ["demo", "docs"],
  "created_at": "2026-03-03T12:00:00+00:00",
  "updated_at": "2026-03-03T12:00:00+00:00"
}
```

#### List Presentations

```
GET /api/presentations
```

Returns array of `PresentationResponse` for the authenticated tenant.

#### Get Presentation

```
GET /api/presentations/{id}
```

#### Update Presentation

```
PUT /api/presentations/{id}
```

All fields optional. If `markdown_content` changes, the knowledge base is automatically re-indexed.

```json
{
  "title": "Updated Title",
  "markdown_content": "# New content\n\nUpdated.",
  "chat_enabled": false
}
```

#### Delete Presentation

```
DELETE /api/presentations/{id}
```

Deletes the presentation and its associated knowledge base. Returns `204`.

#### List Chat Queries

```
GET /api/presentations/{id}/queries?page=1&page_size=20
```

Returns paginated chat queries for the presentation.

#### Delete Single Chat Query

```
DELETE /api/presentations/{id}/queries/{query_id}
```

Deletes a single chat query. Returns `204`.

#### Delete All Chat Queries

```
DELETE /api/presentations/{id}/queries
```

Deletes all chat queries for the presentation. Returns `200` with deleted count.

#### Upload Header Logo

```
POST /api/presentations/{id}/logo
```

Upload an image file as the presentation's header logo. Multipart form data.

#### Remove Header Logo

```
DELETE /api/presentations/{id}/logo
```

Removes the header logo. Returns `200`.

#### Toggle Published Status

```
PATCH /api/presentations/{id}/publish
```

Toggles `is_published` between `true` and `false`. Unpublished pages return 404 to visitors.

### Public Endpoints (No Auth)

#### View Hosted Page

```
GET /p/{slug}
```

Renders the Markdown as a styled HTML page with an embedded chat widget. Tracks view count and viewer IP.

#### Verify Access Code

```
POST /p/{slug}/verify
```

```json
{ "access_code": "secret123" }
```

Validates the access code for a protected page. Sets a session cookie on success.

#### Serve Header Logo

```
GET /p/{slug}/logo
```

Returns the header logo image (cached 1 hour). Returns `404` if no logo is set.

#### Chat with Page Content

```
POST /api/chat/{presentation_id}
```

```json
{
  "message": "What is this page about?",
  "session_id": null
}
```

Returns a Server-Sent Events stream. Events:
- `event: session` — session ID (persist this for conversation continuity)
- `event: start` — stream started
- `event: chunk` — answer text chunk
- `event: done` — stream complete
- `event: error` — error message

Rate limited to 30 messages/minute per IP.

#### Health Check

```
GET /health
```

```json
{"status": "ok", "service": "content-portal"}
```

## Project Structure

```
ambivo-content-portal/
├── app/
│   ├── main.py                      # FastAPI app, CORS, router includes
│   ├── config.py                    # Pydantic-settings (env-based)
│   ├── db.py                        # Motor async MongoDB connection
│   ├── auth/
│   │   └── jwt_auth.py              # JWT decode (shared secret with ambivo_api)
│   ├── routes/
│   │   ├── auth_routes.py           # Login page, dashboard, create/edit pages, login proxy
│   │   ├── presentations.py         # CRUD API (authenticated)
│   │   ├── public.py                # GET /p/{slug} — serve rendered page
│   │   ├── chat.py                  # POST /api/chat/{id} — SSE proxy
│   │   └── health.py                # GET /health
│   ├── services/
│   │   ├── presentation_service.py  # CRUD + slug generation + KB lifecycle
│   │   ├── kb_service.py            # VectorDB KB create/index/delete/stream
│   │   ├── quota_service.py         # GenAI quota checks & daily chat limits
│   │   └── md_renderer.py           # Markdown → HTML
│   ├── models/
│   │   └── presentation.py          # Pydantic request/response models
│   ├── templates/
│   │   ├── login.html               # Sign-in page
│   │   ├── dashboard.html           # Presentation list
│   │   ├── create.html              # New presentation form (markdown paste + preview)
│   │   ├── edit.html                # Edit presentation form (pre-populated)
│   │   ├── access_code.html         # Access code verification page
│   │   └── page.html                # Public rendered page for visitors
│   └── static/
│       ├── css/
│       │   ├── dashboard.css        # Shared styles for dashboard, create, edit
│       │   └── page.css             # GitHub-flavored Markdown styling
│       ├── img/                     # Static images
│       └── js/
│           ├── dashboard.js         # Auth guard, list, create/edit, tags, preview
│           └── chat-widget.js       # Floating chat bubble + SSE streaming
├── Dockerfile
├── railway.json
├── requirements.txt
└── .env.example
```

## MongoDB

**Database:** `omnilonely`

**Connection:** Set via `MONGODB_URL` in `.env`

### Collections

| Collection | Purpose |
|------------|---------|
| `content_presentations` | Stores all presentation documents (markdown, metadata, KB references) |
| `content_chat_queries` | Recorded chat queries per presentation (TTL 90 days) |

> The VectorDB knowledge bases are stored externally in the Ambivo VectorDB API, not in MongoDB. Each presentation gets a dedicated KB collection named `content_{tenant_id[:8]}_{slug}`.

### `content_presentations` Document Schema

```
{
  "_id":              ObjectId
  "tenant_id":        string        — tenant that owns this presentation
  "userid":           string        — user who created it
  "title":            string        — display title
  "slug":             string        — URL-safe identifier (unique index)
  "markdown_content": string        — raw Markdown source
  "kb_name":          string        — VectorDB collection name
  "is_published":     boolean       — whether visible at /p/{slug}
  "chat_enabled":     boolean       — whether chat widget is active
  "description":      string | null — optional summary
  "tags":             [string]      — categorization tags
  "access_protected": boolean       — whether access code is required
  "access_codes":     [string]      — list of valid access codes
  "header": {                       — header configuration
    "enabled":        boolean
    "link_url":       string | null
    "link_text":      string | null
    "email":          string | null
    "phone":          string | null
    "text":           string | null
  }
  "header_logo":              binary | null  — uploaded logo image data
  "header_logo_content_type": string | null  — MIME type of the logo
  "has_header_logo":          boolean        — whether a logo is set
  "num_views":                int            — total page view count
  "last_view_date":           datetime | null — last view timestamp
  "last_view_ip":             string | null  — IP of last viewer
  "last_view_access_code":    string | null  — access code used by last viewer
  "created_at":       datetime      — UTC creation timestamp
  "updated_at":       datetime      — UTC last-modified timestamp
}
```

### Indexes — `content_presentations`

| Index | Type | Purpose |
|-------|------|---------|
| `slug` | Unique | Fast lookup for `/p/{slug}`, prevents duplicates |
| `tenant_id` | Secondary | Filter presentations by tenant |

### `content_chat_queries` Document Schema

```
{
  "_id":              ObjectId
  "presentation_id":  string        — associated presentation ID
  "tenant_id":        string        — tenant that owns the presentation
  "kb_name":          string        — VectorDB collection name
  "session_id":       string        — chat session ID
  "question":         string        — user's chat question
  "client_ip":        string        — IP address of the chatter
  "access_code":      string | null — access code used (if any)
  "date":             string        — date string (YYYY-MM-DD)
  "created_at":       datetime      — UTC timestamp
}
```

### Indexes — `content_chat_queries`

| Index | Type | Purpose |
|-------|------|---------|
| `(presentation_id, date)` | Compound | Filter queries by presentation and date |
| `created_at` | TTL (90 days) | Auto-expire old chat queries |

## Data Flow

### 1. Login
```
Browser → POST /api/auth/login → Portal proxies to ambivo_api /user/login
       ← JWT token + user info → stored in localStorage
```

### 2. Create Presentation
```
Browser → POST /api/presentations (JWT auth)
       → Portal inserts doc into content_presentations (MongoDB)
       → Portal calls VectorDB API: create KB + index markdown content
       ← PresentationResponse returned to browser
```

### 3. Edit Presentation
```
Browser → GET /api/presentations/{id} (returns PresentationDetail with markdown_content)
       → User edits markdown in browser
       → PUT /api/presentations/{id}
       → Portal updates MongoDB doc
       → If markdown changed: truncate KB → re-index new content in VectorDB
       ← Updated PresentationResponse
```

### 4. View Published Page
```
Visitor → GET /p/{slug}
       → Portal fetches doc from MongoDB by slug
       → Renders markdown to HTML via Jinja2 template + chat widget JS
       ← Styled HTML page with floating chat bubble
```

### 5. Chat with Content
```
Visitor → POST /api/chat/{presentation_id} { message, session_id }
       → Portal proxies to VectorDB /kh/get_answer (SSE stream)
       ← SSE events: session ID, answer chunks, done/error
```

## Knowledge Base Naming

Each presentation gets a dedicated VectorDB collection:

```
content_{tenant_id[:8]}_{slug}
```

This ensures isolation per tenant and clean deletion when a presentation is removed.

## Chat Widget

The hosted page includes a vanilla JS chat widget (no dependencies):

- Floating blue bubble in the bottom-right corner
- Click to open a chat panel
- Messages stream in real-time via SSE (POST + ReadableStream)
- Session ID persisted in `localStorage` for conversation continuity
- Inline Markdown rendering (bold, italic, code, links)

## Deployment

### Railway

```bash
railway up
```

Uses the included `Dockerfile` and `railway.json`. Set environment variables in the Railway dashboard.

### Docker

```bash
docker build -t ambivo-content-portal .
docker run -p 8003:8003 --env-file .env ambivo-content-portal
```

## Scaling notes

The book-mode KB re-indexing pipeline is **single-process by design** and
must run on a single Railway replica until it is migrated to an external
job queue.

Two pieces of state currently live in the request process:

- `app/services/chapter_service.py` keeps a module-level
  `_kb_locks: dict[str, asyncio.Lock]` to serialize concurrent reindexes
  of the same KB. With multiple replicas, two workers can race the same
  collection (truncate → partial reindex → second truncate → missing
  chapters).
- Chapter add / update / delete / bulk-import schedule the reindex via
  FastAPI `BackgroundTasks`, which runs **inside the request process
  after the response is sent**. A worker restart between response and
  reindex completion silently drops the job.

**Before scaling to >1 replica:** move `reindex_presentation` onto a
proper queue (Celery, RQ, or arq with a Redis broker) so locks become
distributed and jobs survive worker restarts. The function is already
idempotent — pulling it into a worker is a localized change.

For a single-replica deploy on Railway with low write volume (chapter
edits are admin-only), the current implementation is sufficient.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Framework | FastAPI |
| MongoDB driver | Motor (async) |
| HTTP client | httpx (async, streaming) |
| Markdown rendering | Python `markdown` + `pymdown-extensions` |
| Chat streaming | SSE via `sse-starlette` |
| Auth | JWT (PyJWT), shared secret with ambivo_api |
| Slug generation | `python-slugify` |
| Deployment | Docker + Railway |
