import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123:TEST")

import pytest
from telethon.errors import RPCError
from telethon.tl import types
from telethon.tl.types import payments

from upgrade_gift import config
from upgrade_gift.db import DEFAULT_SETTINGS, Database, sanitize_settings
from upgrade_gift.gifts import parse_unique_gift, slug_base
from upgrade_gift.monitor import Monitor
from upgrade_gift.notifier import format_event, matches
from upgrade_gift.payments import XRocket
from upgrade_gift.webapp import create_app, validate_init_data


def make_init_data(user: dict, token: str = config.BOT_TOKEN, auth_date: int | None = None) -> str:
    pairs = {"auth_date": str(auth_date or int(time.time())), "query_id": "q1", "user": json.dumps(user)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def unique_gift(num: int, slug: str = "PlushPepe", **kw) -> payments.UniqueStarGift:
    doc = types.DocumentEmpty(id=1)
    gift = types.StarGiftUnique(
        id=1000 + num, gift_id=42, title="Plush Pepe", slug=f"{slug}-{num}", num=num,
        attributes=[
            types.StarGiftAttributeModel(name="Gold", document=doc, rarity=types.StarGiftAttributeRarity(permille=5)),
            types.StarGiftAttributeBackdrop(name="Onyx", backdrop_id=1, center_color=0, edge_color=0,
                                            pattern_color=0, text_color=0,
                                            rarity=types.StarGiftAttributeRarity(permille=20)),
            types.StarGiftAttributePattern(name="Stars", document=doc, rarity=types.StarGiftAttributeRarityRare()),
        ],
        availability_issued=kw.pop("issued", num), availability_total=5000,
        owner_id=kw.pop("owner_id", types.PeerUser(user_id=7)), **kw,
    )
    user = types.User(id=7, first_name="Ivan", username="ivan")
    return payments.UniqueStarGift(gift=gift, chats=[], users=[user])


def test_init_data():
    user = {"id": 7, "first_name": "Ivan"}
    assert validate_init_data(make_init_data(user), config.BOT_TOKEN)["id"] == 7
    assert validate_init_data(make_init_data(user, token="1:OTHER"), config.BOT_TOKEN) is None
    assert validate_init_data(make_init_data(user, auth_date=1), config.BOT_TOKEN) is None
    assert validate_init_data("garbage", config.BOT_TOKEN) is None


def test_sanitize_settings():
    s = sanitize_settings({"enabled": 0, "max_number": "100", "min_floor": -5,
                           "collections": [3, "2", "x", 3], "evil": 1})
    assert s["enabled"] is False and s["max_number"] == 100
    assert s["min_floor"] is None and s["collections"] == [2, 3] and "evil" not in s


def test_parse_and_format():
    assert slug_base("Durov's Cap") == "DurovsCap" and slug_base("B-Day Candle") == "BDayCandle"
    event = parse_unique_gift(unique_gift(15))
    assert event.slug == "PlushPepe-15" and event.owner.username == "ivan"
    assert event.transferable and event.model.rarity == 0.5
    event.floor_usd, event.floor_stars = 1234.5, 100000
    text = format_event(event, DEFAULT_SETTINGS)
    assert "https://t.me/nft/PlushPepe-15" in text and "@ivan" in text
    assert "$1 234.50" in text and "Доступен к передаче" in text

    onchain = parse_unique_gift(unique_gift(16, owner_id=None, owner_address="UQabc123456789"))
    assert not onchain.transferable and "TON" in format_event(onchain, DEFAULT_SETTINGS)


def test_filters():
    event = parse_unique_gift(unique_gift(15))
    event.floor_usd = 50
    s = dict(DEFAULT_SETTINGS)
    assert matches(event, s)
    assert not matches(event, {**s, "enabled": False})
    assert not matches(event, {**s, "collections": [1]})
    assert matches(event, {**s, "collections": [42]})
    assert not matches(event, {**s, "max_number": 10})
    assert not matches(event, {**s, "min_floor": 100})
    assert matches(event, {**s, "max_model_rarity": 1})
    assert not matches(event, {**s, "max_model_rarity": 0.1})
    assert matches(event, {**s, "models": {"42": ["Gold"]}, "backdrops": ["Onyx"]})
    assert not matches(event, {**s, "models": {"42": ["Silver"]}})
    assert matches(event, {**s, "models": {"7": ["Silver"]}})  # фильтр другой коллекции
    assert not matches(event, {**s, "patterns": {"42": ["Hearts"]}})
    assert not matches(event, {**s, "backdrops": ["Black"]})
    clean = sanitize_settings({"models": {"42": ["Gold", 5], "x": ["A"]}, "backdrops": "bad"})
    assert clean["models"] == {"42": ["Gold"]} and clean["backdrops"] == []


def test_webhook_signature():
    x = XRocket("key")
    body = b'{"type":"invoicePay"}'
    sig = hmac.new(hashlib.sha256(b"key").digest(), body, hashlib.sha256).hexdigest()
    assert x.verify_webhook(body, sig) and not x.verify_webhook(body, "00")


class FakeClient:
    def __init__(self, existing: set[int]):
        self.existing = existing

    async def __call__(self, request):
        num = int(request.slug.rsplit("-", 1)[1])
        if num not in self.existing:
            raise RPCError(request, "STARGIFT_SLUG_INVALID", 400)
        return unique_gift(num, issued=max(self.existing))


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.connect()
    yield database
    await database.close()


async def test_monitor_detects_new_upgrades(db):
    client = FakeClient({1, 2, 3})
    seen = []

    async def on_event(e):
        seen.append(e.num)

    mon = Monitor(client, db, on_event)
    mon.floor = lambda *a: _none()
    await db.upsert_collection(42, "Plush Pepe", "PlushPepe", 5000)
    await mon._init_collection((await db.collections())[0])
    col = (await db.collections())[0]
    assert col["active"] == 1 and col["last_num"] == 3 and seen == []

    client.existing |= {4, 5}
    await mon._scan_collection((await db.collections())[0])
    assert seen == [4, 5]
    assert (await db.collections())[0]["last_num"] == 5


async def test_monitor_attributes(db):
    doc = types.DocumentEmpty(id=1)

    class Client:
        async def __call__(self, request):
            return payments.ResaleStarGifts(count=0, gifts=[], chats=[], users=[], attributes=[
                types.StarGiftAttributeModel(name="B", document=doc, rarity=types.StarGiftAttributeRarity(permille=20)),
                types.StarGiftAttributeModel(name="A", document=doc, rarity=types.StarGiftAttributeRarity(permille=5)),
                types.StarGiftAttributePattern(name="P", document=doc, rarity=types.StarGiftAttributeRarityRare()),
            ])

    data = await Monitor(Client(), db, None).attributes(42)
    assert [m["name"] for m in data["models"]] == ["A", "B"] and data["patterns"][0]["label"] == "редкая"
    # Кэш в БД переживает перезапуск
    assert (await db.get_attributes(42))[1] == data


async def _none():
    return None, None


async def test_api(db, aiohttp_client):
    class FakeBilling:
        async def create(self, user_id, plan):
            await db.add_invoice(99, user_id, plan, 25, "USDT", "https://t.me/xRocket?start=inv_x")
            return {"id": 99, "link": "https://t.me/xRocket?start=inv_x", "status": "active"}

        async def sync(self, inv_id):
            await db.mark_invoice(inv_id, "paid")
            await db.extend_subscription(7, 30)
            return "paid"

    class FakeMonitor:
        async def attributes(self, gift_id):
            return {"models": [{"name": "Gold", "rarity": 0.5, "label": "0.5%"}], "backdrops": [], "patterns": []}

        async def backdrops(self):
            return [{"name": "Onyx", "rarity": 2, "label": "2%"}]

    client = await aiohttp_client(create_app(db, FakeBilling(), FakeMonitor()))
    headers = {"X-Telegram-Init-Data": make_init_data({"id": 7, "first_name": "Ivan"})}

    assert (await client.get("/api/me")).status == 401
    me = await (await client.get("/api/me", headers=headers)).json()
    assert me["user"]["active"] is False and me["user"]["settings"] is None and len(me["plans"]) == 3

    # Без подписки настройки недоступны
    assert (await client.put("/api/settings", headers=headers, json={})).status == 403
    assert (await client.get("/api/collections", headers=headers)).status == 403
    assert (await client.get("/api/collections/42/attributes", headers=headers)).status == 403

    r = await client.post("/api/invoices", headers=headers, json={"plan": "month"})
    assert (await r.json())["id"] == 99
    r = await (await client.get("/api/invoices/99", headers=headers)).json()
    assert r["status"] == "paid" and r["user"]["active"] is True and r["user"]["settings"]

    r = await client.put("/api/settings", headers=headers,
                         json={"collections": [42], "models": {"42": ["Gold"]}})
    assert (await r.json())["settings"]["models"] == {"42": ["Gold"]}
    r = await client.get("/api/collections/42/attributes", headers=headers)
    assert (await r.json())["models"][0]["name"] == "Gold"
    r = await client.get("/api/backdrops", headers=headers)
    assert (await r.json())["backdrops"][0]["name"] == "Onyx"

    assert (await client.get("/")).status == 200
    assert (await client.get("/static/app.js")).status == 200
