"""red note：笔记详情，DOM 提取作用域锁死在 #noteContainer。"""

from __future__ import annotations

import re
from pathlib import Path

from ...cdp import CDP
from ...errors import AuthError, NotFoundError, StructureError, UsageError
from ...util import log
from . import risk
from .count import parse_count
from .web import SITE_URL, goto_and_wait, require_login, settle

JS_DIR = Path(__file__).resolve().parent
_TEMPLATE = (JS_DIR / "note.js").read_text(encoding="utf-8")

NOTE_READY_JS = "document.readyState === 'complete' && !!document.querySelector('#noteContainer')"
NOTE_ID_RE = re.compile(r"(?:^|/)([0-9a-f]{24})(?:$|[/?])")


def resolve_url(target: str) -> tuple[str, str]:
    """入参可以是完整 URL（优先，带 xsec_token）或 24 位 hex note_id。"""
    text = (target or "").strip()
    if text.startswith(("http://", "https://")):
        matched = NOTE_ID_RE.search(text)
        if not matched:
            raise UsageError(f"URL 里找不到 24 位 note_id: {text}")
        return text, matched.group(1)
    matched = NOTE_ID_RE.search(text)
    if not matched:
        raise UsageError(f"note_id 需为 24 位 hex 或完整 URL: {text}")
    note_id = matched.group(1)
    return f"{SITE_URL}explore/{note_id}", note_id


def map_note(note_id: str, url: str, data: dict) -> dict:
    return {
        "note_id": note_id,
        "note_url": url,
        "title": data.get("title"),
        "desc": data.get("desc"),
        "author": data.get("author"),
        "type": "video" if data.get("is_video") else "note",
        "likes": parse_count(data.get("likes_text")),
        "collects": parse_count(data.get("collects_text")),
        "comments": parse_count(data.get("comments_text")),
        "tags": data.get("tags") or [],
        "images": data.get("images") or [],
    }


def run(page: CDP, target: str) -> dict:
    url, note_id = resolve_url(target)
    goto_and_wait(page, url, NOTE_READY_JS, what="笔记容器 #noteContainer")
    require_login(page)
    risk.guard(page, url)
    settle()  # 详情页比闲鱼敏感：2~5s

    data = page.evaluate(_TEMPLATE, retries=0)
    if not isinstance(data, dict):
        raise StructureError(f"note.js 返回了非对象结果: {data!r}")
    if data.get("error") == "NO_NOTE_CONTAINER":
        raise StructureError(f"页面结构不符：找不到 #noteContainer —— {url}")
    if data.get("login_wall"):
        raise AuthError("小红书要求登录后查看笔记，请在该浏览器里登录")
    if data.get("not_found"):
        raise NotFoundError(f"笔记不存在或无法浏览: {note_id}")

    note = map_note(note_id, url, data)
    log(f"笔记 {note_id}: {note['title'] or '(无标题)'}")
    return note
