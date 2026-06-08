"""
analytics/wallet_tracker.py — Трекер Smart Money кошельков

Находит кошельки которые исторически покупают ДО пампов.
Отслеживает их новые входы в токены.

Источники данных:
  Arkham  — лейблы и связи адресов
  Etherscan/BSCScan — raw транзакции (бесплатно)
  GeckoTerminal — DEX свапы конкретных адресов

Логика:
  1. Собираем базу "умных" кошельков из прошлых памп-событий
  2. Считаем win rate каждого кошелька
  3. Мониторим их новые покупки в реальном времени
  4. Алерт когда 2+ умных кошелька входят в один токен
"""
import asyncio
import time
import sqlite3
import os
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from loguru import logger

from core.config import Config


DB_PATH = os.getenv("HISTORY_DB", "data/score_history.db")


# ─── Модели ───────────────────────────────────────────────────────────────────

@dataclass
class WalletProfile:
    address:        str
    label:          str = ""          # Arkham лейбл если есть
    win_rate:       float = 0.0       # % успешных входов
    avg_roi:        float = 0.0       # средний ROI
    total_trades:   int   = 0
    wins:           int   = 0
    known_tokens:   list  = field(default_factory=list)
    last_seen:      float = 0
    is_smart_money: bool  = False


@dataclass
class WalletEntry:
    """Вход умного кошелька в токен"""
    address:    str
    token:      str
    timestamp:  float
    amount_usd: float
    tx_hash:    str = ""
    label:      str = ""
    win_rate:   float = 0.0
    via:        str = ""   # "dex" | "cex" | "transfer"


@dataclass
class WalletSignal:
    """Сигнал: несколько умных кошельков вошли в один токен"""
    token:       str
    timestamp:   float
    entries:     list[WalletEntry]
    total_usd:   float
    avg_win_rate:float
    signal_strength: float   # 0–100


# ─── База данных кошельков ────────────────────────────────────────────────────

def init_wallet_db():
    """Создаёт таблицы для кошельков"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS smart_wallets (
            address     TEXT PRIMARY KEY,
            label       TEXT DEFAULT '',
            win_rate    REAL DEFAULT 0,
            avg_roi     REAL DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            wins        INTEGER DEFAULT 0,
            last_seen   REAL DEFAULT 0,
            is_smart    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS wallet_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            address     TEXT NOT NULL,
            token       TEXT NOT NULL,
            timestamp   REAL NOT NULL,
            amount_usd  REAL DEFAULT 0,
            tx_hash     TEXT DEFAULT '',
            via         TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_we_token_ts
            ON wallet_entries(token, timestamp);

        CREATE INDEX IF NOT EXISTS idx_we_address
            ON wallet_entries(address);
        """)


def save_wallet(wallet: WalletProfile):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO smart_wallets
                (address, label, win_rate, avg_roi, total_trades, wins, last_seen, is_smart)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            wallet.address, wallet.label, wallet.win_rate, wallet.avg_roi,
            wallet.total_trades, wallet.wins, wallet.last_seen,
            1 if wallet.is_smart_money else 0
        ))


def save_entry(entry: WalletEntry):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO wallet_entries (address, token, timestamp, amount_usd, tx_hash, via)
            VALUES (?,?,?,?,?,?)
        """, (entry.address, entry.token, entry.timestamp,
              entry.amount_usd, entry.tx_hash, entry.via))


def get_smart_wallets(min_win_rate: float = 0.65) -> list[WalletProfile]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM smart_wallets
            WHERE win_rate >= ? AND is_smart = 1
            ORDER BY win_rate DESC
        """, (min_win_rate,)).fetchall()
    return [
        WalletProfile(
            address=r["address"], label=r["label"],
            win_rate=r["win_rate"], avg_roi=r["avg_roi"],
            total_trades=r["total_trades"], wins=r["wins"],
            is_smart_money=bool(r["is_smart"])
        )
        for r in rows
    ]


def get_recent_entries(token: str, hours: int = 24) -> list[dict]:
    since = time.time() - hours * 3600
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT we.*, sw.label, sw.win_rate
            FROM wallet_entries we
            LEFT JOIN smart_wallets sw ON we.address = sw.address
            WHERE we.token = ? AND we.timestamp > ?
            ORDER BY we.timestamp DESC
        """, (token, since)).fetchall()
    return [dict(r) for r in rows]


# ─── Трекер ──────────────────────────────────────────────────────────────────

class WalletTracker:
    """
    Отслеживает движения умных кошельков.

    Два режима:
    1. DEX мониторинг (бесплатно) — через GeckoTerminal + Etherscan
    2. Arkham (при score > 65) — глубокий анализ с лейблами
    """

    def __init__(self):
        init_wallet_db()
        self._session:    Optional[aiohttp.ClientSession] = None
        self._known_wallets: dict[str, WalletProfile] = {}
        self._load_known_wallets()

        # Кэш последних проверок
        self._last_check: dict[str, float] = {}

    def _load_known_wallets(self):
        """Загружает известные умные кошельки из БД"""
        wallets = get_smart_wallets(min_win_rate=0.60)
        for w in wallets:
            self._known_wallets[w.address.lower()] = w
        logger.info(f"Загружено {len(self._known_wallets)} Smart Money кошельков")

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ─── Etherscan — бесплатно ────────────────────────────────────────────────

    async def get_token_top_traders(
        self,
        token_address: str,
        chain: str = "eth",
        hours: int = 48,
    ) -> list[WalletEntry]:
        """
        Получает топ-трейдеров токена за последние N часов.
        Использует Etherscan API (бесплатно, 5 запросов/сек).
        """
        if not Config.ETHERSCAN_API_KEY or not token_address:
            return []

        api_base = {
            "eth":  "https://api.etherscan.io/api",
            "bsc":  "https://api.bscscan.com/api",
            "arb":  "https://api.arbiscan.io/api",
        }.get(chain, "https://api.etherscan.io/api")

        start_block = 0  # упрощённо — последние транзакции
        params = {
            "module":          "account",
            "action":          "tokentx",
            "contractaddress": token_address,
            "page":            1,
            "offset":          200,
            "sort":            "desc",
            "apikey":          Config.ETHERSCAN_API_KEY,
        }

        entries = []
        try:
            session = await self._get_session()
            async with session.get(
                api_base, params=params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()

            now = time.time()
            txs  = data.get("result", [])
            if not isinstance(txs, list):
                return []

            # Агрегируем по адресам
            addr_volumes: dict[str, float] = {}
            addr_last_tx: dict[str, dict]  = {}

            for tx in txs:
                ts = float(tx.get("timeStamp", 0))
                if now - ts > hours * 3600:
                    continue

                addr = tx.get("to", "").lower()
                val  = float(tx.get("value", 0))
                decimals = int(tx.get("tokenDecimal", 18))
                amount   = val / (10 ** decimals)

                # Берём только входящие (покупки)
                if tx.get("from", "").lower() == addr:
                    continue

                addr_volumes[addr] = addr_volumes.get(addr, 0) + amount
                if addr not in addr_last_tx:
                    addr_last_tx[addr] = tx

            # Создаём записи для крупных входов
            for addr, volume in addr_volumes.items():
                tx   = addr_last_tx.get(addr, {})
                profile = self._known_wallets.get(addr)

                entry = WalletEntry(
                    address    = addr,
                    token      = token_address,
                    timestamp  = float(tx.get("timeStamp", now)),
                    amount_usd = volume,   # в токенах, не USD (нет цены)
                    tx_hash    = tx.get("hash", ""),
                    label      = profile.label if profile else "Unknown",
                    win_rate   = profile.win_rate if profile else 0,
                    via        = "transfer",
                )
                entries.append(entry)

        except Exception as e:
            logger.debug(f"Etherscan wallet tracker: {e}")

        return sorted(entries, key=lambda e: e.amount_usd, reverse=True)[:20]

    # ─── GeckoTerminal — DEX свапы по адресам ─────────────────────────────────

    async def get_dex_buyers(
        self,
        chain: str,
        pool_address: str,
        min_usd: float = 5000,
    ) -> list[WalletEntry]:
        """
        Получает покупателей конкретного пула за последний час.
        Бесплатно через GeckoTerminal.
        """
        entries = []
        try:
            session = await self._get_session()
            async with session.get(
                f"https://api.geckoterminal.com/api/v2/networks/{chain}/pools/{pool_address}/trades",
                params={"trade_volume_in_usd_greater_than": min_usd},
                headers={"Accept": "application/json;version=20230302"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()

            now = time.time()
            for trade in data.get("data", []):
                a   = trade.get("attributes", {})
                usd = float(a.get("volume_in_usd", 0) or 0)
                ts  = _parse_ts(a.get("block_timestamp", ""))

                if now - ts > 3600 or a.get("kind") != "buy":
                    continue

                addr    = a.get("tx_from_address", "").lower()
                profile = self._known_wallets.get(addr)

                entry = WalletEntry(
                    address    = addr,
                    token      = pool_address,
                    timestamp  = ts,
                    amount_usd = usd,
                    tx_hash    = a.get("tx_hash", ""),
                    label      = profile.label if profile else "Unknown",
                    win_rate   = profile.win_rate if profile else 0,
                    via        = "dex",
                )
                entries.append(entry)
                save_entry(entry)

        except Exception as e:
            logger.debug(f"GeckoTerminal DEX buyers: {e}")

        return entries

    # ─── Детекция сигнала ─────────────────────────────────────────────────────

    def detect_smart_entry(
        self,
        token: str,
        entries: list[WalletEntry],
        min_wallets: int = 2,
    ) -> Optional[WalletSignal]:
        """
        Возвращает сигнал если 2+ умных кошелька вошли в токен.
        """
        smart_entries = [
            e for e in entries
            if e.address in self._known_wallets
            and self._known_wallets[e.address].win_rate >= 0.65
        ]

        if len(smart_entries) < min_wallets:
            return None

        total_usd    = sum(e.amount_usd for e in smart_entries)
        avg_win_rate = sum(
            self._known_wallets[e.address].win_rate
            for e in smart_entries
        ) / len(smart_entries)

        strength = min(100.0,
            len(smart_entries) * 20 +
            avg_win_rate * 30 +
            min(50, total_usd / 10_000)
        )

        return WalletSignal(
            token        = token,
            timestamp    = time.time(),
            entries      = smart_entries,
            total_usd    = total_usd,
            avg_win_rate = avg_win_rate,
            signal_strength = round(strength, 1),
        )

    # ─── Построение рейтинга кошельков ───────────────────────────────────────

    async def build_wallet_rating_from_arkham(
        self,
        known_pump_tokens: list[str],
    ) -> list[WalletProfile]:
        """
        Строит рейтинг умных кошельков на основе прошлых памп-событий.

        Логика:
        1. Берём список токенов которые были пампнуты
        2. Через Arkham смотрим кто покупал ДО пампа
        3. Считаем win rate каждого адреса
        4. Сохраняем топ кошельков в БД
        """
        if not Config.ARKHAM_API_KEY:
            logger.warning("ARKHAM_API_KEY не задан — рейтинг кошельков недоступен")
            return []

        addr_stats: dict[str, dict] = {}

        for token_addr in known_pump_tokens[:20]:   # ограничиваем запросы
            try:
                session = await self._get_session()
                since   = int(time.time()) - 30 * 86400   # 30 дней

                async with session.get(
                    "https://api.arkhamintelligence.com/transfers",
                    params={
                        "base":    token_addr,
                        "fromEntity": "exchange",  # купили с биржи
                        "limit":   50,
                        "timeGte": since,
                    },
                    headers={"API-Key": Config.ARKHAM_API_KEY},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    data = await r.json()

                for tx in data.get("transfers", []):
                    addr = tx.get("toAddress", {}).get("address", "").lower()
                    usd  = tx.get("unitValue", 0)
                    if not addr or usd < 5000:
                        continue
                    if addr not in addr_stats:
                        addr_stats[addr] = {
                            "label":   tx.get("toAddress", {}).get("arkhamEntity", {}).get("name", ""),
                            "entries": 0,
                            "wins":    0,
                            "total_usd": 0,
                        }
                    addr_stats[addr]["entries"] += 1
                    addr_stats[addr]["total_usd"] += usd

                await asyncio.sleep(0.5)   # rate limit

            except Exception as e:
                logger.debug(f"Arkham wallet build: {e}")

        # Создаём профили
        profiles = []
        for addr, stats in addr_stats.items():
            if stats["entries"] < 2:
                continue
            # Win rate упрощённый: если вошёл в 2+ памп-токена → умный
            win_rate = min(0.95, stats["entries"] / max(stats["entries"] + 1, 1))
            profile  = WalletProfile(
                address      = addr,
                label        = stats["label"],
                win_rate     = round(win_rate, 2),
                total_trades = stats["entries"],
                wins         = stats["entries"],
                is_smart_money = win_rate >= 0.65,
            )
            save_wallet(profile)
            self._known_wallets[addr] = profile
            profiles.append(profile)

        profiles.sort(key=lambda p: p.win_rate, reverse=True)
        logger.info(f"Рейтинг кошельков: {len(profiles)} адресов")
        return profiles[:50]

    def get_token_wallet_data(self, token: str) -> dict:
        """Возвращает сводку по умным кошелькам для скоринга"""
        entries = get_recent_entries(token, hours=48)
        smart   = [
            e for e in entries
            if e.get("win_rate", 0) >= 0.65
        ]
        return {
            "smart_wallets_entering": len(set(e["address"] for e in smart)),
            "smart_total_usd":        sum(e["amount_usd"] for e in smart),
            "entries_24h":            len(entries),
        }


def _parse_ts(ts_str: str) -> float:
    if not ts_str:
        return 0
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0
