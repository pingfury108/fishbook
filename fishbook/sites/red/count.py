"""小红书计数解析："9776" / "1.2万" / "3亿" / "9.8K" / "1.5M" → int，解析不了返回 None。"""

from __future__ import annotations

import re

_UNITS = {
    "": 1,
    "k": 1_000,
    "K": 1_000,
    "w": 10_000,
    "W": 10_000,
    "万": 10_000,
    "m": 1_000_000,
    "M": 1_000_000,
    "亿": 100_000_000,
}

_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*([万wW亿kKmM]?)$")


def parse_count(text) -> int | None:
    """解析不了返回 None，绝不返回 0（0 是有效值，会和「没有」混淆）。"""
    if text is None:
        return None
    cleaned = str(text).strip().replace(",", "").replace("+", "")
    if not cleaned:
        return None
    matched = _PATTERN.match(cleaned)
    if not matched:
        return None
    return round(float(matched.group(1)) * _UNITS[matched.group(2)])
