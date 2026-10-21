import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from .db import Database
from .gifts import GiftEvent

log = logging.getLogger(__name__)

SEND_DELAY = 0.04  # ~25 сообщений в секунду, в пределах лимитов Bot API


def matches(event: GiftEvent, s: dict) -> bool:
    if not s["enabled"]:
        return False
    if s["collections"] and event.gift_id not in s["collections"]:
        return False
    if s["only_transferable"] and not event.transferable:
        return False
    if s["only_visible_owner"] and event.owner.hidden:
        return False
    if s["max_number"] is not None and event.num > s["max_number"]:
        return False
    if s["min_floor"] is not None and (event.floor_usd is None or event.floor_usd < s["min_floor"]):
        return False
    if s["max_floor"] is not None and (event.floor_usd is None or event.floor_usd > s["max_floor"]):
        return False
    if s["max_model_rarity"] is not None:
        rarity = event.model.rarity if event.model else None
        if rarity is None or rarity > s["max_model_rarity"]:
            return False
    return True


def _owner_html(event: GiftEvent) -> str:
    o = event.owner
    if o.username:
        return f"@{html.escape(o.username)}"
    if o.ton_address:
        addr = html.escape(o.ton_address)
        short = f"{addr[:6]}…{addr[-4:]}"
        return f'<a href="https://tonviewer.com/{addr}">{short}</a> (TON)'
    if o.id and o.name:
        return f'<a href="tg://user?id={o.id}">{html.escape(o.name)}</a>'
    if o.name:
        return html.escape(o.name)
    return "🙈 скрыт"


def _floor_html(event: GiftEvent) -> str:
    parts = []
    if event.floor_usd is not None:
        parts.append(f"${event.floor_usd:,.2f}".replace(",", " "))
    if event.floor_stars is not None:
        parts.append(f"{event.floor_stars:,} ⭐".replace(",", " "))
    return " · ".join(parts) or "нет данных"


def format_event(event: GiftEvent, s: dict) -> str:
    title = html.escape(event.title)
    lines = [
        "🎁 <b>Подарок улучшен!</b>",
        "",
        f'<a href="{event.link}"><b>{title} #{event.num:,}</b></a>'.replace(",", " "),
    ]
    if s["show_attributes"]:
        for label, attr in (("Модель", event.model), ("Фон", event.backdrop), ("Узор", event.pattern)):
            if attr:
                rarity = f" <i>({html.escape(attr.rarity_label)})</i>" if attr.rarity_label else ""
                lines.append(f"• {label}: {html.escape(attr.name)}{rarity}")
    lines.append("")
    if s["show_floor"]:
        lines.append(f"💰 Флор: <b>{_floor_html(event)}</b>")
    if s["show_owner"]:
        lines.append(f"👤 Владелец: {_owner_html(event)}")
    if s["show_transfer"]:
        lines.append(f"🔄 Передача: {html.escape(event.transfer_status)}")
    lines.append(f"📊 Улучшено: {event.issued:,} / {event.total:,}".replace(",", " "))
    return "\n".join(lines)


class Notifier:
    def __init__(self, bot: Bot, db: Database):
        self.bot = bot
        self.db = db
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=50_000)

    async def dispatch(self, event: GiftEvent) -> None:
        log.info("Улучшен %s (флор %s)", event.slug, event.floor_usd)
        for sub in await self.db.subscribers():
            if matches(event, sub["settings"]):
                try:
                    self.queue.put_nowait((sub["id"], event, sub["settings"]))
                except asyncio.QueueFull:
                    log.warning("Очередь уведомлений переполнена")
                    return

    async def _send(self, user_id: int, event: GiftEvent, s: dict) -> None:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎁 Открыть подарок", url=event.link)
        ]])
        for _ in range(3):
            try:
                await self.bot.send_message(
                    user_id,
                    format_event(event, s),
                    reply_markup=keyboard,
                    disable_notification=s["silent"],
                    link_preview_options=LinkPreviewOptions(url=event.link, prefer_large_media=True),
                )
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                await self.db.set_blocked(user_id)
                return
            except Exception as e:
                log.warning("Не удалось отправить %s: %s", user_id, e)
                return

    async def run(self) -> None:
        while True:
            user_id, event, s = await self.queue.get()
            await self._send(user_id, event, s)
            await asyncio.sleep(SEND_DELAY)
