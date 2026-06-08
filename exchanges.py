"""
core/exchanges.py — Мультибиржевой коллектор данных

7 бирж × спот + фьючерсы = 14 потоков данных
Все данные нормализуются в единый формат Trade / OrderBookUpdate / FuturesData
"""
import asyncio
import json
import time
import random
from dataclasses import dataclass
from typing import Optional, Callable

import aiohttp
from loguru import logger

from core.config import Trade, OrderBookUpdate, FuturesData


# ─── Конфигурация WebSocket для каждой биржи ─────────────────────────────────

WS_CONFIG = {
    "binance_spot": {
        "url_fn":   lambda syms: f"wss://stream.binance.com:9443/stream?streams={'/'.join(s.lower()+'@aggTrade' for s in syms[:100])}",
        "market":   "spot",
        "exchange": "binance",
        "depth_fn": lambda syms: f"wss://stream.binance.com:9443/stream?streams={'/'.join(s.lower()+'@depth@100ms' for s in syms[:100])}",
        "max_syms": 100,
    },
    "binance_futures": {
        "url_fn":   lambda syms: f"wss://fstream.binance.com/stream?streams={'/'.join(s.lower()+'@aggTrade' for s in syms[:100])}",
        "market":   "futures",
        "exchange": "binance",
        "extra_fn": lambda syms: f"wss://fstream.binance.com/stream?streams={'/'.join(s.lower()+'@forceOrder' for s in syms[:50])}",
        "max_syms": 100,
    },
    "bybit_spot": {
        "url":      "wss://stream.bybit.com/v5/public/spot",
        "market":   "spot",
        "exchange": "bybit",
        "sub_fn":   lambda syms: {"op":"subscribe","args":[f"publicTrade.{s}" for s in syms[:50]]},
        "max_syms": 50,
    },
    "bybit_futures": {
        "url":      "wss://stream.bybit.com/v5/public/linear",
        "market":   "futures",
        "exchange": "bybit",
        "sub_fn":   lambda syms: {"op":"subscribe","args":[f"publicTrade.{s}" for s in syms[:50]] + [f"tickers.{s}" for s in syms[:50]]},
        "max_syms": 50,
    },
    "okx_spot": {
        "url":      "wss://ws.okx.com:8443/ws/v5/public",
        "market":   "spot",
        "exchange": "okx",
        "sub_fn":   lambda syms: {"op":"subscribe","args":[{"channel":"trades","instId":s} for s in syms[:50]]},
        "max_syms": 50,
    },
    "okx_futures": {
        "url":      "wss://ws.okx.com:8443/ws/v5/public",
        "market":   "futures",
        "exchange": "okx",
        "sub_fn":   lambda syms: {"op":"subscribe","args":[{"channel":"trades","instId":s} for s in syms[:50]] + [{"channel":"open-interest","instId":s} for s in syms[:50]]},
        "max_syms": 50,
    },
    "gate_spot": {
        "url":      "wss://api.gateio.ws/ws/v4/",
        "market":   "spot",
        "exchange": "gate",
        "sub_fn":   lambda syms: {"time":int(time.time()),"channel":"spot.trades","event":"subscribe","payload":syms[:100]},
        "max_syms": 100,
    },
    "gate_futures": {
        "url":      "wss://fx-ws.gateio.ws/v4/ws/usdt",
        "market":   "futures",
        "exchange": "gate",
        "sub_fn":   lambda syms: {"time":int(time.time()),"channel":"futures.trades","event":"subscribe","payload":["1000","" ]+syms[:50]},
        "max_syms": 50,
    },
    "bitget_spot": {
        "url":      "wss://ws.bitget.com/v2/ws/public",
        "market":   "spot",
        "exchange": "bitget",
        "sub_fn":   lambda syms: {"op":"subscribe","args":[{"instType":"SPOT","channel":"trade","instId":s} for s in syms[:50]]},
        "max_syms": 50,
    },
    "bitget_futures": {
        "url":      "wss://ws.bitget.com/v2/ws/public",
        "market":   "futures",
        "exchange": "bitget",
        "sub_fn":   lambda syms: {"op":"subscribe","args":[{"instType":"USDT-FUTURES","channel":"trade","instId":s} for s in syms[:50]]},
        "max_syms": 50,
    },
    "mexc_spot": {
        "url":      "wss://wbs.mexc.com/ws",
        "market":   "spot",
        "exchange": "mexc",
        "sub_fn":   lambda syms: {"method":"SUBSCRIPTION","params":[f"spot@public.deals.v3.api@{s}" for s in syms[:30]]},
        "max_syms": 30,
    },
    "kucoin_spot": {
        "url":      "",   # KuCoin требует токен — получаем динамически
        "market":   "spot",
        "exchange": "kucoin",
        "max_syms": 50,
        "needs_token": True,
    },
}


def normalize_symbol(raw: str, exchange: str) -> str:
    """Приводит символ к формату BTC/USDT"""
    r = raw.upper()
    if "-USDT-SWAP" in r: r = r.replace("-USDT-SWAP", "")
    if "-USDT" in r:       r = r.replace("-USDT", "")
    if "_USDT" in r:       r = r.replace("_USDT", "")
    if r.endswith("USDT"): r = r[:-4]
    return r + "/USDT"


# ─── Получение списков символов ───────────────────────────────────────────────

async def fetch_symbols(exchange_id: str, proxy: str = None) -> list[str]:
    """Получает все USDT-пары с биржи"""

    endpoints = {
        "binance_spot":    ("https://api.binance.com", "/api/v3/exchangeInfo"),
        "binance_futures": ("https://fapi.binance.com", "/fapi/v1/exchangeInfo"),
        "bybit_spot":      ("https://api.bybit.com", "/v5/market/instruments-info?category=spot&limit=500"),
        "bybit_futures":   ("https://api.bybit.com", "/v5/market/instruments-info?category=linear&limit=500"),
        "okx_spot":        ("https://www.okx.com", "/api/v5/public/instruments?instType=SPOT"),
        "okx_futures":     ("https://www.okx.com", "/api/v5/public/instruments?instType=SWAP"),
        "gate_spot":       ("https://api.gateio.ws", "/api/v4/spot/currency_pairs"),
        "gate_futures":    ("https://api.gateio.ws", "/api/v4/futures/usdt/contracts"),
        "bitget_spot":     ("https://api.bitget.com", "/api/v2/spot/public/symbols"),
        "bitget_futures":  ("https://api.bitget.com", "/api/v2/mix/market/tickers?productType=USDT-FUTURES"),
        "mexc_spot":       ("https://api.mexc.com", "/api/v3/exchangeInfo"),
        "kucoin_spot":     ("https://api.kucoin.com", "/api/v2/symbols"),
    }

    if exchange_id not in endpoints:
        return []

    base, path = endpoints[exchange_id]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                base + path, proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                data = await r.json(content_type=None)
        return _extract_symbols(data, exchange_id)
    except Exception as e:
        logger.warning(f"Символы {exchange_id}: {e}")
        return []


def _extract_symbols(data: dict, exchange_id: str) -> list[str]:
    syms = []
    if exchange_id.startswith("binance"):
        for s in data.get("symbols", []):
            if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
                syms.append(s["symbol"])
    elif exchange_id.startswith("bybit"):
        for s in data.get("result", {}).get("list", []):
            if s.get("quoteCoin") == "USDT" and s.get("status") == "Trading":
                syms.append(s["symbol"])
    elif exchange_id.startswith("okx"):
        for s in data.get("data", []):
            if s.get("quoteCcy") == "USDT" or "USDT" in s.get("instId", ""):
                if s.get("state") == "live":
                    syms.append(s["instId"])
    elif exchange_id.startswith("gate"):
        if isinstance(data, list):
            for s in data:
                name = s.get("id", s.get("name", ""))
                if "USDT" in name and s.get("trade_status", s.get("in_delisting", False)) != True:
                    syms.append(name)
    elif exchange_id.startswith("bitget"):
        for s in data.get("data", []):
            sym = s.get("symbol", "")
            if sym.endswith("USDT"):
                syms.append(sym)
    elif exchange_id.startswith("mexc"):
        for s in data.get("symbols", []):
            if s.get("quoteAsset") == "USDT" and s.get("status") == 1:
                syms.append(s["symbol"])
    elif exchange_id.startswith("kucoin"):
        for s in data.get("data", []):
            if s.get("quoteCurrency") == "USDT" and s.get("enableTrading"):
                syms.append(s["symbol"])
    return [s for s in syms if s]


# ─── WebSocket Worker ─────────────────────────────────────────────────────────

class ExchangeWorker:
    def __init__(
        self,
        exchange_id: str,
        symbols: list[str],
        proxy: Optional[str],
        on_trade: Callable,
        on_futures: Callable,
    ):
        self.exchange_id = exchange_id
        self.symbols     = symbols
        self.proxy       = proxy
        self.on_trade    = on_trade
        self.on_futures  = on_futures
        self.cfg         = WS_CONFIG.get(exchange_id, {})
        self._running    = False
        self._delay      = 1

    async def run(self):
        self._running = True
        while self._running:
            try:
                await self._connect()
                self._delay = 1
            except Exception as e:
                logger.debug(f"[{self.exchange_id}] {e}, reconnect {self._delay}s")
                await asyncio.sleep(self._delay)
                self._delay = min(self._delay * 2, 60)

    async def _connect(self):
        import websockets

        cfg = self.cfg
        url_fn  = cfg.get("url_fn")
        url_str = cfg.get("url", "")
        sub_fn  = cfg.get("sub_fn")

        if url_fn:
            url = url_fn(self.symbols)
        elif url_str:
            url = url_str
        else:
            return

        proxy_arg = self.proxy if self.proxy and "socks" in str(self.proxy) else None

        async with websockets.connect(
            url,
            proxy=proxy_arg,
            ping_interval=20,
            max_size=10*1024*1024,
        ) as ws:
            if sub_fn:
                await ws.send(json.dumps(sub_fn(self.symbols)))

            async for raw in ws:
                try:
                    await self._parse(json.loads(raw))
                except Exception:
                    pass

    async def _parse(self, data: dict):
        exch = self.cfg.get("exchange", "")
        mkt  = self.cfg.get("market", "spot")

        # ── Binance ──────────────────────────────────────────────────────────
        if exch == "binance":
            stream = data.get("stream", "")
            d = data.get("data", {})
            if "aggTrade" in stream:
                await self.on_trade(Trade(
                    symbol    = normalize_symbol(d.get("s",""), "binance"),
                    timestamp = d.get("T", 0) / 1000,
                    price     = float(d.get("p", 0)),
                    qty       = float(d.get("q", 0)),
                    side      = "sell" if d.get("m") else "buy",
                    exchange  = "binance",
                    market    = mkt,
                ))
            elif "forceOrder" in stream:
                o = d.get("o", {})
                await self.on_futures(FuturesData(
                    symbol    = normalize_symbol(o.get("s",""), "binance"),
                    timestamp = time.time(),
                    exchange  = "binance",
                    long_liq_usd  = float(o.get("q",0)) * float(o.get("p",0)) if o.get("S") == "SELL" else None,
                    short_liq_usd = float(o.get("q",0)) * float(o.get("p",0)) if o.get("S") == "BUY"  else None,
                ))

        # ── Bybit ─────────────────────────────────────────────────────────────
        elif exch == "bybit":
            topic = data.get("topic", "")
            d     = data.get("data", {})
            if "publicTrade" in topic:
                sym = topic.split(".")[-1]
                for t in (d if isinstance(d, list) else [d]):
                    await self.on_trade(Trade(
                        symbol    = normalize_symbol(sym, "bybit"),
                        timestamp = int(t.get("T", 0)) / 1000,
                        price     = float(t.get("p", 0)),
                        qty       = float(t.get("v", 0)),
                        side      = "sell" if t.get("S") == "Sell" else "buy",
                        exchange  = "bybit",
                        market    = mkt,
                    ))
            elif "tickers" in topic:
                sym = topic.split(".")[-1]
                await self.on_futures(FuturesData(
                    symbol       = normalize_symbol(sym, "bybit"),
                    timestamp    = time.time(),
                    exchange     = "bybit",
                    open_interest= float(d.get("openInterestValue", 0) or 0),
                    funding_rate = float(d.get("fundingRate", 0) or 0),
                ))

        # ── OKX ───────────────────────────────────────────────────────────────
        elif exch == "okx":
            arg  = data.get("arg", {})
            ch   = arg.get("channel", "")
            inst = arg.get("instId", "")
            lst  = data.get("data", [])
            if not lst: return
            d = lst[0]
            if ch == "trades":
                await self.on_trade(Trade(
                    symbol    = normalize_symbol(inst, "okx"),
                    timestamp = int(d.get("ts", 0)) / 1000,
                    price     = float(d.get("px", 0)),
                    qty       = float(d.get("sz", 0)),
                    side      = d.get("side", "buy"),
                    exchange  = "okx",
                    market    = mkt,
                ))
            elif ch == "open-interest":
                await self.on_futures(FuturesData(
                    symbol       = normalize_symbol(inst, "okx"),
                    timestamp    = time.time(),
                    exchange     = "okx",
                    open_interest= float(d.get("oiCcy", 0) or 0),
                ))

        # ── Gate ──────────────────────────────────────────────────────────────
        elif exch == "gate":
            ch  = data.get("channel", "")
            res = data.get("result", {})
            if "trades" in ch and isinstance(res, dict):
                sym = res.get("currency_pair", res.get("contract", ""))
                await self.on_trade(Trade(
                    symbol    = normalize_symbol(sym, "gate"),
                    timestamp = float(res.get("create_time", time.time())),
                    price     = float(res.get("price", 0)),
                    qty       = float(res.get("amount", res.get("size", 0))),
                    side      = res.get("side", "buy"),
                    exchange  = "gate",
                    market    = mkt,
                ))

        # ── Bitget ────────────────────────────────────────────────────────────
        elif exch == "bitget":
            arg  = data.get("arg", {})
            inst = arg.get("instId", "")
            for t in data.get("data", []):
                await self.on_trade(Trade(
                    symbol    = normalize_symbol(inst, "bitget"),
                    timestamp = float(t.get("ts", time.time()*1000)) / 1000,
                    price     = float(t.get("price", 0)),
                    qty       = float(t.get("size", 0)),
                    side      = t.get("side", "buy"),
                    exchange  = "bitget",
                    market    = mkt,
                ))

        # ── MEXC ──────────────────────────────────────────────────────────────
        elif exch == "mexc":
            ch = data.get("c", "")
            d  = data.get("d", {})
            if "deals" in ch:
                sym = data.get("s", "")
                for t in d.get("deals", []):
                    await self.on_trade(Trade(
                        symbol    = normalize_symbol(sym, "mexc"),
                        timestamp = float(t.get("t", 0)) / 1000,
                        price     = float(t.get("p", 0)),
                        qty       = float(t.get("v", 0)),
                        side      = "sell" if t.get("S") == 2 else "buy",
                        exchange  = "mexc",
                        market    = mkt,
                    ))


# ─── Менеджер всех бирж ───────────────────────────────────────────────────────

class MultiExchangeManager:

    def __init__(
        self,
        enabled_exchanges: list[str],
        proxy_list: list[str],
        min_volume: float,
        on_trade: Callable,
        on_futures: Callable,
    ):
        self.enabled    = enabled_exchanges
        self.proxies    = proxy_list
        self.min_volume = min_volume
        self.on_trade   = on_trade
        self.on_futures = on_futures
        self.symbols_by_exchange: dict = {}
        self.stats: dict = {"trades": 0, "workers": 0}

    def _get_proxy(self) -> Optional[str]:
        return random.choice(self.proxies) if self.proxies else None

    async def fetch_all_symbols(self):
        logger.info("Получаем символы со всех бирж...")
        tasks = {
            ex: fetch_symbols(ex, self._get_proxy())
            for ex in self.enabled
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for ex, res in zip(tasks.keys(), results):
            if isinstance(res, list):
                self.symbols_by_exchange[ex] = res
                logger.info(f"  {ex}: {len(res)} символов")
            else:
                self.symbols_by_exchange[ex] = []

        total = sum(len(v) for v in self.symbols_by_exchange.values())
        logger.info(f"Итого: {total} символов")

    async def run(self):
        await self.fetch_all_symbols()

        tasks = []
        for ex in self.enabled:
            syms = self.symbols_by_exchange.get(ex, [])
            if not syms:
                continue
            cfg      = WS_CONFIG.get(ex, {})
            max_syms = cfg.get("max_syms", 50)

            for i in range(0, len(syms), max_syms):
                batch  = syms[i:i + max_syms]
                proxy  = self._get_proxy()
                worker = ExchangeWorker(
                    exchange_id = ex,
                    symbols     = batch,
                    proxy       = proxy,
                    on_trade    = self._on_trade,
                    on_futures  = self.on_futures,
                )
                tasks.append(asyncio.create_task(worker.run()))
                self.stats["workers"] += 1

        logger.info(f"Запущено {self.stats['workers']} WebSocket воркеров")
        await asyncio.gather(*tasks)

    async def _on_trade(self, trade: Trade):
        self.stats["trades"] += 1
        await self.on_trade(trade)
