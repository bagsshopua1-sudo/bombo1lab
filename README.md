# Crypto Manipulation Monitor — Финальная версия

## Что изменилось vs предыдущих версий

### Удалено (не даёт реального преимущества):
- ❌ Social score (LunarCrush) — запаздывает на 12–48ч, легко накручивается
- ❌ Wash trading detection — 40% ложных срабатываний
- ❌ Layering detector — ненадёжно, много шума
- ❌ Long/Short Ratio — задержанные агрегированные данные
- ❌ Nansen — дублирует Glassnode+Arkham за $150/мес

### Оставлено и усилено:
- ✅ Exchange Supply Change — первопричина, нельзя подделать
- ✅ Smart Money Accumulation — требует реального капитала
- ✅ CVD Divergence — подтверждение в реальном времени
- ✅ Wallet Dormancy Wake — инсайдеры просыпаются
- ✅ Derivatives Basis + Borrow Rate — институциональные деньги
- ✅ Arkham — умный триггер только при score > 65
- ✅ GeckoTerminal DEX — ранний сигнал бесплатно

### Новое:
- ✅ Real Free Float — структурное условие для пампа
- ✅ Unlock Risk Penalty — штраф за приближающийся unlock
- ✅ Inflow Penalty — штраф если токены идут НА биржи
- ✅ Deribit Basis — институциональный cash-and-carry сигнал
- ✅ AI анализ с rule-based fallback (работает без API ключа)

---

## Архитектура скоринга

```
НЕОБХОДИМЫЕ УСЛОВИЯ (проверяются первыми):
  • Не топ-токен (BTC/ETH/SOL и др. — исключены)
  • Токены уходят с бирж, не приходят на них

КАТЕГОРИЯ A — Первопричины (60 баллов):
  📤 Exchange Supply Change    0–25 баллов
  🎯 Smart Money Accumulation  0–25 баллов
  🔒 Real Free Float           0–10 баллов

КАТЕГОРИЯ B — Подтверждающие (35 баллов):
  📈 CVD Divergence 72h+       0–15 баллов
  💤 Wallet Dormancy Wake      0–10 баллов
  📊 Derivatives Basis/Borrow  0–10 баллов

КАТЕГОРИЯ C — Второстепенные (5 баллов):
  🧊 Iceberg + Absorption       0–5 баллов

ШТРАФЫ:
  ❌ Unlock < 7 дней           -25 баллов
  ❌ Unlock < 14 дней          -15 баллов
  ❌ Exchange Inflows > Outflows -15 баллов
  ❌ Funding перегрет           -5 баллов
```

---

## Быстрый старт

```bash
# Установка
pip install -r requirements.txt
cp .env.example .env
nano .env   # вставь прокси и токен бота

# Тест (демо без реальных API)
python main.py --demo

# Боевой режим
python main.py

# Без прокси (не рекомендуется — риск бана основного IP)
python main.py --no-proxy
```

---

## Telegram команды

```
/scan WIF       — полный анализ с AI объяснением
/score WIF      — score + вероятность %
/top            — топ-10 подозрительных малых токенов
/dex            — аномалии DEX прямо сейчас (ранний сигнал)
/watch WIF      — подписаться на алерты
/unwatch WIF    — отписаться
/watchlist      — мои подписки
/alerts         — история алертов с вероятностью
/stats          — статистика системы
```

---

## Как выглядит алерт

```
⚠ WIF/USDT — Высокая вероятность манипуляции
🟠 Score: 67/100 | Вероятность: 75%

🤖 Анализ:
На WIF/USDT обнаружены признаки вывода токенов с бирж
и накопления со стороны Smart Money кошельков.
Вероятность манипуляции: 75% — одновременно активны
3 независимых сигнала. Паттерн "сжатой пружины"
исторически предшествует движению в течение 1–14 дней.

📊 Сигналы:
• 📤 Вывод с бирж: -8.3% за 7д — накопление
• 🎯 Smart Money: покупки $340K за 14д
• 📈 CVD дивергенция: CVD +14% при боковой цене

👉 /scan WIF
[📊 Подробно] [👁 Следить]
```

---

## Стоимость

| Сервис          | Цена        | Что даёт                    | Нужен? |
|-----------------|-------------|----------------------------|--------|
| Биржи WS        | Бесплатно   | Стакан, сделки, OI         | Да     |
| GeckoTerminal   | Бесплатно   | DEX свапы, trending        | Да     |
| Дeribit         | Бесплатно   | Опционы, basis             | Да     |
| Прокси          | $5–50/мес   | Защита основного IP        | Да     |
| Glassnode       | $29/мес     | Exchange flows             | Важно  |
| Coinglass       | $50/мес     | OI, funding агрег.         | Важно  |
| Arkham          | Бесплатно*  | Entity clusters (score>65) | Да     |
| Anthropic API   | ~$5/мес     | AI объяснения              | Опц.   |

Минимальный бюджет: **$10/мес** (только прокси + бесплатные API)
Рекомендуемый: **$84/мес** (прокси + Glassnode + Coinglass)

---

## Структура проекта

```
monitor_final/
├── core/
│   ├── config.py          — модели данных, топ-токены, конфиг
│   ├── exchanges.py       — WebSocket коннекторы к 7 биржам
│   ├── proxy_manager.py   — ротация прокси
│   └── ai_analyst.py      — AI анализ + rule-based fallback
├── detectors/
│   └── cvd.py             — CVD дивергенция + Iceberg/Absorption
├── onchain/
│   └── clients.py         — Glassnode, Coinglass, Arkham, Deribit, GeckoTerminal
├── scoring/
│   └── engine.py          — финальный скоринг 0–100
├── bot/
│   └── telegram_bot.py    — бот с AI алертами
├── main.py                — оркестратор
├── requirements.txt
└── .env.example
```
