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
