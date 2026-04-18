"""
historical_trainer.py — Bot belajar dari data historis (1-2 tahun ke belakang)

Cara kerja:
  1. Ambil OHLCV 1h + 4h untuk semua SCAN_POOL coins dari Binance (gratis)
  2. Replay candle per candle (sliding window 150 candle)
  3. Setiap window: deteksi signal + kondisi pasar (RSI, EMA, struktur, dll)
  4. Simulasi ke depan: apakah price ke TP1 dulu atau SL?
  5. Catat semua ke learning_engine.db
  6. Jalankan auto_tune() → threshold ter-update otomatis

Dijalankan via:
  python historical_trainer.py --days 365
  python historical_trainer.py --days 730 --coins ETH XRP
  atau via Telegram: /train
"""

import os
import sys
import time
import logging
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Pastikan bisa import dari root
sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger(__name__)

# ── Konstanta ──────────────────────────────────────────────────
WINDOW       = 150    # jumlah candle per window analisa
MIN_WINDOW   = 80     # minimal candle sebelum mulai analisa
STEP         = 3      # geser 3 candle per iterasi (efisiensi)
MAX_HOLD     = 48     # maksimal candle ke depan untuk tunggu TP/SL
MIN_RR       = 2.0    # minimum RR — naik dari 1.5, hanya setup dengan ruang cukup
SL_ATR_MULT  = 1.2    # SL = low/high zone ± ATR * 1.2
TP1_ATR_MULT = 2.0    # TP1 = entry + ATR * 2.0  (50% qty)
TP2_ATR_MULT = 3.5    # TP2 = entry + ATR * 3.5  (50% qty)
TP_ATR_MULT  = TP1_ATR_MULT  # alias untuk kompatibilitas

# Minimum score untuk signal yang dicatat — hanya GOOD, bukan MODERATE/WAIT
MIN_QUALITY_SCORE = 6   # score < 6 = sinyal lemah, skip (naik dari 4)
MIN_QUALITY_KILLS = 1   # max 1 kill — ada celah tapi tidak lemah total

# Risk management tetap — sama dengan .env
RISK_USD     = 3.0    # $ yang di-risk per trade (TRADE_RISK_USD)


# ══════════════════════════════════════════════════════════════
# HELPER — kalkulasi indikator murni (tanpa API call)
# ══════════════════════════════════════════════════════════════

def _calc_ema(closes: np.ndarray, period: int) -> np.ndarray:
    ema = np.zeros_like(closes)
    ema[0] = closes[0]
    k = 2 / (period + 1)
    for i in range(1, len(closes)):
        ema[i] = closes[i] * k + ema[i-1] * (1 - k)
    return ema


def _calc_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    delta = np.diff(closes[-period*2:])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    ag = np.mean(gain[-period:]) if len(gain) >= period else 0.001
    al = np.mean(loss[-period:]) if len(loss) >= period else 0.001
    rs = ag / max(al, 1e-10)
    return float(100 - 100 / (1 + rs))


def _calc_atr(highs, lows, closes, period: int = 14) -> float:
    if len(closes) < period + 1:
        return float(np.mean(np.array(highs) - np.array(lows)))
    tr = []
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i-1]),
                      abs(lows[i] - closes[i-1])))
    return float(np.mean(tr[-period:]))


def _detect_structure(highs: np.ndarray, lows: np.ndarray, window: int = 10) -> str:
    """Deteksi UPTREND/DOWNTREND/SIDEWAYS dari HH/HL atau LH/LL."""
    if len(highs) < window * 2:
        return 'SIDEWAYS'

    # Bagi jadi dua bagian: lebih lama vs lebih baru
    mid = len(highs) // 2
    h1  = np.max(highs[:mid])
    h2  = np.max(highs[mid:])
    l1  = np.min(lows[:mid])
    l2  = np.min(lows[mid:])

    if h2 > h1 and l2 > l1:
        return 'UPTREND'    # HH dan HL
    elif h2 < h1 and l2 < l1:
        return 'DOWNTREND'  # LH dan LL
    else:
        return 'SIDEWAYS'


def _detect_ema_trend(c: float, e8: float, e21: float, e50: float) -> str:
    if e8 > e21 > e50 and c > e8:
        return 'STRONG_UP'
    elif e8 > e21 and c > e8:
        return 'UP'
    elif e8 > e21:
        return 'WEAK_UP'
    elif e8 < e21 < e50 and c < e8:
        return 'STRONG_DOWN'
    elif e8 < e21 and c < e8:
        return 'DOWN'
    elif e8 < e21:
        return 'WEAK_DOWN'
    return 'SIDEWAYS'


def _detect_support_resistance(df: pd.DataFrame, atr: float):
    """
    Cari support dan resistance terdekat dari swing high/low.
    Return: (support_price, resistance_price) atau (None, None)
    """
    highs  = df['high'].values
    lows   = df['low'].values
    closes = df['close'].values
    price  = closes[-1]
    win    = min(5, len(df) // 4)

    # Swing highs
    resistances = []
    for i in range(win, len(highs) - win):
        if all(highs[i] >= highs[i-j] for j in range(1, win+1)) and \
           all(highs[i] >= highs[i+j] for j in range(1, win+1)):
            if highs[i] > price:
                resistances.append(highs[i])

    # Swing lows
    supports = []
    for i in range(win, len(lows) - win):
        if all(lows[i] <= lows[i-j] for j in range(1, win+1)) and \
           all(lows[i] <= lows[i+j] for j in range(1, win+1)):
            if lows[i] < price:
                supports.append(lows[i])

    margin   = atr * 0.8
    near_sup = None
    near_res = None

    if supports:
        nearest_sup = max(s for s in supports if s < price + margin)
        if abs(nearest_sup - price) < atr * 1.5:
            near_sup = nearest_sup

    if resistances:
        nearest_res = min(r for r in resistances if r > price - margin)
        if abs(nearest_res - price) < atr * 1.5:
            near_res = nearest_res

    return near_sup, near_res


def _score_simple(direction: str, rsi: float, ema_trend: str,
                  structure: str, htf_ema: str, atr: float) -> tuple:
    """
    Simplified scorer untuk training historis.
    Tidak sepenuhnya sama dengan analyze_coin (tidak ada SMC/OB/FVG),
    tapi menangkap faktor utama yang menentukan WR.
    Return: (score, kills)
    """
    score = 0
    kills = []
    is_long = direction == 'LONG'

    # EMA trend
    if ema_trend in ('STRONG_UP', 'UP') if is_long else ema_trend in ('STRONG_DOWN', 'DOWN'):
        score += 2
    elif ema_trend in ('WEAK_UP',) if is_long else ema_trend in ('WEAK_DOWN',):
        score += 1
    elif ema_trend in ('STRONG_DOWN', 'DOWN') if is_long else ema_trend in ('STRONG_UP', 'UP'):
        kills.append('EMA melawan')

    # Struktur
    if structure == ('UPTREND' if is_long else 'DOWNTREND'):
        score += 2
    elif structure == ('DOWNTREND' if is_long else 'UPTREND'):
        kills.append('Struktur melawan')

    # HTF EMA
    if htf_ema in ('STRONG_UP', 'UP') if is_long else htf_ema in ('STRONG_DOWN', 'DOWN'):
        score += 2
    elif htf_ema in ('STRONG_DOWN', 'DOWN') if is_long else htf_ema in ('STRONG_UP', 'UP'):
        kills.append('HTF EMA melawan')

    # RSI
    if (rsi <= 30 and is_long) or (rsi >= 70 and not is_long):
        score += 3
    elif (rsi <= 40 and is_long) or (rsi >= 60 and not is_long):
        score += 1
    elif (rsi >= 70 and is_long) or (rsi <= 30 and not is_long):
        kills.append(f'RSI {rsi:.0f} melawan')
    elif (rsi >= 62 and is_long) or (rsi <= 38 and not is_long):
        score -= 2

    return score, kills


def _get_session(ts: pd.Timestamp) -> str:
    """Tentukan sesi trading dari timestamp UTC."""
    h = ts.hour
    if 13 <= h < 16:  return 'OVERLAP'
    if 7  <= h < 16:  return 'LONDON'
    if 16 <= h < 21:  return 'NY'
    if 0  <= h < 7:   return 'ASIA'
    return 'DEAD'


def _simulate_outcome(df_future: pd.DataFrame, entry: float,
                      sl: float, tp1: float, tp2: float, direction: str) -> str:
    """
    Simulasi dua tahap:
      Tahap 1: cek TP1 vs SL
      Tahap 2: kalau TP1 kena, cek TP2 vs BE (entry = break-even)
    Return: 'FULL_TP' / 'TP1_ONLY' / 'SL' / 'EXPIRED'
    """
    is_long = direction == 'LONG'
    rows = list(df_future.iterrows())

    # Tahap 1: TP1 vs SL
    tp1_idx = None
    for i, (_, row) in enumerate(rows):
        h = float(row['high'])
        l = float(row['low'])
        if is_long:
            if l <= sl:  return 'SL'
            if h >= tp1:
                tp1_idx = i
                break
        else:
            if h >= sl:  return 'SL'
            if l <= tp1:
                tp1_idx = i
                break

    if tp1_idx is None:
        return 'EXPIRED'

    # Tahap 2: setelah TP1 kena, SL geser ke BE (entry)
    # Cek apakah TP2 kena sebelum harga balik ke entry
    for _, row in rows[tp1_idx + 1:]:
        h = float(row['high'])
        l = float(row['low'])
        if is_long:
            if l <= entry:  return 'TP1_ONLY'   # BE kena
            if h >= tp2:    return 'FULL_TP'
        else:
            if h >= entry:  return 'TP1_ONLY'   # BE kena
            if l <= tp2:    return 'FULL_TP'

    return 'TP1_ONLY'  # Waktu habis setelah TP1, anggap TP2 tidak kena


# ══════════════════════════════════════════════════════════════
# MAIN TRAINER CLASS
# ══════════════════════════════════════════════════════════════

class HistoricalTrainer:
    def __init__(self, progress_cb: Callable = None):
        """
        progress_cb: fungsi(str) untuk laporan progress (opsional)
        """
        from learning_engine import get_learning_engine
        from backtesting.data_fetcher import DataFetcher

        self.le       = get_learning_engine()
        self.fetcher  = DataFetcher()
        self._cb      = progress_cb or (lambda s: print(s, flush=True))

    def _log(self, msg: str):
        logger.info(msg)
        self._cb(msg)

    # ──────────────────────────────────────────────────────────
    # Fetch data historis
    # ──────────────────────────────────────────────────────────
    def _fetch(self, symbol: str, days: int) -> tuple:
        """Return (df_1h, df_4h) atau (None, None)."""
        self._log(f"  [FETCH] Ambil data {symbol} {days} hari...")
        df_1h = self.fetcher.fetch_ohlcv(symbol, '1h', days=days)
        df_4h = self.fetcher.fetch_ohlcv(symbol, '4h', days=days)
        if df_1h is None or len(df_1h) < MIN_WINDOW:
            self._log(f"  [WARN] Data {symbol} tidak cukup — skip")
            return None, None
        n = len(df_1h)
        self._log(f"  [OK] {symbol}: {n} candle 1h, {len(df_4h) if df_4h is not None else 0} candle 4h")
        return df_1h, df_4h

    # ──────────────────────────────────────────────────────────
    # Training satu coin
    # ──────────────────────────────────────────────────────────
    def train_symbol(self, symbol: str, df_1h: pd.DataFrame,
                     df_4h: Optional[pd.DataFrame],
                     df_btc: Optional[pd.DataFrame]) -> int:
        """
        Replay data historis satu coin.
        Return: jumlah signal yang diproses.
        """
        closes_1h  = df_1h['close'].values
        highs_1h   = df_1h['high'].values
        lows_1h    = df_1h['low'].values
        n          = len(df_1h)
        logged     = 0
        n_tp       = 0
        n_full_tp  = 0
        n_tp1_only = 0
        n_sl       = 0
        total_pnl  = 0.0

        # Pre-hitung EMA global (lebih cepat)
        e8_all  = _calc_ema(closes_1h, 8)
        e21_all = _calc_ema(closes_1h, 21)
        e50_all = _calc_ema(closes_1h, 50)

        # Pre-hitung 4h EMA per candle (hindari pandas filter loop)
        ts_1h = df_1h['timestamp'].values.astype('int64')
        htf_ema_arr = None
        if df_4h is not None and len(df_4h) >= 21:
            ts_4h     = df_4h['timestamp'].values.astype('int64')
            closes_4h = df_4h['close'].values
            e21_4h    = _calc_ema(closes_4h, 21)
            htf_ema_arr = np.empty(n, dtype=object)
            htf_ema_arr[:] = 'SIDEWAYS'
            for idx in range(MIN_WINDOW, n - MAX_HOLD, STEP):
                j = int(np.searchsorted(ts_4h, ts_1h[idx-1], side='right')) - 1
                if j >= 21:
                    c = closes_4h[j]
                    e = e21_4h[j]
                    htf_ema_arr[idx] = ('UP' if c > e * 1.005 else
                                        'DOWN' if c < e * 0.995 else 'SIDEWAYS')

        # Pre-hitung BTC state per candle
        btc_state_arr = None
        if df_btc is not None and len(df_btc) >= 21:
            ts_btc     = df_btc['timestamp'].values.astype('int64')
            closes_btc = df_btc['close'].values
            e21_btc    = _calc_ema(closes_btc, 21)
            # Rolling RSI vectorized (tanpa loop per candle)
            delta   = np.diff(closes_btc)
            gains   = np.where(delta > 0, delta, 0.0)
            losses  = np.where(delta < 0, -delta, 0.0)
            rsi_btc_all = np.full(len(closes_btc), 50.0)
            P = 14
            for k in range(P, len(closes_btc)):
                ag = np.mean(gains[k-P:k])
                al = np.mean(losses[k-P:k])
                rs = ag / max(al, 1e-10)
                rsi_btc_all[k] = 100 - 100 / (1 + rs)
            btc_state_arr = np.empty(n, dtype=object)
            btc_state_arr[:] = 'NEUTRAL'
            for idx in range(MIN_WINDOW, n - MAX_HOLD, STEP):
                j = int(np.searchsorted(ts_btc, ts_1h[idx-1], side='right')) - 1
                if j >= 21:
                    bc = closes_btc[j]
                    be = e21_btc[j]
                    br = rsi_btc_all[j]
                    if bc < be * 0.99 and br < 48:
                        btc_state_arr[idx] = 'BEARISH'
                    elif bc > be * 1.01 and br > 52:
                        btc_state_arr[idx] = 'BULLISH'

        for i in range(MIN_WINDOW, n - MAX_HOLD, STEP):
            # Window saat ini
            w_start = max(0, i - WINDOW)
            closes = closes_1h[w_start:i]
            highs  = highs_1h[w_start:i]
            lows   = lows_1h[w_start:i]
            price  = float(closes[-1])

            if price <= 0:
                continue

            # Indikator
            atr     = _calc_atr(highs, lows, closes)
            rsi     = _calc_rsi(closes)
            e8      = float(e8_all[i-1])
            e21     = float(e21_all[i-1])
            e50     = float(e50_all[i-1])
            et      = _detect_ema_trend(price, e8, e21, e50)

            # Gunakan pre-computed dataframe hanya untuk S&R (butuh DataFrame)
            df_w    = df_1h.iloc[w_start:i]
            struct  = _detect_structure(highs, lows)

            # HTF EMA (pre-computed)
            htf_ema = htf_ema_arr[i] if htf_ema_arr is not None else 'SIDEWAYS'

            # BTC state (pre-computed)
            btc_state = btc_state_arr[i] if btc_state_arr is not None else 'NEUTRAL'

            # Cari level support/resistance
            near_sup, near_res = _detect_support_resistance(df_w, atr)

            # Tentukan direction berdasarkan kondisi
            candidates = []
            if near_sup and abs(price - near_sup) < atr * 1.2:
                candidates.append('LONG')
            if near_res and abs(price - near_res) < atr * 1.2:
                candidates.append('SHORT')

            if not candidates:
                continue  # Tidak ada level yang dekat

            # Session filter — skip DEAD session (02-07 UTC), WR historis 19%
            ts_now = df_1h['timestamp'].iloc[i-1]
            if hasattr(ts_now, 'hour') and 2 <= ts_now.hour < 7:
                continue

            for direction in candidates:
                # Block LONG saat BTC tidak bullish — data: LONG di BTC bearish/neutral WR rendah
                if direction == 'LONG' and btc_state == 'BEARISH':
                    continue

                # Block SHORT saat BTC bullish kuat — whale masih beli
                if direction == 'SHORT' and btc_state == 'BULLISH':
                    continue

                score, kills = _score_simple(direction, rsi, et, struct, htf_ema, atr)
                n_kills = len(kills)

                # Hanya proses signal GOOD: score tinggi, nol kill factor
                # MODERATE dan WAIT dibuang — data: sinyal lemah = 40-45% WR = tidak worth it
                # Score >= 7 dengan kills = 0 sudah memastikan semua faktor utama aligned
                if score < MIN_QUALITY_SCORE or n_kills > MIN_QUALITY_KILLS:
                    continue

                quality = 'GOOD'

                # Hitung entry/SL/TP
                if direction == 'LONG':
                    entry = price
                    sl    = near_sup - atr * SL_ATR_MULT if near_sup else price - atr * 2
                    tp1   = price + atr * TP1_ATR_MULT
                    tp2   = price + atr * TP2_ATR_MULT
                else:
                    entry = price
                    sl    = near_res + atr * SL_ATR_MULT if near_res else price + atr * 2
                    tp1   = price - atr * TP1_ATR_MULT
                    tp2   = price - atr * TP2_ATR_MULT

                sl  = max(sl, 0.0001)
                rr1 = abs(tp1 - entry) / max(abs(entry - sl), 0.0001)
                rr2 = abs(tp2 - entry) / max(abs(entry - sl), 0.0001)

                # Cek rr2 (TP2) — ini target utama, harus minimal 2.0R
                # rr1 = TP1_ATR_MULT/SL_ATR_MULT = 2.0/1.2 = 1.67 (selalu)
                # rr2 = TP2_ATR_MULT/SL_ATR_MULT = 3.5/1.2 = 2.92 (selalu)
                if rr2 < MIN_RR:
                    continue  # Setup terlalu sempit, skip

                # Simulasi outcome dua tahap
                df_future = df_1h.iloc[i:i + MAX_HOLD]
                outcome   = _simulate_outcome(df_future, entry, sl, tp1, tp2, direction)

                if outcome == 'EXPIRED':
                    continue  # Skip expired untuk data training murni

                # PnL riil berdasarkan risk $RISK_USD per trade
                # TP1 (50% qty): profit = RISK_USD * rr1 * 0.5
                # TP2 (50% qty setelah BE): profit = RISK_USD * rr2 * 0.5
                # SL: loss = -RISK_USD
                if outcome == 'FULL_TP':
                    pnl_usd = round(RISK_USD * rr1 * 0.5 + RISK_USD * rr2 * 0.5, 2)
                elif outcome == 'TP1_ONLY':
                    pnl_usd = round(RISK_USD * rr1 * 0.5, 2)
                else:
                    pnl_usd = -RISK_USD

                # Session dari timestamp candle
                ts      = df_1h['timestamp'].iloc[i-1]
                session = _get_session(ts)

                # Catat hasil — TIDAK masuk ke learning.db (DB hanya untuk trade nyata)
                logged    += 1
                total_pnl += pnl_usd
                if outcome == 'FULL_TP':
                    n_full_tp += 1
                    n_tp      += 1
                elif outcome == 'TP1_ONLY':
                    n_tp1_only += 1
                    n_tp       += 1
                else:
                    n_sl += 1

        return logged, n_full_tp, n_tp1_only, n_sl, round(total_pnl, 2)

    # ──────────────────────────────────────────────────────────
    # Training semua coin
    # ──────────────────────────────────────────────────────────
    def train_all(self, coins: list = None, days: int = 365,
                  progress_cb: Callable = None) -> dict:
        """
        Training semua coin dari data historis.
        progress_cb(coin, n_signals, n_tp, n_sl) — dipanggil tiap coin selesai.
        Return: dict ringkasan hasil.
        """
        from config import SCAN_POOL

        if coins is None:
            coins = SCAN_POOL

        self._log(f"\n[TRAIN] HISTORICAL TRAINING MULAI")
        self._log(f"   Coins  : {', '.join(coins)}")
        self._log(f"   Periode: {days} hari ke belakang")
        self._log(f"   Estimasi: {len(coins) * days // 30} menit\n")

        # Fetch BTC sebagai referensi market
        self._log("[FETCH] Ambil data BTC untuk referensi market...")
        df_btc = self.fetcher.fetch_ohlcv('BTC', '1h', days=days)
        if df_btc is None:
            self._log("  [WARN] Data BTC gagal — btc_state akan selalu NEUTRAL")

        results       = {}
        total_logged  = 0
        total_full_tp = 0
        total_tp1only = 0
        total_sl      = 0
        total_pnl     = 0.0
        start_time    = time.time()

        for idx, symbol in enumerate(coins, 1):
            self._log(f"\n[{idx}/{len(coins)}] Training {symbol}...")
            t0 = time.time()

            df_1h, df_4h = self._fetch(symbol, days)
            if df_1h is None:
                results[symbol] = {'status': 'skip', 'logged': 0, 'full_tp': 0, 'tp1_only': 0, 'sl': 0, 'pnl': 0}
                continue

            try:
                n, n_full_tp, n_tp1_only, n_sl, pnl = self.train_symbol(symbol, df_1h, df_4h, df_btc)
                elapsed = time.time() - t0
                n_tp = n_full_tp + n_tp1_only
                wr   = round(n_tp / n * 100, 1) if n else 0
                self._log(f"  [OK] {symbol}: {n} trade | FullTP:{n_full_tp} TP1:{n_tp1_only} SL:{n_sl} WR:{wr}% PnL:${pnl:+.2f} ({elapsed:.0f}s)")
                results[symbol] = {
                    'status'  : 'ok', 'logged': n,
                    'full_tp' : n_full_tp, 'tp1_only': n_tp1_only, 'sl': n_sl,
                    'pnl'     : pnl
                }
                total_logged  += n
                total_full_tp += n_full_tp
                total_tp1only += n_tp1_only
                total_sl      += n_sl
                total_pnl     += pnl
                if progress_cb:
                    try:
                        progress_cb(symbol, n, n_full_tp, n_tp1_only, n_sl, pnl)
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"train_symbol {symbol}: {e}", exc_info=True)
                self._log(f"  [ERR] {symbol}: error — {e}")
                results[symbol] = {'status': 'error', 'logged': 0, 'full_tp': 0, 'tp1_only': 0, 'sl': 0, 'pnl': 0}

        total_time = (time.time() - start_time) / 60
        total_tp   = total_full_tp + total_tp1only
        wr_all       = round(total_tp / total_logged * 100, 1) if total_logged else 0
        ev_per_trade = round(total_pnl / total_logged, 3) if total_logged else 0

        # Breakeven WR dan apakah sistem profitable
        avg_win_est  = total_pnl / total_tp if total_tp else 0
        avg_loss_est = RISK_USD
        rr_est       = avg_win_est / avg_loss_est if avg_loss_est else 0
        breakeven_wr = round(1 / (1 + rr_est) * 100, 1) if rr_est > 0 else 50.0
        surplus_wr   = round(wr_all - breakeven_wr, 1)
        is_profitable = surplus_wr > 0

        self._log(f"\n[OK] TRAINING SELESAI")
        self._log(f"   Total trade   : {total_logged}")
        self._log(f"   Full TP       : {total_full_tp} ({total_full_tp/total_logged*100:.1f}%)" if total_logged else "")
        self._log(f"   TP1 Only      : {total_tp1only} ({total_tp1only/total_logged*100:.1f}%)" if total_logged else "")
        self._log(f"   SL            : {total_sl} ({total_sl/total_logged*100:.1f}%)" if total_logged else "")
        self._log(f"   Win Rate      : {wr_all}%")
        self._log(f"   Breakeven WR  : {breakeven_wr}%  ← WR minimum agar tidak rugi")
        self._log(f"   Surplus       : {surplus_wr:+.1f}%  ({'PROFITABLE' if is_profitable else 'MERUGI'})")
        self._log(f"   Total PnL     : ${total_pnl:+.2f}  (risk $3/trade)")
        self._log(f"   EV/trade      : ${ev_per_trade:+.3f}")
        self._log(f"   Durasi        : {total_time:.1f} menit")

        # Simpan hasil per coin ke JSON — dipakai smart coin selector
        self._save_training_results(results, total_logged, total_full_tp,
                                    total_tp1only, total_sl, total_pnl,
                                    wr_all, ev_per_trade, breakeven_wr, surplus_wr)

        return {
            'total_signals': total_logged,
            'total_full_tp': total_full_tp,
            'total_tp1only': total_tp1only,
            'total_tp'     : total_tp,
            'total_sl'     : total_sl,
            'total_pnl'    : round(total_pnl, 2),
            'ev_per_trade' : ev_per_trade,
            'breakeven_wr' : breakeven_wr,
            'surplus_wr'   : surplus_wr,
            'win_rate'     : wr_all,
            'coins'        : results,
            'duration_min' : round(total_time, 1),
        }

    def _save_training_results(self, results, total, full_tp, tp1only, sl,
                                pnl, wr, ev, breakeven_wr, surplus_wr):
        """
        Simpan hasil training per coin ke data/training_results.json.
        Dipakai oleh smart coin selector untuk menentukan coin mana yang layak di-trade.
        """
        import json
        from datetime import datetime

        # Kategorikan setiap coin
        profitable_coins = []
        avoid_coins      = []
        per_coin         = {}

        for sym, r in results.items():
            if r['status'] != 'ok' or r['logged'] < 20:
                continue
            n   = r['logged']
            tp  = r['full_tp'] + r['tp1_only']
            wr_ = round(tp / n * 100, 1)
            per_coin[sym] = {
                'total'   : n,
                'full_tp' : r['full_tp'],
                'tp1_only': r['tp1_only'],
                'sl'      : r['sl'],
                'wr'      : wr_,
                'pnl'     : r['pnl'],
            }
            if r['pnl'] > 0 and wr_ >= 45:
                profitable_coins.append(sym)
            elif r['pnl'] < 0 or wr_ < 40:
                avoid_coins.append(sym)

        # Sort profitable by PnL tertinggi
        profitable_coins.sort(key=lambda s: per_coin[s]['pnl'], reverse=True)

        data = {
            'trained_at'      : datetime.now().strftime('%Y-%m-%d %H:%M'),
            'total_signals'   : total,
            'overall_wr'      : wr,
            'overall_pnl'     : round(pnl, 2),
            'ev_per_trade'    : ev,
            'breakeven_wr'    : breakeven_wr,
            'surplus_wr'      : surplus_wr,
            'profitable_coins': profitable_coins,
            'avoid_coins'     : avoid_coins,
            'per_coin'        : per_coin,
        }

        path = 'data/training_results.json'
        os.makedirs('data', exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
        self._log(f"\n[SAVE] Hasil training disimpan → {path}")
        self._log(f"   Coin profitable ({len(profitable_coins)}): {', '.join(profitable_coins[:10])}{'...' if len(profitable_coins)>10 else ''}")
        self._log(f"   Coin dihindari  ({len(avoid_coins)}): {', '.join(avoid_coins)}")


# ══════════════════════════════════════════════════════════════
# CLI RUNNER
# ══════════════════════════════════════════════════════════════

def main():
    import logging
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s %(levelname)s %(message)s'
    )

    parser = argparse.ArgumentParser(description='Historical Training untuk Learning Engine')
    parser.add_argument('--days',  type=int, default=365,
                        help='Jumlah hari data historis (default: 365)')
    parser.add_argument('--coins', nargs='+', default=None,
                        help='Coin yang ditraining (default: semua SCAN_POOL)')
    args = parser.parse_args()

    print(f"\n{'='*50}")
    print(f" HISTORICAL TRAINER - Bot Belajar dari Masa Lalu")
    print(f"{'='*50}")
    print(f" Periode : {args.days} hari")
    print(f" Coins   : {args.coins or 'SCAN_POOL (default)'}")
    print(f"{'='*50}\n")

    trainer = HistoricalTrainer()
    result  = trainer.train_all(coins=args.coins, days=args.days)

    n       = result.get('total_signals', 0)
    full_tp = result.get('total_full_tp', 0)
    tp1only = result.get('total_tp1only', 0)
    sl      = result.get('total_sl', 0)
    pnl     = result.get('total_pnl', 0)
    growth  = result.get('growth_pct', 0)
    ev      = result.get('ev_per_trade', 0)
    wr      = result.get('win_rate', 0)

    print(f"\n{'='*52}")
    print(f" HASIL TRAINING - {n} trade dicatat")
    print(f"{'='*52}")
    print(f" Full TP (TP1+TP2)   : {full_tp}  ({full_tp/n*100:.1f}%)" if n else "")
    print(f" TP1 Only (BE stop)  : {tp1only}  ({tp1only/n*100:.1f}%)" if n else "")
    print(f" SL                  : {sl}  ({sl/n*100:.1f}%)" if n else "")
    print(f" Win Rate            : {wr:.1f}%")
    print(f" Total PnL           : ${pnl:+.2f}  (risk $3/trade)")
    print(f" EV per trade        : ${ev:+.3f}")
    print(f" Growth (modal $100) : {growth:+.1f}%")
    print(f" Durasi              : {result['duration_min']} menit")
    print(f"{'='*52}\n")


if __name__ == '__main__':
    main()
