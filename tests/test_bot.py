"""Сквозные сценарии: настоящий Dispatcher + поддельный Bot API сервер."""
import itertools
import json
import re
import time

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import Update
from aiohttp import web

from rocket import config
from rocket.db import Database
from rocket.handlers import admin, p2p, wallet
from rocket.main import BanMiddleware
from rocket.rates import Rates

ADMIN = 100
BOT_USER = {"id": 1, "is_bot": True, "first_name": "Rocket", "username": "rocket_bot"}


class FakeApi:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.ids = itertools.count(1000)

    async def handle(self, request: web.Request):
        method = request.match_info["method"]
        data = dict(await request.post()) if request.can_read_body else {}
        self.calls.append((method, data))
        if method == "getMe":
            result = BOT_USER
        elif method in ("sendMessage", "editMessageText"):
            chat_id = int(data.get("chat_id") or 0)
            result = {"message_id": next(self.ids), "date": int(time.time()),
                      "chat": {"id": chat_id, "type": "private"}, "text": data.get("text", "")}
        elif method == "copyMessage":
            result = {"message_id": next(self.ids)}
        else:
            result = True
        return web.json_response({"ok": True, "result": result})

    def texts(self, chat_id: int) -> list[str]:
        return [d.get("text", "") for m, d in self.calls
                if m in ("sendMessage", "editMessageText") and str(d.get("chat_id")) == str(chat_id)]

    def last(self, chat_id: int) -> str:
        return self.texts(chat_id)[-1]

    def alerts(self) -> list[str]:
        return [d.get("text", "") for m, d in self.calls if m == "answerCallbackQuery" and d.get("text")]


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", {ADMIN})
    api = FakeApi()
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", api.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    db = Database(str(tmp_path / "bot.db"))
    await db.connect()
    rates = Rates()
    rates.usd.update({"USDT": 1, "BTC": 100000, "GRAM": 0.005})
    rates.updated_at = time.time() + 10**9  # без запросов в интернет
    from decimal import Decimal
    rates.usd = {k: Decimal(str(v)) for k, v in rates.usd.items()}

    session = AiohttpSession(api=TelegramAPIServer.from_base(f"http://127.0.0.1:{port}"))
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db, rates=rates)
    dp.message.outer_middleware(BanMiddleware(db))
    dp.callback_query.outer_middleware(BanMiddleware(db))
    for r in (admin.router, wallet.router, p2p.router):
        r._parent_router = None
    dp.include_routers(admin.router, wallet.router, p2p.router)

    counter = itertools.count(1)

    def user(uid):
        return {"id": uid, "is_bot": False, "first_name": f"U{uid}", "username": f"u{uid}"}

    async def say(uid, text):
        update = {"update_id": next(counter), "message": {
            "message_id": next(counter), "date": int(time.time()), "chat": {"id": uid, "type": "private"},
            "from": user(uid), "text": text,
            **({"entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}
               if text.startswith("/") else {}),
        }}
        await dp.feed_update(bot, Update.model_validate(update, context={"bot": bot}))

    async def press(uid, data):
        update = {"update_id": next(counter), "callback_query": {
            "id": str(next(counter)), "from": user(uid), "chat_instance": "x", "data": data,
            "message": {"message_id": 1, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                        "from": BOT_USER, "text": "screen"},
        }}
        await dp.feed_update(bot, Update.model_validate(update, context={"bot": bot}))

    yield type("Env", (), {"api": api, "db": db, "say": say, "press": press})
    await bot.session.close()
    await db.close()
    await runner.cleanup()


async def usdt(env, uid):
    return (await env.db.balances(uid))["USDT"]


async def test_full_flow(env):
    api, db, say, press = env.api, env.db, env.say, env.press
    for uid in (10, 11, ADMIN):
        await say(uid, "/start")
    assert "Добро пожаловать" in api.texts(10)[0] and "Баланс" in api.last(10)
    addr11 = (await db.addresses(11))["USDT"]

    # пополнение показывает постоянный адрес
    await press(10, "dep:USDT")
    assert (await db.addresses(10))["USDT"] in api.last(10)

    # админ начисляет монеты
    await say(ADMIN, "/mint @u10 USDT 100")
    assert (await usdt(env, 10))["available"] == 100_000_000 and "Зачислено" in api.last(10)

    # перевод по адресу
    await press(10, "send:USDT")
    await say(10, "GRAMxxxx")
    assert "Это не адрес USDT" in api.last(10)
    await say(10, addr11)
    await say(10, "5")
    assert "Подтвердите перевод" in api.last(10)
    await press(10, "send_ok")
    assert (await usdt(env, 11))["available"] == 5_000_000 and "Получено" in api.last(11)

    # перевод по @username
    await press(11, "send:USDT")
    await say(11, "@u10")
    await press(11, "send_all")
    await press(11, "send_ok")
    assert (await usdt(env, 11))["available"] == 0

    # обмен USDT -> BTC по курсу 100000 с комиссией 1%
    await press(10, "swt:USDT:BTC")
    await say(10, "10")
    await press(10, "swap_ok")
    b = await db.balances(10)
    assert b["USDT"]["available"] == 90_000_000 and b["BTC"]["available"] == 9900

    # чек на 2 активации
    await press(10, "chqc:USDT")
    await say(10, "1")
    await press(10, "chq_cnt:2")
    code = re.search(r"start=c_([\w-]+)", api.last(10)).group(1)
    await say(11, f"/start c_{code}")
    assert (await usdt(env, 11))["available"] == 1_000_000
    await say(11, f"/start c_{code}")
    assert "уже активировали" in " ".join(api.texts(11))

    # P2P: 10 продаёт 50 USDT по 70 ⭐
    await press(10, "new")
    await press(10, "new:sell")
    await press(10, "newc:sell:USDT")
    await say(10, "70")
    await say(10, "50")
    await say(10, "1-20")
    await press(10, "newm:stars")
    await press(10, "newt:skip")
    await press(10, "new_ok")
    assert "опубликовано" in api.last(10)
    assert await usdt(env, 10) == {"available": 38_000_000, "locked": 50_000_000}

    # 11 покупает 10 USDT
    await press(11, "p2p:b:USDT")
    markup = json.loads([d for m, d in api.calls if m == "editMessageText"][-1]["reply_markup"])
    buttons = [b for row in markup["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "ad:1" and "70 ⭐" in b["text"] for b in buttons)
    await press(11, "adtake:1")
    await say(11, "100")  # больше лимита
    assert "вне лимитов" in api.last(11)
    await say(11, "10")
    assert "700 ⭐" in api.last(11)
    await press(11, "take_ok")
    assert "Новая сделка #1" in api.last(10)
    await press(10, "release:1")  # ещё не оплачено
    assert "недоступно" in api.alerts()[-1]
    await press(11, "paid:1")
    assert "отметил оплату" in api.last(10)
    await press(10, "release:1")
    await press(10, "release!:1")
    assert (await usdt(env, 11))["available"] == 11_000_000
    assert "завершена" in api.last(11)

    # Спор: 11 оплатил, но продавец не согласен
    await press(11, "adtake:1")
    await say(11, "5")
    await press(11, "take_ok")
    await press(11, "paid:2")
    await press(11, "disp:2")  # покупатель должен подождать
    assert "через" in api.alerts()[-1]
    await press(10, "disp:2")
    await say(10, "оплата не пришла")
    assert "Новый спор" in api.last(ADMIN)
    # чат по сделке во время спора видит и модератор
    await press(11, "chat:2")
    await say(11, "вот чек оплаты")
    assert any(m == "copyMessage" and d["chat_id"] == str(ADMIN) for m, d in api.calls)
    await press(ADMIN, "adm_seller:2")
    assert (await db.get_order(2))["status"] == "cancelled"
    assert (await db.get_ad(1))["amount_left"] == 40_000_000  # монеты вернулись в объявление

    # закрытие объявления разблокирует остаток
    await press(10, "adclose:1")
    assert await usdt(env, 10) == {"available": 78_000_000, "locked": 0}

    # бан
    await say(ADMIN, "/ban @u11")
    await say(11, "/start")
    assert "заблокирован" in api.last(11)
    # не-админ не может выпускать монеты
    await say(10, "/mint @u10 USDT 1000")
    assert (await usdt(env, 10))["available"] == 78_000_000
