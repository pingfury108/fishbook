"""小红书公共部分：导航/就绪轮询、登录态判定。"""

from __future__ import annotations

import random
import time

from ...cdp import CDP, is_transient
from ...errors import AuthError, CDPError, StructureError
from ...util import log

SITE_URL = "https://www.xiaohongshu.com/"
EXPLORE_URL = SITE_URL + "explore"

LOGIN_COOKIES = ("web_session",)
# 登录态被风控降级后，web_session 会挪到 last_web_session
DEMOTED_COOKIES = ("last_web_session",)


def cookie_names(page: CDP) -> set[str]:
    cookies = page.send("Network.getCookies", {"urls": [SITE_URL]}).get("cookies") or []
    return {cookie.get("name") or "" for cookie in cookies}


def require_login(page: CDP) -> None:
    names = cookie_names(page)
    if any(name in names for name in DEMOTED_COOKIES):
        raise AuthError(
            "小红书登录态已被风控降级（cookie 出现 last_web_session），请在该浏览器里重新登录后再试"
        )
    if not all(name in names for name in LOGIN_COOKIES):
        raise AuthError("小红书未登录（缺少 web_session cookie），请在该浏览器里登录")


def wait_js(page: CDP, expr: str, timeout: float = 20.0) -> bool:
    """轮询页面表达式；导航期间上下文被销毁视为可重试。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if page.evaluate(expr, retries=2):
                return True
        except CDPError as exc:
            if not is_transient(exc):
                raise
        time.sleep(0.3)
    return False


def goto_and_wait(
    page: CDP, url: str, ready_js: str, timeout: float = 20.0, what: str = "目标节点"
) -> None:
    page.send("Page.navigate", {"url": url})
    if not wait_js(page, ready_js, timeout):
        raise StructureError(f"页面结构不符：{timeout:.0f}s 内未出现{what} —— {url}")


def settle(seconds: tuple[float, float] = (2.0, 5.0)) -> None:
    """小红书详情页比闲鱼敏感，settle 用 2~5s 随机。"""
    time.sleep(random.uniform(*seconds))


def scroll_pause() -> None:
    time.sleep(random.uniform(1.0, 2.0))


def warn(message: str) -> None:
    log(message)
