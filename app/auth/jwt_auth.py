"""
JWT Authentication — matching ambivo_api token structure
"""
import jwt
import logging
from typing import Dict, Any, Optional
from fastapi import HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings

logger = logging.getLogger(__name__)
security = HTTPBearer()
security_optional = HTTPBearer(auto_error=False)


class JWTAuth:
    def __init__(self):
        self.secret_key = settings.jwt_secret_key
        self.algorithm = settings.jwt_algorithm

    def decode_token(self, token: str) -> Dict[str, Any]:
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
        except jwt.ExpiredSignatureError:
            logger.info("JWT decode rejected: token expired")
            raise HTTPException(status_code=401, detail="Token has expired")
        except jwt.InvalidTokenError as exc:
            logger.warning("JWT decode rejected: %s", exc)
            raise HTTPException(status_code=401, detail="Invalid token")

        user_id = payload.get("user_id") or payload.get("userid")
        tenant_id = payload.get("tenant_id")

        if not user_id or not tenant_id:
            logger.warning(
                "JWT payload missing required fields user_id/tenant_id (keys=%s)",
                list(payload.keys()),
            )
            raise HTTPException(status_code=401, detail="Invalid token: missing required fields")

        return {
            "userid": user_id,
            "tenant_id": tenant_id,
            "email": payload.get("email", ""),
            "scopes": payload.get("scopes", []),
            "is_tenant_admin": bool(payload.get("is_tenant_admin", False)),
        }


jwt_auth = JWTAuth()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Dict[str, Any]:
    return jwt_auth.decode_token(credentials.credentials)


async def get_current_user_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_optional),
) -> Optional[Dict[str, Any]]:
    """Return user dict if a valid bearer is present, else None.

    Bearer can come from:
      - Authorization header (preferred)
      - ?token=<jwt> query param (for iframe/top-level navigation where
        the parent can't inject headers)

    Never raises on missing/invalid token — callers decide whether the
    route requires auth.
    """
    token: Optional[str] = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    else:
        qp_token = request.query_params.get("token")
        if qp_token:
            token = qp_token

    if not token:
        return None

    try:
        return jwt_auth.decode_token(token)
    except HTTPException:
        return None
