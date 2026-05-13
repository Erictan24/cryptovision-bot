"""
backtest_zone_margin.py — Bandingkan WR/volume untuk zone_margin_atr_mult 0.6 / 0.8 / 1.0

Usage:
    python backtest_zone_margin.py               -- fetch data dulu (cache 12 jam)
    python backtest_zone_margin.py --no-fetch    -- pakai cache langsung
"""

import sys
import time
import argparse
import copy
import pickle
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "backtesting"))

from config import SCAN_POOL, SIGNAL_PARAMS
from backtesting.data_fetcher import DataFetcher, CACHE_FILE
from backtesting.replay_engine import BacktestEngine
from backtesting.simulator import simulate_all
from backtesting.report import compute_report
from backtesting.run_backtest import scan_coin_tf
import signal_generator as sg

COINS  = SCAN_POOL          # 30 coin
TFS    = ["1h", "4h"]
DAYS   = 60
VALUES = [0.6, 0.8, 1.0]


def run_one(data, coins, zone_mult):
    """Jalankan backtest dengan zone_margin_atr_mult tertentu."""
    # Override SIGNAL_PARAMS — modifikasi dict in-place agar SP di signal_generator ikut update
    original = SIGNAL_PARAMS["zone_margin_atr_mult"]
    SIGNAL_PARAMS["zone_margin_atr_mult"] = zone_mult

    engine = BacktestEngine(data)
    all_results = []
    total_raw = 0
    total_simulated = 0

    for symbol in coins:
        for tf in TFS:
            try:
                raw = scan_coin_tf(engine, symbol, tf, data, verbose=False, use_per_coin=False)
            except Exception as e:
                print(f"  [WARN] {symbol} {tf}: {e}")
                continue

            if not raw:
                continue

            total_raw += len(raw)
            results = simulate_all(raw, tf)
            total_simulated += len(results)
            for r in results:
                r.symbol = symbol
                r.tf     = tf
            all_results.extend(results)

    print(f"  [debug] total raw signals: {total_raw}, after simulate: {total_simulated}", flush=True)

    # Restore
    SIGNAL_PARAMS["zone_margin_atr_mult"] = original
    return all_results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args()

    fetch_tfs = sorted(set(TFS) | {"4h", "1d"})
    if not args.no_fetch:
        print(f"Fetching data {len(COINS)} coins x {DAYS} hari...")
        fetcher = DataFetcher()
        data = fetcher.fetch_all(coins=COINS, tfs=fetch_tfs, days=DAYS)
    else:
        print(f"Memuat cache dari {CACHE_FILE}...")
        with open(CACHE_FILE, "rb") as f:
            data = pickle.load(f)
        print(f"Cache: {len(data)} coins")

    print(f"\n{'='*60}")
    print(f"  BACKTEST: zone_margin_atr_mult comparison")
    print(f"  Coins: {len(COINS)} | TF: {TFS} | Days: {DAYS}")
    print(f"{'='*60}\n")

    summary = []
    for mult in VALUES:
        print(f"--- zone_margin={mult} ---", flush=True)
        t0 = time.time()
        results = run_one(data, COINS, mult)
        elapsed = time.time() - t0

        if not results:
            summary.append((mult, 0, 0.0, 0.0, 0.0, 0.0, 0.0))
            print(f"  0 trades ({elapsed:.0f}s)\n", flush=True)
            continue

        # Hitung manual dari list trades — lebih reliable dari compute_report
        n   = len(results)
        tp2 = sum(1 for r in results if r.outcome == "TP2_HIT")
        tp1 = sum(1 for r in results if r.outcome == "TP1_HIT")
        sl  = sum(1 for r in results if r.outcome == "SL_HIT")
        wr  = (tp2 + tp1) / n * 100 if n > 0 else 0
        # EV per trade (dalam R unit)
        ev  = sum(getattr(r, "pnl_r", 0) or 0 for r in results) / n if n > 0 else 0
        per_mo = n / (DAYS / 30)
        # LONG / SHORT breakdown
        longs  = [r for r in results if getattr(r, "direction", "") == "LONG"]
        shorts = [r for r in results if getattr(r, "direction", "") == "SHORT"]
        long_wr  = sum(1 for r in longs  if r.outcome in ("TP2_HIT", "TP1_HIT")) / len(longs)  * 100 if longs  else 0
        short_wr = sum(1 for r in shorts if r.outcome in ("TP2_HIT", "TP1_HIT")) / len(shorts) * 100 if shorts else 0

        summary.append((mult, n, wr, ev, per_mo, long_wr, short_wr))
        print(f"  {n} trades | WR {wr:.1f}% | EV {ev:+.2f}R | "
              f"{per_mo:.0f}/mo | LONG WR {long_wr:.0f}% SHORT WR {short_wr:.0f}%  ({elapsed:.0f}s)\n",
              flush=True)

    print(f"\n{'='*60}")
    print(f"  HASIL PERBANDINGAN")
    print(f"{'='*60}")
    print(f"  {'mult':<6} {'trades':>7} {'WR%':>6} {'EV':>8} {'per/mo':>7} {'LONG%':>7} {'SHORT%':>7}")
    print(f"  {'-'*52}")
    for row in summary:
        mult, n, wr, ev, per_mo, long_wr, short_wr = row
        marker = "  <- baseline" if mult == 0.6 else ""
        print(f"  {mult:<6} {n:>7} {wr:>6.1f}% {ev:>+7.2f}R {per_mo:>7.0f} "
              f"{long_wr:>7.0f}% {short_wr:>7.0f}%{marker}")

    print(f"\nInterpretasi:")
    print(f"  - 0.6 = baseline sekarang")
    print(f"  - 0.8 = entry sedikit lebih jauh dari zona")
    print(f"  - 1.0 = entry paling jauh (volume tertinggi, WR mungkin turun)")
    print(f"  Target: naikkan trades/mo tanpa WR turun di bawah 60%")


if __name__ == "__main__":
    main()
