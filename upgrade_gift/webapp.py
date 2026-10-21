"""HTTP-сервер мини-аппа: статика + API + вебхук xRocket."""
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qsl

from aiohttp import web

from . import config
from .db import Database, sanitize_settings
from .payments import Billing, XRocketError

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "webapp"


def validate_init_data(init_data: str, bot_token: str, max_age: int = config.INIT_DATA_TTL) -> dict | None:
    """Проверка подписи Telegram WebApp initData. Возвращает пользователя или None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        if max_age and time.time() - int(pairs.get("auth_date", 0)) > max_age:
            return None
        user = json.loads(pairs.get("user", "null"))
    except ValueError:
        return None
    return user if isinstance(user, dict) and "id" in user else None


def _is_active(user: dict) -> bool:
    return user["sub_until"] > time.time()


def _public_user(user: dict) -> dict:
    active = _is_active(user)
    return {
        "id": user["id"],
        "username": user["username"],
        "first_name": user["first_name"],
        "sub_until": user["sub_until"],
        "active": active,
        # Настройки видны только с активной подпиской
        "settings": user["settings"] if active else None,
    }


def _forbidden() -> web.HTTPForbidden:
    return web.HTTPForbidden(text=json.dumps({"error": "Нужна подписка"}),
                             content_type="application/json")


def create_app(db: Database, billing: Billing | None,
               attributes: Callable[[int], Awaitable[dict]] | None = None) -> web.Application:
    routes = web.RouteTableDef()

    async def current_user(request: web.Request) -> dict:
        tg_user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""), config.BOT_TOKEN)
        if not tg_user:
            raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}),
                                       content_type="application/json")
        user = await db.get_user(tg_user["id"])
        if not user:
            await db.upsert_user(tg_user["id"], tg_user.get("username"), tg_user.get("first_name"))
            user = await db.get_user(tg_user["id"])
        return user

    async def subscribed_user(request: web.Request) -> dict:
        user = await current_user(request)
        if not _is_active(user):
            raise _forbidden()
        return user

    @routes.get("/")
    async def index(_):
        return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @routes.get("/api/me")
    async def me(request):
        user = await current_user(request)
        plans = [{"key": k, **v, "currency": config.XROCKET_CURRENCY} for k, v in config.PLANS.items()]
        return web.json_response({"user": _public_user(user), "plans": plans})

    @routes.put("/api/settings")
    async def save_settings(request):
        user = await subscribed_user(request)
        try:
            raw = await request.json()
        except ValueError:
            raise web.HTTPBadRequest()
        settings = sanitize_settings(raw, base=user["settings"])
        await db.save_settings(user["id"], settings)
        return web.json_response({"settings": settings})

    @routes.get("/api/collections")
    async def collections(request):
        await subscribed_user(request)
        items = [
            {"id": c["gift_id"], "title": c["title"], "issued": c["issued"],
             "total": c["total"], "active": bool(c["active"])}
            for c in await db.collections()
        ]
        return web.json_response({"collections": items})

    @routes.get("/api/collections/{id:\\d+}/attributes")
    async def collection_attributes(request):
        await subscribed_user(request)
        if not attributes:
            return web.json_response({"error": "Мониторинг не запущен"}, status=503)
        try:
            data = await attributes(int(request.match_info["id"]))
        except Exception as e:
            log.warning("Атрибуты коллекции %s: %s", request.match_info["id"], e)
            return web.json_response({"error": "Не удалось загрузить список"}, status=502)
        return web.json_response(data)

    @routes.post("/api/invoices")
    async def create_invoice(request):
        user = await current_user(request)
        if not billing:
            return web.json_response({"error": "Оплата временно недоступна"}, status=503)
        try:
            plan = (await request.json()).get("plan")
        except (ValueError, AttributeError):
            plan = None
        if plan not in config.PLANS:
            return web.json_response({"error": "Неизвестный тариф"}, status=400)
        try:
            invoice = await billing.create(user["id"], plan)
        except XRocketError as e:
            log.error("Создание счёта: %s", e)
            return web.json_response({"error": f"Не удалось создать счёт: {e.message}"}, status=502)
        return web.json_response(invoice)

    @routes.get("/api/invoices/{id:\\d+}")
    async def invoice_status(request):
        user = await current_user(request)
        inv_id = int(request.match_info["id"])
        invoice = await db.get_invoice(inv_id)
        if not invoice or invoice["user_id"] != user["id"]:
            raise web.HTTPNotFound()
        status = invoice["status"]
        if status == "active" and billing:
            try:
                status = await billing.sync(inv_id)
            except XRocketError as e:
                log.warning("Статус счёта %s: %s", inv_id, e)
        user = await db.get_user(user["id"])
        return web.json_response({"status": status, "user": _public_user(user)})

    @routes.post("/xrocket/webhook")
    async def xrocket_webhook(request):
        body = await request.read()
        if not billing or not billing.xrocket.verify_webhook(body, request.headers.get("rocket-pay-signature", "")):
            raise web.HTTPForbidden()
        try:
            data = json.loads(body)
            inv_id = int(data["data"]["id"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"ok": True})
        if data.get("type") == "invoicePay":
            try:
                # Статус всё равно подтверждаем запросом к API
                await billing.sync(inv_id)
            except XRocketError as e:
                log.warning("Вебхук, счёт %s: %s", inv_id, e)
                raise web.HTTPServiceUnavailable()
        return web.json_response({"ok": True})

    @routes.get("/health")
    async def health(_):
        return web.json_response({"ok": True})

    app = web.Application(client_max_size=256 * 1024)
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    return app
