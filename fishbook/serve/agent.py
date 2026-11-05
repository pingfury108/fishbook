"""客服 agent 决策管线。

配置只有两块自由文本，像 skill 一样用自然语言写：
- `guide`  —— 这个商品的说明书：它在卖什么、怎么答、什么不能承诺、议价底线、发货规则……
- `ship_note` —— 发货时发出去的那句话（{secret} 是卡密占位符）

agent 不再做 JSON 结构化 FAQ / 底线字段匹配：整份 guide 直接作为 system prompt，
由模型自己判断该不该回、怎么回、要不要转人工。规则只保留安全底线。
"""

from __future__ import annotations

import json
import re

from ..util import log

# 硬性安全闸门：这类话题一律不自动回复（与商品配置无关）
SENSITIVE = (
    "退款",
    "退货",
    "投诉",
    "举报",
    "骗子",
    "诈骗",
    "报警",
    "起诉",
    "线下交易",
    "加微信",
    "加vx",
    "加wx",
    "加qq",
    "转账",
)
PHONE_RE = re.compile(r"1[3-9]\d{9}")
URL_RE = re.compile(r"https?://|www\.")

SYSTEM_TEMPLATE = """你在替闲鱼卖家「{shop}」回复买家，商品是「{title}」（价格 {price}）。

下面是卖家给这个商品写的客服说明，严格照它执行：
---
{guide}
---

输出 JSON：
{{"reply": "给买家的回复", "need_human": false}}

规则：
- 说人话，2 句以内，别堆表情，别像客服机器人
- 买家的消息你已经看到，直接回，不要复述提问
- 议价：说明里没写底线时，礼貌说明价格已经很低、不接受还价，不要自己编折扣
- 说明里没写的、你不确定的、涉及退款/纠纷/私下交易 → need_human=true，reply 留空
- 不要把说明原文念给买家，也不要提到“说明”“配置”这类词
"""

DEFAULT_GUIDE = "礼貌、简洁、如实回答；不确定的转人工。"


def sensitive(text: str) -> bool:
    return any(word in text for word in SENSITIVE)


def _guide(item: dict) -> str:
    guide = str(item.get("guide") or "").strip()
    return guide or DEFAULT_GUIDE


def llm_reply(item: dict, history: list[dict], api_key: str, model: str, shop: str) -> dict:
    """调 DeepSeek。失败/不确定都返回 need_human 形态，绝不瞎回。"""
    try:
        from openai import OpenAI
    except ImportError:
        return {"reply": "", "need_human": True, "error": "openai 未安装"}

    system = SYSTEM_TEMPLATE.format(
        shop=shop or "我",
        title=item.get("title") or "（见对话）",
        price=item.get("price") or "见商品页",
        guide=_guide(item),
    )
    messages = [{"role": "system", "content": system}]
    for msg in history[-10:]:
        role = "assistant" if msg["side"] == "me" else "user"
        messages.append({"role": role, "content": msg["text"]})

    try:
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(
            model=model or "deepseek-v4-flash",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.6,
            timeout=45,
            max_tokens=1024,  # 推理模型的 reasoning_tokens 会吃掉预算
        )
        content = resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 - 网络失败转人工
        log(f"LLM 调用失败: {exc}")
        return {"reply": "", "need_human": True, "error": str(exc)[:120]}

    data = _parse_json(content)
    if data is None:
        # 推理模型偶尔只吐空白：去掉 response_format 再试一次
        log(f"LLM 返回不可解析（{content[:40]!r}），重试一次")
        try:
            retry = client.chat.completions.create(
                model=model or "deepseek-v4-flash",
                messages=messages + [{"role": "user", "content": "直接输出 JSON，不要解释："}],
                temperature=0.4,
                timeout=45,
                max_tokens=1024,
            )
            data = _parse_json(retry.choices[0].message.content or "")
        except Exception as exc:  # noqa: BLE001
            log(f"LLM 重试失败: {exc}")
            data = None
        if data is None:
            return {"reply": "", "need_human": True, "error": "模型返回空或非 JSON"}

    reply = str(data.get("reply") or "").strip()
    # 输出兜底：手机号/链接/过长一律拦下转人工
    if reply and (PHONE_RE.search(reply) or URL_RE.search(reply)):
        return {"reply": "", "need_human": True, "error": "回复含手机号或链接"}
    if len(reply) > 300:
        return {"reply": "", "need_human": True, "error": "回复过长"}
    return {"reply": reply, "need_human": bool(data.get("need_human")) or not reply}


def _parse_json(content: str) -> dict | None:
    """推理模型偶尔只吐空白或带前后缀，这里做兜底提取。"""
    text = (content or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    matched = re.search(r"\{.*\}", text, re.DOTALL)
    if not matched:
        return None
    try:
        parsed = json.loads(matched.group(0))
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        return None


def decide(item: dict, history: list[dict], settings: dict) -> dict:
    """返回 {action: reply|human|ignore, text, decision, detail}。"""
    last = history[-1]["text"] if history else ""
    if not last or history[-1]["side"] == "me":
        return {"action": "ignore", "text": "", "decision": "gate", "detail": "最后一条是自己发的"}

    if sensitive(last):
        return {
            "action": "human",
            "text": "",
            "decision": "gate",
            "detail": f"命中敏感话题: {last[:40]}",
        }

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
            "detail": str(result.get("error") or ""),
        }
    return {"action": "reply", "text": result["reply"], "decision": "llm", "detail": ""}
