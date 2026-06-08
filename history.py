"""
core/history.py — История скоров по токенам

Хранит скоры во времени. Нужно для:
1. Обнаружения токенов где score растёт несколько дней подряд
   (это сильнее чем одиночный всплеск)
2. Графиков в дашборде
3. Бэктеста

Использует SQLite — не требует отдельного сервера.
"""
import sqlite3
import time
import os
from dataclasses import dataclass
from typing import Optional

from loguru import logger


DB_PATH = os.getenv("HISTORY_DB", "data/score_history.db")


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Создаёт таблицы если не существуют"""
    with _get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS score_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT    NOT NULL,
            timestamp   REAL    NOT NULL,
            score       REAL    NOT NULL,
            probability INTEGER NOT NULL,
            level       TEXT    NOT NULL,
            supply_score    REAL DEFAULT 0,
            sm_score        REAL DEFAULT 0,
            cvd_score       REAL DEFAULT 0,
            dormancy_score  REAL DEFAULT 0,
            deriv_score     REAL DEFAULT 0,
            signals     TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_symbol_ts
            ON score_history(symbol, timestamp);

        CREATE INDEX IF NOT EXISTS idx_ts
            ON score_history(timestamp);

        CREATE TABLE IF NOT EXISTS volume_cache (
            symbol      TEXT PRIMARY KEY,
            volume_24h  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pump_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT    NOT NULL,
            started_at  REAL    NOT NULL,
            peak_at     REAL,
            price_start REAL    NOT NULL,
            price_peak  REAL,
            gain_pct    REAL,
            exchange    TEXT    DEFAULT ''
        );
        """)
    logger.info(f"История инициализирована: {DB_PATH}")


# ─── Запись скоров ────────────────────────────────────────────────────────────

def save_score(score) -> None:
    """Сохраняет ManipulationScore в историю"""
    try:
        signals_str = " | ".join(score.signals[:5]) if score.signals else ""
        with _get_conn() as conn:
            conn.execute("""
                INSERT INTO score_history
                    (symbol, timestamp, score, probability, level,
                     supply_score, sm_score, cvd_score,
                     dormancy_score, deriv_score, signals)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                score.symbol,
                score.timestamp or time.time(),
                score.total_score,
                score.probability,
                score.level,
                score.supply_score,
                score.smart_money_score,
                score.cvd_score,
                score.dormancy_score,
                score.derivatives_score,
                signals_str,
            ))
    except Exception as e:
        logger.debug(f"save_score error: {e}")


# ─── Чтение истории ───────────────────────────────────────────────────────────

def get_score_history(symbol: str, hours: int = 168) -> list[dict]:
    """История скоров токена за последние N часов (168ч = 7 дней)"""
    since = time.time() - hours * 3600
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT timestamp, score, probability, level,
                   supply_score, sm_score, cvd_score, signals
            FROM score_history
            WHERE symbol = ? AND timestamp > ?
            ORDER BY timestamp ASC
        """, (symbol, since)).fetchall()
    return [dict(r) for r in rows]


def get_score_trend(symbol: str, hours: int = 72) -> dict:
    """
    Тренд скора за последние N часов.
    Растущий тренд 3+ дня подряд = сильный сигнал.
    """
    history = get_score_history(symbol, hours)
    if len(history) < 3:
        return {"trend": "unknown", "direction": 0, "points": len(history)}

    scores = [h["score"] for h in history]

    # Простая линейная регрессия
    n    = len(scores)
    xs   = list(range(n))
    x_m  = sum(xs) / n
    y_m  = sum(scores) / n
    num  = sum((xs[i] - x_m) * (scores[i] - y_m) for i in range(n))
    den  = sum((xs[i] - x_m) ** 2 for i in range(n))
    slope = num / den if den != 0 else 0

    first_avg = sum(scores[:n//3]) / (n//3) if n >= 3 else scores[0]
    last_avg  = sum(scores[-n//3:]) / (n//3) if n >= 3 else scores[-1]
    change    = last_avg - first_avg

    trend = "rising" if slope > 0.5 else ("falling" if slope < -0.5 else "flat")

    return {
        "trend":        trend,
        "slope":        round(slope, 3),
        "change":       round(change, 1),
        "first_avg":    round(first_avg, 1),
        "last_avg":     round(last_avg, 1),
        "points":       n,
        "max_score":    round(max(scores), 1),
        "current":      round(scores[-1], 1),
    }


def get_rising_tokens(min_hours: int = 48, min_slope: float = 0.3) -> list[dict]:
    """
    Токены где score растёт несколько дней подряд.
    Это сильнее одиночного всплеска.
    """
    since = time.time() - min_hours * 3600
    with _get_conn() as conn:
        symbols = conn.execute("""
            SELECT DISTINCT symbol FROM score_history
            WHERE timestamp > ? AND score > 20
            ORDER BY symbol
        """, (since,)).fetchall()

    results = []
    for row in symbols:
        sym   = row["symbol"]
        trend = get_score_trend(sym, min_hours)
        if trend["slope"] >= min_slope and trend["last_avg"] >= 35:
            results.append({
                "symbol":    sym,
                "slope":     trend["slope"],
                "current":   trend["current"],
                "change":    trend["change"],
                "max_score": trend["max_score"],
                "trend":     trend["trend"],
            })

    return sorted(results, key=lambda x: x["slope"], reverse=True)[:20]


# ─── Фильтр по объёму ─────────────────────────────────────────────────────────

def save_volume(symbol: str, volume_24h: float) -> None:
    with _get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO volume_cache (symbol, volume_24h, updated_at)
            VALUES (?, ?, ?)
        """, (symbol, volume_24h, time.time()))


def get_volume(symbol: str) -> float:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT volume_24h FROM volume_cache WHERE symbol = ?", (symbol,)
        ).fetchone()
    return row["volume_24h"] if row else 0.0


def passes_volume_filter(symbol: str, min_volume: float = 100_000) -> bool:
    """Проверяет прошёл ли токен фильтр по объёму"""
    vol = get_volume(symbol)
    if vol == 0:
        return True   # неизвестный объём — пропускаем (обновится позже)
    return vol >= min_volume


# ─── Хранение памп-событий (для бэктеста) ────────────────────────────────────

def save_pump_event(
    symbol: str,
    started_at: float,
    price_start: float,
    price_peak: float = None,
    peak_at: float = None,
    exchange: str = "",
) -> None:
    gain = (price_peak - price_start) / price_start * 100 if price_peak else None
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO pump_events
                (symbol, started_at, peak_at, price_start, price_peak, gain_pct, exchange)
            VALUES (?,?,?,?,?,?,?)
        """, (symbol, started_at, peak_at, price_start, price_peak, gain, exchange))


def get_pump_events(min_gain_pct: float = 30.0) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM pump_events
            WHERE gain_pct >= ?
            ORDER BY gain_pct DESC
        """, (min_gain_pct,)).fetchall()
    return [dict(r) for r in rows]
