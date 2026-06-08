"""
core/config.py — Конфигурация и модели данных

Финальная архитектура основана на принципе:
"Отслеживай только то что невозможно подделать дёшево"

Удалено: social score, wash trading, long/short ratio, layering
Оставлено: exchange supply, smart money, CVD, dormancy, options/borrow rate
"""
import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


# ─── Топ-токены — исключаем из алертов ───────────────────────────────────────
# Для них крупные движения это норма рынка, не манипуляция
TOP_TOKENS = {
    "BTC","ETH","SOL","BNB","XRP","DOGE","ADA","AVAX","DOT","MATIC",
    "LINK","UNI","ATOM","LTC","ETC","FIL","APT","NEAR","OP","ARB",
    "SHIB","TRX","TON","SUI","IMX","INJ","RUNE","FTM","ALGO","XLM",
    "HBAR","VET","ICP","SAND","MANA","AXS","GALA","ENJ","CHZ","FLOW",
}

def is_top_token(symbol: str) -> bool:
    base = symbol.upper().replace("/USDT","").replace("USDT","").replace("-USDT","")
    return base in TOP_TOKENS


# ─── Конфиг ───────────────────────────────────────────────────────────────────

class Config:
    # Биржи
    BINANCE_API_KEY   = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET    = os.getenv("BINANCE_SECRET", "")

    # Ончейн (платные)
    GLASSNODE_API_KEY = os.getenv("GLASSNODE_API_KEY", "")
    NANSEN_API_KEY    = os.getenv("NANSEN_API_KEY", "")
    ARKHAM_API_KEY    = os.getenv("ARKHAM_API_KEY", "")
    ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

    # Деривативы
    COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")
    DERIBIT_API_KEY   = os.getenv("DERIBIT_API_KEY", "")   # опционы

    # AI аналитик
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

    # Telegram
    BOT_TOKEN         = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ADMIN_CHAT_ID     = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
    EXTRA_CHAT_IDS    = os.getenv("TELEGRAM_EXTRA_CHAT_IDS", "")

    # БД
    REDIS_URL         = os.getenv("REDIS_URL", "redis://localhost:6379")
    CLICKHOUSE_HOST   = os.getenv("CLICKHOUSE_HOST", "localhost")
    CLICKHOUSE_DB     = os.getenv("CLICKHOUSE_DB", "monitor")

    # Прокси
    PROXY_LIST        = os.getenv("PROXY_LIST", "")

    # Пороги
    SCORE_ALERT       = int(os.getenv("SCORE_ALERT_THRESHOLD", "50"))
    SCORE_CRITICAL    = int(os.getenv("SCORE_CRITICAL_THRESHOLD", "70"))
    ALERT_COOLDOWN    = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30")) * 60

    # Фильтры
    MIN_VOLUME_24H    = float(os.getenv("MIN_VOLUME_24H_USD", "100000"))
    SCORING_INTERVAL  = int(os.getenv("SCORING_INTERVAL_SEC", "60"))
    SCORING_WORKERS   = int(os.getenv("SCORING_WORKERS", "20"))

    ENABLED_EXCHANGES = [
        e.strip() for e in
        os.getenv("ENABLED_EXCHANGES",
            "binance_spot,binance_futures,bybit_spot,bybit_futures,"
            "okx_spot,okx_futures,gate_spot,gate_futures,"
            "bitget_spot,bitget_futures,mexc_spot,mexc_futures,kucoin_spot"
        ).split(",") if e.strip()
    ]


# ─── Модели данных ────────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol:    str
    timestamp: float
    price:     float
    qty:       float
    side:      str       # "buy" | "sell"
    exchange:  str = ""
    market:    str = ""  # "spot" | "futures"

    @property
    def usd_volume(self) -> float:
        return self.price * self.qty


@dataclass
class OrderBookUpdate:
    symbol:    str
    timestamp: float
    bids:      list   # [[price, qty], ...]
    asks:      list
    exchange:  str = ""
    market:    str = "spot"


@dataclass
class FuturesData:
    symbol:        str
    timestamp:     float
    exchange:      str
    open_interest: Optional[float] = None
    funding_rate:  Optional[float] = None
    long_liq_usd:  Optional[float] = None
    short_liq_usd: Optional[float] = None
    basis_pct:     Optional[float] = None   # (futures_price - spot) / spot * 100


@dataclass
class OnchainData:
    """Ончейн данные — самые надёжные сигналы"""
    symbol:    str
    timestamp: float

    # ПЕРВОПРИЧИНЫ (категория A)
    exchange_supply_change_7d:  float = 0.0   # % изменение токенов на биржах
    exchange_supply_change_30d: float = 0.0
    real_free_float_pct:        float = 100.0 # % реально доступного supply

    # Smart Money
    smart_money_net_flow_14d:   float = 0.0   # USD нетто-поток от SM кошельков
    smart_money_buyers:         int   = 0     # уникальных SM покупателей

    # Dormancy
    dormant_wallets_woke:       int   = 0     # кошельков 6м+ которые проснулись
    dormant_volume_usd:         float = 0.0   # объём от проснувшихся кошельков

    # Деривативы basis
    derivatives_basis_pct:      float = 0.0   # аномалия basis (норма 5-15% ann)
    borrow_rate_change_pct:     float = 0.0   # изменение ставки займа

    # Unlock риск
    next_unlock_days:           int   = 999   # дней до ближайшего unlock
    unlock_pct_of_supply:       float = 0.0   # % supply который разблокируется

    # Arkham данные (заполняются при score > 65)
    whale_withdrawals_7d_usd:   float = 0.0
    exchange_inflows_7d_usd:    float = 0.0
    top10_concentration_pct:    float = 0.0

    # DEX данные
    dex_buy_vol_1h:             float = 0.0
    dex_buy_ratio_1h:           float = 0.5  # 0.0–1.0


@dataclass
class ManipulationScore:
    """Итоговый скор — результат работы всей системы"""
    symbol:    str
    timestamp: float

    # Итог
    total_score:  float = 0.0   # 0–100
    probability:  int   = 0     # % вероятность манипуляции
    level:        str   = "NORMAL"

    # Компоненты (новая архитектура)
    # Категория A — первопричины (макс 60)
    supply_score:       float = 0.0   # exchange supply change    макс 25
    smart_money_score:  float = 0.0   # smart money accumulation  макс 25
    free_float_score:   float = 0.0   # real free float           макс 10

    # Категория B — подтверждающие (макс 35)
    cvd_score:          float = 0.0   # CVD divergence            макс 15
    dormancy_score:     float = 0.0   # wallet dormancy wake      макс 10
    derivatives_score:  float = 0.0   # options/basis/borrow      макс 10

    # Категория C — второстепенные (макс 5)
    orderflow_score:    float = 0.0   # iceberg + absorption      макс 5

    # Штрафы
    penalties:          float = 0.0

    # Объяснение
    signals:            list  = field(default_factory=list)
    ai_analysis:        str   = ""
    risk_factors:       list  = field(default_factory=list)


def score_to_level(score: float) -> str:
    if score < 30:  return "NORMAL"
    if score < 50:  return "SUSPICIOUS"
    if score < 70:  return "HIGH"
    return "CRITICAL"


def score_to_emoji(score: float) -> str:
    if score < 30:  return "🟢"
    if score < 50:  return "🟡"
    if score < 70:  return "🟠"
    return "🔴"


def score_to_probability(score: float) -> int:
    """
    Нелинейная шкала — несколько одновременных сигналов
    значительно увеличивают достоверность.
    """
    if score >= 85: return 92
    if score >= 75: return 85
    if score >= 65: return 75
    if score >= 55: return 65
    if score >= 50: return 55
    if score >= 40: return 42
    if score >= 30: return 30
    return 12
