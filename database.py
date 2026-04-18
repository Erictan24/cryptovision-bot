import sqlite3
import os
from datetime import datetime
from config import DATABASE_PATH

class DatabaseManager:
    def __init__(self):
        self.db_path = DATABASE_PATH
        # Buat folder data jika belum ada — fix untuk OperationalError
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self.init_database()

    def init_database(self):
        """Buat tabel database kalau belum ada"""
        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp      TEXT    NOT NULL,
                pair           TEXT    NOT NULL,
                direction      TEXT    NOT NULL,
                entry_price    REAL    NOT NULL,
                stop_loss      REAL    NOT NULL,
                take_profit    REAL    NOT NULL,
                position_size  REAL    NOT NULL,
                risk_amount    REAL    NOT NULL,
                risk_reward    REAL    NOT NULL,
                risk_level     TEXT    NOT NULL,
                reason         TEXT,
                status         TEXT    DEFAULT 'open',
                exit_price     REAL,
                exit_timestamp TEXT,
                pnl            REAL,
                pnl_percent    REAL,
                created_at     TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        conn.close()

    def save_trade(self, trade_data):
        """Simpan sinyal trade baru"""
        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO trades (
                timestamp, pair, direction, entry_price, stop_loss,
                take_profit, position_size, risk_amount, risk_reward,
                risk_level, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade_data['timestamp'],
            trade_data['pair'],
            trade_data['direction'],
            trade_data['entry_price'],
            trade_data['stop_loss'],
            trade_data['take_profit'],
            trade_data['position_size'],
            trade_data['risk_amount'],
            trade_data['risk_reward'],
            trade_data['risk_level'],
            trade_data['reason']
        ))

        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return trade_id

    def update_trade_exit(self, trade_id, exit_data):
        """Update trade yang sudah ditutup (hit SL atau TP)"""
        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE trades
            SET status = ?, exit_price = ?, exit_timestamp = ?,
                pnl = ?, pnl_percent = ?
            WHERE id = ?
        ''', (
            exit_data['status'],
            exit_data['exit_price'],
            exit_data['exit_timestamp'],
            exit_data['pnl'],
            exit_data['pnl_percent'],
            trade_id
        ))

        conn.commit()
        conn.close()

    def get_open_trades(self):
        """Ambil semua trade yang masih terbuka"""
        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM trades WHERE status = "open"')
        trades = cursor.fetchall()
        conn.close()
        return trades

    def get_all_trades(self, limit=100):
        """Ambil riwayat trade terbaru"""
        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?', (limit,))
        trades = cursor.fetchall()
        conn.close()
        return trades

    def get_performance_stats(self):
        """Hitung statistik performa keseluruhan"""
        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM trades WHERE status IN ("win", "loss")')
        trades = cursor.fetchall()
        conn.close()

        if not trades:
            return None

        total_trades   = len(trades)
        winning_trades = len([t for t in trades if t[15] and t[15] > 0])
        losing_trades  = total_trades - winning_trades
        win_rate       = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        total_pnl      = sum([t[15] for t in trades if t[15] is not None])

        wins   = [t[15] for t in trades if t[15] and t[15] > 0]
        losses = [abs(t[15]) for t in trades if t[15] and t[15] < 0]

        avg_win       = sum(wins)   / len(wins)   if wins   else 0
        avg_loss      = sum(losses) / len(losses) if losses else 0
        profit_factor = sum(wins)   / sum(losses) if losses and sum(losses) > 0 else 0

        return {
            'total_trades'  : total_trades,
            'winning_trades': winning_trades,
            'losing_trades' : losing_trades,
            'win_rate'      : round(win_rate, 2),
            'total_pnl'     : round(total_pnl, 2),
            'avg_win'       : round(avg_win, 2),
            'avg_loss'      : round(avg_loss, 2),
            'profit_factor' : round(profit_factor, 2)
        }
