import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, CallbackQuery, Message, TelegramObject

from . import config
from .db import Database
from .handlers import admin, p2p, wallet
from .rates import Rates

log = logging.getLogger("rocket")


class BanMiddleware(BaseMiddleware):
    """Заблокированные пользователи не могут пользоваться ботом."""

    def __init__(self, db: Database):
        self.db = db

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        user = data.get("event_from_user")
        if user and user.id not in config.ADMIN_IDS:
            row = await self.db.get_user(user.id)
            if row and row["banned"]:
                if isinstance(event, CallbackQuery):
                    await event.answer("Аккаунт заблокирован", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("🚫 Аккаунт заблокирован. Обратитесь в поддержку.")
                return None
        return await handler(event, data)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан (.env)")

    db = Database(config.DB_PATH)
    await db.connect()
    rates = Rates()

    # Bot API OceanGram совместим с Telegram — меняется только адрес сервера
    session = AiohttpSession(api=TelegramAPIServer.from_base(config.API_BASE))
    bot = Bot(config.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    dp = Dispatcher(db=db, rates=rates)
    dp.message.outer_middleware(BanMiddleware(db))
    dp.callback_query.outer_middleware(BanMiddleware(db))
    dp.include_routers(admin.router, wallet.router, p2p.router)

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Кошелёк"),
            BotCommand(command="cancel", description="Отмена"),
        ])
    except Exception as e:
        log.warning("Не удалось установить команды: %s", e)

    tasks = [asyncio.create_task(rates.run()), asyncio.create_task(p2p.expire_loop(bot, db))]
    me = await bot.me()
    log.info("%s запущен: @%s через %s", config.BOT_NAME, me.username, config.API_BASE)
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
