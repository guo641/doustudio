from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.app.storage import db
from server.app.web import routes
from server.app.web.auth import csrf_token


ADMIN_USER = "admin"
ADMIN_PASSWORD = "correct-horse-battery-staple"
LICENSE_HMAC = "a" * 64
NOWISH = int(time.time())


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    db_path = tmp_path / "license.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    monkeypatch.setenv("DOUSTUDIO_ADMIN_USER", ADMIN_USER)
    monkeypatch.setenv("DOUSTUDIO_ADMIN_PASSWORD", ADMIN_PASSWORD)

    app = FastAPI()
    app.include_router(routes.router)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    with TestClient(app) as client:
        yield client, db_path


def _auth(user: str = ADMIN_USER, password: str = ADMIN_PASSWORD):
    return (user, password)


def _csrf_headers(*, origin: str = "http://testserver") -> dict[str, str]:
    return {"X-DouStudio-CSRF": csrf_token(), "Origin": origin}


def _insert_license(
    db_path,
    *,
    license_hmac: str = LICENSE_HMAC,
    fingerprint: str = "sensitive-fingerprint-value",
    expires_at: int | None = NOWISH + 86400,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO license_activations (
                license_hmac, fingerprint_hex, expires_at, first_seen_at,
                last_seen_at, heartbeat_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (license_hmac, fingerprint, expires_at, NOWISH - 100, NOWISH, 3),
        )


def test_admin_auth_fails_closed_but_health_remains_public(web_client, monkeypatch):
    client, _ = web_client
    monkeypatch.delenv("DOUSTUDIO_ADMIN_PASSWORD")

    assert client.get("/healthz").json() == {"ok": True}
    response = client.get("/admin/", auth=_auth())
    assert response.status_code == 503
    assert response.json()["detail"] == "Admin interface is not configured"


def test_admin_requires_correct_basic_auth_and_sets_security_headers(web_client):
    client, _ = web_client

    missing = client.get("/admin/")
    wrong = client.get("/admin/", auth=_auth(password="wrong"))
    correct = client.get("/admin/", auth=_auth())

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Basic")
    assert wrong.status_code == 401
    assert correct.status_code == 200
    assert "运行概览" in correct.text
    assert correct.headers["cache-control"].startswith("no-store")
    assert correct.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in correct.headers["content-security-policy"]


def test_dashboard_and_license_page_render_without_fingerprint_leak(web_client):
    client, db_path = web_client
    _insert_license(db_path)

    dashboard = client.get("/admin/", auth=_auth())
    listing = client.get(
        "/admin/licenses?filter=active_24h&sort=heartbeat_count&order=desc",
        auth=_auth(),
    )

    assert dashboard.status_code == 200
    assert "历史激活总数" in dashboard.text
    assert listing.status_code == 200
    assert LICENSE_HMAC[:16] in listing.text
    assert "sensitive-fingerprint-value" not in listing.text
    assert "fingerprint_hex" not in listing.text
    assert 'meta name="doustudio-csrf"' in listing.text


def test_revoked_page_escapes_untrusted_reason_and_operator(web_client, monkeypatch):
    client, db_path = web_client
    malicious = '<img src=x onerror="alert(1)">'
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO revoked_license_hmac_prefixes VALUES (?, ?, ?)",
            ("b" * 16, NOWISH, malicious),
        )
    monkeypatch.setenv("DOUSTUDIO_ADMIN_USER", "admin<script>")

    response = client.get(
        "/admin/revoked", auth=_auth(user="admin<script>")
    )

    assert response.status_code == 200
    assert malicious not in response.text
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in response.text
    assert "操作员 admin&lt;script&gt;" in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/admin/licenses?page=0",
        "/admin/licenses?page=100001",
        "/admin/licenses?filter=unknown",
        "/admin/licenses?sort=license_hmac",
        "/admin/licenses?order=sideways",
    ],
)
def test_license_page_rejects_invalid_query_values(web_client, path):
    client, _ = web_client
    assert client.get(path, auth=_auth()).status_code == 422


def test_license_page_rejects_out_of_range_page(web_client):
    client, db_path = web_client
    _insert_license(db_path)

    response = client.get("/admin/licenses?page=2", auth=_auth())

    assert response.status_code == 404


@pytest.mark.parametrize(
    "path, body",
    [
        ("/admin/licenses/extend", {"license_hmacs": ["bad"], "days": 1}),
        ("/admin/licenses/extend", {"license_hmacs": [LICENSE_HMAC], "days": 0}),
        ("/admin/licenses/extend", {"license_hmacs": [LICENSE_HMAC], "days": True}),
        ("/admin/licenses/extend", {"license_hmacs": [LICENSE_HMAC], "days": "365"}),
        ("/admin/licenses/extend", {"license_hmacs": [LICENSE_HMAC], "days": 3651}),
        ("/admin/licenses/revoke", {"license_hmacs": [LICENSE_HMAC], "reason": " "}),
        ("/admin/licenses/revoke", {"license_hmacs": [], "reason": "test"}),
    ],
)
def test_mutation_routes_validate_json_body(web_client, path, body):
    client, _ = web_client
    response = client.post(
        path, auth=_auth(), headers=_csrf_headers(), json=body
    )
    assert response.status_code == 422


def test_mutation_requires_token_and_same_origin(web_client):
    client, db_path = web_client
    _insert_license(db_path)
    body = {"license_hmacs": [LICENSE_HMAC], "days": 1}

    no_token = client.post(
        "/admin/licenses/extend",
        auth=_auth(),
        headers={"Origin": "http://testserver"},
        json=body,
    )
    no_origin = client.post(
        "/admin/licenses/extend",
        auth=_auth(),
        headers={"X-DouStudio-CSRF": csrf_token()},
        json=body,
    )
    cross_origin = client.post(
        "/admin/licenses/extend",
        auth=_auth(),
        headers=_csrf_headers(origin="https://attacker.invalid"),
        json=body,
    )

    assert no_token.status_code == 403
    assert no_origin.status_code == 403
    assert cross_origin.status_code == 403


def test_extend_route_updates_finite_license(web_client):
    client, db_path = web_client
    _insert_license(db_path, expires_at=NOWISH + 50)

    response = client.post(
        "/admin/licenses/extend",
        auth=_auth(),
        headers=_csrf_headers(),
        json={"license_hmacs": [LICENSE_HMAC], "days": 2},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "updated": 1,
        "skipped_unbounded": 0,
    }
    with sqlite3.connect(db_path) as conn:
        expiry = conn.execute(
            "SELECT expires_at FROM license_activations WHERE license_hmac = ?",
            (LICENSE_HMAC,),
        ).fetchone()[0]
    assert expiry == NOWISH + 50 + 2 * 86400


def test_unknown_mutation_target_returns_404(web_client):
    client, _ = web_client
    response = client.post(
        "/admin/licenses/extend",
        auth=_auth(),
        headers=_csrf_headers(),
        json={"license_hmacs": ["f" * 64], "days": 1},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "license_not_found"


def test_revoke_route_is_idempotent(web_client):
    client, db_path = web_client
    _insert_license(db_path)
    request_kwargs = {
        "auth": _auth(),
        "headers": _csrf_headers(),
        "json": {"license_hmacs": [LICENSE_HMAC], "reason": "smoke test"},
    }

    first = client.post("/admin/licenses/revoke", **request_kwargs)
    second = client.post("/admin/licenses/revoke", **request_kwargs)

    assert first.status_code == 200
    assert first.json() == {"ok": True, "revoked": 1, "already_revoked": 0}
    assert second.status_code == 200
    assert second.json() == {"ok": True, "revoked": 0, "already_revoked": 1}
