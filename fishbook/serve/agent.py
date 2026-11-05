"""客服 agent 决策管线：敏感闸门 → FAQ → 议价 → LLM(DeepSeek) → 输出校验。"""

from __future__ import annotations

import json
import re

from ..util import log

SENSITIVE = ("退款", "投诉", "举报", "骗子", "报警", "线下交易", "加微信", "加qq")
PHONE_RE = re.compile(r"1[3-9]\d{9}")
URL_RE = re.compile(r"https?://|www\.")

SYSTEM_TEMPLATE = """你是闲鱼卖家「{shop}」的客服，正在回复买家。只输出 JSON。

【商品】{title}
价格：{price}
【话术要求】
{prompt}
【常见问题】
{faq}
【议价底线】低于 {floor} 元不卖。买家砍价低于底线时礼貌拒绝：{floor_reply}

【输出格式】
{{"reply": "给买家的回复文字", "intent": "chat|bargain|buy|payment|other", "need_human": false}}

规则：
- reply 用口语化中文，2 句以内，像真人卖家，不用表情符号堆砌
- 涉及退款/投诉/纠纷 → need_human=true，reply 留空
- 不确定怎么答 → need_human=true
- 拍下付款 intent=payment
"""


def faq_match(item: dict, text: str) -> str | None:
    try:
        entries = json.loads(item.get("faq") or "[]")
    except ValueError:
        return None
    for entry in entries:
        keywords = [k.strip() for k in str(entry.get("k", "")).split("|") if k.strip()]
        if keywords and any(k in text for k in keywords):
            return str(entry.get("a", "")).strip()
    return None


def price_violation(item: dict, text: str) -> str | None:
    floor = str(item.get("price_floor") or "").strip()
    reply = str(item.get("floor_reply") or "").strip()
    if not floor or not reply:
        return None
    try:
        floor_num = float(floor)
    except ValueError:
        return None
    # 提取消息里的纯数字（过滤长度>6 的单号/手机号）
    for num in re.findall(r"\d+(?:\.\d+)?", text):
        if len(num) > 6:
            continue
        try:
            if float(num) < floor_num:
                return reply
        except ValueError:
            continue
    return None


def sensitive(text: str) -> bool:
    return any(w in text for w in SENSITIVE)


def llm_reply(item: dict, history: list[dict], api_key: str, model: str, shop: str) -> dict:
    """DeepSeek 调用；失败/不确定都返回 need_human 形态。"""
    try:
        from openai import OpenAI
    except ImportError:
        return {"reply": "", "intent": "other", "need_human": True, "error": "openai 未安装"}

    faq_lines = "\n".join(f"- 问：{e.get('k')} 答：{e.get('a')}" for e in _safe_faq(item))
    system = SYSTEM_TEMPLATE.format(
        shop=shop,
        title=item.get("title") or "（见对话）",
        price=item.get("price") or "见商品页",
        prompt=item.get("prompt") or "礼貌、简洁、如实回答",
        faq=faq_lines or "（无）",
        floor=item.get("price_floor") or "（未设）",
        floor_reply=item.get("floor_reply") or "抱歉，这个价格出不了",
    )
    messages = [{"role": "system", "content": system}]
    for m in history[-10:]:
        role = "assistant" if m["side"] == "me" else "user"
        messages.append({"role": role, "content": m["text"]})

    try:
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(
            model=model or "deepseek-chat",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.6,
            timeout=30,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001 - 网络/解析失败都转人工
        log(f"LLM 调用失败: {exc}")
        return {"reply": "", "intent": "other", "need_human": True, "error": str(exc)[:120]}

    reply = str(data.get("reply") or "").strip()
    # 输出校验：手机号/链接直接拦
    if reply and (PHONE_RE.search(reply) or URL_RE.search(reply)):
        return {
            "reply": "",
            "intent": data.get("intent", "other"),
            "need_human": True,
            "error": "回复含手机号或链接，已拦截",
        }
    return {
        "reply": reply,
        "intent": str(data.get("intent") or "other"),
        "need_human": bool(data.get("need_human")) or not reply,
    }


def _safe_faq(item: dict) -> list[dict]:
    try:
        entries = json.loads(item.get("faq") or "[]")
        return [e for e in entries if isinstance(e, dict)]
    except ValueError:
        return []


def decide(item: dict, history: list[dict], settings: dict) -> dict:
    """返回 {action: reply|human|ignore, text, decision, detail}。

    history: [{side: me|other, text}]，最后一条是触发消息。
    """
    last = history[-1]["text"] if history else ""
    if not last or history[-1]["side"] == "me":
        return {"action": "ignore", "text": "", "decision": "gate", "detail": "最后一条是自己发的"}

    if sensitive(last):
        return {
            "action": "human",
            "text": "",
            "decision": "gate",
            "detail": f"命中敏感词: {last[:40]}",
        }

    faq = faq_match(item, last)
    if faq:
        return {"action": "reply", "text": faq, "decision": "faq", "detail": "FAQ 命中"}

    floor = price_violation(item, last)
    if floor:
        return {"action": "reply", "text": floor, "decision": "price_floor", "detail": "低于底线"}

    if not settings.get("api_key"):
        return {"action": "human", "text": "", "decision": "gate", "detail": "未配置 LLM key"}

    result = llm_reply(
        item, history, settings["api_key"], settings.get("model", ""), settings.get("shop", "")
    )
    if result.get("need_human"):
        return {
            "action": "human",
            "text": "",
            "decision": "llm_human",
            "detail": json.dumps(result, ensure_ascii=False),
        }
    return {
        "action": "reply",
        "text": result["reply"],
        "decision": "llm",
        "detail": json.dumps({"intent": result.get("intent")}, ensure_ascii=False),
    }
