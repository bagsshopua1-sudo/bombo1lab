"""
scoring/engine.py — Финальный движок скоринга

Архитектура основана на принципе:
"Отслеживай только то что невозможно подделать дёшево"

УДАЛЕНО из старой версии:
  ✗ Social score (LunarCrush) — запаздывает, легко накручивается
  ✗ Wash trading detection — 40% ложных срабатываний
  ✗ Layering — сложно реализовать надёжно
  ✗ Long/Short Ratio — агрегированные задержанные данные
  ✗ Bid/Ask imbalance как самостоятельный сигнал — слишком краткосрочный

ОСТАВЛЕНО и УСИЛЕНО:
  ✓ Exchange Supply Change — первопричина, нельзя подделать без реальных денег
  ✓ Smart Money Accumulation — требует реального капитала
  ✓ CVD Divergence — подтверждает накопление в реальном времени
  ✓ Wallet Dormancy Wake — проснувшиеся кошельки = инсайдеры готовятся
  ✓ Derivatives Basis + Borrow Rate — институциональные деньги
  ✓ OI + Neutral Funding — позиционирование без перегрева
  ✓ Iceberg + Absorption — только как подтверждение, не как основной сигнал

НОВОЕ:
  ✓ Real Free Float — структурное условие для возможности пампа
  ✓ Unlock Risk Penalty — штраф за приближающийся unlock
  ✓ Inflow Penalty — штраф если токены идут НА биржи (готовятся продавать)
"""
import time
from dataclasses import dataclass
from typing import Optional

from core.config import (
    ManipulationScore, OnchainData,
    score_to_level, score_to_emoji, score_to_probability, is_top_token
)


@dataclass
class ScoreInput:
    """Все входные данные для расчёта скора"""
    symbol:    str
    timestamp: float = 0

    # Ончейн (самые важные)
    onchain:   Optional[OnchainData] = None

    # Order Flow (вторичные)
    cvd_divergence:  dict = None   # от CVDCalculator
    large_trade_ratio: dict = None
    iceberg_count:   int  = 0
    absorption_ratio: float = 0.0
    defended_levels: int  = 0

    # Деривативы
    oi_change_24h_pct: float = 0.0
    funding_rate:      float = 0.0   # %
    funding_signal:    str   = "neutral"


class ManipulationScorer:
    """
    Финальный скоринг.

    Веса компонентов (итого 100):

    КАТЕГОРИЯ A — Первопричины (60 баллов)
    ┌──────────────────────────────────────┬──────┐
    │ Exchange Supply Change (-7d/-30d)    │  25  │
    │ Smart Money Accumulation             │  25  │
    │ Real Free Float < 10%                │  10  │
    └──────────────────────────────────────┴──────┘

    КАТЕГОРИЯ B — Подтверждающие (35 баллов)
    ┌──────────────────────────────────────┬──────┐
    │ CVD Divergence (72h+)                │  15  │
    │ Wallet Dormancy Wake                 │  10  │
    │ Derivatives Basis + Borrow Rate      │  10  │
    └──────────────────────────────────────┴──────┘

    КАТЕГОРИЯ C — Второстепенные (5 баллов)
    ┌──────────────────────────────────────┬──────┐
    │ Iceberg + Absorption + OI            │   5  │
    └──────────────────────────────────────┴──────┘

    ШТРАФЫ
    ┌──────────────────────────────────────┬──────┐
    │ Unlock < 14 дней                     │  -20 │
    │ Exchange Inflow рост > 20%           │  -15 │
    │ Funding перегрет (лонги)             │   -5 │
    └──────────────────────────────────────┴──────┘
    """

    def calculate(self, inp: ScoreInput) -> ManipulationScore:
        signals      = []
        risk_factors = []
        penalties    = 0.0

        od = inp.onchain  # OnchainData или None

        # ══ НЕОБХОДИМЫЕ УСЛОВИЯ ══════════════════════════════════════════════
        # Если не выполнены — скор 0, смысла считать дальше нет

        # 1. Не топ-токен
        if is_top_token(inp.symbol):
            return self._zero(inp.symbol, "Топ-токен — алерты отключены")

        # 2. Токены должны уходить с бирж, не приходить
        if od and od.exchange_supply_change_7d > 2:
            return self._zero(inp.symbol,
                f"Токены идут НА биржи (+{od.exchange_supply_change_7d:.1f}%) — риск дампа")

        # ══ КАТЕГОРИЯ A — ПЕРВОПРИЧИНЫ (макс 60) ═════════════════════════════

        # ── A1. Exchange Supply Change (макс 25) ──────────────────────────────
        supply_score = 0.0
        if od:
            chg7  = od.exchange_supply_change_7d   # отрицательный = хорошо
            chg30 = od.exchange_supply_change_30d

            if chg7 < -10:
                supply_score += 15
                signals.append(f"📤 Вывод с бирж: {chg7:.1f}% за 7д — сильное накопление")
            elif chg7 < -5:
                supply_score += 10
                signals.append(f"📤 Вывод с бирж: {chg7:.1f}% за 7д")
            elif chg7 < -2:
                supply_score += 5

            if chg30 < -15:
                supply_score += 10
                signals.append(f"📤 Вывод с бирж: {chg30:.1f}% за 30д — устойчивый тренд")
            elif chg30 < -8:
                supply_score += 5

            supply_score = min(25, supply_score)

        # ── A2. Smart Money Accumulation (макс 25) ────────────────────────────
        sm_score = 0.0
        if od:
            net = od.smart_money_net_flow_14d
            buyers = od.smart_money_buyers

            if net > 2_000_000:
                sm_score += 15
                signals.append(f"🎯 Smart Money: накопление ${net/1e6:.1f}M за 14д")
            elif net > 500_000:
                sm_score += 10
                signals.append(f"🎯 Smart Money: покупки ${net/1e3:.0f}K за 14д")
            elif net > 100_000:
                sm_score += 5

            if buyers >= 5:
                sm_score += 10
                signals.append(f"🎯 Smart Money: {buyers} уникальных кошельков покупают")
            elif buyers >= 3:
                sm_score += 5
            elif buyers >= 1:
                sm_score += 2

            sm_score = min(25, sm_score)

        # ── A3. Real Free Float (макс 10) ─────────────────────────────────────
        ff_score = 0.0
        if od:
            ff = od.real_free_float_pct
            if ff < 3:
                ff_score = 10
                signals.append(f"🔒 Free Float: {ff:.1f}% — минимальный supply на рынке")
            elif ff < 5:
                ff_score = 8
                signals.append(f"🔒 Free Float: {ff:.1f}% — очень ограниченное предложение")
            elif ff < 10:
                ff_score = 5
            elif ff < 15:
                ff_score = 2

        # ══ КАТЕГОРИЯ B — ПОДТВЕРЖДАЮЩИЕ (макс 35) ═══════════════════════════

        # ── B1. CVD Divergence (макс 15) ──────────────────────────────────────
        cvd_score = 0.0
        if inp.cvd_divergence:
            div_score = inp.cvd_divergence.get("score", 0)
            div_type  = inp.cvd_divergence.get("divergence", "none")
            cvd_pct   = inp.cvd_divergence.get("cvd_pct", 0)

            if div_type == "bullish" and div_score > 60:
                cvd_score = 15
                signals.append(
                    f"📈 CVD дивергенция: CVD +{cvd_pct:.1f}% при боковой цене "
                    f"— скрытое накопление 72ч+"
                )
            elif div_type == "bullish" and div_score > 30:
                cvd_score = 8
                signals.append(f"📈 CVD дивергенция: нарастает")
            elif div_type == "bullish":
                cvd_score = 4

        if inp.large_trade_ratio:
            ratio = inp.large_trade_ratio.get("ratio", 1)
            if ratio > 5:
                cvd_score = min(15, cvd_score + 5)
                signals.append(f"🐋 Крупные Buy/Sell: {ratio:.1f}:1 — институционалы покупают")
            elif ratio > 3:
                cvd_score = min(15, cvd_score + 3)

        # ── B2. Wallet Dormancy Wake (макс 10) ────────────────────────────────
        dormancy_score = 0.0
        if od:
            woke  = od.dormant_wallets_woke
            vol   = od.dormant_volume_usd

            if woke >= 5 and vol > 1_000_000:
                dormancy_score = 10
                signals.append(
                    f"💤 Проснулись {woke} кошельков (6м+): ${vol/1e6:.1f}M — "
                    f"инсайдеры или ранние инвесторы готовятся"
                )
            elif woke >= 3 or vol > 200_000:
                dormancy_score = 6
                signals.append(f"💤 Проснулись {woke} кошельков (6м+): ${vol/1e3:.0f}K")
            elif woke >= 1:
                dormancy_score = 3

        # ── B3. Derivatives: Basis + Borrow Rate (макс 10) ────────────────────
        deriv_score = 0.0
        if od:
            basis = od.derivatives_basis_pct   # аномалия выше 20% ann = институционалы
            borrow_chg = od.borrow_rate_change_pct

            # Высокий basis = cash-and-carry институционалов
            if basis > 25:
                deriv_score += 5
                signals.append(
                    f"📊 Derivatives basis: {basis:.1f}% — институционалы делают "
                    f"cash-and-carry, видно за 7–21 день"
                )
            elif basis > 15:
                deriv_score += 3

            # Рост стоимости заимствования = дефицит supply для шортов
            if borrow_chg > 50:
                deriv_score += 5
                signals.append(
                    f"💰 Borrow rate +{borrow_chg:.0f}% — дефицит токена для займа, "
                    f"предложение сокращается"
                )
            elif borrow_chg > 20:
                deriv_score += 2

        # OI + нейтральный funding (бонус в категории B)
        oi_bonus = 0.0
        if inp.oi_change_24h_pct > 20 and inp.funding_signal in ("neutral", "shorts_elevated"):
            oi_bonus = 5
            signals.append(
                f"📊 OI +{inp.oi_change_24h_pct:.0f}% при нейтральном funding — "
                f"умное накопление позиций без перегрева"
            )
        elif inp.oi_change_24h_pct > 10:
            oi_bonus = 2

        deriv_score = min(10, deriv_score + oi_bonus)

        # ══ КАТЕГОРИЯ C — ВТОРОСТЕПЕННЫЕ (макс 5) ════════════════════════════

        orderflow_score = 0.0
        if inp.iceberg_count >= 3:
            orderflow_score += 2
        if inp.absorption_ratio > 10:
            orderflow_score += 2
        if inp.defended_levels >= 3:
            orderflow_score += 1
        orderflow_score = min(5, orderflow_score)

        # ══ ШТРАФЫ ═══════════════════════════════════════════════════════════

        if od:
            # Приближающийся unlock — риск дампа
            if od.next_unlock_days < 7:
                penalties += 25
                risk_factors.append(
                    f"⚠ Unlock через {od.next_unlock_days}д "
                    f"({od.unlock_pct_of_supply:.1f}% supply) — высокий риск дампа"
                )
            elif od.next_unlock_days < 14:
                penalties += 15
                risk_factors.append(
                    f"⚠ Unlock через {od.next_unlock_days}д — умеренный риск"
                )

            # Токены идут на биржи от крупных игроков (Arkham данные)
            if od.exchange_inflows_7d_usd > od.whale_withdrawals_7d_usd * 1.5:
                penalties += 15
                risk_factors.append(
                    f"⚠ Вводы на биржи ${od.exchange_inflows_7d_usd/1e3:.0f}K "
                    f"превышают выводы — подготовка к продаже"
                )

        # Перегрев лонгов
        if inp.funding_signal == "longs_overheated":
            penalties += 5
            risk_factors.append("⚠ Funding перегрет — лонги перегружены, риск лонг-сквиза")

        # ══ ИТОГ ═════════════════════════════════════════════════════════════

        raw = (supply_score + sm_score + ff_score +
               cvd_score + dormancy_score + deriv_score +
               orderflow_score)

        total = max(0.0, min(100.0, round(raw - penalties, 1)))
        level = score_to_level(total)
        prob  = score_to_probability(total)

        return ManipulationScore(
            symbol=inp.symbol,
            timestamp=inp.timestamp or time.time(),
            total_score=total,
            probability=prob,
            level=level,
            supply_score=round(supply_score, 1),
            smart_money_score=round(sm_score, 1),
            free_float_score=round(ff_score, 1),
            cvd_score=round(cvd_score, 1),
            dormancy_score=round(dormancy_score, 1),
            derivatives_score=round(deriv_score, 1),
            orderflow_score=round(orderflow_score, 1),
            penalties=round(penalties, 1),
            signals=signals,
            risk_factors=risk_factors,
        )

    def _zero(self, symbol: str, reason: str) -> ManipulationScore:
        return ManipulationScore(
            symbol=symbol,
            timestamp=time.time(),
            total_score=0,
            probability=0,
            level="NORMAL",
            signals=[reason],
        )
