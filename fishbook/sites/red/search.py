"""red search：走页面自身的搜索链路 —— 打开搜索 URL → 滚动收割卡片。

小红书没有可借的 SDK（`X-S-Common` 与浏览器指纹绑定，手工 fetch 会 461/300015），
所以既不自己签名，也不 `evaluate(fetch)`：直接打开 `search_result?keyword=`，
翻页由页面自己在滚动时用它的签名去请求。

当前版本实测：首页搜索框是 textarea `#search-input-in-feeds`，
方案里的 `#search-input` 只存在于窄版 header 且 `display:none`，
所以不走「填输入框 + 点按钮」，改为直接导航（等价于用户点进结果页）。

等待与滚动节奏都在 Python 侧驱动：页面里长等待会被后台标签页的定时器节流，容易挂死一次 evaluate。
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from ...cdp import CDP
from ...errors import AuthError, RiskError, UsageError
from ...util import log
from . import risk
from .count import parse_count
from .web import EXPLORE_URL, SITE_URL, goto_and_wait, require_login, scroll_pause

SEARCH_URL = SITE_URL + "search_result?keyword={q}&source=web_search_result_notes"

JS_DIR = Path(__file__).resolve().parent
_HARVEST_JS = (JS_DIR / "harvest.js").read_text(encoding="utf-8")

CARDS_READY_JS = """document.readyState === 'complete' && (
  !!document.querySelector('section.note-item')
  || !!document.querySelector('.search-empty-wrapper')
  || /登录后查看|请登录/.test(document.body ? document.body.innerText : '')
)"""
RESULT_TIMEOUT = 20.0
DEFAULT_LIMIT = 20
MAX_LIMIT = 60
MAX_SCROLLS = 15


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


def _harvest(page: CDP) -> dict:
    result = page.evaluate(_HARVEST_JS, retries=2)
    return result if isinstance(result, dict) else {}


def _collect(page: CDP, seen: dict, snapshot: dict) -> int:
    added = 0
    for card in snapshot.get("cards") or []:
        note_id = card.get("note_id")
        if note_id and note_id not in seen:
            seen[note_id] = card
            added += 1
    return added


def _scroll(page: CDP) -> None:
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)", retries=2)


def _search_once(page: CDP, keyword: str, limit: int, max_scrolls: int) -> list[dict]:
    goto_and_wait(
        page,
        SEARCH_URL.format(q=urllib.parse.quote(keyword)),
        CARDS_READY_JS,
        timeout=RESULT_TIMEOUT,
        what="搜索结果卡片 section.note-item",
    )
    require_login(page)

    snapshot = _harvest(page)
    if snapshot.get("risk"):
        raise RiskError(risk_msg(snapshot["risk"]))
    if snapshot.get("loginWall"):
        raise AuthError("小红书要求登录后才能搜索，请在该浏览器里登录")
    if snapshot.get("empty"):
        log(f"搜索「{keyword}」没有结果")
        return []

    seen: dict[str, dict] = {}
    _collect(page, seen, snapshot)
    idle = 0
    for _ in range(max_scrolls):
        if len(seen) >= limit:
            break
        _scroll(page)
        scroll_pause()
        snapshot = _harvest(page)
        if snapshot.get("risk"):
            raise RiskError(risk_msg(snapshot["risk"]))
        added = _collect(page, seen, snapshot)
        idle = 0 if added else idle + 1
        if idle >= 2:  # 连续 2 轮无新增即停
            break

    return [map_card(card) for card in list(seen.values())[:limit]]


def risk_msg(marker: str) -> str:
    return f"小红书风控拦截（{marker}），停止本次运行"


def run(
    page: CDP, keyword: str, limit: int = DEFAULT_LIMIT, max_scrolls: int = MAX_SCROLLS
) -> list[dict]:
    if not 1 <= limit <= MAX_LIMIT:
        raise UsageError(f"--limit 需在 1..{MAX_LIMIT}")
    if not 1 <= max_scrolls <= MAX_SCROLLS:
        raise UsageError(f"--max-scrolls 需在 1..{MAX_SCROLLS}")

    risk.guard(page, EXPLORE_URL)

    try:
        notes = _search_once(page, keyword, limit, max_scrolls)
    except RiskError as exc:
        # 命中风控：冷却一次，再跑一遍；还挡就抛出去（绝不循环）
        log(str(exc))
        risk.cooldown("search")
        notes = _search_once(page, keyword, limit, max_scrolls)

    log(f"搜索「{keyword}」: {len(notes)} 条")
    return notes
