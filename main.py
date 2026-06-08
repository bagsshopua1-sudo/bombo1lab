"""
main.py — Главный оркестратор финальной версии

Архитектура (от важного к менее важному):
  1. Exchange Supply Change      — первопричина, 25 баллов
  2. Smart Money Accumulation    — первопричина, 25 баллов
  3. Real Free Float             — структурное условие, 10 баллов
  4. CVD Divergence              — подтверждение, 15 баллов
  5. Wallet Dormancy Wake        — подтверждение, 10 баллов
  6. Derivatives Basis/Borrow    — подтверждение, 10 баллов
  7. Iceberg + Absorption        — второстепенное, 5 баллов

Удалено: social score, wash trading, layering, long/short ratio
"""
import asyncio
import argparse
import os
import time
from collections import defaultdict
from typing import Optional

from loguru import logger
from dotenv import load_dotenv
load_dotenv()

from core.config import (
    Config, Trade, FuturesData, ManipulationScore,
    is_top_token, score_to_level, score_to_probability
)
from core.exchanges import MultiExchangeManager
from core.ai_analyst import AIAnalyst
from core.proxy_manager import ProxyManager
from onchain.clients import DataAggregator
from detectors.cvd import CVDCalculator, IcebergAbsorptionDetector
from scoring.engine import ManipulationScorer, ScoreInput


# ─── Состояние одного токена ──────────────────────────────────────────────────

class TokenState:
    def __init__(self, symbol: str):
        self.symbol     = symbol
        self.cvd        = CVDCalculator(window_sec=7200)
        self.detector   = IcebergAbsorptionDetector()
        self.last_price = 0.0
        self.futures:   dict = {}   # exchange → FuturesData
        self.last_score: Optional[ManipulationScore] = None
        self.scored_at:  float = 0

    def feed_trade(self, t: Trade):
        self.cvd.feed_trade(t)
        self.detector.feed_trade(t)
        self.last_price = t.price

    def feed_futures(self, f: FuturesData):
        self.futures[f.exchange] = f

    def agg_futures(self) -> dict:
        oi   = sum(f.open_interest or 0 for f in self.futures.values())
        rates = [f.funding_rate for f in self.futures.values() if f.funding_rate is not None]
        avg_f = sum(rates) / len(rates) if rates else 0
        return {"oi": oi, "funding": avg_f}


# ─── Основная система ─────────────────────────────────────────────────────────

class MonitorSystem:

    def __init__(self, demo: bool = False, no_proxy: bool = False):
        self.demo      = demo
        self.tokens:   dict[str, TokenState] = {}
        self._lock     = asyncio.Lock()
        self.scorer    = ManipulationScorer()
        self.ai        = AIAnalyst()
        self.aggregator = DataAggregator()
        self._alert_cache: dict[str, float] = {}

        # Прокси
        proxy_list = []
        if not no_proxy:
            raw = Config.PROXY_LIST
            if raw:
                proxy_list = [p.strip() for p in raw.split(",") if p.strip()]
        self.proxy_manager = ProxyManager(proxy_list)

        # Telegram бот
        self.bot = None
        if Config.BOT_TOKEN:
            try:
                from bot.telegram_bot import CryptoMonitorBot
                self.bot = CryptoMonitorBot()
                self.bot.set_system(self)
                logger.info("Telegram бот инициализирован")
            except Exception as e:
                logger.warning(f"Бот не запущен: {e}")

        # Мультибиржевой менеджер
        self.exchange_mgr = MultiExchangeManager(
            enabled_exchanges = Config.ENABLED_EXCHANGES,
            proxy_list        = proxy_list,
            min_volume        = Config.MIN_VOLUME_24H,
            on_trade          = self._on_trade,
            on_futures        = self._on_futures,
        )

        # Статистика
        self.stats = {
            "started": time.time(), "trades": 0,
            "tokens": 0, "alerts": 0,
        }

    # ─── Обработчики данных ───────────────────────────────────────────────────

    async def _get_token(self, symbol: str) -> TokenState:
        if symbol not in self.tokens:
            async with self._lock:
                if symbol not in self.tokens:
                    self.tokens[symbol] = TokenState(symbol)
                    self.stats["tokens"] = len(self.tokens)
        return self.tokens[symbol]

    async def _on_trade(self, trade: Trade):
        self.stats["trades"] += 1
        tok = await self._get_token(trade.symbol)
        tok.feed_trade(trade)

    async def _on_futures(self, fut: FuturesData):
        tok = await self._get_token(fut.symbol)
        tok.feed_futures(fut)

    # ─── Scoring ─────────────────────────────────────────────────────────────

    async def score_token(self, symbol: str) -> Optional[ManipulationScore]:
        """Считает скор для одного токена"""
        if is_top_token(symbol):
            return None

        tok = self.tokens.get(symbol)
        if not tok or not tok.last_price:
            return None

        try:
            # Ончейн данные (кэшируются 5 мин)
            onchain = await self.aggregator.get_onchain(
                symbol        = symbol,
                current_score = tok.last_score.total_score if tok.last_score else 0,
            )

            # OI + Funding
            fut = tok.agg_futures()
            oi_chg     = 0.0
            fund_sig   = "neutral"
            fund_rate  = fut["funding"]

            if abs(fund_rate) > 0:
                fund_sig = _funding_signal(fund_rate)

            inp = ScoreInput(
                symbol             = symbol,
                timestamp          = time.time(),
                onchain            = onchain,
                cvd_divergence     = tok.cvd.get_divergence_score(),
                large_trade_ratio  = tok.cvd.get_large_trade_ratio(),
                iceberg_count      = tok.detector.get_iceberg_count(),
                absorption_ratio   = tok.detector.get_absorption_ratio(),
                defended_levels    = tok.detector.get_defended_levels_count(),
                oi_change_24h_pct  = oi_chg,
                funding_rate       = fund_rate * 100,
                funding_signal     = fund_sig,
            )

            score = self.scorer.calculate(inp)
            tok.last_score = score
            tok.scored_at  = time.time()
            return score

        except Exception as e:
            logger.debug(f"Score error {symbol}: {e}")
            return None

    async def _scoring_loop(self):
        interval = Config.SCORING_INTERVAL
        workers  = Config.SCORING_WORKERS

        while True:
            await asyncio.sleep(interval)
            items = list(self.tokens.items())
            logger.info(f"Scoring {len(items)} токенов...")

            scores = []
            for i in range(0, len(items), workers):
                batch   = items[i:i + workers]
                results = await asyncio.gather(*[
                    self.score_token(s) for s, _ in batch
                ])
                scores.extend([r for r in results if r])

            # Алерты
            now     = time.time()
            to_send = []
            for s in scores:
                if s.total_score < Config.SCORE_ALERT:
                    continue
                last = self._alert_cache.get(s.symbol, 0)
                if now - last < Config.ALERT_COOLDOWN:
                    continue
                self._alert_cache[s.symbol] = now
                self.stats["alerts"] += 1
                to_send.append(s)

            if to_send and self.bot:
                await self.bot.send_alerts(to_send)

    # ─── Публичные методы (для бота) ─────────────────────────────────────────

    async def analyze(self, symbol: str) -> ManipulationScore:
        """Анализ по запросу из бота"""
        score = await self.score_token(symbol)
        if score:
            return score
        return ManipulationScore(
            symbol=symbol, timestamp=time.time(),
            level="NORMAL", signals=["Нет данных — символ ещё не отслеживается"]
        )

    def get_top(self, n: int = 10) -> list[ManipulationScore]:
        """Топ-N токенов по score (только малые)"""
        scores = [
            t.last_score for t in self.tokens.values()
            if t.last_score and t.last_score.total_score > 0
            and not is_top_token(t.symbol)
        ]
        return sorted(scores, key=lambda s: s.total_score, reverse=True)[:n]

    def get_stats(self) -> dict:
        up = round((time.time() - self.stats["started"]) / 3600, 1)
        return {
            **self.stats,
            "uptime_h":  up,
            "proxy":     self.proxy_manager.status(),
            "exchanges": self.exchange_mgr.stats,
        }

    # ─── Запуск ───────────────────────────────────────────────────────────────

    async def _stats_loop(self):
        while True:
            await asyncio.sleep(300)
            s = self.get_stats()
            logger.info(
                f"STATS uptime={s['uptime_h']}ч | tokens={s['tokens']} | "
                f"trades={s['trades']:,} | alerts={s['alerts']}"
            )

    async def _demo_loop(self):
        import random
        syms   = ["WIF/USDT","PEPE/USDT","BONK/USDT","JUP/USDT","JTO/USDT",
                  "PYTH/USDT","TNSR/USDT","W/USDT","SAGA/USDT","STRK/USDT"]
        prices = {s: random.uniform(0.001, 5) for s in syms}

        while True:
            await asyncio.sleep(0.08)
            sym = random.choice(syms)
            prices[sym] *= 1 + (random.random() - 0.499) * 0.0008
            await self._on_trade(Trade(
                symbol    = sym,
                timestamp = time.time(),
                price     = prices[sym],
                qty       = random.random() * 1000,
                side      = "sell" if random.random() > 0.51 else "buy",
                exchange  = random.choice(["binance","bybit","okx","gate"]),
                market    = random.choice(["spot","futures"]),
            ))

    async def run(self):
        logger.info("=" * 48)
        logger.info("   CRYPTO MANIPULATION MONITOR — FINAL")
        logger.info("=" * 48)

        await self.proxy_manager.check_all()

        tasks = []
        if self.demo:
            logger.info("ДЕМО режим")
            tasks.append(asyncio.create_task(self._demo_loop()))
        else:
            tasks.append(asyncio.create_task(self.exchange_mgr.run()))

        tasks.append(asyncio.create_task(self._scoring_loop()))
        tasks.append(asyncio.create_task(self._stats_loop()))

        if self.bot:
            tasks.append(asyncio.create_task(self.bot.run()))

        logger.info("Запущено. Ctrl+C для остановки.")
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            logger.info("Остановка.")


def _funding_signal(rate: float) -> str:
    p = rate * 100
    if p >  0.10: return "longs_overheated"
    if p >  0.05: return "longs_elevated"
    if p < -0.05: return "shorts_overheated"
    if p < -0.02: return "shorts_elevated"
    return "neutral"


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--demo",     action="store_true")
    p.add_argument("--no-proxy", action="store_true")
    args = p.parse_args()
    await MonitorSystem(demo=args.demo, no_proxy=args.no_proxy).run()


if __name__ == "__main__":
    asyncio.run(main())


# ─── Интеграция истории и фильтра объёма ─────────────────────────────────────
# Добавляется в MonitorSystem.__init__ и _scoring_loop

async def _init_history_and_volume(system):
    """Инициализирует историю и фильтр объёма"""
    from core.history import init_db
    from core.volume_filter import VolumeFilter
    init_db()
    vf = VolumeFilter(min_volume_usd=system.__class__.__mro__[0] and 100_000)
    proxy = system.proxy_manager.get()
    await vf.refresh(proxy)
    return vf


# ─── Методы для аналитики (добавляются в MonitorSystem) ──────────────────────

    async def get_potential(self, symbol: str):
        from analytics.scores import ThreeScoreEngine
        from analytics.potential_scanner import PotentialScanner, TokenPotential
        from core.config import ManipulationScore
        score = await self.analyze(symbol)
        od    = await self.aggregator.get_onchain(symbol, current_score=score.total_score)
        ex    = self.get_exchange_analysis(symbol)
        wdata = {}
        scanner = PotentialScanner()
        return scanner.evaluate(score, od, ex, wdata)

    async def get_top_potential(self, n: int = 10):
        from analytics.potential_scanner import PotentialScanner, rank_tokens
        scanner   = PotentialScanner()
        items     = list(self.tokens.items())
        potentials = []
        for sym, tok in items:
            if not tok.last_score or tok.last_score.total_score < 25:
                continue
            try:
                p = await self.get_potential(sym)
                potentials.append(p)
            except Exception:
                pass
        return rank_tokens(potentials)[:n]

    def get_exchange_analysis(self, symbol: str):
        tok = self.tokens.get(symbol)
        if not tok:
            return None
        if hasattr(self, '_ex_analyzer'):
            return self._ex_analyzer.analyze(symbol)
        return None
