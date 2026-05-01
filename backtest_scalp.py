"""
backtest_scalp.py — Backtest engine untuk Bot 2 (Scalping 15m).

Self-contained: fetch data → generate signal → simulate trade → report.
Tidak perlu BacktestEngine/replay_engine — scalping engine independen.

Usage:
  python backtest_scalp.py                         # default: 20 coin, 90 hari
  python backtest_scalp.py --days 30 --coins BTC ETH XRP
  python backtest_scalp.py --no-fetch              # pakai cache
"""

import os
import sys
import io
import time
import pickle
import argparse
import logging
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from dataclasses import dataclass, field

# Fix Windows UTF-8
if hasattr(sys.stdout, 'buffer') and not sys.stdout.closed:
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

from scalping_signal_engine import generate_scalping_signal, get_htf_bias
from indicators import calc_atr, calc_rsi, calc_adx, analyze_ema_trend

# Learning modules
try:
    import scalp_trade_journal as journal
    import scalp_coin_learning as coin_learn
    import scalp_session_filter as session_filter
    _LEARNING_ENABLED = True
except ImportError:
    _LEARNING_ENABLED = False

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(message)s')
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────
CACHE_DIR = 'backtesting/cache'
CACHE_FILE = os.path.join(CACHE_DIR, 'scalp_data.pkl')

# TF-specific params untuk multi-TF backtest
# 2026-04-29: tambah support 5m TF biar volume scalp naik 5-10/hari
TF_PARAMS = {
    '15m': {
        'max_bars': 10,        # 2.5 jam max trade duration
        'dedup_hours': 4.0,    # min interval signal sama coin/direction
        'min_window': 80,      # candle minimum sebelum start scan
    },
    '5m': {
        'max_bars': 30,        # 2.5 jam max trade duration (proporsional)
        'dedup_hours': 1.5,    # 1.5 jam (proporsional, lebih ketat)
        'min_window': 80,
    },
}

# Coin pool untuk backtest — 20 coin paling likuid
DEFAULT_COINS = [
    # Mega cap
    'BTC', 'ETH', 'XRP', 'SOL', 'BNB',
    'DOGE', 'ADA', 'TRX', 'AVAX', 'LINK',
    # Large cap
    'SUI', 'TON', 'DOT', 'NEAR', 'LTC',
    'BCH', 'UNI', 'APT', 'ICP', 'ETC',
    'STX', 'INJ', 'TAO', 'HBAR', 'IMX',
    'FET', 'OP', 'ARB', 'WLD', 'SEI',
    # Mid cap aktif
    'TIA', 'STRK', 'PYTH', 'JUP', 'WIF',
    'ORDI', 'PENDLE', 'KAS', 'ONDO', 'LDO',
    'BLUR', 'GRT', 'DYDX', 'MKR', 'AAVE',
    'ATOM', 'FIL', 'SAND', 'MANA', 'AXS',
    # Established alts
    'GMX', 'ENS', 'CRV', 'SNX', 'THETA',
    'ALGO', 'VET', 'EOS', 'XTZ', 'CAKE',
    'XLM', 'ZEC', 'DASH', 'GALA', 'MAGIC',
    'RENDER', 'JTO', 'DYM', 'MANTA', 'NOT',
    # Emerging / memecoin liquid
    'PEPE', 'BONK', 'FLOKI', 'SHIB', 'BOME',
    'POPCAT', 'PNUT', 'CATI', 'HMSTR', 'DOGS',
    'GOAT', 'VIRTUAL', 'SONIC', 'MOVE', 'GRASS',
    'EIGEN', 'ZRO', 'ETHFI', 'SAGA', 'IO',
    # Tambahan liquid Bitunix
    'ACT', 'HYPE', 'TRUMP', 'MELANIA', 'FARTCOIN',
    'MOODENG', 'MEW', 'KAIA', 'USUAL', 'BANANA',
]

# Backtest parameters
SCAN_STEP = 1         # cek setiap candle


# ── Data Classes ────────────────────────────────────────────
@dataclass
class TradeResult:
    symbol: str
    direction: str
    quality: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    rr1: float
    rr2: float
    score: int
    kills: int
    timestamp: str
    outcome: str        # TP1, TP2, TP3, SL, EXPIRED, BEP
    bars_to_outcome: int
    pnl_r: float        # profit in R units
    reasons: list = field(default_factory=list)
    htf_bias: str = ''
    wedge_pattern: str = ''
    candle_pattern: str = ''
    # v4.3 features
    trend_state: str = ''
    trend_strength: int = 0
    pullback_quality: str = ''
    session: str = ''
    macro_4h_bias: str = ''
    smc_bos: str = ''
    volume_pressure: str = ''
    tf: str = '15m'  # main timeframe yang generate signal


# ── 1. DATA FETCHER ─────────────────────────────────────────
def fetch_klines_paginated(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """
    Fetch OHLCV dari Binance dengan pagination.
    1000 candle per request. Paginate backward dari sekarang.
    """
    cpd = {'5m': 288, '15m': 96, '1h': 24, '4h': 6}
    total_needed = cpd.get(interval, 96) * days
    url = 'https://api.binance.com/api/v3/klines'

    all_rows = []
    end_time = None
    retries = 0

    while len(all_rows) < total_needed:
        params = {
            'symbol': f'{symbol}USDT',
            'interval': interval,
            'limit': 1000,
        }
        if end_time:
            params['endTime'] = end_time

        try:
            resp = requests.get(url, params=params, timeout=15)

            if resp.status_code == 429:
                logger.warning("Rate limited, waiting 30s...")
                time.sleep(30)
                continue
            if resp.status_code == 418:
                logger.warning("IP banned, waiting 60s...")
                time.sleep(60)
                continue

            resp.raise_for_status()
            data = resp.json()

            if not data or len(data) == 0:
                break

            for k in data:
                all_rows.append({
                    'timestamp': pd.to_datetime(int(k[0]), unit='ms'),
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5]),
                })

            # Paginate backward
            oldest = int(data[0][0])
            end_time = oldest - 1

            if len(data) < 1000:
                break  # Tidak ada data lebih lama

            retries = 0
            time.sleep(0.3)  # rate limit courtesy

        except requests.exceptions.RequestException as e:
            retries += 1
            if retries > 3:
                logger.error(f"Fetch failed {symbol} {interval}: {e}")
                break
            time.sleep(5)

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows)
    df = df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)

    # Buang candle terakhir (belum close)
    if len(df) > 1:
        df = df.iloc[:-1]

    return df


def fetch_all_data(coins: list, days: int,
                   timeframes: list = None,
                   force: bool = False) -> dict:
    """
    Fetch data untuk semua coin: list timeframe + 1h (HTF bias).
    timeframes: ['15m'] (default) atau ['15m', '5m'] dst.
    Cache per-TF list ke disk untuk reuse.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    timeframes = timeframes or ['15m']
    tf_key = '_'.join(sorted(timeframes))
    cache_file = os.path.join(CACHE_DIR, f'scalp_data_{tf_key}.pkl')

    # Cek cache
    if not force and os.path.exists(cache_file):
        try:
            with open(cache_file, 'rb') as f:
                cached = pickle.load(f)
            meta = cached.get('_meta', {})
            if (meta.get('days', 0) >= days and
                    set(coins).issubset(set(meta.get('coins', []))) and
                    set(timeframes).issubset(set(meta.get('tfs', []))) and
                    time.time() - meta.get('ts', 0) < 86400):
                logger.info(f"Using cached data ({meta.get('days')}d, "
                            f"{len(meta.get('coins', []))} coins, "
                            f"TFs={meta.get('tfs', [])})")
                return cached
        except Exception:
            pass

    logger.info(f"Fetching data: {len(coins)} coins, {days} days, "
                f"TFs={timeframes}...")
    data = {'_meta': {'days': days, 'coins': coins,
                      'tfs': timeframes, 'ts': time.time()}}

    min_window_min = min(TF_PARAMS[tf]['min_window'] for tf in timeframes)

    for i, coin in enumerate(coins):
        logger.info(f"  [{i+1}/{len(coins)}] {coin}...")
        data[coin] = {}

        # Fetch tiap main TF
        ok_count = 0
        for tf in timeframes:
            df = fetch_klines_paginated(coin, tf, days)
            if df is not None and len(df) > min_window_min:
                data[coin][tf] = df
                logger.info(f"    {tf}: {len(df)} candles "
                            f"({df['timestamp'].iloc[0].date()} -> "
                            f"{df['timestamp'].iloc[-1].date()})")
                ok_count += 1
            else:
                logger.warning(f"    {tf}: SKIP (insufficient data)")

        if ok_count == 0:
            continue

        # 1h data (untuk HTF bias) — sama untuk semua main TF
        df_1h = fetch_klines_paginated(coin, '1h', days)
        if df_1h is not None and len(df_1h) > 55:
            data[coin]['1h'] = df_1h
            logger.info(f"    1h: {len(df_1h)} candles")

        time.sleep(0.5)  # courtesy delay between coins

    # Save cache
    try:
        with open(cache_file, 'wb') as f:
            pickle.dump(data, f, protocol=4)
        logger.info(f"Data cached to {cache_file}")
    except Exception as e:
        logger.warning(f"Cache save failed: {e}")

    return data


# ── 2. TRADE SIMULATOR ─────────────────────────────────────
def simulate_trade(signal: dict, df_future: pd.DataFrame) -> TradeResult:
    """
    Simulasi trade. Trailing strategy dipilih via env RR_VARIANT:

    BASELINE (default):
      TP1 hit  → close 50%, SL → entry (BEP)
      TP2 hit  → SL → tp1
      TP3 hit  → SL → tp3, trail extreme − 0.5R

    A — BEP+buffer:
      TP1 hit  → close 50%, SL → entry + 0.2R (buffer)
      TP2 hit  → SL → tp1
      TP3 hit  → SL → tp3, trail extreme − 0.5R

    B — Cascade 50/30/20:
      TP1 hit  → close 50%, SL → entry + 0.2R
      TP2 hit  → close 30% LAGI (cumulative 80%), SL → tp1 untuk sisa 20%
      TP3 hit  → close 0%, SL → tp3 untuk sisa 20%, trail extreme − 0.5R

    C — Dynamic high-water trail post-TP1:
      TP1 hit  → close 50%, SL → max(extreme − 1.0R, entry + 0.2R)
      TP2 hit  → SL → max(extreme − 0.7R, tp1)
      TP3 hit  → SL → tp3, trail extreme − 0.5R

    D — Closer TP targets (override rr2=1.3, rr3=2.0):
      TP2 di-override ke entry + 1.3R, TP3 ke entry + 2.0R.
      Trail logic = baseline.

    E — Drop TP3 + cascade 70/30 full close:
      TP1 hit  → close 70%, SL → entry (BEP)
      TP2 hit  → close 30% (FULL close at TP2)
      No TP3 logic.

    Quality risk multiplier (semua variant):
      GOOD = 1.5x base risk, WAIT = 0.5x.

    Returns: TradeResult
    """
    variant = os.getenv('RR_VARIANT', 'baseline').lower()
    if variant not in ('baseline', 'a', 'b', 'c', 'd', 'e'):
        variant = 'baseline'

    direction = signal['direction']
    entry = signal['entry']
    sl = signal['sl']
    tp1 = signal['tp1']
    tp2 = signal['tp2']
    tp3 = signal.get('tp3', tp2)
    rr1 = signal.get('rr1', 1.0)
    rr2 = signal.get('rr2', 1.8)
    risk = abs(entry - sl)
    rr3 = abs(tp3 - entry) / risk if risk > 0 else 1.5

    # Variant D: override TP2/TP3 ke level lebih dekat
    if variant == 'd':
        rr2 = 1.3
        rr3 = 2.0
        if direction == 'LONG':
            tp2 = entry + 1.3 * risk
            tp3 = entry + 2.0 * risk
        else:
            tp2 = entry - 1.3 * risk
            tp3 = entry - 2.0 * risk

    # Variant E: drop TP3 (set ke tp2 supaya gak pernah trigger TP3 logic)
    if variant == 'e':
        rr3 = rr2
        tp3 = tp2

    quality = signal.get('quality', 'WAIT')
    risk_mult = {'GOOD': 1.5, 'WAIT': 0.5}.get(quality, 1.0)

    # Buffer setelah TP1 — variant A/B/C pake 0.2R, baseline/D/E pake 0
    bep_buffer = 0.2 if variant in ('a', 'b', 'c') else 0.0
    if direction == 'LONG':
        bep_sl = entry + bep_buffer * risk
    else:
        bep_sl = entry - bep_buffer * risk

    # Cascade weights per variant
    # baseline/A/C/D: 50% di TP1, sisa 50% lanjut (no extra close di TP2)
    # B: 50% di TP1, 30% lagi di TP2 (sisa 20% lanjut)
    # E: 70% di TP1, 30% di TP2 (FULL close, no runner)
    if variant == 'b':
        tp1_partial = 0.5
        tp2_partial = 0.3
    elif variant == 'e':
        tp1_partial = 0.7
        tp2_partial = 0.3
    else:
        tp1_partial = 0.5
        tp2_partial = 0.0

    use_cascade = variant in ('b', 'e')

    tp1_hit = False
    tp2_hit = False
    tp3_hit = False
    partial_pnl = 0.0
    remaining_weight = 1.0   # 1.0 = full position
    extreme_price = entry
    current_sl = sl
    max_bars = len(df_future)

    def _close(outcome: str, bar: int, remainder_r: float) -> TradeResult:
        total_r = partial_pnl + remaining_weight * remainder_r
        return _make_result(signal, outcome, bar,
                            round(total_r * risk_mult, 3))

    def _update_trail_long(current_sl_val: float) -> float:
        """Variant C: high-water trail post-TP1/TP2. Returns new SL."""
        if not tp1_hit:
            return current_sl_val
        if tp3_hit:
            cand = extreme_price - 0.5 * risk
            return max(current_sl_val, cand)
        if tp2_hit:
            cand = max(extreme_price - 0.7 * risk, tp1)
            return max(current_sl_val, cand)
        # Post-TP1 only
        cand = max(extreme_price - 1.0 * risk, entry + 0.2 * risk)
        return max(current_sl_val, cand)

    def _update_trail_short(current_sl_val: float) -> float:
        if not tp1_hit:
            return current_sl_val
        if tp3_hit:
            cand = extreme_price + 0.5 * risk
            return min(current_sl_val, cand)
        if tp2_hit:
            cand = min(extreme_price + 0.7 * risk, tp1)
            return min(current_sl_val, cand)
        cand = min(extreme_price + 1.0 * risk, entry - 0.2 * risk)
        return min(current_sl_val, cand)

    for i in range(max_bars):
        high_i = df_future['high'].iloc[i]
        low_i = df_future['low'].iloc[i]

        if direction == 'LONG':
            extreme_price = max(extreme_price, high_i)

            # Variant C: dynamic trail every bar (post-TP1)
            if variant == 'c' and tp1_hit:
                current_sl = _update_trail_long(current_sl)

            # Runner trail setelah TP3 (semua variant)
            if tp3_hit:
                trail_sl = extreme_price - 0.5 * risk
                if trail_sl > current_sl:
                    current_sl = trail_sl

            if low_i <= current_sl:
                if tp3_hit:
                    exit_r = (current_sl - entry) / risk if risk else rr3
                    return _close('TP3_TRAIL', i + 1, exit_r)
                if tp2_hit:
                    # Exit at TP1 level (current_sl after TP2)
                    return _close('TP2', i + 1, rr1)
                if tp1_hit:
                    # Exit at bep_sl (BEP atau BEP+buffer)
                    exit_r = (current_sl - entry) / risk if risk else 0.0
                    return _close('BEP', i + 1, exit_r)
                return _close('SL', i + 1, -1.0)

            if high_i >= tp3 and tp2_hit and not tp3_hit:
                tp3_hit = True
                current_sl = max(current_sl, tp3)

            if high_i >= tp2 and not tp2_hit:
                tp2_hit = True
                if use_cascade:
                    partial_pnl += tp2_partial * rr2
                    remaining_weight -= tp2_partial
                # Variant E: full close at TP2 (no runner)
                if variant == 'e' and remaining_weight <= 1e-6:
                    return _make_result(signal, 'TP2', i + 1,
                                        round(partial_pnl * risk_mult, 3))
                current_sl = tp1

            if high_i >= tp1 and not tp1_hit:
                tp1_hit = True
                partial_pnl = tp1_partial * rr1
                remaining_weight = 1.0 - tp1_partial
                current_sl = bep_sl

        else:  # SHORT
            extreme_price = min(extreme_price, low_i)

            if variant == 'c' and tp1_hit:
                current_sl = _update_trail_short(current_sl)

            if tp3_hit:
                trail_sl = extreme_price + 0.5 * risk
                if trail_sl < current_sl:
                    current_sl = trail_sl

            if high_i >= current_sl:
                if tp3_hit:
                    exit_r = (entry - current_sl) / risk if risk else rr3
                    return _close('TP3_TRAIL', i + 1, exit_r)
                if tp2_hit:
                    return _close('TP2', i + 1, rr1)
                if tp1_hit:
                    exit_r = (entry - current_sl) / risk if risk else 0.0
                    return _close('BEP', i + 1, exit_r)
                return _close('SL', i + 1, -1.0)

            if low_i <= tp3 and tp2_hit and not tp3_hit:
                tp3_hit = True
                current_sl = min(current_sl, tp3)

            if low_i <= tp2 and not tp2_hit:
                tp2_hit = True
                if use_cascade:
                    partial_pnl += tp2_partial * rr2
                    remaining_weight -= tp2_partial
                if variant == 'e' and remaining_weight <= 1e-6:
                    return _make_result(signal, 'TP2', i + 1,
                                        round(partial_pnl * risk_mult, 3))
                current_sl = tp1

            if low_i <= tp1 and not tp1_hit:
                tp1_hit = True
                partial_pnl = tp1_partial * rr1
                remaining_weight = 1.0 - tp1_partial
                current_sl = bep_sl

    # Expired
    if tp3_hit:
        return _close('TP3', max_bars, rr3)
    if tp2_hit:
        return _close('TP2', max_bars, rr2)
    if tp1_hit:
        return _close('TP1', max_bars, rr1)

    last_close = df_future['close'].iloc[max_bars - 1] \
        if max_bars <= len(df_future) else df_future['close'].iloc[-1]
    if risk > 0:
        if direction == 'LONG':
            actual_pnl = (last_close - entry) / risk
        else:
            actual_pnl = (entry - last_close) / risk
    else:
        actual_pnl = 0
    actual_pnl = max(actual_pnl, -1.0)
    return _close('EXPIRED', max_bars, actual_pnl)


def _make_result(signal: dict, outcome: str, bars: int,
                 pnl_r: float) -> TradeResult:
    """Helper untuk buat TradeResult dari signal dict."""
    return TradeResult(
        symbol=signal.get('_symbol', ''),
        direction=signal['direction'],
        quality=signal['quality'],
        entry=signal['entry'],
        sl=signal['sl'],
        tp1=signal['tp1'],
        tp2=signal['tp2'],
        tp3=signal.get('tp3', signal['tp2']),
        rr1=signal.get('rr1', 1.0),
        rr2=signal.get('rr2', 1.8),
        score=signal.get('confluence_score', 0),
        kills=signal.get('kill_count', 0),
        timestamp=signal.get('_timestamp', ''),
        outcome=outcome,
        bars_to_outcome=bars,
        pnl_r=pnl_r,
        reasons=signal.get('reasons', []),
        htf_bias=signal.get('_htf_bias', ''),
        wedge_pattern=signal.get('wedge', {}).get('pattern', ''),
        candle_pattern=signal.get('candle_confirm', ''),
        trend_state=signal.get('trend_state', ''),
        trend_strength=signal.get('trend_strength', 0),
        pullback_quality=signal.get('pullback_quality', ''),
        session=signal.get('session', ''),
        macro_4h_bias=signal.get('macro_4h_bias', ''),
        smc_bos=signal.get('smc_bos', ''),
        volume_pressure=signal.get('volume_pressure', ''),
    )


# ── 3. BACKTEST RUNNER ──────────────────────────────────────
def _simulate_with_variant(signal: dict, df_future: pd.DataFrame,
                           variant: str) -> 'TradeResult':
    """Simulate trade dengan variant tertentu (override env)."""
    prev = os.environ.get('RR_VARIANT', '')
    os.environ['RR_VARIANT'] = variant
    try:
        return simulate_trade(signal, df_future)
    finally:
        if prev:
            os.environ['RR_VARIANT'] = prev
        else:
            os.environ.pop('RR_VARIANT', None)


def run_backtest_coin(symbol: str, df_main: pd.DataFrame,
                      df_1h: pd.DataFrame = None,
                      tf_main: str = '15m',
                      verbose: bool = False,
                      last_signal_shared: dict = None,
                      multi_variant: list = None) -> list:
    """
    Jalankan backtest untuk satu coin.
    Sliding window di df_main (TF utama), step=1 candle.

    last_signal_shared: kalau ada (mode multi-TF combined),
      dedup global per coin/direction lintas TF.
    multi_variant: kalau di-set (e.g. ['baseline','a','b','c']), simulate per
      variant dan return dict {variant: [trades...]}. Otherwise pakai
      RR_VARIANT env (default baseline) dan return list.
    """
    tf_p = TF_PARAMS.get(tf_main, TF_PARAMS['15m'])
    max_bars = tf_p['max_bars']
    dedup_hours = tf_p['dedup_hours']
    min_window = tf_p['min_window']

    n = len(df_main)
    if n < min_window + max_bars + 10:
        logger.warning(f"  {symbol}: data kurang ({n} candles)")
        return {} if multi_variant else []

    trades = []
    multi_trades = {v: [] for v in multi_variant} if multi_variant else None
    last_signal = last_signal_shared if last_signal_shared is not None else {}

    for i in range(min_window, n - max_bars):
        # Ambil window data untuk analisa
        df_window = df_main.iloc[max(0, i - 200):i + 1].copy()
        df_window = df_window.reset_index(drop=True)

        # Hitung indikator
        try:
            atr = float(calc_atr(df_window, 14).iloc[-1])
            if atr <= 0:
                continue
            rsi_val = float(calc_rsi(df_window, 14).iloc[-1])
            adx_val = calc_adx(df_window, 14)
            ema_trend, _, _ = analyze_ema_trend(df_window)
            price = float(df_window['close'].iloc[-1])
        except Exception:
            continue

        # HTF bias dari 1H (cari data 1H yang sesuai timestamp)
        htf_ema = 'SIDEWAYS'
        if df_1h is not None:
            ts_now = df_main['timestamp'].iloc[i]
            # Ambil 1H data sampai timestamp ini
            df_1h_slice = df_1h[df_1h['timestamp'] <= ts_now].tail(60)
            if len(df_1h_slice) >= 55:
                htf_ema_bias = get_htf_bias(df_1h_slice)
                htf_map = {'BULLISH': 'UP', 'BEARISH': 'DOWN',
                           'SIDEWAYS': 'SIDEWAYS'}
                htf_ema = htf_map.get(htf_ema_bias, 'SIDEWAYS')

        # Generate signal — v4: pass df_1h via smc dict untuk trend detection
        df_1h_slice_local = None
        if df_1h is not None:
            ts_now_local = df_main['timestamp'].iloc[i]
            df_1h_slice_local = df_1h[df_1h['timestamp'] <= ts_now_local].tail(60)
        smc_payload = {'df_1h': df_1h_slice_local} \
            if df_1h_slice_local is not None and len(df_1h_slice_local) >= 55 \
            else {}
        signal = generate_scalping_signal(
            price=price, atr=atr, ema_trend=ema_trend,
            structure='SIDEWAYS',
            ks=None, kr=None, res_mtf=[], sup_mtf=[],
            smc=smc_payload, rsi=rsi_val, htf_ema=htf_ema,
            df_main=df_window, symbol=symbol,
            adx=adx_val, signal_cache=None,
        )

        if signal is None:
            continue

        # Dedup: cek apakah sudah ada signal sama dalam dedup_hours
        direction = signal['direction']
        ts = df_main['timestamp'].iloc[i]
        dedup_key = direction

        if dedup_key in last_signal:
            hours_since = (ts - last_signal[dedup_key]).total_seconds() / 3600
            if hours_since < dedup_hours:
                continue

        last_signal[dedup_key] = ts

        # Tambahkan metadata
        signal['_symbol'] = symbol
        signal['_timestamp'] = str(ts)
        signal['_htf_bias'] = htf_ema
        signal['_tf'] = tf_main

        # Simulasi trade
        df_future = df_main.iloc[i + 1:i + 1 + max_bars]
        if len(df_future) < 5:
            continue

        if multi_variant:
            # Run all variants on same signal+future
            for v in multi_variant:
                result = _simulate_with_variant(signal, df_future, v)
                result.tf = tf_main
                multi_trades[v].append(result)
        else:
            result = simulate_trade(signal, df_future)
            result.tf = tf_main
            trades.append(result)

            if verbose:
                ico = '+' if result.pnl_r > 0 else '-' if result.pnl_r < 0 else '='
                logger.info(f"  {ico} {ts} [{tf_main}] {direction} "
                            f"[{result.quality}] → {result.outcome} "
                            f"({result.pnl_r:+.1f}R) in {result.bars_to_outcome} bars")

    return multi_trades if multi_variant else trades


def run_full_backtest(coins: list, data: dict,
                      timeframes: list = None,
                      verbose: bool = False,
                      record_journal: bool = True,
                      engine_version: str = 'v4.3',
                      shared_dedup: bool = False,
                      multi_variant: list = None):
    """
    Jalankan backtest untuk semua coin di list timeframes.

    timeframes: list TF utama, e.g. ['15m'] atau ['15m', '5m'].
    shared_dedup: True = dedup global per coin/direction lintas TF
                  (mode 'both combined'). False = independent per TF.
    multi_variant: list of RR variants (e.g. ['baseline','a','b','c']).
                   Kalau di-set, return dict {variant: trades_list}.
                   Otherwise return list trades pakai RR_VARIANT env.
    """
    timeframes = timeframes or ['15m']
    if multi_variant:
        all_trades_mv = {v: [] for v in multi_variant}
    else:
        all_trades = []

    for i, coin in enumerate(coins):
        coin_data = data.get(coin, {})
        if not any(tf in coin_data for tf in timeframes):
            continue

        logger.info(f"[{i+1}/{len(coins)}] Backtest {coin}...")
        df_1h = coin_data.get('1h')

        # Shared dedup state per coin (only used kalau shared_dedup=True)
        coin_dedup = {} if shared_dedup else None

        n_per_tf = {}
        for tf in timeframes:
            df_main = coin_data.get(tf)
            if df_main is None:
                continue
            result = run_backtest_coin(
                coin, df_main, df_1h,
                tf_main=tf,
                verbose=verbose,
                last_signal_shared=coin_dedup,
                multi_variant=multi_variant,
            )
            if multi_variant:
                for v, t_list in result.items():
                    all_trades_mv[v].extend(t_list)
                # log signal count (baseline trades count = total)
                n_per_tf[tf] = len(result.get(multi_variant[0], []))
            else:
                all_trades.extend(result)
                n_per_tf[tf] = len(result)

        if n_per_tf:
            logger.info(f"  {coin}: " +
                        ", ".join(f"{tf}={n}" for tf, n in n_per_tf.items()))

    if multi_variant:
        # Skip journal recording untuk multi-variant mode
        return all_trades_mv

    # Record ke journal untuk learning
    if record_journal and _LEARNING_ENABLED and all_trades:
        logger.info("Recording trades ke journal...")
        records = []
        for t in all_trades:
            # Parse timestamp to get hour/session
            try:
                ts = pd.to_datetime(t.timestamp)
                hour_utc = ts.hour
                day_of_week = ts.dayofweek
                session = session_filter.get_session(hour_utc)
            except Exception:
                hour_utc = 0
                day_of_week = 0
                session = 'UNKNOWN'

            records.append({
                'timestamp': t.timestamp,
                'symbol': t.symbol,
                'direction': t.direction,
                'quality': t.quality,
                'entry_price': t.entry,
                'sl': t.sl,
                'tp1': t.tp1,
                'tp2': t.tp2,
                'tp3': t.tp3,
                'score': t.score,
                'kills': t.kills,
                'trend_state': getattr(t, 'trend_state', ''),
                'trend_strength': 0,
                'pullback_quality': getattr(t, 'pullback_quality', ''),
                'rsi': 0,
                'adx': 0,
                'adx_1h': 0,
                'session': session,
                'hour_utc': hour_utc,
                'day_of_week': day_of_week,
                'bb_width_pct': 0,
                'volume_ratio': 0,
                'atr_pct': 0,
                'outcome': t.outcome,
                'pnl_r': t.pnl_r,
                'bars_to_outcome': t.bars_to_outcome,
                'closed_timestamp': t.timestamp,
                'engine_version': engine_version,
            })

        try:
            count = journal.bulk_insert_trades(records)
            logger.info(f"Journal: {count} trades recorded")
        except Exception as e:
            logger.warning(f"Journal insert failed: {e}")

    return all_trades


# ── 4. REPORT GENERATOR ────────────────────────────────────
def generate_report(trades: list, days: int) -> dict:
    """Generate comprehensive backtest report."""
    if not trades:
        return {'error': 'No trades found'}

    n = len(trades)

    # Outcome counts
    outcomes = {}
    for t in trades:
        outcomes[t.outcome] = outcomes.get(t.outcome, 0) + 1

    # PnL
    pnls = [t.pnl_r for t in trades]
    wins = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r < 0]
    bep_trades = [t for t in trades if t.pnl_r == 0]

    wr = len(wins) / n * 100 if n > 0 else 0
    avg_pnl = sum(pnls) / n if n > 0 else 0
    total_pnl = sum(pnls)

    # Drawdown
    equity = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    # By direction
    longs = [t for t in trades if t.direction == 'LONG']
    shorts = [t for t in trades if t.direction == 'SHORT']

    # By quality
    by_quality = {}
    for q in ['GOOD', 'MODERATE', 'WAIT']:
        q_trades = [t for t in trades if t.quality == q]
        if q_trades:
            q_wins = [t for t in q_trades if t.pnl_r > 0]
            by_quality[q] = {
                'n': len(q_trades),
                'wr': len(q_wins) / len(q_trades) * 100,
                'avg_pnl': sum(t.pnl_r for t in q_trades) / len(q_trades),
            }

    # By coin
    by_coin = {}
    for t in trades:
        if t.symbol not in by_coin:
            by_coin[t.symbol] = []
        by_coin[t.symbol].append(t)

    coin_stats = {}
    for coin, ct in by_coin.items():
        cw = [t for t in ct if t.pnl_r > 0]
        coin_stats[coin] = {
            'n': len(ct),
            'wr': len(cw) / len(ct) * 100 if ct else 0,
            'avg_pnl': sum(t.pnl_r for t in ct) / len(ct) if ct else 0,
            'total_pnl': sum(t.pnl_r for t in ct),
        }

    # Avg bars to outcome
    avg_bars = sum(t.bars_to_outcome for t in trades) / n if n else 0

    # Avg hold minutes (adaptive ke TF)
    tf_min = {'5m': 5, '15m': 15, '1h': 60}
    avg_hold_min = sum(
        t.bars_to_outcome * tf_min.get(getattr(t, 'tf', '15m'), 15)
        for t in trades) / n if n else 0

    # Per-TF breakdown
    by_tf = {}
    tf_seen = set(getattr(t, 'tf', '15m') for t in trades)
    for tf in tf_seen:
        tf_trades = [t for t in trades if getattr(t, 'tf', '15m') == tf]
        if tf_trades:
            tf_wins = [t for t in tf_trades if t.pnl_r > 0]
            by_tf[tf] = {
                'n': len(tf_trades),
                'wr': len(tf_wins) / len(tf_trades) * 100,
                'avg_pnl': sum(t.pnl_r for t in tf_trades) / len(tf_trades),
                'total_pnl': sum(t.pnl_r for t in tf_trades),
            }

    # Wedge vs BB breakdown
    wedge_trades = [t for t in trades if t.wedge_pattern]
    bb_trades = [t for t in trades if not t.wedge_pattern]

    report = {
        'total_trades': n,
        'days': days,
        'trades_per_week': n / (days / 7) if days > 0 else 0,
        'outcomes': outcomes,
        'win_rate': wr,
        'avg_pnl_r': avg_pnl,
        'total_pnl_r': total_pnl,
        'max_drawdown_r': max_dd,
        'avg_bars': avg_bars,
        'avg_hold_minutes': avg_hold_min,
        'by_tf': by_tf,
        'long': {
            'n': len(longs),
            'wr': len([t for t in longs if t.pnl_r > 0]) / len(longs) * 100 if longs else 0,
            'avg_pnl': sum(t.pnl_r for t in longs) / len(longs) if longs else 0,
        },
        'short': {
            'n': len(shorts),
            'wr': len([t for t in shorts if t.pnl_r > 0]) / len(shorts) * 100 if shorts else 0,
            'avg_pnl': sum(t.pnl_r for t in shorts) / len(shorts) if shorts else 0,
        },
        'by_quality': by_quality,
        'by_coin': coin_stats,
        'wedge_trades': {
            'n': len(wedge_trades),
            'wr': len([t for t in wedge_trades if t.pnl_r > 0]) / len(wedge_trades) * 100 if wedge_trades else 0,
        },
        'bb_trades': {
            'n': len(bb_trades),
            'wr': len([t for t in bb_trades if t.pnl_r > 0]) / len(bb_trades) * 100 if bb_trades else 0,
        },
    }

    return report


def print_report(report: dict):
    """Print formatted report."""
    if 'error' in report:
        print(f"\n  ERROR: {report['error']}")
        return

    print("\n" + "=" * 65)
    print(" SCALPING BACKTEST REPORT")
    print("=" * 65)

    print(f"\n  Period       : {report['days']} hari")
    print(f"  Total Trades : {report['total_trades']}")
    print(f"  Trades/week  : {report['trades_per_week']:.1f}")
    print(f"  Avg hold     : {report['avg_hold_minutes']:.0f} menit "
          f"({report['avg_bars']:.0f} candle)")

    print(f"\n  --- PERFORMANCE ---")
    print(f"  Win Rate     : {report['win_rate']:.1f}%")
    print(f"  Avg PnL/trade: {report['avg_pnl_r']:+.2f}R")
    print(f"  Total PnL    : {report['total_pnl_r']:+.1f}R")
    print(f"  Max Drawdown : {report['max_drawdown_r']:.1f}R")

    # Outcomes
    print(f"\n  --- OUTCOMES ---")
    for out in ['TP3', 'TP2', 'TP1', 'BEP', 'SL', 'EXPIRED']:
        cnt = report['outcomes'].get(out, 0)
        pct = cnt / report['total_trades'] * 100 if report['total_trades'] else 0
        bar = '#' * int(pct / 2)
        print(f"  {out:8s}: {cnt:4d} ({pct:5.1f}%) {bar}")

    # Dollar estimate
    risk_usd = 1.0
    total_usd = report['total_pnl_r'] * risk_usd
    dd_usd = report['max_drawdown_r'] * risk_usd
    monthly_usd = total_usd / (report['days'] / 30) if report['days'] > 0 else 0
    print(f"\n  --- DOLLAR ESTIMATE (risk = ${risk_usd}) ---")
    print(f"  Total Profit : ${total_usd:+.2f}")
    print(f"  Monthly est  : ${monthly_usd:+.2f}/bulan")
    print(f"  Max DD       : ${dd_usd:.2f}")

    # Direction
    print(f"\n  --- LONG vs SHORT ---")
    l = report['long']
    s = report['short']
    print(f"  LONG  : {l['n']:3d} trades, WR {l['wr']:.1f}%, "
          f"avg {l['avg_pnl']:+.2f}R")
    print(f"  SHORT : {s['n']:3d} trades, WR {s['wr']:.1f}%, "
          f"avg {s['avg_pnl']:+.2f}R")

    # Quality
    print(f"\n  --- BY QUALITY ---")
    for q in ['GOOD', 'MODERATE', 'WAIT']:
        if q in report['by_quality']:
            qd = report['by_quality'][q]
            print(f"  {q:8s}: {qd['n']:3d} trades, WR {qd['wr']:.1f}%, "
                  f"avg {qd['avg_pnl']:+.2f}R")

    # Wedge vs BB
    print(f"\n  --- WEDGE vs BB ---")
    w = report['wedge_trades']
    b = report['bb_trades']
    print(f"  Wedge : {w['n']:3d} trades, WR {w['wr']:.1f}%")
    print(f"  BB    : {b['n']:3d} trades, WR {b['wr']:.1f}%")

    # Per TF (kalau multi-TF)
    if report.get('by_tf') and len(report['by_tf']) > 1:
        print(f"\n  --- BY TIMEFRAME ---")
        for tf in sorted(report['by_tf'].keys()):
            td = report['by_tf'][tf]
            print(f"  {tf:5s} : {td['n']:3d} trades, WR {td['wr']:.1f}%, "
                  f"avg {td['avg_pnl']:+.2f}R, total {td['total_pnl']:+.1f}R")

    # Per coin (top 10)
    print(f"\n  --- TOP COINS ---")
    coins_sorted = sorted(report['by_coin'].items(),
                          key=lambda x: x[1]['total_pnl'], reverse=True)
    print(f"  {'Coin':8s} {'Trades':>6s} {'WR':>6s} {'AvgPnL':>8s} "
          f"{'TotalR':>8s}")
    for coin, cs in coins_sorted[:15]:
        print(f"  {coin:8s} {cs['n']:6d} {cs['wr']:5.1f}% "
              f"{cs['avg_pnl']:+7.2f}R {cs['total_pnl']:+7.1f}R")

    # Verdict
    print(f"\n  --- VERDICT ---")
    wr = report['win_rate']
    ev = report['avg_pnl_r']
    if wr >= 55 and ev > 0.3:
        print(f"  EXCELLENT — WR {wr:.0f}%, EV {ev:+.2f}R. "
              f"Siap live trading.")
    elif wr >= 50 and ev > 0.1:
        print(f"  GOOD — WR {wr:.0f}%, EV {ev:+.2f}R. "
              f"Bisa live dengan monitoring ketat.")
    elif wr >= 45 and ev > 0:
        print(f"  MARGINAL — WR {wr:.0f}%, EV {ev:+.2f}R. "
              f"Perlu tuning sebelum live.")
    else:
        print(f"  NEEDS WORK — WR {wr:.0f}%, EV {ev:+.2f}R. "
              f"Jangan live sebelum diperbaiki.")

    if report['trades_per_week'] < 5:
        print(f"  WARNING: Signal terlalu jarang "
              f"({report['trades_per_week']:.0f}/minggu)")
    elif report['trades_per_week'] > 100:
        print(f"  WARNING: Signal terlalu banyak "
              f"({report['trades_per_week']:.0f}/minggu) — mungkin noisy")


# ── 5. MAIN ─────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='Backtest Bot 2 (Scalping)')
    parser.add_argument('--days', type=int, default=90,
                        help='Periode backtest (default: 90 hari)')
    parser.add_argument('--coins', nargs='+', default=None,
                        help='Coin list (default: 100 coin)')
    parser.add_argument('--tf', choices=['15m', '5m', 'both'],
                        default='15m',
                        help='Timeframe utama (default: 15m). '
                             '"both" run separate + combined report.')
    parser.add_argument('--no-fetch', action='store_true',
                        help='Pakai cached data saja')
    parser.add_argument('--force-fetch', action='store_true',
                        help='Force re-download data')
    parser.add_argument('--verbose', action='store_true',
                        help='Print setiap trade')
    parser.add_argument('--two-pass', action='store_true',
                        help='Two-pass learning mode: Pass 1 learn, Pass 2 apply')
    parser.add_argument('--clear-journal', action='store_true',
                        help='Clear journal sebelum run (fresh start)')
    parser.add_argument('--rr-variant',
                        choices=['baseline', 'A', 'B', 'C', 'D', 'E',
                                 'a', 'b', 'c', 'd', 'e',
                                 'all', 'all2'],
                        default=None,
                        help='RR strategy variant. baseline=current, '
                             'A=BEP+buffer, B=cascade 50/30/20, '
                             'C=high-water trail post-TP1, '
                             'D=closer TP (rr2=1.3, rr3=2.0), '
                             'E=drop TP3 + cascade 70/30 full close. '
                             'all=baseline+A+B+C, all2=baseline+D+E.')
    args = parser.parse_args()

    multi_variant_list = None
    if args.rr_variant == 'all':
        multi_variant_list = ['baseline', 'a', 'b', 'c']
        logger.info("RR variant: ALL (multi-variant single-pass mode)")
        logging.getLogger('scalping_signal_engine').setLevel(logging.WARNING)
    elif args.rr_variant == 'all2':
        multi_variant_list = ['baseline', 'd', 'e']
        logger.info("RR variant: ALL2 (baseline + D + E)")
        logging.getLogger('scalping_signal_engine').setLevel(logging.WARNING)
    elif args.rr_variant:
        os.environ['RR_VARIANT'] = args.rr_variant.lower()
        logger.info(f"RR variant: {args.rr_variant.upper()}")

    coins = args.coins or DEFAULT_COINS
    days = args.days

    # Resolve TF list
    if args.tf == 'both':
        timeframes = ['15m', '5m']
    else:
        timeframes = [args.tf]

    tf_key = '_'.join(sorted(timeframes))
    cache_file = os.path.join(CACHE_DIR, f'scalp_data_{tf_key}.pkl')

    print("=" * 65)
    print(f" SCALPING BACKTEST — {len(coins)} coins, {days} hari")
    print(f" Timeframe: {'+'.join(timeframes)} (main) + 1H (HTF bias)")
    print(f" Teknik: BB(20,2) + RSI(14) + MACD(12,26,9) + Wedge")
    print("=" * 65)

    # Phase 1: Fetch data
    if args.no_fetch:
        if not os.path.exists(cache_file):
            print(f"ERROR: Cache file tidak ditemukan ({cache_file}). "
                  "Jalankan tanpa --no-fetch dulu.")
            return 1
        with open(cache_file, 'rb') as f:
            data = pickle.load(f)
        logger.info(f"Data loaded from cache: {cache_file}")
    else:
        data = fetch_all_data(coins, days, timeframes=timeframes,
                              force=args.force_fetch)

    # Clear journal kalau diminta
    if args.clear_journal and _LEARNING_ENABLED:
        journal.clear_trades()
        logger.info("Trade journal cleared")

    # Phase 2: Run backtest
    logger.info("\nMulai backtest...")
    start_ts = time.time()

    if args.two_pass and _LEARNING_ENABLED:
        # ───── TWO-PASS LEARNING MODE ─────
        logger.info("TWO-PASS MODE: Pass 1 = learning, Pass 2 = applied")

        # Pass 1: Run tanpa coin_learning (default params)
        # Clear cache dulu supaya default params dipakai
        learning = coin_learn.get_learning()
        learning._cache = {}  # clear in-memory
        learning._save_cache()

        logger.info("═══ PASS 1: Collecting stats ═══")
        trades_p1 = run_full_backtest(coins, data,
                                      timeframes=timeframes,
                                      verbose=args.verbose,
                                      record_journal=True,
                                      engine_version='v4.3_pass1')

        # Refresh coin learning dari Pass 1 data
        logger.info("Refreshing coin learning from Pass 1...")
        learning.refresh(coins)
        learning.print_summary()

        # Session gate refresh juga
        gate = session_filter.get_session_gate()
        gate.refresh()
        gate.print_summary()

        # Clear journal Pass 1 supaya Pass 2 stats bersih
        journal.clear_trades(engine_version='v4.3_pass1')

        logger.info("═══ PASS 2: Applying learned params ═══")
        trades = run_full_backtest(coins, data,
                                   timeframes=timeframes,
                                   verbose=args.verbose,
                                   record_journal=True,
                                   engine_version='v4.3_pass2')
    elif multi_variant_list:
        # Multi-variant single-pass: signal generation 1x, simulate 4x
        trades_mv = run_full_backtest(coins, data,
                                      timeframes=timeframes,
                                      verbose=False,
                                      record_journal=False,
                                      engine_version='v4.3',
                                      multi_variant=multi_variant_list)
        elapsed = time.time() - start_ts
        n_per_variant = {v: len(t) for v, t in trades_mv.items()}
        logger.info(f"Multi-variant backtest selesai: {n_per_variant} "
                    f"({elapsed:.1f}s)")

        # Save raw trades + print report per variant
        results_dir = 'backtesting/results'
        os.makedirs(results_dir, exist_ok=True)
        ts_label = datetime.now().strftime('%Y%m%d_%H%M')

        for v in multi_variant_list:
            v_trades = trades_mv[v]
            v_report = generate_report(v_trades, days)
            print(f"\n{'#' * 65}")
            print(f"# REPORT: VARIANT {v.upper()}")
            print(f"{'#' * 65}")
            print_report(v_report)

            # Save per-variant pkl + log
            try:
                pkl_path = os.path.join(
                    results_dir,
                    f'scalp_backtest_rr_{v}_{ts_label}.pkl')
                with open(pkl_path, 'wb') as f:
                    pickle.dump({'trades': v_trades, 'report': v_report}, f)
            except Exception as e:
                logger.warning(f"Save variant {v} failed: {e}")

        # Save combined dict for compare_rr_variants.py
        try:
            combined_path = os.path.join(
                results_dir,
                f'scalp_backtest_rr_all_{ts_label}.pkl')
            with open(combined_path, 'wb') as f:
                pickle.dump({
                    'trades_mv': trades_mv,
                    'reports': {v: generate_report(t, days)
                                for v, t in trades_mv.items()},
                }, f)
            logger.info(f"Combined results saved: {combined_path}")
        except Exception as e:
            logger.warning(f"Save combined failed: {e}")

        return 0
    else:
        # Single pass (no learning)
        trades = run_full_backtest(coins, data,
                                   timeframes=timeframes,
                                   verbose=args.verbose,
                                   record_journal=True,
                                   engine_version='v4.3')

    elapsed = time.time() - start_ts
    logger.info(f"Backtest selesai: {len(trades)} trades ({elapsed:.1f}s)")

    # Phase 3: Report
    # Untuk mode 'both', print report per-TF dulu, lalu COMBINED
    if len(timeframes) > 1:
        for tf in timeframes:
            tf_trades = [t for t in trades if getattr(t, 'tf', '15m') == tf]
            print(f"\n{'#' * 65}\n# REPORT: {tf} ONLY\n{'#' * 65}")
            print_report(generate_report(tf_trades, days))
        print(f"\n{'#' * 65}\n# REPORT: ALL COMBINED ({'+'.join(timeframes)})\n{'#' * 65}")
    report = generate_report(trades, days)
    print_report(report)

    # Print learning summary kalau ada
    if _LEARNING_ENABLED and trades:
        try:
            coin_learn.get_learning().refresh(coins)
            coin_learn.get_learning().print_summary()
            session_filter.get_session_gate().refresh()
            session_filter.get_session_gate().print_summary()
        except Exception as e:
            logger.debug(f"Learning summary failed: {e}")

    # Save results
    results_dir = 'backtesting/results'
    os.makedirs(results_dir, exist_ok=True)
    ts_label = datetime.now().strftime('%Y%m%d_%H%M')
    results_file = os.path.join(results_dir,
                                f'scalp_backtest_{ts_label}.pkl')
    try:
        with open(results_file, 'wb') as f:
            pickle.dump({'trades': trades, 'report': report}, f)
        logger.info(f"Results saved to {results_file}")
    except Exception as e:
        logger.warning(f"Save results failed: {e}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
