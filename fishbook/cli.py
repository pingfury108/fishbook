"""命令行入口：check / xy search|item / red search|item|check。"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import cdp
from .cdp import MTOP_READY_JS
from .errors import EXIT_ENDPOINT, EXIT_OK, EndpointError, FishbookError, UsageError
from .sites.red import note as red_note
from .sites.red import search as red_search
from .sites.red import web as red_web
from .sites.xy import item as xy_item
from .sites.xy import search as xy_search
from .sites.xy.mtop import has_punish_cookie, log

DOMAINS = {"xy": "goofish.com", "red": "xiaohongshu.com"}
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


def _red_search_table(notes: list[dict]) -> None:
    print_table(
        [
            {
                "note_id": note["note_id"],
                "likes": note["likes"],
                "type": note["type"],
                "title": note["title"],
                "author": note["author"],
            }
            for note in notes
        ],
        [
            ("note_id", "NOTE_ID"),
            ("likes", "LIKES"),
            ("type", "TYPE"),
            ("title", "TITLE"),
            ("author", "AUTHOR"),
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


def _check_head(base, ws_url, record, site: str) -> str:
    """端点 + page target 的通用检查，返回要连接的 ws 地址。"""
    version = cdp.http_get_json(base, "/json/version")
    record("端点连通", True, version.get("Browser") or base)

    pages = cdp.page_targets(base)
    record("page target", bool(pages), f"{len(pages)} 个")
    for target in pages:
        print(f"    - {target.get('url')}", file=sys.stderr)
    if ws_url:
        return ws_url

    matched = [t for t in pages if DOMAINS[site] in (t.get("url") or "")]
    chosen = matched[0] if matched else (pages[0] if pages else None)
    if chosen is None or not chosen.get("webSocketDebuggerUrl"):
        raise EndpointError("没有任何可用的 page target")
    print(f"    选中: {chosen.get('url')}", file=sys.stderr)
    return chosen["webSocketDebuggerUrl"]


def _record_factory(checks: list[dict]):
    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"{'✓' if ok else '✗'} {name}: {detail}", file=sys.stderr, flush=True)

    return record


def _check_xy(page, record, current: str, keyword: str) -> None:
    # cookie 按站点查，否则标签停在 chrome:// 时永远查不到
    cookie_url = current if "goofish.com" in current else "https://www.goofish.com/"
    cookies = page.send("Network.getCookies", {"urls": [cookie_url]})
    names = {c.get("name") for c in cookies.get("cookies") or []}
    for name in ("unb", "tracknick"):
        record(
            f"cookie {name}", name in names, "存在" if name in names else f"缺失（{cookie_url}）"
        )

    ready = bool(page.evaluate(MTOP_READY_JS, retries=2))
    record("mtop SDK", ready, "就绪" if ready else "未就绪（先把该标签打开 goofish.com）")

    # 只是惩罚期痕迹，不代表当前请求一定被拦，仅作提示
    punish = has_punish_cookie(page)
    record("x5sec 痕迹", True, "存在（曾被惩罚，注意限速）" if punish else "无")

    if ready and keyword:
        try:
            data = xy_search.fetch_page(page, keyword)
            entries = data.get("resultList") or []
            record("最小搜索", bool(entries), f"{len(entries)} 条（keyword={keyword}）")
        except FishbookError as exc:
            record("最小搜索", False, exc.message)


def _check_red(page, record, keyword: str) -> None:
    names = red_web.cookie_names(page)
    record(
        "cookie web_session", "web_session" in names, "存在" if "web_session" in names else "缺失"
    )
    # 以页面为准：登录墙文案才说明真的没登录（last_web_session 可能是残留）
    login_wall = bool(
        page.evaluate(
            "!!(document.body && /登录后查看|请登录/.test(document.body.innerText))", retries=2
        )
    )
    record("页面无登录墙", not login_wall, "命中登录墙（需重新登录）" if login_wall else "正常")

    has_input = bool(page.evaluate("!!document.querySelector('#search-input')", retries=2))
    record(
        "搜索输入框 #search-input",
        has_input,
        "存在" if has_input else "不存在（先访问 xiaohongshu.com/explore）",
    )

    if keyword:
        try:
            notes = red_search.run(page, keyword, limit=5)
            record("最小搜索", bool(notes), f"{len(notes)} 条（keyword={keyword}）")
        except FishbookError as exc:
            record("最小搜索", False, exc.message)


def cmd_check(args) -> int:
    site = args.site or ""
    raw = args.cdp or os.environ.get("FISHBOOK_CDP") or None
    base, ws_url = cdp.normalize_endpoint(raw)
    checks: list[dict] = []
    record = _record_factory(checks)
    ws_url = _check_head(base, ws_url, record, site or "xy")

    page = cdp.CDP(ws_url).connect()
    try:
        current = page.url()
        record("当前 URL", bool(current), current)
        if site == "xy":
            _check_xy(page, record, current, args.keyword)
        elif site == "red":
            _check_red(page, record, args.keyword)
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


def cmd_red_search(args) -> int:
    page = open_target(args, "red")
    try:
        notes = red_search.run(page, args.keyword, limit=args.limit, max_scrolls=args.max_scrolls)
    finally:
        page.close()
    return emit_ok(notes, args.format, _red_search_table)


def cmd_red_note(args) -> int:
    page = open_target(args, "red")
    try:
        note = red_note.run(page, args.target)
    finally:
        page.close()
    return emit_ok(note, args.format, _item_table)


def cmd_red_check(args) -> int:
    raw = args.cdp or os.environ.get("FISHBOOK_CDP") or None
    base, ws_url = cdp.normalize_endpoint(raw)
    checks: list[dict] = []
    record = _record_factory(checks)
    ws_url = _check_head(base, ws_url, record, "red")

    page = cdp.CDP(ws_url).connect()
    try:
        current = page.url()
        record("当前 URL", bool(current), current)
        names = red_web.cookie_names(page)
        record(
            "cookie web_session",
            "web_session" in names,
            "存在" if "web_session" in names else "缺失",
        )
        # 出现 last_web_session 说明登录态被风控降级过，需要重新登录
        demoted = "last_web_session" in names
        record("登录态未降级", not demoted, "被降级（需重新登录）" if demoted else "正常")

        if args.keyword:
            try:
                notes = red_search.run(page, args.keyword, limit=5)
                record("最小搜索", bool(notes), f"{len(notes)} 条（keyword={args.keyword}）")
            except FishbookError as exc:
                record("最小搜索", False, exc.message)
    finally:
        page.close()

    ok = all(check["ok"] for check in checks)
    print(json.dumps({"ok": ok, "data": {"checks": checks}}, ensure_ascii=False))
    return EXIT_OK if ok else EXIT_ENDPOINT


# ---------- 入口 ----------


def cmd_serve(args) -> int:
    try:
        import uvicorn
    except ImportError:
        log("缺少依赖：请先 uv sync（fastapi/uvicorn/openai）")
        return EXIT_ENDPOINT

    from pathlib import Path

    from .serve.app import create_app
    from .serve.service import Service

    cdp_raw = args.cdp or os.environ.get("FISHBOOK_CDP") or None
    base, _ = cdp.normalize_endpoint(cdp_raw)
    service = Service(
        cdp_raw or base, Path(args.state), poll_interval=(args.poll_min, args.poll_max)
    )
    service.start()

    uvicorn.run(create_app(service), host=args.host, port=args.port, log_level="warning")
    return EXIT_OK


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

    check = sub.add_parser("check", help="自检：端点 / target / 当前 URL / cookie，可叠加站点细节")
    check.add_argument(
        "--site", choices=("xy", "red"), help="追加站点细节：xy=mtop 就绪；red=输入框/登录态"
    )
    check.add_argument("--keyword", default="", help="最小搜索用的关键词，留空跳过该步")

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

    red = sub.add_parser("red", help="小红书")
    red_sub = red.add_subparsers(dest="red_command", required=True)

    red_search_parser = red_sub.add_parser("search", help="关键词搜索（SSR 首屏）")
    red_search_parser.add_argument("keyword")
    red_search_parser.add_argument(
        "--limit", type=int, default=20, help="条数上限（默认 20，最多 60）"
    )
    red_search_parser.add_argument(
        "--max-scrolls", type=int, default=15, help="最多滚动轮数（默认 15）"
    )

    red_note_parser = red_sub.add_parser("note", help="笔记详情")
    red_note_parser.add_argument(
        "target", help="完整 note URL（推荐，带 xsec_token）或 24 位 note_id"
    )

    serve = sub.add_parser("serve", help="托管服务：Web UI + 客服 agent + 自动发货（虚拟）")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8756)
    serve.add_argument("--poll-min", type=float, default=25.0, help="IM 轮询最小间隔秒")
    serve.add_argument("--poll-max", type=float, default=45.0, help="IM 轮询最大间隔秒")
    serve.add_argument("--state", default=".fishbook/state.db", help="SQLite 路径")
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
        if getattr(args, "red_command", None) == "search":
            return cmd_red_search(args)
        if getattr(args, "red_command", None) == "note":
            return cmd_red_note(args)
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "serve":
            return cmd_serve(args)
        raise UsageError("未知子命令")
    except FishbookError as exc:
        emit_error(exc)
        return exc.exit_code
    except KeyboardInterrupt:
        log("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
