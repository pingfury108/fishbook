"""闲鱼「我发布的」页面（goofish.com/personal）：读取在售商品。

要点：
- tab 文案带计数（「在售9」），必须前缀匹配，不能全等；
- tab 栏是渲染后才出现的，复用旧页面时经常还没出来 → 每次重新导航再点；
- 点完用 tab 上的数字自校验（期望 9 条就该拿到 9 条），不符重试一次。
"""

from __future__ import annotations

import contextlib
import re
import time
from pathlib import Path

from ...cdp import CDP, find_target_url, open_tab
from ...errors import StructureError
from ...util import log

PERSONAL_URL = "https://www.goofish.com/personal"
DOMAIN = "goofish.com/personal"
READY_JS = "document.readyState === 'complete' && /宝贝|个人经营/.test(document.body ? document.body.innerText : '')"
TAB_READY_JS = '!!document.querySelector(\'[class*="tabItem"], [class*="tab-item"]\')'
CARD_COUNT_JS = "new Set([...document.querySelectorAll('a[href*=\"item?id=\"]')].map((a) => a.getAttribute('href'))).size"

DISCOVER_JS = (Path(__file__).resolve().parent / "personal_discover.js").read_text(encoding="utf-8")
SHOP_NAME_JS = """(() => {
  const h1 = document.querySelector('h1, [class*="nickname"], [class*="user-name"], [class*="userName"]');
  const text = h1 ? (h1.textContent || "").trim() : "";
  if (text) return text.split("\\n")[0].slice(0, 30);
  return (document.title || "").replace(/_闲鱼$/, "").trim().slice(0, 30);
})()"""

CLICK_TAB_JS = """((name) => {
  const tabs = [...document.querySelectorAll('[class*="tabItem"], [class*="tab-item"]')];
  const tab = tabs.find((el) => el.textContent.trim().startsWith(name));
  if (!tab) return { error: "NO_TAB", tabs: tabs.map((el) => el.textContent.trim().slice(0, 10)) };
  const opts = { bubbles: true, cancelable: true, view: window };
  tab.dispatchEvent(new MouseEvent("mousedown", opts));
  tab.dispatchEvent(new MouseEvent("mouseup", opts));
  tab.dispatchEvent(new MouseEvent("click", opts));
  return { ok: true, label: tab.textContent.trim() };
})("""


def _close_other_tabs(base: str, keep_ws: str) -> None:
    """个人页标签也只保留一个。"""
    from ... import cdp as cdp_mod

    keep = (keep_ws or "").rstrip("/").rsplit("/", 1)[-1]
    for target in cdp_mod.page_targets(base):
        if "goofish.com/personal" not in (target.get("url") or ""):
            continue
        if target.get("id") == keep:
            continue
        cdp_mod.close_tab(base, target.get("id", ""))


def ensure_page(cdp_endpoint: str, page: CDP | None) -> CDP:
    """拿一个停在「我发布的」页面的长驻连接：优先复用，其次新开唯一一个。"""
    from ... import cdp as cdp_mod

    base, _ = cdp_mod.normalize_endpoint(cdp_endpoint)

    if page is not None:
        on_page = False
        with contextlib.suppress(Exception):
            on_page = DOMAIN in page.url()
        if on_page:
            _close_other_tabs(base, page.ws_url)
            goto_personal(page)  # 不在线时内部会刷新
            return page
        goto_personal(page)  # 同一个连接导航过去，不新开
        _close_other_tabs(base, page.ws_url)
        return page

    ws_url = find_target_url(base, DOMAIN)
    if ws_url:
        page = cdp_mod.CDP(ws_url).connect()
        _close_other_tabs(base, page.ws_url)
        goto_personal(page)
        return page

    tab = open_tab(base, PERSONAL_URL)
    page = cdp_mod.CDP(tab["ws_url"]).connect()
    _close_other_tabs(base, page.ws_url)
    goto_personal(page)
    return page


def shop_name_once(cdp_endpoint: str) -> str:
    """读店铺名：需要临时标签时用完立刻关掉，不留残留。"""
    from ... import cdp as cdp_mod

    base, _ = cdp_mod.normalize_endpoint(cdp_endpoint)
    existing = find_target_url(base, DOMAIN)
    page = None
    opened_id = ""
    try:
        if existing:
            page = cdp_mod.CDP(existing).connect()
        else:
            tab = open_tab(base, PERSONAL_URL)
            page = cdp_mod.CDP(tab["ws_url"]).connect()
            opened_id = tab["target_id"]
        goto_personal(page)
        return shop_name(page)
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                page.close()
        if opened_id:
            cdp_mod.close_tab(base, opened_id)


def discover_once(cdp_endpoint: str) -> list[dict]:
    """一次性读取在售商品：需要新标签就新开，读完立刻关掉，不留残留标签。"""
    from ... import cdp as cdp_mod

    base, _ = cdp_mod.normalize_endpoint(cdp_endpoint)
    existing = find_target_url(base, DOMAIN)
    page = None
    opened_id = ""
    try:
        if existing:
            page = cdp_mod.CDP(existing).connect()
        else:
            tab = open_tab(base, PERSONAL_URL)
            page = cdp_mod.CDP(tab["ws_url"]).connect()
            opened_id = tab["target_id"]
        return discover(page)
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                page.close()
        if opened_id:
            cdp_mod.close_tab(base, opened_id)


def goto_personal(page: CDP, timeout: float = 20.0) -> None:
    from ...cdp import is_transient

    # 已在个人页且还活着 → 直接复用，不重新导航
    on_page = False
    with contextlib.suppress(Exception):
        on_page = DOMAIN in page.url() and page.evaluate(TAB_READY_JS, retries=1)
    if on_page:
        time.sleep(0.5)
        return

    in_domain = False
    with contextlib.suppress(Exception):
        in_domain = DOMAIN in page.url()
    if in_domain:
        page.send("Page.reload")  # 页面死了就刷新这个标签
    else:
        page.send("Page.navigate", {"url": PERSONAL_URL})
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate(READY_JS, retries=2):
                break
        except Exception as exc:
            if not is_transient(exc):
                raise
        time.sleep(0.4)
    else:
        raise StructureError(f"个人主页 {timeout:.0f}s 未就绪: {PERSONAL_URL}")

    # 再等 tab 栏出现，点击才有意义
    deadline = time.time() + 10
    while time.time() < deadline:
        if page.evaluate(TAB_READY_JS, retries=1):
            time.sleep(0.8)  # 等列表首屏
            return
        time.sleep(0.4)
    log("[personal] tab 栏 10s 未渲染")


def _click_tab(page: CDP, name: str) -> int | None:
    """点击 tab，返回该 tab 上标注的条数（用于自校验）。"""
    before = page.evaluate(CARD_COUNT_JS, retries=1)
    result = page.evaluate(CLICK_TAB_JS + repr(name) + ")", retries=2)
    if not isinstance(result, dict) or not result.get("ok"):
        log(f"[personal] 找不到「{name}」tab: {result}")
        return None

    label = str(result.get("label") or "")
    digits = re.sub(r"\D", "", label)
    expected = int(digits) if digits else None

    time.sleep(1.2)
    after = page.evaluate(CARD_COUNT_JS, retries=1)
    deadline = time.time() + 5
    while time.time() < deadline and after == before:
        time.sleep(0.5)
        after = page.evaluate(CARD_COUNT_JS, retries=1)
    log(f"[personal] 切到 {label}: 卡片 {before} → {after}")
    return expected


def _scrape(page: CDP) -> list[dict]:
    result = page.evaluate(DISCOVER_JS, retries=2)
    if not isinstance(result, dict):
        raise StructureError(f"个人页解析返回异常: {result!r}")
    return result.get("items") or []


def discover(page: CDP, attempts: int = 2) -> list[dict]:
    """只取「在售」商品。"""
    items: list[dict] = []
    for attempt in range(1, attempts + 1):
        goto_personal(page)
        expected = _click_tab(page, "在售")
        items = _scrape(page)
        if expected is None or expected == len(items):
            break
        log(f"[personal] 条数不符（期望 {expected}，实际 {len(items)}），重试 {attempt}/{attempts}")
        time.sleep(1.5)
    log(f"[personal] 在售商品 {len(items)} 个")
    return items


def shop_name(page: CDP) -> str:
    """读当前登录账号在个人主页显示的名字。"""
    result = page.evaluate(SHOP_NAME_JS, retries=2)
    return result.strip() if isinstance(result, str) else ""
