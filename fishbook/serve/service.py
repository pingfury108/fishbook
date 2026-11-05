"""常驻服务：FastAPI UI + 轮询循环（IM 消息 / 发货触发）。"""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path

from ..sites.xy import im
from ..util import log, sleep_jitter
from . import agent
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

        # 全量落库，UI 会话列表始终有数据
        for conv in conversations:
            existing = self.store.get_conversation(conv["name"]) or {}
            self.store.upsert_conversation(
                conv["name"], conv.get("unread") or 0, conv.get("preview") or ""
            )
            if existing and existing.get("mode") == "manual":
                self.store.set_conversation(conv["name"], mode="manual")

        # 有未读且接管中的会话才进一步处理
        targets = []
        for conv in conversations:
            if not conv.get("unread"):
                continue
            record = self.store.get_conversation(conv["name"]) or {}
            item_id = record.get("item_id") or ""
            item = self.store.get_item(item_id) if item_id else None
            if not item or not item.get("managed"):
                self.store.upsert_conversation(conv["name"], conv["unread"], conv["preview"])
                continue
            if record.get("mode") == "manual":
                continue
            targets.append((conv, item))

        if not targets:
            return

        for conv, item in targets:
            self._handle(conv, item)
            sleep_jitter()

    def _handle(self, conv: dict, item: dict) -> None:
        name = conv["name"]
        opened = self.worker.submit(lambda ctx: im.open_conversation(ctx.page, name))
        if not opened.get("ok"):
            log(f"[watcher] 打开会话失败 {name}: {opened}")
            return
        snapshot = self.worker.submit(lambda ctx: im.read(ctx.page, limit=20))
        history = snapshot.get("messages") or []
        if not history or history[-1]["side"] != "other":
            return

        self.store.set_conversation(
            name, unread=0, last_msg=history[-1]["text"][:60], last_msg_at=time.time()
        )
        self.store.add_message(name, "in", history[-1]["text"])

        settings = self.settings()
        decision = agent.decide(item, history, settings)
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

        secret = self.store.take_secret(item["item_id"])
        if not secret:
            self.store.add_message(name, "dry", "", "ship", "卡密池为空，转人工")
            self.store.set_conversation(name, mode="manual")
            log(f"[ship] {name} 卡密池为空")
            return

        note = item.get("ship_note") or "已付款成功，这是你的兑换码：{secret}"
        text = note.replace("{secret}", secret)

        if item.get("dry_run"):
            self.store.add_message(name, "dry", text, "ship", "dry-run 拟发货")
            log(f"[ship] {name} dry-run 拟发货")
            return

        result = self.worker.submit(lambda ctx: im.send(ctx.page, text))
        if result.get("ok"):
            self.store.add_message(name, "out", text, "ship", "自动发货")
            self.store.set_conversation(name, shipped_at=time.time())
            self._count_sent()
            log(f"[ship] {name} 已发货")
        else:
            self.store.add_message(name, "dry", text, "ship", f"发货失败: {result}")
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
