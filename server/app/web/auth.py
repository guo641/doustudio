"""Authentication and request-forgery protection for the admin UI."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials


security = HTTPBasic(auto_error=False)


def _admin_credentials() -> tuple[str, str]:
    return (
        os.environ.get("DOUSTUDIO_ADMIN_USER", "").strip(),
        os.environ.get("DOUSTUDIO_ADMIN_PASSWORD", ""),
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": 'Basic realm="DouStudio License Admin"'},
    )


def verify_credentials(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
) -> str:
    """Require configured Basic Auth credentials; missing config never opens access."""
    admin_user, admin_password = _admin_credentials()
    if not admin_user or not admin_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin interface is not configured",
        )
    if credentials is None:
        raise _unauthorized("Authentication required")

    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), admin_user.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"), admin_password.encode("utf-8")
    )
    if not (user_ok and password_ok):
        raise _unauthorized("Incorrect username or password")
    return admin_user


def csrf_token() -> str:
    """Derive a stable per-deployment token shared by all uvicorn workers."""
    admin_user, admin_password = _admin_credentials()
    if not admin_user or not admin_password:
        return ""
    return hmac.new(
        admin_password.encode("utf-8"),
        b"doustudio-admin-csrf-v1\0" + admin_user.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_csrf(request: Request) -> None:
    """Require both a same-origin request and the deployment-derived CSRF token."""
    expected = csrf_token()
    supplied = request.headers.get("X-DouStudio-CSRF", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    origin = request.headers.get("Origin")
    if not origin:
        raise HTTPException(status_code=403, detail="Missing Origin header")
    parsed = urlsplit(origin)
    origin_host = parsed.netloc.lower()
    request_host = request.headers.get("Host", "").lower()
    if parsed.scheme not in {"http", "https"} or not secrets.compare_digest(
        origin_host, request_host
    ):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
