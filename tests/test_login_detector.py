from doupool.login.detector import DoubaoLoginDetector, ResponseMeta


class FakeResponse:
    ok = True
    status = 200

    def json(self):
        return {"data": {"user": {"user_id": "u-1", "name": "莲韵"}}, "code": 0}


class FakeRequest:
    def get(self, url):
        assert url.endswith("/passport/web/account/info/")
        return FakeResponse()


class FakeContext:
    request = FakeRequest()


class FakePage:
    context = FakeContext()


def test_login_response_triggers_verification():
    detector = DoubaoLoginDetector()
    assert detector.observe(
        ResponseMeta(
            url="https://www.doubao.com/passport/web/login/confirm/",
            status=200,
            method="POST",
        )
    )


def test_identity_requires_nonempty_user_id():
    identity = DoubaoLoginDetector().verify(FakePage())
    assert identity.user_id == "u-1"
    assert identity.nickname == "莲韵"


def test_confirmed_qr_response_returns_identity():
    detector = DoubaoLoginDetector()
    meta = ResponseMeta(
        url="https://www.doubao.com/passport/web/check_qrconnect/",
        status=200,
        method="GET",
    )
    payload = {
        "message": "success",
        "data": {
            "status": "confirmed",
            "user_data": {
                "user_id_str": "123456",
                "name": "莲韵",
                "screen_name": "莲韵",
            },
        },
    }

    identity = detector.identity_from_response(meta, payload)

    assert identity.user_id == "123456"
    assert identity.nickname == "莲韵"


def test_scanned_qr_response_is_not_login_success():
    detector = DoubaoLoginDetector()
    meta = ResponseMeta(
        url="https://www.doubao.com/passport/web/check_qrconnect/",
        status=200,
        method="GET",
    )
    assert detector.identity_from_response(meta, {"data": {"status": "scanned"}}) is None


def test_user_launch_is_post_login_fallback():
    detector = DoubaoLoginDetector()
    meta = ResponseMeta(
        url="https://www.doubao.com/alice/user/launch",
        status=200,
        method="POST",
    )
    payload = {"code": 0, "data": {"sec_user_id": "sec-1", "extra": {"is_login": "1"}}}
    assert detector.identity_from_response(meta, payload).user_id == "sec-1"


def test_account_info_response_yields_identity():
    """doubao 登录成功后前端会主动调 /passport/web/account/info/ 拿昵称,
    identity_from_response 必须能从中提取 user_id,否则 identity_ready 永远不 set"""
    detector = DoubaoLoginDetector()
    meta = ResponseMeta(
        url="https://www.doubao.com/passport/web/account/info/",
        status=200,
        method="GET",
    )
    payload = {
        "code": 0,
        "data": {
            "user": {
                "user_id": "3830030044314",
                "name": "用户3830030044314",
            },
        },
    }
    identity = detector.identity_from_response(meta, payload)
    assert identity is not None
    assert identity.user_id == "3830030044314"
    assert identity.nickname == "用户3830030044314"


def test_account_info_handles_flat_user_id_field():
    """有些版本 data 下直接放 user_id,不在 user 子对象里"""
    detector = DoubaoLoginDetector()
    meta = ResponseMeta(
        url="https://www.doubao.com/passport/web/account/info/",
        status=200,
        method="GET",
    )
    payload = {"code": 0, "data": {"user_id": "u-flat", "name": "扁平昵称"}}
    identity = detector.identity_from_response(meta, payload)
    assert identity is not None
    assert identity.user_id == "u-flat"
    assert identity.nickname == "扁平昵称"


def test_account_info_without_user_id_returns_none():
    """account/info 返回但 data 没 user_id(未登录)→ None,不假装登录"""
    detector = DoubaoLoginDetector()
    meta = ResponseMeta(
        url="https://www.doubao.com/passport/web/account/info/",
        status=200,
        method="GET",
    )
    assert detector.identity_from_response(meta, {"code": 0, "data": {}}) is None
