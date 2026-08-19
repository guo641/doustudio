"""Server-rendered administration routes for the license service."""
from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from ..storage.admin_queries import (
    extend_license_expiry,
    get_all_licenses,
    get_heartbeat_distribution,
    get_revoked_licenses,
    get_statistics,
    revoke_licenses,
)
from .auth import csrf_token, verify_credentials, verify_csrf


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")

LicenseFilter = Literal["active_24h", "active_7d", "expiring_30d", "expired"]
SortField = Literal["last_seen_at", "first_seen_at", "expires_at", "heartbeat_count"]
SortOrder = Literal["asc", "desc"]
_HMAC_RE = __import__("re").compile(r"^[0-9a-f]{64}$")


def timestamp_to_datetime(value: int | None) -> str:
    if value is None:
        return "-"
    # 北京时间 UTC+8
    beijing_tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.fromtimestamp(value, tz=beijing_tz).strftime(
        "%Y-%m-%d %H:%M:%S +08"
    )


templates.env.filters["timestamp_to_datetime"] = timestamp_to_datetime


class LicenseTargets(BaseModel):
    license_hmacs: list[str] = Field(min_length=1, max_length=50)

    @field_validator("license_hmacs")
    @classmethod
    def normalize_hmacs(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            candidate = value.strip().lower()
            if not _HMAC_RE.fullmatch(candidate):
                raise ValueError("license_hmacs must contain 64-character hex values")
            if candidate not in seen:
                seen.add(candidate)
                normalized.append(candidate)
        if not normalized:
            raise ValueError("at least one license_hmac is required")
        return normalized


class ExtendRequest(LicenseTargets):
    days: int = Field(ge=1, le=3650, strict=True)


class RevokeRequest(LicenseTargets):
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


def _page_response(name: str, request: Request, context: dict) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context={
            **context,
            "csrf_token": csrf_token(),
            "current_path": request.url.path,
        },
    )
    response.headers.update(
        {
            "Cache-Control": "no-store, max-age=0",
            "Content-Security-Policy": (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            ),
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }
    )
    return response


def _json_response(content: dict, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    username: Annotated[str, Depends(verify_credentials)],
) -> HTMLResponse:
    now = int(time.time())
    return _page_response(
        "dashboard.html",
        request,
        {
            "stats": get_statistics(now=now),
            "heartbeat_distribution": get_heartbeat_distribution(now=now),
            "username": username,
        },
    )


@router.get("/licenses", response_class=HTMLResponse)
def licenses_page(
    request: Request,
    username: Annotated[str, Depends(verify_credentials)],
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
    filter_status: Annotated[LicenseFilter | None, Query(alias="filter")] = None,
    sort: Annotated[SortField, Query()] = "last_seen_at",
    order: Annotated[SortOrder, Query()] = "desc",
) -> HTMLResponse:
    per_page = 50
    now = int(time.time())
    licenses, total = get_all_licenses(
        page=page,
        per_page=per_page,
        order_by=sort,
        order=order,
        filter_status=filter_status,
        now=now,
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        raise HTTPException(status_code=404, detail="Page not found")

    for license_row in licenses:
        # 计算激活时长
        first_seen = license_row["first_seen_at"]
        if first_seen:
            activation_seconds = now - first_seen
            if activation_seconds < 3600:
                license_row["activation_duration"] = f"{activation_seconds // 60} 分钟"
            elif activation_seconds < 86400:
                license_row["activation_duration"] = f"{activation_seconds // 3600} 小时"
            else:
                license_row["activation_duration"] = f"{activation_seconds // 86400} 天"
        else:
            license_row["activation_duration"] = "-"

        # 计算到期剩余时间
        expires_at = license_row["expires_at"]
        if expires_at is None:
            license_row["expiry_state"] = "unset"
            license_row["days_label"] = "不修改"
        else:
            seconds_left = expires_at - now
            if seconds_left < 0:
                license_row["expiry_state"] = "expired"
                elapsed = -seconds_left
                if elapsed < 3600:
                    license_row["days_label"] = f"{elapsed // 60} 分钟"
                elif elapsed < 86400:
                    license_row["days_label"] = f"{elapsed // 3600} 小时"
                else:
                    license_row["days_label"] = f"{elapsed // 86400} 天"
            elif seconds_left < 3600:
                license_row["expiry_state"] = "expiring"
                license_row["days_label"] = f"{seconds_left // 60} 分钟"
            elif seconds_left < 86400:
                license_row["expiry_state"] = "expiring"
                license_row["days_label"] = f"{seconds_left // 3600} 小时"
            else:
                license_row["expiry_state"] = (
                    "expiring" if seconds_left <= 30 * 86400 else "active"
                )
                license_row["days_label"] = f"{seconds_left // 86400} 天"

    page_start = max(1, page - 2)
    page_end = min(total_pages, page + 2)
    return _page_response(
        "licenses.html",
        request,
        {
            "licenses": licenses,
            "page": page,
            "page_numbers": range(page_start, page_end + 1),
            "total_pages": total_pages,
            "total": total,
            "filter": filter_status,
            "sort": sort,
            "order": order,
            "username": username,
        },
    )


@router.post("/licenses/extend")
def extend_license(
    request: Request,
    body: ExtendRequest,
    username: Annotated[str, Depends(verify_credentials)],
) -> JSONResponse:
    verify_csrf(request)
    result = extend_license_expiry(body.license_hmacs, body.days)
    if result.not_found:
        logger.warning(
            "admin_extend rejected operator=%s targets=%d not_found=%d",
            username,
            len(body.license_hmacs),
            result.not_found,
        )
        return _json_response(
            {"ok": False, "error": "license_not_found", "not_found": result.not_found},
            status_code=404,
        )

    logger.info(
        "admin_extend operator=%s targets=%d days=%d updated=%d skipped_unbounded=%d",
        username,
        len(body.license_hmacs),
        body.days,
        result.changed,
        result.skipped,
    )
    return _json_response(
        {
            "ok": True,
            "updated": result.changed,
            "skipped_unbounded": result.skipped,
        }
    )


@router.post("/licenses/revoke")
def revoke_license(
    request: Request,
    body: RevokeRequest,
    username: Annotated[str, Depends(verify_credentials)],
) -> JSONResponse:
    verify_csrf(request)
    result = revoke_licenses(body.license_hmacs, body.reason)
    if result.not_found:
        logger.warning(
            "admin_revoke rejected operator=%s targets=%d not_found=%d",
            username,
            len(body.license_hmacs),
            result.not_found,
        )
        return _json_response(
            {"ok": False, "error": "license_not_found", "not_found": result.not_found},
            status_code=404,
        )

    logger.info(
        "admin_revoke operator=%s targets=%d revoked=%d already_revoked=%d",
        username,
        len(body.license_hmacs),
        result.changed,
        result.skipped,
    )
    return _json_response(
        {"ok": True, "revoked": result.changed, "already_revoked": result.skipped}
    )


@router.get("/revoked", response_class=HTMLResponse)
def revoked_page(
    request: Request,
    username: Annotated[str, Depends(verify_credentials)],
) -> HTMLResponse:
    return _page_response(
        "revoked.html",
        request,
        {"revoked": get_revoked_licenses(), "username": username},
    )
