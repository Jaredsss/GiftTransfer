"""Хранилище и бухгалтерия. Все движения средств — в одной транзакции SQLite."""
import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager
from decimal import ROUND_CEILING, Decimal

import aiosqlite

from .currencies import CODES, CURRENCIES, generate_address, normalize_address


class LedgerError(Exception):
    """Ошибка операции, текст можно показать пользователю."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    deals INTEGER NOT NULL DEFAULT 0,
    banned INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS addresses (
    user_id INTEGER NOT NULL,
    currency TEXT NOT NULL,
    address TEXT NOT NULL UNIQUE,
    PRIMARY KEY (user_id, currency)
);
CREATE TABLE IF NOT EXISTS balances (
    user_id INTEGER NOT NULL,
    currency TEXT NOT NULL,
    available INTEGER NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, currency)
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    currency TEXT NOT NULL,
    delta INTEGER NOT NULL,
    counterparty TEXT,
    ref TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_user ON ledger(user_id, id);
CREATE TABLE IF NOT EXISTS cheques (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    creator_id INTEGER NOT NULL,
    currency TEXT NOT NULL,
    amount INTEGER NOT NULL,
    total INTEGER NOT NULL,
    left INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cheque_uses (
    cheque_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (cheque_id, user_id)
);
CREATE TABLE IF NOT EXISTS p2p_ads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    side TEXT NOT NULL,            -- sell: автор продаёт монеты, buy: автор покупает
    currency TEXT NOT NULL,
    price TEXT NOT NULL,           -- звёзд за 1 монету
    amount_left INTEGER NOT NULL,  -- для sell — монеты в эскроу
    min_units INTEGER NOT NULL,
    max_units INTEGER NOT NULL,
    method TEXT NOT NULL,
    terms TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS p2p_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id INTEGER NOT NULL,
    buyer_id INTEGER NOT NULL,
    seller_id INTEGER NOT NULL,
    currency TEXT NOT NULL,
    amount INTEGER NOT NULL,
    price TEXT NOT NULL,
    total_stars INTEGER NOT NULL,
    escrow TEXT NOT NULL,          -- ad: монеты из пула объявления, order: заблокированы при сделке
    status TEXT NOT NULL,          -- wait_payment, paid, dispute, completed, cancelled
    dispute_by INTEGER,
    dispute_reason TEXT,
    created_at INTEGER NOT NULL,
    pay_deadline INTEGER NOT NULL,
    paid_at INTEGER,
    closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS p2p_orders_status ON p2p_orders(status);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    @asynccontextmanager
    async def tx(self):
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    async def _one(self, sql: str, params=()) -> dict | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, params=()) -> list[dict]:
        async with self.conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # --- низкоуровневые движения (только внутри tx) ---

    async def _move(self, user_id: int, code: str, available: int = 0, locked: int = 0) -> None:
        cur = await self.conn.execute(
            """UPDATE balances SET available=available+?, locked=locked+?
               WHERE user_id=? AND currency=? AND available+?>=0 AND locked+?>=0""",
            (available, locked, user_id, code, available, locked),
        )
        if cur.rowcount != 1:
            raise LedgerError("Недостаточно средств")

    async def _log(self, user_id: int, kind: str, code: str, delta: int,
                   counterparty: str | None = None, ref: str | None = None) -> None:
        await self.conn.execute(
            "INSERT INTO ledger (user_id, kind, currency, delta, counterparty, ref, created_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, kind, code, delta, counterparty, ref, int(time.time())),
        )

    # --- пользователи и адреса ---

    async def ensure_user(self, user_id: int, username: str | None, first_name: str | None) -> bool:
        """Создаёт пользователя с вечными адресами. True — если пользователь новый."""
        async with self.tx() as conn:
            exists = await self._one("SELECT id FROM users WHERE id=?", (user_id,))
            if exists:
                await conn.execute("UPDATE users SET username=?, first_name=? WHERE id=?",
                                   (username, first_name, user_id))
                return False
            await conn.execute(
                "INSERT INTO users (id, username, first_name, created_at) VALUES (?,?,?,?)",
                (user_id, username, first_name, int(time.time())),
            )
            for code in CODES:
                while True:
                    address = generate_address(code)
                    if not await self._one("SELECT 1 FROM addresses WHERE address=?", (address,)):
                        break
                await conn.execute("INSERT INTO addresses (user_id, currency, address) VALUES (?,?,?)",
                                   (user_id, code, address))
                await conn.execute("INSERT INTO balances (user_id, currency) VALUES (?,?)", (user_id, code))
            return True

    async def get_user(self, user_id: int) -> dict | None:
        return await self._one("SELECT * FROM users WHERE id=?", (user_id,))

    async def find_user(self, query: str) -> dict | None:
        """По id, @username или адресу."""
        query = query.strip()
        if query.lstrip("-").isdigit():
            return await self.get_user(int(query))
        if query.startswith("@"):
            return await self._one("SELECT * FROM users WHERE username=? COLLATE NOCASE", (query[1:],))
        owner = await self.address_owner(query)
        return await self.get_user(owner[0]) if owner else None

    async def set_banned(self, user_id: int, banned: bool) -> None:
        await self.conn.execute("UPDATE users SET banned=? WHERE id=?", (int(banned), user_id))

    async def addresses(self, user_id: int) -> dict[str, str]:
        rows = await self._all("SELECT currency, address FROM addresses WHERE user_id=?", (user_id,))
        return {r["currency"]: r["address"] for r in rows}

    async def address_owner(self, address: str) -> tuple[int, str] | None:
        row = await self._one("SELECT user_id, currency FROM addresses WHERE address=?",
                              (normalize_address(address),))
        return (row["user_id"], row["currency"]) if row else None

    async def balances(self, user_id: int) -> dict[str, dict]:
        rows = await self._all("SELECT currency, available, locked FROM balances WHERE user_id=?", (user_id,))
        result = {code: {"available": 0, "locked": 0} for code in CODES}
        for r in rows:
            result[r["currency"]] = {"available": r["available"], "locked": r["locked"]}
        return result

    async def history(self, user_id: int, limit: int = 15) -> list[dict]:
        return await self._all("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))

    # --- переводы, обмен, выпуск ---

    async def transfer(self, from_id: int, to_id: int, code: str, units: int, via: str) -> None:
        if from_id == to_id:
            raise LedgerError("Нельзя отправить самому себе")
        async with self.tx():
            await self._move(from_id, code, available=-units)
            await self._move(to_id, code, available=units)
            await self._log(from_id, "send", code, -units, via)
            await self._log(to_id, "receive", code, units, str(from_id))

    async def swap(self, user_id: int, src: str, dst: str, units_src: int, units_dst: int) -> None:
        if units_dst <= 0:
            raise LedgerError("Сумма слишком мала для обмена")
        async with self.tx():
            await self._move(user_id, src, available=-units_src)
            await self._move(user_id, dst, available=units_dst)
            await self._log(user_id, "swap_out", src, -units_src, dst)
            await self._log(user_id, "swap_in", dst, units_dst, src)

    async def mint(self, user_id: int, code: str, units: int, admin_id: int) -> None:
        """Выпуск (units > 0) или списание (units < 0) администратором."""
        async with self.tx():
            await self._move(user_id, code, available=units)
            await self._log(user_id, "mint" if units > 0 else "burn", code, units, str(admin_id))

    # --- чеки ---

    async def create_cheque(self, user_id: int, code: str, amount: int, count: int) -> str:
        cheque_code = secrets.token_urlsafe(9)
        async with self.tx() as conn:
            await self._move(user_id, code, available=-amount * count)
            await conn.execute(
                """INSERT INTO cheques (code, creator_id, currency, amount, total, left, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (cheque_code, user_id, code, amount, count, count, int(time.time())),
            )
            await self._log(user_id, "cheque_create", code, -amount * count, None, cheque_code)
        return cheque_code

    async def get_cheque(self, cheque_code: str) -> dict | None:
        return await self._one("SELECT * FROM cheques WHERE code=?", (cheque_code,))

    async def activate_cheque(self, cheque_code: str, user_id: int) -> dict:
        async with self.tx() as conn:
            cheque = await self._one("SELECT * FROM cheques WHERE code=?", (cheque_code,))
            if not cheque or not cheque["active"] or cheque["left"] <= 0:
                raise LedgerError("Чек не найден или уже активирован")
            if cheque["creator_id"] == user_id:
                raise LedgerError("Нельзя активировать свой чек")
            if await self._one("SELECT 1 FROM cheque_uses WHERE cheque_id=? AND user_id=?", (cheque["id"], user_id)):
                raise LedgerError("Вы уже активировали этот чек")
            await conn.execute("INSERT INTO cheque_uses (cheque_id, user_id) VALUES (?,?)", (cheque["id"], user_id))
            await conn.execute("UPDATE cheques SET left=left-1, active=(left-1>0) WHERE id=?", (cheque["id"],))
            await self._move(user_id, cheque["currency"], available=cheque["amount"])
            await self._log(user_id, "cheque_in", cheque["currency"], cheque["amount"],
                            str(cheque["creator_id"]), cheque_code)
        return cheque

    async def cancel_cheque(self, cheque_id: int, user_id: int) -> int:
        async with self.tx() as conn:
            cheque = await self._one("SELECT * FROM cheques WHERE id=? AND creator_id=?", (cheque_id, user_id))
            if not cheque or not cheque["active"]:
                raise LedgerError("Чек уже неактивен")
            refund = cheque["amount"] * cheque["left"]
            await conn.execute("UPDATE cheques SET active=0, left=0 WHERE id=?", (cheque_id,))
            await self._move(user_id, cheque["currency"], available=refund)
            await self._log(user_id, "cheque_refund", cheque["currency"], refund, None, cheque["code"])
        return refund

    async def my_cheques(self, user_id: int) -> list[dict]:
        return await self._all(
            "SELECT * FROM cheques WHERE creator_id=? AND active=1 ORDER BY id DESC LIMIT 20", (user_id,))

    # --- P2P: объявления ---

    async def create_ad(self, user_id: int, side: str, code: str, price: Decimal, amount: int,
                        min_units: int, max_units: int, method: str, terms: str) -> int:
        async with self.tx() as conn:
            if side == "sell":
                await self._move(user_id, code, available=-amount, locked=amount)  # в эскроу
            cur = await conn.execute(
                """INSERT INTO p2p_ads (user_id, side, currency, price, amount_left, min_units, max_units,
                   method, terms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (user_id, side, code, str(price), amount, min_units, max_units, method, terms, int(time.time())),
            )
            return cur.lastrowid

    async def get_ad(self, ad_id: int) -> dict | None:
        return await self._one(
            """SELECT a.*, u.username, u.first_name, u.deals FROM p2p_ads a
               JOIN users u ON u.id=a.user_id WHERE a.id=?""", (ad_id,))

    async def list_ads(self, side: str, code: str, limit: int = 10) -> list[dict]:
        order = "ASC" if side == "sell" else "DESC"  # покупателю — дешёвые, продавцу — дорогие
        ads = await self._all(
            f"""SELECT a.*, u.username, u.first_name, u.deals FROM p2p_ads a JOIN users u ON u.id=a.user_id
                WHERE a.side=? AND a.currency=? AND a.active=1 AND a.amount_left>=a.min_units AND u.banned=0""",
            (side, code),
        )
        ads.sort(key=lambda a: Decimal(a["price"]), reverse=(order == "DESC"))
        return ads[:limit]

    async def my_ads(self, user_id: int) -> list[dict]:
        return await self._all(
            "SELECT * FROM p2p_ads WHERE user_id=? AND active=1 ORDER BY id DESC", (user_id,))

    async def close_ad(self, ad_id: int, user_id: int | None = None) -> None:
        async with self.tx() as conn:
            ad = await self._one("SELECT * FROM p2p_ads WHERE id=?", (ad_id,))
            if not ad or not ad["active"] or (user_id is not None and ad["user_id"] != user_id):
                raise LedgerError("Объявление уже закрыто")
            await conn.execute("UPDATE p2p_ads SET active=0, amount_left=0 WHERE id=?", (ad_id,))
            if ad["side"] == "sell" and ad["amount_left"]:
                await self._move(ad["user_id"], ad["currency"], available=ad["amount_left"], locked=-ad["amount_left"])

    # --- P2P: сделки ---

    async def create_order(self, ad_id: int, taker_id: int, units: int, pay_minutes: int) -> int:
        async with self.tx() as conn:
            ad = await self._one("SELECT * FROM p2p_ads WHERE id=?", (ad_id,))
            if not ad or not ad["active"]:
                raise LedgerError("Объявление уже неактивно")
            if ad["user_id"] == taker_id:
                raise LedgerError("Это ваше объявление")
            if not ad["min_units"] <= units <= min(ad["max_units"], ad["amount_left"]):
                raise LedgerError("Сумма вне лимитов объявления")
            if ad["side"] == "sell":
                buyer, seller, escrow = taker_id, ad["user_id"], "ad"
            else:
                buyer, seller, escrow = ad["user_id"], taker_id, "order"
                await self._move(seller, ad["currency"], available=-units, locked=units)
            await conn.execute("UPDATE p2p_ads SET amount_left=amount_left-? WHERE id=?", (units, ad_id))
            price = Decimal(ad["price"])
            coins = Decimal(units) / CURRENCIES[ad["currency"]].unit
            total = int((coins * price).to_integral_value(rounding=ROUND_CEILING))  # звёзды — целые
            now = int(time.time())
            cur = await conn.execute(
                """INSERT INTO p2p_orders (ad_id, buyer_id, seller_id, currency, amount, price, total_stars,
                   escrow, status, created_at, pay_deadline) VALUES (?,?,?,?,?,?,?,?, 'wait_payment', ?, ?)""",
                (ad_id, buyer, seller, ad["currency"], units, ad["price"], max(total, 1), escrow,
                 now, now + pay_minutes * 60),
            )
            return cur.lastrowid

    async def get_order(self, order_id: int) -> dict | None:
        return await self._one("SELECT * FROM p2p_orders WHERE id=?", (order_id,))

    async def my_orders(self, user_id: int, limit: int = 15) -> list[dict]:
        return await self._all(
            """SELECT * FROM p2p_orders WHERE buyer_id=? OR seller_id=?
               ORDER BY status IN ('completed','cancelled'), id DESC LIMIT ?""", (user_id, user_id, limit))

    async def mark_paid(self, order_id: int, buyer_id: int) -> dict:
        async with self.tx() as conn:
            order = await self._one("SELECT * FROM p2p_orders WHERE id=?", (order_id,))
            if not order or order["buyer_id"] != buyer_id or order["status"] != "wait_payment":
                raise LedgerError("Сделку нельзя отметить оплаченной")
            await conn.execute("UPDATE p2p_orders SET status='paid', paid_at=? WHERE id=?",
                               (int(time.time()), order_id))
            return {**order, "status": "paid"}

    async def complete_order(self, order_id: int, fee_percent: Decimal, by_seller: int | None = None,
                             from_status: tuple = ("paid",)) -> dict:
        """Монеты из эскроу уходят покупателю (за вычетом комиссии)."""
        async with self.tx() as conn:
            order = await self._one("SELECT * FROM p2p_orders WHERE id=?", (order_id,))
            if not order or order["status"] not in from_status:
                raise LedgerError("Сделка уже закрыта")
            if by_seller is not None and order["seller_id"] != by_seller:
                raise LedgerError("Подтвердить может только продавец")
            amount, code = order["amount"], order["currency"]
            fee = int(Decimal(amount) * fee_percent / 100)
            await self._move(order["seller_id"], code, locked=-amount)
            await self._move(order["buyer_id"], code, available=amount - fee)
            await self._log(order["seller_id"], "p2p_sell", code, -amount, str(order["buyer_id"]), str(order_id))
            await self._log(order["buyer_id"], "p2p_buy", code, amount - fee, str(order["seller_id"]), str(order_id))
            await conn.execute("UPDATE p2p_orders SET status='completed', closed_at=? WHERE id=?",
                               (int(time.time()), order_id))
            await conn.execute("UPDATE users SET deals=deals+1 WHERE id IN (?,?)",
                               (order["buyer_id"], order["seller_id"]))
            return {**order, "status": "completed", "fee": fee}

    async def cancel_order(self, order_id: int, by_buyer: int | None = None,
                           from_status: tuple = ("wait_payment",)) -> dict:
        """Отмена: монеты возвращаются в объявление (если оно активно) или продавцу."""
        async with self.tx() as conn:
            order = await self._one("SELECT * FROM p2p_orders WHERE id=?", (order_id,))
            if not order or order["status"] not in from_status:
                raise LedgerError("Сделку уже нельзя отменить")
            if by_buyer is not None and order["buyer_id"] != by_buyer:
                raise LedgerError("Отменить может только покупатель")
            ad = await self._one("SELECT * FROM p2p_ads WHERE id=?", (order["ad_id"],))
            amount, code, seller = order["amount"], order["currency"], order["seller_id"]
            ad_active = ad and ad["active"]
            if order["escrow"] == "ad" and ad_active:
                await conn.execute("UPDATE p2p_ads SET amount_left=amount_left+? WHERE id=?", (amount, ad["id"]))
            else:
                await self._move(seller, code, available=amount, locked=-amount)
                if ad_active:  # объявление на покупку снова может принять эту сумму
                    await conn.execute("UPDATE p2p_ads SET amount_left=amount_left+? WHERE id=?",
                                       (amount, ad["id"]))
            await conn.execute("UPDATE p2p_orders SET status='cancelled', closed_at=? WHERE id=?",
                               (int(time.time()), order_id))
            return {**order, "status": "cancelled"}

    async def open_dispute(self, order_id: int, user_id: int, reason: str) -> dict:
        async with self.tx() as conn:
            order = await self._one("SELECT * FROM p2p_orders WHERE id=?", (order_id,))
            if not order or user_id not in (order["buyer_id"], order["seller_id"]):
                raise LedgerError("Сделка не найдена")
            if order["status"] != "paid":
                raise LedgerError("Спор можно открыть только после отметки об оплате")
            await conn.execute(
                "UPDATE p2p_orders SET status='dispute', dispute_by=?, dispute_reason=? WHERE id=?",
                (user_id, reason[:500], order_id))
            return {**order, "status": "dispute", "dispute_by": user_id}

    async def disputes(self) -> list[dict]:
        return await self._all("SELECT * FROM p2p_orders WHERE status='dispute' ORDER BY id")

    async def expired_orders(self) -> list[dict]:
        return await self._all("SELECT * FROM p2p_orders WHERE status='wait_payment' AND pay_deadline<?",
                               (int(time.time()),))

    # --- статистика ---

    async def stats(self) -> dict:
        users = (await self._one("SELECT COUNT(*) AS n FROM users"))["n"]
        supply = {r["currency"]: r["s"] for r in await self._all(
            "SELECT currency, SUM(available+locked) AS s FROM balances GROUP BY currency")}
        deals = (await self._one("SELECT COUNT(*) AS n FROM p2p_orders WHERE status='completed'"))["n"]
        return {"users": users, "supply": supply, "deals": deals}
