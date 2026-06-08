"""
analytics/exchange_analyzer.py — Анализ бирж

Отвечает на вопросы:
1. На какой бирже начинается движение раньше других?
2. Где аномально высокий объём до анонса на других биржах?
3. Есть ли расхождение цены между биржами?
4. Где идут агрессивные продажи (слив)?
5. Координация ли это между биржами?
"""
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from core.config import Trade


@dataclass
class ExchangeMetrics:
    exchange:      str
    symbol:        str
    timestamp:     float

    price:         float = 0.0
    volume_1h:     float = 0.0
    buy_vol_1h:    float = 0.0
    sell_vol_1h:   float = 0.0
    trade_count:   int   = 0
    avg_trade_size:float = 0.0

    # Относительные метрики
    vol_rank:      int   = 0      # ранг по объёму среди бирж
    price_vs_avg:  float = 0.0   # % отклонение от средней цены
    cvd_1h:        float = 0.0   # кумулятивная дельта за час


@dataclass
class ExchangeAnalysis:
    """Результат анализа бирж для одного токена"""
    symbol:    str
    timestamp: float

    # Ведущая биржа (где началось движение раньше)
    leading_exchange:     Optional[str] = None
    leading_reason:       str = ""

    # Расхождение цен
    price_divergence_pct: float = 0.0   # макс расхождение между биржами
    highest_price_ex:     str   = ""
    lowest_price_ex:      str   = ""

    # Аномальный объём
    volume_anomaly_ex:    Optional[str] = None  # биржа с аномальным объёмом
    volume_ratio:         float = 0.0    # во сколько раз выше нормы

    # Где активнее покупают / продают
    pump_exchange:        Optional[str] = None  # где больше buy pressure
    dump_exchange:        Optional[str] = None  # где больше sell pressure

    # Координация между биржами
    coordinated_move:     bool  = False
    coordination_score:   float = 0.0

    # Кластер ликвидаций шортов
    short_liq_cluster_above: bool = False
    short_liq_usd:           float = 0.0

    # Все метрики по биржам
    by_exchange: dict = field(default_factory=dict)

    # Сигналы
    signals: list = field(default_factory=list)


class ExchangeAnalyzer:
    """
    Анализирует поведение токена на разных биржах.

    Ключевые находки из исследования:
    - Gate.io / MEXC часто опережают Binance на 2–6 часов
    - Аномальный объём на малой бирже до листинга на Binance — сильный сигнал
    - Расхождение цены > 0.5% без арбитража = что-то блокирует нормальный поток
    - Агрессивные продажи на одной бирже при росте на других = координированный слив
    """

    def __init__(self):
        # symbol → exchange → deque of Trade
        self._trades: dict = defaultdict(lambda: defaultdict(lambda: deque(maxlen=5000)))
        # symbol → exchange → deque of price
        self._prices: dict = defaultdict(lambda: defaultdict(lambda: deque(maxlen=100)))
        # Базовый объём для аномалий (rolling average)
        self._vol_baseline: dict = defaultdict(lambda: defaultdict(float))

    def feed_trade(self, trade: Trade):
        sym  = trade.symbol
        exch = trade.exchange
        self._trades[sym][exch].append(trade)
        self._prices[sym][exch].append((trade.timestamp, trade.price))

    def analyze(self, symbol: str) -> ExchangeAnalysis:
        result = ExchangeAnalysis(symbol=symbol, timestamp=time.time())

        exchanges = list(self._trades[symbol].keys())
        if len(exchanges) < 2:
            return result

        now = time.time()
        metrics: dict[str, ExchangeMetrics] = {}

        # ── Считаем метрики по каждой бирже ──────────────────────────────────
        for exch in exchanges:
            trades_1h = [
                t for t in self._trades[symbol][exch]
                if now - t.timestamp < 3600
            ]
            if not trades_1h:
                continue

            buy_vol  = sum(t.usd_volume for t in trades_1h if t.side == "buy")
            sell_vol = sum(t.usd_volume for t in trades_1h if t.side == "sell")
            total    = buy_vol + sell_vol

            m = ExchangeMetrics(
                exchange       = exch,
                symbol         = symbol,
                timestamp      = now,
                price          = trades_1h[-1].price,
                volume_1h      = total,
                buy_vol_1h     = buy_vol,
                sell_vol_1h    = sell_vol,
                trade_count    = len(trades_1h),
                avg_trade_size = total / len(trades_1h) if trades_1h else 0,
                cvd_1h         = buy_vol - sell_vol,
            )
            metrics[exch] = m

        if not metrics:
            return result

        result.by_exchange = {k: vars(v) for k, v in metrics.items()}

        # ── Расхождение цен ────────────────────────────────────────────────────
        prices = {e: m.price for e, m in metrics.items() if m.price > 0}
        if len(prices) >= 2:
            max_p    = max(prices.values())
            min_p    = min(prices.values())
            div_pct  = (max_p - min_p) / min_p * 100 if min_p > 0 else 0

            result.price_divergence_pct = round(div_pct, 3)
            result.highest_price_ex = max(prices, key=prices.get)
            result.lowest_price_ex  = min(prices, key=prices.get)

            if div_pct > 0.5:
                result.signals.append(
                    f"⚡ Расхождение цены {div_pct:.2f}% между "
                    f"{result.highest_price_ex} и {result.lowest_price_ex}"
                )
            if div_pct > 1.0:
                result.signals.append(
                    f"🚨 Сильное расхождение {div_pct:.2f}% — возможна "
                    f"манипуляция или блокировка арбитража"
                )

        # ── Аномальный объём ───────────────────────────────────────────────────
        vols    = {e: m.volume_1h for e, m in metrics.items()}
        avg_vol = sum(vols.values()) / len(vols) if vols else 0

        if avg_vol > 0:
            for exch, vol in vols.items():
                ratio = vol / avg_vol
                if ratio > 3.0:
                    result.volume_anomaly_ex = exch
                    result.volume_ratio      = round(ratio, 1)
                    result.signals.append(
                        f"📊 Аномальный объём на {exch}: "
                        f"в {ratio:.1f}x больше среднего"
                    )

        # ── Ведущая биржа ──────────────────────────────────────────────────────
        # Определяем где CVD позитивнее всего при меньшем объёме
        # (накопление идёт тихо на конкретной бирже)
        cvds = {e: m.cvd_1h for e, m in metrics.items()}
        max_cvd_ex = max(cvds, key=lambda e: cvds[e] / max(metrics[e].volume_1h, 1))

        if cvds.get(max_cvd_ex, 0) > 0:
            result.leading_exchange = max_cvd_ex
            result.leading_reason   = (
                f"Лучший buy/sell ratio при объёме "
                f"${metrics[max_cvd_ex].volume_1h/1e3:.0f}K"
            )

        # ── Где пампят / где сливают ──────────────────────────────────────────
        buy_ratios = {
            e: m.buy_vol_1h / max(m.volume_1h, 1)
            for e, m in metrics.items()
        }

        if buy_ratios:
            pump_ex = max(buy_ratios, key=buy_ratios.get)
            dump_ex = min(buy_ratios, key=buy_ratios.get)

            if buy_ratios[pump_ex] > 0.65:
                result.pump_exchange = pump_ex
                result.signals.append(
                    f"🟢 {pump_ex}: buy ratio {buy_ratios[pump_ex]:.0%} — "
                    f"агрессивные покупки"
                )

            if buy_ratios[dump_ex] < 0.35:
                result.dump_exchange = dump_ex
                result.signals.append(
                    f"🔴 {dump_ex}: sell ratio {1-buy_ratios[dump_ex]:.0%} — "
                    f"агрессивные продажи"
                )

        # ── Координация между биржами ─────────────────────────────────────────
        # Если объём вырос синхронно на 3+ биржах за последние 10 минут
        recent_surges = 0
        for exch, m_data in metrics.items():
            trades_10m = [
                t for t in self._trades[symbol][exch]
                if now - t.timestamp < 600
            ]
            trades_10m_prev = [
                t for t in self._trades[symbol][exch]
                if 600 < now - t.timestamp < 1200
            ]
            vol_now  = sum(t.usd_volume for t in trades_10m)
            vol_prev = sum(t.usd_volume for t in trades_10m_prev)
            if vol_prev > 0 and vol_now / vol_prev > 2.0:
                recent_surges += 1

        if recent_surges >= 3:
            result.coordinated_move   = True
            result.coordination_score = min(100, recent_surges * 25.0)
            result.signals.append(
                f"🔗 Синхронный рост объёма на {recent_surges} биржах — "
                f"координированное движение"
            )

        return result
