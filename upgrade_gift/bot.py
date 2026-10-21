import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from . import config
from .db import Database


def _app_keyboard(text: str = "⚙️ Открыть Upgrade Gift") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, web_app=WebAppInfo(url=config.WEBAPP_URL))
    ]])


def _sub_text(user: dict) -> str:
    if user["sub_until"] > time.time():
        until = time.strftime("%d.%m.%Y %H:%M", time.gmtime(user["sub_until"]))
        state = "включены" if user["settings"]["enabled"] else "выключены в настройках"
        return f"✅ Подписка активна до <b>{until} UTC</b>\n🔔 Уведомления {state}."
    return "❌ Подписки нет — уведомления не приходят.\nОформите её в мини-аппе 👇"


def create_router(db: Database) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message):
        u = message.from_user
        await db.upsert_user(u.id, u.username, u.first_name)
        user = await db.get_user(u.id)
        await message.answer(
            f"🎁 <b>{config.BOT_NAME}</b>\n\n"
            "Я слежу за улучшением подарков в Telegram и сразу присылаю:\n"
            "• ссылку на подарок\n"
            "• флор коллекции\n"
            "• владельца\n"
            "• доступен ли подарок к передаче\n\n"
            f"{_sub_text(user)}\n\n"
            "В мини-аппе можно купить подписку и настроить, какие уведомления и по каким подаркам приходят.",
            reply_markup=_app_keyboard(),
        )

    @router.message(Command("status"))
    async def status(message: Message):
        u = message.from_user
        await db.upsert_user(u.id, u.username, u.first_name)
        await message.answer(_sub_text(await db.get_user(u.id)), reply_markup=_app_keyboard())

    @router.message(Command("help"))
    async def help_(message: Message):
        await message.answer(
            "/start — главное меню\n/status — статус подписки\n\n"
            "Все настройки и оплата — в мини-аппе.",
            reply_markup=_app_keyboard(),
        )

    @router.message(Command("grant"), F.from_user.id.in_(config.ADMIN_IDS))
    async def grant(message: Message, command: CommandObject):
        try:
            user_id, days = (int(x) for x in (command.args or "").split())
        except ValueError:
            await message.answer("Использование: /grant <user_id> <дней>")
            return
        if not await db.get_user(user_id):
            await message.answer("Пользователь ещё не запускал бота.")
            return
        until = await db.extend_subscription(user_id, days)
        await message.answer(f"Готово, подписка до {time.strftime('%d.%m.%Y %H:%M', time.gmtime(until))} UTC")

    @router.message(Command("stats"), F.from_user.id.in_(config.ADMIN_IDS))
    async def stats(message: Message):
        s = await db.stats()
        await message.answer(
            f"👥 Пользователей: {s['users']}\n✅ Активных подписок: {s['subs']}\n"
            f"💳 Оплат: {s['paid']} на {s['revenue']:g} {config.XROCKET_CURRENCY}"
        )

    return router


async def setup_bot(bot: Bot, dp: Dispatcher, db: Database) -> None:
    dp.include_router(create_router(db))
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="status", description="Статус подписки"),
        BotCommand(command="help", description="Помощь"),
    ])
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Upgrade Gift", web_app=WebAppInfo(url=config.WEBAPP_URL))
    )
