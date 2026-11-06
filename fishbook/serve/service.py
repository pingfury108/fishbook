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

        # 全量落库，UI 会话列表始终有数据；顺手把没关联商品的会话补上关联
        managed = self.store.managed_items()
        for conv in conversations:
            existing = self.store.get_conversation(conv["name"]) or {}
            self.store.upsert_conversation(
                conv["name"], conv.get("unread") or 0, conv.get("preview") or ""
            )
            if existing and existing.get("mode") == "manual":
                self.store.set_conversation(conv["name"], mode="manual")
            if not (existing.get("item_id") or ""):
                guessed = self._guess_item(conv, managed)
                if guessed:
                    self.store.set_conversation(conv["name"], item_id=guessed)
                    log(f"[watcher] {conv['name']} 自动关联到商品 {guessed}")

        # 新消息判定：不能只看未读——IM 标签一直开着，新消息会被页面立刻标成已读。
        # 所以用「预览文本变了」作为主信号，未读徽标只作为补充。
        targets = []
        for conv in conversations:
            name = conv["name"]
            if self._is_system_chat(name):
                continue
            record = self.store.get_conversation(name) or {}
            preview = (conv.get("preview") or "").strip()
            prev = (record.get("last_msg") or "").strip()
            fresh = bool(conv.get("unread")) or (preview and preview != prev)
            if not fresh:
                continue

            item_id = record.get("item_id") or ""
            item = self.store.get_item(item_id) if item_id else None
            if not item or not item.get("managed"):
                log(f"[watcher] 跳过 {name}: 未关联到已接管商品")
                continue
            if record.get("mode") == "manual":
                log(f"[watcher] 跳过 {name}: 人工模式（UI 里可切回自动）")
                continue
            targets.append((conv, item))

        if not targets:
            return

        for conv, item in targets:
            try:
                self._handle(conv, item)
            finally:
                # 记住这一轮的预览，避免下一轮重复处理（含自己刚发出去的消息）
                self.store.set_conversation(
                    conv["name"], last_msg=(conv.get("preview") or "").strip()
                )
            sleep_jitter()

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

    def _handle(self, conv: dict, item: dict) -> None:
        name = conv["name"]
        opened = self.worker.submit(lambda ctx: im.open_conversation(ctx.page, name))
        if not opened.get("ok"):
            log(f"[watcher] 打开会话失败 {name}: {opened}")
            return
        snapshot = self.worker.submit(lambda ctx: im.read(ctx.page, limit=20))
        history = snapshot.get("messages") or []
        if not history:
            log(f"[watcher] {name} 读不到消息")
            return
        last = history[-1]
        log(f"[watcher] {name} 最后一条: {last['side']} | {last['text'][:40]}")
        if last["side"] != "other":
            log(f"[watcher] {name} 最后一条是自己发的，跳过")
            return

        self.store.set_conversation(
            name, unread=0, last_msg=history[-1]["text"][:60], last_msg_at=time.time()
        )
        self.store.add_message(name, "in", history[-1]["text"])

        settings = self.settings()
        secrets = self.store.item_secrets(item["item_id"])
        decision = agent.decide(item, history, settings, secrets=secrets)
        self._apply_decision(name, item, decision)

        # 自动发货：LLM 判断买家已付款
        if decision.get("decision") == "llm" and '"payment"' in str(decision.get("detail", "")):
            self._maybe_ship(name, item)

    def _apply_decision(self, name: str, item: dict, decision: dict) -> None:
        action = decision["action"]
        if action == "ignore":
            return

        if action == "human":
            self.store.add_message(
                name, "dry", "", decision["decision"], decision.get("detail", "")
            )
            self.store.set_conversation(name, mode="manual")
            log(f"[agent] {name} 转人工: {decision.get('detail', '')[:60]}")
            return

        text = decision["text"]
        dry = bool(item.get("dry_run"))
        if dry:
            self.store.add_message(name, "dry", text, decision["decision"], "dry-run 拟发送")
            log(f"[agent] {name} dry-run 拟回复: {text[:40]}")
            return

        if self._rate_limited():
            self.store.add_message(name, "dry", text, decision["decision"], "触发每日上限，未发送")
            return

        # 发送前最后一道：卡密 / 手机号 / 站外链接 / 疑似卡密串
        secrets = self.store.item_secrets(item["item_id"])
        reason = guard.check_outgoing(text, guard.item_sources(item), secrets)
        if reason:
            self.store.add_message(name, "dry", "", decision["decision"], f"发送前拦截：{reason}")
            self.store.set_conversation(name, mode="manual")
            log(f"[guard] {name} 回复被拦截（{reason}），转人工")
            return

        time.sleep(random.uniform(*REPLY_DELAY))
        result = self.worker.submit(lambda ctx: im.send(ctx.page, text))
        if result.get("ok"):
            self.store.add_message(
                name, "out", text, decision["decision"], decision.get("detail", "")
            )
            self.store.set_conversation(name, replied_at=time.time(), unread=0)
            self._count_sent()
        else:
            self.store.add_message(name, "dry", text, decision["decision"], f"发送失败: {result}")
            log(f"[agent] {name} 发送失败: {result}")

    # ---- 发货 ----

    def _maybe_ship(self, name: str, item: dict) -> None:
        if not item.get("auto_ship"):
            return
        record = self.store.get_conversation(name) or {}
        if record.get("shipped_at"):
            return  # 幂等：一个会话只自动发一次

        note = item.get("ship_note") or "已付款成功，这是你的兑换码：{secret}"

        if item.get("dry_run"):
            # 演练只预览，不消耗卡密（否则每轮轮询都会把池子抽干）
            preview = self.store.peek_secret(item["item_id"])
            if not preview:
                self.store.add_message(name, "dry", "", "ship", "卡密池为空，转人工")
                self.store.set_conversation(name, mode="manual")
                log(f"[ship] {name} 卡密池为空")
                return
            text = note.replace("{secret}", preview)
            self.store.add_message(
                name, "dry", guard.scrub(text, [preview]), "ship", "dry-run 拟发货（未消耗卡密）"
            )
            log(f"[ship] {name} dry-run 拟发货")
            return

        secret = self.store.take_secret(item["item_id"], chat=name)
        if not secret:
            self.store.add_message(name, "dry", "", "ship", "卡密池为空，转人工")
            self.store.set_conversation(name, mode="manual")
            log(f"[ship] {name} 卡密池为空")
            return
        text = note.replace("{secret}", secret)

        result = self.worker.submit(lambda ctx: im.send(ctx.page, text))
        if result.get("ok"):
            self.store.add_message(name, "out", guard.scrub(text, [secret]), "ship", "自动发货")
            self.store.set_conversation(name, shipped_at=time.time())
            self._count_sent()
            log(f"[ship] {name} 已发货")
        else:
            self.store.add_message(name, "dry", "", "ship", f"发货失败: {result}")
            # 卡密退回池子
            self.store.add_secrets(item["item_id"], [secret])

    # ---- 护栏 ----

    def _rate_limited(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        if self._sent_day != today:
            self._sent_day, self._sent_today = today, 0
        return self._sent_today >= DAILY_LIMIT

    def _count_sent(self) -> None:
        self._sent_today += 1
