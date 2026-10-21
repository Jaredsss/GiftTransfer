import json
import os
import time
from typing import Any

import aiosqlite

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,              # получать уведомления
    "collections": [],            # id коллекций; пусто = все
    "only_transferable": False,   # только подарки, доступные к передаче в Telegram
    "only_visible_owner": False,  # только если владелец не скрыт
    "max_number": None,           # только номера ≤ N
    "min_floor": None,            # флор коллекции от, $
    "max_floor": None,            # флор коллекции до, $
    "max_model_rarity": None,     # редкость модели ≤ X %
    "show_floor": True,
    "show_owner": True,
    "show_transfer": True,
    "show_attributes": True,
    "silent": False,              # без звука
}

_BOOL_KEYS = [k for k, v in DEFAULT_SETTINGS.items() if isinstance(v, bool)]
_NUM_KEYS = ["max_number", "min_floor", "max_floor", "max_model_rarity"]


def sanitize_settings(raw: Any, base: dict | None = None) -> dict:
    """Приводит пользовательские настройки к безопасному виду."""
    result = dict(DEFAULT_SETTINGS)
    if base:
        result.update({k: v for k, v in base.items() if k in DEFAULT_SETTINGS})
    if not isinstance(raw, dict):
        return result
    for key in _BOOL_KEYS:
        if key in raw:
            result[key] = bool(raw[key])
    for key in _NUM_KEYS:
        if key in raw:
            value = raw[key]
            if value in (None, ""):
                result[key] = None
                continue
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            if num < 0 or num != num or num > 1e12:
                continue
            result[key] = int(num) if key == "max_number" else num
    if "collections" in raw and isinstance(raw["collections"], list):
        ids = []
        for item in raw["collections"][:1000]:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                pass
        result["collections"] = sorted(set(ids))
    return result


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                sub_until INTEGER NOT NULL DEFAULT 0,
                settings TEXT,
                blocked INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                link TEXT,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                paid_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS invoices_status ON invoices(status);
            CREATE TABLE IF NOT EXISTS collections (
                gift_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                slug_base TEXT NOT NULL,
                last_num INTEGER NOT NULL DEFAULT 0,
                issued INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 0,
                checked_at INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # --- users ---

    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        await self.conn.execute(
            """
            INSERT INTO users (id, username, first_name, settings, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET username=excluded.username,
                first_name=excluded.first_name, blocked=0
            """,
            (user_id, username, first_name, json.dumps(DEFAULT_SETTINGS), int(time.time())),
        )
        await self.conn.commit()

    async def get_user(self, user_id: int) -> dict | None:
        async with self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        user = dict(row)
        user["settings"] = sanitize_settings(json.loads(user["settings"] or "{}"))
        return user

    async def save_settings(self, user_id: int, settings: dict) -> None:
        await self.conn.execute(
            "UPDATE users SET settings=? WHERE id=?", (json.dumps(settings), user_id)
        )
        await self.conn.commit()

    async def extend_subscription(self, user_id: int, days: int) -> int:
        now = int(time.time())
        user = await self.get_user(user_id)
        start = max(now, user["sub_until"] if user else 0)
        until = start + days * 86400
        await self.conn.execute("UPDATE users SET sub_until=? WHERE id=?", (until, user_id))
        await self.conn.commit()
        return until

    async def set_blocked(self, user_id: int) -> None:
        await self.conn.execute("UPDATE users SET blocked=1 WHERE id=?", (user_id,))
        await self.conn.commit()

    async def subscribers(self) -> list[dict]:
        async with self.conn.execute(
            "SELECT id, settings FROM users WHERE sub_until>? AND blocked=0", (int(time.time()),)
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r["id"], "settings": sanitize_settings(json.loads(r["settings"] or "{}"))}
            for r in rows
        ]

    async def stats(self) -> dict:
        now = int(time.time())
        async with self.conn.execute(
            """SELECT COUNT(*), SUM(sub_until>?) FROM users""", (now,)
        ) as cur:
            users, subs = await cur.fetchone()
        async with self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM invoices WHERE status='paid'"
        ) as cur:
            paid, revenue = await cur.fetchone()
        return {"users": users, "subs": subs or 0, "paid": paid, "revenue": revenue}

    # --- invoices ---

    async def add_invoice(self, inv_id: int, user_id: int, plan: str, amount: float,
                          currency: str, link: str) -> None:
        await self.conn.execute(
            """INSERT INTO invoices (id, user_id, plan, amount, currency, link, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
            (inv_id, user_id, plan, amount, currency, link, int(time.time())),
        )
        await self.conn.commit()

    async def get_invoice(self, inv_id: int) -> dict | None:
        async with self.conn.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def active_invoices(self) -> list[dict]:
        async with self.conn.execute("SELECT * FROM invoices WHERE status='active'") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def mark_invoice(self, inv_id: int, status: str) -> bool:
        """Меняет статус активного счёта. True — если статус действительно сменился."""
        cur = await self.conn.execute(
            "UPDATE invoices SET status=?, paid_at=? WHERE id=? AND status='active'",
            (status, int(time.time()) if status == "paid" else None, inv_id),
        )
        await self.conn.commit()
        return cur.rowcount == 1

    # --- collections ---

    async def upsert_collection(self, gift_id: int, title: str, slug_base: str, total: int) -> None:
        await self.conn.execute(
            """INSERT INTO collections (gift_id, title, slug_base, total) VALUES (?, ?, ?, ?)
               ON CONFLICT(gift_id) DO UPDATE SET title=excluded.title,
                   slug_base=excluded.slug_base,
                   total=MAX(collections.total, excluded.total)""",
            (gift_id, title, slug_base, total),
        )
        await self.conn.commit()

    async def update_collection(self, gift_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(
            f"UPDATE collections SET {cols} WHERE gift_id=?", (*fields.values(), gift_id)
        )
        await self.conn.commit()

    async def collections(self) -> list[dict]:
        async with self.conn.execute("SELECT * FROM collections ORDER BY title") as cur:
            return [dict(r) for r in await cur.fetchall()]
