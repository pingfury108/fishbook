"""xy item：商品详情。"""

from __future__ import annotations

from ...cdp import CDP
from .mtop import JS_DIR, call, ensure_ready, number, render

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


def run(page: CDP, item_id: str) -> dict:
    url = ITEM_URL.format(item_id=item_id)
    ensure_ready(page, url, exact=True)
    data = call(page, render(_TEMPLATE, ITEM_ID=str(item_id)), url)
    return map_detail(data)
