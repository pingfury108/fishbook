"""对外发送内容的护栏：卡密、手机号、站外链接一律不放行。

原则：卡密只由发货模板注入，永远不经过 LLM，也永远不出现在 prompt 里；
LLM 的回复在发出去之前必须再过一遍这里。
"""

from __future__ import annotations

import re

PHONE_RE = re.compile(r"1[3-9]\d{9}")
# 域名/链接：商品信息里本来就有的（虚拟商品常常就是卖某个站/App）予以放行
LINK_RE = re.compile(
    r"(?:https?://[^\s，。；]+|(?:[a-z0-9-]+\.)+(?:cn|com|net|org|io|cc|xyz|top|site)(?:/[^\s，。；]*)?)",
    re.IGNORECASE,
)
MASK = "****"

# 套卡密：买家在聊天里要卡密/兑换码/密钥（付款前套话的主路径）
SECRET_ASK_RE = re.compile(
    r"(卡密|卡号|兑换码|激活码|密钥|秘钥|序列号|license|注册码|验证码|密码|口令|礼包码)",
    re.IGNORECASE,
)

# 提示注入 / 套 prompt：让模型吐系统指令、改规则、扮演别的角色
INJECTION_RE = re.compile(
    r"("
    r"忽略(之前|以上|上面|前面)?(的)?(所有)?(指令|提示|rules?|instructions?)"
    r"|ignore\s+(all\s+)?(previous|above|prior)"
    r"|system\s*prompt|系统提示词?|你的(提示词|指令|设定|规则|配置)"
    r"|repeat\s+(the|your)\s+(above|instructions?)"
    r"|进入.{0,4}(开发者|调试|管理员)模式"
    r"|developer\s+mode|jailbreak|DAN\b"
    r"|扮演|假装你是|你现在是"
    r"|输出?(你的)?(全部)?(配置|上下文|prompt)"
    r"|告诉我你的(规则|设定|限制)"
    r")",
    re.IGNORECASE,
)
# 只是在问「有没有卡密/怎么发」也要转人工，别让模型自己解释内部机制
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")

# 形如卡密的随机串：长度 ≥ 12 且同时含字母与数字
TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{12,}")


def _norm(text: str) -> str:
    """去掉空格/连字符并小写，避免卡密被拆开写就绕过匹配。"""
    return re.sub(r"[\s\-_]+", "", (text or "").lower())


def scrub(text: str, secrets: list[str]) -> str:
    """把文本里出现的卡密替换掉（用于 prompt 与日志/审计）。"""
    out = text or ""
    for secret in secrets:
        if not secret or len(secret) < 4:
            continue
        out = re.sub(re.escape(secret), MASK, out, flags=re.IGNORECASE)
    return out


def leaks_secret(text: str, secrets: list[str]) -> str | None:
    """文本里是否夹带了卡密（忽略大小写与空格/连字符）。"""
    haystack = _norm(text)
    if not haystack:
        return None
    for secret in secrets:
        needle = _norm(secret)
        if len(needle) >= 4 and needle in haystack:
            return secret
    return None


def foreign_link(text: str, sources: str) -> str | None:
    """回复里出现商品信息之外的链接 → 认为在往站外引。"""
    allowed = {link.lower().rstrip(".") for link in LINK_RE.findall(sources or "")}
    for link in LINK_RE.findall(text or ""):
        target = link.lower().rstrip(".")
        if not any(target in one or one in target for one in allowed):
            return target
    return None


def looks_like_token(text: str, sources: str) -> str | None:
    """回复里出现「商品信息里没有的、像卡密的随机串」→ 拦。

    商品信息里本来就有的串（比如域名、型号）不算。
    """
    allowed = _norm(sources)
    for token in TOKEN_RE.findall(text or ""):
        if re.fullmatch(r"[A-Za-z]+", token) or token.isdigit():
            continue  # 纯字母/纯数字多是型号或年份，放过
        if _norm(token) in allowed:
            continue
        return token
    return None


PAYMENT_RE = re.compile(r"(已拍|拍下|付款|已付|支付成功|买下|下单了|拍好了)", re.IGNORECASE)


def is_payment_intent(text: str) -> bool:
    """买家是否在说「我付款了」。确定性判断，不依赖模型。"""
    return bool(PAYMENT_RE.search(text or ""))


def classify_inbound(text: str) -> str | None:
    """买家消息里的高风险意图：套卡密 / 提示注入。命中就转人工，不交给模型。"""
    raw = text or ""
    cleaned = ZERO_WIDTH_RE.sub("", raw)
    if INJECTION_RE.search(cleaned):
        return "疑似提示注入/套系统指令"
    if SECRET_ASK_RE.search(cleaned):
        return "买家在要卡密/兑换码等敏感信息"
    return None


def check_outgoing(text: str, sources: str, secrets: list[str]) -> str | None:
    """发送前总检查，返回拦截原因（None 表示通过）。"""
    if not (text or "").strip():
        return "空消息"
    if PHONE_RE.search(text):
        return "含手机号"
    leaked = leaks_secret(text, secrets)
    if leaked:
        return "含卡密"
    link = foreign_link(text, sources)
    if link:
        return f"含站外链接 {link}"
    token = looks_like_token(text, sources)
    if token:
        return f"含疑似卡密串 {token}"
    if len(text) > 500:
        return "消息过长"
    return None


def item_sources(item: dict) -> str:
    """商品自身信息（用于链接/随机串白名单）。"""
    return " ".join(
        str(item.get(key) or "") for key in ("title", "item_desc", "guide", "ship_note")
    )
