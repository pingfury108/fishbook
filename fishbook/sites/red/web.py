"""小红书公共部分：SSR 导航、登录态判定、__INITIAL_STATE__ 解包。

小红书 web 的 search/feed API 需要 X-s/X-t/X-S-Common 三件套签名，且实测会被
风控判为「当前账号存在异常」(300011) 并降级 web_session。因此这里不调 API，
直接读页面自身的 SSR 数据（`window.__INITIAL_STATE__`），请求量与普通浏览一致。
"""

from __future__ import annotations

import time
from pathlib import Path

from ...cdp import CDP, is_transient
from ...errors import AuthError, CDPError, EndpointError
from ...util import log

JS_DIR = Path(__file__).resolve().parent

SITE_URL = "https://www.xiaohongshu.com/"
LOGIN_WALL = "登录后查看搜索结果"

UNWRAP_JS = (JS_DIR / "unwrap.js").read_text(encoding="utf-8")
READY_JS = "document.readyState === 'complete' && !!window.__INITIAL_STATE__"

# 登录态被风控降级后，web_session 会挪到 last_web_session
LOGIN_COOKIES = ("web_session",)
DEMOTED_COOKIES = ("last_web_session",)


def cookie_names(page: CDP) -> set[str]:
    cookies = page.send("Network.getCookies", {"urls": [SITE_URL]}).get("cookies") or []
    return {cookie.get("name") or "" for cookie in cookies}


def is_logged_in(page: CDP) -> bool:
    names = cookie_names(page)
    return all(name in names for name in LOGIN_COOKIES) and not any(
        name in names for name in DEMOTED_COOKIES
    )


def require_login(page: CDP) -> None:
    names = cookie_names(page)
    if any(name in names for name in DEMOTED_COOKIES):
        raise AuthError(
            "小红书登录态已被风控降级（cookie 出现 last_web_session），请在该浏览器里重新登录后再试"
        )
    if not all(name in names for name in LOGIN_COOKIES):
        raise AuthError("小红书未登录（缺少 web_session cookie），请在该浏览器里登录")


def goto(page: CDP, url: str, timeout: float = 15.0) -> None:
    """导航并等到 SSR state 可读；导航期间上下文销毁视为可重试。"""
    page.send("Page.navigate", {"url": url})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if page.evaluate(READY_JS, retries=2):
                return
        except CDPError as exc:
            if not is_transient(exc):
                raise
        time.sleep(0.3)
    raise EndpointError(f"小红书页面未就绪（{timeout:.0f}s）: {url}")


def evaluate_state(page: CDP, js: str) -> dict:
    """执行一段解包 JS，统一处理返回结构异常。"""
    result = page.evaluate(f"(() => {{ {UNWRAP_JS} {js} }})()", retries=2)
    if not isinstance(result, dict):
        raise EndpointError(f"页面返回了非对象结果: {result!r}")
    if result.get("error"):
        log(f"页面状态异常: {result['error']}")
    return result
