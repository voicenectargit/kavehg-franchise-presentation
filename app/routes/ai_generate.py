"""
AI HTML page generation routes.

All endpoints require authentication and enforce GenAI quota before
every LLM call.  Returns 402 if the tenant has no GenAI access.
"""
import logging
import re
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from app.auth.jwt_auth import get_current_user
from app.services import ai_generator_service
from app.services.quota_service import check_genai_quota
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai-generate"])

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


# ── Request / response models ────────────────────────────────────────────

class StartRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=10000)


class AnswerRequest(BaseModel):
    answers: list[str] = Field(..., min_length=1, max_length=20)


class DirectGenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=10000)


# ── Helpers ──────────────────────────────────────────────────────────────

async def _enforce_quota(tenant_id: str) -> None:
    """Check GenAI quota; raise 402/503 on failure."""
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="AI generation is not available on this instance.")

    quota = await check_genai_quota(tenant_id)
    if not quota.get("allowed", True):
        detail = quota.get("error", "GenAI quota exhausted. Please add a payment method.")
        code = 402 if quota.get("error_code") == "PAYMENT_REQUIRED" else 429
        raise HTTPException(status_code=code, detail=detail)


def _validate_session_id(session_id: str) -> None:
    """Reject malformed session IDs."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format")


# ── Endpoints ────────────────────────────────────────────────────────────

@router.get("/generate/available")
async def check_ai_available(
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Check if AI generation is available for this tenant.

    Returns {"available": true/false, "reason": "..."}.
    Used by the frontend to show/hide the AI Generate button.
    """
    if not settings.anthropic_api_key:
        return {"available": False, "reason": "AI generation is not configured on this instance."}

    tenant_id = user["tenant_id"]
    quota = await check_genai_quota(tenant_id)
    if not quota.get("allowed", True):
        reason = quota.get("error", "GenAI quota exhausted.")
        return {"available": False, "reason": reason}

    return {"available": True}

@router.post("/generate/start")
async def start_generation(
    data: StartRequest,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Start an AI page-generation session.

    Returns a session_id and the first round of clarifying questions.
    """
    if not data.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    tenant_id = user["tenant_id"]
    await _enforce_quota(tenant_id)

    try:
        return await ai_generator_service.start_session(data.prompt.strip(), tenant_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception:
        logger.exception("AI start_session failed")
        raise HTTPException(status_code=500, detail="An internal error occurred while starting the AI session.")


@router.post("/generate/{session_id}/answer")
async def answer_questions(
    session_id: str,
    data: AnswerRequest,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Continue the design interview with answers.

    Returns either more questions (status=interviewing) or the generated
    HTML (status=complete).
    """
    _validate_session_id(session_id)
    tenant_id = user["tenant_id"]
    await _enforce_quota(tenant_id)

    try:
        result = await ai_generator_service.answer_questions(session_id, data.answers, tenant_id)
        # Clean up session when complete
        if result.get("status") == "complete":
            ai_generator_service.cleanup_session(session_id, tenant_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception:
        logger.exception("AI answer_questions failed")
        raise HTTPException(status_code=500, detail="An internal error occurred during the design interview.")


@router.post("/generate/direct")
async def generate_direct(
    data: DirectGenerateRequest,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Skip the interview and generate HTML directly from a detailed prompt.

    Use this when the user already has a very clear idea of what they want.
    """
    if not data.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    tenant_id = user["tenant_id"]
    await _enforce_quota(tenant_id)

    try:
        return await ai_generator_service.generate_direct(data.prompt.strip(), tenant_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception:
        logger.exception("AI generate_direct failed")
        raise HTTPException(status_code=500, detail="An internal error occurred during HTML generation.")
