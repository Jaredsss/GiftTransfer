"""Поиск свободных красивых юзернеймов для ботов.

Telegram добавляет новые окончания для юзернеймов ботов (ai, game, app, agent).
Охотник перебирает «красивые» сочетания слово + окончание, проверяет их и
сохраняет результат. Порядок проверки:
1. t.me/<имя> — если у имени есть страница, оно точно занято (дёшево, без MTProto);
2. account.checkUsername — точный ответ: свободно / продаётся на Fragment / занято.
"""
import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    RPCError,
    UsernameInvalidError,
    UsernameOccupiedError,
    UsernamePurchaseAvailableError,
)
from telethon.tl.functions.account import CheckUsernameRequest

from . import config
from .db import Database

log = logging.getLogger(__name__)

SUFFIXES = ["ai", "game", "app", "agent", "bot"]
SUFFIX_BONUS = {"ai": 15, "app": 10, "game": 10, "agent": 8, "bot": 0}

# Слова с высоким спросом: деньги, крипта, ИИ, игры, коммерция
PREMIUM = """
pay cash money bank coin crypto ton nft gift star stars trade trader buy sell shop store market deal
win bet casino poker slot lucky jackpot vip pro gold rich lux luxury prime elite royal king queen boss
chat gpt brain mind smart genius think code dev data cloud web net link tech robot neo meta
game play player arena quest hero battle war fight race racer score level boss
news media music movie film video photo art design style love date dating friend social
travel taxi food pizza coffee shop deliver health doctor fit sport bet
""".split()

# Большой словарь осмысленных слов — words.txt рядом с модулем
_WORDS_FILE = Path(__file__).with_name("words.txt")
EXTRA_WORDS = [
    w for line in _WORDS_FILE.read_text(encoding="utf-8").splitlines()
    if not line.startswith("#") for w in line.split()
]

WORDS = sorted(set(PREMIUM + EXTRA_WORDS + """
ask help support assist answer search find seek guide tutor teach learn study school quiz test exam
write writer text translate voice speak talk story book read poem joke meme fun happy smile
sale sales offer promo bonus reward earn invest wallet swap mint drop air airdrop farm stake
job work hire task todo plan note notes remind alarm time clock calendar event ticket
home house rent car auto drive map city world global planet space moon sun sky star fire ice
wolf lion tiger bear fox cat dog bird shark dragon phoenix eagle
red blue black white green pink alpha beta omega zero one nova pixel byte bit chip quantum
cyber magic dream hype vibe mood wave flow spark flash rocket jet turbo super mega ultra hyper
easy fast quick free best top first smart real true open
""".split()))

# Короткие основы: из них получаются самые дорогие имена (xgame, goapp, myagent)
SHORT = list("abcdefghijklmnopqrstuvwxyz") + """
go my ok hi me up io ux tv vr ar ex on in ai gg pp vip pay win bet top fun art car sky
""".split()

STATUS_RECHECK = {
    "free": 6 * 3600,
    "fragment": 24 * 3600,
    "taken": 7 * 86400,
    "invalid": 30 * 86400,
}

_VALID = re.compile(r"^[a-z][a-z0-9_]{3,31}$")


def is_valid(name: str) -> bool:
    return bool(_VALID.match(name)) and not name.endswith("_") and "__" not in name


def split_name(name: str) -> tuple[str, str]:
    for suffix in sorted(SUFFIXES, key=len, reverse=True):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], suffix
    return name, ""


def score(name: str) -> int:
    """Оценка «красоты» и будущей ценности 0..100: короче, осмысленнее, без цифр — дороже."""
    base, suffix = split_name(name)
    base = base.rstrip("_")
    s = {1: 60, 2: 50, 3: 34, 4: 28, 5: 22, 6: 16, 7: 10}.get(len(base), 4)
    if base in PREMIUM:
        s += 25
    elif base in WORDS:
        s += 15
    total = len(name)
    s += 15 if total <= 6 else 8 if total <= 8 else 0
    s += SUFFIX_BONUS.get(suffix, 0)
    if re.search(r"[0-9_]", name):
        s -= 20
    return max(0, min(100, s))


_CANDIDATES: list[str] = []


def candidates() -> list[str]:
    if _CANDIDATES:
        return _CANDIDATES
    names = set()
    for base in WORDS + SHORT:
        for suffix in SUFFIXES:
            name = base + suffix
            if is_valid(name):
                names.add(name)
    _CANDIDATES.extend(sorted(names, key=lambda n: (-score(n), n)))
    return _CANDIDATES


class UsernameHunter:
    def __init__(self, client: TelegramClient, db: Database,
                 on_found: Callable[[dict], Awaitable[None]] | None = None,
                 interval: float = config.USERNAME_CHECK_INTERVAL):
        self.client = client
        self.db = db
        self.on_found = on_found
        self.interval = interval
        self.urgent: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._pause_until = 0.0
        self._http: aiohttp.ClientSession | None = None

    # --- проверка одного имени ---

    async def _page_exists(self, name: str) -> bool | None:
        """True — у имени есть страница на t.me (занято); None — не удалось понять."""
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),
                                               headers={"User-Agent": "Mozilla/5.0"})
        try:
            async with self._http.get(f"https://t.me/{name}") as resp:
                if resp.status != 200:
                    return None
                page = await resp.text()
        except Exception:
            return None
        return 'class="tgme_page_title"' in page

    async def check(self, name: str) -> str:
        if not is_valid(name):
            return "invalid"
        if await self._page_exists(name):
            return "taken"
        try:
            free = await self.client(CheckUsernameRequest(username=name))
            return "free" if free else "taken"
        except UsernamePurchaseAvailableError:
            return "fragment"
        except UsernameOccupiedError:
            return "taken"
        except UsernameInvalidError:
            return "invalid"

    # --- очередь ---

    def request(self, names: list[str]) -> None:
        """Срочная проверка (из мини-аппа) — вне очереди фонового перебора."""
        for name in names:
            if name not in self._queued:
                self._queued.add(name)
                self.urgent.put_nowait(name)

    async def _next_background(self) -> str | None:
        now = time.time()
        known = {r["name"]: r for r in await self.db.usernames_all()}
        for name in candidates():
            row = known.get(name)
            if not row or now - row["checked_at"] > STATUS_RECHECK.get(row["status"], 86400):
                return name
        return None

    async def _process(self, name: str) -> None:
        previous = await self.db.get_username(name)
        try:
            status = await self.check(name)
        except FloodWaitError as e:
            self._pause_until = time.time() + e.seconds + 1
            log.warning("Юзернеймы: FloodWait %s с, пауза", e.seconds)
            self.request([name])
            return
        except RPCError as e:
            log.warning("Юзернейм %s: %s", name, e)
            status = "unknown"
        base, suffix = split_name(name)
        item = await self.db.save_username(name, suffix, score(name), status)
        was_free = previous and previous["status"] == "free"
        if status == "free" and not was_free and self.on_found:
            try:
                await self.on_found(item)
            except Exception:
                log.exception("Уведомление о юзернейме %s", name)

    async def run(self) -> None:
        log.info("Поиск юзернеймов запущен: %s вариантов", len(candidates()))
        while True:
            try:
                wait = self._pause_until - time.time()
                if wait > 0:
                    await asyncio.sleep(min(wait, 60))
                    continue
                if not self.urgent.empty():
                    name = self.urgent.get_nowait()
                    self._queued.discard(name)
                else:
                    name = await self._next_background()
                    if name is None:
                        await asyncio.sleep(300)
                        continue
                await self._process(name)
            except Exception:
                log.exception("Ошибка поиска юзернеймов")
            await asyncio.sleep(self.interval)
