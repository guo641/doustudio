from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from .detector import DoubaoIdentity
from .service import VerifiedLogin

_LOG = logging.getLogger("doupool.login")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


# v0.2.7:沿 yaonieyo/doubao-account-pool 双轨判定,完全抛弃
# /passport/web/account/info/ 调用 —— 字节系 aegis 风控会把浏览器外任何
# 客户端(包括 page.evaluate 内的 fetch)判为指纹不合法,返回 1011。
#
# v0.2.8 重大补丁:加 sessionid cookie 闸门 + 最小冷却。
# v0.2.7 上线后用户报"点添加账号,浏览器窗口刚打开就自动关闭,状态显示已
# 登录但没扫码"。日志显示 4 次假阳性都在 1.4-2.0 秒内命中 identity。
# 根因:字节系前端 JS 在**首次访问** doubao.com 时,就会在
# localStorage.__tea_cache_tokens_497858 写入 19 位 tracking ID
# (user_unique_id 字段)。这是 byteDance 全站通用的 analytics token,
# 与登录无关,v0.2.7 的 _read_user_unique_id_from_page 无脑读这个字段
# 就把它当成真 user_id 接受了。
#
# sessionid cookie 是字节系登录凭证:**只有服务端响应才会下发,首访不存在**。
# 日志中所有 v0.2.6/v0.2.5 真登录(scan QR 后)的 sessionid 都是
# 32 字符十六进制格式,无例外。我们现在用它作硬闸门:
#   Tier 1: context.cookies() 有 doubao.com cookie(可能含 tracking)
#   Tier 2: page.evaluate 读 DOM innerText 不含登录关键词
#   闸门 1: 主循环起 N 秒内不接收任何 identity(给 page.goto + tracking 写入留时间)
#   闸门 2: sessionid cookie 必须存在且格式合法(否则视为 tracking)
#   user_id: localStorage.__tea_cache_tokens_497858.user_unique_id
#
# 视频流程安全不受影响(video/browser.py 把 user_unique_id 当 route metadata,
# 真登录靠 persistent profile 里的 sessionid cookie 单独保证)。
#
# Playwright sync API 必须在创建 sync_playwright() 的同一 OS thread 内调用
# (gevent 实现,跨线程会触发 "Cannot switch to a different thread")。所有
# page.evaluate / context.cookies() 都走 owner thread,不需要 daemon 线程。
GRACE_PERIOD_SECONDS = 6.0  # 页面全部关闭后的等待窗口(给新 page / cookie 生效)
LOOP_TICK_MS = 250  # 主循环 sleep 间隔(同时 pump Playwright 事件)
VERIFY_RETRY_BACKOFF = 0.3  # user_id 重读退避
USER_ID_LOCALSTORAGE_KEY = "__tea_cache_tokens_497858"

# v0.2.8:sessionid 闸门常量。
# byteDance 登录凭证 sessionid(以及带 _ss 后缀的 secure-session 版本)是
# 32 字符十六进制。日志 v0.2.6 真登录会话里全部命中,如
#   sessionid=ded34fe0089ace6b55d2701b52d7cabb
#   sessionid=5db19d22654e3bdca3e333a8fe804156
# tracking cookies(s_v_web_id/odin_tt/ttwid/n_mh)首访即下发但没有这个格式。
#
# v0.2.8.1:升级成模块公共符号 —— service._verify_from_disk 也需要同一组
# 规则做 disk fallback 闸门,避免在两个文件里各写一遍正则导致漂移。
SESSIONID_NAME_HINTS = ("sessionid", "sessionid_ss")
SESSIONID_VALUE_PATTERN = re.compile(r"^[0-9a-f]{32}$")

# v0.2.8:主循环最小冷却。page.goto(domcontentloaded)+ tracking ID 写入
# 最快 ~1.5 秒(假阳性窗口);真 QR 扫码确认 ≥3 秒才发生。这里设 3 秒
# 是给前端留点缓冲,让"刚刚打开浏览器"这个阶段完全被排除。
_MIN_LOOP_SECONDS_BEFORE_IDENTITY = 3.0

# DOM 关键词集合 —— 任一命中即视为未登录。
# 集中维护:doubao 文案改了改这里一处。
LOGGED_OUT_KEYWORDS = (
    "扫码登录",   # 桌面端 QR 入口
    "手机号登录",  # 切换手机号 tab
    "验证码登录",  # 切换验证码 tab
    "登录/注册",  # 顶部 CTA
    "扫码",       # 通用兜底,会被 "扫码登录" 命中,留作未来兼容
    "登录",       # 兜底,但会跟"已登录用户xxx"误伤 —— 我们用 not any(...) 判定
)

# cookie 兜底:字节通常不把 user_id 放进 cookie,命中率低,留作最后一道防线。
_USER_ID_COOKIE_HINTS = ("user_unique_id", "user_id", "uid")


def _is_doubao_cookie(cookie: dict) -> bool:
    """判断 cookie 是否来自 doubao 域(对标 yaonieyo cookies.filter)。"""
    return "doubao.com" in (cookie.get("domain") or "")


def _context_is_alive(context) -> bool:
    """Playwright 1.54+ 提供 is_closed(),用于 TOCTOU 提示
    (调用外层仍必须 try/except,is_closed 和真正调用之间还有时间窗)。"""
    try:
        return not context.is_closed()
    except PlaywrightError:
        return False


def _page_is_alive(page) -> bool:
    try:
        return not page.is_closed()
    except PlaywrightError:
        return False


def _context_doubao_cookie_count(context) -> int:
    """v0.2.7 Tier 1:返回 context 内 doubao.com 域非空 cookie 数量
    (对标 yaonieyo electron/main.ts:303-314 `cookies.length > 0`)。"""
    if not _context_is_alive(context):
        return 0
    try:
        cookies = context.cookies()
    except PlaywrightError as exc:
        _LOG.debug("context.cookies() 不可用: %s", exc)
        return 0
    return sum(1 for c in cookies if _is_doubao_cookie(c) and c.get("value"))


def _page_looks_logged_in(page) -> bool:
    """v0.2.7 Tier 2:DOM 文本不含登录关键词 → 视为已登录
    (对标 yaonieyo electron/executor.ts:326-334 looksLoggedOut)。

    注意:由于 "登录" 是 "登录/注册" 的子串,完整关键词集合仍可命中,
    代价是已登录用户昵称若含"登录"二字会被误判。我们没找到 doubao 实际
    出现这种情况的证据,如有可在 LOGGED_OUT_KEYWORDS 调整。
    """
    if not _page_is_alive(page):
        return False
    try:
        text = page.evaluate(
            "() => document.body ? (document.body.innerText || '') : ''"
        )
    except PlaywrightError as exc:
        _LOG.debug("读 DOM innerText 失败: %s", exc)
        return False
    if not isinstance(text, str):
        return False
    return not any(keyword in text for keyword in LOGGED_OUT_KEYWORDS)


def _read_user_unique_id_from_page(page) -> str | None:
    """v0.2.7:从 Chromium 进程内读 localStorage.__tea_cache_tokens_497858。

    本项目 src/doupool/video/browser.py:28,33-34 已验证:
      - tea.user_unique_id 是字节系 web 通用 user_id
      - 兜底 tea.web_id
    完全在浏览器进程内读,无 aegis 风险。
    """
    if not _page_is_alive(page):
        return None
    try:
        payload = page.evaluate(
            """
            () => {
              try {
                const raw = localStorage.getItem('__tea_cache_tokens_497858');
                if (!raw) return { ok: false };
                const tea = JSON.parse(raw);
                if (tea && typeof tea === 'object') {
                  if (tea.user_unique_id) return { ok: true, value: String(tea.user_unique_id) };
                  if (tea.web_id)         return { ok: true, value: String(tea.web_id) };
                }
              } catch (e) { /* JSON parse fail */ }
              return { ok: false };
            }
            """
        )
    except PlaywrightError as exc:
        _LOG.debug("读 user_unique_id 失败: %s", exc)
        return None
    if isinstance(payload, dict) and payload.get("ok"):
        value = payload.get("value")
        return str(value) if value else None
    return None


def _extract_user_id_from_cookies(cookies: list[dict]) -> str | None:
    """从 doubao.com 域 cookie 列表中按 hint 顺序找 user_id。
    字节通常不把 user_id 放进 cookie,这是兜底链最末一环。"""
    for hint in _USER_ID_COOKIE_HINTS:
        for c in cookies:
            if c.get("name") == hint and c.get("value"):
                return str(c["value"])
    return None


def _has_valid_sessionid(context) -> bool:
    """v0.2.8:登录凭证闸门。返回 True 当 context 内任意 doubao.com 域
    cookie 名字是 sessionid / sessionid_ss 且值是 32-char hex。

    这是字节系唯一可信的登录证据 —— tracking cookies + localStorage
    tracking ID 都不算。日志中所有 v0.2.6 真登录会话都满足这个格式,
    无例外。若字节系未来改 sessionid 格式,改 SESSIONID_VALUE_PATTERN
    一行即可。
    """
    if not _context_is_alive(context):
        return False
    try:
        cookies = context.cookies()
    except PlaywrightError:
        return False
    for c in cookies:
        if not _is_doubao_cookie(c):
            continue
        name = c.get("name", "")
        value = c.get("value", "")
        if name in SESSIONID_NAME_HINTS and SESSIONID_VALUE_PATTERN.match(value or ""):
            return True
    return False


def _sessionid_cookie_value(context) -> str | None:
    """v0.2.8:调试 / 日志用 —— 返回第一个匹配的 sessionid 值,无则 None。
    不抛错(与 _has_valid_sessionid 行为一致)。
    """
    if not _context_is_alive(context):
        return None
    try:
        cookies = context.cookies()
    except PlaywrightError:
        return None
    for c in cookies:
        if not _is_doubao_cookie(c):
            continue
        name = c.get("name", "")
        value = c.get("value", "")
        if name in SESSIONID_NAME_HINTS and SESSIONID_VALUE_PATTERN.match(value or ""):
            return value
    return None


def _iso_now() -> str:
    # v0.2.16:cookies.json debug 字段的时间戳也用北京时间,跟 DB 一致
    return datetime.now(_SHANGHAI).isoformat()


def _save_doubao_cookies_to_disk(context, profile_dir: Path) -> bool:
    """v0.2.7:抢救 doubao.com 域 cookie 到 profile_dir/cookies.json。
    后续 service 层 disk fallback 会读这个文件用 _extract_user_id_from_cookies 兜底。
    """
    if not _context_is_alive(context):
        return False
    try:
        all_cookies = context.cookies()
    except PlaywrightError as exc:
        _LOG.warning("抢救 cookies 失败(context.cookies): %s", exc)
        return False
    doubao_cookies = [
        {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "expires": c.get("expires", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": c.get("sameSite"),
        }
        for c in all_cookies
        if _is_doubao_cookie(c) and c.get("value")
    ]
    if not doubao_cookies:
        _LOG.info("抢救 cookies: 没有 doubao.com cookie,跳过")
        return False
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        target = profile_dir / "cookies.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(doubao_cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(target)
        _LOG.info("抢救 cookies 成功: %d 条 cookie 写到 %s",
                  len(doubao_cookies), target)
        return True
    except OSError as exc:
        _LOG.warning("抢救 cookies 写盘失败: %s", exc)
        return False


def _try_rescue_for_fallback(context, profile_dir: Path | None) -> None:
    """v0.2.7 抢救 helper:同时抢救 cookies 和 identity。
    v0.2.8 补丁:rescue 也走 sessionid 闸门 —— 不写 tracking ID 进
    identity.json,否则 service._verify_from_disk 兜底还会假阳性。

    - cookies 由 Chromium CookieMonster 持有,可直接 context.cookies() 取
    - identity(user_unique_id)必须 page.evaluate 读 localStorage
    - v0.2.8:_has_valid_sessionid(context) 为真才写 identity.json

    抢救对象是 service 层 disk fallback,只读 identity.json / cookies.json。
    context 已死 → 全失败,静默返回(不抛错,让上层正常处理)。
    """
    if profile_dir is None or not _context_is_alive(context):
        return
    _save_doubao_cookies_to_disk(context, profile_dir)
    # v0.2.8:sessionid 闸门 —— 没有 sessionid cookie 视为 tracking 态,
    # 不写 identity.json。cookies.json 仍写(供 service 调试 / 后续重试)。
    if not _has_valid_sessionid(context):
        _LOG.info("rescue: sessionid cookie 未下发,跳过 identity.json")
        return
    active_pages = [
        p for p in getattr(context, "pages", []) if _page_is_alive(p)
    ]
    if not active_pages:
        return
    user_id = _read_user_unique_id_from_page(active_pages[0])
    if not user_id:
        return
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        target = profile_dir / "identity.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"user_id": user_id, "rescued_at": _iso_now()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(target)
        _LOG.info(
            "rescue identity: user_unique_id=%s (sessionid=%s) → %s",
            user_id, _sessionid_cookie_value(context), target,
        )
    except OSError as exc:
        _LOG.warning("rescue identity 写盘失败: %s", exc)


class _LockCtx:
    """最小互斥:保护 identities 写入与 identity_ready.set()"""

    def __init__(self):
        self._lock = threading.Lock()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


def _lock_identities():
    return _LockCtx()


def _monotonic() -> float:
    import time
    return time.monotonic()


def wait_for_identity(
    pages_provider,
    identity_ready: threading.Event,
    identities: list[DoubaoIdentity],
    cancel_event: threading.Event,
    context=None,
    profile_dir: Path | None = None,
) -> DoubaoIdentity:
    """v0.2.7 重写主循环 + v0.2.8 加 sessionid 闸门 + 最小冷却。

    不再监听 on_response(被 Connection.cleanup() 短路吞),不再调
    /passport/web/account/info/(aegis 风控拒一切非浏览器指纹请求)。

    判定链(Tier 1 + Tier 2 + 闸门 + localStorage 四件套):
      1. context.cookies() 有 doubao.com cookie → 可能已登录
      2. active page 的 DOM innerText 不含登录关键词 → 真的已登录
      3. v0.2.8 闸门 1:主循环起 ≥_MIN_LOOP_SECONDS_BEFORE_IDENTITY 秒
         → 排除 page.goto + tracking ID 写入的假阳性窗口
      4. v0.2.8 闸门 2:_has_valid_sessionid(context) 为 True
         → sessionid cookie(32-hex)是字节系登录凭证,tracking 没有
      5. page.evaluate 读 localStorage.__tea_cache_tokens_497858.user_unique_id
         → 拿到 identity

    容错:
      - 每步独立 try/except
      - 所有 page 关闭后给 GRACE_PERIOD_SECONDS 时间等新 page / cookie 生效
      - 放弃前 _try_rescue_for_fallback 把 cookies + user_id 写盘,
        让 service 层 disk fallback 能用 identity.json / cookies.json 兜底
        (v0.2.8:rescue 也走 sessionid 闸门,不写 tracking ID 进 identity.json)
    """
    if context is None:
        raise RuntimeError("wait_for_identity 必须在 owner thread 调用且需要 context")

    last_user_id_attempt = 0.0
    page_closed_since: float | None = None
    loop_started_at = _monotonic()  # v0.2.8:主循环起跑时间,用于最小冷却

    while not cancel_event.is_set():
        # 1. 已经有 identity(其他路径设的)→ 立刻返回
        if identity_ready.is_set() and identities:
            return identities[0]

        # 2. context 已死 → 最后一次抢救后放弃
        if not _context_is_alive(context):
            _LOG.warning("wait_for_identity: context 已关闭,抢救后放弃")
            _try_rescue_for_fallback(context, profile_dir)
            if identities:
                return identities[0]
            raise RuntimeError("登录窗口已关闭")

        # 3. Tier 1: 有 doubao cookie?
        cookie_count = _context_doubao_cookie_count(context)
        has_cookie = cookie_count > 0

        # 4. Tier 2: 找 active page,DOM 探测
        active = list(pages_provider())
        any_alive = any(_page_is_alive(p) for p in active)
        looks_logged_in = False
        if any_alive and has_cookie:
            if _page_looks_logged_in(active[0]):
                looks_logged_in = True
            else:
                _LOG.info("cookie 有但 DOM 仍含登录关键词,继续等")

        # 5. localStorage user_id 探针(三道闸门:v0.2.8 新增 1 + 2)
        now = _monotonic()
        if looks_logged_in and (now - last_user_id_attempt) >= VERIFY_RETRY_BACKOFF:
            # 闸门 1:v0.2.8 最小冷却,排除 page 刚加载的 tracking ID 假阳性
            elapsed = now - loop_started_at
            if elapsed < _MIN_LOOP_SECONDS_BEFORE_IDENTITY:
                _LOG.debug(
                    "wait_for_identity: 主循环才过 %.1fs,小于最小冷却 %.1fs,继续等",
                    elapsed, _MIN_LOOP_SECONDS_BEFORE_IDENTITY,
                )
            # 闸门 2:v0.2.8 sessionid cookie 必须是 32-hex 才接受身份
            elif not _has_valid_sessionid(context):
                _LOG.debug(
                    "wait_for_identity: localStorage user_unique_id 有但 sessionid cookie "
                    "未下发(疑似 tracking ID),继续等"
                )
            else:
                # 闸门 3:实际读 user_id,接受 identity
                last_user_id_attempt = now
                user_id = _read_user_unique_id_from_page(active[0])
                if user_id:
                    identity = DoubaoIdentity(user_id=user_id, nickname=None)
                    with _lock_identities():
                        if not identities:
                            identities.append(identity)
                            identity_ready.set()
                    _LOG.info(
                        "wait_for_identity: 命中 identity user_id=%s sessionid=%s",
                        user_id, _sessionid_cookie_value(context),
                    )
                    return identities[0]

        # 6. grace period —— 全部 page 关闭后等新 page / cookie 生效
        if not any_alive:
            if page_closed_since is None:
                page_closed_since = now
                _LOG.info(
                    "wait_for_identity: 没有 active page,开始 grace period (%.1fs)",
                    GRACE_PERIOD_SECONDS,
                )
            elif (now - page_closed_since) >= GRACE_PERIOD_SECONDS:
                _LOG.warning(
                    "wait_for_identity: grace period 超时 (%.1fs),抢救 cookies+identity",
                    GRACE_PERIOD_SECONDS,
                )
                _try_rescue_for_fallback(context, profile_dir)
                if identities:
                    return identities[0]
                raise RuntimeError("登录窗口已关闭")
        else:
            if page_closed_since is not None:
                _LOG.info("wait_for_identity: 新 page 出现,重置 grace period")
            page_closed_since = None

        # 7. pump Playwright 事件
        try:
            if active and any_alive:
                active[0].wait_for_timeout(LOOP_TICK_MS)
            else:
                try:
                    context.wait_for_event("page", timeout=LOOP_TICK_MS / 1000.0)
                except PlaywrightError as exc:
                    _LOG.debug("context.wait_for_event('page') 失败: %s", exc)
        except PlaywrightError as exc:
            # page 在 sleep 中被关 —— 不 raise,下一轮重新检查
            _LOG.debug("wait_for_timeout 抛 PlaywrightError: %s", exc)

        # 8. pump 期间可能有外部线程设了 identity_ready
        if identity_ready.is_set() and identities:
            return identities[0]

    raise RuntimeError("登录已取消")


class PlaywrightLoginRunner:
    """v0.2.7:不再依赖 detector,直接走 yaonieyo 双轨判定。
    v0.2.20:扫码成功后 keepalive_seconds 秒内不关 context,
    让用户在那个浏览器窗口里访问 doubao.com/chat/ 生成 WebMSSDK token。
    """

    def __init__(self, keepalive_seconds: float = 30.0):
        self.keepalive_seconds = keepalive_seconds

    def run(self, attempt_id, profile_dir: Path, emit, cancel_event: threading.Event):
        identity_ready = threading.Event()
        identities: list[DoubaoIdentity] = []
        active_pages: list = []
        active_pages_lock = threading.Lock()

        def add_page(page):
            with active_pages_lock:
                if page not in active_pages:
                    active_pages.append(page)
                    _LOG.info("active page added: %s url=%s", id(page), page.url)

        def remove_page(page):
            with active_pages_lock:
                if page in active_pages:
                    active_pages.remove(page)
                    _LOG.info("active page removed: %s", id(page))

        def get_active():
            with active_pages_lock:
                return [p for p in active_pages if _page_is_alive(p)]

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
                viewport={"width": 1100, "height": 760},
            )

            context.on("page", add_page)

            initial_page = context.pages[0] if context.pages else context.new_page()
            add_page(initial_page)

            def _on_initial_page_close(_payload):
                remove_page(initial_page)
                _LOG.info("initial page closed,尝试抢救 cookies + identity")
                _try_rescue_for_fallback(context, profile_dir)

            initial_page.on("close", _on_initial_page_close)
            initial_page.on(
                "framenavigated",
                lambda _: _LOG.debug("initial page framenavigated url=%s", initial_page.url),
            )

            try:
                initial_page.goto(
                    "https://www.doubao.com/", wait_until="domcontentloaded"
                )
            except PlaywrightError as exc:
                raise RuntimeError(f"无法打开豆包登录页:{exc}") from exc
            emit("waiting_for_scan", "请在豆包窗口中扫码登录")

            try:
                identity = wait_for_identity(
                    get_active,
                    identity_ready,
                    identities,
                    cancel_event,
                    context=context,
                    profile_dir=profile_dir,
                )
                emit("verifying", "已检测到登录，正在确认账号")
                # v0.2.20:keepalive —— 保持浏览器窗口打开 N 秒,让用户
                # 在里面访问 doubao.com/chat/ 让 WebMSSDK 写入 leveldb。
                # 期间 Playwright 需要 pump 事件(用户操作/动画),所以
                # 主线程不能 sleep —— 用 cancel_event.wait(timeout) 阻塞,
                # Playwright sync API 在 wait 期间不主动 pump,但 context 是
                # 真实 Chromium 进程,JS / 渲染照样跑。需要 pump 时我们
                # 间隔调 active[0].wait_for_timeout(250)。
                if self.keepalive_seconds > 0 and not cancel_event.is_set():
                    _LOG.info(
                        "login keepalive: 浏览器保持打开 %s 秒,请访问 doubao.com/chat/",
                        self.keepalive_seconds,
                    )
                    end_at = _monotonic() + self.keepalive_seconds
                    while not cancel_event.is_set():
                        remaining = end_at - _monotonic()
                        if remaining <= 0:
                            break
                        # 250ms 一片,既 pump Playwright 事件,又能即时响应 cancel
                        active = get_active()
                        if active:
                            try:
                                active[0].wait_for_timeout(250)
                            except PlaywrightError:
                                # page 在 keepalive 期间被用户手关 —— 抢救
                                # cookies 后提前结束
                                _try_rescue_for_fallback(context, profile_dir)
                                break
                        else:
                            # 没有活动 page,context 自动 close 在即 —— 退出
                            break
                    _LOG.info("login keepalive: 窗口已结束")
                return VerifiedLogin(identity.as_mapping(), str(profile_dir))
            finally:
                _try_rescue_for_fallback(context, profile_dir)
                try:
                    context.close()
                except PlaywrightError:
                    pass