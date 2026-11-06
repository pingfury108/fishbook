"""浏览器操作队列：所有页面读/写动作在单线程里串行执行。

原因：页面级 CDP 端点同一时刻只允许一个客户端；UI 自动化（打字/点击）并发必乱。
另外：绝不调用 Page.bringToFront —— 那会把浏览器窗口抢到最前，打扰用户。watcher / agent / sender / UI 手动操作全部 submit 到这里。

worker 拥有两个长驻连接（worker 线程内使用）：
- ctx.page        IM 页（goofish.com/im），消息轮询与发送
- ctx.personal_page 闲鱼「我发布的」页（goofish.com/personal），在售商品读取
"""

from __future__ import annotations

import contextlib
import queue
import threading
from collections.abc import Callable
from typing import Any

from ..sites.xy import im, personal


class BrowserWorker:
    def __init__(self, cdp_endpoint: str) -> None:
        self.endpoint = cdp_endpoint
        self._queue: queue.Queue = queue.Queue()
        self._pages: dict[str, Any] = {}  # kind -> CDP 连接，worker 线程独占
        self._thread = threading.Thread(target=self._run, name="browser-worker", daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[Any], Any]) -> Any:
        """同步提交一个任务，fn(ctx) 里可取 ctx.page / ctx.seller_page。返回结果或抛原异常。"""
        box: queue.Queue = queue.Queue(maxsize=1)

        def task(ctx: Any) -> None:
            try:
                box.put(("ok", fn(ctx)))
            except Exception as exc:  # noqa: BLE001 - 原样抛回调用方
                box.put(("err", exc))

        self._queue.put(task)
        status, value = box.get()
        if status == "err":
            raise value
        return value

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            ctx = _Context(self)
            with contextlib.suppress(Exception):  # 单任务失败不拖垮队列
                task(ctx)

    # ---- worker 线程内使用 ----

    def page_for(self, kind: str):
        """按 kind 取长驻连接（im / personal），坏了自动重建。"""
        ensure = im.ensure_page if kind == "im" else personal.ensure_page
        current = self._pages.get(kind)
        try:
            self._pages[kind] = ensure(self.endpoint, current)
        except Exception:
            with contextlib.suppress(Exception):
                current.close() if current else None
            raise
        return self._pages[kind]


class _Context:
    def __init__(self, worker: BrowserWorker) -> None:
        self._worker = worker

    @property
    def page(self):
        return self._worker.page_for("im")

    @property
    def personal_page(self):
        return self._worker.page_for("personal")
