"""P2P-маркет: монеты за звёзды OceanGram с гарантом (эскроу) и спорами."""
import asyncio
import logging
import time
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import config
from ..currencies import CURRENCIES, fmt, fmt_stars, parse_amount
from ..db import Database, LedgerError
from ..rates import Rates
from ..ui import back, clock, currency_rows, esc, kb, money, notify, notify_admins, show, user_link

log = logging.getLogger(__name__)
router = Router()

METHODS = {"stars": "⭐ Перевод звёзд", "gifts": "🎁 Подарками"}
STATUS = {
    "wait_payment": "⏳ Ожидает оплаты",
    "paid": "💸 Покупатель отметил оплату",
    "dispute": "⚠️ Открыт спор",
    "completed": "✅ Завершена",
    "cancelled": "❌ Отменена",
}


class Take(StatesGroup):
    amount = State()


class NewAd(StatesGroup):
    price = State()
    amount = State()
    limits = State()
    method = State()
    terms = State()


class Dispute(StatesGroup):
    reason = State()


class Chat(StatesGroup):
    active = State()


def parse_price(text: str) -> Decimal | None:
    try:
        value = Decimal(text.strip().replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01")) if value.is_finite() and value > 0 else None


# --- меню ---

@router.callback_query(F.data == "p2p")
async def p2p_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, (
        "🤝 <b>P2P-маркет</b>\n\n"
        "Покупайте и продавайте монеты за звёзды OceanGram напрямую у других пользователей.\n\n"
        "🛡 <b>Безопасно:</b> монеты продавца блокируются ботом и уходят покупателю только после "
        "подтверждения оплаты. Если что-то пошло не так — откройте спор, модератор разберётся."
    ), kb(
        [("🟢 Купить", "p2p:b"), ("🔴 Продать", "p2p:s")],
        [("➕ Разместить объявление", "new")],
        [("📋 Мои объявления", "myads"), ("📦 Мои сделки", "myord")],
        back(),
    ))


@router.callback_query(F.data.in_({"p2p:b", "p2p:s"}))
async def p2p_side(cb: CallbackQuery):
    side = cb.data[-1]
    title = "🟢 Что хотите купить?" if side == "b" else "🔴 Что хотите продать?"
    await show(cb, title, kb(*currency_rows(f"p2p:{side}"), back("p2p")))


@router.callback_query(F.data.regexp(r"^p2p:[bs]:[A-Z]+$"))
async def p2p_list(cb: CallbackQuery, db: Database, rates: Rates):
    _, side, code = cb.data.split(":")
    await rates.refresh()
    # Покупатель смотрит объявления о продаже и наоборот
    ads = await db.list_ads("sell" if side == "b" else "buy", code)
    market = rates.price_stars(code)
    action = "Купить" if side == "b" else "Продать"
    lines = [f"{'🟢' if side == 'b' else '🔴'} <b>{action} {code}</b>",
             f"Рыночный курс: {fmt_stars(market)} за 1 {code}", ""]
    rows = []
    if not ads:
        lines.append("Подходящих объявлений пока нет. Разместите своё!")
    for ad in ads:
        name = f"@{ad['username']}" if ad["username"] else (ad["first_name"] or "user")
        rows.append([(f"{fmt_stars(Decimal(ad['price']))} · {name} · до {fmt(ad['amount_left'], code, False)}",
                      f"ad:{ad['id']}")])
    rows.append([("➕ Своё объявление", "new"), ("🔄", cb.data)])
    await show(cb, "\n".join(lines), kb(*rows, back(f"p2p:{side}")))


# --- объявление и создание сделки ---

def ad_text(ad: dict) -> str:
    code = ad["currency"]
    seller = ad["side"] == "sell"
    owner = f"@{esc(ad['username'])}" if ad.get("username") else esc(ad.get("first_name") or "user")
    lines = [
        f"{'🔴 Продаёт' if seller else '🟢 Покупает'} {CURRENCIES[code].emoji} <b>{code}</b> — #{ad['id']}",
        "",
        f"👤 {owner} · сделок: {ad.get('deals', 0)}",
        f"💰 Цена: <b>{fmt_stars(Decimal(ad['price']))}</b> за 1 {code}",
        f"📦 {'Доступно' if seller else 'Купит до'}: {fmt(ad['amount_left'], code)}",
        f"↔️ Лимиты: {fmt(ad['min_units'], code, False)} – {fmt(min(ad['max_units'], ad['amount_left']), code)}",
        f"💳 Оплата: {esc(ad['method'])}",
    ]
    if ad["terms"]:
        lines.append(f"📝 Условия: {esc(ad['terms'])}")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("ad:"))
async def ad_view(cb: CallbackQuery, db: Database, state: FSMContext):
    await state.clear()
    ad = await db.get_ad(int(cb.data.split(":")[1]))
    if not ad or not ad["active"]:
        return await cb.answer("Объявление уже неактивно", show_alert=True)
    side = "b" if ad["side"] == "sell" else "s"
    rows = []
    if ad["user_id"] != cb.from_user.id:
        rows.append([("🟢 Купить" if ad["side"] == "sell" else "🔴 Продать", f"adtake:{ad['id']}")])
    else:
        rows.append([("❌ Закрыть объявление", f"adclose:{ad['id']}")])
    await show(cb, ad_text(ad), kb(*rows, back(f"p2p:{side}:{ad['currency']}")))


@router.callback_query(F.data.startswith("adtake:"))
async def ad_take(cb: CallbackQuery, db: Database, state: FSMContext, rates: Rates):
    ad = await db.get_ad(int(cb.data.split(":")[1]))
    if not ad or not ad["active"]:
        return await cb.answer("Объявление уже неактивно", show_alert=True)
    code = ad["currency"]
    hint = ""
    if ad["side"] == "buy":
        hint = f"\nВаш баланс: {money((await db.balances(cb.from_user.id))[code]['available'], code, rates)}"
    await state.set_state(Take.amount)
    await state.update_data(ad_id=ad["id"])
    await show(cb, (
        f"{ad_text(ad)}\n\nВведите количество {code} "
        f"({fmt(ad['min_units'], code, False)} – {fmt(min(ad['max_units'], ad['amount_left']), code, False)}):{hint}"
    ), kb(back(f"ad:{ad['id']}", "Отмена")))


@router.message(Take.amount, F.text)
async def take_amount(message: Message, state: FSMContext, db: Database):
    ad = await db.get_ad((await state.get_data())["ad_id"])
    if not ad or not ad["active"]:
        await state.clear()
        return await message.answer("Объявление уже неактивно.")
    code = ad["currency"]
    units = parse_amount(message.text, code)
    if not units or not ad["min_units"] <= units <= min(ad["max_units"], ad["amount_left"]):
        return await message.answer("Сумма вне лимитов объявления. Попробуйте ещё раз.")
    stars = (Decimal(units) / CURRENCIES[code].unit * Decimal(ad["price"])).to_integral_value(ROUND_CEILING)
    await state.update_data(units=units)
    verb = "покупаете" if ad["side"] == "sell" else "продаёте"
    await message.answer(
        f"Вы {verb} <b>{fmt(units, code)}</b> за <b>{fmt_stars(Decimal(stars))}</b>\n"
        f"Цена: {fmt_stars(Decimal(ad['price']))} за 1 {code}\n\n"
        + ("После создания сделки переведите звёзды продавцу и нажмите «Я оплатил»."
           if ad["side"] == "sell" else
           "Ваши монеты будут заблокированы до тех пор, пока вы не подтвердите получение звёзд."),
        reply_markup=kb([("✅ Создать сделку", "take_ok")], back(f"ad:{ad['id']}", "❌ Отмена")),
    )


@router.callback_query(F.data == "take_ok", Take.amount)
async def take_ok(cb: CallbackQuery, state: FSMContext, db: Database, bot: Bot, rates: Rates):
    data = await state.get_data()
    if not data.get("units"):
        return await cb.answer()
    await state.clear()
    try:
        order_id = await db.create_order(data["ad_id"], cb.from_user.id, data["units"], config.P2P_PAY_MINUTES)
    except LedgerError as e:
        return await cb.answer(str(e), show_alert=True)
    order = await db.get_order(order_id)
    ad = await db.get_ad(order["ad_id"])
    other = ad["user_id"]
    await notify(bot, other, f"🤝 <b>Новая сделка #{order_id}</b> по вашему объявлению #{ad['id']}\n\n"
                             + await order_text(db, order, other), await order_kb(order, other))
    await show_order(cb, db, order)


# --- экран сделки ---

async def order_text(db: Database, order: dict, viewer: int) -> str:
    code = order["currency"]
    buyer, seller = await db.get_user(order["buyer_id"]), await db.get_user(order["seller_id"])
    ad = await db.get_ad(order["ad_id"])
    role = "покупатель" if viewer == order["buyer_id"] else "продавец" if viewer == order["seller_id"] else "модератор"
    lines = [
        f"📦 <b>Сделка #{order['id']}</b> · {fmt(order['amount'], code)}",
        "",
        f"💰 К оплате: <b>{fmt_stars(Decimal(order['total_stars']))}</b>"
        f" ({fmt_stars(Decimal(order['price']))} за 1 {code})",
        f"🟢 Покупатель: {user_link(buyer)}",
        f"🔴 Продавец: {user_link(seller)}",
        f"💳 Оплата: {esc(ad['method']) if ad else '—'}",
        f"📍 Статус: <b>{STATUS[order['status']]}</b>",
        f"👁 Вы — {role}",
    ]
    if ad and ad["terms"]:
        lines.append(f"📝 Условия: {esc(ad['terms'])}")
    if order["status"] == "wait_payment":
        lines.append("")
        if viewer == order["buyer_id"]:
            lines.append(f"Переведите <b>{fmt_stars(Decimal(order['total_stars']))}</b> продавцу {user_link(seller)} "
                         f"и нажмите «Я оплатил» до {clock(order['pay_deadline'])}. "
                         "Монеты продавца уже заблокированы ботом.")
        else:
            lines.append(f"Монеты заблокированы. Ждём оплату от покупателя до {clock(order['pay_deadline'])}.")
    elif order["status"] == "paid" and viewer == order["seller_id"]:
        lines += ["", "Проверьте, что звёзды пришли, и подтвердите — монеты уйдут покупателю. "
                      "<b>Не подтверждайте, пока не получили оплату!</b>"]
    elif order["status"] == "paid" and viewer == order["buyer_id"]:
        lines += ["", "Ждём, пока продавец подтвердит получение. Если он не отвечает — откройте спор."]
    elif order["status"] == "dispute":
        lines += ["", f"Причина: {esc(order['dispute_reason'])}",
                  "Модератор изучит сделку. Отправьте доказательства через «💬 Чат по сделке»."]
    return "\n".join(lines)


async def order_kb(order: dict, viewer: int, admin: bool = False):
    oid, status = order["id"], order["status"]
    rows = []
    buyer, seller = viewer == order["buyer_id"], viewer == order["seller_id"]
    if status == "wait_payment" and buyer:
        rows += [[("✅ Я оплатил", f"paid:{oid}")], [("❌ Отменить сделку", f"ocancel:{oid}")]]
    if status == "paid" and seller:
        rows.append([("✅ Подтвердить получение", f"release:{oid}")])
    if status == "paid" and (seller or buyer):
        rows.append([("⚠️ Открыть спор", f"disp:{oid}")])
    if status in ("wait_payment", "paid", "dispute") and (buyer or seller):
        rows.append([("💬 Чат по сделке", f"chat:{oid}")])
    if admin and status == "dispute":
        rows.append([("✅ Отдать покупателю", f"adm_buyer:{oid}"), ("↩️ Вернуть продавцу", f"adm_seller:{oid}")])
    rows.append([("🔄 Обновить", f"ord:{oid}"), ("📦 Мои сделки", "myord")])
    return kb(*rows)


async def show_order(target, db: Database, order: dict) -> None:
    viewer = target.from_user.id
    admin = viewer in config.ADMIN_IDS
    await show(target, await order_text(db, order, viewer), await order_kb(order, viewer, admin))


@router.callback_query(F.data.startswith("ord:"))
async def order_view(cb: CallbackQuery, db: Database, state: FSMContext):
    await state.clear()
    order = await db.get_order(int(cb.data.split(":")[1]))
    if not order or (cb.from_user.id not in (order["buyer_id"], order["seller_id"])
                     and cb.from_user.id not in config.ADMIN_IDS):
        return await cb.answer("Сделка не найдена", show_alert=True)
    await show_order(cb, db, order)


async def _notify_parties(bot: Bot, db: Database, order: dict, text: str, skip: int | None = None) -> None:
    for uid in (order["buyer_id"], order["seller_id"]):
        if uid != skip:
            await notify(bot, uid, f"{text}\n\n" + await order_text(db, order, uid), await order_kb(order, uid))


@router.callback_query(F.data.startswith("paid:"))
async def order_paid(cb: CallbackQuery, db: Database, bot: Bot):
    try:
        order = await db.mark_paid(int(cb.data.split(":")[1]), cb.from_user.id)
    except LedgerError as e:
        return await cb.answer(str(e), show_alert=True)
    order = await db.get_order(order["id"])
    await _notify_parties(bot, db, order, f"💸 Покупатель отметил оплату по сделке #{order['id']}",
                          skip=cb.from_user.id)
    await show_order(cb, db, order)


@router.callback_query(F.data.startswith("ocancel:"))
async def order_cancel(cb: CallbackQuery, db: Database, bot: Bot):
    try:
        order = await db.cancel_order(int(cb.data.split(":")[1]), by_buyer=cb.from_user.id)
    except LedgerError as e:
        return await cb.answer(str(e), show_alert=True)
    order = await db.get_order(order["id"])
    await _notify_parties(bot, db, order, f"❌ Покупатель отменил сделку #{order['id']}", skip=cb.from_user.id)
    await show_order(cb, db, order)


@router.callback_query(F.data.startswith("release:"))
async def order_release_ask(cb: CallbackQuery, db: Database):
    order = await db.get_order(int(cb.data.split(":")[1]))
    if not order or order["seller_id"] != cb.from_user.id or order["status"] != "paid":
        return await cb.answer("Действие недоступно", show_alert=True)
    await show(cb, (
        f"Вы подтверждаете, что получили <b>{fmt_stars(Decimal(order['total_stars']))}</b> по сделке #{order['id']}?\n\n"
        f"{fmt(order['amount'], order['currency'])} сразу уйдут покупателю. <b>Отменить будет нельзя.</b>"
    ), kb([("✅ Да, звёзды получены", f"release!:{order['id']}")], back(f"ord:{order['id']}", "‹ Нет, назад")))


@router.callback_query(F.data.startswith("release!:"))
async def order_release(cb: CallbackQuery, db: Database, bot: Bot):
    try:
        order = await db.complete_order(int(cb.data.split(":")[1]), config.P2P_FEE_PERCENT, by_seller=cb.from_user.id)
    except LedgerError as e:
        return await cb.answer(str(e), show_alert=True)
    order = await db.get_order(order["id"])
    await _notify_parties(bot, db, order, f"✅ Сделка #{order['id']} завершена, монеты зачислены", skip=cb.from_user.id)
    await show_order(cb, db, order)


# --- спор ---

@router.callback_query(F.data.startswith("disp:"))
async def dispute_ask(cb: CallbackQuery, db: Database, state: FSMContext):
    order = await db.get_order(int(cb.data.split(":")[1]))
    if not order or order["status"] != "paid" or cb.from_user.id not in (order["buyer_id"], order["seller_id"]):
        return await cb.answer("Спор сейчас открыть нельзя", show_alert=True)
    wait = order["paid_at"] + config.P2P_DISPUTE_AFTER_MINUTES * 60 - time.time()
    if cb.from_user.id == order["buyer_id"] and wait > 0:
        return await cb.answer(f"Дайте продавцу время проверить оплату. Спор можно открыть через "
                               f"{int(wait // 60) + 1} мин.", show_alert=True)
    await state.set_state(Dispute.reason)
    await state.update_data(order_id=order["id"])
    await show(cb, "⚠️ <b>Открытие спора</b>\n\nОпишите, что произошло, одним сообщением:",
               kb(back(f"ord:{order['id']}", "Отмена")))


@router.message(Dispute.reason, F.text)
async def dispute_open(message: Message, state: FSMContext, db: Database, bot: Bot):
    order_id = (await state.get_data())["order_id"]
    await state.clear()
    try:
        await db.open_dispute(order_id, message.from_user.id, message.text)
    except LedgerError as e:
        return await message.answer(str(e))
    order = await db.get_order(order_id)
    who = user_link(await db.get_user(message.from_user.id))
    await _notify_parties(bot, db, order, f"⚠️ {who} открыл спор по сделке #{order_id}", skip=message.from_user.id)
    for admin_id in config.ADMIN_IDS:
        await notify(bot, admin_id, f"🚨 <b>Новый спор</b>\n\n" + await order_text(db, order, admin_id),
                     await order_kb(order, admin_id, admin=True))
    await show_order(message, db, order)


# --- чат по сделке ---

@router.callback_query(F.data.startswith("chat:"))
async def chat_start(cb: CallbackQuery, db: Database, state: FSMContext):
    order = await db.get_order(int(cb.data.split(":")[1]))
    if not order or cb.from_user.id not in (order["buyer_id"], order["seller_id"]):
        return await cb.answer("Сделка не найдена", show_alert=True)
    await state.set_state(Chat.active)
    await state.update_data(order_id=order["id"])
    await show(cb, (
        f"💬 <b>Чат по сделке #{order['id']}</b>\n\n"
        "Отправляйте сообщения, фото или файлы — бот перешлёт их второй стороне"
        + (" и модератору." if order["status"] == "dispute" else ".")
    ), kb([("✅ Закончить", f"ord:{order['id']}")]))


@router.message(Chat.active)
async def chat_message(message: Message, state: FSMContext, db: Database, bot: Bot):
    order = await db.get_order((await state.get_data())["order_id"])
    if not order:
        return await state.clear()
    me = message.from_user.id
    other = order["seller_id"] if me == order["buyer_id"] else order["buyer_id"]
    role = "покупателя" if me == order["buyer_id"] else "продавца"
    recipients = [other] + (list(config.ADMIN_IDS) if order["status"] == "dispute" else [])
    for uid in recipients:
        try:
            await bot.send_message(uid, f"💬 Сделка #{order['id']}, сообщение от {role}:",
                                   reply_markup=kb([("↩️ Ответить", f"chat:{order['id']}")]) if uid == other else None)
            await bot.copy_message(uid, message.chat.id, message.message_id)
        except Exception as e:
            log.info("Чат сделки %s → %s: %s", order["id"], uid, e)
    await message.answer("✓ Отправлено", reply_markup=kb([("✅ Закончить", f"ord:{order['id']}")]))


# --- мои сделки и объявления ---

@router.callback_query(F.data == "myord")
async def my_orders(cb: CallbackQuery, db: Database, state: FSMContext):
    await state.clear()
    orders = await db.my_orders(cb.from_user.id)
    if not orders:
        return await show(cb, "📦 Сделок пока нет.", kb(back("p2p")))
    rows = []
    for o in orders:
        side = "🟢" if o["buyer_id"] == cb.from_user.id else "🔴"
        rows.append([(f"{side} #{o['id']} · {fmt(o['amount'], o['currency'])} · {STATUS[o['status']]}", f"ord:{o['id']}")])
    await show(cb, "📦 <b>Мои сделки</b>\n🟢 — покупка, 🔴 — продажа", kb(*rows, back("p2p")))


@router.callback_query(F.data == "myads")
async def my_ads(cb: CallbackQuery, db: Database):
    ads = await db.my_ads(cb.from_user.id)
    if not ads:
        return await show(cb, "📋 Активных объявлений нет.", kb([("➕ Разместить", "new")], back("p2p")))
    rows = [[(f"{'🔴' if a['side'] == 'sell' else '🟢'} #{a['id']} · {a['currency']} · "
              f"{fmt_stars(Decimal(a['price']))} · {fmt(a['amount_left'], a['currency'], False)}", f"ad:{a['id']}")]
            for a in ads]
    await show(cb, "📋 <b>Мои объявления</b>\n🔴 — продаю, 🟢 — покупаю", kb(*rows, back("p2p")))


@router.callback_query(F.data.startswith("adclose:"))
async def ad_close(cb: CallbackQuery, db: Database):
    try:
        await db.close_ad(int(cb.data.split(":")[1]), cb.from_user.id)
        await cb.answer("Объявление закрыто, монеты разблокированы")
    except LedgerError as e:
        await cb.answer(str(e), show_alert=True)
    await my_ads(cb, db)


# --- новое объявление ---

@router.callback_query(F.data == "new")
async def new_ad(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "➕ <b>Новое объявление</b>\n\nЧто хотите сделать?",
               kb([("🔴 Продать монеты", "new:sell"), ("🟢 Купить монеты", "new:buy")], back("p2p")))


@router.callback_query(F.data.in_({"new:sell", "new:buy"}))
async def new_ad_side(cb: CallbackQuery):
    side = cb.data.split(":")[1]
    await show(cb, "Выберите валюту:", kb(*currency_rows(f"newc:{side}"), back("new")))


@router.callback_query(F.data.startswith("newc:"))
async def new_ad_currency(cb: CallbackQuery, state: FSMContext, rates: Rates):
    _, side, code = cb.data.split(":")
    await rates.refresh()
    market = rates.price_stars(code)
    await state.set_state(NewAd.price)
    await state.update_data(side=side, code=code)
    await show(cb, (
        f"Цена за 1 {code} в звёздах?\n\nРыночный курс: <b>{fmt_stars(market)}</b>"
    ), kb([(f"По рынку: {fmt_stars(market)}", "newp:market")], back("new", "Отмена")))


async def _ask_amount(target, state: FSMContext, db: Database, rates: Rates) -> None:
    data = await state.get_data()
    code = data["code"]
    await state.set_state(NewAd.amount)
    if data["side"] == "sell":
        available = (await db.balances(target.from_user.id))[code]["available"]
        text = (f"Сколько {code} продаёте? Доступно: {money(available, code, rates)}\n"
                "<i>Эта сумма будет заблокирована, пока объявление активно.</i>")
    else:
        text = f"Сколько {code} хотите купить всего?"
    await show(target, text, kb(back("new", "Отмена")))


@router.callback_query(F.data == "newp:market", NewAd.price)
async def new_ad_market(cb: CallbackQuery, state: FSMContext, db: Database, rates: Rates):
    code = (await state.get_data())["code"]
    await state.update_data(price=str(rates.price_stars(code).quantize(Decimal("0.01"))))
    await _ask_amount(cb, state, db, rates)


@router.message(NewAd.price, F.text)
async def new_ad_price(message: Message, state: FSMContext, db: Database, rates: Rates):
    price = parse_price(message.text)
    if not price:
        return await message.answer("Введите цену числом, например 66.5")
    await state.update_data(price=str(price))
    await _ask_amount(message, state, db, rates)


@router.message(NewAd.amount, F.text)
async def new_ad_amount(message: Message, state: FSMContext):
    code = (await state.get_data())["code"]
    units = parse_amount(message.text, code)
    if not units:
        return await message.answer("Не понял сумму. Пример: 100 или 0.5")
    await state.update_data(amount=units)
    await state.set_state(NewAd.limits)
    await message.answer(
        f"Лимиты одной сделки в {code}: напишите «мин-макс», например <code>1-50</code>",
        reply_markup=kb([("Без ограничений", "newl:any")], back("new", "Отмена")))


async def _ask_method(target, state: FSMContext) -> None:
    await state.set_state(NewAd.method)
    await show(target, "Как покупатель будет платить? Выберите или напишите свой вариант:",
               kb([(v, f"newm:{k}") for k, v in METHODS.items()], back("new", "Отмена")))


@router.callback_query(F.data == "newl:any", NewAd.limits)
async def new_ad_limits_any(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(min_units=1, max_units=data["amount"])
    await _ask_method(cb, state)


@router.message(NewAd.limits, F.text)
async def new_ad_limits(message: Message, state: FSMContext):
    data = await state.get_data()
    parts = message.text.replace(" ", "").split("-")
    if len(parts) != 2:
        return await message.answer("Формат: мин-макс, например 1-50")
    lo, hi = parse_amount(parts[0], data["code"]), parse_amount(parts[1], data["code"])
    if not lo or not hi or lo > hi:
        return await message.answer("Проверьте лимиты: минимум должен быть не больше максимума")
    await state.update_data(min_units=lo, max_units=hi)
    await _ask_method(message, state)


async def _ask_terms(target, state: FSMContext, method: str) -> None:
    await state.update_data(method=method[:100])
    await state.set_state(NewAd.terms)
    await show(target, "Условия сделки (необязательно): например, сроки или требования к покупателю.",
               kb([("Пропустить", "newt:skip")], back("new", "Отмена")))


@router.callback_query(F.data.startswith("newm:"), NewAd.method)
async def new_ad_method_cb(cb: CallbackQuery, state: FSMContext):
    await _ask_terms(cb, state, METHODS[cb.data.split(":")[1]])


@router.message(NewAd.method, F.text)
async def new_ad_method(message: Message, state: FSMContext):
    await _ask_terms(message, state, message.text.strip())


async def _new_ad_confirm(target, state: FSMContext, terms: str) -> None:
    await state.update_data(terms=terms[:300])
    d = await state.get_data()
    code = d["code"]
    await show(target, (
        f"Проверьте объявление:\n\n"
        f"{'🔴 Продаю' if d['side'] == 'sell' else '🟢 Покупаю'} {fmt(d['amount'], code)}\n"
        f"Цена: {fmt_stars(Decimal(d['price']))} за 1 {code}\n"
        f"Лимиты: {fmt(d['min_units'], code, False)} – {fmt(d['max_units'], code)}\n"
        f"Оплата: {esc(d['method'])}\n" + (f"Условия: {esc(terms)}\n" if terms else "")
    ), kb([("✅ Опубликовать", "new_ok")], back("new", "❌ Отмена")))


@router.callback_query(F.data == "newt:skip", NewAd.terms)
async def new_ad_terms_skip(cb: CallbackQuery, state: FSMContext):
    await _new_ad_confirm(cb, state, "")


@router.message(NewAd.terms, F.text)
async def new_ad_terms(message: Message, state: FSMContext):
    await _new_ad_confirm(message, state, message.text.strip())


@router.callback_query(F.data == "new_ok", NewAd.terms)
async def new_ad_publish(cb: CallbackQuery, state: FSMContext, db: Database):
    d = await state.get_data()
    try:
        ad_id = await db.create_ad(cb.from_user.id, d["side"], d["code"], Decimal(d["price"]), d["amount"],
                                   d["min_units"], min(d["max_units"], d["amount"]), d["method"], d["terms"])
    except LedgerError as e:
        return await cb.answer(str(e), show_alert=True)
    await state.clear()
    await show(cb, f"✅ Объявление #{ad_id} опубликовано!", kb([("📋 Мои объявления", "myads")], back("p2p", "‹ P2P")))


# --- модерация споров ---

@router.callback_query(F.data.regexp(r"^adm_(buyer|seller):\d+$"))
async def admin_resolve(cb: CallbackQuery, db: Database, bot: Bot):
    if cb.from_user.id not in config.ADMIN_IDS:
        return await cb.answer("Только для модераторов", show_alert=True)
    action, oid = cb.data.split(":")
    try:
        if action == "adm_buyer":
            await db.complete_order(int(oid), config.P2P_FEE_PERCENT, from_status=("dispute",))
            result = "✅ Спор решён в пользу покупателя — монеты зачислены ему"
        else:
            await db.cancel_order(int(oid), from_status=("dispute",))
            result = "↩️ Спор решён в пользу продавца — монеты возвращены"
    except LedgerError as e:
        return await cb.answer(str(e), show_alert=True)
    order = await db.get_order(int(oid))
    await _notify_parties(bot, db, order, f"⚖️ Решение по сделке #{oid}: {result}")
    await show(cb, f"{result}\n\n" + await order_text(db, order, cb.from_user.id), kb(back("main")))


# --- фоновая отмена просроченных сделок ---

async def expire_loop(bot: Bot, db: Database) -> None:
    while True:
        try:
            for order in await db.expired_orders():
                try:
                    order = await db.cancel_order(order["id"])
                except LedgerError:
                    continue
                order = await db.get_order(order["id"])
                await _notify_parties(bot, db, order, f"⌛ Сделка #{order['id']} отменена: покупатель не оплатил вовремя")
        except Exception:
            log.exception("Проверка просроченных сделок")
        await asyncio.sleep(30)
