"""xy item：商品详情。"""

from __future__ import annotations

from ...cdp import CDP, MTOP_READY_JS
from ...util import log, number, render
from .mtop import JS_DIR, call, ensure_ready

ITEM_URL = "https://www.goofish.com/item?id={item_id}"

_TEMPLATE = (JS_DIR / "mtop_detail.js").read_text(encoding="utf-8")


def _num_or_none(value):
    """接口里的 "0" / "False" 都是「没有」，统一成 None。"""
    num = number(value)
    if isinstance(num, (int, float)) and num:
        return num
    return None


def _price(item: dict):
    for key in ("soldPrice", "defaultPrice"):  # defaultPrice 可能是字符串 "False"
        num = _num_or_none(item.get(key))
        if num is not None:
            return num
    return None


def map_detail(data: dict) -> dict:
    """详情字段映射（已按真实响应校正，详见 README）。"""
    item = data.get("itemDO") or {}
    seller = data.get("sellerDO") or {}

    images = [
        image.get("url")
        for image in (item.get("imageInfos") or [])
        if isinstance(image, dict) and image.get("url")
    ]
    labels = [
        label.get("text")
        for label in (item.get("itemLabelExtList") or [])
        if isinstance(label, dict) and label.get("text")
    ]
    return {
        "item_id": str(item.get("itemId") or ""),
        "title": item.get("title"),
        "desc": item.get("desc"),
        "price": _price(item),
        "original_price": _num_or_none(item.get("originalPrice")),
        "want_cnt": _num_or_none(item.get("wantCnt")),
        "collect_cnt": _num_or_none(item.get("collectCnt")),
        "browse_cnt": _num_or_none(item.get("browseCnt")),
        "status": item.get("itemStatusStr"),
        "images": images,
        "labels": labels,
        "seller": {
            "nick": seller.get("nick"),
            "seller_id": str(seller.get("sellerId") or ""),
            "city": seller.get("publishCity"),
            "summary": seller.get("xianyuSummary"),
            "reply_ratio_24h": seller.get("replyRatio24h"),
            "reply_interval": seller.get("replyInterval"),
        },
    }


def fetch_light(page: CDP, item_id: str) -> dict | None:
    """在任意已就绪的 goofish 页面直接调详情接口，不导航（批量取详情用这个）。

    mtop 的签名/token 由页面 SDK 负责，和当前停留在哪个 goofish 页面无关。
    """
    if not page.evaluate(MTOP_READY_JS, retries=2):
        log("[item] 当前页面没有 mtop SDK，跳过详情获取")
        return None
    js = render(_TEMPLATE, ITEM_ID=str(item_id))
    data = call(page, js, ITEM_URL.format(item_id=item_id))
    return map_detail(data)


def run(page: CDP, item_id: str) -> dict:
    url = ITEM_URL.format(item_id=item_id)
    ensure_ready(page, url, exact=True)
    data = call(page, render(_TEMPLATE, ITEM_ID=str(item_id)), url)
    return map_detail(data)
