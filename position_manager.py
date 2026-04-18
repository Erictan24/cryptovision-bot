"""
position_manager.py — Shared position tracker untuk Unified Bot.

Single source of truth untuk semua open positions dari Bot 1 (SWING) + Bot 2 (SCALP).

Fungsi:
  - Track semua open position di memory + database
  - Query: "apakah coin X sudah ada posisinya?"
  - Query: "berapa total posisi saat ini?"
  - Query: "berapa posisi ke arah LONG/SHORT?"
  - Thread-safe (kedua engine bisa akses bareng)

Digunakan oleh Signal Arbitrator untuk cegah signal tabrakan.
"""

import os
import sqlite3
import logging
import threading
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

POSITION_DB = 'data/unified_positions.db'


class PositionManager:
    """
    Thread-safe shared position tracker untuk unified bot.

    Track semua open position (live + paper) dari kedua engine.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._ensure_db()
        self._cache = {}  # {symbol: [list of position dicts]}
        self._refresh_cache()

    def _ensure_db(self):
        """Create position tracking database."""
        os.makedirs(os.path.dirname(POSITION_DB), exist_ok=True)
        conn = sqlite3.connect(POSITION_DB)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS active_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                engine TEXT NOT NULL,          -- 'SWING' | 'SCALP'
                mode TEXT NOT NULL,            -- 'LIVE' | 'PAPER'
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,       -- 'LONG' | 'SHORT'
                entry_price REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                tp3 REAL,
                risk_usd REAL,
                quality TEXT,
                opened_at TEXT NOT NULL,
                external_id TEXT,              -- bitunix order id or paper trade id
                status TEXT DEFAULT 'OPEN',    -- 'OPEN' | 'CLOSED'
                closed_at TEXT,
                outcome TEXT,
                pnl_r REAL,
                pnl_usd REAL,
                UNIQUE(engine, mode, symbol, external_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pos_status ON active_positions(status)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pos_symbol ON active_positions(symbol)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pos_engine ON active_positions(engine)
        """)
        conn.commit()
        conn.close()

    def _refresh_cache(self):
        """Reload cache dari database."""
        with self._lock:
            conn = sqlite3.connect(POSITION_DB)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM active_positions WHERE status = 'OPEN'
            """)
            self._cache = {}
            for row in cur.fetchall():
                sym = row['symbol']
                if sym not in self._cache:
                    self._cache[sym] = []
                self._cache[sym].append(dict(row))
            conn.close()

    # ══════════════════════════════════════════════════
    #  QUERIES (untuk arbitrator)
    # ══════════════════════════════════════════════════
    def has_position(self, symbol: str) -> bool:
        """Apakah coin ini sudah ada posisinya (dari engine manapun)?"""
        with self._lock:
            return symbol in self._cache and len(self._cache[symbol]) > 0

    def get_positions(self, symbol: str = None) -> list:
        """Get semua open positions, optionally filtered by symbol."""
        with self._lock:
            if symbol:
                return list(self._cache.get(symbol, []))
            all_positions = []
            for sym_positions in self._cache.values():
                all_positions.extend(sym_positions)
            return all_positions

    def count_positions(self, engine: str = None,
                        direction: str = None) -> int:
        """Count open positions dengan filter optional."""
        with self._lock:
            count = 0
            for sym_positions in self._cache.values():
                for p in sym_positions:
                    if engine and p['engine'] != engine:
                        continue
                    if direction and p['direction'] != direction:
                        continue
                    count += 1
            return count

    def get_symbol_direction(self, symbol: str) -> Optional[str]:
        """Return direction dari posisi yang sudah ada di symbol ini."""
        with self._lock:
            positions = self._cache.get(symbol, [])
            if not positions:
                return None
            # Return direction dari posisi pertama
            return positions[0]['direction']

    def has_opposite_direction(self, symbol: str, direction: str) -> bool:
        """Cek apakah ada posisi di arah berlawanan di symbol ini."""
        existing = self.get_symbol_direction(symbol)
        if existing is None:
            return False
        return existing != direction

    def count_by_direction(self, direction: str) -> int:
        """Total posisi di arah tertentu (LONG/SHORT)."""
        return self.count_positions(direction=direction)

    # ══════════════════════════════════════════════════
    #  MUTATIONS (untuk traders)
    # ══════════════════════════════════════════════════
    def open_position(self,
                      engine: str,           # 'SWING' | 'SCALP'
                      mode: str,             # 'LIVE' | 'PAPER'
                      symbol: str,
                      direction: str,
                      entry_price: float,
                      sl: float,
                      tp1: float,
                      tp2: float,
                      tp3: float = 0.0,
                      risk_usd: float = 1.0,
                      quality: str = 'GOOD',
                      external_id: str = None) -> int:
        """
        Record new open position.
        Returns: internal position ID.
        """
        with self._lock:
            conn = sqlite3.connect(POSITION_DB)
            cur = conn.cursor()
            try:
                cur.execute("""
                    INSERT INTO active_positions (
                        engine, mode, symbol, direction,
                        entry_price, sl, tp1, tp2, tp3,
                        risk_usd, quality, opened_at, external_id, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
                """, (
                    engine, mode, symbol, direction,
                    entry_price, sl, tp1, tp2, tp3,
                    risk_usd, quality,
                    datetime.now().isoformat(),
                    external_id or f'{engine}_{symbol}_{int(time.time())}',
                ))
                pos_id = cur.lastrowid
                conn.commit()
            except sqlite3.IntegrityError as e:
                logger.warning(f"Position already exists: {e}")
                pos_id = None
            finally:
                conn.close()

            if pos_id:
                self._refresh_cache()
                logger.info(f"[{engine}] POSITION OPEN #{pos_id}: "
                            f"{symbol} {direction} @ {entry_price}")
            return pos_id

    def close_position(self,
                       position_id: int,
                       outcome: str,
                       close_price: float = 0.0,
                       pnl_r: float = 0.0,
                       pnl_usd: float = 0.0):
        """Mark position as closed."""
        with self._lock:
            conn = sqlite3.connect(POSITION_DB)
            cur = conn.cursor()
            cur.execute("""
                UPDATE active_positions SET
                    status = 'CLOSED',
                    closed_at = ?,
                    outcome = ?,
                    pnl_r = ?,
                    pnl_usd = ?
                WHERE id = ?
            """, (datetime.now().isoformat(), outcome, pnl_r, pnl_usd, position_id))
            conn.commit()
            conn.close()
            self._refresh_cache()
            logger.info(f"POSITION CLOSE #{position_id}: "
                        f"{outcome} PnL={pnl_r:+.2f}R")

    def close_by_symbol(self, symbol: str, engine: str = None) -> int:
        """Close all positions untuk symbol + engine tertentu."""
        with self._lock:
            conn = sqlite3.connect(POSITION_DB)
            cur = conn.cursor()
            if engine:
                cur.execute("""
                    UPDATE active_positions SET status = 'CLOSED',
                           closed_at = ?, outcome = 'MANUAL'
                    WHERE symbol = ? AND engine = ? AND status = 'OPEN'
                """, (datetime.now().isoformat(), symbol, engine))
            else:
                cur.execute("""
                    UPDATE active_positions SET status = 'CLOSED',
                           closed_at = ?, outcome = 'MANUAL'
                    WHERE symbol = ? AND status = 'OPEN'
                """, (datetime.now().isoformat(), symbol))
            count = cur.rowcount
            conn.commit()
            conn.close()
            self._refresh_cache()
            return count

    def sync_from_exchange(self, exchange_positions: list):
        """
        Sync state dari exchange (Bitunix) — untuk live mode.
        exchange_positions: list of dict dari exchange API
        """
        # TODO: implement setelah tahu format Bitunix exchange positions
        pass

    # ══════════════════════════════════════════════════
    #  UTILITIES
    # ══════════════════════════════════════════════════
    def get_summary(self) -> dict:
        """Ringkasan state posisi."""
        with self._lock:
            total = self.count_positions()
            swing = self.count_positions(engine='SWING')
            scalp = self.count_positions(engine='SCALP')
            longs = self.count_positions(direction='LONG')
            shorts = self.count_positions(direction='SHORT')

            symbols = list(self._cache.keys())

            return {
                'total': total,
                'swing': swing,
                'scalp': scalp,
                'longs': longs,
                'shorts': shorts,
                'symbols': symbols,
            }

    def format_summary(self) -> str:
        """Format summary untuk Telegram."""
        s = self.get_summary()
        lines = [
            "Position Manager State:",
            f"  Total open: {s['total']}",
            f"  SWING: {s['swing']}, SCALP: {s['scalp']}",
            f"  LONG: {s['longs']}, SHORT: {s['shorts']}",
        ]
        if s['symbols']:
            lines.append(f"  Symbols: {', '.join(s['symbols'])}")
        return '\n'.join(lines)


# ══════════════════════════════════════════════════
#  SINGLETON
# ══════════════════════════════════════════════════
_instance = None


def get_position_manager() -> PositionManager:
    global _instance
    if _instance is None:
        _instance = PositionManager()
    return _instance
