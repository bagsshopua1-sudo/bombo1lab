"""
onchain/clients.py — Все внешние API в одном файле

Приоритет данных (от самых надёжных к менее надёжным):
  1. Glassnode    — exchange flows, whale metrics        $29/мес
  2. Deribit      — опционы, borrow rate                бесплатно
  3. Coinglass    — OI, funding, liquidations           $50/мес
  4. Arkham       — entity clusters (только при score>65) бесплатно*
  5. GeckoTerminal — DEX swaps                          бесплатно
  6. Etherscan    — raw on-chain                        бесплатно

УДАЛЕНО: LunarCrush (social) — запаздывает, не даёт преимущества
УДАЛЕНО: Nansen — дублирует Glassnode+Arkham за $150/мес
"""
import asyncio
import time
from typing import Optional

import aiohttp
from loguru import logger

from core.config import Config, OnchainData


# ─── Base ─────────────────────────────────────────────────────────────────────

class BaseClient:
    def __init__(self, base_url: str, rate_limit: int = 20):
        self.base_url = base_url
        self._rl = rate_limit
        self._reqs: list = []
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get(self, endpoint: str, params: dict = None, headers: dict = None) -> dict:
        now = time.time()
        self._reqs = [t for t in self._reqs if now - t < 60]
        if len(self._reqs) >= self._rl:
            await asyncio.sleep(60 - (now - self._reqs[0]) + 0.5)
        self._reqs.append(time.time())

        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()

        try:
            async with self._session.get(
                self.base_url + endpoint,
                params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                if r.status == 429:
                    await asyncio.sleep(30)
                return {}
        except Exception as e:
            logger.debug(f"API error {self.base_url}{endpoint}: {e}")
            return {}


# ─── Glassnode — exchange flows, whale metrics ────────────────────────────────

class GlassnodeClient(BaseClient):
    """
    Самые надёжные ончейн данные.
    Ключевые метрики: exchange supply change, whale transactions.
    """

    def __init__(self):
        super().__init__("https://api.glassnode.com/v1/metrics", rate_limit=20)

    async def get_exchange_supply_change(self, asset: str) -> dict:
        """
        Изменение % токенов на биржах — ПЕРВОПРИЧИНА категории A.
        Отрицательное значение = накопление.
        """
        if not Config.GLASSNODE_API_KEY:
            return {}

        p = {"a": asset, "api_key": Config.GLASSNODE_API_KEY, "i": "24h", "limit": 30}

        data_7d  = await self._get("/distribution/supply_exchanges_percent", p | {"limit": 7})
        data_30d = await self._get("/distribution/supply_exchanges_percent", p | {"limit": 30})

        if not data_7d or not data_30d:
            return {}

        vals_7d  = [d.get("v", 0) for d in data_7d  if d.get("v") is not None]
        vals_30d = [d.get("v", 0) for d in data_30d if d.get("v") is not None]

        if len(vals_7d) < 2 or len(vals_30d) < 2:
            return {}

        change_7d  = vals_7d[-1]  - vals_7d[0]
        change_30d = vals_30d[-1] - vals_30d[0]

        return {
            "change_7d":  round(change_7d, 3),
            "change_30d": round(change_30d, 3),
            "current_pct": round(vals_7d[-1], 3),
            "signal": "outflow" if change_7d < -2 else ("inflow" if change_7d > 2 else "neutral"),
        }

    async def get_whale_transactions(self, asset: str) -> dict:
        """
        Транзакции > $100K — косвенный индикатор активности китов.
        """
        if not Config.GLASSNODE_API_KEY:
            return {}

        p = {"a": asset, "api_key": Config.GLASSNODE_API_KEY, "i": "24h", "limit": 14}
        data = await self._get("/transactions/count_greater_than_100k_usd", p)

        if not data:
            return {}

        vals = [d.get("v", 0) for d in data]
        avg  = sum(vals) / len(vals) if vals else 0
        last = vals[-1] if vals else 0
        chg  = (last - avg) / avg * 100 if avg > 0 else 0

        return {
            "last_24h":    last,
            "avg_14d":     round(avg, 1),
            "change_pct":  round(chg, 1),
            "spike":       chg > 80,
        }

    async def get_exchange_inflow_outflow(self, asset: str) -> dict:
        """Нетто-поток на/с бирж в USD"""
        if not Config.GLASSNODE_API_KEY:
            return {}

        p = {"a": asset, "api_key": Config.GLASSNODE_API_KEY, "i": "24h", "limit": 7}
        data = await self._get("/transactions/transfers_volume_exchanges_net", p)

        if not data:
            return {}

        vals = [d.get("v", 0) for d in data]
        net_7d = sum(vals)

        return {
            "net_7d_usd":  net_7d,
            "last_24h":    vals[-1] if vals else 0,
            "trend":       "outflow" if net_7d < 0 else "inflow",
        }


# ─── Deribit — опционы + borrow rate (бесплатно) ─────────────────────────────

class DeribitClient(BaseClient):
    """
    Опционный рынок — один из самых ранних и надёжных сигналов.
    Невозможно подделать — требует реальных денег.

    Call options accumulation перед пампом появляется за 5–14 дней.
    Borrow rate рост = дефицит токена для займа = накопление.
    """

    def __init__(self):
        super().__init__("https://www.deribit.com/api/v2", rate_limit=30)

    async def get_options_summary(self, currency: str = "BTC") -> dict:
        """
        Сводка по опционам: call/put ratio, IV skew.
        Высокий call/put ratio при нейтральной цене = накопление.
        """
        data = await self._get(
            "/public/get_book_summary_by_currency",
            {"currency": currency.upper(), "kind": "option"}
        )
        if not data:
            return {}

        result = data.get("result", [])
        calls = [i for i in result if "C" in i.get("instrument_name", "")]
        puts  = [i for i in result if "P" in i.get("instrument_name", "")]

        call_vol = sum(i.get("volume", 0) for i in calls)
        put_vol  = sum(i.get("volume", 0) for i in puts)
        ratio    = call_vol / put_vol if put_vol > 0 else 1

        # IV skew: если call IV > put IV → рынок ожидает рост
        call_ivs = [i.get("mark_iv", 0) for i in calls if i.get("mark_iv")]
        put_ivs  = [i.get("mark_iv", 0) for i in puts  if i.get("mark_iv")]

        avg_call_iv = sum(call_ivs) / len(call_ivs) if call_ivs else 0
        avg_put_iv  = sum(put_ivs)  / len(put_ivs)  if put_ivs  else 0
        iv_skew     = avg_call_iv - avg_put_iv

        return {
            "call_put_ratio": round(ratio, 2),
            "call_volume":    round(call_vol, 2),
            "put_volume":     round(put_vol, 2),
            "iv_skew":        round(iv_skew, 2),
            "signal": "bullish_options" if ratio > 1.5 and iv_skew > 5 else "neutral",
        }

    async def get_futures_basis(self, currency: str = "BTC") -> dict:
        """
        Basis = (futures_price - spot_price) / spot_price * 100 * (365/days).
        Аномально высокий basis (>20% ann) = институционалы делают cash-and-carry.
        Это сигнал за 7–21 день до движения.
        """
        data = await self._get(
            "/public/get_book_summary_by_currency",
            {"currency": currency.upper(), "kind": "future"}
        )
        if not data:
            return {}

        result = data.get("result", [])
        # Берём ближайший квартальный фьючерс
        quarterlies = [
            i for i in result
            if i.get("instrument_name", "").count("-") == 2
            and "PERPETUAL" not in i.get("instrument_name", "")
        ]

        if not quarterlies:
            return {}

        fut = quarterlies[0]
        mark_price = fut.get("mark_price", 0)
        spot_data  = await self._get(
            "/public/get_index_price",
            {"index_name": f"{currency.lower()}_usd"}
        )
        spot = spot_data.get("result", {}).get("index_price", mark_price)

        if not spot:
            return {}

        raw_basis_pct = (mark_price - spot) / spot * 100
        # Аннуализируем (примерно)
        ann_basis = raw_basis_pct * 4  # quarterly * 4

        return {
            "raw_basis_pct": round(raw_basis_pct, 3),
            "ann_basis_pct": round(ann_basis, 1),
            "signal": "institutional_carry" if ann_basis > 20 else "normal",
        }


# ─── Coinglass — OI, Funding, Liquidations ────────────────────────────────────

class CoinglassClient(BaseClient):
    """
    Лучший источник данных по деривативам.
    OI + нейтральный funding = умное позиционирование без перегрева.
    """

    def __init__(self):
        super().__init__("https://open-api.coinglass.com/public/v2", rate_limit=15)

    def _headers(self):
        return {"coinglassSecret": Config.COINGLASS_API_KEY}

    async def get_oi_and_funding(self, symbol: str) -> dict:
        if not Config.COINGLASS_API_KEY:
            return {}

        oi_data = await self._get(
            "/indicator/open_interest",
            {"symbol": symbol},
            self._headers()
        )
        fund_data = await self._get(
            "/indicator/funding_rate",
            {"symbol": symbol},
            self._headers()
        )

        result = {}

        if oi_data and oi_data.get("code") == "0":
            d = oi_data.get("data", {})
            result["oi_change_24h_pct"] = d.get("oiUsdPctChange", 0)
            result["oi_usd"] = d.get("oiUsd", 0)

        if fund_data and fund_data.get("code") == "0":
            rates = fund_data.get("data", [])
            if rates:
                avg = sum(r.get("fundingRate", 0) for r in rates) / len(rates)
                result["funding_rate_pct"] = round(avg * 100, 4)
                result["funding_signal"]   = self._funding_signal(avg)

        return result

    def _funding_signal(self, rate: float) -> str:
        p = rate * 100
        if p >  0.10: return "longs_overheated"
        if p >  0.05: return "longs_elevated"
        if p < -0.05: return "shorts_overheated"
        if p < -0.02: return "shorts_elevated"
        return "neutral"

    async def get_liquidation_heatmap(self, symbol: str) -> dict:
        """
        Карта ликвидаций — буквально карта куда маркетмейкер
        будет двигать цену для максимального profit.
        """
        if not Config.COINGLASS_API_KEY:
            return {}

        data = await self._get(
            "/indicator/liquidation_ex",
            {"symbol": symbol, "time": "24h"},
            self._headers()
        )
        if not data or data.get("code") != "0":
            return {}

        d = data.get("data", {})
        long_liq  = d.get("longLiquidationUsd", 0)
        short_liq = d.get("shortLiquidationUsd", 0)

        # Кластер шорт-ликвидаций выше текущей цены = цель для пампа
        return {
            "long_liq_24h":  long_liq,
            "short_liq_24h": short_liq,
            "short_squeeze_potential": short_liq > long_liq * 2,
        }


# ─── Arkham — умный триггер при score > 65 ────────────────────────────────────

class ArkhamClient(BaseClient):
    """
    Запускается ТОЛЬКО при score > 65.
    Не тратим лимиты API на все токены подряд.
    """

    def __init__(self):
        super().__init__("https://api.arkhamintelligence.com", rate_limit=10)
        self._analyzed: dict = {}  # symbol → timestamp

    def should_run(self, symbol: str, score: float) -> bool:
        if score < 65 or not Config.ARKHAM_API_KEY:
            return False
        last = self._analyzed.get(symbol, 0)
        return time.time() - last > 3600  # не чаще раза в час

    async def deep_analyze(self, token_address: str, symbol: str) -> dict:
        if not Config.ARKHAM_API_KEY:
            return {}

        self._analyzed[symbol] = time.time()
        headers = {"API-Key": Config.ARKHAM_API_KEY}
        since   = int(time.time()) - 7 * 86400

        withdrawals, deposits = await asyncio.gather(
            self._get("/transfers", {
                "base": token_address, "fromEntity": "exchange",
                "limit": 30, "timeGte": since,
                "sortKey": "usdValue", "sortDir": "desc",
            }, headers),
            self._get("/transfers", {
                "base": token_address, "toEntity": "exchange",
                "limit": 30, "timeGte": since,
                "sortKey": "usdValue", "sortDir": "desc",
            }, headers),
            return_exceptions=True
        )

        wdraw_list = withdrawals.get("transfers", []) if isinstance(withdrawals, dict) else []
        dep_list   = deposits.get("transfers", [])   if isinstance(deposits,   dict) else []

        whale_out = sum(t.get("unitValue", 0) for t in wdraw_list)
        exch_in   = sum(t.get("unitValue", 0) for t in dep_list)

        signals = []
        if whale_out > 500_000:
            signals.append(f"📤 Arkham: выведено с бирж ${whale_out/1e6:.2f}M за 7д")
        if exch_in > whale_out * 1.5:
            signals.append(f"⚠ Arkham: вводы на биржи ${exch_in/1e3:.0f}K > выводов — риск дампа")

        return {
            "whale_withdrawals_usd": whale_out,
            "exchange_inflows_usd":  exch_in,
            "signals":               signals,
        }


# ─── GeckoTerminal — DEX swaps (бесплатно) ────────────────────────────────────

class GeckoTerminalClient(BaseClient):
    """
    DEX активность опережает CEX на 1–6 часов.
    Используем как ранний сигнал, не как основной.
    """

    CHAINS = {"eth": "ethereum", "bsc": "bsc", "arb": "arbitrum", "base": "base", "sol": "solana"}

    def __init__(self):
        super().__init__(
            "https://api.geckoterminal.com/api/v2",
            rate_limit=28
        )

    async def get_pool_trades(self, chain: str, pool_address: str, min_usd: float = 10000) -> dict:
        data = await self._get(
            f"/networks/{chain}/pools/{pool_address}/trades",
            {"trade_volume_in_usd_greater_than": min_usd},
            {"Accept": "application/json;version=20230302"}
        )
        trades = data.get("data", [])
        now    = time.time()

        buy_vol  = 0.0
        sell_vol = 0.0
        large    = 0

        for t in trades:
            a   = t.get("attributes", {})
            usd = float(a.get("volume_in_usd", 0) or 0)
            ts  = _parse_ts(a.get("block_timestamp", ""))
            if now - ts > 3600:
                continue
            if a.get("kind") == "buy":
                buy_vol += usd
            else:
                sell_vol += usd
            if usd > 25000:
                large += 1

        total = buy_vol + sell_vol
        return {
            "dex_buy_vol_1h":    buy_vol,
            "dex_sell_vol_1h":   sell_vol,
            "dex_buy_ratio_1h":  buy_vol / total if total > 0 else 0.5,
            "large_swaps_count": large,
        }

    async def search_pools(self, symbol: str, chain: str = "eth") -> list[str]:
        """Находит адреса пулов для токена"""
        data = await self._get(
            "/search/pools",
            {"query": symbol, "network": chain, "page": 1},
            {"Accept": "application/json;version=20230302"}
        )
        pools = []
        for p in data.get("data", [])[:3]:
            addr = p.get("id", "").split("_")[-1]
            if addr:
                pools.append(addr)
        return pools

    async def get_trending_anomalies(self) -> list[dict]:
        """
        Сканирует trending пулы на аномальный buy ratio.
        DEX активность часто опережает CEX на 1–6 часов.
        """
        results = []
        for chain_id in ["eth", "bsc", "arb", "base", "sol"]:
            data = await self._get(
                f"/networks/{chain_id}/trending_pools",
                headers={"Accept": "application/json;version=20230302"}
            )
            for p in data.get("data", [])[:20]:
                a     = p.get("attributes", {})
                buys  = a.get("transactions", {}).get("h1", {}).get("buys",  0)
                sells = a.get("transactions", {}).get("h1", {}).get("sells", 0)
                vol   = float(a.get("volume_usd", {}).get("h24", 0) or 0)
                total = buys + sells
                if total == 0 or vol < 50_000:
                    continue
                ratio = buys / total
                if ratio > 0.72:
                    results.append({
                        "name":    a.get("name", ""),
                        "chain":   chain_id,
                        "volume":  vol,
                        "buy_ratio": round(ratio, 2),
                        "price_change_1h": float(
                            a.get("price_change_percentage", {}).get("h1", 0) or 0
                        ),
                        "pool_address": p.get("id", "").split("_")[-1],
                    })
        return sorted(results, key=lambda x: x["volume"], reverse=True)[:20]


# ─── Aggregator ───────────────────────────────────────────────────────────────

class DataAggregator:
    """
    Собирает данные из всех источников в OnchainData.
    Кэширует на 5 минут чтобы не расходовать API лимиты.
    """

    def __init__(self):
        self.glassnode = GlassnodeClient()
        self.deribit   = DeribitClient()
        self.coinglass = CoinglassClient()
        self.arkham    = ArkhamClient()
        self.gecko     = GeckoTerminalClient()

        self._cache: dict = {}
        self._cache_ts: dict = {}
        self._cache_ttl = 300  # 5 минут

    async def get_onchain(
        self,
        symbol: str,
        token_address: str = "",
        chain: str = "eth",
        current_score: float = 0,
    ) -> OnchainData:
        """Главный метод — возвращает OnchainData для символа"""

        now = time.time()
        if symbol in self._cache and now - self._cache_ts.get(symbol, 0) < self._cache_ttl:
            od = self._cache[symbol]
            # Arkham запускаем при новом высоком score даже если есть кэш
            if self.arkham.should_run(symbol, current_score) and token_address:
                await self._enrich_arkham(od, token_address, symbol)
            return od

        asset = symbol.replace("/USDT", "").replace("USDT", "")
        od    = OnchainData(symbol=symbol, timestamp=now)

        # Параллельные запросы
        tasks = [
            self.glassnode.get_exchange_supply_change(asset),
            self.glassnode.get_whale_transactions(asset),
            self.coinglass.get_oi_and_funding(asset),
            self.coinglass.get_liquidation_heatmap(asset),
            self.deribit.get_futures_basis(asset),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        supply_data, whale_data, oi_data, liq_data, basis_data = results

        if isinstance(supply_data, dict):
            od.exchange_supply_change_7d  = supply_data.get("change_7d", 0)
            od.exchange_supply_change_30d = supply_data.get("change_30d", 0)

        if isinstance(whale_data, dict) and whale_data.get("spike"):
            od.smart_money_buyers = int(whale_data.get("last_24h", 0) / 10)

        if isinstance(oi_data, dict):
            pass  # OI идёт напрямую в ScoreInput

        if isinstance(basis_data, dict):
            od.derivatives_basis_pct = basis_data.get("ann_basis_pct", 0)

        # DEX данные
        if token_address:
            pools = await self.gecko.search_pools(asset, chain)
            if pools:
                dex = await self.gecko.get_pool_trades(chain, pools[0])
                od.dex_buy_vol_1h   = dex.get("dex_buy_vol_1h", 0)
                od.dex_buy_ratio_1h = dex.get("dex_buy_ratio_1h", 0.5)

        # Arkham — только при высоком score
        if self.arkham.should_run(symbol, current_score) and token_address:
            await self._enrich_arkham(od, token_address, symbol)

        self._cache[symbol]    = od
        self._cache_ts[symbol] = now
        return od

    async def _enrich_arkham(self, od: OnchainData, token_address: str, symbol: str):
        arkham = await self.arkham.deep_analyze(token_address, symbol)
        if arkham:
            od.whale_withdrawals_7d_usd = arkham.get("whale_withdrawals_usd", 0)
            od.exchange_inflows_7d_usd  = arkham.get("exchange_inflows_usd", 0)

    async def get_oi_funding(self, symbol: str) -> dict:
        """Быстрый запрос OI+funding для скоринга"""
        asset = symbol.replace("/USDT","").replace("USDT","")
        return await self.coinglass.get_oi_and_funding(asset)

    async def get_trending_dex(self) -> list[dict]:
        return await self.gecko.get_trending_anomalies()


def _parse_ts(ts_str: str) -> float:
    if not ts_str:
        return 0
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0
