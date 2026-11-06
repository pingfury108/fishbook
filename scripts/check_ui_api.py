"""静态检查：前端 api() 调用的 method 是否与 FastAPI 路由匹配。

起因：`testKey()` 里 `api("/api/llm/test")` 少写 method，后端只有 POST → 405，
排查半天。这个脚本把 UI 里所有调用和 app.routes 对比一遍，防同类低级错误。

用法：uv run python scripts/check_ui_api.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "fishbook/serve/static/index.html"
sys.path.insert(0, str(ROOT))

PATH_RE = re.compile(r"[`\"']([^`\"']+)[`\"']")
METHOD_RE = re.compile(r"method\s*:\s*[\"'](\w+)[\"']")


def _call_span(text: str, start: int) -> str:
    """从 api( 之后开始扫描，返回这一整个调用的文本（括号配平）。"""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return text[start:i]
            depth -= 1
    return text[start:]


def _join_path(span: str) -> str:
    """把 `"/api/items/" + id + "/pool"` 拼回 `/api/items/{param}/pool`。"""
    # 只看第一个参数（遇到顶层逗号就停）
    depth = 0
    first_arg = []
    for ch in span:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            break
        first_arg.append(ch)
    parts = "".join(first_arg).split("+")
    out = []
    for part in parts:
        part = part.strip()
        literal = re.fullmatch(r"[`\"']([^`\"']*)[`\"']", part)
        if literal:
            out.append(literal.group(1))
        elif part:
            out.append("{param}")
    path = "".join(out)
    path = re.sub(r"(\{param\})+", "{param}", path)
    return path or "/"


def ui_calls() -> list[tuple[str, str]]:
    text = HTML.read_text(encoding="utf-8")
    calls = []
    for match in re.finditer(r"api\(", text):
        span = _call_span(text, match.end())
        found = PATH_RE.search(span)
        if not found:
            continue
        method_match = METHOD_RE.search(span)
        method = method_match.group(1).upper() if method_match else "GET"
        calls.append((_join_path(span), method))
    return calls


def routes() -> set[tuple[str, str]]:
    from pathlib import Path as P

    from fishbook.serve.app import create_app
    from fishbook.serve.service import Service

    app = create_app(Service("http://127.0.0.1:1", P(".fishbook/check-only.db")))
    table: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path:
            continue
        for method in methods - {"HEAD", "OPTIONS"}:
            table.add((path, method))
    return table


def normalize_route(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{param}", path)


def same_shape(ui_path: str, route_path: str) -> bool:
    """按段比较：`/api/items/{param}/pool` 与 `/api/items/{id}/pool` 视为相同。"""
    a = [seg for seg in ui_path.split("/") if seg != ""]
    b = [seg for seg in route_path.split("/") if seg != ""]
    if len(a) != len(b):
        return False
    for left, right in zip(a, b):
        if left == right:
            continue
        if left.startswith("{") or right.startswith("{"):
            continue
        return False
    return True


def main() -> int:
    table = {(normalize_route(p), m) for p, m in routes()}
    bad = []
    for path, method in ui_calls():
        if any(m == method and same_shape(path, p) for p, m in table):
            continue
        bad.append((path, method))

    if bad:
        print("✗ 前端调用与后端路由不匹配（method 或路径）：")
        for path, method in sorted(set(bad)):
            print(f"  {method:6s} {path}")
        return 1
    print(f"✓ 前端 {len(ui_calls())} 处 api() 调用与后端路由全部匹配")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
