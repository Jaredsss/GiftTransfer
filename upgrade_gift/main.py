import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web
from telethon import TelegramClient
from telethon.sessions import StringSession

from . import config
from .bot import setup_bot
from .db import Database
from .monitor import Monitor
from .notifier import Notifier
from .payments import Billing, XRocket
from .usernames import UsernameHunter
from .webapp import create_app

log = logging.getLogger("upgrade_gift")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан (.env)")

    db = Database(config.DB_PATH)
    await db.connect()

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    await setup_bot(bot, dp, db)

    xrocket = XRocket(config.XROCKET_API_KEY) if config.XROCKET_API_KEY else None
    billing = Billing(xrocket, db, bot) if xrocket else None
    if not billing:
        log.warning("XROCKET_API_KEY не задан — оплата отключена")
    else:
        try:
            info = await xrocket.app_info()
            log.info("xRocket: приложение %s", info.get("name"))
        except Exception as e:
            log.error("xRocket: ключ не работает (%s) — счета создаваться не будут", e)

    notifier = Notifier(bot, db)
    tasks = [asyncio.create_task(notifier.run())]
    if billing:
        tasks.append(asyncio.create_task(billing.poll()))

    client = monitor = hunter = None
    if config.TG_SESSION and config.TG_API_ID and config.TG_API_HASH:
        client = TelegramClient(StringSession(config.TG_SESSION), config.TG_API_ID, config.TG_API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            raise SystemExit("TG_SESSION недействительна — создайте новую: python -m scripts.create_session")
        monitor = Monitor(client, db, notifier.dispatch)
        tasks.append(asyncio.create_task(monitor.run()))
        log.info("Мониторинг улучшений запущен")
        if config.USERNAME_HUNTER:
            hunter = UsernameHunter(client, db, notifier.dispatch_username)
            tasks.append(asyncio.create_task(hunter.run()))
    else:
        log.warning("TG_API_ID / TG_API_HASH / TG_SESSION не заданы — мониторинг подарков отключён")

    runner = web.AppRunner(create_app(db, billing, monitor, hunter))
    await runner.setup()
    await web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT).start()
    log.info("Мини-апп: http://%s:%s -> %s", config.WEB_HOST, config.WEB_PORT, config.WEBAPP_URL)

    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        if client:
            await client.disconnect()
        if xrocket:
            await xrocket.close()
        await runner.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
