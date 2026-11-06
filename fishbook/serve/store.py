"""托管状态存储：SQLite（.fishbook/state.db）。"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_items (
  item_id TEXT PRIMARY KEY,
  title TEXT DEFAULT '',
  price TEXT DEFAULT '',
  managed INTEGER DEFAULT 0,          -- 接管中
  auto_reply INTEGER DEFAULT 1,
  auto_ship INTEGER DEFAULT 0,        -- 自动发货（虚拟）
  dry_run INTEGER DEFAULT 1,          -- 默认演练：只记录不发送
  item_desc TEXT DEFAULT '',          -- 商品描述（自动从详情接口取，用户不用填）
  item_status TEXT DEFAULT '',        -- 在售/已下架等
  fetched_at REAL DEFAULT 0,          -- 详情最近一次拉取时间
  guide TEXT DEFAULT '',              -- 补充说明：商品信息里没有的部分
  ship_note TEXT DEFAULT '',          -- 发货时发的那句话（{secret} 为卡密占位）
  created_at REAL
);

CREATE TABLE IF NOT EXISTS conversations (
  conv_key TEXT PRIMARY KEY,          -- sessionId（准确 ID）；未绑定时是 tmp:<nick>#<avatar>
  session_id TEXT DEFAULT '',         -- headinfo 拿到的会话 id
  nick TEXT DEFAULT '',               -- 展示用（页面上的打码昵称）
  avatar TEXT DEFAULT '',
  item_id TEXT DEFAULT '',            -- 关联的托管商品（headinfo 直接给）
  mode TEXT DEFAULT 'auto',           -- auto / manual
  unread INTEGER DEFAULT 0,
  preview TEXT DEFAULT '',            -- 会话行当前预览（每轮刷新）
  last_seen_preview TEXT DEFAULT '',  -- 已处理到的预览（触发门控）
  answered_upto INTEGER DEFAULT 0,    -- 已回复到 messages.id（水位）
  last_msg_at REAL DEFAULT 0,
  replied_at REAL DEFAULT 0,
  shipped_at REAL DEFAULT 0,
  updated_at REAL
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_key TEXT DEFAULT '',           -- 属于哪个会话
  chat TEXT DEFAULT '',               -- 展示用昵称
  direction TEXT,                     -- in / out / dry
  content TEXT,
  decision TEXT DEFAULT '',           -- llm / gate / manual / ship
  detail TEXT DEFAULT '',             -- 决策依据
  ts REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_key, id);

CREATE TABLE IF NOT EXISTS ship_pool (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT DEFAULT 'unused',       -- unused / used
  chat TEXT DEFAULT '',
  used_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def now_ts() -> float:
    return time.time()


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(managed_items)")}
        conv_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(conversations)")}
        if "name" in conv_cols and "conv_key" not in conv_cols:
            # v1 → v2：name 主键改成 conv_key（旧数据挂在 tmp:<nick> 下，绑定后改写）
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations_v2 (
                  conv_key TEXT PRIMARY KEY, session_id TEXT DEFAULT '',
                  nick TEXT DEFAULT '', avatar TEXT DEFAULT '', item_id TEXT DEFAULT '',
                  mode TEXT DEFAULT 'auto', unread INTEGER DEFAULT 0,
                  preview TEXT DEFAULT '', last_seen_preview TEXT DEFAULT '',
                  answered_upto INTEGER DEFAULT 0,
                  last_msg_at REAL DEFAULT 0, replied_at REAL DEFAULT 0,
                  shipped_at REAL DEFAULT 0, updated_at REAL
                );
                INSERT OR IGNORE INTO conversations_v2
                  (conv_key, nick, item_id, mode, unread, preview, last_seen_preview, updated_at)
                  SELECT 'tmp:' || name, name, COALESCE(item_id, ''), COALESCE(mode, 'auto'),
                         COALESCE(unread, 0), COALESCE(preview, ''), COALESCE(last_msg, ''),
                         COALESCE(updated_at, 0)
                  FROM conversations;
                DROP TABLE conversations;
                ALTER TABLE conversations_v2 RENAME TO conversations;
                """
            )
        if "preview" not in conv_cols and "conv_key" in conv_cols:
            self._conn.execute("ALTER TABLE conversations ADD COLUMN preview TEXT DEFAULT ''")
        for col in ("item_desc", "item_status"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE managed_items ADD COLUMN {col} TEXT DEFAULT ''")
        if "fetched_at" not in cols:
            self._conn.execute("ALTER TABLE managed_items ADD COLUMN fetched_at REAL DEFAULT 0")
        msg_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(messages)")}
        if "conv_key" not in msg_cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN conv_key TEXT DEFAULT ''")
        if "guide" not in cols:
            self._conn.execute("ALTER TABLE managed_items ADD COLUMN guide TEXT DEFAULT ''")
        # 旧字段合进 guide：prompt + FAQ 文本化
        if "prompt" in cols:
            rows = self._conn.execute("SELECT item_id, prompt, faq FROM managed_items").fetchall()
            for row in rows:
                parts = [row["prompt"] or ""]
                raw_faq = row["faq"] or "[]"
                if raw_faq and raw_faq != "[]":
                    try:
                        for entry in json.loads(raw_faq):
                            if isinstance(entry, dict) and entry.get("k"):
                                parts.append(f"买家问「{entry['k']}」时回：{entry.get('a', '')}")
                    except ValueError:
                        parts.append(raw_faq)
                merged = "\n".join(p for p in parts if p).strip()
                if merged:
                    self._conn.execute(
                        "UPDATE managed_items SET guide=? WHERE item_id=?", (merged, row["item_id"])
                    )

    # ---- settings ----

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---- managed items ----

    def upsert_item(self, item_id: str, title: str = "", price: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO managed_items(item_id, title, price, created_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET title=excluded.title, price=excluded.price",
                (item_id, title, price, now_ts()),
            )

    def update_item(self, item_id: str, fields: dict) -> None:
        allowed = {
            "title",
            "price",
            "managed",
            "auto_reply",
            "auto_ship",
            "dry_run",
            "item_desc",
            "item_status",
            "fetched_at",
            "guide",
            "ship_note",
        }
        keys = [k for k in fields if k in allowed]
        if not keys:
            return
        sets = ", ".join(f"{k}=?" for k in keys)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE managed_items SET {sets} WHERE item_id=?",
                [fields[k] for k in keys] + [item_id],
            )

    def get_item(self, item_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM managed_items WHERE item_id=?", (item_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_items(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM managed_items ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def managed_items(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM managed_items WHERE managed=1").fetchall()
        return [dict(r) for r in rows]

    def delete_item(self, item_id: str) -> None:
        """删商品：连带清掉它的卡密池，并把关联会话置空（会话本身保留）。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM managed_items WHERE item_id=?", (item_id,))
            self._conn.execute("DELETE FROM ship_pool WHERE item_id=?", (item_id,))
            self._conn.execute("UPDATE conversations SET item_id='' WHERE item_id=?", (item_id,))

    # ---- conversations ----

    @staticmethod
    def tmp_key(nick: str, avatar: str = "") -> str:
        """sessionId 还没拿到时的临时 key（昵称 + 头像片段，尽量不撞）。"""
        tail = (avatar or "").rstrip("/").split("/")[-1][:24]
        return f"tmp:{nick}#{tail}"

    def find_conversation(self, nick: str, avatar: str = "") -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE session_id!='' AND nick=? AND avatar=? LIMIT 1",
            (nick, avatar),
        ).fetchone()
        if row:
            return dict(row)
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE conv_key=? LIMIT 1", (self.tmp_key(nick, avatar),)
        ).fetchone()
        return dict(row) if row else None

    def get_conversation(self, conv_key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE conv_key=?", (conv_key,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_scan(self, nick: str, avatar: str, unread: int, preview: str) -> str:
        """刷新会话行的未读/预览（不动 last_seen_preview），返回 conv_key。"""
        record = self.find_conversation(nick, avatar)
        now = now_ts()
        if record:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE conversations SET unread=?, preview=?, avatar=?, updated_at=?, "
                    "last_msg_at=? WHERE conv_key=?",
                    (unread, preview, avatar, now, now, record["conv_key"]),
                )
            return record["conv_key"]
        key = self.tmp_key(nick, avatar)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO conversations(conv_key, nick, avatar, unread, preview, "
                "updated_at, last_msg_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (key, nick, avatar, unread, preview, now, now),
            )
        return key

    def bind(self, nick: str, avatar: str, session_id: str, item_id: str = "") -> str:
        """把 tmp key 升级成 sessionId；已绑定过就只更新商品信息。"""
        existing = None
        if session_id:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE session_id=? LIMIT 1", (session_id,)
            ).fetchone()
            existing = dict(row) if row else None
        if existing is None:
            existing = self.find_conversation(nick, avatar)

        with self._lock, self._conn:
            if existing and session_id and existing["conv_key"] != session_id:
                old_key = existing["conv_key"]
                self._conn.execute(
                    "UPDATE conversations SET conv_key=?, session_id=?, nick=?, avatar=? WHERE conv_key=?",
                    (session_id, session_id, nick, avatar, old_key),
                )
                # 消息跟着搬家
                self._conn.execute(
                    "UPDATE messages SET conv_key=? WHERE conv_key=?", (session_id, old_key)
                )
                existing = self.get_conversation(session_id)
            elif existing:
                self._conn.execute(
                    "UPDATE conversations SET session_id=COALESCE(NULLIF(?,''), session_id), "
                    "nick=?, avatar=? WHERE conv_key=?",
                    (session_id, nick, avatar, existing["conv_key"]),
                )
                existing = self.get_conversation(existing["conv_key"])
            else:
                key = session_id or self.tmp_key(nick, avatar)
                self._conn.execute(
                    "INSERT OR IGNORE INTO conversations(conv_key, session_id, nick, avatar, updated_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (key, session_id, nick, avatar, now_ts()),
                )
                existing = self.get_conversation(key)

            if item_id and existing and not existing.get("item_id"):
                self._conn.execute(
                    "UPDATE conversations SET item_id=? WHERE conv_key=?",
                    (item_id, existing["conv_key"]),
                )
                existing = self.get_conversation(existing["conv_key"])
        return existing["conv_key"]

    def set_conversation(self, conv_key: str, **fields) -> None:
        allowed = {
            "item_id",
            "mode",
            "unread",
            "replied_at",
            "shipped_at",
            "preview",
            "last_seen_preview",
            "answered_upto",
            "last_msg_at",
        }
        keys = [k for k in fields if k in allowed]
        if not keys:
            return
        sets = ", ".join(f"{k}=?" for k in keys)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE conversations SET {sets}, updated_at=? WHERE conv_key=?",
                [fields[k] for k in keys] + [now_ts(), conv_key],
            )

    def list_conversations(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM conversations ORDER BY last_msg_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- messages ----

    def tail_messages(self, conv_key: str, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE conv_key=? ORDER BY id DESC LIMIT ?", (conv_key, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def sync_messages(self, conv_key: str, chat: str, page_msgs: list[dict]) -> list[dict]:
        """按「后缀对齐」把页面消息增量写库，返回新插入的行。

        page_msgs: [{"side": "me"|"other", "text": str}]，按页面顺序（旧→新）。
        """
        page = [(m.get("side") or "", (m.get("text") or "").strip()) for m in page_msgs]
        if not page:
            return []
        stored = self.tail_messages(conv_key, len(page))
        # 页面用 other/me，DB 用 in/out/dry —— 统一成 other/me 再比
        stored_seq = [
            ("me" if m["direction"] in ("out", "dry") else "other", (m["content"] or "").strip())
            for m in stored
        ]

        k = 0
        for size in range(min(len(page), len(stored_seq)), 0, -1):
            if stored_seq[-size:] == page[-size:]:
                k = size
                break

        new_items = page[: len(page) - k] if k else page
        if k == 0 and stored_seq:
            log_line = f"[sync] {conv_key} 历史断层，整段重灌 {len(page)} 条"
            print(log_line, file=sys.stderr, flush=True)

        added: list[dict] = []
        with self._lock, self._conn:
            for direction, text in new_items:
                cur = self._conn.execute(
                    "INSERT INTO messages(conv_key, chat, direction, content, ts) VALUES(?, ?, ?, ?, ?)",
                    (conv_key, chat, "in" if direction == "other" else "out", text, now_ts()),
                )
                added.append({"id": cur.lastrowid, "direction": direction, "text": text})
        return added

    def add_message(
        self,
        chat: str,
        direction: str,
        content: str,
        decision: str = "",
        detail: dict | str = "",
        conv_key: str = "",
    ) -> None:
        """写一条审计消息（direction: in / out / dry）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO messages(conv_key, chat, direction, content, decision, detail, ts) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    conv_key or chat,
                    chat,
                    direction,
                    content,
                    decision,
                    detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False),
                    now_ts(),
                ),
            )

    def list_messages(self, conv_key: str, limit: int = 80) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE conv_key=? OR chat=? ORDER BY id DESC LIMIT ?",
            (conv_key, conv_key, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---- ship pool ----

    def add_secrets(self, item_id: str, contents: list[str]) -> int:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO ship_pool(item_id, content) VALUES(?, ?)",
                [(item_id, c.strip()) for c in contents if c.strip()],
            )
        return len([c for c in contents if c.strip()])

    def item_secrets(self, item_id: str) -> list[str]:
        """全部卡密（含已用），用于外发内容防泄漏比对。"""
        rows = self._conn.execute(
            "SELECT content FROM ship_pool WHERE item_id=?", (item_id,)
        ).fetchall()
        return [row["content"] for row in rows if row["content"]]

    def peek_secret(self, item_id: str) -> str | None:
        """看一眼下一条可用卡密，但不消耗（dry-run 用）。"""
        row = self._conn.execute(
            "SELECT content FROM ship_pool WHERE item_id=? AND status='unused' ORDER BY id LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["content"] if row else None

    def take_secret(self, item_id: str, chat: str = "") -> str | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id, content FROM ship_pool WHERE item_id=? AND status='unused' ORDER BY id LIMIT 1",
                (item_id,),
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE ship_pool SET status='used', used_at=?, chat=? WHERE id=?",
                (now_ts(), chat, row["id"]),
            )
            return row["content"]

    def pool_stats(self, item_id: str) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) AS total, SUM(status='unused') AS unused FROM ship_pool WHERE item_id=?",
            (item_id,),
        ).fetchone()
        return {"total": row["total"], "unused": row["unused"] or 0}
