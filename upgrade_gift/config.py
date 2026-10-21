import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int = 0) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


BOT_NAME = "Upgrade Gift"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

WEBAPP_URL = os.getenv("WEBAPP_URL", "https://jsqvpn.online").rstrip("/")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = _int("WEB_PORT", 8080)
# Максимальный возраст initData мини-аппа (сек)
INIT_DATA_TTL = _int("INIT_DATA_TTL", 86400)

TG_API_ID = _int("TG_API_ID")
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION = os.getenv("TG_SESSION", "").strip()
SCAN_CONCURRENCY = _int("SCAN_CONCURRENCY", 4)
SCAN_INTERVAL = _float("SCAN_INTERVAL", 2.0)
FLOOR_CACHE_TTL = _int("FLOOR_CACHE_TTL", 120)

XROCKET_API_KEY = os.getenv("XROCKET_API_KEY", "").strip()
# Секрет вебхука из настроек приложения Rocket Pay (если пусто — используется API-токен)
XROCKET_WEBHOOK_SECRET = os.getenv("XROCKET_WEBHOOK_SECRET", "").strip()
XROCKET_CURRENCY = os.getenv("XROCKET_CURRENCY", "USDT").strip()
XROCKET_BASE_URL = os.getenv("XROCKET_BASE_URL", "https://pay.xrocket.tg").rstrip("/")

DB_PATH = os.getenv("DB_PATH", "data/upgrade_gift.db")

# Тарифы: цена в XROCKET_CURRENCY (USDT ≈ $)
PLANS = {
    "day": {"title": "1 день", "days": 1, "price": 1},
    "month": {"title": "30 дней", "days": 30, "price": 25},
    "quarter": {"title": "90 дней", "days": 90, "price": 60},
}
