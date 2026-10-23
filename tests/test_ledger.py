from decimal import Decimal

import pytest

from rocket.currencies import (
    CURRENCIES, address_currency, fmt, fmt_stars, generate_address, parse_amount,
)
from rocket.db import Database, LedgerError


def test_addresses():
    for code, cur in CURRENCIES.items():
        for _ in range(50):
            addr = generate_address(code)
            assert len(addr) == cur.length and addr.startswith(cur.prefix)
            assert address_currency(addr) == code
        other = next(ch for ch in cur.alphabet if ch.lower() != addr[-1].lower())
        broken = addr[:-1] + other
        assert address_currency(broken) is None  # опечатка ловится контрольной суммой
    assert len(generate_address("USDT")) == 34 and len(generate_address("GRAM")) == 48
    assert len(generate_address("BTC")) == 42
    btc = generate_address("BTC")
    assert address_currency(btc.upper()) == "BTC"  # bech32 не зависит от регистра
    assert address_currency("hello") is None


def test_amounts():
    assert parse_amount("1.5", "USDT") == 1_500_000
    assert parse_amount("0,00000001", "BTC") == 1
    assert parse_amount("0.000000001", "BTC") is None  # слишком много знаков
    assert parse_amount("-1", "USDT") is None and parse_amount("abc", "USDT") is None
    assert parse_amount("0", "GRAM") is None and parse_amount("nan", "GRAM") is None
    assert fmt(1_500_000, "USDT") == "1.5 USDT" and fmt(123456789012, "BTC") == "1 234.56789012 BTC"
    assert fmt_stars(Decimal("6666.6")) == "6 667 ⭐" and fmt_stars(Decimal("0.5")) == "0.5 ⭐"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "r.db"))
    await database.connect()
    for uid in (1, 2, 3):
        await database.ensure_user(uid, f"user{uid}", f"U{uid}")
    yield database
    await database.close()


async def bal(db, uid, code="USDT"):
    return (await db.balances(uid))[code]


async def test_users_and_transfer(db):
    assert await db.ensure_user(1, "user1", "U1") is False  # адреса не меняются
    a1 = await db.addresses(1)
    assert set(a1) == {"USDT", "GRAM", "BTC"} and await db.address_owner(a1["USDT"]) == (1, "USDT")
    assert (await db.find_user("@USER2"))["id"] == 2 and (await db.find_user(a1["GRAM"]))["id"] == 1

    await db.mint(1, "USDT", 10_000_000, admin_id=99)
    with pytest.raises(LedgerError):
        await db.transfer(1, 2, "USDT", 20_000_000, "x")
    await db.transfer(1, 2, "USDT", 4_000_000, a1["USDT"])
    assert (await bal(db, 1))["available"] == 6_000_000 and (await bal(db, 2))["available"] == 4_000_000
    with pytest.raises(LedgerError):
        await db.transfer(1, 1, "USDT", 1, "x")
    assert [h["kind"] for h in await db.history(1)] == ["send", "mint"]


async def test_swap(db):
    await db.mint(1, "USDT", 5_000_000, 99)
    await db.swap(1, "USDT", "BTC", 5_000_000, 5000)
    b = await db.balances(1)
    assert b["USDT"]["available"] == 0 and b["BTC"]["available"] == 5000
    with pytest.raises(LedgerError):
        await db.swap(1, "USDT", "BTC", 1, 1)


async def test_cheques(db):
    await db.mint(1, "GRAM", 3_000_000_000, 99)
    code = await db.create_cheque(1, "GRAM", 1_000_000_000, 2)
    assert (await bal(db, 1, "GRAM"))["available"] == 1_000_000_000
    with pytest.raises(LedgerError):
        await db.activate_cheque(code, 1)
    await db.activate_cheque(code, 2)
    with pytest.raises(LedgerError):
        await db.activate_cheque(code, 2)
    cheque = await db.get_cheque(code)
    refund = await db.cancel_cheque(cheque["id"], 1)
    assert refund == 1_000_000_000
    assert (await bal(db, 1, "GRAM"))["available"] == 2_000_000_000
    assert (await bal(db, 2, "GRAM"))["available"] == 1_000_000_000
    with pytest.raises(LedgerError):
        await db.activate_cheque(code, 3)


async def test_p2p_sell_ad_flow(db):
    await db.mint(1, "USDT", 100_000_000, 99)  # 100 USDT
    ad = await db.create_ad(1, "sell", "USDT", Decimal("66.7"), 50_000_000, 1_000_000, 50_000_000, "Звёзды", "")
    assert await bal(db, 1) == {"available": 50_000_000, "locked": 50_000_000}

    order_id = await db.create_order(ad, 2, 10_000_000, 15)
    order = await db.get_order(order_id)
    assert order["buyer_id"] == 2 and order["seller_id"] == 1 and order["total_stars"] == 667
    assert (await db.get_ad(ad))["amount_left"] == 40_000_000

    with pytest.raises(LedgerError):
        await db.complete_order(order_id, Decimal(0), by_seller=1)  # ещё не оплачено
    await db.mark_paid(order_id, 2)
    with pytest.raises(LedgerError):
        await db.complete_order(order_id, Decimal(0), by_seller=2)  # не продавец
    await db.complete_order(order_id, Decimal(0), by_seller=1)
    assert await bal(db, 1) == {"available": 50_000_000, "locked": 40_000_000}
    assert (await bal(db, 2))["available"] == 10_000_000
    assert (await db.get_user(1))["deals"] == 1

    # отменённая сделка возвращает монеты в объявление
    o2 = await db.create_order(ad, 3, 5_000_000, 15)
    await db.cancel_order(o2, by_buyer=3)
    assert (await db.get_ad(ad))["amount_left"] == 40_000_000

    # закрытие объявления возвращает остаток продавцу
    await db.close_ad(ad, 1)
    assert await bal(db, 1) == {"available": 90_000_000, "locked": 0}


async def test_p2p_buy_ad_and_disputes(db):
    await db.mint(2, "BTC", 100_000, 99)
    ad = await db.create_ad(1, "buy", "BTC", Decimal("7000000"), 100_000, 10_000, 100_000, "Звёзды", "")
    order_id = await db.create_order(ad, 2, 50_000, 15)  # 2 продаёт свои BTC автору
    assert await bal(db, 2, "BTC") == {"available": 50_000, "locked": 50_000}
    await db.mark_paid(order_id, 1)
    with pytest.raises(LedgerError):
        await db.open_dispute(order_id, 3, "чужой")
    await db.open_dispute(order_id, 1, "продавец не подтверждает")
    # админ решил в пользу продавца — монеты возвращаются ему
    await db.cancel_order(order_id, from_status=("dispute",))
    assert await bal(db, 2, "BTC") == {"available": 100_000, "locked": 0}

    o2 = await db.create_order(ad, 2, 20_000, 15)
    await db.mark_paid(o2, 1)
    await db.open_dispute(o2, 2, "x")
    await db.complete_order(o2, Decimal(0), from_status=("dispute",))  # в пользу покупателя
    assert (await bal(db, 1, "BTC"))["available"] == 20_000
    assert await bal(db, 2, "BTC") == {"available": 80_000, "locked": 0}

    with pytest.raises(LedgerError):
        await db.create_order(ad, 1, 10_000, 15)  # своё объявление
    with pytest.raises(LedgerError):
        await db.create_order(ad, 3, 10_000, 15)  # у 3 нет BTC
