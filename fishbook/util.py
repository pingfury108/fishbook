"""站点无关的小工具。"""

from __future__ import annotations

import json
import random
import sys
import time


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def sleep_jitter() -> None:
    """请求间随机抖动，比任何 stealth 补丁都有效。"""
    time.sleep(random.uniform(1, 3))


def number(value):
    """数字字符串转 number，其余（含 "False"、空串）原样返回。"""
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return value
    return value


def render(template: str, **values) -> str:
    """把 __KEY__ 占位符换成 JSON 字面量，避免手写转义。"""
    for key, value in values.items():
        template = template.replace(f"__{key}__", json.dumps(value, ensure_ascii=False))
    return template
