"""FastAPI 应用：Web UI + REST API。"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from ..sites.xy import im, personal
from .service import STATIC, Service


def create_app(service: Service) -> FastAPI:
    app = FastAPI(title="fishbook serve", docs_url=None, redoc_url=None)
    store = service.store

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/shop")
    async def shop_name():
        """从闲鱼个人主页读取当前账号昵称，作为 prompt 里的店铺名。"""
        cached = store.get_setting("shop")
        if cached:
            return {"ok": True, "shop": cached, "cached": True}
        try:
            name = service.worker.submit(lambda ctx: personal.shop_name(ctx.personal_page))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)[:160]}, status_code=502)
        if name:
            store.set_setting("shop", name)
        return {"ok": True, "shop": name or "", "cached": False}

    @app.get("/api/status")
    def status():
        return {
            "paused": service.paused.is_set(),
            "dry_run_default": True,
            "items": store.list_items(),
            "conversations": store.list_conversations(),
            "settings": {
                k: (v if k != "api_key" else ("已配置" if v else ""))
                for k, v in service.settings().items()
            },
            "api_key_set": bool(service.settings().get("api_key")),
        }

    @app.get("/api/items/discover")
    async def discover_items():
        """从闲鱼「我发布的」读取在售商品列表（供 UI 勾选添加）。"""
        try:
            items = service.worker.submit(lambda ctx: personal.discover(ctx.personal_page))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=502)
        known = {i["item_id"] for i in store.list_items()}
        for item in items:
            item["added"] = item["item_id"] in known
        return {"ok": True, "items": items}

    @app.post("/api/items")
    async def add_item(request: Request):
        body = await request.json()
        item_id = str(body.get("item_id") or "").strip()
        if not item_id:
            return JSONResponse({"ok": False, "error": "item_id 必填"}, status_code=400)
        store.upsert_item(item_id, str(body.get("title") or ""), str(body.get("price") or ""))
        return {"ok": True}

    @app.post("/api/items/{item_id}")
    async def update_item(item_id: str, request: Request):
        body = await request.json()
        if body.get("faq") is not None and not isinstance(body["faq"], str):
            body["faq"] = json.dumps(body["faq"], ensure_ascii=False)
        if not store.get_item(item_id):
            store.upsert_item(item_id)
        store.update_item(item_id, body)
        return {"ok": True, "item": store.get_item(item_id)}

    @app.post("/api/items/{item_id}/secrets")
    async def add_secrets(item_id: str, request: Request):
        body = await request.json()
        lines = [ln for ln in str(body.get("contents") or "").splitlines()]
        added = store.add_secrets(item_id, lines)
        return {"ok": True, "added": added, "pool": store.pool_stats(item_id)}

    @app.get("/api/items/{item_id}/pool")
    def pool(item_id: str):
        return store.pool_stats(item_id)

    @app.post("/api/conversations/{name}/item")
    async def assign_item(name: str, request: Request):
        body = await request.json()
        item_id = str(body.get("item_id") or "").strip()
        store.set_conversation(name, item_id=item_id)
        return {"ok": True}

    @app.post("/api/conversations/{name}/mode")
    async def set_mode(name: str, request: Request):
        body = await request.json()
        mode = body.get("mode")
        if mode not in ("auto", "manual"):
            return JSONResponse({"ok": False, "error": "mode 只能是 auto/manual"}, status_code=400)
        store.set_conversation(name, mode=mode)
        return {"ok": True}

    @app.get("/api/messages/{name}")
    def messages(name: str):
        return store.list_messages(name)

    @app.post("/api/conversations/{name}/send")
    async def manual_send(name: str, request: Request):
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "text 必填"}, status_code=400)
        opened = service.worker.submit(lambda ctx: im.open_conversation(ctx.page, name))
        if not opened.get("ok"):
            return JSONResponse({"ok": False, "error": f"打开会话失败: {opened}"}, status_code=400)
        result = service.worker.submit(lambda ctx: im.send(ctx.page, text))
        if result.get("ok"):
            store.add_message(name, "out", text, "manual", "UI 手动发送")
            store.set_conversation(name, mode="manual")
            return {"ok": True}
        return JSONResponse({"ok": False, "error": f"发送失败: {result}"}, status_code=500)

    @app.post("/api/settings")
    async def update_settings(request: Request):
        body = await request.json()
        for key in ("api_key", "model", "shop"):
            if body.get(key) is not None and body.get(key) != "":
                store.set_setting(key, str(body[key]))
        return {"ok": True}

    @app.post("/api/guard")
    async def guard(request: Request):
        body = await request.json()
        if body.get("paused") is True:
            service.paused.set()
        else:
            service.paused.clear()
        return {"ok": True, "paused": service.paused.is_set()}

    @app.post("/api/watch/trigger")
    async def trigger():
        """手动触发一轮扫描（调试用）。"""
        service._tick()
        return {"ok": True}

    return app
