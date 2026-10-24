"""
AI HTML page generation routes.

All endpoints require authentication.  Core's /genai/complete decides, per
tenant and per call, how the call is served: a tenant on its own AI keys goes
through its own gateway; any other tenant uses Ambivo's keys, with Core's quota
and billing.  There is no pre-check here: Core's /genai/check_quota does not
know about a tenant's own keys, so it would refuse a tenant Core would serve.
A quota refusal from Core comes back as 402.
"""
import logging
import re
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from app.auth.jwt_auth import get_current_user
from app.services import ai_generator_service
from app.services.ai_generator_service import AIRefusedError
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

def _require_configured() -> None:
    """503 when this server cannot reach Core's AI."""
    if not settings.ambivo_internal_secret:
        raise HTTPException(status_code=503, detail="AI page generation is turned off on this server.")


def _validate_session_id(session_id: str) -> None:
    """Reject malformed session IDs."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format")


# ── Endpoints ────────────────────────────────────────────────────────────

@router.get("/generate/available")
async def check_ai_available(
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Check if AI generation is available on this server.

    Quota is decided by Core on each call (see the module docstring).

    Returns {"available": true/false, "reason": "..."}.
    Used by the frontend to show/hide the AI Generate button.
    """
    if not settings.ambivo_internal_secret:
        return {"available": False, "reason": "AI page generation is turned off on this server."}

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
    _require_configured()

    try:
        return await ai_generator_service.start_session(data.prompt.strip(), tenant_id, user.get("userid"))
    except AIRefusedError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
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
    _require_configured()

    try:
        result = await ai_generator_service.answer_questions(session_id, data.answers, tenant_id,
                                                             user.get("userid"))
        # Clean up session when complete
        if result.get("status") == "complete":
            ai_generator_service.cleanup_session(session_id, tenant_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AIRefusedError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
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
    _require_configured()

    try:
        return await ai_generator_service.generate_direct(data.prompt.strip(), tenant_id, user.get("userid"))
    except AIRefusedError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception:
        logger.exception("AI generate_direct failed")
        raise HTTPException(status_code=500, detail="An internal error occurred during HTML generation.")
