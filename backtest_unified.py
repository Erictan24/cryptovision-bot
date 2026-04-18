"""
backtest_unified.py — Unified backtest: Bot 1 (SWING) + Bot 2 (SCALP)

Run kedua bot di data yang sama, tag setiap signal dengan engine,
hasilkan report yang split per engine + combined summary.

Features:
  - Fetch data sekali untuk 15m, 1h, 4h (shared cache)
  - Run Bot 2 (SCALP) di 15m
  - Run Bot 1 (SWING) di 1h
  - Tag setiap trade dengan 'SCALP' atau 'SWING'
  - Report split: scalp stats, swing stats, combined
  - Per-coin comparison table
  - Deploy-ready: sekali run, dua bot hasil

Usage:
  python backtest_unified.py                         # 20 coin, 90 hari
  python backtest_unified.py --days 30               # period pendek
  python backtest_unified.py --coins BTC ETH XRP     # subset coin
  python backtest_unified.py --no-fetch              # pakai cached data
  python backtest_unified.py --engines scalp swing   # salah satu saja
"""

import os
import sys
import io
import time
import pickle
import argparse
import logging
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd

# UTF-8 handling dilakukan di backtest_scalp.py (shared)

# Bot 2 (SCALP) imports
from scalping_signal_engine import generate_scalping_signal
from indicators import calc_atr, calc_rsi, calc_adx, analyze_ema_trend

# Bot 1 (SWING) imports
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backtesting'))
    from backtesting.replay_engine import BacktestEngine
    from backtesting.simulator import simulate_outcome, OUTCOME_TP2, \
        OUTCOME_TP1, OUTCOME_SL, OUTCOME_EXPIRED
    _SWING_AVAILABLE = True
except ImportError as e:
    print(f"WARNING: Bot 1 (SWING) not available: {e}")
    _SWING_AVAILABLE = False

# Reuse scalp fetcher
from backtest_scalp import (
    fetch_all_data as fetch_scalp_data,
    simulate_trade as simulate_scalp_trade,
    run_backtest_coin as run_scalp_coin,
    CACHE_FILE as SCALP_CACHE_FILE,
)

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


# ── Unified Coin Pool ───────────────────────────────────────
DEFAULT_COINS = [
    'BTC', 'ETH', 'XRP', 'SOL', 'DOGE',
    'ADA', 'SUI', 'AVAX', 'LTC', 'FET',
    'DOT', 'ARB', 'OP', 'TON', 'INJ',
    'APT', 'SEI', 'WLD', 'FIL', 'TAO',
]


# ── Unified Trade Record ───────────────────────────────────
@dataclass
class UnifiedTrade:
    """Trade record dari kedua engine dengan tag."""
    engine: str           # 'SCALP' atau 'SWING'
    symbol: str
    tf: str
    direction: str
    quality: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float = 0.0
    rr1: float = 1.0
    rr2: float = 1.8
    score: int = 0
    kills: int = 0
    timestamp: str = ''
    outcome: str = ''
    bars_to_outcome: int = 0
    pnl_r: float = 0.0
    reasons: list = field(default_factory=list)


# ═══════════════════════════════════════════════════════════
#  BOT 2 (SCALP) BACKTEST WRAPPER
# ═══════════════════════════════════════════════════════════
def run_scalp_backtest(coins: list, data: dict,
                       verbose: bool = False) -> list:
    """Run Bot 2 backtest, return list of UnifiedTrade."""
    logger.info("═══ RUNNING BOT 2 (SCALP) ═══")
    all_trades = []

    for i, coin in enumerate(coins):
        if coin not in data or '15m' not in data.get(coin, {}):
            continue

        logger.info(f"[SCALP {i+1}/{len(coins)}] {coin}...")
        df_15m = data[coin]['15m']
        df_1h = data[coin].get('1h')

        scalp_trades = run_scalp_coin(coin, df_15m, df_1h, verbose)

        # Convert to UnifiedTrade
        for t in scalp_trades:
            all_trades.append(UnifiedTrade(
                engine='SCALP',
                symbol=t.symbol,
                tf='15m',
                direction=t.direction,
                quality=t.quality,
                entry=t.entry,
                sl=t.sl,
                tp1=t.tp1,
                tp2=t.tp2,
                tp3=getattr(t, 'tp3', t.tp2),
                rr1=t.rr1,
                rr2=t.rr2,
                score=t.score,
                kills=t.kills,
                timestamp=t.timestamp,
                outcome=t.outcome,
                bars_to_outcome=t.bars_to_outcome,
                pnl_r=t.pnl_r,
                reasons=t.reasons,
            ))

        logger.info(f"  SCALP {coin}: {len(scalp_trades)} trades")

    return all_trades


# ═══════════════════════════════════════════════════════════
#  BOT 1 (SWING) BACKTEST WRAPPER
# ═══════════════════════════════════════════════════════════
# Swing backtest parameters
SWING_SCAN_EVERY_1H = 2    # scan setiap 2 candle 1H
SWING_MIN_WINDOW_1H = 200
SWING_MAX_BARS_1H = 72     # 72 × 1h = 3 hari
DEDUP_HOURS_SWING = 8


def run_swing_backtest_coin(engine: 'BacktestEngine', symbol: str,
                             df_1h: pd.DataFrame, df_4h: pd.DataFrame,
                             verbose: bool = False) -> list:
    """Run swing backtest untuk satu coin di 1H."""
    if df_1h is None or len(df_1h) < SWING_MIN_WINDOW_1H + 20:
        return []

    n = len(df_1h)
    step = SWING_SCAN_EVERY_1H
    raw_signals = []
    n_errors = 0

    for i in range(SWING_MIN_WINDOW_1H, n - 10, step):
        try:
            engine.set_context(symbol, '1h', i)
            result, err = engine.analyze_coin(symbol, '1h')

            if err or result is None:
                continue

            signal = result.get("signal")
            if signal is None or signal.get("quality") == "WAIT":
                signal = result.get("limit_signal")
            if signal is None or signal.get("quality") == "WAIT":
                continue

            scan_ts = df_1h.iloc[i]["timestamp"]
            df_future = df_1h.iloc[i + 1:i + 1 + 200].reset_index(drop=True)
            if len(df_future) < 5:
                continue

            signal["_symbol"] = symbol
            signal["_tf"] = "1h"
            raw_signals.append((signal, df_future, scan_ts))

            if verbose:
                logger.info(f"    [SWING {i}/{n}] {signal['direction']} "
                            f"{signal['quality']} score="
                            f"{signal.get('confluence_score', 0)}")

        except Exception as e:
            n_errors += 1
            if verbose:
                logger.debug(f"    Swing scan err: {e}")
            continue

    # Dedup: same symbol+direction within 8 hours
    last = {}
    dedup_signals = []
    for sig, df_f, ts in raw_signals:
        key = sig['direction']
        if key in last:
            hours = (ts - last[key]).total_seconds() / 3600
            if hours < DEDUP_HOURS_SWING:
                continue
        last[key] = ts
        dedup_signals.append((sig, df_f, ts))

    # Simulate outcomes
    trades = []
    for sig, df_future, scan_ts in dedup_signals:
        try:
            outcome, bars, pnl_r = simulate_outcome(
                sig, df_future, '1h', max_bars=SWING_MAX_BARS_1H)
        except Exception as e:
            logger.debug(f"Simulate err: {e}")
            continue

        trades.append(UnifiedTrade(
            engine='SWING',
            symbol=symbol,
            tf='1h',
            direction=sig['direction'],
            quality=sig.get('quality', 'GOOD'),
            entry=sig.get('entry', 0),
            sl=sig.get('sl', 0),
            tp1=sig.get('tp1', 0),
            tp2=sig.get('tp2', 0),
            tp3=sig.get('tp_max', 0),
            rr1=sig.get('rr1', 1.0),
            rr2=sig.get('rr2', 1.8),
            score=sig.get('confluence_score', 0),
            kills=sig.get('kill_count', 0),
            timestamp=str(scan_ts),
            outcome=outcome,
            bars_to_outcome=bars,
            pnl_r=pnl_r,
            reasons=sig.get('reasons', []),
        ))

    return trades


def run_swing_backtest(coins: list, data: dict,
                       verbose: bool = False) -> list:
    """Run Bot 1 backtest, return list of UnifiedTrade."""
    if not _SWING_AVAILABLE:
        logger.warning("SWING engine not available, skipping")
        return []

    logger.info("═══ RUNNING BOT 1 (SWING) ═══")

    # Convert data format untuk BacktestEngine (butuh 1h + 4h)
    historical_data = {}
    for coin in coins:
        if coin not in data:
            continue
        coin_data = {}
        if '1h' in data[coin]:
            coin_data['1h'] = data[coin]['1h']
        if '4h' in data[coin]:
            coin_data['4h'] = data[coin]['4h']
        if '15m' in data[coin]:
            coin_data['15m'] = data[coin]['15m']
        if coin_data:
            historical_data[coin] = coin_data

    try:
        engine = BacktestEngine(historical_data)
    except Exception as e:
        logger.error(f"Failed to init BacktestEngine: {e}")
        return []

    all_trades = []
    for i, coin in enumerate(coins):
        if coin not in historical_data:
            continue

        df_1h = historical_data[coin].get('1h')
        df_4h = historical_data[coin].get('4h')
        if df_1h is None:
            continue

        logger.info(f"[SWING {i+1}/{len(coins)}] {coin}...")
        try:
            trades = run_swing_backtest_coin(
                engine, coin, df_1h, df_4h, verbose)
            all_trades.extend(trades)
            logger.info(f"  SWING {coin}: {len(trades)} trades")
        except Exception as e:
            logger.error(f"SWING {coin} error: {e}")
            continue

    return all_trades


# ═══════════════════════════════════════════════════════════
#  DATA FETCHER — UNIFIED (15m + 1h + 4h)
# ═══════════════════════════════════════════════════════════
UNIFIED_CACHE_FILE = 'backtesting/cache/unified_data.pkl'


def fetch_unified_data(coins: list, days: int,
                       force: bool = False) -> dict:
    """Fetch 15m + 1h + 4h untuk semua coin, cache ke disk."""
    os.makedirs(os.path.dirname(UNIFIED_CACHE_FILE), exist_ok=True)

    # Try cache
    if not force and os.path.exists(UNIFIED_CACHE_FILE):
        try:
            with open(UNIFIED_CACHE_FILE, 'rb') as f:
                cached = pickle.load(f)
            meta = cached.get('_meta', {})
            cached_coins = set(meta.get('coins', []))
            if (meta.get('days', 0) >= days and
                    set(coins).issubset(cached_coins) and
                    time.time() - meta.get('ts', 0) < 86400):
                logger.info(f"Loaded unified cache "
                            f"({meta.get('days')}d, {len(cached_coins)} coins)")
                return cached
        except Exception as e:
            logger.debug(f"Cache load failed: {e}")

    # Fetch fresh: reuse scalp fetcher (yang fetch 15m + 1h)
    logger.info(f"Fetching 15m + 1h data untuk {len(coins)} coins...")
    scalp_data = fetch_scalp_data(coins, days, force=force)

    # Additionally fetch 4h untuk swing engine
    from backtest_scalp import fetch_klines_paginated
    logger.info("Fetching additional 4h data...")
    for i, coin in enumerate(coins):
        if coin not in scalp_data:
            continue
        if '4h' in scalp_data[coin]:
            continue  # already have
        logger.info(f"  [{i+1}/{len(coins)}] {coin} 4h...")
        df_4h = fetch_klines_paginated(coin, '4h', days)
        if df_4h is not None:
            scalp_data[coin]['4h'] = df_4h
        time.sleep(0.3)

    # Update meta
    scalp_data['_meta'] = {
        'days': days,
        'coins': coins,
        'ts': time.time(),
        'unified': True,
    }

    # Save unified cache
    try:
        with open(UNIFIED_CACHE_FILE, 'wb') as f:
            pickle.dump(scalp_data, f, protocol=4)
        logger.info(f"Unified cache saved to {UNIFIED_CACHE_FILE}")
    except Exception as e:
        logger.warning(f"Cache save failed: {e}")

    return scalp_data


# ═══════════════════════════════════════════════════════════
#  UNIFIED REPORT
# ═══════════════════════════════════════════════════════════
def compute_stats(trades: list, label: str = '') -> dict:
    """Compute stats dari list of UnifiedTrade."""
    if not trades:
        return {
            'label': label,
            'n': 0, 'wr': 0, 'ev': 0, 'total_pnl': 0,
            'max_dd': 0, 'tp1_rate': 0, 'tp2_rate': 0,
            'sl_rate': 0, 'expired_rate': 0,
        }

    n = len(trades)
    wins = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r < 0]
    total_pnl = sum(t.pnl_r for t in trades)
    avg_pnl = total_pnl / n

    # Outcome breakdown
    tp2_hits = sum(1 for t in trades if 'TP2' in t.outcome)
    tp1_hits = sum(1 for t in trades if 'TP1' in t.outcome)
    tp3_hits = sum(1 for t in trades if 'TP3' in t.outcome)
    sl_hits = sum(1 for t in trades if 'SL' in t.outcome)
    expired = sum(1 for t in trades if 'EXPIRED' in t.outcome)
    bep = sum(1 for t in trades if t.outcome == 'BEP')

    # Drawdown
    equity = 0
    peak = 0
    max_dd = 0
    for t in trades:
        equity += t.pnl_r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    # Direction breakdown
    longs = [t for t in trades if t.direction == 'LONG']
    shorts = [t for t in trades if t.direction == 'SHORT']
    long_wr = (sum(1 for t in longs if t.pnl_r > 0) / len(longs) * 100
               if longs else 0)
    short_wr = (sum(1 for t in shorts if t.pnl_r > 0) / len(shorts) * 100
                if shorts else 0)

    return {
        'label': label,
        'n': n,
        'wr': len(wins) / n * 100,
        'ev': avg_pnl,
        'total_pnl': total_pnl,
        'max_dd': max_dd,
        'tp1_rate': tp1_hits / n * 100,
        'tp2_rate': tp2_hits / n * 100,
        'tp3_rate': tp3_hits / n * 100,
        'sl_rate': sl_hits / n * 100,
        'expired_rate': expired / n * 100,
        'bep_rate': bep / n * 100,
        'long_n': len(longs),
        'long_wr': long_wr,
        'short_n': len(shorts),
        'short_wr': short_wr,
    }


def print_unified_report(trades: list, days: int):
    """Print unified report dengan split scalp vs swing."""
    scalp_trades = [t for t in trades if t.engine == 'SCALP']
    swing_trades = [t for t in trades if t.engine == 'SWING']

    scalp_stats = compute_stats(scalp_trades, 'SCALP')
    swing_stats = compute_stats(swing_trades, 'SWING')
    combined_stats = compute_stats(trades, 'COMBINED')

    print("\n" + "=" * 75)
    print(" UNIFIED BACKTEST REPORT — BOT 1 (SWING) + BOT 2 (SCALP)")
    print("=" * 75)
    print(f" Period: {days} hari\n")

    # Side-by-side comparison
    print(f" {'Metric':<20} {'SCALP':>15} {'SWING':>15} {'COMBINED':>15}")
    print(" " + "-" * 73)

    def row(label, key, fmt="{:.1f}", suffix=""):
        s = scalp_stats.get(key, 0)
        w = swing_stats.get(key, 0)
        c = combined_stats.get(key, 0)
        print(f" {label:<20} "
              f"{(fmt+suffix).format(s):>15} "
              f"{(fmt+suffix).format(w):>15} "
              f"{(fmt+suffix).format(c):>15}")

    row('Trades', 'n', '{:.0f}')
    row('Win Rate', 'wr', '{:.1f}', '%')
    row('Avg PnL/trade', 'ev', '{:+.2f}', 'R')
    row('Total PnL', 'total_pnl', '{:+.1f}', 'R')
    row('Max Drawdown', 'max_dd', '{:.1f}', 'R')
    row('TP1 rate', 'tp1_rate', '{:.1f}', '%')
    row('TP2 rate', 'tp2_rate', '{:.1f}', '%')
    row('SL rate', 'sl_rate', '{:.1f}', '%')
    row('Expired rate', 'expired_rate', '{:.1f}', '%')
    row('LONG trades', 'long_n', '{:.0f}')
    row('LONG WR', 'long_wr', '{:.1f}', '%')
    row('SHORT trades', 'short_n', '{:.0f}')
    row('SHORT WR', 'short_wr', '{:.1f}', '%')

    # Dollar estimate
    risk_usd = 1.0
    print(f"\n --- DOLLAR ESTIMATE (risk = ${risk_usd}) ---")
    for stats in [scalp_stats, swing_stats, combined_stats]:
        total = stats['total_pnl'] * risk_usd
        monthly = total / (days / 30) if days > 0 else 0
        dd = stats['max_dd'] * risk_usd
        print(f" {stats['label']:<10} Total=${total:+.2f} "
              f"Monthly=${monthly:+.2f}/mo "
              f"MaxDD=${dd:.2f}")

    # Per-coin comparison
    print(f"\n --- PER-COIN PERFORMANCE ---")
    print(f" {'Coin':<8} {'SCALP n/WR/PnL':<25} {'SWING n/WR/PnL':<25}")
    print(" " + "-" * 58)

    coins = sorted(set(t.symbol for t in trades))
    for coin in coins:
        s_trades = [t for t in scalp_trades if t.symbol == coin]
        w_trades = [t for t in swing_trades if t.symbol == coin]

        def coin_line(ts):
            if not ts:
                return '         -         '
            n = len(ts)
            wr = sum(1 for t in ts if t.pnl_r > 0) / n * 100
            pnl = sum(t.pnl_r for t in ts)
            return f"{n:3d}/{wr:5.1f}%/{pnl:+6.1f}R"

        print(f" {coin:<8} {coin_line(s_trades):<25} "
              f"{coin_line(w_trades):<25}")

    # Verdict
    print(f"\n --- VERDICT ---")
    for stats in [scalp_stats, swing_stats]:
        if stats['n'] == 0:
            continue
        label = stats['label']
        wr = stats['wr']
        ev = stats['ev']
        if wr >= 55 and ev > 0.3:
            verdict = "EXCELLENT — siap live trading"
        elif wr >= 50 and ev > 0.1:
            verdict = "GOOD — live dengan monitoring"
        elif wr >= 45 and ev > 0:
            verdict = "MARGINAL — perlu tuning"
        else:
            verdict = "NEEDS WORK — jangan live"
        print(f" [{label}] WR {wr:.1f}%, EV {ev:+.2f}R → {verdict}")


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description='Unified Backtest: Bot 1 (SWING) + Bot 2 (SCALP)')
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--coins', nargs='+', default=None)
    parser.add_argument('--no-fetch', action='store_true')
    parser.add_argument('--force-fetch', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--engines', nargs='+',
                        default=['scalp', 'swing'],
                        choices=['scalp', 'swing'],
                        help='Engines to run')
    args = parser.parse_args()

    coins = args.coins or DEFAULT_COINS
    days = args.days

    print("=" * 75)
    print(f" UNIFIED BACKTEST — Bot 1 (SWING) + Bot 2 (SCALP)")
    print(f" {len(coins)} coins × {days} hari × {len(args.engines)} engines")
    print("=" * 75)

    # Phase 1: Fetch data
    if args.no_fetch:
        if not os.path.exists(UNIFIED_CACHE_FILE):
            # Fallback: cek scalp cache
            if os.path.exists(SCALP_CACHE_FILE):
                print("Unified cache tidak ada, pakai scalp cache (no 4h)")
                with open(SCALP_CACHE_FILE, 'rb') as f:
                    data = pickle.load(f)
            else:
                print("ERROR: No cache available")
                return 1
        else:
            with open(UNIFIED_CACHE_FILE, 'rb') as f:
                data = pickle.load(f)
            logger.info("Loaded unified cache")
    else:
        data = fetch_unified_data(coins, days, force=args.force_fetch)

    # Phase 2: Run backtests
    all_trades = []
    start_ts = time.time()

    if 'scalp' in args.engines:
        scalp_trades = run_scalp_backtest(coins, data, verbose=args.verbose)
        all_trades.extend(scalp_trades)
        logger.info(f"SCALP: {len(scalp_trades)} trades total")

    if 'swing' in args.engines and _SWING_AVAILABLE:
        swing_trades = run_swing_backtest(coins, data, verbose=args.verbose)
        all_trades.extend(swing_trades)
        logger.info(f"SWING: {len(swing_trades)} trades total")

    elapsed = time.time() - start_ts
    logger.info(f"\nTotal backtest selesai: {len(all_trades)} trades "
                f"({elapsed:.1f}s)")

    # Phase 3: Report
    print_unified_report(all_trades, days)

    # Save results
    results_dir = 'backtesting/results'
    os.makedirs(results_dir, exist_ok=True)
    ts_label = datetime.now().strftime('%Y%m%d_%H%M')
    results_file = os.path.join(results_dir,
                                f'unified_backtest_{ts_label}.pkl')
    try:
        with open(results_file, 'wb') as f:
            # Convert UnifiedTrade to dict untuk pickle portability
            serializable = {
                'trades': [asdict(t) for t in all_trades],
                'days': days,
                'coins': coins,
                'engines': args.engines,
                'timestamp': ts_label,
            }
            pickle.dump(serializable, f)
        logger.info(f"Results saved to {results_file}")
    except Exception as e:
        logger.warning(f"Save results failed: {e}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
