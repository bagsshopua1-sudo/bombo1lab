"""
backtest/backtester.py — Бэктест системы скоринга

Логика:
  1. Скачиваем исторические данные с Binance (бесплатно, до 1000 свечей)
  2. Находим реальные памп-события (рост > 30% за 48ч)
  3. Симулируем работу детекторов за 7 дней ДО пампа
  4. Считаем: сколько памп-событий система поймала бы заранее

Это позволяет понять реальную точность системы
до запуска в production.
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from loguru import logger

from core.config import Trade, is_top_token
from detectors.cvd import CVDCalculator
from scoring.engine import ManipulationScorer, ScoreInput
from core.config import OnchainData


# ─── Модели ───────────────────────────────────────────────────────────────────

@dataclass
class Candle:
    timestamp: float
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float
    quote_vol: float   # объём в USDT


@dataclass
class PumpEvent:
    symbol:      str
    started_at:  float
    price_start: float
    price_peak:  float
    peak_at:     float
    gain_pct:    float
    duration_h:  float


@dataclass
class BacktestResult:
    symbol:          str
    pump_event:      PumpEvent
    # Что видела система за 7 дней до пампа
    max_score_7d:    float
    max_score_3d:    float
    max_score_1d:    float
    score_at_peak:   float
    # Поймала ли система
    detected_50:     bool   # score > 50 за 7д до пампа
    detected_70:     bool   # score > 70 за 7д до пампа
    hours_before:    float  # за сколько часов до пампа score превысил 50
    # Компоненты
    cvd_max:         float  = 0
    signals:         list   = field(default_factory=list)


@dataclass
class BacktestSummary:
    total_pumps:       int
    detected_50:       int    # обнаружено при пороге 50
    detected_70:       int    # обнаружено при пороге 70
    precision_50:      float  # % правильных сигналов (score>50 → был памп)
    recall_50:         float  # % пампов которые система нашла
    avg_hours_before:  float  # среднее за сколько часов до пампа
    false_positives:   int    # score>50 но пампа не было
    results:           list[BacktestResult] = field(default_factory=list)


# ─── Загрузка исторических данных ─────────────────────────────────────────────

class BinanceHistoryClient:
    """Загружает исторические свечи с Binance (бесплатно)"""

    BASE = "https://api.binance.com"

    async def get_candles(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 500,
        end_time: int = None,
    ) -> list[Candle]:
        """
        Получает свечи.
        interval: 1m, 5m, 15m, 1h, 4h, 1d
        limit: максимум 1000
        """
        params = {
            "symbol":   symbol.replace("/USDT", "USDT").replace("-USDT", "USDT"),
            "interval": interval,
            "limit":    min(limit, 1000),
        }
        if end_time:
            params["endTime"] = end_time

        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{self.BASE}/api/v3/klines",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()

            return [
                Candle(
                    timestamp = float(c[0]) / 1000,
                    open      = float(c[1]),
                    high      = float(c[2]),
                    low       = float(c[3]),
                    close     = float(c[4]),
                    volume    = float(c[5]),
                    quote_vol = float(c[7]),
                )
                for c in data
            ]
        except Exception as e:
            logger.debug(f"Candles {symbol}: {e}")
            return []

    async def get_all_usdt_symbols(self, min_volume: float = 500_000) -> list[str]:
        """Получает все USDT-пары с минимальным объёмом"""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{self.BASE}/api/v3/ticker/24hr",
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    data = await r.json()

            symbols = []
            for item in data:
                sym = item.get("symbol", "")
                vol = float(item.get("quoteVolume", 0))
                if sym.endswith("USDT") and vol >= min_volume:
                    base = sym[:-4]
                    if not is_top_token(base):
                        symbols.append(sym)

            logger.info(f"Найдено {len(symbols)} символов с объёмом > ${min_volume/1e3:.0f}K")
            return symbols
        except Exception as e:
            logger.error(f"Symbols fetch: {e}")
            return []


# ─── Поиск памп-событий ───────────────────────────────────────────────────────

class PumpFinder:
    """Находит памп-события в исторических данных"""

    def find_pumps(
        self,
        candles: list[Candle],
        min_gain_pct: float = 30.0,
        window_hours: int   = 48,
    ) -> list[PumpEvent]:
        """
        Ищет периоды где цена выросла на min_gain_pct за window_hours часов.
        """
        if len(candles) < window_hours:
            return []

        pumps = []
        i     = 0

        while i < len(candles) - window_hours:
            start_price = candles[i].close
            if start_price <= 0:
                i += 1
                continue

            # Смотрим вперёд на window_hours свечей
            window = candles[i:i + window_hours]
            peak   = max(window, key=lambda c: c.high)
            gain   = (peak.high - start_price) / start_price * 100

            if gain >= min_gain_pct:
                pump = PumpEvent(
                    symbol      = "",
                    started_at  = candles[i].timestamp,
                    price_start = start_price,
                    price_peak  = peak.high,
                    peak_at     = peak.timestamp,
                    gain_pct    = round(gain, 1),
                    duration_h  = (peak.timestamp - candles[i].timestamp) / 3600,
                )
                pumps.append(pump)
                # Пропускаем вперёд чтобы не дублировать
                i += window_hours
            else:
                i += 1

        return pumps


# ─── Симуляция скоринга на истории ────────────────────────────────────────────

class HistoricalScorer:
    """
    Симулирует работу CVD детектора на исторических свечах.

    Ограничение: полный скоринг требует ончейн данных которых
    нет в истории. Поэтому бэктест симулирует только:
    - CVD дивергенцию (из OHLCV данных)
    - Объёмные аномалии
    - Ценовое поведение

    Это ~30% от полного скора. Но позволяет проверить
    работает ли базовая логика.
    """

    def __init__(self):
        self.scorer = ManipulationScorer()

    def score_period(
        self,
        candles: list[Candle],
        window_hours: int = 24,
    ) -> list[dict]:
        """
        Считает скор для каждой свечи в периоде.
        Возвращает список {timestamp, score, cvd_divergence}.
        """
        results = []
        cvd_calc = CVDCalculator(window_sec=window_hours * 3600)

        for i, candle in enumerate(candles):
            # Синтетические сделки из OHLCV
            # Логика: если close > open → больше buy volume
            buy_ratio = (candle.close - candle.low) / (candle.high - candle.low + 1e-10)
            buy_vol   = candle.quote_vol * buy_ratio
            sell_vol  = candle.quote_vol * (1 - buy_ratio)

            # Добавляем как агрегированные сделки
            cvd_calc.feed_trade(Trade(
                symbol    = "",
                timestamp = candle.timestamp,
                price     = candle.close,
                qty       = buy_vol / max(candle.close, 1e-10),
                side      = "buy",
            ))
            cvd_calc.feed_trade(Trade(
                symbol    = "",
                timestamp = candle.timestamp,
                price     = candle.close,
                qty       = sell_vol / max(candle.close, 1e-10),
                side      = "sell",
            ))

            if i < 6:   # ждём накопления данных
                continue

            cvd_div   = cvd_calc.get_divergence_score()
            lt_ratio  = cvd_calc.get_large_trade_ratio(
                threshold_usd=candle.quote_vol * 0.1  # 10% от среднего объёма
            )

            # Объёмная аномалия
            recent_vols = [c.quote_vol for c in candles[max(0,i-20):i]]
            avg_vol     = sum(recent_vols) / len(recent_vols) if recent_vols else 0
            vol_ratio   = candle.quote_vol / avg_vol if avg_vol > 0 else 1

            # Упрощённый ончейн (нет реальных данных — используем прокси)
            # Если объём растёт при боковой цене → признак накопления
            price_change = abs(candle.close - candles[max(0,i-6)].close) / candles[max(0,i-6)].close * 100
            volume_up    = vol_ratio > 1.5

            onchain = OnchainData(
                symbol    = "",
                timestamp = candle.timestamp,
                # Прокси для smart money: рост объёма при боковой цене
                smart_money_net_flow_14d = buy_vol * 0.1 if (volume_up and price_change < 2) else 0,
                smart_money_buyers       = 3 if (volume_up and price_change < 1) else 0,
            )

            inp = ScoreInput(
                symbol            = "",
                timestamp         = candle.timestamp,
                onchain           = onchain,
                cvd_divergence    = cvd_div,
                large_trade_ratio = lt_ratio,
            )

            score = self.scorer.calculate(inp)

            results.append({
                "timestamp":      candle.timestamp,
                "score":          score.total_score,
                "cvd_divergence": cvd_div.get("score", 0),
                "cvd_type":       cvd_div.get("divergence", "none"),
                "vol_ratio":      round(vol_ratio, 2),
                "price":          candle.close,
            })

        return results


# ─── Основной бэктест ──────────────────────────────────────────────────────────

class Backtester:
    """
    Запускает полный бэктест:
    1. Скачивает историю для N токенов
    2. Находит памп-события
    3. Симулирует скоринг до каждого пампа
    4. Считает метрики точности
    """

    def __init__(self):
        self.client  = BinanceHistoryClient()
        self.finder  = PumpFinder()
        self.scorer  = HistoricalScorer()

    async def run(
        self,
        symbols:       list[str] = None,
        min_gain_pct:  float     = 30.0,
        lookback_days: int       = 180,
        max_symbols:   int       = 50,
        alert_threshold: float   = 50.0,
    ) -> BacktestSummary:
        """
        Запускает бэктест.

        symbols:       список токенов (None = берём топ по объёму автоматически)
        min_gain_pct:  минимальный рост для считания пампом (30%)
        lookback_days: период истории (180 дней)
        max_symbols:   максимум токенов для анализа
        alert_threshold: порог скора для "обнаружения"
        """
        if not symbols:
            logger.info("Получаем список токенов с Binance...")
            all_syms = await self.client.get_all_usdt_symbols(min_volume=200_000)
            symbols  = all_syms[:max_symbols]

        logger.info(f"Бэктест: {len(symbols)} токенов, мин. памп {min_gain_pct}%, {lookback_days}д истории")

        all_results  = []
        all_pumps    = 0
        false_pos    = 0

        for i, symbol in enumerate(symbols):
            logger.info(f"[{i+1}/{len(symbols)}] {symbol}")
            try:
                results = await self._backtest_symbol(
                    symbol, min_gain_pct, lookback_days, alert_threshold
                )
                all_results.extend(results)
                all_pumps += len(results)
            except Exception as e:
                logger.debug(f"Backtest error {symbol}: {e}")

            # Пауза чтобы не забанили
            if (i + 1) % 10 == 0:
                await asyncio.sleep(2)
            else:
                await asyncio.sleep(0.3)

        # Считаем метрики
        detected_50 = sum(1 for r in all_results if r.detected_50)
        detected_70 = sum(1 for r in all_results if r.detected_70)
        hours_list  = [r.hours_before for r in all_results if r.hours_before > 0]
        avg_hours   = sum(hours_list) / len(hours_list) if hours_list else 0

        recall_50 = detected_50 / all_pumps * 100 if all_pumps > 0 else 0

        summary = BacktestSummary(
            total_pumps     = all_pumps,
            detected_50     = detected_50,
            detected_70     = detected_70,
            precision_50    = 0,   # требует подсчёта ложных сигналов
            recall_50       = round(recall_50, 1),
            avg_hours_before= round(avg_hours, 1),
            false_positives  = false_pos,
            results         = all_results,
        )

        self._print_summary(summary)
        return summary

    async def _backtest_symbol(
        self,
        symbol:          str,
        min_gain_pct:    float,
        lookback_days:   int,
        alert_threshold: float,
    ) -> list[BacktestResult]:
        """Бэктест одного токена"""

        # Загружаем историю
        candles = await self.client.get_candles(
            symbol   = symbol,
            interval = "1h",
            limit    = min(lookback_days * 24, 1000),
        )

        if len(candles) < 48:
            return []

        # Находим памп-события
        pumps = self.finder.find_pumps(candles, min_gain_pct=min_gain_pct)
        if not pumps:
            return []

        # Считаем скоры для всего периода
        scores_by_ts = {}
        scored = self.scorer.score_period(candles, window_hours=24)
        for s in scored:
            scores_by_ts[s["timestamp"]] = s

        results = []
        for pump in pumps:
            pump.symbol = symbol

            # Берём скоры за 7 дней ДО пампа
            pump_ts   = pump.started_at
            window_7d = [
                s for s in scored
                if pump_ts - 7 * 3600 * 24 <= s["timestamp"] <= pump_ts
            ]
            window_3d = [s for s in window_7d if s["timestamp"] >= pump_ts - 3*3600*24]
            window_1d = [s for s in window_7d if s["timestamp"] >= pump_ts - 1*3600*24]

            max_7d = max((s["score"] for s in window_7d), default=0)
            max_3d = max((s["score"] for s in window_3d), default=0)
            max_1d = max((s["score"] for s in window_1d), default=0)

            # За сколько часов до пампа score превысил порог
            hours_before = 0
            for s in sorted(window_7d, key=lambda x: x["timestamp"]):
                if s["score"] >= alert_threshold:
                    hours_before = (pump_ts - s["timestamp"]) / 3600
                    break

            result = BacktestResult(
                symbol       = symbol,
                pump_event   = pump,
                max_score_7d = round(max_7d, 1),
                max_score_3d = round(max_3d, 1),
                max_score_1d = round(max_1d, 1),
                score_at_peak= 0,
                detected_50  = max_7d >= 50,
                detected_70  = max_7d >= 70,
                hours_before = round(hours_before, 1),
                cvd_max      = round(max(s.get("cvd_divergence",0) for s in window_7d) if window_7d else 0, 1),
            )
            results.append(result)

        return results

    def _print_summary(self, s: BacktestSummary):
        logger.info("=" * 50)
        logger.info("  РЕЗУЛЬТАТЫ БЭКТЕСТА")
        logger.info("=" * 50)
        logger.info(f"Пампов найдено:        {s.total_pumps}")
        logger.info(f"Обнаружено (score>50): {s.detected_50} ({s.recall_50:.1f}%)")
        logger.info(f"Обнаружено (score>70): {s.detected_70}")
        logger.info(f"Среднее упреждение:    {s.avg_hours_before:.1f}ч до пампа")
        logger.info("=" * 50)

        # Топ результаты
        top = sorted(s.results, key=lambda r: r.pump_event.gain_pct, reverse=True)[:10]
        logger.info("Топ пампов:")
        for r in top:
            status = "✓" if r.detected_50 else "✗"
            logger.info(
                f"  {status} {r.symbol:<14} "
                f"+{r.pump_event.gain_pct:.0f}% | "
                f"score_7d={r.max_score_7d} | "
                f"за {r.hours_before:.0f}ч"
            )
