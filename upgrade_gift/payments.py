"""xRocket Pay API: https://pay.xrocket.tg/api/"""
import asyncio
import hashlib
import hmac
import json
import logging
import time

import aiohttp
from aiogram import Bot

from . import config
from .db import Database

log = logging.getLogger(__name__)


class XRocketError(Exception):
    def __init__(self, text: str, message: str = ""):
        super().__init__(text)
        self.message = message  # ответ xRocket, понятный человеку


class XRocket:
    def __init__(self, api_key: str, base_url: str = config.XROCKET_BASE_URL):
        self.api_key = api_key
        self.base_url = base_url
        self._session: aiohttp.ClientSession | None = None

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Rocket-Pay-Key": self.api_key, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20),
            )
        async with self._session.request(method, self.base_url + path, json=payload) as resp:
            try:
                data = await resp.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                data = None
        if not isinstance(data, dict) or not data.get("success"):
            message = ""
            if isinstance(data, dict):
                errors = "; ".join(
                    f"{e.get('property')}: {e.get('error')}" for e in data.get("errors") or []
                    if isinstance(e, dict)
                )
                message = "; ".join(x for x in (str(data.get("message") or ""), errors) if x)
            raise XRocketError(f"{method} {path}: HTTP {resp.status} {data}",
                               message or f"HTTP {resp.status}")
        return data["data"]

    async def create_invoice(self, amount: float, currency: str, description: str,
                             payload: str, expired_in: int = 3600) -> dict:
        return await self._request("POST", "/tg-invoices", {
            "amount": amount,
            "currency": currency,
            "numPayments": 1,
            "description": description,
            "hiddenMessage": "Спасибо! Подписка Upgrade Gift активирована.",
            "payload": payload,
            "expiredIn": expired_in,
        })

    async def app_info(self) -> dict:
        return await self._request("GET", "/app/info")

    async def get_invoice(self, invoice_id: int) -> dict:
        return await self._request("GET", f"/tg-invoices/{int(invoice_id)}")

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        """HMAC-SHA256(body) с ключом SHA256(api_key), заголовок rocket-pay-signature."""
        secret = hashlib.sha256(self.api_key.encode()).digest()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")

    async def close(self) -> None:
        if self._session:
            await self._session.close()


class Billing:
    """Создание счетов и активация подписки после оплаты."""

    def __init__(self, xrocket: XRocket, db: Database, bot: Bot):
        self.xrocket = xrocket
        self.db = db
        self.bot = bot

    async def create(self, user_id: int, plan_key: str) -> dict:
        plan = config.PLANS[plan_key]
        inv = await self.xrocket.create_invoice(
            amount=plan["price"],
            currency=config.XROCKET_CURRENCY,
            description=f"{config.BOT_NAME}: подписка на {plan['title']}",
            payload=json.dumps({"u": user_id, "p": plan_key}),
        )
        await self.db.add_invoice(inv["id"], user_id, plan_key, plan["price"],
                                  config.XROCKET_CURRENCY, inv["link"])
        return {"id": inv["id"], "link": inv["link"], "status": "active"}

    async def sync(self, invoice_id: int) -> str:
        """Сверяет статус счёта с xRocket и активирует подписку при оплате."""
        local = await self.db.get_invoice(invoice_id)
        if not local:
            return "unknown"
        if local["status"] != "active":
            return local["status"]
        remote = await self.xrocket.get_invoice(invoice_id)
        status = remote.get("status")
        if status == "paid":
            await self._activate(local)
        elif status == "expired" or (status != "active" and status is not None):
            await self.db.mark_invoice(invoice_id, status)
        return status or "active"

    async def _activate(self, invoice: dict) -> None:
        if not await self.db.mark_invoice(invoice["id"], "paid"):
            return  # уже обработан
        plan = config.PLANS[invoice["plan"]]
        until = await self.db.extend_subscription(invoice["user_id"], plan["days"])
        log.info("Оплачен счёт %s: user %s +%s дн.", invoice["id"], invoice["user_id"], plan["days"])
        try:
            await self.bot.send_message(
                invoice["user_id"],
                f"✅ Оплата получена! Подписка активна до <b>{time.strftime('%d.%m.%Y %H:%M', time.gmtime(until))} UTC</b>.",
            )
        except Exception as e:
            log.warning("Не удалось уведомить об оплате: %s", e)

    async def poll(self, interval: int = 20) -> None:
        """Резервная проверка счетов на случай, если вебхук не настроен."""
        while True:
            for inv in await self.db.active_invoices():
                if time.time() - inv["created_at"] > 2 * 86400:
                    await self.db.mark_invoice(inv["id"], "expired")
                    continue
                try:
                    await self.sync(inv["id"])
                except Exception as e:
                    log.warning("Проверка счёта %s: %s", inv["id"], e)
            await asyncio.sleep(interval)
