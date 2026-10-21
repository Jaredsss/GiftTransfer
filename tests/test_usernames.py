import os

os.environ.setdefault("BOT_TOKEN", "123:TEST")

import pytest
from telethon.errors import UsernamePurchaseAvailableError

from test_core import make_init_data
from upgrade_gift.db import Database
from upgrade_gift.usernames import UsernameHunter, candidates, is_valid, score, split_name
from upgrade_gift.webapp import create_app


def test_rules_and_score():
    assert is_valid("payai") and not is_valid("pay") and not is_valid("1payai") and not is_valid("pay_")
    assert split_name("payagent") == ("pay", "agent") and split_name("chatgame") == ("chat", "game")
    # короче и «денежнее» — дороже
    assert score("payai") > score("travelai") > score("xyzqwerty1ai")
    assert score("xgame") > score("supergame")
    names = candidates()
    assert names and all(is_valid(n) for n in names) and len(set(names)) == len(names)
    assert score(names[0]) >= score(names[-1])


class FakeClient:
    def __init__(self, answers):
        self.answers = answers

    async def __call__(self, request):
        answer = self.answers[request.username]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "u.db"))
    await database.connect()
    yield database
    await database.close()


async def test_hunter_statuses_and_notification(db):
    client = FakeClient({
        "payai": True,
        "chatai": False,
        "cashai": UsernamePurchaseAvailableError(None),
    })
    found = []

    async def on_found(item):
        found.append(item["name"])

    hunter = UsernameHunter(client, db, on_found, interval=0)

    async def no_page(name):
        return None
    hunter._page_exists = no_page

    for name in ("payai", "chatai", "cashai"):
        await hunter._process(name)
    assert (await db.get_username("payai"))["status"] == "free"
    assert (await db.get_username("chatai"))["status"] == "taken"
    assert (await db.get_username("cashai"))["status"] == "fragment"
    assert found == ["payai"]

    # повторная проверка того же свободного имени не шлёт уведомление снова
    await hunter._process("payai")
    assert found == ["payai"]

    # страница на t.me = занято, MTProto не нужен
    async def page(name):
        return True
    hunter._page_exists = page
    assert await hunter.check("whatever_ai") == "taken"


async def test_api(db, aiohttp_client):
    class FakeHunter:
        def __init__(self):
            self.requested = []

        def request(self, names):
            self.requested += names

    hunter = FakeHunter()
    await db.upsert_user(7, "ivan", "Ivan")
    await db.save_username("payai", "ai", score("payai"), "free")
    await db.save_username("cashai", "ai", score("cashai"), "fragment")
    client = await aiohttp_client(create_app(db, None, None, hunter))
    headers = {"X-Telegram-Init-Data": make_init_data({"id": 7, "first_name": "Ivan"})}

    # без подписки закрыто
    assert (await client.get("/api/usernames/top", headers=headers)).status == 403
    await db.extend_subscription(7, 1)

    top = await (await client.get("/api/usernames/top", headers=headers)).json()
    assert [i["name"] for i in top["items"]] == ["payai"] and top["stats"]["fragment"] == 1
    top = await (await client.get("/api/usernames/top?status=fragment", headers=headers)).json()
    assert [i["name"] for i in top["items"]] == ["cashai"]

    r = await client.post("/api/usernames/check", headers=headers,
                          json={"words": ["@Gold", "x"], "suffixes": ["ai", "game"]})
    items = {i["name"]: i["status"] for i in (await r.json())["items"]}
    assert items == {"goldai": "checking", "goldgame": "checking", "xai": "invalid", "xgame": "checking"}
    assert hunter.requested == ["goldai", "goldgame", "xgame"]

    await db.save_username("goldai", "ai", score("goldai"), "free")
    r = await client.get("/api/usernames/status?since=0&names=goldai,goldgame", headers=headers)
    assert [i["status"] for i in (await r.json())["items"]] == ["free", "checking"]
