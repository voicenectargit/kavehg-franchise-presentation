"""
Chat SSE proxy — streams answers from vectordb for a presentation's KB
Parses VectorDB JSON events and emits clean SSE to the browser.

Access modes mirror /p/{slug}:
  - public / access_code → no chat-time auth gate (page-level check enforces)
  - ambivo_session       → require a valid bearer token; attribute query
                           to the authenticated Ambivo user
"""
import asyncio
import json
import re
import time
import uuid
import logging
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.auth.jwt_auth import jwt_auth
from app.config import settings
from app.db import get_db
from app.models.presentation import ChatRequest
from app.services import chapter_service, kb_service
from app.services.quota_service import check_genai_quota, check_daily_chat_limit, record_chat_query

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

# Simple in-memory rate limiter: {ip: [timestamps]}
_rate: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = 30  # messages per minute
RATE_WINDOW = 60  # seconds

# Global cap on how many answers we stream from the inference backend at
# once. Excess requests await a free slot (FIFO via asyncio.Semaphore's
# internal waiter queue) rather than hammering the inferencing server in
# parallel. Created at import; binds to the running loop on first acquire.
_inference_sem = asyncio.Semaphore(settings.max_concurrent_chats)

# Qdrant emits "expected dim: 1536, got 1024" when the collection's vector
# size doesn't match the active embedder. We detect this and auto-trigger a
# force-recreate reindex so the KB self-heals after an embed-model swap.
# The regex tracks Qdrant's raw error tokens — if VectorDB ever rewrites
# these messages, this detector silently stops working (the chat would
# revert to surfacing the raw error). Update _DIM_MISMATCH_RE alongside
# any VectorDB error-format change.
_DIM_MISMATCH_RE = re.compile(
    r"expected dim:\s*(\d+).{0,60}?got\s*(\d+)",
    re.IGNORECASE | re.DOTALL,
)

# kb_names currently being auto-healed — prevents N concurrent chats from
# triggering N redundant reindexes for the same stale collection.
_healing_in_progress: set[str] = set()

# Strong references to in-flight heal tasks. asyncio.create_task only weak-
# refs the returned task, so without this the task can be garbage-collected
# mid-reindex. Tasks self-remove via add_done_callback.
_heal_tasks: set[asyncio.Task] = set()


def _parse_dim_mismatch(error_text: str | None) -> tuple[int, int] | None:
    """Return (expected_dim, got_dim) if the Qdrant vector-dim-mismatch
    error pattern is present, else None."""
    if not error_text:
        return None
    m = _DIM_MISMATCH_RE.search(error_text)
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except (TypeError, ValueError):
        return None


def _maybe_handle_dim_mismatch(
    payload: str | None,
    *,
    presentation_id: str,
    tenant_id: str,
    user_id: str,
    kb_name: str,
) -> str | None:
    """If `payload` carries Qdrant's dim-mismatch error, schedule an
    auto-heal reindex (deduped per-kb) and return the user-facing
    rebuild message. Otherwise return None.

    Detection lives here so both stream_chunk (where VectorDB actually
    delivers the upsert/query failure today) and stream_error (where
    VectorDB might emit it after future error-routing changes) share
    one code path.
    """
    dims = _parse_dim_mismatch(payload)
    if dims is None:
        return None
    expected, got = dims
    scheduled = _schedule_dim_autoheal(
        presentation_id=presentation_id,
        tenant_id=tenant_id,
        user_id=user_id,
        kb_name=kb_name,
    )
    logger.warning(
        "Stale Qdrant dim for kb=%s presentation=%s: "
        "collection=%d, embedder=%d — auto-heal %s",
        kb_name, presentation_id, expected, got,
        "scheduled" if scheduled else "already in progress",
    )
    return (
        "The knowledge base is being rebuilt to use the current "
        "embedding model. Please retry in a few minutes."
    )


def _schedule_dim_autoheal(
    *, presentation_id: str, tenant_id: str, user_id: str, kb_name: str
) -> bool:
    """Fire-and-forget force-recreate reindex when the KB collection has
    a stale embedding dim. Returns True if a heal was scheduled, False if
    one is already running for this kb."""
    if kb_name in _healing_in_progress:
        return False
    _healing_in_progress.add(kb_name)

    async def _run():
        try:
            await chapter_service.reindex_presentation(
                presentation_id, tenant_id, user_id, force_recreate=True
            )
        except Exception:
            logger.exception(
                "Auto-heal reindex failed for presentation=%s kb=%s",
                presentation_id, kb_name,
            )
        finally:
            _healing_in_progress.discard(kb_name)

    try:
        task = asyncio.create_task(_run())
    except RuntimeError:
        # No running event loop — should not happen inside a FastAPI
        # request handler, but unwind the in-progress marker so a later
        # request can retry rather than getting permanently locked out.
        _healing_in_progress.discard(kb_name)
        logger.exception(
            "Failed to schedule auto-heal task for kb=%s presentation=%s",
            kb_name, presentation_id,
        )
        return False
    _heal_tasks.add(task)
    task.add_done_callback(_heal_tasks.discard)
    return True


def _check_rate(ip: str):
    now = time.time()
    timestamps = _rate[ip]
    _rate[ip] = [t for t in timestamps if now - t < RATE_WINDOW]
    if len(_rate[ip]) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again shortly.")
    _rate[ip].append(now)


def _chat_access_mode(doc: dict) -> str:
    mode = doc.get("access_mode")
    if mode:
        return mode
    if doc.get("access_protected") and doc.get("access_codes"):
        return "access_code"
    return "public"


def _verify_chat_bearer(request: Request) -> dict | None:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    try:
        return jwt_auth.decode_token(token)
    except HTTPException as exc:
        logger.info("Chat bearer rejected: %s", exc.detail)
        return None


@router.post("/{presentation_id}")
async def chat(presentation_id: str, body: ChatRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    _check_rate(client_ip)

    from bson import ObjectId

    coll = get_db()["content_presentations"]
    doc = await coll.find_one({"_id": ObjectId(presentation_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Presentation not found")
    if not doc.get("chat_enabled", True):
        raise HTTPException(status_code=403, detail="Chat is disabled for this page")

    # Enforce ambivo_session at the chat boundary so a leaked
    # presentation_id can't be queried without a valid token.
    ambivo_user: dict | None = None
    if _chat_access_mode(doc) == "ambivo_session":
        ambivo_user = _verify_chat_bearer(request)
        if not ambivo_user:
            raise HTTPException(
                status_code=401,
                detail="Authentication required for this chat.",
            )
        if doc.get("access_tenant_only") and ambivo_user.get("tenant_id") != doc.get("tenant_id"):
            logger.warning(
                "Cross-tenant chat denied on presentation=%s: requester tenant=%s, owner tenant=%s, requester userid=%s",
                presentation_id,
                ambivo_user.get("tenant_id"),
                doc.get("tenant_id"),
                ambivo_user.get("userid"),
            )
            raise HTTPException(
                status_code=403,
                detail="This chat is restricted to its creator's organization.",
            )

    kb_name = doc["kb_name"]
    tenant_id = doc["tenant_id"]
    user_id = doc.get("userid") or doc.get("user_id", "")
    session_id = body.session_id or uuid.uuid4().hex
    access_code = request.cookies.get(f"cp_access_{doc['slug']}")

    # --- Quota & limit checks ---
    quota = await check_genai_quota(tenant_id)
    if not quota.get("allowed", True):
        raise HTTPException(
            status_code=402,
            detail=quota.get("error", "GenAI quota exhausted. Please add a payment method."),
        )

    if not await check_daily_chat_limit(presentation_id):
        raise HTTPException(
            status_code=429,
            detail=f"Daily chat limit ({settings.daily_chat_limit} queries) reached for this page. Try again tomorrow.",
        )

    async def event_generator():
        # Send session_id first so client can cache it for conversation memory
        yield {"event": "session", "data": session_id}

        # Record the chat query (fire-and-forget inside the stream)
        try:
            await record_chat_query(
                presentation_id, tenant_id, kb_name, session_id, body.message, client_ip,
                access_code=access_code,
                ambivo_user_id=(ambivo_user or {}).get("userid"),
                ambivo_email=(ambivo_user or {}).get("email"),
            )
        except Exception as rec_exc:
            logger.warning("Failed to record chat query: %s", rec_exc)

        # Throttle concurrent inference streams. If no slot is free, tell
        # the client it's queued (so it can show a "waiting in line" note)
        # and then block until a slot frees up. Released in finally so a
        # client disconnect (GeneratorExit) never leaks a permit.
        if _inference_sem.locked():
            yield {"event": "queued", "data": "Waiting for an available slot…"}
        await _inference_sem.acquire()
        try:
            async for line in kb_service.query_kb_stream(
                kb_name, body.message, session_id, tenant_id, user_id
            ):
                # Each line from VectorDB is "data: {json}" — extract the JSON
                raw = line
                if raw.startswith("data:"):
                    raw = raw[5:].strip()
                if not raw:
                    continue

                # Try to parse as JSON
                try:
                    evt = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    logger.debug(
                        "Skipped non-JSON line: %.200s  kb=%s presentation=%s",
                        raw, kb_name, presentation_id,
                    )
                    continue

                evt_type = evt.get("type", "")

                if evt_type == "stream_chunk":
                    text = evt.get("text", "")
                    # VectorDB delivers query/upsert failures inline as a
                    # stream_chunk whose text holds the raw Qdrant error
                    # ("Wrong input: Vector dimension error: ..."). Catch
                    # it here so the chat surfaces a clean rebuild
                    # message and self-heals, instead of printing the
                    # raw error to the user.
                    rebuild_msg = _maybe_handle_dim_mismatch(
                        text,
                        presentation_id=presentation_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        kb_name=kb_name,
                    )
                    if rebuild_msg is not None:
                        yield {"event": "error", "data": rebuild_msg}
                        return
                    if text:
                        yield {"event": "chunk", "data": text}

                elif evt_type == "stream_start":
                    yield {"event": "start", "data": ""}

                elif evt_type == "stream_complete":
                    # VectorDB packs source metadata under
                    # answer_dict_list[i].source_list[]. Each entry is
                    # {display_file_name, source_text, score, page, ...}.
                    # We dedupe by display_file_name (multiple chunks
                    # from one chapter collapse to one citation), keep
                    # the highest score, and forward a sources event.
                    sources_by_name: dict = {}
                    for ad in evt.get("answer_dict_list") or []:
                        for src in (ad or {}).get("source_list") or []:
                            name = (src.get("display_file_name") or "").strip()
                            if not name:
                                continue
                            score = src.get("score") or 0
                            excerpt = (src.get("source_text") or "").strip()
                            existing = sources_by_name.get(name)
                            if existing is None or score > existing.get("score", 0):
                                sources_by_name[name] = {
                                    "display_file_name": name,
                                    "score": score,
                                    "excerpt": excerpt[:200],
                                }
                    if sources_by_name:
                        ordered = sorted(
                            sources_by_name.values(),
                            key=lambda s: s.get("score", 0),
                            reverse=True,
                        )
                        try:
                            yield {"event": "sources", "data": json.dumps(ordered)}
                        except (TypeError, ValueError):
                            logger.warning(
                                "Failed to serialize sources for kb=%s", kb_name
                            )
                    else:
                        # Useful for diagnosing missing citations on an
                        # otherwise-successful answer.
                        logger.debug(
                            "stream_complete with no usable sources for kb=%s "
                            "(answer_dict_list present=%s)",
                            kb_name,
                            bool(evt.get("answer_dict_list")),
                        )
                    yield {"event": "done", "data": ""}
                    return

                elif evt_type == "stream_error":
                    error_msg = evt.get("error", "Unknown error")
                    rebuild_msg = _maybe_handle_dim_mismatch(
                        error_msg,
                        presentation_id=presentation_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        kb_name=kb_name,
                    )
                    if rebuild_msg is not None:
                        yield {"event": "error", "data": rebuild_msg}
                        return
                    logger.error(f"VectorDB stream error for kb={kb_name}: {error_msg}")
                    yield {"event": "error", "data": error_msg}
                    return

                else:
                    logger.debug(
                        "Unrecognized event type=%s  kb=%s presentation=%s",
                        evt_type, kb_name, presentation_id,
                    )

        except Exception as exc:
            logger.exception(
                "Chat stream error: %s(%s)  kb=%s tenant=%s presentation=%s",
                type(exc).__name__, exc, kb_name, tenant_id, presentation_id,
            )
            yield {"event": "error", "data": "An error occurred while generating the response."}
        finally:
            _inference_sem.release()
        yield {"event": "done", "data": ""}

    return EventSourceResponse(event_generator())
