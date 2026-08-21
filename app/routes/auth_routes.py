"""
Login page + login API proxy to ambivo_api /user/login
"""
import logging
from typing import Dict, Any

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.asset_version import asset_version
from app.auth.jwt_auth import jwt_auth
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


class LoginRequest(BaseModel):
    email: str
    password: str
    device_id: str | None = None
    os: str = "web"


class TokenLoginRequest(BaseModel):
    token: str


@router.get("/", response_class=HTMLResponse)
async def root_redirect(request: Request):
    """Adopt the shared .ambivo.com auth cookie (SSO), then route to
    dashboard or login based on the resulting client-side auth state."""
    return HTMLResponse(
        '<script>'
        '(async function(){'
        'function rc(){var n=["auth_token","dev_auth_token"];'
        'for(var i=0;i<n.length;i++){var m=document.cookie.match(new RegExp("(?:^|; )"+n[i]+"=([^;]+)"));'
        'if(m)return decodeURIComponent(m[1]);}return null;}'
        'if(!localStorage.getItem("cp_token")&&!localStorage.getItem("cp_sso_optout")){var t=rc();if(t){try{'
        'var r=await fetch("/api/auth/from-token",{method:"POST",'
        'headers:{"Content-Type":"application/json"},body:JSON.stringify({token:t})});'
        'if(r.ok){var d=await r.json();if(d.result===1&&d.token){'
        'localStorage.setItem("cp_token",d.token);'
        'localStorage.setItem("cp_user",JSON.stringify(d.user));}}}catch(e){}}}'
        'if(localStorage.getItem("cp_token")){window.location.replace("/dashboard");}'
        'else{window.location.replace("/login");}'
        '})();'
        '</script>'
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {
        "api_base": "",
    })


@router.get("/dashboard/create", response_class=HTMLResponse)
async def create_page(request: Request):
    return templates.TemplateResponse(request, "create.html", {
        "api_base": "",
    })


@router.get("/dashboard/edit/{presentation_id}", response_class=HTMLResponse)
async def edit_page(request: Request, presentation_id: str):
    return templates.TemplateResponse(request, "edit.html", {
        "api_base": "",
        "presentation_id": presentation_id,
        "asset_version": asset_version(
            "js/dashboard.js", "js/chapters-editor.js", "css/dashboard.css"
        ),
    })


@router.post("/api/auth/login")
async def login_api(request: Request, data: LoginRequest) -> Dict[str, Any]:
    """Proxy login to ambivo_api /user/login, return token."""
    try:
        payload = {
            "email": data.email,
            "password": data.password,
            "os": data.os,
        }
        if data.device_id:
            payload["device_id"] = data.device_id

        # Forward client IP and User-Agent so Core API sees the real client, not the proxy
        client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if not client_ip:
            client_ip = request.client.host if request.client else ""

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.ambivo_api_url}/user/login",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Forwarded-For": client_ip,
                    "User-Agent": request.headers.get("user-agent", ""),
                },
            )
            result = resp.json()
    except Exception as e:
        logger.exception("Login proxy error")
        raise HTTPException(status_code=502, detail="Unable to reach authentication service")

    if result.get("result") != 1:
        raise HTTPException(
            status_code=401,
            detail=result.get("error", {}).get("message") or result.get("error_code") or "Login failed",
        )

    user = result.get("user", {})
    return {
        "result": 1,
        "token": user.get("token"),
        "user": {
            "id": user.get("id"),
            "name": user.get("name") or user.get("formatted_name") or user.get("email"),
            "email": user.get("email"),
            "tenant_id": user.get("tenant_id"),
        },
    }


@router.post("/api/auth/from-token")
async def from_token_api(data: TokenLoginRequest) -> Dict[str, Any]:
    """Validate a shared Ambivo JWT and return a portal session.

    The Ambivo apps store the auth token in a cookie scoped to the parent
    `.ambivo.com` domain, so it is already present on this subdomain. Since the
    portal validates tokens with the same shared secret, the cookie token can be
    adopted directly here — enabling cross-app SSO with no relogin.
    """
    claims = jwt_auth.decode_token(data.token)  # raises 401 if invalid/expired
    return {
        "result": 1,
        "token": data.token,
        "user": {
            "id": claims.get("userid"),
            "name": claims.get("email") or claims.get("userid"),
            "email": claims.get("email"),
            "tenant_id": claims.get("tenant_id"),
        },
    }
