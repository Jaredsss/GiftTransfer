"""Валюты, суммы и адреса кошельков.

Суммы хранятся целыми числами в минимальных единицах (как сатоши).
Адреса внутренние: работают только между пользователями бота, но по виду и
длине похожи на настоящие и содержат контрольную сумму от опечаток.
"""
import hashlib
import secrets
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


@dataclass(frozen=True)
class Currency:
    code: str
    name: str
    emoji: str
    decimals: int
    prefix: str      # начало адреса
    alphabet: str    # символы адреса
    length: int      # полная длина адреса

    @property
    def unit(self) -> int:
        return 10 ** self.decimals


CURRENCIES = {
    # 34 символа, как у адресов TRON (USDT TRC-20)
    "USDT": Currency("USDT", "Tether USD", "💵", 6, "USDT", BASE58, 34),
    # 48 символов, как у адресов TON
    "GRAM": Currency("GRAM", "Gram", "💎", 9, "GRAM", BASE64URL, 48),
    # 42 символа, как у bech32-адресов Bitcoin (bc1q…)
    "BTC": Currency("BTC", "Bitcoin", "🟠", 8, "btc1q", BECH32, 42),
}
CODES = list(CURRENCIES)

CHECKSUM_LEN = 4


def _checksum(cur: Currency, body: str) -> str:
    digest = hashlib.sha256(f"{cur.code}:{cur.prefix}{body}".encode()).digest()
    return "".join(cur.alphabet[b % len(cur.alphabet)] for b in digest[:CHECKSUM_LEN])


def generate_address(code: str) -> str:
    cur = CURRENCIES[code]
    size = cur.length - len(cur.prefix) - CHECKSUM_LEN
    body = "".join(secrets.choice(cur.alphabet) for _ in range(size))
    return cur.prefix + body + _checksum(cur, body)


def address_currency(address: str) -> str | None:
    """Код валюты, если адрес корректен (формат + контрольная сумма), иначе None."""
    address = address.strip()
    for cur in CURRENCIES.values():
        candidate = address.lower() if cur.alphabet == BECH32 else address
        if not candidate.startswith(cur.prefix) or len(candidate) != cur.length:
            continue
        body = candidate[len(cur.prefix):-CHECKSUM_LEN]
        if any(ch not in cur.alphabet for ch in body):
            return None
        if candidate[-CHECKSUM_LEN:] == _checksum(cur, body):
            return cur.code
        return None
    return None


def normalize_address(address: str) -> str:
    address = address.strip()
    code = address_currency(address)
    return address.lower() if code and CURRENCIES[code].alphabet == BECH32 else address


# --- суммы ---

def parse_amount(text: str, code: str) -> int | None:
    """'1.5' -> 1500000 (для USDT). None, если сумма некорректна."""
    cur = CURRENCIES[code]
    try:
        value = Decimal(text.strip().replace(",", ".").replace(" ", ""))
    except (InvalidOperation, AttributeError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    units = value * cur.unit
    if units != units.to_integral_value():
        return None  # больше знаков после запятой, чем у валюты
    units = int(units)
    return units if 0 < units < 10 ** 24 else None


def to_decimal(units: int, code: str) -> Decimal:
    return Decimal(units) / CURRENCIES[code].unit


def from_decimal(value: Decimal, code: str) -> int:
    """Округление вниз до минимальной единицы валюты."""
    return int((value * CURRENCIES[code].unit).to_integral_value(rounding=ROUND_DOWN))


def fmt(units: int, code: str, symbol: bool = True) -> str:
    value = to_decimal(units, code)
    text = f"{value:,.{CURRENCIES[code].decimals}f}".rstrip("0").rstrip(".").replace(",", " ")
    return f"{text} {code}" if symbol else text


def fmt_stars(value: Decimal) -> str:
    if value >= 100:
        text = f"{value.quantize(Decimal(1)):,}"
    else:
        text = f"{value.quantize(Decimal('0.01')):,}".rstrip("0").rstrip(".")
    return text.replace(",", " ") + " ⭐"
