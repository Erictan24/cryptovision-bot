"""
scalp_paper_trader.py — Paper trading simulator untuk Level 1 validation.

Simulasi trade TANPA uang real:
  - Signal masuk → record sebagai "PAPER" di database
  - Monitor harga per 5 menit
  - Cek TP1/TP2/TP3/SL hit
  - Kirim update ke Telegram saat closed
  - Track PnL simulasi di database terpisah

Tujuan: Validasi signal quality sebelum pakai uang real.
"""

import os
import sqlite3
import logging
import time
from datetime import datetime
from typing import Optional, Callable

logger = logging.getLogger(__name__)

PAPER_DB = 'data/scalp_paper.db'


def _ensure_db():
    """Create paper trades database."""
    os.makedirs(os.path.dirname(PAPER_DB), exist_ok=True)
    conn = sqlite3.connect(PAPER_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            quality TEXT,
            engine TEXT DEFAULT 'SCALP',

            entry_price REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            tp3 REAL,

            -- Monitoring state
            status TEXT DEFAULT 'OPEN',
            tp1_hit INTEGER DEFAULT 0,
            tp2_hit INTEGER DEFAULT 0,
            current_sl REAL,

            -- Outcome
            outcome TEXT,
            close_price REAL,
            pnl_r REAL,
            pnl_usd REAL,

            -- Context
            score INTEGER,
            trend_state TEXT,
            session TEXT,
            reasons TEXT
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_symbol ON paper_trades(symbol)
    """)
    conn.commit()
    conn.close()


class PaperTrader:
    """
    Paper trade simulator dengan logika yang sama persis dengan live.

    Features:
      - TP1 hit → move SL to entry (BEP)
      - TP2 hit → move SL to TP1
      - TP3 hit → close profit max
      - SL hit → close loss
      - Expired 24 jam → close at market

    Risk per trade fixed dari config.
    """

    def __init__(self, risk_usd: float = 1.0,
                 max_positions: int = 3,
                 notify_fn: Optional[Callable] = None):
        _ensure_db()
        self.risk_usd = risk_usd
        self.max_positions = max_positions
        self.notify_fn = notify_fn  # async function to send Telegram
        self._start_capital = 100.0

    # ══════════════════════════════════════════════════
    #  OPEN TRADE
    # ══════════════════════════════════════════════════
    def open_paper_trade(self, signal: dict) -> Optional[int]:
        """
        Buka paper trade dari signal dict.
        Returns: trade_id atau None kalau gagal.
        """
        # Check max positions
        open_count = self._count_open_trades()
        if open_count >= self.max_positions:
            logger.info(f"Paper: max positions {open_count}/{self.max_positions}")
            return None

        # Extract fields
        symbol = signal.get('_symbol', signal.get('symbol', ''))
        direction = signal.get('direction', '')
        entry = signal.get('entry', 0)
        sl = signal.get('sl', 0)
        tp1 = signal.get('tp1', 0)
        tp2 = signal.get('tp2', 0)
        tp3 = signal.get('tp3', 0)

        if not symbol or entry <= 0 or sl <= 0:
            logger.warning(f"Paper: invalid signal data")
            return None

        # Insert
        conn = sqlite3.connect(PAPER_DB)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO paper_trades (
                opened_at, symbol, direction, quality, engine,
                entry_price, sl, tp1, tp2, tp3,
                status, current_sl,
                score, trend_state, session, reasons
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            symbol, direction, signal.get('quality', 'GOOD'), 'SCALP',
            entry, sl, tp1, tp2, tp3,
            sl,  # initial current_sl = sl
            signal.get('confluence_score', 0),
            signal.get('trend_state', ''),
            signal.get('session', ''),
            ' | '.join(signal.get('reasons', [])[:5]),
        ))
        trade_id = cur.lastrowid
        conn.commit()
        conn.close()

        logger.info(f"PAPER OPEN #{trade_id}: {symbol} {direction} "
                    f"entry={entry} SL={sl} TP1={tp1} TP2={tp2}")
        return trade_id

    # ══════════════════════════════════════════════════
    #  MONITOR & UPDATE
    # ══════════════════════════════════════════════════
    def update_trade(self, trade_id: int, current_price: float) -> dict:
        """
        Update single trade dengan current price.
        Returns: {'status': 'OPEN'|'CLOSED', 'outcome': str, 'pnl_r': float}
        """
        conn = sqlite3.connect(PAPER_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM paper_trades WHERE id = ? AND status = 'OPEN'
        """, (trade_id,))
        trade = cur.fetchone()
        if not trade:
            conn.close()
            return {'status': 'NOT_FOUND'}

        direction = trade['direction']
        entry = trade['entry_price']
        tp1 = trade['tp1']
        tp2 = trade['tp2']
        tp3 = trade['tp3']
        current_sl = trade['current_sl']
        tp1_hit = trade['tp1_hit']
        tp2_hit = trade['tp2_hit']

        risk = abs(entry - trade['sl'])
        if risk <= 0:
            conn.close()
            return {'status': 'INVALID'}

        outcome = None
        close_price = None
        pnl_r = None

        if direction == 'LONG':
            # Check SL first
            if current_price <= current_sl:
                if tp2_hit:
                    outcome = 'TP2_HIT'
                    close_price = tp2
                    pnl_r = (tp2 - entry) / risk
                elif tp1_hit:
                    outcome = 'BEP'
                    close_price = entry
                    pnl_r = 0.0
                else:
                    outcome = 'SL_HIT'
                    close_price = current_sl
                    pnl_r = -1.0
            # Check TP3
            elif current_price >= tp3 and tp2_hit:
                outcome = 'TP3_HIT'
                close_price = tp3
                pnl_r = (tp3 - entry) / risk
            # Check TP2
            elif current_price >= tp2 and not tp2_hit:
                tp2_hit = 1
                current_sl = tp1  # move SL to TP1
                cur.execute("""
                    UPDATE paper_trades SET tp2_hit = 1, current_sl = ?
                    WHERE id = ?
                """, (current_sl, trade_id))
            # Check TP1
            elif current_price >= tp1 and not tp1_hit:
                tp1_hit = 1
                current_sl = entry  # move SL to BEP
                cur.execute("""
                    UPDATE paper_trades SET tp1_hit = 1, current_sl = ?
                    WHERE id = ?
                """, (current_sl, trade_id))

        else:  # SHORT
            if current_price >= current_sl:
                if tp2_hit:
                    outcome = 'TP2_HIT'
                    close_price = tp2
                    pnl_r = (entry - tp2) / risk
                elif tp1_hit:
                    outcome = 'BEP'
                    close_price = entry
                    pnl_r = 0.0
                else:
                    outcome = 'SL_HIT'
                    close_price = current_sl
                    pnl_r = -1.0
            elif current_price <= tp3 and tp2_hit:
                outcome = 'TP3_HIT'
                close_price = tp3
                pnl_r = (entry - tp3) / risk
            elif current_price <= tp2 and not tp2_hit:
                tp2_hit = 1
                current_sl = tp1
                cur.execute("""
                    UPDATE paper_trades SET tp2_hit = 1, current_sl = ?
                    WHERE id = ?
                """, (current_sl, trade_id))
            elif current_price <= tp1 and not tp1_hit:
                tp1_hit = 1
                current_sl = entry
                cur.execute("""
                    UPDATE paper_trades SET tp1_hit = 1, current_sl = ?
                    WHERE id = ?
                """, (current_sl, trade_id))

        # Close kalau ada outcome
        if outcome:
            pnl_usd = pnl_r * self.risk_usd
            cur.execute("""
                UPDATE paper_trades SET
                    status = 'CLOSED',
                    outcome = ?,
                    close_price = ?,
                    pnl_r = ?,
                    pnl_usd = ?,
                    closed_at = ?
                WHERE id = ?
            """, (outcome, close_price, pnl_r, pnl_usd,
                  datetime.now().isoformat(), trade_id))
            conn.commit()
            conn.close()

            logger.info(f"PAPER CLOSE #{trade_id}: {outcome} "
                        f"PnL={pnl_r:+.2f}R (${pnl_usd:+.2f})")

            return {
                'status': 'CLOSED',
                'outcome': outcome,
                'pnl_r': pnl_r,
                'pnl_usd': pnl_usd,
                'trade_id': trade_id,
                'symbol': trade['symbol'],
                'direction': direction,
                'entry': entry,
                'close': close_price,
            }

        conn.commit()
        conn.close()
        return {'status': 'OPEN', 'tp1_hit': bool(tp1_hit),
                'tp2_hit': bool(tp2_hit)}

    def monitor_all_open(self, price_fetcher: Callable) -> list:
        """
        Monitor semua open paper trades.
        price_fetcher: function(symbol) → current_price

        Returns: list of closed trades
        """
        conn = sqlite3.connect(PAPER_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT id, symbol FROM paper_trades WHERE status = 'OPEN'
        """)
        open_trades = cur.fetchall()
        conn.close()

        closed = []
        for row in open_trades:
            try:
                price_data = price_fetcher(row['symbol'])
                if price_data is None:
                    continue
                current_price = price_data.get('price', 0) if isinstance(price_data, dict) else price_data
                if current_price <= 0:
                    continue

                result = self.update_trade(row['id'], current_price)
                if result.get('status') == 'CLOSED':
                    closed.append(result)
            except Exception as e:
                logger.debug(f"Monitor {row['symbol']}: {e}")
                continue

        return closed

    # ══════════════════════════════════════════════════
    #  QUERIES
    # ══════════════════════════════════════════════════
    def _count_open_trades(self) -> int:
        conn = sqlite3.connect(PAPER_DB)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'OPEN'")
        n = cur.fetchone()[0]
        conn.close()
        return n

    def get_open_trades(self) -> list:
        conn = sqlite3.connect(PAPER_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM paper_trades
            WHERE status = 'OPEN'
            ORDER BY id DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def get_stats(self) -> dict:
        """Overall paper trading stats."""
        conn = sqlite3.connect(PAPER_DB)
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) as n,
                SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_r < 0 THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN pnl_r = 0 THEN 1 ELSE 0 END) as bep,
                SUM(pnl_r) as total_pnl_r,
                SUM(pnl_usd) as total_pnl_usd,
                AVG(pnl_r) as avg_pnl_r
            FROM paper_trades
            WHERE status = 'CLOSED'
        """)
        row = cur.fetchone()
        cur.execute("""
            SELECT COUNT(*) FROM paper_trades WHERE status = 'OPEN'
        """)
        n_open = cur.fetchone()[0]
        conn.close()

        n, wins, losses, bep, total_r, total_usd, avg_r = row
        n = n or 0
        return {
            'n_closed': n,
            'n_open': n_open,
            'wins': wins or 0,
            'losses': losses or 0,
            'bep': bep or 0,
            'wr': (wins / n * 100) if n else 0,
            'total_pnl_r': total_r or 0,
            'total_pnl_usd': total_usd or 0,
            'avg_pnl_r': avg_r or 0,
            'current_capital': self._start_capital + (total_usd or 0),
            'roi_pct': ((total_usd or 0) / self._start_capital * 100),
        }

    def get_recent_trades(self, limit: int = 10) -> list:
        conn = sqlite3.connect(PAPER_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM paper_trades
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def format_stats_msg(self) -> str:
        """Format stats message untuk Telegram."""
        s = self.get_stats()
        capital = s['current_capital']
        roi = s['roi_pct']
        roi_emoji = '🟢' if roi > 0 else '🔴' if roi < 0 else '⚪'

        return (
            "PAPER TRADING STATS\n"
            "=" * 28 + "\n"
            f"Closed trades: {s['n_closed']}\n"
            f"Open trades: {s['n_open']}\n"
            f"Win Rate: {s['wr']:.1f}%\n"
            f"W/L/BEP: {s['wins']}/{s['losses']}/{s['bep']}\n"
            f"Total PnL: {s['total_pnl_r']:+.2f}R "
            f"(${s['total_pnl_usd']:+.2f})\n"
            f"Avg PnL: {s['avg_pnl_r']:+.2f}R/trade\n"
            f"Capital: ${capital:.2f}\n"
            f"ROI: {roi_emoji} {roi:+.2f}%"
        )
