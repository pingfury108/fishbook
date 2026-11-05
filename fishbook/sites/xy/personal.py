"""闲鱼「我发布的」页面（goofish.com/personal）：在售商品读取。"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

from ...cdp import CDP, find_target_url, open_tab
from ...errors import StructureError
from ...util import log

PERSONAL_URL = "https://www.goofish.com/personal"
DOMAIN = "goofish.com/personal"
READY_JS = "document.readyState === 'complete' && /宝贝|个人经营/.test(document.body ? document.body.innerText : '')"

DISCOVER_JS = (Path(__file__).resolve().parent / "personal_discover.js").read_text(encoding="utf-8")


def ensure_page(cdp_endpoint: str, page: CDP | None) -> CDP:
    """拿一个停在「我发布的」页面的长驻连接（worker 线程独占）；没有标签就新开一个。"""
    if page is not None:
        ok = False
        with contextlib.suppress(Exception):
            ok = DOMAIN in page.url()
        if ok:
            return page
        page.close()

    from ... import cdp as cdp_mod

    base, _ = cdp_mod.normalize_endpoint(cdp_endpoint)
    ws_url = find_target_url(base, DOMAIN)
    if not ws_url:
        ws_url = open_tab(base, PERSONAL_URL)
    page = cdp_mod.CDP(ws_url).connect()
    goto_personal(page)
    return page


def goto_personal(page: CDP, timeout: float = 15.0) -> None:
    from ...cdp import is_transient

    with contextlib.suppress(Exception):
        page.send("Page.bringToFront")
    page.send("Page.navigate", {"url": PERSONAL_URL})
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate(READY_JS, retries=2):
                time.sleep(1.5)  # 等宝贝列表渲染
                return
        except Exception as exc:
            if not is_transient(exc):
                raise
        time.sleep(0.4)
    raise StructureError(f"个人主页 15s 未就绪: {PERSONAL_URL}")


def _click_tab(page: CDP, name: str) -> None:
    page.evaluate(
        """((name) => {
          const tab = [...document.querySelectorAll('*')].find(el =>
            el.children.length === 0 && el.textContent.trim() === name);
          if (!tab) return false;
          const opts = {bubbles: true, cancelable: true, view: window};
          tab.dispatchEvent(new MouseEvent('mousedown', opts));
          tab.dispatchEvent(new MouseEvent('mouseup', opts));
          tab.dispatchEvent(new MouseEvent('click', opts));
          return true;
        })"""
        + f"('{name}')",
        retries=2,
    )


def discover(page: CDP, scrolls: int = 3) -> list[dict]:
    """切到「在售」筛选，滚动加载后解析商品卡。"""
    _click_tab(page, "在售")
    time.sleep(2.5)
    for _ in range(scrolls):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)", retries=2)
        time.sleep(1.2)
    page.evaluate("window.scrollTo(0, 0)", retries=2)
    time.sleep(0.5)

    result = page.evaluate(DISCOVER_JS, retries=2)
    if not isinstance(result, dict):
        raise StructureError(f"个人页解析返回异常: {result!r}")
    items = result.get("items") or []
    log(f"[personal] 在售商品 {len(items)} 个")
    return items
