from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .. import config
from ..currencies import CODES, fmt, parse_amount
from ..db import Database, LedgerError
from ..rates import Rates
from ..ui import esc, kb, money, notify, user_link

router = Router()
router.message.filter(lambda message: message.from_user.id in config.ADMIN_IDS)

HELP = (
    "🛠 <b>Админ-панель</b>\n\n"
    "/mint <code>&lt;кому&gt; &lt;валюта&gt; &lt;сумма&gt;</code> — начислить монеты\n"
    "/burn <code>&lt;кому&gt; &lt;валюта&gt; &lt;сумма&gt;</code> — списать\n"
    "/user <code>&lt;кому&gt;</code> — балансы и адреса\n"
    "/ban, /unban <code>&lt;кому&gt;</code>\n"
    "/disputes — открытые споры P2P\n\n"
    "<i>Кому — id, @username или адрес кошелька. Валюта — USDT, GRAM или BTC.</i>"
)


@router.message(Command("admin"))
async def admin(message: Message, db: Database):
    s = await db.stats()
    supply = "\n".join(f"  {code}: {fmt(s['supply'].get(code) or 0, code)}" for code in CODES)
    await message.answer(f"{HELP}\n\n👥 Пользователей: {s['users']}\n🤝 Сделок P2P: {s['deals']}\n"
                         f"💰 В обороте:\n{supply}")


async def _target(message: Message, db: Database, query: str) -> dict | None:
    user = await db.find_user(query)
    if not user:
        await message.answer("Пользователь не найден (он должен хотя бы раз запустить бота).")
    return user


@router.message(Command("mint", "burn"))
async def mint(message: Message, command: CommandObject, db: Database, rates: Rates, bot: Bot):
    args = (command.args or "").split()
    if len(args) != 3 or args[1].upper() not in CODES:
        return await message.answer(f"Использование: /{command.command} &lt;кому&gt; &lt;USDT|GRAM|BTC&gt; &lt;сумма&gt;")
    code = args[1].upper()
    units = parse_amount(args[2], code)
    if not units:
        return await message.answer("Некорректная сумма")
    user = await _target(message, db, args[0])
    if not user:
        return
    sign = 1 if command.command == "mint" else -1
    try:
        await db.mint(user["id"], code, sign * units, message.from_user.id)
    except LedgerError as e:
        return await message.answer(str(e))
    await message.answer(f"✅ {'Начислено' if sign > 0 else 'Списано'} {fmt(units, code)} — {user_link(user)}")
    if sign > 0:
        await notify(bot, user["id"], f"📥 Зачислено {money(units, code, rates)}")


@router.message(Command("user"))
async def user_info(message: Message, command: CommandObject, db: Database, rates: Rates):
    user = await _target(message, db, command.args or "")
    if not user:
        return
    balances, addresses = await db.balances(user["id"]), await db.addresses(user["id"])
    lines = [f"👤 {user_link(user)} · id <code>{user['id']}</code>",
             f"Сделок: {user['deals']}" + (" · 🚫 заблокирован" if user["banned"] else ""), ""]
    for code in CODES:
        b = balances[code]
        lines.append(f"{code}: {money(b['available'], code, rates)}"
                     + (f" (🔒 {fmt(b['locked'], code)})" if b["locked"] else "")
                     + f"\n<code>{esc(addresses[code])}</code>")
    await message.answer("\n".join(lines))


@router.message(Command("ban", "unban"))
async def ban(message: Message, command: CommandObject, db: Database):
    user = await _target(message, db, command.args or "")
    if not user:
        return
    banned = command.command == "ban"
    await db.set_banned(user["id"], banned)
    await message.answer(f"{'🚫 Заблокирован' if banned else '✅ Разблокирован'}: {user_link(user)}")


@router.message(Command("disputes"))
async def disputes(message: Message, db: Database):
    items = await db.disputes()
    if not items:
        return await message.answer("Открытых споров нет 👍")
    rows = [[(f"⚠️ #{o['id']} · {fmt(o['amount'], o['currency'])}", f"ord:{o['id']}")] for o in items]
    await message.answer("🚨 <b>Открытые споры</b>", reply_markup=kb(*rows))
