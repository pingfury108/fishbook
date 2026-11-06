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


IM_DOMAIN = "goofish.com/im"
READY_JS = (
    "document.readyState === 'complete' "
    "&& !!document.querySelector('[class*=\"conversation-item\"]')"
)


def _target_id(ws_url: str) -> str:
    return (ws_url or "").rstrip("/").rsplit("/", 1)[-1]


def _close_other_im_tabs(base: str, keep_ws: str) -> None:
    """IM 标签只保留一个：把其它闲鱼 IM 标签关掉，绝不多开。"""
    from ... import cdp as cdp_mod

    keep = _target_id(keep_ws)
    for target in cdp_mod.page_targets(base):
        url = target.get("url") or ""
        if IM_DOMAIN not in url:
            continue
        if target.get("id") == keep:
            continue
        cdp_mod.close_tab(base, target.get("id", ""))


def _ready(page: CDP, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.suppress(Exception):  # 导航中上下文会被销毁
            if page.evaluate(READY_JS, retries=1):
                return True
        time.sleep(0.4)
    return False


def goto_im(page: CDP, reload: bool = False) -> None:
    """确保页面在 IM 页且会话列表已渲染；不在线就刷新，不新开标签。"""
    from ...errors import StructureError

    with contextlib.suppress(Exception):
        if "goofish.com/im" in page.url() and not reload:
            if _ready(page, timeout=8.0):
                return
            reload = True  # 在 IM 页但这页是死的 → 刷新

    if reload or "goofish.com/im" not in (page.url() or ""):
        page.send("Page.reload" if reload else "Page.navigate", {} if reload else {"url": IM_URL})

    if not _ready(page, timeout=15.0):
        # 还不行就再整页导航一次，仍然只用这一个标签
        page.send("Page.navigate", {"url": IM_URL})
        if not _ready(page, timeout=15.0):
            raise StructureError(f"IM 页 15s 未就绪: {IM_URL}")


def ensure_page(cdp_endpoint: str, page: CDP | None) -> CDP:
    """拿 IM 长驻连接：优先复用现有连接/现有标签，其次新开唯一一个。"""
    from ... import cdp as cdp_mod

    base, _ = cdp_mod.normalize_endpoint(cdp_endpoint)

    if page is not None:
        on_im = False
        with contextlib.suppress(Exception):
            on_im = IM_DOMAIN in page.url()
        if on_im:
            _close_other_im_tabs(base, page.ws_url)
            goto_im(page)  # 死页面会自动 reload
            return page
        # 同一个连接导航回 IM，不开新标签
        goto_im(page)
        _close_other_im_tabs(base, page.ws_url)
        return page

    existing = cdp_mod.find_target_url(base, IM_DOMAIN)
    if existing:
        page = cdp_mod.CDP(existing).connect()
        _close_other_im_tabs(base, page.ws_url)
        goto_im(page)
        return page

    tab = cdp_mod.open_tab(base, IM_URL)
    page = cdp_mod.CDP(tab["ws_url"]).connect()
    _close_other_im_tabs(base, page.ws_url)
    goto_im(page, reload=False)
    return page


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
