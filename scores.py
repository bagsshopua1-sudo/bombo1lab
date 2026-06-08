"""
analytics/scores.py — Три аналитических скора

Accumulation Score    — вероятность что крупные игроки накапливают позицию
Pump Readiness Score  — вероятность сильного движения в ближайшие дни
Distribution Risk     — вероятность что памп заканчивается и идёт разгрузка

Каждый скор 0–100 с объяснением.
"""
import time
from dataclasses import dataclass, field
from typing import Optional

from core.config import ManipulationScore, OnchainData, score_to_emoji


@dataclass
class ThreeScores:
    symbol:    str
    timestamp: float

    # Три основных скора
    accumulation:   float = 0.0   # 0–100
    pump_readiness: float = 0.0   # 0–100
    distribution:   float = 0.0   # 0–100 (высокий = опасно)

    # Потенциал движения
    potential_multiplier: float = 1.0   # 1x, 2x, 5x, 10x...
    potential_label:      str   = "—"  # "2–5x", "5–10x", "10x+"

    # Объяснения
    acc_signals:   list = field(default_factory=list)
    pump_signals:  list = field(default_factory=list)
    dist_signals:  list = field(default_factory=list)

    # Общий вывод
    verdict: str = ""


class ThreeScoreEngine:
    """
    Рассчитывает три независимых скора на основе всех доступных данных.

    Логика разделения:
    - Accumulation = что уже происходит (накопление идёт сейчас)
    - Pump Readiness = условия для движения (пружина сжата)
    - Distribution Risk = признаки завершения (крупные игроки выходят)
    """

    def calculate(
        self,
        base_score:    ManipulationScore,
        onchain:       Optional[OnchainData] = None,
        exchange_data: dict = None,   # от ExchangeAnalyzer
        wallet_data:   dict = None,   # от WalletTracker
    ) -> ThreeScores:

        result = ThreeScores(
            symbol    = base_score.symbol,
            timestamp = time.time(),
        )

        # ── 1. ACCUMULATION SCORE ─────────────────────────────────────────────
        # Отвечает на вопрос: идёт ли накопление прямо сейчас?
        acc = 0.0

        # Exchange Supply уходит с бирж — прямой признак
        if onchain and onchain.exchange_supply_change_7d < -5:
            acc += 30
            result.acc_signals.append(
                f"📤 Токены уходят с бирж: {onchain.exchange_supply_change_7d:.1f}% за 7д"
            )
        elif onchain and onchain.exchange_supply_change_7d < -2:
            acc += 15

        # Smart Money покупают
        if onchain and onchain.smart_money_net_flow_14d > 500_000:
            acc += 25
            result.acc_signals.append(
                f"🎯 Smart Money: ${onchain.smart_money_net_flow_14d/1e3:.0f}K за 14д"
            )
        elif onchain and onchain.smart_money_buyers >= 3:
            acc += 15
            result.acc_signals.append(
                f"🎯 Smart Money: {onchain.smart_money_buyers} кошельков покупают"
            )

        # Arkham — выводы с бирж крупными игроками
        if onchain and onchain.whale_withdrawals_7d_usd > 1_000_000:
            acc += 20
            result.acc_signals.append(
                f"🐋 Киты вывели ${onchain.whale_withdrawals_7d_usd/1e6:.1f}M с бирж за 7д"
            )
        elif onchain and onchain.whale_withdrawals_7d_usd > 200_000:
            acc += 10

        # Проснувшиеся кошельки
        if onchain and onchain.dormant_wallets_woke >= 3:
            acc += 15
            result.acc_signals.append(
                f"💤 Проснулись {onchain.dormant_wallets_woke} кошельков (6м+)"
            )

        # CVD дивергенция — покупают скрытно
        if base_score.cvd_score >= 12:
            acc += 10
            result.acc_signals.append("📈 CVD дивергенция — скрытые покупки")

        # Wallet tracker — умные кошельки входят
        if wallet_data and wallet_data.get("smart_wallets_entering", 0) >= 2:
            acc += 10
            result.acc_signals.append(
                f"🧠 {wallet_data['smart_wallets_entering']} Smart Money кошельков входят"
            )

        result.accumulation = min(100.0, round(acc, 1))

        # ── 2. PUMP READINESS SCORE ───────────────────────────────────────────
        # Отвечает на вопрос: созданы ли условия для движения?
        pr = 0.0

        # Ограниченное предложение — топливо для пампа
        if onchain and onchain.real_free_float_pct < 5:
            pr += 25
            result.pump_signals.append(
                f"🔒 Free Float: {onchain.real_free_float_pct:.1f}% — минимальное предложение"
            )
        elif onchain and onchain.real_free_float_pct < 10:
            pr += 15

        # OI растёт при нейтральном funding — умное позиционирование
        if base_score.oi_score >= 7:
            pr += 15
            result.pump_signals.append("📊 OI растёт без перегрева funding")

        # Derivatives basis — институционалы готовятся
        if onchain and onchain.derivatives_basis_pct > 20:
            pr += 15
            result.pump_signals.append(
                f"📊 Basis {onchain.derivatives_basis_pct:.0f}% — институционалы в позиции"
            )

        # Кластер ликвидаций шортов выше цены = цель для движения
        if exchange_data and exchange_data.get("short_liq_cluster_above"):
            pr += 15
            result.pump_signals.append(
                f"⚡ Кластер шорт-ликвидаций выше цены — шорт-сквиз возможен"
            )

        # Аномалия на конкретной бирже — там начинается движение
        if exchange_data and exchange_data.get("leading_exchange"):
            pr += 10
            result.pump_signals.append(
                f"🏦 {exchange_data['leading_exchange']} опережает рынок"
            )

        # Накопление уже идёт
        if result.accumulation >= 60:
            pr += 10

        # Unlock не скоро — нет давления сверху
        if onchain and onchain.next_unlock_days > 60:
            pr += 10
            result.pump_signals.append(f"✅ Unlock через {onchain.next_unlock_days}д — нет давления")

        result.pump_readiness = min(100.0, round(pr, 1))

        # ── 3. DISTRIBUTION RISK SCORE ────────────────────────────────────────
        # Отвечает на вопрос: идёт ли разгрузка позиции?
        dist = 0.0

        # Токены идут НА биржи — готовятся продавать
        if onchain and onchain.exchange_inflows_7d_usd > onchain.whale_withdrawals_7d_usd * 1.5:
            dist += 35
            result.dist_signals.append(
                f"⚠ Вводы на биржи превышают выводы — подготовка к продаже"
            )

        # Unlock скоро
        if onchain and onchain.next_unlock_days < 7:
            dist += 30
            result.dist_signals.append(
                f"🔓 Unlock через {onchain.next_unlock_days}д — высокий риск дампа"
            )
        elif onchain and onchain.next_unlock_days < 21:
            dist += 15
            result.dist_signals.append(f"🔓 Unlock через {onchain.next_unlock_days}д")

        # Funding перегрет — лонги перегружены
        if base_score.oi_score > 0 and "longs_overheated" in str(base_score.signals):
            dist += 20
            result.dist_signals.append("⚠ Funding перегрет — лонги перегружены")

        # Расхождение цены на биржах в пользу продажи
        if exchange_data and exchange_data.get("dump_exchange"):
            dist += 15
            result.dist_signals.append(
                f"📉 {exchange_data['dump_exchange']} продаёт агрессивнее других бирж"
            )

        # CVD медвежья дивергенция
        if base_score.cvd_score > 0 and "bearish" in str(base_score.signals):
            dist += 10
            result.dist_signals.append("📉 CVD: продавцы агрессивнее покупателей")

        result.distribution = min(100.0, round(dist, 1))

        # ── ПОТЕНЦИАЛ ДВИЖЕНИЯ ────────────────────────────────────────────────
        result.potential_multiplier, result.potential_label = self._calc_potential(
            result, onchain
        )

        # ── ИТОГОВЫЙ ВЕРДИКТ ──────────────────────────────────────────────────
        result.verdict = self._verdict(result)

        return result

    def _calc_potential(self, r: ThreeScores, od: Optional[OnchainData]) -> tuple:
        """
        Оценивает потенциал движения исходя из структуры рынка.

        Логика:
        - Чем меньше Free Float → тем меньше нужно денег для движения → выше потенциал
        - Чем выше накопление → тем сильнее сжата пружина
        - Чем выше Pump Readiness → тем ближе к движению
        """
        score = (r.accumulation * 0.4 + r.pump_readiness * 0.4 +
                 (100 - r.distribution) * 0.2)

        # Бонус за ограниченный supply
        ff_bonus = 1.0
        if od:
            if od.real_free_float_pct < 3:   ff_bonus = 2.5
            elif od.real_free_float_pct < 5:  ff_bonus = 2.0
            elif od.real_free_float_pct < 10: ff_bonus = 1.5

        adjusted = score * ff_bonus

        if adjusted >= 160:  return 50.0, "50–100x"
        if adjusted >= 130:  return 20.0, "20–50x"
        if adjusted >= 100:  return 10.0, "10–20x"
        if adjusted >= 75:   return 5.0,  "5–10x"
        if adjusted >= 50:   return 2.0,  "2–5x"
        if adjusted >= 30:   return 1.5,  "1.5–2x"
        return 1.0, "< 1.5x"

    def _verdict(self, r: ThreeScores) -> str:
        dist = r.distribution

        if dist >= 60:
            return (
                f"🔴 Высокий риск разгрузки ({dist:.0f}/100). "
                f"Накопление может заканчиваться — осторожно."
            )

        if r.accumulation >= 70 and r.pump_readiness >= 60 and dist < 30:
            return (
                f"🟢 Сильная установка: накопление {r.accumulation:.0f}/100, "
                f"готовность {r.pump_readiness:.0f}/100. "
                f"Потенциал: {r.potential_label}."
            )

        if r.accumulation >= 50 and dist < 40:
            return (
                f"🟡 Накопление идёт ({r.accumulation:.0f}/100), "
                f"условия формируются. Потенциал: {r.potential_label}."
            )

        return (
            f"⚪ Недостаточно сигналов. "
            f"Acc={r.accumulation:.0f} PR={r.pump_readiness:.0f} Dist={dist:.0f}."
        )
