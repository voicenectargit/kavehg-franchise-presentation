"""
GenAI quota enforcement and daily chat-limit tracking.
"""
import logging
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.db import get_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GenAI token quota — delegates to ambivo_api
# ---------------------------------------------------------------------------

async def check_genai_quota(tenant_id: str) -> dict:
    """Call ambivo_api to verify the tenant still has GenAI quota.

    Returns a dict with at least ``allowed`` (bool).
    On connection errors the call **fails open** so chat isn't broken by
    an API outage.
    """
    url = f"{settings.ambivo_api_url}/genai/check_quota"
    headers = {"X-Internal-API-Key": settings.ambivo_internal_secret}
    params = {"tenant_id": tenant_id}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers, params=params)

            if resp.status_code == 402:
                body = resp.json()
                return {
                    "allowed": False,
                    "error": body.get("error", "Payment required"),
                    "error_code": body.get("error_code", "PAYMENT_REQUIRED"),
                }

            resp.raise_for_status()
            return resp.json()

    except httpx.HTTPStatusError as exc:
        logger.error(
            "GenAI quota check HTTP %s for tenant=%s: %s",
            exc.response.status_code, tenant_id, exc,
        )
        return {"allowed": False, "error": str(exc)}

    except Exception as exc:
        # Fail open — log and allow
        logger.warning(
            "GenAI quota check failed (allowing): tenant=%s %s(%s)",
            tenant_id, type(exc).__name__, exc,
        )
        return {"allowed": True}


# ---------------------------------------------------------------------------
# Daily per-presentation chat limit
# ---------------------------------------------------------------------------

async def check_daily_chat_limit(presentation_id: str, daily_limit: int | None = None) -> bool:
    """Return ``True`` if the presentation is still under its daily chat limit."""
    limit = daily_limit if daily_limit is not None else settings.daily_chat_limit
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    coll = get_db()["content_chat_queries"]
    count = await coll.count_documents({"presentation_id": presentation_id, "date": today})
    return count < limit


# ---------------------------------------------------------------------------
# Record a chat query
# ---------------------------------------------------------------------------

async def record_chat_query(
    presentation_id: str,
    tenant_id: str,
    kb_name: str,
    session_id: str,
    question: str,
    client_ip: str,
    access_code: str | None = None,
    ambivo_user_id: str | None = None,
    ambivo_email: str | None = None,
) -> None:
    """Insert a chat-query tracking document."""
    now = datetime.now(timezone.utc)
    doc = {
        "presentation_id": presentation_id,
        "tenant_id": tenant_id,
        "kb_name": kb_name,
        "session_id": session_id,
        "question": question,
        "client_ip": client_ip,
        "date": now.strftime("%Y-%m-%d"),
        "created_at": now,
    }
    if access_code:
        doc["access_code"] = access_code
    if ambivo_user_id:
        doc["ambivo_user_id"] = ambivo_user_id
    if ambivo_email:
        doc["ambivo_email"] = ambivo_email
    coll = get_db()["content_chat_queries"]
    await coll.insert_one(doc)
