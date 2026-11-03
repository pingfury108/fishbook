"""red search：读搜索页 SSR 数据（首屏），请求量与普通浏览一致。"""

from __future__ import annotations

import urllib.parse

from ...cdp import CDP
from ...errors import AuthError, BusinessError, UsageError
from ...util import log, number
from .web import JS_DIR, evaluate_state, goto, require_login

SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={q}&source=web_search_result_notes"
DEFAULT_LIMIT = 20
MAX_LIMIT = 40  # SSR 首屏条数有限，翻页需要 API（当前环境被风控）

_STATE_JS = (JS_DIR / "search_state.js").read_text(encoding="utf-8")


def map_note(feed: dict) -> dict:
    note_id = feed.get("id")
    token = feed.get("token") or ""
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if token:
        url = f"{url}?xsec_token={token}&xsec_source=pc_search"
    return {
        "note_id": note_id,
        "title": feed.get("title"),
        "type": feed.get("type") or feed.get("modelType"),
        "author": feed.get("author"),
        "author_id": feed.get("authorId"),
        "liked": number(feed.get("liked")),
        "cover": feed.get("cover"),
        "tags": feed.get("tags") or [],
        "url": url,
        "xsec_token": token or None,
    }


def run(page: CDP, keyword: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    if not 1 <= limit <= MAX_LIMIT:
        raise UsageError(f"--limit 需在 1..{MAX_LIMIT}")

    url = SEARCH_URL.format(q=urllib.parse.quote(keyword))
    goto(page, url)
    require_login(page)

    snapshot = evaluate_state(page, _STATE_JS)
    if snapshot.get("loginWall"):
        raise AuthError("小红书要求登录后才能查看搜索结果，请在该浏览器里登录")
    feeds = snapshot.get("feeds") or []
    if not feeds:
        raise BusinessError("搜索页没有下发任何结果（可能被风控或关键词无结果）")

    notes = [map_note(feed) for feed in feeds]
    if limit > len(notes):
        log(f"该关键词首屏只有 {len(notes)} 条（SSR 不翻页），按 {len(notes)} 条返回")
    return notes[:limit]
