import time
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import config
from ..currencies import CODES, CURRENCIES, address_currency, fmt, fmt_stars, from_decimal, parse_amount, to_decimal
from ..db import Database, LedgerError
from ..rates import Rates
from ..ui import LEDGER_KINDS, back, currency_rows, esc, kb, money, notify, show, user_link

router = Router()


class Send(StatesGroup):
    recipient = State()
    amount = State()


class Swap(StatesGroup):
    amount = State()


class Cheque(StatesGroup):
    amount = State()
    count = State()


MAIN_KB = kb(
    [("📥 Пополнить", "dep"), ("📤 Отправить", "send")],
    [("🔄 Обмен", "swap"), ("🤝 P2P-маркет", "p2p")],
    [("🎫 Чеки", "chq"), ("📜 История", "hist")],
    [("📊 Курсы", "rates"), ("ℹ️ Помощь", "help")],
)


async def main_text(db: Database, rates: Rates, user_id: int) -> str:
    await rates.refresh()
    balances = await db.balances(user_id)
    total = sum(
        to_decimal(b["available"] + b["locked"], code) * rates.price_stars(code)
        for code, b in balances.items()
    )
    lines = [f"🚀 <b>{config.BOT_NAME}</b> — кошелёк OceanGram", "",
             f"💰 Баланс: <b>≈ {fmt_stars(total)}</b>", ""]
    for code in CODES:
        b = balances[code]
        lines.append(f"{CURRENCIES[code].emoji} {code}: {money(b['available'], code, rates)}")
        if b["locked"]:
            lines.append(f"     🔒 в сделках: {fmt(b['locked'], code)}")
    return "\n".join(lines)


async def show_main(target, db: Database, rates: Rates, state: FSMContext | None = None) -> None:
    if state:
        await state.clear()
    await show(target, await main_text(db, rates, target.from_user.id), MAIN_KB)


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, db: Database, rates: Rates,
                state: FSMContext, bot: Bot):
    u = message.from_user
    new = await db.ensure_user(u.id, u.username, u.first_name)
    if new:
        await message.answer(
            f"👋 Добро пожаловать в <b>{config.BOT_NAME}</b>!\n\n"
            "Для вас созданы постоянные адреса для USDT, GRAM и BTC — они закреплены "
            "за вами навсегда. Их можно найти в разделе «📥 Пополнить».\n\n"
            "<i>Адреса и балансы работают внутри Rocket в OceanGram: переводы, обмен, чеки и P2P.</i>"
        )
    arg = command.args or ""
    if arg.startswith("c_"):
        await activate_cheque(message, arg[2:], db, rates, bot)
    await show_main(message, db, rates, state)


@router.callback_query(F.data == "main")
async def main_cb(cb: CallbackQuery, db: Database, rates: Rates, state: FSMContext):
    await show_main(cb, db, rates, state)


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, db: Database, rates: Rates, state: FSMContext):
    await show_main(message, db, rates, state)


# --- пополнение ---

@router.callback_query(F.data == "dep")
async def deposit(cb: CallbackQuery):
    await show(cb, "📥 <b>Пополнение</b>\n\nВыберите валюту:", kb(*currency_rows("dep"), back()))


@router.callback_query(F.data.startswith("dep:"))
async def deposit_currency(cb: CallbackQuery, db: Database):
    code = cb.data.split(":")[1]
    address = (await db.addresses(cb.from_user.id))[code]
    cur = CURRENCIES[code]
    await show(cb, (
        f"📥 <b>Пополнение {cur.emoji} {code}</b>\n\n"
        f"Ваш постоянный адрес {code}:\n<code>{address}</code>\n\n"
        "Нажмите на адрес, чтобы скопировать. Он закреплён за вами навсегда.\n\n"
        f"Пополнить можно переводом от другого пользователя {config.BOT_NAME} "
        f"или купив {code} на P2P-маркете.\n"
        f"<i>Адрес работает только внутри {config.BOT_NAME}.</i>"
    ), kb([("🤝 Купить на P2P", f"p2p:b:{code}")], back("dep")))


# --- перевод ---

@router.callback_query(F.data == "send")
async def send_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "📤 <b>Отправить</b>\n\nВыберите валюту:", kb(*currency_rows("send"), back()))


@router.callback_query(F.data.startswith("send:"))
async def send_currency(cb: CallbackQuery, state: FSMContext, db: Database, rates: Rates):
    code = cb.data.split(":")[1]
    available = (await db.balances(cb.from_user.id))[code]["available"]
    await state.set_state(Send.recipient)
    await state.update_data(code=code)
    await show(cb, (
        f"📤 <b>Отправить {code}</b>\n\nДоступно: {money(available, code, rates)}\n\n"
        f"Отправьте адрес {code} получателя или его @username."
    ), kb(back("send", "Отмена")))


@router.message(Send.recipient, F.text)
async def send_recipient(message: Message, state: FSMContext, db: Database):
    code = (await state.get_data())["code"]
    text = message.text.strip()
    if text.startswith("@"):
        user = await db.find_user(text)
        if not user:
            await message.answer("Пользователь не найден — он должен хотя бы раз запустить бота.")
            return
        address = (await db.addresses(user["id"]))[code]
    else:
        if address_currency(text) != code:
            await message.answer(f"Это не адрес {code}. Проверьте адрес и отправьте ещё раз.")
            return
        owner = await db.address_owner(text)
        if not owner:
            await message.answer("Такой адрес не найден.")
            return
        user, address = await db.get_user(owner[0]), text
    if user["id"] == message.from_user.id:
        await message.answer("Это ваш собственный адрес. Укажите адрес получателя.")
        return
    available = (await db.balances(message.from_user.id))[code]["available"]
    await state.update_data(to_id=user["id"], address=address)
    await state.set_state(Send.amount)
    await message.answer(
        f"Получатель: {user_link(user)}\n<code>{esc(address)}</code>\n\n"
        f"Введите сумму {code}. Доступно: <b>{fmt(available, code)}</b>",
        reply_markup=kb([("Отправить всё", "send_all")], back("send", "Отмена")),
    )


async def _send_confirm(target, state: FSMContext, db: Database, rates: Rates, units: int) -> None:
    data = await state.get_data()
    code = data["code"]
    to = await db.get_user(data["to_id"])
    await state.update_data(units=units)
    await show(target, (
        f"📤 <b>Подтвердите перевод</b>\n\n"
        f"Сумма: {money(units, code, rates)}\n"
        f"Получатель: {user_link(to)}\n<code>{esc(data['address'])}</code>\n\nКомиссия: 0"
    ), kb([("✅ Отправить", "send_ok")], back("main", "❌ Отмена")))


@router.message(Send.amount, F.text)
async def send_amount(message: Message, state: FSMContext, db: Database, rates: Rates):
    code = (await state.get_data())["code"]
    units = parse_amount(message.text, code)
    if not units:
        await message.answer("Не понял сумму. Пример: 10 или 0.5")
        return
    await _send_confirm(message, state, db, rates, units)


@router.callback_query(F.data == "send_all", Send.amount)
async def send_all(cb: CallbackQuery, state: FSMContext, db: Database, rates: Rates):
    code = (await state.get_data())["code"]
    units = (await db.balances(cb.from_user.id))[code]["available"]
    if not units:
        await cb.answer("Баланс пуст", show_alert=True)
        return
    await _send_confirm(cb, state, db, rates, units)


@router.callback_query(F.data == "send_ok", Send.amount)
async def send_ok(cb: CallbackQuery, state: FSMContext, db: Database, rates: Rates, bot: Bot):
    data = await state.get_data()
    code, units = data["code"], data.get("units")
    if not units:
        return await cb.answer()
    await state.clear()
    try:
        await db.transfer(cb.from_user.id, data["to_id"], code, units, data["address"])
    except LedgerError as e:
        await cb.answer(str(e), show_alert=True)
        return
    sender = await db.get_user(cb.from_user.id)
    await notify(bot, data["to_id"], f"📥 Получено {money(units, code, rates)}\nОт: {user_link(sender)}")
    await show(cb, f"✅ Отправлено {money(units, code, rates)}", kb(back("main", "‹ В кошелёк")))


# --- обмен ---

@router.callback_query(F.data == "swap")
async def swap_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "🔄 <b>Обмен</b>\n\nЧто отдаёте?", kb(*currency_rows("swf"), back()))


@router.callback_query(F.data.startswith("swf:"))
async def swap_from(cb: CallbackQuery):
    src = cb.data.split(":")[1]
    rows = [[(f"{CURRENCIES[c].emoji} {c}", f"swt:{src}:{c}") for c in CODES if c != src]]
    await show(cb, f"🔄 <b>Обмен {src}</b>\n\nНа что меняете?", kb(*rows, back("swap")))


@router.callback_query(F.data.startswith("swt:"))
async def swap_to(cb: CallbackQuery, state: FSMContext, db: Database, rates: Rates):
    _, src, dst = cb.data.split(":")
    await rates.refresh()
    available = (await db.balances(cb.from_user.id))[src]["available"]
    rate = rates.convert(Decimal(1), src, dst)
    await state.set_state(Swap.amount)
    await state.update_data(src=src, dst=dst)
    await show(cb, (
        f"🔄 <b>{src} → {dst}</b>\n\n"
        f"Курс: 1 {src} = {rate:.8g} {dst}\nКомиссия: {config.SWAP_FEE_PERCENT}%\n"
        f"Доступно: {money(available, src, rates)}\n\nВведите сумму {src}:"
    ), kb([("Обменять всё", "swap_all")], back("swap", "Отмена")))


def _quote(rates: Rates, src: str, dst: str, units: int) -> int:
    value = rates.convert(to_decimal(units, src), src, dst) * (1 - config.SWAP_FEE_PERCENT / 100)
    return from_decimal(value, dst)


async def _swap_confirm(target, state: FSMContext, rates: Rates, units: int) -> None:
    data = await state.get_data()
    src, dst = data["src"], data["dst"]
    receive = _quote(rates, src, dst, units)
    await state.update_data(units=units)
    await show(target, (
        f"🔄 <b>Подтвердите обмен</b>\n\nОтдаёте: {money(units, src, rates)}\n"
        f"Получаете: {money(receive, dst, rates)}\n"
        f"Комиссия: {config.SWAP_FEE_PERCENT}%\n\n<i>Курс зафиксируется в момент подтверждения.</i>"
    ), kb([("✅ Обменять", "swap_ok")], back("main", "❌ Отмена")))


@router.message(Swap.amount, F.text)
async def swap_amount(message: Message, state: FSMContext, rates: Rates):
    units = parse_amount(message.text, (await state.get_data())["src"])
    if not units:
        await message.answer("Не понял сумму. Пример: 10 или 0.5")
        return
    await _swap_confirm(message, state, rates, units)


@router.callback_query(F.data == "swap_all", Swap.amount)
async def swap_all(cb: CallbackQuery, state: FSMContext, db: Database, rates: Rates):
    units = (await db.balances(cb.from_user.id))[(await state.get_data())["src"]]["available"]
    if not units:
        return await cb.answer("Баланс пуст", show_alert=True)
    await _swap_confirm(cb, state, rates, units)


@router.callback_query(F.data == "swap_ok", Swap.amount)
async def swap_ok(cb: CallbackQuery, state: FSMContext, db: Database, rates: Rates):
    data = await state.get_data()
    if not data.get("units"):
        return await cb.answer()
    await state.clear()
    await rates.refresh()
    src, dst, units = data["src"], data["dst"], data["units"]
    receive = _quote(rates, src, dst, units)
    try:
        await db.swap(cb.from_user.id, src, dst, units, receive)
    except LedgerError as e:
        return await cb.answer(str(e), show_alert=True)
    await show(cb, f"✅ Обмен выполнен\n\n{fmt(units, src)} → {money(receive, dst, rates)}",
               kb(back("main", "‹ В кошелёк")))


# --- чеки ---

def cheque_link(bot_username: str, code: str) -> str:
    return f"{config.LINK_BASE}/{bot_username}?start=c_{code}"


@router.callback_query(F.data == "chq")
async def cheques(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, (
        "🎫 <b>Чеки</b>\n\nЧек — ссылка, по которой можно забрать монеты. "
        "Удобно, чтобы отправить крипту в чат или разыграть среди нескольких человек."
    ), kb([("➕ Создать чек", "chq_new")], [("📋 Мои чеки", "chq_my")], back()))


@router.callback_query(F.data == "chq_new")
async def cheque_new(cb: CallbackQuery):
    await show(cb, "🎫 <b>Новый чек</b>\n\nВыберите валюту:", kb(*currency_rows("chqc"), back("chq")))


@router.callback_query(F.data.startswith("chqc:"))
async def cheque_currency(cb: CallbackQuery, state: FSMContext, db: Database, rates: Rates):
    code = cb.data.split(":")[1]
    available = (await db.balances(cb.from_user.id))[code]["available"]
    await state.set_state(Cheque.amount)
    await state.update_data(code=code)
    await show(cb, f"🎫 Чек в {code}\nДоступно: {money(available, code, rates)}\n\n"
                   "Сколько получит <b>один</b> активировавший? Введите сумму:", kb(back("chq", "Отмена")))


@router.message(Cheque.amount, F.text)
async def cheque_amount(message: Message, state: FSMContext):
    units = parse_amount(message.text, (await state.get_data())["code"])
    if not units:
        return await message.answer("Не понял сумму. Пример: 10 или 0.5")
    await state.update_data(units=units)
    await state.set_state(Cheque.count)
    await message.answer("Сколько человек смогут активировать чек? (1–1000)",
                         reply_markup=kb([("1", "chq_cnt:1"), ("5", "chq_cnt:5"), ("10", "chq_cnt:10")],
                                         back("chq", "Отмена")))


async def _cheque_create(target, state: FSMContext, db: Database, rates: Rates, bot: Bot, count: int):
    data = await state.get_data()
    code, units = data["code"], data["units"]
    try:
        cheque_code = await db.create_cheque(target.from_user.id, code, units, count)
    except LedgerError as e:
        await show(target, f"❌ {e}", kb(back("chq")))
        return
    await state.clear()
    me = await bot.me()
    await show(target, (
        f"✅ <b>Чек создан</b>\n\n{money(units, code, rates)} × {count}\n\n"
        f"Ссылка:\n<code>{cheque_link(me.username, cheque_code)}</code>\n\n"
        "Отправьте её получателям. Неиспользованное можно вернуть в «Мои чеки»."
    ), kb(back("main", "‹ В кошелёк")))


@router.message(Cheque.count, F.text)
async def cheque_count(message: Message, state: FSMContext, db: Database, rates: Rates, bot: Bot):
    if not message.text.strip().isdigit() or not 1 <= int(message.text) <= 1000:
        return await message.answer("Введите число от 1 до 1000")
    await _cheque_create(message, state, db, rates, bot, int(message.text))


@router.callback_query(F.data.startswith("chq_cnt:"), Cheque.count)
async def cheque_count_cb(cb: CallbackQuery, state: FSMContext, db: Database, rates: Rates, bot: Bot):
    await _cheque_create(cb, state, db, rates, bot, int(cb.data.split(":")[1]))


@router.callback_query(F.data == "chq_my")
async def cheque_my(cb: CallbackQuery, db: Database, bot: Bot):
    items = await db.my_cheques(cb.from_user.id)
    if not items:
        return await show(cb, "У вас нет активных чеков.", kb(back("chq")))
    me = await bot.me()
    lines, rows = ["🎫 <b>Активные чеки</b>", ""], []
    for c in items:
        lines.append(f"#{c['id']} · {fmt(c['amount'], c['currency'])} × {c['left']}/{c['total']}\n"
                     f"<code>{cheque_link(me.username, c['code'])}</code>")
        rows.append([(f"❌ Удалить чек #{c['id']}", f"chq_del:{c['id']}")])
    await show(cb, "\n\n".join(lines), kb(*rows, back("chq")))


@router.callback_query(F.data.startswith("chq_del:"))
async def cheque_delete(cb: CallbackQuery, db: Database, bot: Bot):
    try:
        await db.cancel_cheque(int(cb.data.split(":")[1]), cb.from_user.id)
        await cb.answer("Чек удалён, остаток вернулся на баланс")
    except LedgerError as e:
        await cb.answer(str(e), show_alert=True)
    await cheque_my(cb, db, bot)


async def activate_cheque(message: Message, code: str, db: Database, rates: Rates, bot: Bot) -> None:
    try:
        cheque = await db.activate_cheque(code, message.from_user.id)
    except LedgerError as e:
        await message.answer(f"🎫 {e}")
        return
    await message.answer(f"🎫 Чек активирован!\nВы получили {money(cheque['amount'], cheque['currency'], rates)}")
    await notify(bot, cheque["creator_id"],
                 f"🎫 Ваш чек #{cheque['id']} активировал {user_link(await db.get_user(message.from_user.id))}")


# --- история, курсы, помощь ---

@router.callback_query(F.data == "hist")
async def history(cb: CallbackQuery, db: Database):
    items = await db.history(cb.from_user.id)
    if not items:
        return await show(cb, "📜 Операций пока нет.", kb(back()))
    lines = ["📜 <b>Последние операции</b>", ""]
    for h in items:
        sign = "+" if h["delta"] > 0 else "−"
        when = time.strftime("%d.%m %H:%M", time.gmtime(h["created_at"]))
        lines.append(f"{LEDGER_KINDS.get(h['kind'], h['kind'])} · <b>{sign}{fmt(abs(h['delta']), h['currency'])}</b>"
                     f"\n<i>{when}</i>")
    await show(cb, "\n".join(lines), kb(back()))


@router.callback_query(F.data == "rates")
async def rates_screen(cb: CallbackQuery, rates: Rates):
    await rates.refresh()
    lines = ["📊 <b>Курсы</b>", ""]
    for code in CODES:
        lines.append(f"{CURRENCIES[code].emoji} 1 {code} = <b>{fmt_stars(rates.price_stars(code))}</b>"
                     f"  <i>(${rates.price_usd(code):,.6g})</i>")
    lines += ["", f"1 ⭐ = ${config.STAR_PRICE_USD}", "<i>Курсы USDT и BTC — рыночные, обновляются каждую минуту.</i>"]
    await show(cb, "\n".join(lines), kb([("🔄 Обновить", "rates")], back()))


@router.callback_query(F.data == "help")
async def help_screen(cb: CallbackQuery):
    await show(cb, (
        f"ℹ️ <b>{config.BOT_NAME}</b> — кошелёк в OceanGram\n\n"
        "📥 <b>Пополнить</b> — ваши постоянные адреса USDT, GRAM и BTC\n"
        "📤 <b>Отправить</b> — перевод по адресу или @username, без комиссии\n"
        "🔄 <b>Обмен</b> — мгновенный обмен по рыночному курсу\n"
        "🤝 <b>P2P-маркет</b> — покупка и продажа монет за звёзды с гарантом\n"
        "🎫 <b>Чеки</b> — отправка монет ссылкой\n\n"
        "Монеты и адреса работают внутри Rocket. /start — главное меню, /cancel — отмена."
    ), kb(back()))
