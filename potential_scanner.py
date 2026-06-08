"""
analytics/potential_scanner.py — Сканер потенциала токенов

Находит токены с наибольшей вероятностью сильного движения.
Объединяет все три скора + анализ бирж + трекер кошельков.

Логика отбора:
  СИГНАЛ (что реально работает):
    1. Exchange Supply падает при боковой цене — первопричина
    2. Smart Money кошельки входят — требует реального капитала
    3. CVD дивергенция 72ч+ — подтверждение в реальном времени
    4. Derivatives basis аномалия — институционалы позиционируются
    5. Wallet dormancy wake — инсайдеры просыпаются

  ШУМ (что убрано):
    ✗ Social score — запаздывает
    ✗ Wash trading — 40% ложных
    ✗ Long/Short ratio — задержанные данные
    ✗ Одиночные паттерны стакана — без контекста бесполезны
"""
import time
from dataclasses import dataclass, field
from typing import Optional

from core.config import ManipulationScore, OnchainData, score_to_emoji
from analytics.scores import ThreeScores, ThreeScoreEngine
from analytics.exchange_analyzer import ExchangeAnalysis


@dataclass
class TokenPotential:
    """Полная оценка потенциала одного токена"""
    symbol:    str
    timestamp: float

    # Три скора
    accumulation:   float = 0.0
    pump_readiness: float = 0.0
    distribution:   float = 0.0

    # Общий manipulation score
    manipulation_score: float = 0.0
    probability:        int   = 0

    # Потенциал движения
    potential_label: str  = "—"
    potential_x:     float= 1.0

    # Биржевой анализ
    leading_exchange:     str   = ""
    price_divergence_pct: float = 0.0
    coordinated_move:     bool  = False

    # Кошельки
    smart_wallets_count: int   = 0
    smart_wallets_usd:   float = 0.0

    # Ключевые сигналы (топ-5 самых важных)
    top_signals: list = field(default_factory=list)

    # Риск-факторы
    risk_factors: list = field(default_factory=list)

    # Итоговый вердикт
    verdict:  str = ""
    verdict_short: str = ""


class PotentialScanner:
    """
    Собирает все данные по токену и выдаёт итоговую оценку потенциала.
    """

    def __init__(self):
        self._engine = ThreeScoreEngine()

    def evaluate(
        self,
        base_score:      ManipulationScore,
        onchain:         Optional[OnchainData]  = None,
        exchange_data:   Optional[ExchangeAnalysis] = None,
        wallet_data:     dict = None,
    ) -> TokenPotential:

        symbol = base_score.symbol
        result = TokenPotential(symbol=symbol, timestamp=time.time())

        # ── Три скора ──────────────────────────────────────────────────────────
        ex_dict = None
        if exchange_data:
            ex_dict = {
                "leading_exchange":       exchange_data.leading_exchange,
                "short_liq_cluster_above":exchange_data.short_liq_cluster_above,
                "dump_exchange":          exchange_data.dump_exchange,
            }

        three = self._engine.calculate(base_score, onchain, ex_dict, wallet_data)

        result.accumulation   = three.accumulation
        result.pump_readiness = three.pump_readiness
        result.distribution   = three.distribution
        result.potential_label= three.potential_label
        result.potential_x    = three.potential_multiplier

        # ── Manipulation score ─────────────────────────────────────────────────
        result.manipulation_score = base_score.total_score
        result.probability        = base_score.probability

        # ── Биржевой анализ ────────────────────────────────────────────────────
        if exchange_data:
            result.leading_exchange     = exchange_data.leading_exchange or ""
            result.price_divergence_pct = exchange_data.price_divergence_pct
            result.coordinated_move     = exchange_data.coordinated_move

        # ── Кошельки ───────────────────────────────────────────────────────────
        if wallet_data:
            result.smart_wallets_count = wallet_data.get("smart_wallets_entering", 0)
            result.smart_wallets_usd   = wallet_data.get("smart_total_usd", 0)

        # ── Топ-5 сигналов (самые ранние и надёжные) ─────────────────────────
        all_signals = []

        # Ранжируем по надёжности
        if onchain and onchain.dormant_wallets_woke >= 3:
            all_signals.append((
                100,
                f"💤 Проснулись {onchain.dormant_wallets_woke} кошельков (6м+) — "
                f"инсайдерский сигнал"
            ))

        if onchain and onchain.exchange_supply_change_7d < -5:
            all_signals.append((
                95,
                f"📤 Вывод с бирж: {onchain.exchange_supply_change_7d:.1f}% за 7д"
            ))

        if result.smart_wallets_count >= 2:
            all_signals.append((
                90,
                f"🧠 {result.smart_wallets_count} Smart Money кошельков входят "
                f"(${result.smart_wallets_usd/1e3:.0f}K)"
            ))

        if onchain and onchain.derivatives_basis_pct > 20:
            all_signals.append((
                85,
                f"📊 Basis {onchain.derivatives_basis_pct:.0f}% — "
                f"институционалы в позиции (сигнал за 7–21д)"
            ))

        if three.cvd_score >= 10:
            all_signals.append((
                80,
                f"📈 CVD дивергенция: покупатели агрессивнее 72ч+"
            ))

        if onchain and onchain.smart_money_net_flow_14d > 200_000:
            all_signals.append((
                75,
                f"🎯 Smart Money нетто: ${onchain.smart_money_net_flow_14d/1e3:.0f}K за 14д"
            ))

        if exchange_data and exchange_data.leading_exchange:
            all_signals.append((
                70,
                f"🏦 {exchange_data.leading_exchange} опережает рынок — "
                f"движение начинается там"
            ))

        if exchange_data and exchange_data.price_divergence_pct > 0.5:
            all_signals.append((
                65,
                f"⚡ Расхождение цен {exchange_data.price_divergence_pct:.2f}% — аномалия"
            ))

        if exchange_data and exchange_data.coordinated_move:
            all_signals.append((
                60,
                f"🔗 Синхронный рост объёма на нескольких биржах"
            ))

        # Добавляем сигналы из base_score
        for sig in base_score.signals[:3]:
            all_signals.append((50, sig))

        # Сортируем и берём топ-5
        all_signals.sort(key=lambda x: x[0], reverse=True)
        result.top_signals = [s[1] for s in all_signals[:5]]

        # ── Риск-факторы ──────────────────────────────────────────────────────
        result.risk_factors = list(base_score.risk_factors)
        if exchange_data and exchange_data.dump_exchange:
            result.risk_factors.append(
                f"📉 {exchange_data.dump_exchange}: агрессивные продажи"
            )
        if result.distribution >= 50:
            result.risk_factors.append(
                f"⚠ Distribution Risk {result.distribution:.0f}/100 — возможна разгрузка"
            )

        # ── Итоговый вердикт ──────────────────────────────────────────────────
        result.verdict, result.verdict_short = self._build_verdict(result, three)

        return result

    def _build_verdict(self, r: TokenPotential, three: ThreeScores) -> tuple[str, str]:
        """Строит развёрнутый и краткий вердикт"""

        # Плохой сценарий
        if r.distribution >= 60:
            return (
                f"🔴 Вероятна разгрузка позиции. Distribution Risk {r.distribution:.0f}/100. "
                f"Накопление могло завершиться — высокий риск снижения цены.",
                "Вероятная разгрузка"
            )

        # Сильный сценарий
        if r.accumulation >= 65 and r.pump_readiness >= 55 and r.distribution < 30:
            return (
                f"🟢 Сильная установка. Накопление {r.accumulation:.0f}/100, "
                f"готовность {r.pump_readiness:.0f}/100. "
                f"Потенциал: {r.potential_label}. "
                f"Первые сигналы: {r.top_signals[0] if r.top_signals else '—'}",
                f"Высокий потенциал {r.potential_label}"
            )

        # Средний сценарий — накопление идёт
        if r.accumulation >= 45 and r.distribution < 40:
            return (
                f"🟡 Накопление формируется ({r.accumulation:.0f}/100). "
                f"Условия ещё не созрели полностью. "
                f"Потенциал: {r.potential_label} при подтверждении.",
                f"Накопление идёт, потенциал {r.potential_label}"
            )

        return (
            f"⚪ Недостаточно сигналов для уверенного вывода. "
            f"Acc={r.accumulation:.0f} PR={r.pump_readiness:.0f} Dist={r.distribution:.0f}",
            "Сигналов недостаточно"
        )

    def format_for_telegram(self, p: TokenPotential) -> str:
        """Форматирует оценку потенциала для Telegram"""
        emoji  = score_to_emoji(p.manipulation_score)
        lines  = [
            f"⬡ *{p.symbol}* — Оценка потенциала",
            f"",
            f"{emoji} Manipulation: *{p.manipulation_score:.0f}/100* (~{p.probability}%)",
            f"🎯 Потенциал: *{p.potential_label}*",
            f"",
            f"*Три скора:*",
            f"  📥 Accumulation:    `{p.accumulation:5.1f}/100`",
            f"  🚀 Pump Readiness:  `{p.pump_readiness:5.1f}/100`",
            f"  📤 Distribution:    `{p.distribution:5.1f}/100`",
        ]

        if p.leading_exchange:
            lines.append(f"  🏦 Ведущая биржа:   `{p.leading_exchange}`")

        if p.price_divergence_pct > 0.3:
            lines.append(f"  ⚡ Расхождение цен: `{p.price_divergence_pct:.2f}%`")

        if p.smart_wallets_count > 0:
            lines.append(
                f"  🧠 Smart Money:     `{p.smart_wallets_count} кошельков`"
            )

        if p.top_signals:
            lines += ["", "*Ключевые сигналы:*"]
            for s in p.top_signals[:4]:
                lines.append(f"• {s}")

        if p.risk_factors:
            lines += ["", "*Риски:*"]
            for r in p.risk_factors[:3]:
                lines.append(f"• {r}")

        lines += ["", f"*Вердикт:* {p.verdict_short}"]
        return "\n".join(lines)


def rank_tokens(potentials: list[TokenPotential]) -> list[TokenPotential]:
    """
    Ранжирует токены по суммарной привлекательности.
    Учитываем все три скора с весами.
    """
    def score_fn(p: TokenPotential) -> float:
        # Накопление и готовность важны, распределение снижает
        base = (
            p.accumulation   * 0.40 +
            p.pump_readiness * 0.35 +
            (100 - p.distribution) * 0.25
        )
        # Бонус за умные кошельки
        if p.smart_wallets_count >= 3:
            base *= 1.2
        elif p.smart_wallets_count >= 1:
            base *= 1.1
        # Бонус за расхождение цен (аномалия)
        if p.price_divergence_pct > 0.5:
            base *= 1.1
        return base

    return sorted(potentials, key=score_fn, reverse=True)
