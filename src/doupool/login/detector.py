from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


ACCOUNT_INFO_URL = "https://www.doubao.com/passport/web/account/info/"


@dataclass(frozen=True, slots=True)
class ResponseMeta:
    url: str
    status: int
    method: str


@dataclass(frozen=True, slots=True)
class DoubaoIdentity:
    user_id: str
    nickname: str | None = None

    def as_mapping(self) -> dict[str, str | None]:
        return {"user_id": self.user_id, "nickname": self.nickname}


class DoubaoLoginDetector:
    PATH_HINTS = (
        "/passport/web/login/",
        "/passport/web/scan/",
        "/passport/web/account/",
        "/passport/web/check_qrconnect/",
        "/alice/user/launch",
    )

    def observe(self, response: ResponseMeta) -> bool:
        parsed = urlparse(response.url)
        return (
            parsed.hostname is not None
            and parsed.hostname.endswith("doubao.com")
            and response.status == 200
            and any(hint in parsed.path for hint in self.PATH_HINTS)
        )

    def identity_from_response(
        self, response: ResponseMeta, payload: object
    ) -> DoubaoIdentity | None:
        if response.status != 200 or not isinstance(payload, dict):
            return None
        path = urlparse(response.url).path
        data = payload.get("data")
        if not isinstance(data, dict):
            return None

        if path == "/passport/web/check_qrconnect/":
            if data.get("status") != "confirmed":
                return None
            user = data.get("user_data")
            if not isinstance(user, dict):
                return None
            user_id = user.get("user_id_str") or user.get("user_id") or user.get("sec_user_id")
            if not user_id:
                return None
            nickname = user.get("name") or user.get("screen_name")
            return DoubaoIdentity(str(user_id), str(nickname) if nickname else None)

        if path == "/alice/user/launch":
            extra = data.get("extra")
            is_login = isinstance(extra, dict) and str(extra.get("is_login")) == "1"
            user_id = data.get("sec_user_id")
            if is_login and user_id:
                return DoubaoIdentity(str(user_id))
        return None

    def verify(self, page_or_context) -> DoubaoIdentity | None:
        """
        用 cookie 直接调 /passport/web/account/info/ 验证是否已登录。

        接受 page 或 context:登录成功后 doubao 前端常常会做 navigation,把
        原始 page 关掉,但 context 还在(cookie 已经写入持久化 profile)。
        这种情况用 context.request.get() 仍能验证身份,这就是 wait_for_identity
        的 fallback 路径。
        """
        request_api = getattr(page_or_context, "context", page_or_context).request
        try:
            response = request_api.get(ACCOUNT_INFO_URL)
        except Exception:
            return None
        if not response.ok or response.status != 200:
            return None
        try:
            payload = response.json()
        except Exception:
            return None
        if payload.get("code") not in (0, None):
            return None
        data = payload.get("data") or {}
        user = data.get("user") or {}
        user_id = user.get("user_id") or data.get("user_id") or data.get("id")
        if not user_id:
            return None
        return DoubaoIdentity(str(user_id), user.get("name") or data.get("name"))
