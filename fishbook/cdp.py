"""最小 CDP 客户端：endpoint 归一化 / target 发现 / WS 收发 / Runtime.evaluate。

设计约束（对应方案 §3）：
- 同步 API，串行调用，不用 asyncio；
- 只做 request/response，不订阅事件（返回里没有 id 的帧直接跳过）；
- 页面级 WS 端点同一时刻只允许一个客户端，别同时开 DevTools。
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.request
from typing import Any, Self

from websockets.sync.client import connect as ws_connect

from .errors import CDPError, CommandTimeout, EndpointError

DEFAULT_ENDPOINT = "http://127.0.0.1:9222"
CONNECT_TIMEOUT = 5.0
COMMAND_TIMEOUT = 30.0
MAX_FRAME = 64 * 1024 * 1024

# 页面就绪判定：DOM 完整 + mtop SDK 可调用，两件事一起覆盖（方案 §4）
READY_JS = """(async () => {
  const t = Date.now();
  while (Date.now() - t < 1500) {
    if (document.readyState === 'complete'
        && window.lib && window.lib.mtop
        && typeof window.lib.mtop.request === 'function') return true;
    await new Promise(r => setTimeout(r, 200));
  }
  return false;
})()"""

MTOP_READY_JS = "!!(window.lib && window.lib.mtop && typeof window.lib.mtop.request === 'function')"

_TRANSIENT_MARKERS = ("execution context was destroyed", "cannot find context", "context not found")


def normalize_endpoint(raw: str | None) -> tuple[str, str | None]:
    """把三种写法统一成 (http 基址, 直接指定的 ws 地址或 None)。"""
    text = (raw or "").strip() or DEFAULT_ENDPOINT
    if text.startswith(("ws://", "wss://")):
        base, sep, _rest = text.partition("/devtools/")
        if not sep:
            raise EndpointError(f"WS 地址缺少 /devtools/ 路径: {text}")
        return base, text
    if not text.startswith(("http://", "https://")):
        text = "http://" + text
    return text.rstrip("/"), None


def http_get_json(base: str, path: str, timeout: float = CONNECT_TIMEOUT) -> Any:
    url = f"{base}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise EndpointError(f"GET {url} 失败: {exc}") from exc


def list_targets(base: str) -> list[dict]:
    items = http_get_json(base, "/json/list")
    if not isinstance(items, list):
        raise EndpointError(f"{base}/json/list 返回格式异常")
    return items


def page_targets(base: str) -> list[dict]:
    return [t for t in list_targets(base) if t.get("type") == "page"]


def find_target_url(base: str, domain: str) -> str | None:
    """返回命中 domain 的 page target ws 地址，没有则 None（不回退不报错）。"""
    for target in page_targets(base):
        if domain in (target.get("url") or "") and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"]
    return None


def open_tab(base: str, url: str) -> dict:
    """新开一个标签页，返回 {ws_url, target_id}（Chrome 128+ 要求 PUT）。"""
    from urllib.parse import quote

    req = urllib.request.Request(f"{base}/json/new?url={quote(url, safe='')}", method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise EndpointError(f"新开标签页失败 {url}: {exc}") from exc
    return {"ws_url": info["webSocketDebuggerUrl"], "target_id": info.get("id", "")}


def close_tab(base: str, target_id: str) -> None:
    """关掉自己开出来的标签页；关不掉就当无事发生。"""
    if not target_id:
        return
    try:
        urllib.request.urlopen(f"{base}/json/close/{target_id}", timeout=CONNECT_TIMEOUT)
    except (urllib.error.URLError, OSError):
        pass


def resolve_target(base: str, domain: str) -> tuple[str, str]:
    """挑一个可用的 page target，返回 (ws 地址, 说明)。

    优先命中 domain；否则若只有一个 page target（例如 chrome://newtab）就回退到它，
    由调用方导航过去；多个 page target 且都不匹配时报错并列出，避免猜错标签。
    """
    pages = [t for t in page_targets(base) if t.get("webSocketDebuggerUrl")]
    for target in pages:
        if domain in (target.get("url") or ""):
            return target["webSocketDebuggerUrl"], f"匹配 {domain}: {target.get('url')}"
    if len(pages) == 1:
        return pages[0]["webSocketDebuggerUrl"], f"回退到唯一 page target: {pages[0].get('url')}"
    listing = "\n".join(f"  - {t.get('url')}" for t in pages) or "  (没有任何 page target)"
    raise EndpointError(
        f"没有匹配 {domain} 的 page target，且存在多个候选无法自动选择。实际存在:\n{listing}"
        "\n可用 --cdp ws://…/devtools/page/<id> 指定具体标签"
    )


def is_transient(exc: Exception) -> bool:
    """导航/重定向期间上下文会被销毁，这类错误可安全重试。"""
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


class CDP:
    """单条页面级 WS 连接，串行发送命令。"""

    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self._ws = None
        self._next_id = 0

    # ---- 生命周期 ----

    def connect(self) -> CDP:
        try:
            # 新版 websockets（sync 实现）默认不发 Origin 头，
            # 所以不需要 suppress_origin，也不会被 Chrome 以 origin 为由拒绝。
            # proxy=None：CDP 永远直连，不要受 https_proxy 等环境变量影响。
            self._ws = ws_connect(
                self.ws_url,
                open_timeout=CONNECT_TIMEOUT,
                close_timeout=2,
                max_size=MAX_FRAME,
                proxy=None,
            )
        except Exception as exc:  # 连接失败统一归一化
            raise EndpointError(f"WS 连接失败 {self.ws_url}: {exc}") from exc
        return self

    def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):  # 关闭失败无需关心
                self._ws.close()
            self._ws = None

    def __enter__(self) -> Self:
        return self.connect()

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- 协议 ----

    def send(
        self, method: str, params: dict | None = None, timeout: float = COMMAND_TIMEOUT
    ) -> dict:
        if self._ws is None:
            raise CDPError("WS 未连接")
        self._next_id += 1
        msg_id = self._next_id
        payload = {"id": msg_id, "method": method, "params": params or {}}
        try:
            self._ws.send(json.dumps(payload))
        except Exception as exc:
            raise CDPError(f"发送 {method} 失败: {exc}") from exc

        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise CommandTimeout(f"{method} 超时 {timeout:.0f}s")
            try:
                raw = self._ws.recv(timeout=remain)
            except TimeoutError as exc:
                raise CommandTimeout(f"{method} 超时 {timeout:.0f}s") from exc
            except Exception as exc:
                raise CDPError(f"{method} 接收失败: {exc}") from exc
            try:
                msg = json.loads(raw)
            except ValueError as exc:
                raise CDPError(f"{method} 收到非 JSON 帧: {raw[:200]!r}") from exc
            if msg.get("id") != msg_id:
                continue  # 事件帧或过期响应，直接丢弃
            if "error" in msg:
                raise CDPError(f"{method} 返回错误: {msg['error']}")
            return msg.get("result") or {}

    def evaluate(self, js: str, timeout: float = COMMAND_TIMEOUT, retries: int = 0) -> Any:
        """在页面主世界执行 JS，返回值必须是可 JSON 化的纯对象。

        retries 只应用于幂等的探测（location / 就绪 / 风控文本），
        mtop 请求默认不重试，避免重复请求抬高风控风险。
        """
        attempt = 0
        while True:
            try:
                return self._evaluate_once(js, timeout)
            except CDPError as exc:
                if attempt >= retries or not is_transient(exc):
                    raise
            attempt += 1
            time.sleep(0.2)

    def _evaluate_once(self, js: str, timeout: float) -> Any:
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": js,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        details = result.get("exceptionDetails")
        if details:
            exception = details.get("exception") or {}
            text = exception.get("description") or details.get("text") or "JS 执行异常"
            raise CDPError(f"JS 执行异常: {text}")
        return (result.get("result") or {}).get("value")

    # ---- 便捷方法 ----

    def url(self) -> str:
        return self.evaluate("location.href", retries=2) or ""

    def goto(self, url: str, ready_timeout: float = 12.0) -> None:
        self.send("Page.navigate", {"url": url})
        if not self.wait_ready(ready_timeout):
            raise EndpointError(f"页面未就绪（{ready_timeout:.0f}s 内 mtop SDK 未出现）: {url}")

    def wait_ready(self, total: float = 8.0) -> bool:
        """轮询页面内 READY_JS；导航期间上下文被销毁视为可重试。"""
        deadline = time.monotonic() + total
        while time.monotonic() < deadline:
            try:
                if self.evaluate(READY_JS, timeout=15.0):
                    return True
            except CommandTimeout:
                raise
            except CDPError as exc:
                if not is_transient(exc):
                    raise
            time.sleep(0.3)
        return False
