"""
core/ai_analyst.py — AI аналитик

Два режима:
  1. С ANTHROPIC_API_KEY — вызывает Claude, пишет живой анализ
  2. Без ключа — rule-based анализ (тоже хорошо работает)

Вызывается только при score > 50 и только для не-топ токенов.
"""
import os
import time
from typing import Optional

import aiohttp
from loguru import logger

from core.config import ManipulationScore, is_top_token, score_to_probability


class AIAnalyst:

    def __init__(self):
        self.api_key  = os.getenv("ANTHROPIC_API_KEY", "")
        self._cache:  dict = {}
        self._ttl:    int  = 1800   # 30 минут

    async def analyze(self, score: ManipulationScore) -> str:
        """Возвращает текстовый анализ для Telegram сообщения"""
        if score.total_score < 50 or is_top_token(score.symbol):
            return ""

        cached = self._cache.get(score.symbol)
        if cached and time.time() - cached["ts"] < self._ttl:
            return cached["text"]

        if self.api_key:
            try:
                text = await self._call_api(score)
                self._cache[score.symbol] = {"text": text, "ts": time.time()}
                return text
            except Exception as e:
                logger.debug(f"AI API error: {e}")

        text = self._rule_based(score)
        self._cache[score.symbol] = {"text": text, "ts": time.time()}
        return text

    async def _call_api(self, score: ManipulationScore) -> str:
        signals_text = "\n".join(f"- {s}" for s in score.signals) if score.signals else "- нет"
        risks_text   = "\n".join(f"- {r}" for r in score.risk_factors) if score.risk_factors else "- нет"

        prompt = f"""Ты аналитик криптовалютного рынка. Напиши короткий анализ на русском языке (3–4 предложения).

Токен: {score.symbol}
Manipulation Score: {score.total_score}/100
Вероятность манипуляции: {score.probability}%

Компоненты:
- Вывод с бирж / Supply: {score.supply_score}/25
- Smart Money накопление: {score.smart_money_score}/25
- Real Free Float: {score.free_float_score}/10
- CVD дивергенция: {score.cvd_score}/15
- Dormancy (проснувшиеся кошельки): {score.dormancy_score}/10
- Деривативы (basis/borrow): {score.derivatives_score}/10
- Order Flow: {score.orderflow_score}/5
- Штрафы: -{score.penalties}

Сигналы:
{signals_text}

Риски:
{risks_text}

Напиши: что происходит, почему вероятность именно такая, что может быть дальше.
Только текст, без заголовков и списков."""

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-haiku-4-5-20251001",
                    "max_tokens": 350,
                    "messages":   [{"role": "user", "content": prompt}]
                },
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return data["content"][0]["text"].strip()
                raise Exception(f"status {r.status}")

    def _rule_based(self, score: ManipulationScore) -> str:
        """Генерирует анализ без API — работает всегда"""
        parts = []

        # Что происходит — определяем по доминирующим компонентам
        dominant = self._dominant(score)
        if dominant:
            parts.append(f"На {score.symbol} обнаружены признаки {dominant}.")

        # Вероятность и обоснование
        active = sum(1 for v in [
            score.supply_score, score.smart_money_score, score.cvd_score,
            score.dormancy_score, score.derivatives_score
        ] if v > 3)

        if score.probability >= 75:
            parts.append(
                f"Вероятность манипуляции: {score.probability}%. "
                f"Одновременно активны {active} независимых сигнала — "
                f"это значительно снижает вероятность случайного совпадения."
            )
        elif score.probability >= 55:
            parts.append(
                f"Вероятность манипуляции: {score.probability}%. "
                f"Несколько аномалий указывают на возможное накопление крупным игроком."
            )
        else:
            parts.append(
                f"Вероятность манипуляции: {score.probability}%. "
                f"Признаки присутствуют, но требуют подтверждения."
            )

        # Прогноз
        parts.append(self._forecast(score))

        return " ".join(parts)

    def _dominant(self, s: ManipulationScore) -> str:
        components = {
            "вывода токенов с бирж и дефицита предложения":     s.supply_score / 25,
            "накопления со стороны Smart Money кошельков":       s.smart_money_score / 25,
            "CVD дивергенции — скрытого накопления позиций":     s.cvd_score / 15,
            "пробуждения крупных кошельков (6м+)":               s.dormancy_score / 10,
            "институциональной активности в деривативах":        s.derivatives_score / 10,
        }
        top = sorted(components.items(), key=lambda x: x[1], reverse=True)
        top = [(k, v) for k, v in top if v > 0.35][:2]
        if not top:
            return ""
        return " и ".join(k for k, _ in top)

    def _forecast(self, s: ManipulationScore) -> str:
        if s.risk_factors:
            return ("Однако есть риски: " +
                    s.risk_factors[0].replace("⚠ ", "") +
                    " — рекомендуется осторожность.")

        if s.dormancy_score >= 8:
            return ("Пробуждение старых кошельков — один из самых надёжных "
                    "ранних сигналов: инсайдеры или ранние инвесторы "
                    "готовятся к действию.")

        if s.supply_score >= 15 and s.cvd_score >= 10:
            return ("Паттерн 'сжатой пружины': supply сокращается, "
                    "покупатели агрессивнее продавцов. "
                    "Исторически предшествует движению в течение 1–14 дней.")

        if s.derivatives_score >= 7:
            return ("Аномалия в деривативах (basis/borrow rate) "
                    "сигнализирует об институциональном позиционировании "
                    "за 7–21 день до движения.")

        if s.total_score >= 70:
            return ("Совокупность сигналов указывает на подготовку "
                    "к направленному движению — рекомендуется повышенное внимание.")

        return "Ситуация требует подтверждения дополнительными сигналами."
