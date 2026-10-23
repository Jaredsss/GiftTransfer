"""Общие элементы интерфейса: кнопки, форматирование, показ экранов."""
import html
import logging
import time
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import config
from .currencies import CODES, CURRENCIES, fmt, fmt_stars, to_decimal
from .rates import Rates

log = logging.getLogger(__name__)

LEDGER_KINDS = {
    "send": "📤 Перевод",
    "receive": "📥 Получено",
    "swap_out": "🔄 Обмен",
    "swap_in": "🔄 Обмен",
    "mint": "🎁 Начисление",
    "burn": "➖ Списание",
    "cheque_create": "🎫 Создан чек",
    "cheque_in": "🎫 Активирован чек",
    "cheque_refund": "🎫 Возврат по чеку",
    "p2p_sell": "🤝 P2P продажа",
    "p2p_buy": "🤝 P2P покупка",
}


def kb(*rows) -> InlineKeyboardMarkup:
    """kb([("Текст", "callback"), ...], ...); callback с http — кнопка-ссылка."""
    keyboard = []
    for row in rows:
        if not row:
            continue
        buttons = []
        for text, data in row:
            if data.startswith(("http://", "https://", "tg://")):
                buttons.append(InlineKeyboardButton(text=text, url=data))
            else:
                buttons.append(InlineKeyboardButton(text=text, callback_data=data))
        keyboard.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def back(data: str = "main", text: str = "‹ Назад") -> list:
    return [(text, data)]


def currency_rows(prefix: str) -> list:
    return [[(f"{CURRENCIES[c].emoji} {c}", f"{prefix}:{c}") for c in CODES]]


def esc(text) -> str:
    return html.escape(str(text or ""))


def user_link(user: dict | None) -> str:
    if not user:
        return "—"
    if user.get("username"):
        return f"@{esc(user['username'])}"
    return esc(user.get("first_name") or f"id{user['id']}")


def money(units: int, code: str, rates: Rates) -> str:
    stars = to_decimal(units, code) * rates.price_stars(code)
    return f"<b>{fmt(units, code)}</b>" + (f" <i>≈ {fmt_stars(stars)}</i>" if units else "")


def price_text(price: Decimal) -> str:
    return fmt_stars(price)


def clock(ts: int) -> str:
    return time.strftime("%H:%M", time.gmtime(ts)) + " UTC"


async def show(target: Message | CallbackQuery, text: str, markup=None) -> None:
    """Показывает экран: на кнопке — редактирует сообщение, иначе — отправляет новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await target.message.answer(text, reply_markup=markup)
        try:
            await target.answer()
        except TelegramBadRequest:
            pass
    else:
        await target.answer(text, reply_markup=markup)


async def notify(bot: Bot, user_id: int, text: str, markup=None) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=markup)
    except Exception as e:
        log.info("Не удалось уведомить %s: %s", user_id, e)


async def notify_admins(bot: Bot, text: str, markup=None) -> None:
    for admin_id in config.ADMIN_IDS:
        await notify(bot, admin_id, text, markup)
