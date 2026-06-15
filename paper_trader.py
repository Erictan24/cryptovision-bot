"""
paper_trader.py — Paper Trading Mode (uang palsu, nol risiko real money)
========================================================================
Dibuat 2026-06-15. Forward-test bot Aggressive Mode tanpa duit asli.

Desain:
  - PaperTrader(BitunixTrader) — subclass, jadi semua helper READ-ONLY
    (_get_current_price, round_qty, get_min_qty, _tg_send) tetap pakai
    Bitunix asli via inheritance. NOL order dikirim ke exchange.
  - Override HANYA method yang nyentuh duit:
      place_order, get_positions, get_open_position, has_pending_order,
      sync_daily_loss_from_exchange, _is_daily_loss_exceeded,
      is_circuit_breaker_active, start_tp1_monitor, resume_monitors_on_startup
  - Trade dicatat ke data/paper_trades.db (SQLite).
  - 1 monitor thread poll harga real tiap 30s, terapin stage trailing
    PERSIS kayak bitunix_trader.py:
       TP1 hit  → SL ke BEP (entry)
       +1.5R    → SL ke +0.5R  (stage 2)
       +2.0R    → SL ke +1.0R  (stage 3)
       TP2 hit  → close sisa
  - Fill model: fill-on-touch. LIMIT order nunggu harga nyentuh entry baru
    jadi OPEN. Kalau gak kena dalam FILL_EXPIRY_HOURS jam → EXPIRED.

Aktifkan: set env PAPER_TRADING=1, lalu restart bot.
"""
import os
import time
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional

from bitunix_trader import BitunixTrader

logger = logging.getLogger(__name__)

DB_PATH = os.path.join('data', 'paper_trades.db')


class PaperTrader(BitunixTrader):
    """Drop-in pengganti BitunixTrader untuk paper trading."""

    FILL_EXPIRY_HOURS = 4   # LIMIT yang gak kena dalam 4 jam → batal
    POLL_INTERVAL     = 30  # detik

    def __init__(self):
        super().__init__()
        self.paper = True
        self._db_path = DB_PATH
        self._db_lock = threading.Lock()
        self._monitor_started = False
        self._init_db()
        logger.info("🧪 PaperTrader AKTIF — uang palsu, nol risiko real money")
        # Notif Telegram sekali saat start
        try:
            self._tg_send(
                "🧪 PAPER TRADING MODE AKTIF\n"
                "=" * 28 + "\n"
                "Semua trade UANG PALSU. Tidak ada order dikirim ke Bitunix.\n"
                "SL/TP dilacak otomatis dari harga real. Cek /paper kapan aja."
            )
        except Exception:
            pass

    # ── is_ready: cukup punya keys buat fetch harga (read-only) ──────
    @property
    def is_ready(self) -> bool:
        return bool(self.api_key and self.secret_key)

    # ── DB helpers ───────────────────────────────────────────────────
    def _conn(self):
        c = sqlite3.connect(self._db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        os.makedirs('data', exist_ok=True)
        with self._db_lock:
            c = self._conn()
            try:
                c.execute('''CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, direction TEXT, quality TEXT, score REAL,
                entry REAL, sl_initial REAL, tp1 REAL, tp2 REAL,
                qty REAL, risk_usd REAL,
                status TEXT, outcome TEXT,
                tp1_hit INTEGER DEFAULT 0, tp2_hit INTEGER DEFAULT 0,
                bep_done INTEGER DEFAULT 0, stage2_done INTEGER DEFAULT 0,
                stage3_done INTEGER DEFAULT 0,
                    cur_sl REAL, exit_price REAL, pnl_r REAL, pnl_usd REAL,
                    created_at TEXT, opened_at TEXT, closed_at TEXT, expire_at TEXT
                )''')
                c.commit()
            finally:
                c.close()

    def _exec(self, sql, params=()):
        with self._db_lock:
            c = self._conn()
            try:
                cur = c.execute(sql, params)
                c.commit()
                return cur.lastrowid
            finally:
                c.close()

    def _query(self, sql, params=()):
        with self._db_lock:
            c = self._conn()
            try:
                return c.execute(sql, params).fetchall()
            finally:
                c.close()

    def _update(self, trade_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE paper_trades SET {cols} WHERE id=?",
                   tuple(fields.values()) + (trade_id,))

    @staticmethod
    def _clean(symbol: str) -> str:
        return symbol.upper().replace('/USDT', '').replace('USDT', '')

    @staticmethod
    def _sym(symbol: str) -> str:
        return PaperTrader._clean(symbol) + 'USDT'

    # ── Override: state read dari DB paper, bukan exchange ───────────
    def get_positions(self) -> list:
        rows = self._query("SELECT symbol,direction,qty FROM paper_trades WHERE status='OPEN'")
        return [{'symbol': r['symbol'], 'side': r['direction'], 'qty': r['qty']} for r in rows]

    def get_open_position(self, symbol: str) -> Optional[dict]:
        sym = self._sym(symbol)
        rows = self._query("SELECT symbol,direction,qty FROM paper_trades WHERE status='OPEN' AND symbol=?", (sym,))
        if rows:
            r = rows[0]
            return {'symbol': r['symbol'], 'side': r['direction'], 'qty': r['qty']}
        return None

    def has_pending_order(self, symbol: str) -> bool:
        sym = self._sym(symbol)
        rows = self._query("SELECT id FROM paper_trades WHERE status='PENDING' AND symbol=?", (sym,))
        return bool(rows)

    # ── Override: no real-money guards ───────────────────────────────
    def sync_daily_loss_from_exchange(self):
        return  # paper: gak ada exchange sync

    def _is_daily_loss_exceeded(self) -> bool:
        # Faithful: jumlah PnL paper yang close hari ini vs max_daily_loss
        today = datetime.now().strftime('%Y-%m-%d')
        rows = self._query(
            "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM paper_trades "
            "WHERE status='CLOSED' AND closed_at LIKE ?", (today + '%',))
        loss = rows[0]['s'] if rows else 0
        try:
            limit = float(self.max_daily_loss_usd)
        except Exception:
            limit = 99999
        return loss <= -abs(limit)

    def is_circuit_breaker_active(self) -> bool:
        return False  # paper: gak pakai circuit breaker (Aggressive Mode)

    # ── Override: paper monitor handle TP1/TP2/SL sendiri ────────────
    def start_tp1_monitor(self, *args, **kwargs):
        # No-op: _monitor_loop sudah handle semua posisi paper.
        self._ensure_monitor()

    def resume_monitors_on_startup(self, notify_fn=None):
        # Restart bot → lanjutin monitor untuk posisi PENDING/OPEN yang ada
        self._ensure_monitor()
        rows = self._query("SELECT COUNT(*) AS n FROM paper_trades WHERE status IN ('PENDING','OPEN')")
        n = rows[0]['n'] if rows else 0
        logger.info(f"🧪 Paper resume: {n} posisi PENDING/OPEN dilanjutkan")

    # ── place_order: catat paper trade, NOL order ke Bitunix ─────────
    def place_order(self, symbol: str, direction: str,
                    entry: float, sl: float, tp1: float, tp2: float,
                    qty: Optional[float] = None,
                    quality: str = 'GOOD',
                    signal_data: dict = None,
                    btc_state: str = 'NEUTRAL',
                    notify_fn=None) -> dict:
        if not self.is_ready:
            return {'ok': False, 'msg': 'Paper trade tidak aktif (keys missing)'}

        sym = self._sym(symbol)

        # Guard: daily loss / max pos / dedupe (versi paper)
        if self._is_daily_loss_exceeded():
            return {'ok': False, 'msg': 'Daily loss limit tercapai (paper)'}
        if len(self.get_positions()) >= self.max_positions:
            return {'ok': False, 'msg': f'Max {self.max_positions} posisi paper terbuka'}
        if self.get_open_position(symbol) or self.has_pending_order(symbol):
            return {'ok': False, 'msg': f'Sudah ada paper order {sym} — skip'}

        # ── Hitung qty PERSIS kayak bitunix_trader.place_order ───────
        if entry == 0:
            entry_for_calc = self._get_current_price(sym)
            if entry_for_calc <= 0:
                return {'ok': False, 'msg': 'Gagal ambil current price untuk qty calc'}
        else:
            entry_for_calc = entry

        risk_per_unit = abs(entry_for_calc - sl)
        if risk_per_unit <= 0:
            return {'ok': False, 'msg': 'SL sama dengan entry — invalid'}

        risk_amount = self.risk_usd if self.risk_usd > 0 else 1.0
        raw_qty = risk_amount / risk_per_unit
        qty_v = self.round_qty(raw_qty, symbol)
        min_qty, _ = self.get_min_qty(symbol)
        if qty_v <= 0:
            return {'ok': False, 'msg': f'Qty terlalu kecil: {qty_v}'}
        if qty_v < min_qty:
            return {'ok': False, 'msg': f'Qty {qty_v} di bawah minimum {min_qty} untuk {sym}'}

        use_market = (entry == 0)
        eff_entry  = entry_for_calc  # MARKET fill di current price
        score      = (signal_data or {}).get('confluence_score', 0)
        now        = datetime.now()
        status     = 'OPEN' if use_market else 'PENDING'
        opened_at  = now.isoformat() if use_market else None
        expire_at  = (now + timedelta(hours=self.FILL_EXPIRY_HOURS)).isoformat()

        self._exec(
            '''INSERT INTO paper_trades
               (symbol,direction,quality,score,entry,sl_initial,tp1,tp2,qty,risk_usd,
                status,cur_sl,created_at,opened_at,expire_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (sym, direction, quality, score, eff_entry, sl, tp1, tp2, qty_v, risk_amount,
             status, sl, now.isoformat(), opened_at, expire_at))

        logger.info(f"🧪 PAPER {status} {sym} {direction} [{quality}] "
                    f"entry={eff_entry} sl={sl} tp1={tp1} tp2={tp2} qty={qty_v} risk=${risk_amount}")
        self._ensure_monitor()
        return {'ok': True, 'order_type': 'MARKET' if use_market else 'LIMIT', 'paper': True}

    # ── Monitor loop ─────────────────────────────────────────────────
    def _ensure_monitor(self):
        if self._monitor_started:
            return
        self._monitor_started = True
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        logger.info(f"👁️ Paper monitor loop START (poll {self.POLL_INTERVAL}s)")

    def _monitor_loop(self):
        while True:
            try:
                self._tick()
            except Exception as e:
                logger.debug(f"paper monitor tick err: {e}")
            time.sleep(self.POLL_INTERVAL)

    def _tick(self):
        rows = self._query("SELECT * FROM paper_trades WHERE status IN ('PENDING','OPEN')")
        price_cache = {}
        for t in rows:
            sym = t['symbol']
            if sym not in price_cache:
                price_cache[sym] = self._get_current_price(sym)
            px = price_cache[sym]
            if px <= 0:
                continue
            if t['status'] == 'PENDING':
                self._handle_pending(t, px)
            else:
                self._handle_open(t, px)

    def _handle_pending(self, t, px):
        is_long = t['direction'] == 'LONG'
        entry   = t['entry']
        now_iso = datetime.now().isoformat()
        # Fill-on-touch: LONG limit fill saat harga turun <= entry,
        # SHORT limit fill saat harga naik >= entry.
        filled = (is_long and px <= entry) or (not is_long and px >= entry)
        if filled:
            self._update(t['id'], status='OPEN', opened_at=now_iso)
            logger.info(f"🧪 PAPER FILL {t['symbol']} {t['direction']} @ {entry} (px={px})")
            self._tg_send(
                f"🧪 [PAPER] LIMIT FILLED — {t['symbol']} {t['direction']}\n"
                f"   Entry: {entry}\n   Monitor TP1/TP2/SL aktif")
        elif now_iso > (t['expire_at'] or now_iso):
            self._update(t['id'], status='EXPIRED', closed_at=now_iso)
            logger.info(f"🧪 PAPER EXPIRED {t['symbol']} — limit gak kena dalam {self.FILL_EXPIRY_HOURS}h")

    def _handle_open(self, t, px):
        is_long    = t['direction'] == 'LONG'
        entry      = t['entry']
        sl_initial = t['sl_initial']
        tp1        = t['tp1']
        tp2        = t['tp2']
        cur_sl     = t['cur_sl']
        tp1_hit    = bool(t['tp1_hit'])
        tp2_hit    = bool(t['tp2_hit'])
        bep_done   = bool(t['bep_done'])
        stage2_done = bool(t['stage2_done'])
        stage3_done = bool(t['stage3_done'])
        risk_dist  = abs(entry - sl_initial)
        sym        = t['symbol']
        changed    = {}

        # ── Stage 1: TP1 kena → SL ke BEP ────────────────────────────
        if not tp1_hit:
            hit = (is_long and px >= tp1) or (not is_long and px <= tp1)
            if hit:
                tp1_hit = bep_done = True
                cur_sl = entry
                changed.update(tp1_hit=1, bep_done=1, cur_sl=entry)
                logger.info(f"🧪 PAPER TP1 {sym} @ {px} — SL ke BEP {entry}")
                self._tg_send(
                    f"🎯 [PAPER] TP1 KENA — {sym}\n"
                    f"   Harga: {round(px,8)}\n   BEP: {round(entry,8)} (50% profit aman)")

        # ── Detect TP2 reached ───────────────────────────────────────
        if not tp2_hit:
            if (is_long and px >= tp2) or (not is_long and px <= tp2):
                tp2_hit = True
                changed['tp2_hit'] = 1

        # ── Stage 2: +1.5R → SL ke +0.5R ─────────────────────────────
        if bep_done and not stage2_done and risk_dist > 0:
            trig2 = entry + risk_dist * 1.5 if is_long else entry - risk_dist * 1.5
            lock2 = entry + risk_dist * 0.5 if is_long else entry - risk_dist * 0.5
            if (is_long and px >= trig2) or (not is_long and px <= trig2):
                stage2_done = True
                cur_sl = lock2
                changed.update(stage2_done=1, cur_sl=lock2)
                logger.info(f"🧪 PAPER Stage2 {sym} @ {px} — SL ke +0.5R ({lock2:.6g})")
                self._tg_send(
                    f"📈 [PAPER] TRAILING SL STAGE 2 — {sym}\n"
                    f"   SL baru: {round(lock2,8)} (+0.5R terkunci)")

        # ── Stage 3: +2.0R → SL ke +1.0R ─────────────────────────────
        if stage2_done and not stage3_done and risk_dist > 0:
            trig3 = entry + risk_dist * 2.0 if is_long else entry - risk_dist * 2.0
            lock3 = entry + risk_dist * 1.0 if is_long else entry - risk_dist * 1.0
            if (is_long and px >= trig3) or (not is_long and px <= trig3):
                stage3_done = True
                cur_sl = lock3
                changed.update(stage3_done=1, cur_sl=lock3)
                logger.info(f"🧪 PAPER Stage3 {sym} @ {px} — SL ke +1.0R ({lock3:.6g})")
                self._tg_send(
                    f"🚀 [PAPER] TRAILING SL STAGE 3 — {sym}\n"
                    f"   SL baru: {round(lock3,8)} (+1R terkunci penuh)")

        # ── Exit checks: TP2 atau SL ─────────────────────────────────
        tp2_reached = (is_long and px >= tp2) or (not is_long and px <= tp2)
        sl_breached = (is_long and px <= cur_sl) or (not is_long and px >= cur_sl)

        if tp2_reached:
            self._close(t, exit_price=tp2, tp1_hit=tp1_hit, tp2_hit=True)
            return
        if sl_breached:
            self._close(t, exit_price=cur_sl, tp1_hit=tp1_hit, tp2_hit=tp2_hit)
            return

        if changed:
            self._update(t['id'], **changed)

    def _close(self, t, exit_price, tp1_hit, tp2_hit):
        is_long    = t['direction'] == 'LONG'
        entry      = t['entry']
        sl_initial = t['sl_initial']
        tp1        = t['tp1']
        risk_dist  = abs(entry - sl_initial)
        risk_usd   = t['risk_usd']
        sign       = 1 if is_long else -1

        if risk_dist <= 0:
            pnl_r = 0.0
        elif tp1_hit:
            # 50% qty close di TP1, 50% di exit_price
            r_tp1  = sign * (tp1 - entry) / risk_dist * 0.5
            r_rest = sign * (exit_price - entry) / risk_dist * 0.5
            pnl_r  = r_tp1 + r_rest
        else:
            # full qty close di exit (SL sebelum TP1)
            pnl_r = sign * (exit_price - entry) / risk_dist

        pnl_usd = pnl_r * risk_usd

        # Outcome label — ikut konvensi bot (tp2_hit > tp1_hit > BEP > SL)
        if tp2_hit:
            outcome = 'TP2'
        elif tp1_hit:
            outcome = 'BEP' if abs(exit_price - entry) < risk_dist * 0.01 else 'TP1'
        else:
            outcome = 'SL'

        now_iso = datetime.now().isoformat()
        self._update(t['id'], status='CLOSED', outcome=outcome,
                     tp1_hit=int(tp1_hit), tp2_hit=int(tp2_hit),
                     exit_price=exit_price, pnl_r=round(pnl_r, 4),
                     pnl_usd=round(pnl_usd, 4), closed_at=now_iso, cur_sl=t['cur_sl'])

        ico = "✅" if pnl_r > 0 else ("➖" if outcome == 'BEP' else "❌")
        logger.info(f"🧪 PAPER CLOSE {t['symbol']} {outcome} @ {exit_price} "
                    f"PnL {pnl_r:+.2f}R (${pnl_usd:+.2f})")
        self._tg_send(
            f"{ico} [PAPER] CLOSED — {t['symbol']} {t['direction']}\n"
            f"   Outcome: {outcome}\n"
            f"   Exit: {round(exit_price,8)}\n"
            f"   PnL: {pnl_r:+.2f}R (${pnl_usd:+.2f})")

    # ── Stats untuk /paper command ───────────────────────────────────
    def paper_stats(self) -> dict:
        closed = self._query(
            "SELECT outcome,direction,quality,pnl_r,pnl_usd FROM paper_trades WHERE status='CLOSED'")
        n = len(closed)
        wins = sum(1 for r in closed if r['pnl_r'] > 0)
        total_r = sum(r['pnl_r'] for r in closed)
        total_usd = sum(r['pnl_usd'] for r in closed)
        open_n = self._query("SELECT COUNT(*) AS n FROM paper_trades WHERE status='OPEN'")[0]['n']
        pend_n = self._query("SELECT COUNT(*) AS n FROM paper_trades WHERE status='PENDING'")[0]['n']
        exp_n  = self._query("SELECT COUNT(*) AS n FROM paper_trades WHERE status='EXPIRED'")[0]['n']

        by_outcome = {}
        for r in closed:
            by_outcome[r['outcome']] = by_outcome.get(r['outcome'], 0) + 1

        return {
            'closed': n,
            'wins': wins,
            'wr': round(wins / n * 100, 1) if n else 0.0,
            'total_r': round(total_r, 2),
            'total_usd': round(total_usd, 2),
            'open': open_n,
            'pending': pend_n,
            'expired': exp_n,
            'by_outcome': by_outcome,
        }
