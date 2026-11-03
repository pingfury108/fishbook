"""MTOP 调用的公共部分：模板渲染、错误码分类、token 刷新、风控检测。"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

from ...cdp import CDP, MTOP_READY_JS, is_transient
from ...errors import AuthError, BusinessError, CDPError, EndpointError, RiskError

JS_DIR = Path(__file__).resolve().parent

RISK_JS = """({
  url: location.href,
  text: document.body ? document.body.innerText.slice(0, 4000) : ''
})"""
# 页面文案类信号
RISK_TEXT_MARKERS = ("验证码", "安全验证", "异常访问", "网络不见了", "请完成验证", "访问受限")
# URL / 惩罚页信号（x5sec 风控拦截会跳到 .../_____tmd_____/punish）
RISK_URL_MARKERS = ("_____tmd_____", "x5secdata", "/punish")

# 注意 FAIL_SYS_TOKEN_EXOIRED 是服务端真实拼写，两个都收
TOKEN_FAILS = ("FAIL_SYS_TOKEN_EMPTY", "FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EXPIRED")
SESSION_FAILS = ("FAIL_SYS_SESSION_EXPIRED",)
X5SEC_URL = "https://h5api.m.goofish.com/"


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


def ret_code(envelope: dict) -> str:
    ret = envelope.get("ret")
    if isinstance(ret, str):
        return ret
    if isinstance(ret, list) and ret:
        return str(ret[0])
    return ""


def ensure_ready(page: CDP, url: str, exact: bool = False) -> None:
    """页面已在站点且 mtop 就绪就复用，否则导航到 url 等它就绪。

    exact=True 时还要求当前地址就是 url（详情页这种和页面状态绑定的场景）。
    """
    current = page.url() if page else ""
    on_site = "goofish.com" in current
    same_page = current.split("#")[0].rstrip("/") == url.rstrip("/")
    if on_site and (same_page or not exact) and page.evaluate(MTOP_READY_JS, retries=2):
        return
    log(f"加载页面 {url}")
    page.goto(url)


def has_punish_cookie(page: CDP) -> bool:
    """h5api 域上的 httpOnly `x5sec` 是 x5sec 风控惩罚的硬信号（document.cookie 看不到）。"""
    cookies = page.send("Network.getCookies", {"urls": [X5SEC_URL]}).get("cookies") or []
    return any(cookie.get("name") == "x5sec" for cookie in cookies)


def check_risk(page: CDP) -> None:
    probe = page.evaluate(RISK_JS, retries=2) or {}
    if not isinstance(probe, dict):
        return
    url = probe.get("url") or ""
    text = probe.get("text") or ""
    hits = [marker for marker in RISK_TEXT_MARKERS if marker in text]
    hits += [marker for marker in RISK_URL_MARKERS if marker in url]
    if hits:
        raise RiskError(f"触发风控（{'、'.join(hits)}）: {url[:120]}")


def call(page: CDP, js: str, refresh_url: str) -> dict:
    """执行一次 mtop 调用，处理错误码分类与 token 刷新（重试 1 次）。"""
    for attempt in (1, 2):
        try:
            envelope = page.evaluate(js)
        except CDPError as exc:
            # 页面自己发生了客户端跳转：重新加载后重试一次
            if attempt == 1 and is_transient(exc):
                log("页面上下文被销毁，重新加载后重试一次")
                page.goto(refresh_url)
                sleep_jitter()
                continue
            raise
        if not isinstance(envelope, dict):
            raise EndpointError(f"页面返回了非对象结果: {envelope!r}")
        ret = ret_code(envelope)
        check_risk(page)

        if ret.startswith("SUCCESS"):
            data = envelope.get("data") or {}
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError as exc:
                    raise BusinessError("mtop 返回的 data 不是合法 JSON") from exc
            if not isinstance(data, dict):
                raise BusinessError(f"mtop 返回的 data 结构异常: {type(data).__name__}")
            return data

        if envelope.get("error") and not ret:
            raise EndpointError(f"页面内调用失败: {envelope['error']}")

        if ret.startswith(TOKEN_FAILS) and attempt == 1:
            log(f"{ret}: H5 token 缺失/过期，重新加载页面刷新 cookie")
            page.goto(refresh_url)
            sleep_jitter()
            continue

        if ret.startswith(SESSION_FAILS):
            raise AuthError(f"登录态失效，请在浏览器中重新登录: {ret}")

        # TIMEOUT::接口超时 这类看不清的错误，配合 x5sec 痕迹才判定为风控
        if ret.startswith("TIMEOUT") and has_punish_cookie(page):
            raise RiskError(f"触发 x5sec 风控（{ret or '未知'}），需等冷却或人工验证")

        raise BusinessError(f"MTOP 业务错误: {ret or '未知'}")

    raise BusinessError("MTOP 调用重试后仍失败")
