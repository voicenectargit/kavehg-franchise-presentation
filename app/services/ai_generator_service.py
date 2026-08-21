"""
AI-powered HTML page generator.

Uses Anthropic Claude to conduct a brief design interview, then generates
a complete, self-contained HTML page.  Quota is enforced *before* every LLM
call via quota_service.check_genai_quota().

Sessions are kept in-memory (dict keyed by session_id).  They're lightweight
and ephemeral — if the process restarts, users just start a new session.
"""
import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone

import anthropic
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── In-memory session store ──────────────────────────────────────────────
_sessions: dict[str, dict] = {}

_MAX_INTERVIEW_ROUNDS = 4
_MAX_SESSIONS = 500          # cap to prevent memory exhaustion
_SESSION_TTL_SECONDS = 1800  # 30 minutes


def _evict_stale_sessions() -> None:
    """Remove sessions older than TTL and enforce max-count cap."""
    now = datetime.now(timezone.utc)
    expired = [
        sid for sid, s in _sessions.items()
        if (now - datetime.fromisoformat(s["created_at"])).total_seconds() > _SESSION_TTL_SECONDS
    ]
    for sid in expired:
        _sessions.pop(sid, None)
    # If still over cap, remove oldest
    if len(_sessions) > _MAX_SESSIONS:
        by_age = sorted(_sessions.items(), key=lambda x: x[1]["created_at"])
        for sid, _ in by_age[: len(_sessions) - _MAX_SESSIONS]:
            _sessions.pop(sid, None)


# ── Prompts ──────────────────────────────────────────────────────────────

def _interview_system_prompt() -> str:
    return """You are an expert web designer conducting a brief design interview to build
a single, self-contained HTML page for the user.

Your job:
1. Identify the purpose of the page (landing page, proposal, product overview, portfolio, etc.)
2. Understand branding: company/project name, colors, tone, whether they have a logo URL
3. Define the hero / header area: headline, subheadline, call-to-action
4. Ask what content sections they want (features, testimonials, pricing, about, FAQ, gallery, stats, etc.)
5. Ask about visual theme: modern/minimal, bold/colorful, elegant/corporate, playful, dark mode, etc.
6. Ask about footer content: contact info, social links, copyright

Rules:
- Ask 3-5 focused, specific questions per round.  Offer concrete examples.
- Reference what the user already told you — don't repeat questions.
- After 1-2 rounds of answers (or if the user gave a very detailed prompt), set status to "ready".
- Keep it conversational and encouraging.

Respond with a JSON object (and nothing else):
{
    "status": "interviewing" or "ready",
    "questions": ["question 1", "question 2", ...],
    "summary": "brief summary of what you understand so far"
}

If status is "ready", questions should be empty and the summary must be comprehensive
enough to generate a complete HTML page."""


def _generation_system_prompt() -> str:
    current_year = datetime.now().year
    return f"""You are an expert web designer. Generate a **complete, self-contained HTML page**
based on the design conversation below.

Requirements for the generated HTML:
1. Use Tailwind CSS via CDN (<script src="https://cdn.tailwindcss.com"></script>).
2. Include Google Fonts via <link> if a specific font is desired.
3. The page must be fully responsive (mobile-first).
4. Use a clean, modern design with proper spacing and visual hierarchy.
5. Include smooth scroll behavior and subtle hover/transition effects.
6. Use semantic HTML (header, main, section, footer).
7. All styles must be inline or via Tailwind — no external CSS files.
8. Use placeholder images from https://placehold.co/ if real images aren't provided.
9. If a logo URL was provided, use it; otherwise use text-only branding.
10. Include a © {current_year} copyright in the footer.
11. The HTML must be completely self-contained — a single file that works when opened in a browser.

Return ONLY the raw HTML (starting with <!DOCTYPE html>), no markdown fences, no explanation."""


# ── Token usage reporting ────────────────────────────────────────────────

async def _report_token_usage(tenant_id: str, input_tokens: int, output_tokens: int) -> None:
    """Report token usage to ambivo_api for billing (fire-and-forget)."""
    url = f"{settings.ambivo_api_url}/genai/report_usage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                url,
                headers={"X-Internal-API-Key": settings.ambivo_internal_secret},
                json={
                    "tenant_id": tenant_id,
                    "source": "content_portal",
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("Failed to report token usage: %s", exc)
    except Exception as exc:
        logger.error("Unexpected error reporting token usage: %s", exc)


# ── LLM call ─────────────────────────────────────────────────────────────

# Lazily-initialized shared Anthropic client
_anthropic_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    """Return a shared AsyncAnthropic client (created once)."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


async def _call_llm(
    system: str,
    messages: list[dict],
    tenant_id: str,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> str:
    """Call Anthropic Claude and return the text response."""
    if not settings.anthropic_api_key:
        raise RuntimeError("AI generation is not configured (ANTHROPIC_API_KEY missing)")

    client = _get_client()
    try:
        resp = await client.messages.create(
            model=settings.ai_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=messages,
        )
        # Report token usage in background (truly fire-and-forget)
        usage = resp.usage
        asyncio.create_task(_report_token_usage(tenant_id, usage.input_tokens, usage.output_tokens))

        return resp.content[0].text
    except anthropic.AuthenticationError:
        raise RuntimeError("AI service authentication failed — check ANTHROPIC_API_KEY")
    except anthropic.RateLimitError:
        raise RuntimeError("AI service rate limit reached — please try again in a moment")
    except anthropic.APIError as exc:
        logger.error("Anthropic API error: %s", exc)
        raise RuntimeError("AI service encountered an error — please try again")


# ── Public API ───────────────────────────────────────────────────────────

async def start_session(prompt: str, tenant_id: str) -> dict:
    """Start a new AI generation session.  Returns session_id + first questions."""
    _evict_stale_sessions()

    session_id = secrets.token_urlsafe(16)

    messages = [{"role": "user", "content": prompt}]

    raw = await _call_llm(
        system=_interview_system_prompt(),
        messages=messages,
        tenant_id=tenant_id,
        temperature=0.3,
    )

    parsed = _parse_json(raw)
    status = parsed.get("status", "interviewing")

    _sessions[session_id] = {
        "tenant_id": tenant_id,
        "prompt": prompt,
        "messages": messages + [{"role": "assistant", "content": raw}],
        "round": 1,
        "status": status,
        "summary": parsed.get("summary", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return {
        "session_id": session_id,
        "status": status,
        "questions": parsed.get("questions", []),
        "summary": parsed.get("summary", ""),
    }


async def answer_questions(session_id: str, answers: list[str], tenant_id: str) -> dict:
    """Continue the interview with user answers.

    Returns more questions (status=interviewing) or triggers HTML generation
    (status=ready) which is followed by status=complete with html.
    """
    session = _sessions.get(session_id)
    if not session:
        raise ValueError("Session not found or expired")
    if session["tenant_id"] != tenant_id:
        raise ValueError("Session not found or expired")

    # Validate answers
    non_empty = [a for a in answers if a.strip()]
    if not non_empty:
        raise ValueError("At least one answer is required")

    # Build the user answer message — stage locally first, commit only on success
    answer_text = "\n".join(f"- {a}" for a in non_empty)
    new_round = session["round"] + 1

    # If we've hit max rounds, force ready
    force_ready = new_round > _MAX_INTERVIEW_ROUNDS
    user_msg = answer_text
    if force_ready:
        user_msg += (
            "\n\n(Note: please wrap up the interview — set status to 'ready' and "
            "provide a comprehensive summary for HTML generation.)"
        )

    # Build messages for LLM call without mutating session yet
    trial_messages = session["messages"] + [{"role": "user", "content": user_msg}]

    raw = await _call_llm(
        system=_interview_system_prompt(),
        messages=trial_messages,
        tenant_id=tenant_id,
        temperature=0.3,
    )

    parsed = _parse_json(raw)
    status = parsed.get("status", "interviewing")

    # Commit the mutation only after successful LLM call
    session["messages"] = trial_messages + [{"role": "assistant", "content": raw}]
    session["round"] = new_round
    session["summary"] = parsed.get("summary", session["summary"])

    if status == "ready" or force_ready:
        session["status"] = "ready"
        try:
            html = await _generate_html(session)
        except Exception:
            # Clean up the broken session so user can start fresh
            _sessions.pop(session_id, None)
            raise
        session["status"] = "complete"
        return {
            "session_id": session_id,
            "status": "complete",
            "html": html,
            "summary": session["summary"],
        }

    session["status"] = "interviewing"
    return {
        "session_id": session_id,
        "status": "interviewing",
        "questions": parsed.get("questions", []),
        "summary": session["summary"],
        "round": session["round"],
    }


async def generate_direct(prompt: str, tenant_id: str) -> dict:
    """Skip the interview and generate HTML directly from a detailed prompt."""
    messages = [
        {
            "role": "user",
            "content": f"Generate a complete HTML page based on this description:\n\n{prompt}",
        }
    ]

    html = await _call_llm(
        system=_generation_system_prompt(),
        messages=messages,
        tenant_id=tenant_id,
        temperature=0.2,
        max_tokens=16000,
    )

    html = _clean_html_response(html)
    return {"status": "complete", "html": html}


def get_session(session_id: str, tenant_id: str) -> dict | None:
    """Return session info (without full message history)."""
    session = _sessions.get(session_id)
    if not session or session["tenant_id"] != tenant_id:
        return None
    return {
        "session_id": session_id,
        "status": session["status"],
        "summary": session["summary"],
        "round": session["round"],
    }


def cleanup_session(session_id: str, tenant_id: str | None = None) -> None:
    """Remove a completed session from memory."""
    if tenant_id:
        session = _sessions.get(session_id)
        if session and session["tenant_id"] != tenant_id:
            return
    _sessions.pop(session_id, None)


# ── Helpers ──────────────────────────────────────────────────────────────

async def _generate_html(session: dict) -> str:
    """Generate the final HTML page from the full conversation."""
    conversation_summary = (
        f"Original prompt: {session['prompt']}\n\n"
        f"Design summary: {session['summary']}"
    )

    # Build a proper alternating-role message sequence for the generation call.
    # Start with a single user message containing the summary + full conversation.
    combined_parts = [conversation_summary, "\n\n--- Design conversation ---\n"]
    for msg in session["messages"]:
        role_label = "Designer" if msg["role"] == "assistant" else "User"
        combined_parts.append(f"\n{role_label}: {msg['content']}")

    combined_parts.append(
        "\n\n--- End of conversation ---\n\n"
        "Based on everything discussed above, please generate the complete HTML page now."
    )

    gen_messages = [
        {"role": "user", "content": "\n".join(combined_parts)},
    ]

    html = await _call_llm(
        system=_generation_system_prompt(),
        messages=gen_messages,
        tenant_id=session["tenant_id"],
        temperature=0.2,
        max_tokens=16000,
    )

    return _clean_html_response(html)


def _clean_html_response(text: str) -> str:
    """Strip markdown fences or other wrapper if the LLM added them."""
    text = text.strip()
    if text.startswith("```html"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _parse_json(text: str) -> dict:
    """Parse JSON from LLM response, tolerating markdown fences."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM JSON response: %.200s", text)
        return {"status": "interviewing", "questions": [], "summary": "Could not parse design summary — please try rephrasing."}
