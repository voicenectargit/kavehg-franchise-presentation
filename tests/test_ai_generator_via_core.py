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


def _plan(n=3):
    secs = [{"id": "header", "name": "Header", "brief": "nav"}]
    secs += [{"id": f"s{i}", "name": f"Part {i}", "brief": f"part {i}"} for i in range(1, n - 1)]
    secs += [{"id": "footer", "name": "Footer", "brief": "copyright"}]
    return {"title": "T", "lang": "en", "body_class": "bg-white",
            "head_html": '<meta charset="UTF-8"><title>T</title><script src="https://cdn.tailwindcss.com"></script>',
            "design_notes": "blue buttons", "sections": secs}


def _page_core(core, plan, fail_first=()):
    """Plan calls (output_schema) get the plan; section calls echo their anchors."""
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        if "output_schema" in body:
            return httpx.Response(200, json={"result": 1, "json": plan, "schema_valid": True, "text": ""})
        ask = body["messages"][-1]["content"].split("--- Write these sections now ---")[1]
        ids = [line[1:].split(":")[0] for line in ask.split("\n") if line.startswith("#")]
        key = ",".join(ids)
        seen[key] = seen.get(key, 0) + 1
        if key in fail_first and seen[key] == 1:
            return httpx.Response(200, json={"result": 2, "error_code": "GENAI_TIMEOUT"})
        html = "".join(f'<section id="{i}">{i}</section>' for i in ids)
        return httpx.Response(200, json={"result": 1, "text": "```html\n" + html + "\n```"})
    core["handler"] = handler
    return seen


def test_direct_generation_plans_then_writes_sections_in_order(core):
    _page_core(core, _plan(5))
    out = _run(svc.generate_direct("a page", "t1", "u1"))
    html = out["html"]
    assert out["status"] == "complete"
    assert html.startswith('<!DOCTYPE html>\n<html lang="en">')
    assert '<body class="bg-white">' in html and "cdn.tailwindcss.com" in html
    ids = ["header", "s1", "s2", "s3", "footer"]
    assert [html.index(f'id="{i}"') for i in ids] == sorted(html.index(f'id="{i}"') for i in ids)
    assert "```" not in html
    bodies = [json.loads(c.content) for c in core["calls"]]
    assert "output_schema" in bodies[0] and bodies[0]["max_tokens"] == svc._PLAN_MAX_TOKENS
    # 5 sections, 2 per call: 3 section calls, each well under Core's limit
    assert len(bodies) == 4
    assert all(b["max_tokens"] == svc._SECTION_MAX_TOKENS for b in bodies[1:])
    assert all(c.headers["X-AMBIVO-USER-ID"] == "u1" for c in core["calls"])


def test_every_section_call_gets_the_shared_design(core):
    _page_core(core, _plan(4))
    _run(svc.generate_direct("a page", "t1"))
    for c in core["calls"][1:]:
        system = json.loads(c.content)["messages"][0]["content"]
        assert "blue buttons" in system and "#footer (Footer)" in system and "cdn.tailwindcss.com" in system


def test_a_failed_section_call_is_retried_once(core):
    seen = _page_core(core, _plan(4), fail_first=("header,s1",))
    out = _run(svc.generate_direct("a page", "t1"))
    assert seen["header,s1"] == 2 and 'id="s1"' in out["html"]


def test_a_section_that_fails_twice_fails_the_page(core):
    plan = _plan(4)

    def handler(req):
        body = json.loads(req.content)
        if "output_schema" in body:
            return httpx.Response(200, json={"result": 1, "json": plan, "schema_valid": True})
        return httpx.Response(200, json={"result": 2, "error_code": "GENAI_PROVIDER_ERROR"})
    core["handler"] = handler
    with pytest.raises(RuntimeError, match="failed"):
        _run(svc.generate_direct("a page", "t1"))


def test_a_plan_that_misses_the_schema_fails_cleanly(core):
    core["handler"] = lambda req: httpx.Response(200, json={
        "result": 1, "json": {"title": "x"}, "schema_valid": False, "schema_errors": ["sections missing"]})
    with pytest.raises(RuntimeError, match="failed"):
        _run(svc.generate_direct("a page", "t1"))
    assert len(core["calls"]) == 1


def test_assembly_adds_tailwind_and_strips_wrappers(core):
    plan = _plan(3)
    plan["head_html"] = "<title>T</title>"
    plan["sections"][0]["id"] = "Top Nav!"
    html = svc._assemble(plan, [svc._clean_fragment(
        "<!DOCTYPE html><html><head><title>x</title></head><body><header id='a'>h</header></body></html>")])
    assert "cdn.tailwindcss.com" in html and 'charset="UTF-8"' in html
    assert html.count("<body") == 1 and html.count("<head>") == 1 and "<header id='a'>h</header>" in html
    _page_core(core, plan)
    _run(svc.generate_direct("p", "t1"))
    assert 'id="topnav"' in _run(svc.generate_direct("p", "t1"))["html"]


def test_interview_ends_in_a_sectioned_page_for_the_same_user(core):
    plan = _plan(3)
    replies = iter([
        json.dumps({"status": "interviewing", "questions": ["Colors?"], "summary": "s"}),
        json.dumps({"status": "ready", "questions": [], "summary": "full"}),
    ])
    page = _page_core(core, plan)
    page_handler = core["handler"]

    def handler(req):
        body = json.loads(req.content)
        if "output_schema" in body or "--- Write these sections now ---" in body["messages"][-1]["content"]:
            return page_handler(req)
        return httpx.Response(200, json={"result": 1, "text": next(replies)})
    core["handler"] = handler
    first = _run(svc.start_session("landing page", "t1", "u1"))
    done = _run(svc.answer_questions(first["session_id"], ["blue"], "t1", "u1"))
    assert done["status"] == "complete" and 'id="footer"' in done["html"]
    assert "blue" in json.loads(core["calls"][2].content)["messages"][-1]["content"]
    assert page and all(c.headers["X-AMBIVO-USER-ID"] == "u1" for c in core["calls"])


def test_route_maps_a_quota_refusal_to_402(core):
    core["handler"] = lambda req: httpx.Response(200, json={
        "result": 2, "error_code": "GENAI_QUOTA_BLOCKED", "message": "Add a payment method."})
    with pytest.raises(HTTPException) as ex:
        _run(ai_generate.generate_direct(ai_generate.DirectGenerateRequest(prompt="p"),
                                         {"tenant_id": "t1", "userid": "u1"}))
    assert ex.value.status_code == 402 and ex.value.detail == "Add a payment method."


def test_availability_needs_only_the_internal_secret(core, monkeypatch):
    assert _run(ai_generate.check_ai_available({"tenant_id": "t1"})) == {"available": True}
    monkeypatch.setattr(settings, "ambivo_internal_secret", "")
    assert _run(ai_generate.check_ai_available({"tenant_id": "t1"}))["available"] is False


def test_routes_leave_the_quota_decision_to_core(core, monkeypatch):
    """A tenant on its own AI keys may have no payment method. Core serves it through
    its gateway, so the portal must not refuse it first with /genai/check_quota."""
    from app.services import quota_service

    async def refuse(*a, **kw):
        raise AssertionError("AI page generation must not call /genai/check_quota")
    monkeypatch.setattr(quota_service, "check_genai_quota", refuse)
    _page_core(core, _plan(3))
    user = {"tenant_id": "byok", "userid": "u1"}
    assert _run(ai_generate.check_ai_available(user)) == {"available": True}
    out = _run(ai_generate.generate_direct(ai_generate.DirectGenerateRequest(prompt="p"), user))
    assert out["html"].startswith("<!DOCTYPE html>")
    assert {str(c.url) for c in core["calls"]} == {"https://core.test/genai/complete"}
