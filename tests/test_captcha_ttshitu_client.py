"""tests/test_captcha_ttshitu_client.py

图鉴 HTTP client 的单测。用 httpx.MockTransport,不发真网络。
覆盖:
  - 成功响应(typeid=27 多点 / typeid=33 单点)
  - 失败响应:网络错 / http 错 / 鉴权错 / json 解析错 / 空 points
  - TtshituDisabled 凭证不可用
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from doupool.captcha.config import CaptchaCredentials
from doupool.captcha.ttshitu_client import (
    TtshituCaptchaClient,
    TtshituError,
    TtshituDisabled,
    TYPEID_COORDINATE_1_4,
    TYPEID_SINGLE_GAP,
)


CREDS = CaptchaCredentials(username="u", password="p", enabled=True)


def _png_bytes() -> bytes:
    # 1x1 transparent png
    return base64.b64decode(
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkAAIAAAoAAv/lxKUAAAAASUVORK5CYII="
    )


def _ok_multi_points() -> dict:
    return {
        "code": 0,
        "msg": "success",
        "data": {"result": "120,45|230,80", "id": "abc"},
    }


def _ok_single_point() -> dict:
    return {
        "code": 0,
        "msg": "success",
        "data": {"result": "300,200", "id": "def"},
    }


def _auth_fail() -> dict:
    return {"code": "1001", "msg": "用户不存在"}


def _empty_points() -> dict:
    return {"code": 0, "msg": "success", "data": {"result": "", "id": "g"}}


def _make_client(handler) -> TtshituCaptchaClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(timeout=5, transport=transport)
    return TtshituCaptchaClient(CREDS, http_client=http)


def test_disabled_raises():
    with pytest.raises(TtshituDisabled):
        TtshituCaptchaClient(CaptchaCredentials(username="", password="", enabled=False))


def test_disabled_with_partial_creds():
    with pytest.raises(TtshituDisabled):
        TtshituCaptchaClient(CaptchaCredentials(username="u", password="", enabled=True))


def test_solve_multi_points():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=_ok_multi_points())

    client = _make_client(handler)
    solve = client.solve_image(_png_bytes(), typeid=TYPEID_COORDINATE_1_4)
    try:
        assert len(solve.points) == 2
        assert solve.points[0] == (120, 45)
        assert solve.points[1] == (230, 80)
        assert solve.typeid == TYPEID_COORDINATE_1_4
        assert solve.primary == (120, 45)
        # 发的就是 base64 + username + typeid
        assert captured["body"]["username"] == "u"
        assert captured["body"]["typeid"] == TYPEID_COORDINATE_1_4
        assert isinstance(captured["body"]["image"], str)
        assert len(captured["body"]["image"]) > 50
    finally:
        client.close()


def test_solve_single_gap():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_single_point())

    client = _make_client(handler)
    solve = client.solve_image(_png_bytes(), typeid=TYPEID_SINGLE_GAP)
    try:
        assert solve.points == [(300, 200)]
        assert solve.typeid == TYPEID_SINGLE_GAP
    finally:
        client.close()


def test_auth_fail_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_auth_fail())

    client = _make_client(handler)
    try:
        with pytest.raises(TtshituError) as ei:
            client.solve_image(_png_bytes())
        assert "用户" in str(ei.value) or "code=" in str(ei.value)
        assert ei.value.raw == _auth_fail()
    finally:
        client.close()


def test_http_500_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="bad gateway")

    client = _make_client(handler)
    try:
        with pytest.raises(TtshituError) as ei:
            client.solve_image(_png_bytes())
        assert "503" in str(ei.value)
    finally:
        client.close()


def test_invalid_json_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = _make_client(handler)
    try:
        with pytest.raises(TtshituError) as ei:
            client.solve_image(_png_bytes())
        assert "json" in str(ei.value)
    finally:
        client.close()


def test_network_error_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail")

    client = _make_client(handler)
    try:
        with pytest.raises(TtshituError) as ei:
            client.solve_image(_png_bytes())
        assert "network" in str(ei.value)
    finally:
        client.close()


def test_empty_points_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_empty_points())

    client = _make_client(handler)
    try:
        with pytest.raises(TtshituError) as ei:
            client.solve_image(_png_bytes())
        assert "empty" in str(ei.value)
    finally:
        client.close()


def test_empty_png_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_multi_points())

    client = _make_client(handler)
    try:
        with pytest.raises(TtshituError) as ei:
            client.solve_image(b"")
        assert "empty" in str(ei.value)
    finally:
        client.close()


def test_legacy_point_list_format():
    """旧版返回 data.point = [{x,y}, ...] 也要能解。"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "success": True,
            "data": {"point": [{"x": 50, "y": 60}, {"x": 100, "y": 200}]},
        })

    client = _make_client(handler)
    try:
        solve = client.solve_image(_png_bytes())
        assert solve.points == [(50, 60), (100, 200)]
    finally:
        client.close()


def test_data_is_string_format():
    """data 直接是 '120,45|230,80' 的变体。"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": "10,20|30,40"})

    client = _make_client(handler)
    try:
        solve = client.solve_image(_png_bytes())
        assert solve.points == [(10, 20), (30, 40)]
    finally:
        client.close()


def test_cost_ms_recorded():
    def handler(req: httpx.Request) -> httpx.Response:
        import time
        time.sleep(0.05)
        return httpx.Response(200, json=_ok_multi_points())

    client = _make_client(handler)
    try:
        solve = client.solve_image(_png_bytes())
        assert solve.cost_ms >= 30
    finally:
        client.close()