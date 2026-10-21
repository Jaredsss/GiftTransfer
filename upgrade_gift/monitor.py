"""Отслеживание улучшений подарков.

Каждый улучшенный подарок получает следующий порядковый номер в своей
коллекции (PlushPepe-1, PlushPepe-2, ...). Монитор помнит последний номер
каждой коллекции и опрашивает следующий: как только `Slug-(N+1)` начинает
существовать — подарок только что улучшили.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import types
from telethon.tl.functions.payments import (
    GetResaleStarGiftsRequest,
    GetStarGiftsRequest,
    GetUniqueStarGiftRequest,
    GetUniqueStarGiftValueInfoRequest,
)

from . import config
from .db import Database
from .gifts import GiftEvent, parse_unique_gift, slug_base

log = logging.getLogger(__name__)

COLLECTIONS_REFRESH = 30 * 60   # обновление списка коллекций
INACTIVE_RECHECK = 15 * 60      # повторная проверка коллекций без улучшений
RESYNC_INTERVAL = 5 * 60        # сверка с availability_issued
BURST = 25                      # максимум новых подарков коллекции за один проход
MAX_CATCHUP = 30                # сколько пропущенных номеров догонять при сверке


class Monitor:
    def __init__(self, client: TelegramClient, db: Database,
                 on_event: Callable[[GiftEvent], Awaitable[None]]):
        self.client = client
        self.db = db
        self.on_event = on_event
        self._sem = asyncio.Semaphore(max(1, config.SCAN_CONCURRENCY))
        self._floor_cache: dict[int, tuple[float, float | None, int | None]] = {}
        self._resync_at: dict[int, float] = {}

    # --- запросы к Telegram ---

    async def _call(self, request):
        while True:
            try:
                async with self._sem:
                    return await self.client(request)
            except FloodWaitError as e:
                log.warning("FloodWait %ss на %s", e.seconds, type(request).__name__)
                await asyncio.sleep(e.seconds + 1)

    async def fetch(self, slug: str):
        try:
            return await self._call(GetUniqueStarGiftRequest(slug=slug))
        except RPCError as e:
            if "INVALID" not in (e.message or "") and "NOT_FOUND" not in (e.message or ""):
                log.debug("%s: %s", slug, e)
            return None

    async def floor(self, gift_id: int, slug: str) -> tuple[float | None, int | None]:
        """Флор коллекции: в $ (оценка Telegram) и в ⭐ (минимальная цена на маркете)."""
        cached = self._floor_cache.get(gift_id)
        if cached and time.time() - cached[0] < config.FLOOR_CACHE_TTL:
            return cached[1], cached[2]

        floor_usd = floor_stars = None
        try:
            info = await self._call(GetUniqueStarGiftValueInfoRequest(slug=slug))
            # floor_price приходит в центах валюты (USD)
            if info.floor_price is not None and info.currency.upper() == "USD":
                floor_usd = info.floor_price / 100
            else:
                log.info("Флор %s: нет оценки в $ (currency=%s floor=%s)",
                         slug, info.currency, info.floor_price)
        except RPCError as e:
            log.warning("Флор (value info) %s: %s", slug, e)
        try:
            resale = await self._call(GetResaleStarGiftsRequest(
                gift_id=gift_id, offset="", limit=1, sort_by_price=True, stars_only=True,
            ))
            if resale.gifts and resale.gifts[0].resell_amount:
                for amount in resale.gifts[0].resell_amount:
                    if isinstance(amount, types.StarsAmount):
                        floor_stars = amount.amount
                        break
        except RPCError as e:
            log.warning("Флор (маркет) %s: %s", gift_id, e)

        self._floor_cache[gift_id] = (time.time(), floor_usd, floor_stars)
        return floor_usd, floor_stars

    # --- коллекции ---

    async def refresh_collections(self) -> None:
        result = await self._call(GetStarGiftsRequest(hash=0))
        count = 0
        for gift in getattr(result, "gifts", []):
            if not isinstance(gift, types.StarGift) or not gift.title:
                continue
            if not (gift.limited or gift.upgrade_stars):
                continue
            await self.db.upsert_collection(
                gift.id, gift.title, slug_base(gift.title), gift.availability_total or 0
            )
            count += 1
        log.info("Коллекций для отслеживания: %s", count)

    async def _init_collection(self, col: dict) -> None:
        """Определяет текущий последний номер, чтобы не слать старые подарки."""
        now = int(time.time())
        for num in (1, 2, 3):
            res = await self.fetch(f"{col['slug_base']}-{num}")
            if not res:
                continue
            event = parse_unique_gift(res)
            if not event:
                continue
            base = event.slug.rsplit("-", 1)[0]
            await self.db.update_collection(
                col["gift_id"], slug_base=base, last_num=event.issued, issued=event.issued,
                total=event.total, active=1, checked_at=now,
            )
            log.info("%s: улучшено %s/%s", col["title"], event.issued, event.total)
            return
        await self.db.update_collection(col["gift_id"], checked_at=now)

    async def _emit(self, col: dict, event: GiftEvent) -> None:
        event.floor_usd, event.floor_stars = await self.floor(col["gift_id"], event.slug)
        try:
            await self.on_event(event)
        except Exception:
            log.exception("Ошибка обработки события %s", event.slug)

    async def _scan_collection(self, col: dict) -> None:
        last = col["last_num"]
        start = last
        issued = col["issued"]

        for _ in range(BURST):
            res = await self.fetch(f"{col['slug_base']}-{last + 1}")
            event = parse_unique_gift(res) if res else None
            if not event:
                break
            last = event.num if event.num > last else last + 1
            issued = max(issued, event.issued)
            await self._emit(col, event)

        # Сверка: если номера идут с пропусками, availability_issued покажет это
        gid = col["gift_id"]
        if last == start and time.time() >= self._resync_at.get(gid, 0):
            self._resync_at[gid] = time.time() + RESYNC_INTERVAL
            res = await self.fetch(f"{col['slug_base']}-{max(last, 1)}")
            event = parse_unique_gift(res) if res else None
            if event and event.issued > last + 1:
                issued = event.issued
                target = min(issued, last + 1 + MAX_CATCHUP)
                for num in range(last + 2, target + 1):
                    res = await self.fetch(f"{col['slug_base']}-{num}")
                    found = parse_unique_gift(res) if res else None
                    if found:
                        last = num
                        await self._emit(col, found)
                if issued > target:
                    last = issued  # слишком сильно отстали — перескакиваем

        if last != start or issued != col["issued"]:
            await self.db.update_collection(gid, last_num=last, issued=issued)

    # --- главный цикл ---

    async def run(self) -> None:
        last_refresh = 0.0
        while True:
            try:
                if time.time() - last_refresh > COLLECTIONS_REFRESH:
                    await self.refresh_collections()
                    last_refresh = time.time()

                now = time.time()
                cols = await self.db.collections()
                tasks = []
                for col in cols:
                    if col["active"]:
                        tasks.append(self._scan_collection(col))
                    elif now - col["checked_at"] > INACTIVE_RECHECK:
                        tasks.append(self._init_collection(col))
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        log.error("Ошибка сканирования: %r", r)
            except Exception:
                log.exception("Ошибка цикла мониторинга")
                await asyncio.sleep(10)
            await asyncio.sleep(config.SCAN_INTERVAL)
