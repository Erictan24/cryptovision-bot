"""
scalp_trade_journal.py — Trade history database untuk self-learning.

Foundation untuk adaptive learning:
  - Log setiap trade yang closed dengan features lengkap
  - Query stats per coin, per session, per quality, per direction
  - Hitung WR, EV, avg hold time, streak
  - Bot bisa belajar dari sini dan adapt threshold-nya

Database: SQLite di data/scalp_trades.db
"""

import os
import sqlite3
import logging
import json
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = 'data/scalp_trades.db'


def _ensure_db():
    """Create database dan table kalau belum ada."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            quality TEXT NOT NULL,

            -- Entry context
            entry_price REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            tp3 REAL,

            -- Features at entry
            score INTEGER,
            kills INTEGER,
            trend_state TEXT,
            trend_strength INTEGER,
            pullback_quality TEXT,
            rsi REAL,
            adx REAL,
            adx_1h REAL,
            session TEXT,
            hour_utc INTEGER,
            day_of_week INTEGER,

            -- Volume/volatility context
            bb_width_pct REAL,
            volume_ratio REAL,
            atr_pct REAL,

            -- Outcome
            outcome TEXT,
            pnl_r REAL,
            bars_to_outcome INTEGER,
            closed_timestamp TEXT,

            -- Metadata
            engine_version TEXT,
            created_at REAL DEFAULT (strftime('%s', 'now'))
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_symbol ON trades(symbol)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_session ON trades(session)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_outcome ON trades(outcome)
    """)
    conn.commit()
    conn.close()


def record_trade(trade: dict) -> int:
    """
    Log satu trade ke database.

    trade dict harus punya:
      timestamp, symbol, direction, quality, entry_price, sl, tp1/2/3,
      score, kills, trend_state, trend_strength, pullback_quality,
      rsi, adx, adx_1h, session, hour_utc, day_of_week,
      bb_width_pct, volume_ratio, atr_pct,
      outcome, pnl_r, bars_to_outcome, closed_timestamp, engine_version

    Returns: trade ID
    """
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    fields = [
        'timestamp', 'symbol', 'direction', 'quality',
        'entry_price', 'sl', 'tp1', 'tp2', 'tp3',
        'score', 'kills', 'trend_state', 'trend_strength',
        'pullback_quality', 'rsi', 'adx', 'adx_1h',
        'session', 'hour_utc', 'day_of_week',
        'bb_width_pct', 'volume_ratio', 'atr_pct',
        'outcome', 'pnl_r', 'bars_to_outcome',
        'closed_timestamp', 'engine_version',
    ]
    placeholders = ', '.join(['?'] * len(fields))
    field_names = ', '.join(fields)
    values = [trade.get(f) for f in fields]

    cur.execute(
        f"INSERT INTO trades ({field_names}) VALUES ({placeholders})",
        values)
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def bulk_insert_trades(trades: list) -> int:
    """Insert banyak trades sekaligus (untuk backtest)."""
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    fields = [
        'timestamp', 'symbol', 'direction', 'quality',
        'entry_price', 'sl', 'tp1', 'tp2', 'tp3',
        'score', 'kills', 'trend_state', 'trend_strength',
        'pullback_quality', 'rsi', 'adx', 'adx_1h',
        'session', 'hour_utc', 'day_of_week',
        'bb_width_pct', 'volume_ratio', 'atr_pct',
        'outcome', 'pnl_r', 'bars_to_outcome',
        'closed_timestamp', 'engine_version',
    ]
    placeholders = ', '.join(['?'] * len(fields))
    field_names = ', '.join(fields)
    sql = f"INSERT INTO trades ({field_names}) VALUES ({placeholders})"

    rows = [[t.get(f) for f in fields] for t in trades]
    cur.executemany(sql, rows)
    count = len(rows)
    conn.commit()
    conn.close()
    return count


def clear_trades(engine_version: str = None):
    """
    Clear trade history. Kalau engine_version diberikan,
    hanya hapus trade dari versi itu.
    """
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if engine_version:
        cur.execute("DELETE FROM trades WHERE engine_version = ?",
                    (engine_version,))
    else:
        cur.execute("DELETE FROM trades")
    conn.commit()
    conn.close()


def get_coin_stats(symbol: str, min_trades: int = 5) -> Optional[dict]:
    """
    Hitung stats per coin.

    Returns dict dengan:
      n_trades, wr, ev_r, total_pnl_r, long_wr, short_wr,
      avg_score, last_10_wr
    """
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT direction, outcome, pnl_r, score
        FROM trades
        WHERE symbol = ?
        ORDER BY id DESC
    """, (symbol,))
    rows = cur.fetchall()
    conn.close()

    if len(rows) < min_trades:
        return None

    n = len(rows)
    wins = sum(1 for r in rows if r[2] > 0)
    total_pnl = sum(r[2] for r in rows)
    longs = [r for r in rows if r[0] == 'LONG']
    shorts = [r for r in rows if r[0] == 'SHORT']

    last_10 = rows[:10]
    last_10_wins = sum(1 for r in last_10 if r[2] > 0)

    return {
        'n_trades': n,
        'wr': wins / n * 100,
        'ev_r': total_pnl / n,
        'total_pnl_r': total_pnl,
        'long_wr': (sum(1 for r in longs if r[2] > 0) / len(longs) * 100
                    if longs else 0),
        'short_wr': (sum(1 for r in shorts if r[2] > 0) / len(shorts) * 100
                     if shorts else 0),
        'avg_score': sum(r[3] or 0 for r in rows) / n,
        'last_10_wr': last_10_wins / len(last_10) * 100 if last_10 else 0,
    }


def get_session_stats(min_trades: int = 10) -> dict:
    """
    Hitung stats per session.
    Returns: {session_name: {n, wr, ev_r, ...}}
    """
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT session, COUNT(*) as n,
               SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) as wins,
               AVG(pnl_r) as ev,
               SUM(pnl_r) as total_pnl
        FROM trades
        GROUP BY session
    """)
    rows = cur.fetchall()
    conn.close()

    result = {}
    for session, n, wins, ev, total_pnl in rows:
        if n < min_trades or session is None:
            continue
        result[session] = {
            'n_trades': n,
            'wr': wins / n * 100,
            'ev_r': ev or 0,
            'total_pnl_r': total_pnl or 0,
        }
    return result


def get_trend_state_stats() -> dict:
    """Stats per trend state (UPTREND vs DOWNTREND)."""
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT trend_state, COUNT(*) as n,
               SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) as wins,
               AVG(pnl_r) as ev
        FROM trades
        GROUP BY trend_state
    """)
    rows = cur.fetchall()
    conn.close()
    return {ts: {'n': n, 'wr': w/n*100, 'ev': ev}
            for ts, n, w, ev in rows if ts}


def get_score_histogram() -> dict:
    """
    Hitung WR per score bucket.
    Berguna untuk tau threshold optimal.
    """
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT score, pnl_r FROM trades WHERE score IS NOT NULL")
    rows = cur.fetchall()
    conn.close()

    buckets = {}
    for score, pnl in rows:
        bucket = (score // 2) * 2  # 0-1, 2-3, 4-5, 6-7, ...
        if bucket not in buckets:
            buckets[bucket] = []
        buckets[bucket].append(pnl)

    result = {}
    for bucket, pnls in buckets.items():
        if len(pnls) < 5:
            continue
        wins = sum(1 for p in pnls if p > 0)
        result[bucket] = {
            'n': len(pnls),
            'wr': wins / len(pnls) * 100,
            'ev': sum(pnls) / len(pnls),
        }
    return result


def get_pullback_quality_stats() -> dict:
    """Stats per pullback quality."""
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT pullback_quality, COUNT(*),
               SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END),
               AVG(pnl_r)
        FROM trades
        GROUP BY pullback_quality
    """)
    rows = cur.fetchall()
    conn.close()
    return {pq: {'n': n, 'wr': w/n*100 if n else 0, 'ev': ev}
            for pq, n, w, ev in rows if pq}


def count_trades(engine_version: str = None) -> int:
    """Count total trades in journal."""
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if engine_version:
        cur.execute("SELECT COUNT(*) FROM trades WHERE engine_version = ?",
                    (engine_version,))
    else:
        cur.execute("SELECT COUNT(*) FROM trades")
    result = cur.fetchone()[0]
    conn.close()
    return result


def get_recent_summary(days: int = 30) -> dict:
    """Summary trade dalam N hari terakhir."""
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).isoformat()
    cur.execute("""
        SELECT COUNT(*) as n,
               SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) as wins,
               AVG(pnl_r) as ev,
               SUM(pnl_r) as total_pnl
        FROM trades
        WHERE timestamp >= ?
    """, (since,))
    row = cur.fetchone()
    conn.close()
    n, wins, ev, total_pnl = row
    return {
        'days': days,
        'n_trades': n or 0,
        'wr': (wins / n * 100) if n else 0,
        'ev_r': ev or 0,
        'total_pnl_r': total_pnl or 0,
    }
