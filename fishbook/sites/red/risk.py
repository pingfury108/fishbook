"""小红书风控识别 + 单次冷却重试。

写成一个 if，绝不写成 loop —— 锤热风控是把限流升级成封号的路径。
"""

from __future__ import annotations

import random
import time

from ...cdp import CDP
from ...errors import RiskError
from ...util import log

RISK_URL_MARKERS = (
    "/website-login/error",
    "error_code=300017",
    "error_code=300031",
    "/website-login/captcha",
)
RISK_TEXT_MARKERS = ("安全限制", "访问链接异常", "请求太频繁", "访问频次异常")

PROBE_JS = (
    "({url: location.href, text: document.body ? document.body.innerText.slice(0, 4000) : ''})"
)


def probe(page: CDP) -> dict:
    result = page.evaluate(PROBE_JS, retries=2)
    return result if isinstance(result, dict) else {}


def hit(result: dict) -> str | None:
    url = result.get("url") or ""
    text = result.get("text") or ""
    for marker in RISK_URL_MARKERS:
        if marker in url:
            return marker
    for marker in RISK_TEXT_MARKERS:
        if marker in text:
            return marker
    return None


def cooldown(marker: str) -> None:
    """冷却 8~18s 随机，给风控一个台阶。"""
    pause = random.uniform(8, 18)
    log(f"命中小红书风控（{marker}），冷却 {pause:.0f}s")
    time.sleep(pause)


def guard(page: CDP, refresh_url: str) -> None:
    """命中风控就冷却一次 + 重载一次，再命中就退出码 4。"""
    result = probe(page)
    marker = hit(result)
    if not marker:
        return

    cooldown(marker)
    page.goto(refresh_url, ready_timeout=20.0)

    result = probe(page)
    marker = hit(result)
    if marker:
        raise RiskError(f"小红书风控拦截（{marker}），停止本次运行")
