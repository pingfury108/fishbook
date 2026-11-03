"""red search：UI 自动化 —— 输入 → 点击 → 滚动收割卡片。

小红书没有可借的 SDK（`X-S-Common` 与浏览器指纹绑定，手工 fetch 会 461/300015），
所以走页面自身的搜索链路：填输入框、点按钮、滚页面，然后从 `section.note-item` 提字段。
"""

from __future__ import annotations

from pathlib import Path

from ...cdp import CDP
from ...errors import AuthError, RiskError, StructureError, UsageError
from ...util import log, render
from . import risk
from .count import parse_count
from .web import EXPLORE_URL, goto_and_wait, require_login

JS_DIR = Path(__file__).resolve().parent
_TEMPLATE = (JS_DIR / "search.js").read_text(encoding="utf-8")

SEARCH_READY_JS = "document.readyState === 'complete' && !!document.querySelector('#search-input')"
SEARCH_TIMEOUT = 90.0
DEFAULT_LIMIT = 20
MAX_LIMIT = 60
MAX_SCROLLS = 15

_STRUCT_ERRORS = {
    "NO_SEARCH_INPUT": "找不到唯一的可见输入框 #search-input",
    "NO_SEARCH_ICON": "找不到唯一的搜索按钮 .search-icon",
    "INPUT_SET_FAILED": "输入框赋值后回读不一致（layout 异常）",
    "SEARCH_TIMEOUT": "点击搜索后 15s 内没出现结果路由或卡片",
}


def map_card(card: dict) -> dict:
    return {
        "note_id": card.get("note_id"),
        # 必须保留完整 URL：详情页靠 xsec_token 才能打开，光有 note_id 不够
        "note_url": card.get("note_url"),
        "title": card.get("title"),
        "author": card.get("author"),
        "likes": parse_count(card.get("likes_text")),
        "type": "video" if card.get("is_video") else "note",
    }


def _evaluate(page: CDP, keyword: str, limit: int, max_scrolls: int) -> dict:
    js = render(_TEMPLATE, KEYWORD=keyword, LIMIT=limit, MAX_SCROLLS=max_scrolls)
    result = page.evaluate(js, timeout=SEARCH_TIMEOUT)
    if not isinstance(result, dict):
        raise StructureError(f"search.js 返回了非对象结果: {result!r}")
    return result


def run(
    page: CDP, keyword: str, limit: int = DEFAULT_LIMIT, max_scrolls: int = MAX_SCROLLS
) -> list[dict]:
    if not 1 <= limit <= MAX_LIMIT:
        raise UsageError(f"--limit 需在 1..{MAX_LIMIT}")
    if not 1 <= max_scrolls <= MAX_SCROLLS:
        raise UsageError(f"--max-scrolls 需在 1..{MAX_SCROLLS}")

    goto_and_wait(page, EXPLORE_URL, SEARCH_READY_JS, what="搜索输入框 #search-input")
    require_login(page)

    result = _evaluate(page, keyword, limit, max_scrolls)

    # 命中风控：冷却一次，只重load 一次，再挡就退出（绝不循环重试）
    if result.get("risk"):
        risk.cooldown(result["risk"])
        goto_and_wait(page, EXPLORE_URL, SEARCH_READY_JS, what="搜索输入框 #search-input")
        result = _evaluate(page, keyword, limit, max_scrolls)
        if result.get("risk"):
            raise RiskError(f"小红书风控拦截（{result['risk']}），停止本次运行")

    error = result.get("error")
    if error == "LOGIN_REQUIRED":
        raise AuthError("小红书要求登录后才能搜索，请在该浏览器里登录")
    if error:
        raise StructureError(_STRUCT_ERRORS.get(error, f"搜索页结构异常: {error}"))

    cards = result.get("cards") or []
    notes = [map_card(card) for card in cards if card.get("note_id")]
    log(f"搜索「{keyword}」: {len(notes)} 条")
    return notes[:limit]
