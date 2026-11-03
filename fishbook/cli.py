"""命令行入口：check / xy search / xy item。"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import cdp
from .cdp import MTOP_READY_JS
from .errors import EXIT_ENDPOINT, EXIT_OK, EndpointError, FishbookError, UsageError
from .sites.xy import item as xy_item
from .sites.xy import search as xy_search
from .sites.xy.mtop import has_punish_cookie, log

DOMAINS = {"xy": "goofish.com"}
FORMATS = ("json", "table")
TITLE_WIDTH = 40


# ---------- 输出 ----------


def _text(value) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).replace("\n", " ")


def _width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _fit(text: str, width: int | None = None) -> str:
    if width and _width(text) > width:
        kept, used = [], 0
        for ch in text:
            step = 2 if ord(ch) > 0x2E80 else 1
            if used + step > width - 1:
                break
            kept.append(ch)
            used += step
        text = "".join(kept) + "…"
    return text + " " * (width - _width(text) if width else 0)


def print_table(
    rows: list[dict], columns: list[tuple[str, str]], widths: dict | None = None
) -> None:
    widths = widths or {}
    headers = [label for _, label in columns]
    body = [[_fit(_text(row.get(key)), widths.get(key)) for key, _ in columns] for row in rows]
    sizes = [max([_width(h)] + [_width(r[i]) for r in body]) for i, h in enumerate(headers)]
    print("  ".join(_fit(h, sizes[i]) for i, h in enumerate(headers)).rstrip())
    print("  ".join("-" * sizes[i] for i in range(len(headers))))
    for row in body:
        print("  ".join(_fit(cell, sizes[i]) for i, cell in enumerate(row)).rstrip())


def _search_table(items: list[dict]) -> None:
    print_table(
        [
            {
                "item_id": item["item_id"],
                "price": item["price"],
                "title": item["title"],
                "city": item["city"],
                "want": item["want_cnt"],
                "seller": item["seller_nick"],
            }
            for item in items
        ],
        [
            ("item_id", "ITEM_ID"),
            ("price", "PRICE"),
            ("title", "TITLE"),
            ("city", "CITY"),
            ("want", "WANT"),
            ("seller", "SELLER"),
        ],
        {"title": TITLE_WIDTH},
    )


def _item_table(item: dict) -> None:
    seller = item.get("seller") or {}
    for key, value in item.items():
        if key == "seller":
            print(f"{key:<14}{json.dumps(seller, ensure_ascii=False)}")
        else:
            print(f"{key:<14}{_text(value)}")


def emit_ok(data, fmt: str, renderer=None) -> int:
    if fmt == "table" and renderer is not None:
        renderer(data)
        return EXIT_OK
    print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
    return EXIT_OK


def emit_error(exc: FishbookError) -> None:
    print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False))
    print(f"错误: {exc.message}", file=sys.stderr)


# ---------- 连接 ----------


def open_target(args, site: str = "xy") -> cdp.CDP:
    raw = args.cdp or os.environ.get("FISHBOOK_CDP") or None
    base, ws_url = cdp.normalize_endpoint(raw)
    if not ws_url:
        ws_url, note = cdp.resolve_target(base, DOMAINS[site])
        log(f"目标标签: {note}")
    return cdp.CDP(ws_url).connect()


# ---------- 子命令 ----------


def cmd_check(args) -> int:
    raw = args.cdp or os.environ.get("FISHBOOK_CDP") or None
    base, ws_url = cdp.normalize_endpoint(raw)
    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"{'✓' if ok else '✗'} {name}: {detail}", file=sys.stderr, flush=True)

    version = cdp.http_get_json(base, "/json/version")
    record("端点连通", True, version.get("Browser") or base)

    pages = cdp.page_targets(base)
    record("page target", bool(pages), f"{len(pages)} 个")
    for target in pages:
        print(f"    - {target.get('url')}", file=sys.stderr)

    if not ws_url:
        matched = [t for t in pages if DOMAINS["xy"] in (t.get("url") or "")]
        chosen = matched[0] if matched else (pages[0] if pages else None)
        if chosen is None or not chosen.get("webSocketDebuggerUrl"):
            raise EndpointError("没有任何可用的 page target")
        ws_url = chosen["webSocketDebuggerUrl"]
        print(f"    选中: {chosen.get('url')}", file=sys.stderr)

    page = cdp.CDP(ws_url).connect()
    try:
        current = page.url()
        record("当前 URL", bool(current), current)
        # cookie 按站点查，否则标签停在 chrome:// 时永远查不到
        cookie_url = current if "goofish.com" in current else "https://www.goofish.com/"
        cookies = page.send("Network.getCookies", {"urls": [cookie_url]})
        names = {c.get("name") for c in cookies.get("cookies") or []}
        for name in ("unb", "tracknick"):
            record(
                f"cookie {name}",
                name in names,
                "存在" if name in names else f"缺失（{cookie_url}）",
            )

        ready = bool(page.evaluate(MTOP_READY_JS, retries=2))
        record("mtop SDK", ready, "就绪" if ready else "未就绪（先把该标签打开 goofish.com）")

        # 只是惩罚期痕迹，不代表当前请求一定被拦，仅作提示
        punish = has_punish_cookie(page)
        record("x5sec 痕迹", True, "存在（曾被惩罚，注意限速）" if punish else "无")

        if ready and args.keyword:
            try:
                data = xy_search.fetch_page(page, args.keyword)
                entries = data.get("resultList") or []
                record("最小搜索", bool(entries), f"{len(entries)} 条（keyword={args.keyword}）")
            except FishbookError as exc:
                record("最小搜索", False, exc.message)
    finally:
        page.close()

    ok = all(check["ok"] for check in checks)
    print(json.dumps({"ok": ok, "data": {"checks": checks}}, ensure_ascii=False))
    return EXIT_OK if ok else EXIT_ENDPOINT


def cmd_xy_search(args) -> int:
    if not 1 <= args.limit <= xy_search.MAX_LIMIT:
        raise UsageError(f"--limit 需在 1..{xy_search.MAX_LIMIT} 之间")
    price = None
    if args.min_price is not None or args.max_price is not None:
        low = 0 if args.min_price is None else args.min_price
        high = 99999999 if args.max_price is None else args.max_price
        if low > high:
            raise UsageError("--min-price 不能大于 --max-price")
        price = (low, high)
    place = (args.province, args.city) if (args.province or args.city) else None

    page = open_target(args)
    try:
        items = xy_search.run(page, args.keyword, limit=args.limit, price=price, place=place)
    finally:
        page.close()
    return emit_ok(items, args.format, _search_table)


def cmd_xy_item(args) -> int:
    page = open_target(args)
    try:
        detail = xy_item.run(page, args.item_id)
    finally:
        page.close()
    return emit_ok(detail, args.format, _item_table)


# ---------- 入口 ----------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fishbook",
        description="闲鱼 / 小红书数据抓取工具：连接别人的浏览器，只做只读抓取",
    )
    parser.add_argument(
        "--cdp",
        help="CDP 地址，支持 http://host:port、host:port、ws://…/devtools/page/<id>"
        "（优先级：--cdp > $FISHBOOK_CDP > http://127.0.0.1:9222）",
    )
    parser.add_argument(
        "-f", "--format", choices=FORMATS, default="json", help="输出格式（默认 json）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="自检：端点 / target / 登录态 / mtop / 最小搜索")
    check.add_argument("--keyword", default="test", help="最小搜索用的关键词，传空串跳过该步")

    xy = sub.add_parser("xy", help="闲鱼")
    xy_sub = xy.add_subparsers(dest="xy_command", required=True)

    search = xy_sub.add_parser("search", help="关键词搜索")
    search.add_argument("keyword")
    search.add_argument("--limit", type=int, default=30, help="条数上限（默认 30，最多 60）")
    search.add_argument("--min-price", type=int, help="最低价（元）")
    search.add_argument("--max-price", type=int, help="最高价（元）")
    search.add_argument("--province", default="", help="省份筛选，如 浙江")
    search.add_argument("--city", default="", help="城市筛选，如 杭州")

    item = xy_sub.add_parser("item", help="商品详情")
    item.add_argument("item_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            return cmd_check(args)
        if getattr(args, "xy_command", None) == "search":
            return cmd_xy_search(args)
        if getattr(args, "xy_command", None) == "item":
            return cmd_xy_item(args)
        raise UsageError("未知子命令")
    except FishbookError as exc:
        emit_error(exc)
        return exc.exit_code
    except KeyboardInterrupt:
        log("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
