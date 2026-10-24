"""
AI-powered HTML page generator.

Conducts a brief design interview, then generates a complete, self-contained
HTML page: one call plans it, then its sections are written a few per call
(see _build_page).  Every model call goes through Core's POST /genai/complete with the
internal secret, so Core picks the model (tier "standard"), enforces quota and
bills the tenant, and a tenant on its own AI keys is served by its gateway.
Core decides that per tenant on every call; nothing here pre-checks quota.

Sessions are kept in-memory (dict keyed by session_id).  They're lightweight
and ephemeral — if the process restarts, users just start a new session.
"""
import asyncio
import json
import logging
import re
import secrets
from datetime import datetime, timezone

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


# A page is written in pieces. Core stops one completion at 110 s, and a full page
# in one reply took longer than that. One call plans the page (head, shared design
# notes, sections); then the sections are written a few per call, in parallel;
# then the page is put together here.

_MAX_SECTIONS = 12
_SECTIONS_PER_CALL = 2
_PARALLEL_SECTION_CALLS = 3
_PLAN_MAX_TOKENS = 4000
_SECTION_MAX_TOKENS = 6000

_PLAN_SCHEMA = {
    "type": "object",
    "required": ["title", "head_html", "design_notes", "sections"],
    "properties": {
        "title": {"type": "string"},
        "lang": {"type": "string"},
        "body_class": {"type": "string"},
        "head_html": {"type": "string"},
        "design_notes": {"type": "string"},
        "sections": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_SECTIONS,
            "items": {
                "type": "object",
                "required": ["id", "name", "brief"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "brief": {"type": "string"},
                },
            },
        },
    },
}


def _plan_system_prompt() -> str:
    current_year = datetime.now().year
    return f"""You are an expert web designer planning a single, self-contained HTML page.
The page will be written section by section by other designers who see only your plan,
so the plan must carry everything they need to make the sections look like one page.

Return a JSON object:
- "title": the page <title>.
- "lang": the page language code, e.g. "en".
- "body_class": Tailwind classes for <body> (background, text color, font).
- "head_html": everything inside <head>: meta charset and viewport, <title>,
  <script src="https://cdn.tailwindcss.com"></script>, a tailwind.config script with the
  brand colors and fonts, Google Fonts <link> tags if a font is wanted, and a small <style>
  with `html {{ scroll-behavior: smooth; }}`. No external CSS files.
- "design_notes": the shared visual rules every section must follow: colors (as the
  Tailwind names you configured), fonts, heading sizes, spacing, container width, button
  and card styles, image style, tone of the copy.
- "sections": the page from top to bottom, at most {_MAX_SECTIONS}. Each has "id" (a short
  lowercase anchor such as "hero" or "pricing"), "name", and "brief": what the section
  contains, in enough detail to write it, including real copy where the user gave it.
  The first section is the header with navigation; the last is the footer, which
  includes a © {current_year} copyright.

Design rules: responsive and mobile-first, clean and modern, semantic HTML, subtle hover
and transition effects, placeholder images from https://placehold.co/ when no real images
are given, the user's logo URL if given, otherwise text-only branding."""


def _section_system_prompt(plan: dict) -> str:
    anchors = ", ".join(f"#{s['id']} ({s['name']})" for s in plan["sections"])
    return f"""You are an expert web designer writing part of one HTML page that uses
Tailwind CSS from its CDN (already loaded, with this configuration in <head>):

{plan.get("head_html", "")}

Shared design rules for every section:
{plan.get("design_notes", "")}

The page's sections, top to bottom (links between them use these anchors): {anchors}

Write ONLY the sections you are asked for, in order, as raw HTML fragments. Use
<header> for the header, <footer> for the footer and <section> for the rest, each with
its id attribute set to its anchor. No <!DOCTYPE>, <html>, <head> or <body>, no markdown
fences, no explanation. Only Tailwind classes and inline styles; a small inline <script>
is fine when a section needs one (a mobile menu, an FAQ toggle)."""


# ── LLM call (Core /genai/complete) ──────────────────────────────────────

# Core stops a completion at 110 s and answers first; the extra seconds cover the network.
_CORE_TIMEOUT = httpx.Timeout(10.0, read=125.0)
_CALLER = "content_portal:page_generator"


class AIRefusedError(RuntimeError):
    """Core refused the call for a reason the user can act on (quota, payment)."""

    def __init__(self, message: str, status_code: int = 402):
        super().__init__(message)
        self.status_code = status_code


async def _call_llm(
    system: str,
    messages: list[dict],
    tenant_id: str,
    user_id: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> str:
    """One completion through Core. Returns the text; raises RuntimeError on failure."""
    data = await _complete(system, messages, tenant_id, user_id, temperature, max_tokens)
    return data.get("text") or ""


async def _complete(
    system: str,
    messages: list[dict],
    tenant_id: str,
    user_id: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    output_schema: dict | None = None,
) -> dict:
    """One completion through Core. Returns Core's answer (text, json, schema_valid)."""
    if not settings.ambivo_internal_secret:
        raise RuntimeError("AI page generation is turned off on this server.")

    headers = {
        "X-AMBIVO-INTERNAL-SECRET": settings.ambivo_internal_secret,
        "X-AMBIVO-TENANT-ID": tenant_id,
    }
    if user_id:
        headers["X-AMBIVO-USER-ID"] = user_id
    body = {
        "messages": [{"role": "system", "content": system}] + messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "tier": "standard",
        "caller": _CALLER,
    }
    if output_schema is not None:
        body["output_schema"] = output_schema
    url = f"{settings.ambivo_api_url.rstrip('/')}/genai/complete"
    try:
        async with httpx.AsyncClient(timeout=_CORE_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        logger.warning("genai/complete timed out tenant=%s", tenant_id)
        raise RuntimeError("The page took too long to make. Try again, or use a shorter description.")
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("genai/complete failed tenant=%s: %r", tenant_id, exc)
        raise RuntimeError("AI page generation failed. Try again.")

    if data.get("result") == 1:
        return data

    code = data.get("error_code") or ""
    logger.warning("genai/complete refused tenant=%s code=%s message=%s",
                   tenant_id, code, data.get("message"))
    if code == "GENAI_QUOTA_BLOCKED":
        raise AIRefusedError(data.get("message") or "Add a payment method to use AI page generation.")
    if code == "GENAI_TIMEOUT":
        raise RuntimeError("The page took too long to make. Try again, or use a shorter description.")
    raise RuntimeError("AI page generation failed. Try again.")


# ── Public API ───────────────────────────────────────────────────────────

async def start_session(prompt: str, tenant_id: str, user_id: str | None = None) -> dict:
    """Start a new AI generation session.  Returns session_id + first questions."""
    _evict_stale_sessions()

    session_id = secrets.token_urlsafe(16)

    messages = [{"role": "user", "content": prompt}]

    raw = await _call_llm(
        system=_interview_system_prompt(),
        messages=messages,
        tenant_id=tenant_id,
        user_id=user_id,
        temperature=0.3,
    )

    parsed = _parse_json(raw)
    status = parsed.get("status", "interviewing")

    _sessions[session_id] = {
        "tenant_id": tenant_id,
        "user_id": user_id,
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


async def answer_questions(session_id: str, answers: list[str], tenant_id: str,
                           user_id: str | None = None) -> dict:
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
        user_id=user_id,
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


async def generate_direct(prompt: str, tenant_id: str, user_id: str | None = None) -> dict:
    """Skip the interview and generate HTML directly from a detailed prompt."""
    html = await _build_page(f"Make a page from this description:\n\n{prompt}", tenant_id, user_id)
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

    # The summary and the whole conversation become the brief for _build_page.
    combined_parts = [conversation_summary, "\n\n--- Design conversation ---\n"]
    for msg in session["messages"]:
        role_label = "Designer" if msg["role"] == "assistant" else "User"
        combined_parts.append(f"\n{role_label}: {msg['content']}")

    combined_parts.append(
        "\n\n--- End of conversation ---\n\n"
        "Make the page from everything discussed above."
    )

    return await _build_page("\n".join(combined_parts), session["tenant_id"], session.get("user_id"))


async def _build_page(brief: str, tenant_id: str, user_id: str | None) -> str:
    """Plan the page, write its sections a few at a time, and put it together."""
    plan = await _plan_page(brief, tenant_id, user_id)
    sections = plan["sections"]
    groups = [sections[i:i + _SECTIONS_PER_CALL] for i in range(0, len(sections), _SECTIONS_PER_CALL)]
    gate = asyncio.Semaphore(_PARALLEL_SECTION_CALLS)

    async def write(group: list[dict]) -> str:
        async with gate:
            return await _write_sections(plan, group, brief, tenant_id, user_id)

    parts = await asyncio.gather(*(write(g) for g in groups))
    logger.info("page generated tenant=%s sections=%d calls=%d", tenant_id, len(sections), len(groups) + 1)
    return _assemble(plan, parts)


async def _plan_page(brief: str, tenant_id: str, user_id: str | None) -> dict:
    data = await _complete(
        system=_plan_system_prompt(),
        messages=[{"role": "user", "content": brief}],
        tenant_id=tenant_id,
        user_id=user_id,
        temperature=0.2,
        max_tokens=_PLAN_MAX_TOKENS,
        output_schema=_PLAN_SCHEMA,
    )
    plan = data.get("json")
    if not data.get("schema_valid") or not isinstance(plan, dict):
        logger.warning("page plan did not match the schema tenant=%s errors=%s",
                       tenant_id, data.get("schema_errors"))
        raise RuntimeError("AI page generation failed. Try again.")
    plan["sections"] = plan["sections"][:_MAX_SECTIONS]
    for i, sec in enumerate(plan["sections"]):
        sec["id"] = re.sub(r"[^a-z0-9-]", "", str(sec.get("id") or "").lower()) or f"section-{i + 1}"
    return plan


async def _write_sections(plan: dict, group: list[dict], brief: str,
                          tenant_id: str, user_id: str | None) -> str:
    """HTML for some sections. One retry, since one failed part would lose the whole page."""
    wanted = "\n\n".join(f"#{s['id']}: {s['name']}\n{s['brief']}" for s in group)
    messages = [{"role": "user", "content": (
        f"What the page is for:\n{brief}\n\n--- Write these sections now ---\n\n{wanted}")}]
    for attempt in (1, 2):
        try:
            text = await _call_llm(
                system=_section_system_prompt(plan),
                messages=messages,
                tenant_id=tenant_id,
                user_id=user_id,
                temperature=0.2,
                max_tokens=_SECTION_MAX_TOKENS,
            )
            return _clean_fragment(text)
        except AIRefusedError:
            raise
        except RuntimeError:
            if attempt == 2:
                raise
            logger.warning("section call failed, retrying tenant=%s sections=%s",
                           tenant_id, [s["id"] for s in group])


def _assemble(plan: dict, parts: list[str]) -> str:
    head = plan.get("head_html") or ""
    if "cdn.tailwindcss.com" not in head:
        head = '<script src="https://cdn.tailwindcss.com"></script>\n' + head
    if "charset" not in head.lower():
        head = '<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n' + head
    lang = re.sub(r"[^A-Za-z-]", "", str(plan.get("lang") or "en")) or "en"
    body_class = str(plan.get("body_class") or "").replace('"', "")
    body = "\n\n".join(p for p in parts if p)
    return (f'<!DOCTYPE html>\n<html lang="{lang}">\n<head>\n{head}\n</head>\n'
            f'<body class="{body_class}">\n{body}\n</body>\n</html>')


_WRAPPER_TAGS = re.compile(r"<!DOCTYPE[^>]*>|</?html[^>]*>|<head\b.*?</head>|</?body[^>]*>",
                           re.IGNORECASE | re.DOTALL)


def _clean_fragment(text: str) -> str:
    """Strip fences and any page wrapper a section reply added."""
    return _WRAPPER_TAGS.sub("", _clean_html_response(text)).strip()


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
