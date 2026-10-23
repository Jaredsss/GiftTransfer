"""Курсы валют: настоящие цены в $, пересчитанные в звёзды OceanGram."""
import asyncio
import logging
import time
from decimal import Decimal

import aiohttp

from . import config

log = logging.getLogger(__name__)

COINGECKO = "https://api.coingecko.com/api/v3/simple/price"
BINANCE = "https://api.binance.com/api/v3/ticker/price"
COINGECKO_IDS = {"USDT": "tether", "BTC": "bitcoin"}


class Rates:
    def __init__(self):
        # Стартовые значения на случай, если API недоступны при запуске
        self.usd: dict[str, Decimal] = {
            "USDT": Decimal(1), "BTC": Decimal(100000), "GRAM": config.GRAM_PRICE_USD,
        }
        self.updated_at = 0.0
        self._lock = asyncio.Lock()

    async def _coingecko(self, session: aiohttp.ClientSession) -> dict[str, Decimal]:
        ids = dict(COINGECKO_IDS)
        if config.GRAM_COINGECKO_ID:
            ids["GRAM"] = config.GRAM_COINGECKO_ID
        async with session.get(COINGECKO, params={"ids": ",".join(ids.values()), "vs_currencies": "usd"}) as r:
            data = await r.json(content_type=None)
        return {code: Decimal(str(data[cid]["usd"])) for code, cid in ids.items() if cid in data}

    async def _binance(self, session: aiohttp.ClientSession) -> dict[str, Decimal]:
        async with session.get(BINANCE, params={"symbol": "BTCUSDT"}) as r:
            data = await r.json(content_type=None)
        return {"BTC": Decimal(data["price"]), "USDT": Decimal(1)}

    async def refresh(self) -> None:
        async with self._lock:
            if time.time() - self.updated_at < config.RATES_TTL:
                return
            prices: dict[str, Decimal] = {}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                for source in (self._coingecko, self._binance):
                    try:
                        prices = await source(session)
                        if "BTC" in prices:
                            break
                    except Exception as e:
                        log.warning("Курсы (%s): %s", source.__name__, e)
            self.usd.update({k: v for k, v in prices.items() if v > 0})
            if not config.GRAM_COINGECKO_ID:
                self.usd["GRAM"] = config.GRAM_PRICE_USD
            self.updated_at = time.time()

    def price_usd(self, code: str) -> Decimal:
        return self.usd[code]

    def price_stars(self, code: str) -> Decimal:
        """Сколько звёзд стоит 1 монета."""
        return self.usd[code] / config.STAR_PRICE_USD

    def convert(self, value: Decimal, src: str, dst: str) -> Decimal:
        return value * self.usd[src] / self.usd[dst]

    async def run(self) -> None:
        while True:
            self.updated_at = 0
            await self.refresh()
            await asyncio.sleep(config.RATES_TTL)
