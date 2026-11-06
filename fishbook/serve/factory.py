"""uvicorn --reload 用的 app 工厂。

服务实例（BrowserWorker + watcher 线程）必须在 worker 进程里创建：
reload 时 uvicorn 杀掉 worker 再起新的，线程跟着重建，不会留下僵尸轮询。
配置通过环境变量传入（CLI 在启动前写入）。
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path

from fastapi import FastAPI

from .app import create_app
from .service import Service
from .worker import BrowserWorker  # noqa: F401  (导入即校验依赖)

_service: Service | None = None


def build_service() -> Service:
    global _service
    if _service is not None:
        return _service
    endpoint = os.environ.get("FISHBOOK_CDP_ADDR", "http://127.0.0.1:9222")
    state = os.environ.get("FISHBOOK_STATE", ".fishbook/state.db")
    poll = (
        float(os.environ.get("FISHBOOK_POLL_MIN", "25")),
        float(os.environ.get("FISHBOOK_POLL_MAX", "45")),
    )
    _service = Service(endpoint, Path(state), poll_interval=poll)
    _service.start()
    atexit.register(stop_service)
    return _service


def stop_service() -> None:
    global _service
    if _service is not None:
        _service.stop()
        _service = None


def app_factory() -> FastAPI:
    return create_app(build_service())
