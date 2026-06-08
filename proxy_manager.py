"""
core/proxy_manager.py — Менеджер прокси
"""
import asyncio
import time
import random
from typing import Optional
import aiohttp
from loguru import logger


class ProxyManager:
    def __init__(self, proxies: list[str]):
        self._proxies = proxies
        self._health  = {p: {"alive": True, "fails": 0, "ban_until": 0} for p in proxies}

    @classmethod
    def from_env(cls) -> "ProxyManager":
        import os
        raw = os.getenv("PROXY_LIST", "")
        if not raw:
            logger.warning("PROXY_LIST не задан — работаем без прокси")
            return cls([])
        proxies = [p.strip() for p in raw.split(",") if p.strip()]
        logger.info(f"Прокси загружены: {len(proxies)}")
        return cls(proxies)

    async def check_all(self):
        if not self._proxies:
            return
        logger.info(f"Проверяем {len(self._proxies)} прокси...")
        for p in self._proxies:
            await self._check(p)
        alive = sum(1 for h in self._health.values() if h["alive"])
        logger.info(f"Живых прокси: {alive}/{len(self._proxies)}")

    async def _check(self, proxy: str):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.binance.com/api/v3/ping",
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as r:
                    self._health[proxy]["alive"] = r.status == 200
        except Exception:
            self._health[proxy]["alive"] = False
            self._health[proxy]["fails"] += 1

    def get(self) -> Optional[str]:
        now       = time.time()
        available = [
            p for p, h in self._health.items()
            if h["alive"] and h["ban_until"] < now
        ]
        return random.choice(available) if available else (
            random.choice(self._proxies) if self._proxies else None
        )

    def ban(self, proxy: str, minutes: int = 30):
        if proxy in self._health:
            self._health[proxy]["ban_until"] = time.time() + minutes * 60
            logger.warning(f"Прокси забанен на {minutes}мин: {proxy[:30]}")

    def status(self) -> dict:
        now = time.time()
        return {
            "total":  len(self._proxies),
            "alive":  sum(1 for h in self._health.values() if h["alive"]),
            "banned": sum(1 for h in self._health.values() if h["ban_until"] > now),
        }
