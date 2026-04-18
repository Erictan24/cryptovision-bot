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

# Coin pool untuk backtest — 20 coin paling likuid
DEFAULT_COINS = [
    'BTC', 'ETH', 'XRP', 'SOL', 'DOGE',
    'ADA', 'SUI', 'AVAX', 'LTC', 'FET',
    'DOT', 'ARB', 'OP', 'TON', 'INJ',
    'APT', 'SEI', 'WLD', 'FIL', 'TAO',
]

# Backtest parameters
SCAN_STEP = 1         # cek setiap candle (15m = tiap 15 menit)
MIN_WINDOW = 80       # minimum candle sebelum mulai scan
MAX_TRADE_BARS = 10   # v5.9.1: 10 × 15m = 2.5 jam (compromise: 8 terlalu ketat, 12 terlalu lama)
DEDUP_HOURS = 4       # lock signal sama selama 4 jam (trend following)


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


# ── 1. DATA FETCHER ─────────────────────────────────────────
def fetch_klines_paginated(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """
    Fetch OHLCV dari Binance dengan pagination.
    1000 candle per request. Paginate backward dari sekarang.
    """
    cpd = {'15m': 96, '1h': 24, '4h': 6}
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


def fetch_all_data(coins: list, days: int, force: bool = False) -> dict:
    """
    Fetch data untuk semua coin: 15m (main) + 1h (HTF bias).
    Cache ke disk untuk reuse.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Cek cache
    if not force and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'rb') as f:
                cached = pickle.load(f)
            meta = cached.get('_meta', {})
            if (meta.get('days', 0) >= days and
                    set(coins).issubset(set(meta.get('coins', []))) and
                    time.time() - meta.get('ts', 0) < 86400):
                logger.info(f"Using cached data ({meta.get('days')}d, "
                            f"{len(meta.get('coins', []))} coins)")
                return cached
        except Exception:
            pass

    logger.info(f"Fetching data: {len(coins)} coins, {days} days...")
    data = {'_meta': {'days': days, 'coins': coins, 'ts': time.time()}}

    for i, coin in enumerate(coins):
        logger.info(f"  [{i+1}/{len(coins)}] {coin}...")
        data[coin] = {}

        # 15m data
        df_15m = fetch_klines_paginated(coin, '15m', days)
        if df_15m is not None and len(df_15m) > MIN_WINDOW:
            data[coin]['15m'] = df_15m
            logger.info(f"    15m: {len(df_15m)} candles "
                        f"({df_15m['timestamp'].iloc[0].date()} -> "
                        f"{df_15m['timestamp'].iloc[-1].date()})")
        else:
            logger.warning(f"    15m: SKIP (insufficient data)")
            continue

        # 1h data (untuk HTF bias)
        df_1h = fetch_klines_paginated(coin, '1h', days)
        if df_1h is not None and len(df_1h) > 55:
            data[coin]['1h'] = df_1h
            logger.info(f"    1h: {len(df_1h)} candles")

        time.sleep(0.5)  # courtesy delay between coins

    # Save cache
    try:
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(data, f, protocol=4)
        logger.info(f"Data cached to {CACHE_FILE}")
    except Exception as e:
        logger.warning(f"Cache save failed: {e}")

    return data


# ── 2. TRADE SIMULATOR ─────────────────────────────────────
def simulate_trade(signal: dict, df_future: pd.DataFrame) -> TradeResult:
    """
    Simulasi trade setelah signal muncul.

    v5.9.1: BEP buffer REVERTED.
      v5.9 BEP buffer -0.15R bikin 13 BEP trades cost -1.95R (sebelumnya 0R).
      Data: BEP at 0R lebih baik — trade yang BEP memang sideways noise.
      Trailing: TP1 hit → SL ke entry (0R). TP2 hit → SL ke TP1 (lock TP1).
      MAX_TRADE_BARS 10 (2.5 jam, compromise antara 8 dan 12).

    Returns: TradeResult
    """
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

    # v5.9.1: BEP reverted ke exact entry (0R)
    bep_sl = entry  # exact BEP — no buffer

    tp1_hit = False
    tp2_hit = False
    current_sl = sl
    max_bars = min(MAX_TRADE_BARS, len(df_future))

    for i in range(max_bars):
        high_i = df_future['high'].iloc[i]
        low_i = df_future['low'].iloc[i]

        if direction == 'LONG':
            # Cek SL dulu (worst case)
            if low_i <= current_sl:
                if tp2_hit:
                    return _make_result(signal, 'TP2', i + 1, rr2)
                elif tp1_hit:
                    return _make_result(signal, 'BEP', i + 1, 0.0)
                else:
                    return _make_result(signal, 'SL', i + 1, -1.0)

            # Cek TP3
            if high_i >= tp3 and tp2_hit:
                return _make_result(signal, 'TP3', i + 1, rr3)

            # Cek TP2
            if high_i >= tp2:
                if not tp2_hit:
                    tp2_hit = True
                    current_sl = tp1  # SL naik ke TP1 (lock TP1 profit)

            # Cek TP1
            if high_i >= tp1:
                if not tp1_hit:
                    tp1_hit = True
                    current_sl = bep_sl  # SL naik ke BEP (entry)

        else:  # SHORT
            # Cek SL dulu
            if high_i >= current_sl:
                if tp2_hit:
                    return _make_result(signal, 'TP2', i + 1, rr2)
                elif tp1_hit:
                    return _make_result(signal, 'BEP', i + 1, 0.0)
                else:
                    return _make_result(signal, 'SL', i + 1, -1.0)

            # Cek TP3
            if low_i <= tp3 and tp2_hit:
                return _make_result(signal, 'TP3', i + 1, rr3)

            # Cek TP2
            if low_i <= tp2:
                if not tp2_hit:
                    tp2_hit = True
                    current_sl = tp1

            # Cek TP1
            if low_i <= tp1:
                if not tp1_hit:
                    tp1_hit = True
                    current_sl = bep_sl

    # Expired — hitung actual PnL dari harga terakhir
    if tp2_hit:
        return _make_result(signal, 'TP2', max_bars, rr2)
    elif tp1_hit:
        return _make_result(signal, 'TP1', max_bars, rr1)
    else:
        # Hitung actual PnL saat expire
        last_close = df_future['close'].iloc[max_bars - 1] \
            if max_bars <= len(df_future) else df_future['close'].iloc[-1]
        risk = abs(entry - sl)
        if risk > 0:
            if direction == 'LONG':
                actual_pnl = (last_close - entry) / risk
            else:
                actual_pnl = (entry - last_close) / risk
        else:
            actual_pnl = 0
        # Cap: tidak bisa lebih buruk dari SL
        actual_pnl = max(actual_pnl, -1.0)
        return _make_result(signal, 'EXPIRED', max_bars,
                            round(actual_pnl, 2))


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
def run_backtest_coin(symbol: str, df_15m: pd.DataFrame,
                      df_1h: pd.DataFrame = None,
                      verbose: bool = False) -> list:
    """
    Jalankan backtest untuk satu coin.
    Sliding window di data 15m, step=1 candle.
    """
    n = len(df_15m)
    if n < MIN_WINDOW + MAX_TRADE_BARS + 10:
        logger.warning(f"  {symbol}: data kurang ({n} candles)")
        return []

    trades = []
    last_signal = {}  # dedup: {direction: timestamp}

    for i in range(MIN_WINDOW, n - MAX_TRADE_BARS):
        # Ambil window data untuk analisa
        df_window = df_15m.iloc[max(0, i - 200):i + 1].copy()
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
            ts_now = df_15m['timestamp'].iloc[i]
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
            ts_now_local = df_15m['timestamp'].iloc[i]
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

        # Dedup: cek apakah sudah ada signal sama dalam 2 jam
        direction = signal['direction']
        ts = df_15m['timestamp'].iloc[i]
        dedup_key = direction

        if dedup_key in last_signal:
            hours_since = (ts - last_signal[dedup_key]).total_seconds() / 3600
            if hours_since < DEDUP_HOURS:
                continue

        last_signal[dedup_key] = ts

        # Tambahkan metadata
        signal['_symbol'] = symbol
        signal['_timestamp'] = str(ts)
        signal['_htf_bias'] = htf_ema

        # Simulasi trade
        df_future = df_15m.iloc[i + 1:i + 1 + MAX_TRADE_BARS]
        if len(df_future) < 5:
            continue

        result = simulate_trade(signal, df_future)
        trades.append(result)

        if verbose:
            ico = '+' if result.pnl_r > 0 else '-' if result.pnl_r < 0 else '='
            logger.info(f"  {ico} {ts} {direction} [{result.quality}] "
                        f"→ {result.outcome} ({result.pnl_r:+.1f}R) "
                        f"in {result.bars_to_outcome} bars")

    return trades


def run_full_backtest(coins: list, data: dict,
                      verbose: bool = False,
                      record_journal: bool = True,
                      engine_version: str = 'v4.3') -> list:
    """Jalankan backtest untuk semua coin."""
    all_trades = []

    for i, coin in enumerate(coins):
        if coin not in data or '15m' not in data.get(coin, {}):
            continue

        logger.info(f"[{i+1}/{len(coins)}] Backtest {coin}...")
        df_15m = data[coin]['15m']
        df_1h = data[coin].get('1h')

        trades = run_backtest_coin(coin, df_15m, df_1h, verbose)
        all_trades.extend(trades)

        logger.info(f"  {coin}: {len(trades)} trades")

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
        'avg_hold_minutes': avg_bars * 15,
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
        description='Backtest Bot 2 (Scalping 15m)')
    parser.add_argument('--days', type=int, default=90,
                        help='Periode backtest (default: 90 hari)')
    parser.add_argument('--coins', nargs='+', default=None,
                        help='Coin list (default: 20 coin)')
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
    args = parser.parse_args()

    coins = args.coins or DEFAULT_COINS
    days = args.days

    print("=" * 65)
    print(f" SCALPING BACKTEST — {len(coins)} coins, {days} hari")
    print(f" Timeframe: 15m (main) + 1H (HTF bias)")
    print(f" Teknik: BB(20,2) + RSI(14) + MACD(12,26,9) + Wedge")
    print("=" * 65)

    # Phase 1: Fetch data
    if args.no_fetch:
        if not os.path.exists(CACHE_FILE):
            print("ERROR: Cache file tidak ditemukan. "
                  "Jalankan tanpa --no-fetch dulu.")
            return 1
        with open(CACHE_FILE, 'rb') as f:
            data = pickle.load(f)
        logger.info("Data loaded from cache")
    else:
        data = fetch_all_data(coins, days, force=args.force_fetch)

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
                                   verbose=args.verbose,
                                   record_journal=True,
                                   engine_version='v4.3_pass2')
    else:
        # Single pass (no learning)
        trades = run_full_backtest(coins, data,
                                   verbose=args.verbose,
                                   record_journal=True,
                                   engine_version='v4.3')

    elapsed = time.time() - start_ts
    logger.info(f"Backtest selesai: {len(trades)} trades ({elapsed:.1f}s)")

    # Phase 3: Report
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
