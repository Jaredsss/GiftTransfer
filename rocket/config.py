import os
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _dec(name: str, default: str) -> Decimal:
    value = os.getenv(name, "").strip()
    return Decimal(value or default)


BOT_NAME = "Rocket"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_BASE = os.getenv("API_BASE", "https://oceanment.space").rstrip("/")
LINK_BASE = os.getenv("LINK_BASE", "https://t.me").rstrip("/")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

STAR_PRICE_USD = _dec("STAR_PRICE_USD", "0.015")
GRAM_PRICE_USD = _dec("GRAM_PRICE_USD", "0.005")
GRAM_COINGECKO_ID = os.getenv("GRAM_COINGECKO_ID", "").strip()
RATES_TTL = _int("RATES_TTL", 60)

SWAP_FEE_PERCENT = _dec("SWAP_FEE_PERCENT", "1")
P2P_FEE_PERCENT = _dec("P2P_FEE_PERCENT", "0")

P2P_PAY_MINUTES = _int("P2P_PAY_MINUTES", 15)
P2P_DISPUTE_AFTER_MINUTES = _int("P2P_DISPUTE_AFTER_MINUTES", 5)

DB_PATH = os.getenv("DB_PATH", "data/rocket.db")
