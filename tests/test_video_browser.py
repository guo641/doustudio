from doupool.video.browser import (
    AISPACE_SCRIPT,
    CHAIN_SCRIPT,
    COMPLETION_SCRIPT,
    UPLOAD_IMAGE_SCRIPT,
    read_browser_fingerprint,
)


class FakePage:
    def __init__(self):
        self.expression = None
        self.timeout = None

    def wait_for_function(self, expression, timeout):
        self.expression = expression
        self.timeout = timeout


class FakeContext:
    def cookies(self, urls):
        assert urls == ["https://www.doubao.com"]
        return [{"name": "s_v_web_id", "value": "verify_current_fp"}]


def test_read_browser_fingerprint_uses_current_tea_key_and_fingerprint_cookie():
    page = FakePage()

    fingerprint = read_browser_fingerprint(page, FakeContext())

    assert "__tea_cache_tokens_497858" in page.expression
    assert "__tea_cache_tokens_2018" not in page.expression
    assert page.timeout == 15_000
    assert fingerprint == "verify_current_fp"


def test_page_requests_use_current_fingerprint_and_web_id_sources():
    assert "fp:payload.ext.fp" in COMPLETION_SCRIPT
    assert "web_id:tea.web_id" in COMPLETION_SCRIPT
    for script in (CHAIN_SCRIPT, AISPACE_SCRIPT):
        assert "s_v_web_id" in script
        assert "web_id:tea.web_id" in script
        assert "__tea_cache_tokens_2018" not in script


def test_upload_script_covers_i2v_pipeline():
    for marker in (
        "/alice/resource/prepare_upload",
        "ApplyImageUpload",
        "CommitImageUpload",
        "/alice/message/pre_handle_v2_without_conv",
        "resource_type:2",
        "entity_type:2",
    ):
        assert marker in UPLOAD_IMAGE_SCRIPT
