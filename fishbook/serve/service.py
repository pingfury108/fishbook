"""常驻服务：FastAPI UI + 轮询循环（IM 消息 / 发货触发）。"""

from __future__ import annotations

import random
import re
import threading
import time
from pathlib import Path

from ..sites.xy import im
from ..util import log, sleep_jitter

# 系统/官方会话，不参与托管（回了就是跟机器人/平台对话）
SYSTEM_CHAT_RE = re.compile(r"^(通知消息|系统消息|.*官方.*|.*小二.*|.*客服.*)$")
from . import agent, guard
from .store import Store

STATIC = Path(__file__).resolve().parent / "static"
from .worker import BrowserWorker

REPLY_DELAY = (3.0, 9.0)  # 收到消息后延迟回复，像人
DAILY_LIMIT = 200


class Service:
    def __init__(
        self,
        cdp_endpoint: str,
        db_path: Path,
        poll_interval: tuple[float, float] = (25.0, 45.0),
    ) -> None:
        self.store = Store(db_path)
        self.worker = BrowserWorker(cdp_endpoint)
        self.cdp_endpoint = cdp_endpoint
        self.poll_interval = poll_interval
        self.paused = threading.Event()  # 熔断
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="watcher", daemon=True)
        self._sent_today = 0
        self._sent_day = ""

    # ---- 设置 ----

    def masked_key(self) -> str:
        key = self.store.get_setting("api_key")
        if not key:
            return ""
        if len(key) <= 12:
            return key[:3] + "*" * (len(key) - 3)
        return f"{key[:7]}…{key[-4:]}"

    def llm_test(self) -> dict:
        """用当前 key 真打一次模型，确认可用（返回模型名与耗时）。"""
        key = self.store.get_setting("api_key")
        if not key:
            return {"ok": False, "error": "未配置 API Key"}
        model = self.store.get_setting("model", "deepseek-v4-flash")
        started = time.time()
        try:
            from openai import OpenAI

            client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": '只输出 JSON：{"ok":true}'}],
                response_format={"type": "json_object"},
                max_tokens=256,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - 原样回给 UI
            return {"ok": False, "error": str(exc)[:200], "model": model}
        latency = int((time.time() - started) * 1000)
        return {
            "ok": True,
            "model": getattr(resp, "model", model),
            "latency_ms": latency,
            "reply": (resp.choices[0].message.content or "").strip()[:60],
        }

    def settings(self) -> dict:
        return {
            "api_key": self.store.get_setting("api_key"),
            "model": self.store.get_setting("model", "deepseek-v4-flash"),
            "shop": self.store.get_setting("shop", ""),
        }

    # ---- 主循环 ----

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.paused.is_set():
                time.sleep(5)
                continue
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - 轮询失败不退出
                log(f"[watcher] 轮询异常: {exc}")
            time.sleep(random.uniform(*self.poll_interval))

    def _tick(self) -> None:
        snapshot = self.worker.submit(lambda ctx: im.scan(ctx.page))
        conversations = snapshot.get("conversations") or []
        self.worker.submit(lambda ctx: im.enable_network(ctx.page))

        # 1) 全量落库（只刷未读/预览），并记下每行的 idx 供点击用
        managed = self.store.managed_items()
        rows = []
        for idx, conv in enumerate(conversations):
            nick = conv.get("name") or ""
            avatar = conv.get("avatar") or ""
            key = self.store.upsert_scan(
                nick, avatar, conv.get("unread") or 0, conv.get("preview") or ""
            )
            record = self.store.get_conversation(key) or {}
            if record.get("mode") == "manual":
                continue
            rows.append({"idx": idx, "conv": conv, "key": key, "record": record})

        # 2) 候选：未绑定 sessionId，或预览变了 / 有未读
        candidates = []
        for row in rows:
            conv, record = row["conv"], row["record"]
            if self._is_system_chat(conv.get("name") or ""):
                continue
            preview = (conv.get("preview") or "").strip()
            fresh = (
                bool(conv.get("unread"))
                or not record.get("session_id")
                or preview != (record.get("last_seen_preview") or "")
            )
            if fresh:
                candidates.append(row)

        if not candidates:
            return

        # 3) 逐个：绑定 → 同步消息 → 新入站才处理（每轮最多 3 个，控制页面操作量）
        for row in candidates[:3]:
            try:
                self._sync_and_maybe_reply(row, managed)
            except Exception as exc:  # noqa: BLE001 - 单个会话失败不影响其他
                log(f"[watcher] {row['conv'].get('name')} 处理失败: {str(exc)[:120]}")
            sleep_jitter()

    def _sync_and_maybe_reply(self, row: dict, managed: list[dict]) -> None:
        conv = row["conv"]
        nick = conv.get("name") or ""
        opened = self.worker.submit(lambda ctx: im.open_by_index(ctx.page, row["idx"]))
        if not opened.get("ok"):
            log(f"[watcher] 打开会话失败 {nick}: {opened}")
            return

        conv_key = self.store.bind(
            nick,
            conv.get("avatar") or "",
            opened.get("session_id") or "",
            opened.get("item_id") or "",
        )
        record = self.store.get_conversation(conv_key) or {}

        # 商品：headinfo 给的 itemId 是权威值，优先于按标题猜的结果
        item_id = record.get("item_id") or ""
        from_headinfo = opened.get("item_id") or ""
        if from_headinfo and from_headinfo != item_id:
            if item_id:
                log(f"[watcher] {nick} 商品以 headinfo 为准: {item_id} → {from_headinfo}")
            self.store.set_conversation(conv_key, item_id=from_headinfo)
            record = self.store.get_conversation(conv_key) or record
            item_id = from_headinfo
        elif not item_id:
            guessed = self._guess_item(conv, managed)
            if guessed:
                self.store.set_conversation(conv_key, item_id=guessed)
                record = self.store.get_conversation(conv_key) or record
                item_id = guessed

        item = self.store.get_item(item_id) if item_id else None
        if not item or not item.get("managed"):
            log(f"[watcher] 跳过 {nick}: 未关联到已接管商品")
            return
        if record.get("mode") == "manual":
            log(f"[watcher] 跳过 {nick}: 人工模式（UI 里可切回自动）")
            return

        # 消息同步（后缀对齐，DB 是真相源）
        page_msgs = (
            self.worker.submit(lambda ctx: im.read(ctx.page, limit=20)).get("messages") or []
        )
        added = self.store.sync_messages(conv_key, nick, page_msgs)
        self.store.set_conversation(
            conv_key, last_seen_preview=(conv.get("preview") or "").strip(), unread=0
        )
        if not added:
            return

        inbound = [m for m in added if m["direction"] == "other"]
        if not inbound:
            log(f"[watcher] {nick} 只有自己发出的新消息，跳过")
            return

        last_in = inbound[-1]
        answered = record.get("answered_upto") or 0
        if last_in["id"] <= answered:
            log(f"[watcher] {nick} 该消息已处理过，跳过")
            return

        log(f"[watcher] {nick} 新消息: {last_in['text'][:40]}")
        # DB 行 → agent 需要的历史格式
        history = [
            {"side": "out" if row["direction"] == "out" else "other", "text": row["content"] or ""}
            for row in self.store.tail_messages(conv_key, 20)
        ]
        history = [
            {"side": "me" if h["side"] == "out" else "other", "text": h["text"]} for h in history
        ]
        if not history:
            return
        settings = self.settings()
        secrets = self.store.item_secrets(item["item_id"])
        decision = agent.decide(item, history, settings, secrets=secrets)
        self._apply_decision(conv_key, nick, item, decision)
        self.store.set_conversation(conv_key, answered_upto=last_in["id"])

        if guard.is_payment_intent(last_in["text"]):
            self._maybe_ship(conv_key, nick, item)

    @staticmethod
    def _is_system_chat(name: str) -> bool:
        return bool(SYSTEM_CHAT_RE.match(name or ""))

    @classmethod
    def _guess_item(cls, conv: dict, managed: list[dict]) -> str:
        """从会话行里的文字猜商品：标题片段命中 → 唯一托管商品兜底。"""
        if cls._is_system_chat(conv.get("name") or ""):
            return ""
        texts = [conv.get("name") or "", conv.get("preview") or ""]
        texts += [str(t) for t in conv.get("extras") or []]
        blob = " ".join(texts)
        hits = [
            item["item_id"]
            for item in managed
            if item.get("title")
            and len(item["title"]) >= 4
            and (item["title"][:8] in blob or blob.find(item["title"][:6]) >= 0)
        ]
        if len(set(hits)) == 1:
            return hits[0]
        if len(managed) == 1:
            return managed[0]["item_id"]
        return ""

    def _live_allowed(self, item: dict) -> bool:
        """发送开关：必须显式关掉演练，且全局 live 开关未关。缺省一律不发。"""
        if int(item.get("dry_run") or 0) != 0:
            return False
        return (self.store.get_setting("live", "1") or "1") == "1"

    def _send(
        self,
        conv_key: str,
        nick: str,
        item: dict,
        text: str,
        decision: str,
        detail: str = "",
        purpose: str = "reply",
    ) -> bool:
        """唯一发送出口：演练/熔断/护栏任一不通过都只记录不发送。"""
        secrets = self.store.item_secrets(item["item_id"])

        if not self._live_allowed(item):
            reason = (
                "dry-run 拟发送" if int(item.get("dry_run") or 0) != 0 else "全局 live 开关关闭"
            )
            self.store.add_message(nick, "dry", text, decision, reason, conv_key=conv_key)
            log(f"[send] {nick} 未发送（{reason}）: {text[:40]}")
            return False

        if self.paused.is_set():
            self.store.add_message(nick, "dry", text, decision, "熔断中，未发送", conv_key=conv_key)
            log(f"[send] {nick} 熔断中，未发送")
            return False

        if self._rate_limited():
            self.store.add_message(
                nick, "dry", text, decision, "触发每日上限，未发送", conv_key=conv_key
            )
            return False

        reason = guard.check_outgoing(text, guard.item_sources(item), secrets)
        if reason:
            self.store.add_message(
                nick, "dry", "", decision, f"发送前拦截：{reason}", conv_key=conv_key
            )
            self.store.set_conversation(conv_key, mode="manual")
            log(f"[guard] {nick} 回复被拦截（{reason}），转人工")
            return False

        time.sleep(random.uniform(*REPLY_DELAY))
        result = self.worker.submit(lambda ctx: im.send(ctx.page, text))
        if result.get("ok"):
            self.store.add_message(
                nick, "out", guard.scrub(text, secrets), decision, detail, conv_key=conv_key
            )
            self._count_sent()
            log(f"[send] {nick} 已发送（{purpose}）: {text[:40]}")
            return True

        self.store.add_message(
            nick, "dry", text, decision, f"发送失败: {result}", conv_key=conv_key
        )
        log(f"[send] {nick} 发送失败: {result}")
        return False

    def _apply_decision(self, conv_key: str, nick: str, item: dict, decision: dict) -> None:
        action = decision["action"]
        if action == "ignore":
            return

        if action == "human":
            self.store.add_message(
                nick, "dry", "", decision["decision"], decision.get("detail", ""), conv_key=conv_key
            )
            self.store.set_conversation(conv_key, mode="manual")
            log(f"[agent] {nick} 转人工: {decision.get('detail', '')[:60]}")
            return

        text = decision["text"]
        if self._send(conv_key, nick, item, text, decision["decision"], decision.get("detail", "")):
            self.store.set_conversation(conv_key, replied_at=time.time(), unread=0)

    # ---- 发货 ----

    def _maybe_ship(self, conv_key: str, nick: str, item: dict) -> None:
        if not item.get("auto_ship"):
            return
        record = self.store.get_conversation(conv_key) or {}
        if record.get("shipped_at"):
            return  # 幂等：一个会话只自动发一次

        note = item.get("ship_note") or "已付款成功，这是你的兑换码：{secret}"

        if int(item.get("dry_run") or 0) != 0:
            preview = self.store.peek_secret(item["item_id"])
            if not preview:
                self.store.add_message(
                    nick, "dry", "", "ship", "卡密池为空，转人工", conv_key=conv_key
                )
                self.store.set_conversation(conv_key, mode="manual")
                return
            text = note.replace("{secret}", preview)
            self.store.add_message(
                nick,
                "dry",
                guard.scrub(text, [preview]),
                "ship",
                "dry-run 拟发货（未消耗卡密）",
                conv_key=conv_key,
            )
            log(f"[ship] {nick} dry-run 拟发货")
            return

        secret = self.store.take_secret(item["item_id"], chat=nick)
        if not secret:
            self.store.add_message(nick, "dry", "", "ship", "卡密池为空，转人工", conv_key=conv_key)
            self.store.set_conversation(conv_key, mode="manual")
            log(f"[ship] {nick} 卡密池为空")
            return
        text = note.replace("{secret}", secret)
        if self._send(conv_key, nick, item, text, "ship", "自动发货", purpose="ship"):
            self.store.set_conversation(conv_key, shipped_at=time.time())
        else:
            self.store.add_secrets(item["item_id"], [secret])  # 没发出去就退回池子

    # ---- 护栏 ----

    def _rate_limited(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        if self._sent_day != today:
            self._sent_day, self._sent_today = today, 0
        return self._sent_today >= DAILY_LIMIT

    def _count_sent(self) -> None:
        self._sent_today += 1
