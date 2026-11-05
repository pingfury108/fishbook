"""IM 页操作编排：JS 模板渲染 + CDP 调用（由 BrowserWorker 线程独占调用）。"""

from __future__ import annotations

import contextlib
import time
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
