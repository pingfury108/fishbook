"""IM 页操作编排：JS 模板渲染 + CDP 调用（由 BrowserWorker 线程独占调用）。"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.parse
from pathlib import Path

from ...cdp import CDP
from ...util import render
from ..xy.mtop import check_risk

JS_DIR = Path(__file__).resolve().parent
IM_URL = "https://www.goofish.com/im"

_SCAN = (JS_DIR / "im_scan.js").read_text(encoding="utf-8")
_OPEN = (JS_DIR / "im_open.js").read_text(encoding="utf-8")
_READ = (JS_DIR / "im_read.js").read_text(encoding="utf-8")
_SEND = (JS_DIR / "im_send.js").read_text(encoding="utf-8")
_VERIFY = (JS_DIR / "im_verify.js").read_text(encoding="utf-8")
_FILL = (JS_DIR / "im_fill.js").read_text(encoding="utf-8")

OPEN_BY_INDEX_JS = """((idx) => {
  const items = [...document.querySelectorAll('[class*="conversation-item"]')].filter(
    (el) => !el.className.includes("skeleton") && el.getBoundingClientRect().height > 0
  );
  const target = items[idx];
  if (!target) return { error: "NO_ITEM", count: items.length };
  const opts = { bubbles: true, cancelable: true, view: window };
  target.dispatchEvent(new MouseEvent("mousedown", opts));
  target.dispatchEvent(new MouseEvent("mouseup", opts));
  target.dispatchEvent(new MouseEvent("click", opts));
  return { ok: true, text: (target.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 30) };
})("""

HEADINFO_API = "message.headinfo"


def ensure_page(cdp_endpoint: str, page: CDP | None) -> CDP:
    """拿一个停在 IM 页的长驻连接（worker 线程独占）。"""
    if page is not None:
        ok = False
        with contextlib.suppress(Exception):  # 连接坏了就重建
            ok = "goofish.com/im" in page.url()
        if ok:
            return page
        page.close()
    from ... import cdp as cdp_mod

    ws_url, _ = cdp_mod.resolve_target(cdp_endpoint, "goofish.com")
    page = cdp_mod.CDP(ws_url).connect()
    with contextlib.suppress(Exception):
        already = "goofish.com/im" in page.url()
    if not already:
        goto_im(page)
    return page


def goto_im(page: CDP) -> None:
    from ...cdp import is_transient
    from ...errors import StructureError

    page.send("Page.bringToFront")
    page.send("Page.navigate", {"url": IM_URL})
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if page.evaluate("document.readyState === 'complete' && location.pathname === '/im'"):
                return
        except Exception as exc:
            if not is_transient(exc):
                raise
        time.sleep(0.3)
    raise StructureError(f"IM 页 15s 未就绪: {IM_URL}")


def scan(page: CDP) -> dict:
    check_risk(page)
    result = page.evaluate(_SCAN, retries=2)
    return result if isinstance(result, dict) else {"conversations": []}


def enable_network(page: CDP) -> None:
    """打开 Network 域才能抓到页面自己的 headinfo 请求（拿 sessionId/itemId）。"""
    with contextlib.suppress(Exception):
        page.send("Network.enable", timeout=10)


def open_by_index(page: CDP, idx: int, wait: float = 2.2) -> dict:
    """按序号点开会话，并从页面自己发出的 headinfo 请求里读 sessionId / itemId。"""
    page.take_events("Network.requestWillBeSent")
    result = page.evaluate(OPEN_BY_INDEX_JS + str(int(idx)) + ")", retries=2)
    if not isinstance(result, dict) or not result.get("ok"):
        return result if isinstance(result, dict) else {"error": "BAD_RETURN"}

    session_id, item_id = "", ""
    deadline = time.time() + 8
    first = True
    while time.time() < deadline and not session_id:
        page.pump(wait if first else 0.6)
        first = False
        for event in page.take_events("Network.requestWillBeSent"):
            request = event.get("request") or {}
            if HEADINFO_API not in request.get("url", ""):
                continue
            body = urllib.parse.unquote(request.get("postData") or "")
            payload = body.split("data=")[-1]
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            session_id = str(data.get("sessionId") or "")
            item_id = str(data.get("itemId") or "")
            break
    result.update({"session_id": session_id, "item_id": item_id})
    return result


def open_conversation(page: CDP, name: str) -> dict:
    result = page.evaluate(render(_OPEN, NAME=name), retries=2)
    if isinstance(result, dict) and result.get("ok"):
        time.sleep(1.5 + (len(name) % 3) * 0.3)  # 让消息流渲染
    return result if isinstance(result, dict) else {"error": "BAD_RETURN"}


def read(page: CDP, limit: int = 20) -> dict:
    result = page.evaluate(render(_READ, LIMIT=limit), retries=2)
    return result if isinstance(result, dict) else {"messages": []}


def send(page: CDP, text: str) -> dict:
    result = page.evaluate(render(_SEND, TEXT=text))
    if not isinstance(result, dict) or result.get("error"):
        return result if isinstance(result, dict) else {"error": "BAD_RETURN"}
    time.sleep(1.2)  # 等消息上屏
    verify = page.evaluate(render(_VERIFY, TEXT=text), retries=2)
    if isinstance(verify, dict) and verify.get("cleared") and verify.get("appears"):
        return {"ok": True}
    return {"error": "SEND_NOT_VERIFIED", "verify": verify}


def fill_test(page: CDP, text: str = "[fishbook 链路测试]") -> dict:
    """链路测试：只填入输入框并清空，绝不点发送。"""
    result = page.evaluate(render(_FILL, TEXT=text))
    return result if isinstance(result, dict) else {"error": "BAD_RETURN"}
