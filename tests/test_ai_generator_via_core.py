"""AI page generation calls Core's /genai/complete, not Anthropic directly.

Core picks the model, enforces quota and bills; the portal holds no AI key.
The tests replace httpx's transport, so the real request building and error
mapping run and nothing leaves the machine.

Run: python -m pytest tests/test_ai_generator_via_core.py
"""
import asyncio
import json
import os

os.environ.setdefault("JWT_SECRET_KEY", "test")
os.environ.setdefault("MONGODB_URL", "mongodb://localhost:27017")

import httpx
import pytest
from fastapi import HTTPException

from app.config import settings
from app.routes import ai_generate
from app.services import ai_generator_service as svc


@pytest.fixture
def core(monkeypatch):
    """Answer Core calls from a handler; record each request."""
    calls = []
    state = {"handler": lambda req: httpx.Response(200, json={"result": 1, "text": "ok"})}
    real_client = httpx.AsyncClient

    def transport(req):
        calls.append(req)
        return state["handler"](req)

    monkeypatch.setattr(svc.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw))
    monkeypatch.setattr(settings, "ambivo_internal_secret", "s3cret")
    monkeypatch.setattr(settings, "ambivo_api_url", "https://core.test/")
    state["calls"] = calls
    return state


def _run(coro):
    return asyncio.run(coro)


def test_the_call_goes_to_core_with_the_internal_secret_and_tenant(core):
    out = _run(svc._call_llm("SYS", [{"role": "user", "content": "hi"}], "t1", user_id="u1",
                             temperature=0.2, max_tokens=16000))
    assert out == "ok"
    req = core["calls"][0]
    assert str(req.url) == "https://core.test/genai/complete"
    assert req.headers["X-AMBIVO-INTERNAL-SECRET"] == "s3cret"
    assert req.headers["X-AMBIVO-TENANT-ID"] == "t1"
    assert req.headers["X-AMBIVO-USER-ID"] == "u1"
    body = json.loads(req.content)
    assert body["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
    assert body["max_tokens"] == 16000 and body["temperature"] == 0.2
    assert body["tier"] == "standard" and body["caller"] == "content_portal:page_generator"


def test_no_user_header_when_the_user_is_unknown(core):
    _run(svc._call_llm("S", [{"role": "user", "content": "x"}], "t1"))
    assert "X-AMBIVO-USER-ID" not in core["calls"][0].headers


def test_quota_refusal_is_a_402_with_cores_message(core):
    core["handler"] = lambda req: httpx.Response(200, json={
        "result": 2, "error_code": "GENAI_QUOTA_BLOCKED", "message": "Add a payment method."})
    with pytest.raises(svc.AIRefusedError) as ex:
        _run(svc._call_llm("S", [{"role": "user", "content": "x"}], "t1"))
    assert ex.value.status_code == 402 and str(ex.value) == "Add a payment method."


def test_core_timeout_and_other_refusals_are_runtime_errors(core):
    core["handler"] = lambda req: httpx.Response(200, json={"result": 2, "error_code": "GENAI_TIMEOUT"})
    with pytest.raises(RuntimeError, match="too long"):
        _run(svc._call_llm("S", [{"role": "user", "content": "x"}], "t1"))
    core["handler"] = lambda req: httpx.Response(200, json={"result": 2, "error_code": "GENAI_PROVIDER_ERROR"})
    with pytest.raises(RuntimeError, match="failed") as ex:
        _run(svc._call_llm("S", [{"role": "user", "content": "x"}], "t1"))
    assert not isinstance(ex.value, svc.AIRefusedError)


def test_http_errors_and_network_timeouts_become_runtime_errors(core):
    core["handler"] = lambda req: httpx.Response(502, text="bad gateway")
    with pytest.raises(RuntimeError, match="failed"):
        _run(svc._call_llm("S", [{"role": "user", "content": "x"}], "t1"))

    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)
    core["handler"] = slow
    with pytest.raises(RuntimeError, match="too long"):
        _run(svc._call_llm("S", [{"role": "user", "content": "x"}], "t1"))


def test_no_internal_secret_means_turned_off(core, monkeypatch):
    monkeypatch.setattr(settings, "ambivo_internal_secret", "")
    with pytest.raises(RuntimeError, match="turned off"):
        _run(svc._call_llm("S", [{"role": "user", "content": "x"}], "t1"))
    assert core["calls"] == []


def test_direct_generation_returns_clean_html(core):
    core["handler"] = lambda req: httpx.Response(200, json={
        "result": 1, "text": "```html\n<!DOCTYPE html><p>x</p>\n```"})
    out = _run(svc.generate_direct("a page", "t1", "u1"))
    assert out == {"status": "complete", "html": "<!DOCTYPE html><p>x</p>"}
    assert json.loads(core["calls"][0].content)["max_tokens"] == 16000


def test_interview_keeps_the_user_for_the_final_generation(core):
    replies = iter([
        json.dumps({"status": "interviewing", "questions": ["Colors?"], "summary": "s"}),
        json.dumps({"status": "ready", "questions": [], "summary": "full"}),
        "<!DOCTYPE html><p>done</p>",
    ])
    core["handler"] = lambda req: httpx.Response(200, json={"result": 1, "text": next(replies)})
    first = _run(svc.start_session("landing page", "t1", "u1"))
    done = _run(svc.answer_questions(first["session_id"], ["blue"], "t1", "u1"))
    assert done["status"] == "complete" and done["html"] == "<!DOCTYPE html><p>done</p>"
    assert [c.headers["X-AMBIVO-USER-ID"] for c in core["calls"]] == ["u1", "u1", "u1"]


def test_route_maps_a_quota_refusal_to_402(core, monkeypatch):
    async def allowed(tenant_id):
        return {"allowed": True}
    monkeypatch.setattr(ai_generate, "check_genai_quota", allowed)
    core["handler"] = lambda req: httpx.Response(200, json={
        "result": 2, "error_code": "GENAI_QUOTA_BLOCKED", "message": "Add a payment method."})
    with pytest.raises(HTTPException) as ex:
        _run(ai_generate.generate_direct(ai_generate.DirectGenerateRequest(prompt="p"),
                                         {"tenant_id": "t1", "userid": "u1"}))
    assert ex.value.status_code == 402 and ex.value.detail == "Add a payment method."


def test_availability_needs_only_the_internal_secret(core, monkeypatch):
    async def allowed(tenant_id):
        return {"allowed": True}
    monkeypatch.setattr(ai_generate, "check_genai_quota", allowed)
    assert _run(ai_generate.check_ai_available({"tenant_id": "t1"})) == {"available": True}
    monkeypatch.setattr(settings, "ambivo_internal_secret", "")
    assert _run(ai_generate.check_ai_available({"tenant_id": "t1"}))["available"] is False
