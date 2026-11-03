"""red item：读笔记详情页 SSR 数据。"""

from __future__ import annotations

import urllib.parse

from ...cdp import CDP
from ...errors import BusinessError
from ...util import number
from .web import JS_DIR, evaluate_state, goto, require_login

NOTE_URL = "https://www.xiaohongshu.com/explore/{note_id}"

_STATE_JS = (JS_DIR / "note_state.js").read_text(encoding="utf-8")


def note_url(note_id: str, xsec_token: str = "") -> str:
    url = NOTE_URL.format(note_id=note_id)
    if xsec_token:
        url = f"{url}?xsec_token={urllib.parse.quote(xsec_token)}&xsec_source=pc_search"
    return url


def map_note(note: dict) -> dict:
    interact = note.get("interact") or {}
    return {
        "note_id": note.get("noteId"),
        "title": note.get("title"),
        "desc": note.get("desc"),
        "type": note.get("type"),
        "time": note.get("time"),
        "ip_location": note.get("ipLocation"),
        "liked": number(interact.get("likedCount")),
        "collected": number(interact.get("collectedCount")),
        "comment": number(interact.get("commentCount")),
        "share": number(interact.get("shareCount")),
        "author": note.get("author"),
        "images": note.get("images") or [],
        "tags": note.get("tags") or [],
        "video": note.get("video"),
    }


def run(page: CDP, note_id: str, xsec_token: str = "") -> dict:
    goto(page, note_url(note_id, xsec_token))
    require_login(page)

    snapshot = evaluate_state(page, _STATE_JS)
    note = snapshot.get("note")
    if not note:
        raise BusinessError(f"笔记详情未下发（可能被风控或笔记已删除）: {note_id}")
    return map_note(note)
