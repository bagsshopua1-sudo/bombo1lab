"""
detectors/cvd.py — CVD калькулятор и детектор дивергенции

Оставлено из старой версии: CVD дивергенция + Large Trade ratio
Удалено: Spoofing (слишком много ложных), Layering (ненадёжно),
         Wash trading detection (40% ложных срабатываний)

Оставлен Iceberg + Absorption — только как подтверждение (вес 5/100)
"""
import time
from collections import deque
from typing import Optional

from core.config import Trade


class CVDCalculator:
    """
    Cumulative Volume Delta — разница Buy Volume vs Sell Volume.

    Ключевой паттерн: CVD растёт при БОКОВОЙ цене 72ч+ =
    скрытое накопление. Покупатели методично скупают без
    движения цены вверх. Это нельзя подделать без реальных денег.
    """

    def __init__(self, window_sec: float = 7200):   # 2 часа
        self.window_sec = window_sec
        self._trades: deque = deque(maxlen=20000)
        self._cvd_cumulative: float = 0.0

    def feed_trade(self, trade: Trade):
        usd   = trade.usd_volume
        delta = usd if trade.side == "buy" else -usd
        self._cvd_cumulative += delta
        self._trades.append({
            "ts":    trade.timestamp,
            "price": trade.price,
            "delta": delta,
            "cvd":   self._cvd_cumulative,
        })

    def get_divergence_score(self) -> dict:
        """
        Оценивает дивергенцию CVD vs Цена за window_sec.

        Возвращает score 0–100:
          0   = нет дивергенции
          100 = максимальная бычья дивергенция (накопление)
        """
        now    = time.time()
        recent = [t for t in self._trades if now - t["ts"] < self.window_sec]

        if len(recent) < 30:
            return {"score": 0, "divergence": "none", "cvd_pct": 0, "price_change_pct": 0}

        price_start = recent[0]["price"]
        price_end   = recent[-1]["price"]
        cvd_start   = recent[0]["cvd"]
        cvd_end     = recent[-1]["cvd"]

        price_chg_pct = (price_end - price_start) / price_start * 100
        cvd_change    = cvd_end - cvd_start

        # Нормализуем CVD к объёму
        total_vol = sum(abs(t["delta"]) for t in recent)
        cvd_pct   = cvd_change / total_vol * 100 if total_vol > 0 else 0

        score = 0
        divergence = "none"

        # Бычья дивергенция: CVD растёт, цена стоит или чуть падает
        if cvd_pct > 8 and price_chg_pct < 1.5:
            score      = min(100, cvd_pct * 2.5)
            divergence = "bullish"
        # Медвежья: CVD падает, цена стоит
        elif cvd_pct < -8 and price_chg_pct > -1.5:
            score      = min(100, abs(cvd_pct) * 2.5)
            divergence = "bearish"

        return {
            "score":            round(score, 1),
            "divergence":       divergence,
            "cvd_pct":          round(cvd_pct, 2),
            "price_change_pct": round(price_chg_pct, 3),
            "cvd_value":        round(cvd_change, 2),
        }

    def get_large_trade_ratio(self, threshold_usd: float = 10000) -> dict:
        """
        Соотношение крупных Buy vs Sell (сделки > $10K).

        Если крупные покупки >> крупных продаж при боковой цене →
        инсайдерское накопление. Это один из сильнейших сигналов.
        """
        now    = time.time()
        recent = [t for t in self._trades if now - t["ts"] < self.window_sec]

        large_buy  = sum(abs(t["delta"]) for t in recent if t["delta"] > threshold_usd)
        large_sell = sum(abs(t["delta"]) for t in recent if t["delta"] < -threshold_usd)

        ratio = large_buy / large_sell if large_sell > 0 else (99 if large_buy > 0 else 1)

        return {
            "large_buy_usd":  round(large_buy, 0),
            "large_sell_usd": round(large_sell, 0),
            "ratio":          round(ratio, 2),
            "signal": "strong_accumulation" if ratio > 5
                      else "accumulation" if ratio > 3
                      else "neutral",
        }


class IcebergAbsorptionDetector:
    """
    Iceberg + Absorption — вторичные сигналы, вес 5/100.

    Используются ТОЛЬКО как подтверждение когда основные
    сигналы уже есть. Не используются как самостоятельный сигнал.
    """

    def __init__(self):
        self._trade_buffer: deque = deque(maxlen=500)
        self._iceberg_levels: dict = {}    # price → refill_count
        self._prev_book: dict = {}         # exchange → {price: qty}
        self._defended: dict = {}          # price → defense_count

    def feed_trade(self, trade: Trade):
        self._trade_buffer.append(trade)

    def feed_orderbook(self, bids: list, asks: list, exchange: str):
        """Обновляет состояние стакана для детекции айсберга"""
        now = time.time()
        bids_dict = {float(p): float(q) for p, q in bids}
        asks_dict = {float(p): float(q) for p, q in asks}

        prev = self._prev_book.get(exchange, {})

        # Детектируем пополняемые уровни (Iceberg)
        for p, q in bids_dict.items():
            if p in prev:
                prev_q = prev.get(p, 0)
                avg    = sum(bids_dict.values()) / len(bids_dict) if bids_dict else 1
                # Уровень частично исполнен но восстановился — айсберг
                if prev_q < q * 0.85 and q > avg * 2:
                    key = round(p, 6)
                    self._iceberg_levels[key] = self._iceberg_levels.get(key, 0) + 1

        self._prev_book[exchange] = {**bids_dict, **asks_dict}

        # Чистим старые уровни (старше 10 минут — не отслеживаем)
        # Упрощённо: ограничиваем размер
        if len(self._iceberg_levels) > 200:
            oldest = sorted(self._iceberg_levels.items(), key=lambda x: x[1])[:50]
            for k, _ in oldest:
                del self._iceberg_levels[k]

    def get_iceberg_count(self, min_refills: int = 3) -> int:
        """Количество активных айсберг-уровней"""
        return sum(1 for v in self._iceberg_levels.values() if v >= min_refills)

    def get_absorption_ratio(self) -> float:
        """
        Absorption: объём продаж / изменение цены.
        Высокое значение = продажи поглощаются без движения цены вниз.
        """
        if len(self._trade_buffer) < 20:
            return 0.0

        recent = list(self._trade_buffer)[-20:]
        sells  = [t for t in recent if t.side == "sell"]
        if not sells:
            return 0.0

        sell_vol  = sum(t.usd_volume for t in sells)
        price_s   = recent[0].price
        price_e   = recent[-1].price
        drop_pct  = max(0.001, abs(price_e - price_s) / price_s * 100)

        return round(sell_vol / (drop_pct * 100), 2)

    def get_defended_levels_count(self, min_defenses: int = 3) -> int:
        return sum(1 for v in self._defended.values() if v >= min_defenses)
