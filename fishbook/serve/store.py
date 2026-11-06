"""托管状态存储：SQLite（.fishbook/state.db）。"""

from __future__ import annotations

import json
import sqlite3
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
  name TEXT PRIMARY KEY,              -- 买家昵称（IM 页无会话 id，v1 用昵称）
  item_id TEXT DEFAULT '',            -- 关联的托管商品
  mode TEXT DEFAULT 'auto',           -- auto / manual
  unread INTEGER DEFAULT 0,
  preview TEXT DEFAULT '',            -- 会话行当前预览（每轮刷新）
  last_msg TEXT DEFAULT '',           -- 已处理到的预览（用于判断有没有新消息）
  last_msg_at REAL DEFAULT 0,
  replied_at REAL DEFAULT 0,          -- agent 上次回复时间
  shipped_at REAL DEFAULT 0,          -- 本会话已自动发货时间（幂等）
  updated_at REAL
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat TEXT NOT NULL,                 -- 会话（买家昵称）
  direction TEXT,                     -- in / out / dry
  content TEXT,
  decision TEXT DEFAULT '',           -- faq / price_floor / llm / llm_human / manual / ship / gate
  detail TEXT DEFAULT '',             -- 决策依据（JSON 或说明）
  ts REAL
);

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


def now() -> float:
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
        if "preview" not in conv_cols:
            self._conn.execute("ALTER TABLE conversations ADD COLUMN preview TEXT DEFAULT ''")
        for col in ("item_desc", "item_status"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE managed_items ADD COLUMN {col} TEXT DEFAULT ''")
        if "fetched_at" not in cols:
            self._conn.execute("ALTER TABLE managed_items ADD COLUMN fetched_at REAL DEFAULT 0")
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
                (item_id, title, price, now()),
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

    def upsert_conversation(self, name: str, unread: int = 0, preview: str = "") -> None:
        """刷新未读与当前预览；last_msg（已处理到的预览）保持不变。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO conversations(name, unread, preview, last_msg_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET unread=excluded.unread, "
                "preview=excluded.preview, last_msg_at=excluded.last_msg_at, updated_at=excluded.updated_at",
                (name, unread, preview, now(), now()),
            )

    def get_conversation(self, name: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM conversations WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def set_conversation(self, name: str, **fields) -> None:
        allowed = {
            "item_id",
            "mode",
            "unread",
            "replied_at",
            "shipped_at",
            "last_msg",
            "last_msg_at",
        }
        keys = [k for k in fields if k in allowed]
        if not keys:
            return
        base = ", ".join(f"{k}=?" for k in keys)
        with self._lock, self._conn:
            row = self._conn.execute("SELECT 1 FROM conversations WHERE name=?", (name,)).fetchone()
            if row:
                self._conn.execute(
                    f"UPDATE conversations SET {base}, updated_at=? WHERE name=?",
                    [fields[k] for k in keys] + [now(), name],
                )
            else:
                cols = "name, " + ", ".join(keys) + ", updated_at"
                marks = ", ".join("?" for _ in range(len(keys) + 2))
                self._conn.execute(
                    f"INSERT INTO conversations({cols}) VALUES({marks})",
                    [name] + [fields[k] for k in keys] + [now()],
                )

    def list_conversations(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM conversations ORDER BY last_msg_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- messages / audit ----

    def add_message(
        self, chat: str, direction: str, content: str, decision: str = "", detail: dict | str = ""
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO messages(chat, direction, content, decision, detail, ts) VALUES(?, ?, ?, ?, ?, ?)",
                (
                    chat,
                    direction,
                    content,
                    decision,
                    detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False),
                    now(),
                ),
            )

    def list_messages(self, chat: str, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE chat=? ORDER BY ts DESC, id DESC LIMIT ?", (chat, limit)
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
                (now(), chat, row["id"]),
            )
            return row["content"]

    def pool_stats(self, item_id: str) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) AS total, SUM(status='unused') AS unused FROM ship_pool WHERE item_id=?",
            (item_id,),
        ).fetchone()
        return {"total": row["total"], "unused": row["unused"] or 0}
