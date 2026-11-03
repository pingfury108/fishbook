"""xy search：关键词搜索，翻页 + 去重 + 限速。"""

from __future__ import annotations

import json
import re
import time
import urllib.parse

from ...cdp import CDP
from ...errors import AbortError, BusinessError, EndpointError, UsageError
from ...util import log, number, sleep_jitter
from .mtop import JS_DIR, call, ensure_ready, render

ROWS_PER_PAGE = 30
MAX_LIMIT = 60
SEARCH_URL = "https://www.goofish.com/search?q={q}"

_TEMPLATE = (JS_DIR / "mtop_search.js").read_text(encoding="utf-8")


def build_filters(
    price: tuple[int, int] | None, place: tuple[str, str] | None
) -> tuple[str, str, bool]:
    """返回 (propValueStr, extraFilterValue, fromFilter)。"""
    props = ""
    from_filter = False
    if price:
        low, high = price
        props = json.dumps({"searchFilter": f"priceRange:{low},{high};"}, ensure_ascii=False)
        from_filter = True

    division = []
    if place:
        province, city = place
        division.append({"province": province or "", "city": city or ""})
        from_filter = True

    extra = json.dumps(
        {"divisionList": division, "excludeMultiPlacesSellers": "0", "extraDivision": ""},
        ensure_ascii=False,
    )
    return props, extra, from_filter


def _js(keyword: str, number: int, price, place) -> str:
    props, extra, from_filter = build_filters(price, place)
    return render(
        _TEMPLATE,
        PAGE=number,
        KEYWORD=keyword,
        PROP_VALUE_STR=props,
        EXTRA_FILTER_VALUE=extra,
        FROM_FILTER=from_filter,
    )


def _search_url(keyword: str) -> str:
    return SEARCH_URL.format(q=urllib.parse.quote(keyword))


def fetch_page(
    page: CDP,
    keyword: str,
    number: int = 1,
    price: tuple[int, int] | None = None,
    place: tuple[str, str] | None = None,
) -> dict:
    """取一页原始 data（自带页面就绪检查，失败后可自愈重试）。"""
    url = _search_url(keyword)
    ensure_ready(page, url)
    return call(page, _js(keyword, number, price, place), url)


_TAG_ROWS = ("r2", "r3", "r4")  # r1 全是 freeShippingIcon 之类的图标名，不要
_WANT_RE = re.compile(r"(\d+)\s*人想要")


def _tags(ex: dict) -> list[str]:
    """fishTags 是 {r2/r3/r4: {tagList: [{data: {content}}]}}，展平成字符串列表。"""
    rows = ex.get("fishTags") or {}
    if not isinstance(rows, dict):
        return []
    tags: list[str] = []
    for key in _TAG_ROWS:
        row = rows.get(key) or {}
        for tag in row.get("tagList") or []:
            content = (tag.get("data") or {}).get("content") if isinstance(tag, dict) else None
            if content and content not in tags:
                tags.append(content)
    return tags


def _want_cnt(args: dict, tags: list[str]):
    """真实想要人数在 r3 标签（如 "17人想要"），args.wantNum 常年是 0。"""
    for text in tags:
        found = _WANT_RE.search(text)
        if found:
            return int(found.group(1))
    return number(args.get("wantNum"))


def map_list_item(entry: dict) -> dict | None:
    """列表项字段映射（已按真实响应校正，详见 README）。"""
    if not isinstance(entry, dict):
        return None
    main = (entry.get("data") or {}).get("item", {}).get("main") or {}
    args = (main.get("clickParam") or {}).get("args") or {}
    ex = main.get("exContent") or {}

    item_id = ex.get("itemId") or args.get("item_id")
    if not item_id:
        return None

    tags = _tags(ex)
    price = args.get("price") or (ex.get("detailParams") or {}).get("soldPrice")
    return {
        "item_id": str(item_id),
        "title": ex.get("title"),
        "price": number(price),
        "original_price": ex.get("oriPrice"),
        "city": ex.get("area") or args.get("p_city"),
        "want_cnt": _want_cnt(args, tags),
        "seller_nick": ex.get("userNickName"),
        "pic": ex.get("picUrl"),
        "tags": tags,
    }


def run(
    page: CDP,
    keyword: str,
    limit: int = 30,
    price: tuple[int, int] | None = None,
    place: tuple[str, str] | None = None,
) -> list[dict]:
    if not 1 <= limit <= MAX_LIMIT:
        raise UsageError(f"--limit 需在 1..{MAX_LIMIT}")

    results: dict[str, dict] = {}
    failures = 0
    number = 1
    while len(results) < limit:
        try:
            data = fetch_page(page, keyword, number, price, place)
            failures = 0
        except (BusinessError, EndpointError) as exc:
            failures += 1
            log(f"第 {number} 页失败（第 {failures} 次）: {exc}")
            if failures >= 3:
                raise AbortError(f"连续 {failures} 次失败，终止: {exc}") from exc
            if failures == 2:
                log("连续 2 次失败，暂停 10 分钟后再试")
                time.sleep(600)
            sleep_jitter()
            continue

        entries = data.get("resultList") or []
        for entry in entries:
            item = map_list_item(entry)
            if item and item["item_id"] not in results:
                results[item["item_id"]] = item
        log(f"第 {number} 页: {len(entries)} 条，累计 {len(results)} 条")

        has_next = (data.get("resultInfo") or {}).get("hasNextPage")
        if has_next is False or len(entries) < ROWS_PER_PAGE or len(results) >= limit:
            break
        number += 1
        sleep_jitter()

    return list(results.values())[:limit]
