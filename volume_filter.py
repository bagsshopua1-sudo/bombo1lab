"""
core/volume_filter.py — Фильтр токенов по объёму торгов

Получает реальные объёмы с бирж и отсеивает мёртвые токены.
Токены с объёмом < $100K/сутки не имеют смысла отслеживать —
там нет ликвидности для реальной манипуляции.

Обновляется каждые 6 часов.
"""
import asyncio
import time
from typing import Optional

import aiohttp
from loguru import logger

from core.history import save_volume, passes_volume_filter


class VolumeFilter:
    """
    Получает 24h объёмы со всех бирж и кэширует их.
    Используется чтобы не скорить токены без ликвидности.
    """

    def __init__(self, min_volume_usd: float = 100_000):
        self.min_volume = min_volume_usd
        self._volumes: dict[str, float] = {}
        self._last_update: float = 0
        self._update_interval = 6 * 3600   # 6 часов

    async def refresh(self, proxy: str = None):
        """Обновляет объёмы со всех бирж"""
        logger.info("Обновляем объёмы токенов...")
        start = time.time()

        results = await asyncio.gather(
            self._fetch_binance(proxy),
            self._fetch_bybit(proxy),
            self._fetch_okx(proxy),
            return_exceptions=True
        )

        merged: dict[str, float] = {}
        for res in results:
            if isinstance(res, dict):
                for sym, vol in res.items():
                    # Берём максимальный объём по всем биржам
                    merged[sym] = max(merged.get(sym, 0), vol)

        # Сохраняем в БД и в память
        for sym, vol in merged.items():
            self._volumes[sym] = vol
            save_volume(sym, vol)

        elapsed = time.time() - start
        passed  = sum(1 for v in merged.values() if v >= self.min_volume)
        logger.info(
            f"Объёмы обновлены за {elapsed:.1f}с: "
            f"{len(merged)} токенов, {passed} прошли фильтр"
        )
        self._last_update = time.time()

    async def _fetch_binance(self, proxy: str = None) -> dict[str, float]:
        """Объёмы всех USDT-пар с Binance"""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.binance.com/api/v3/ticker/24hr",
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    data = await r.json()
            result = {}
            for item in data:
                sym = item.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                vol = float(item.get("quoteVolume", 0))
                base = sym[:-4] + "/USDT"
                result[base] = result.get(base, 0) + vol
            return result
        except Exception as e:
            logger.debug(f"Binance volume fetch: {e}")
            return {}

    async def _fetch_bybit(self, proxy: str = None) -> dict[str, float]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.bybit.com/v5/market/tickers?category=spot",
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    data = await r.json(content_type=None)
            result = {}
            for item in data.get("result", {}).get("list", []):
                sym = item.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                vol = float(item.get("turnover24h", 0) or 0)
                base = sym[:-4] + "/USDT"
                result[base] = result.get(base, 0) + vol
            return result
        except Exception as e:
            logger.debug(f"Bybit volume fetch: {e}")
            return {}

    async def _fetch_okx(self, proxy: str = None) -> dict[str, float]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://www.okx.com/api/v5/market/tickers?instType=SPOT",
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    data = await r.json(content_type=None)
            result = {}
            for item in data.get("data", []):
                inst = item.get("instId", "")
                if not inst.endswith("-USDT"):
                    continue
                vol  = float(item.get("volCcy24h", 0) or 0)
                base = inst.replace("-USDT", "") + "/USDT"
                result[base] = result.get(base, 0) + vol
            return result
        except Exception as e:
            logger.debug(f"OKX volume fetch: {e}")
            return {}

    def passes(self, symbol: str) -> bool:
        """Проверяет прошёл ли токен фильтр"""
        vol = self._volumes.get(symbol, 0)
        if vol == 0:
            return passes_volume_filter(symbol, self.min_volume)
        return vol >= self.min_volume

    def get_volume(self, symbol: str) -> float:
        return self._volumes.get(symbol, 0)

    def should_refresh(self) -> bool:
        return time.time() - self._last_update > self._update_interval

    async def run_refresh_loop(self, proxy: str = None):
        """Фоновый цикл обновления объёмов"""
        while True:
            await self.refresh(proxy)
            await asyncio.sleep(self._update_interval)
