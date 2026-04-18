"""
run_backtest.py v4 — Main backtest orchestrator.

CHANGES dari v3:
  - 15m kembali tersedia (pakai Binance API sekarang)
  - Default TFs: 15m, 1h, 4h
  - MIN_WINDOW disesuaikan: 15m=200, 1h=150, 4h=50
  - SCAN_EVERY: 15m=8, 1h=4, 4h=2
  - Signal count warning jika < 100

Usage:
  python backtesting/run_backtest.py                  -- semua TF, 180 hari
  python backtesting/run_backtest.py --no-fetch       -- pakai cache
  python backtesting/run_backtest.py --force-fetch    -- download ulang
  python backtesting/run_backtest.py --tf 1h 4h       -- TF tertentu
  python backtesting/run_backtest.py --days 90        -- 90 hari
  python backtesting/run_backtest.py --coins BTC ETH  -- coin tertentu
  python backtesting/run_backtest.py --report FILE    -- re-analyze hasil
"""

import sys
import time
import argparse
import pickle
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import SCAN_POOL, SIGNAL_PARAMS as SP
from backtesting.data_fetcher import DataFetcher, CACHE_FILE
from backtesting.replay_engine import BacktestEngine, TF_HIERARCHY
from backtesting.simulator import simulate_all, dedup_signals, TradeResult
from backtesting.report import compute_report, print_report, save_results, load_trades


SCAN_EVERY = {
    "15m": 32,
    "1h" : 2,
    "4h" : 1,
}

MIN_WINDOW = {
    "15m": 200,
    "1h" : 200,
    "4h" : 100,
}

SUPPORTED_TFS = ["15m", "1h", "4h"]


def print_data_coverage(data: dict, tfs: list):
    print("\n=== DATA COVERAGE ===")
    header = f"  {'COIN':<8}"
    for tf in tfs:
        header += f"  {tf:>14}"
    print(header)

    warn = False
    for sym in sorted(data.keys()):
        row = f"  {sym:<8}"
        for tf in tfs:
            df = data[sym].get(tf)
            if df is not None:
                span = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).days
                row += f"  {len(df):>6,}c/{span}d"
                if span < 30:
                    row += "(!)"
                    warn = True
            else:
                row += f"  {'MISSING':>14}"
                warn = True
        print(row)

    if warn:
        print("\n  (!) = data pendek atau missing — re-run --force-fetch")
    print()


def scan_coin_tf(engine: BacktestEngine, symbol: str, tf: str,
                 data: dict, verbose: bool = False,
                 use_per_coin: bool = True) -> list:
    """
    Scan semua candles untuk satu coin x TF dengan config per-coin.

    ATURAN PENTING:
    - preferred_tf dari profil hanya INFORMASIONAL — tidak pernah memblok scan
    - Semua TF yang ada di SCAN_EVERY tetap di-scan untuk semua coin
    - Per-coin hanya adjust parameter scoring dan SL buffer
    """
    sym_data = data.get(symbol, {})
    df_main  = sym_data.get(tf)

    if df_main is None or len(df_main) < MIN_WINDOW[tf] + 20:
        return []

    # Load per-coin params — TIDAK ADA hard skip berdasarkan TF atau tradeable status
    coin_sp  = None
    skip_dir = None
    if use_per_coin:
        try:
            from backtesting.per_coin_config import get_coin_params
            coin_sp = get_coin_params(symbol)
            if coin_sp:
                skip_dir = coin_sp.get("_preferred_direction", "BOTH")
        except ImportError:
            pass  # per_coin_config belum ada, pakai global config

    n_candles = len(df_main)
    step      = SCAN_EVERY[tf]
    min_w     = MIN_WINDOW[tf]
    raw_signals = []
    n_errors    = 0
    n_scanned   = 0

    engine._per_coin_sp = coin_sp

    for i in range(min_w, n_candles - 10, step):
        n_scanned += 1
        try:
            engine.set_context(symbol, tf, i)
            result, err = engine.analyze_coin(symbol, tf)

            if err or result is None:
                continue

            signal = result.get("signal")
            # Fallback ke limit_signal kalau main signal tidak ada
            if signal is None or signal["quality"] == "WAIT":
                signal = result.get("limit_signal")
            if signal is None or signal.get("quality") == "WAIT":
                continue

            # Filter direction — hanya jika ada preferred direction yang kuat
            # LONG_PREFERRED atau SHORT_PREFERRED. BOTH = tidak filter.
            if skip_dir not in ("BOTH", None):
                sig_dir = signal.get("direction", "BOTH")
                if skip_dir == "LONG_PREFERRED" and sig_dir == "SHORT":
                    continue
                elif skip_dir == "SHORT_PREFERRED" and sig_dir == "LONG":
                    continue

            scan_ts   = df_main.iloc[i]["timestamp"]
            df_future = df_main.iloc[i+1:i+1+200].reset_index(drop=True)
            if len(df_future) < 5:
                continue

            signal["_symbol"] = symbol
            signal["_tf"]     = tf
            raw_signals.append((signal, df_future, scan_ts))

            if verbose:
                print(f"    [{i}/{n_candles}] {scan_ts.strftime('%Y-%m-%d %H:%M')} "
                      f"-> {signal['direction']} {signal['quality']} "
                      f"score={signal.get('confluence_score',0)} "
                      f"kills={signal.get('kill_count',0)}", flush=True)

        except Exception as e:
            n_errors += 1
            if verbose:
                print(f"    [{i}] Error: {e}", flush=True)
            continue

    engine._per_coin_sp = None

    if n_errors > 0 and n_errors > n_scanned * 0.1:
        print(f"  [WARN] {n_errors}/{n_scanned} error ({symbol} {tf})", flush=True)

    return dedup_signals(raw_signals)


def run_backtest(data: dict, coins: list, tfs: list,
                 verbose: bool = False) -> list[TradeResult]:
    """Run full backtest untuk semua coin x TF."""
    engine       = BacktestEngine(data)
    all_results  = []
    total_combos = len(coins) * len(tfs)
    done         = 0
    start_time   = time.time()

    print(f"\n{'='*64}")
    print(f"  FASE 2: Running backtest")
    print(f"  {len(coins)} coins x {len(tfs)} TFs = {total_combos} kombinasi")
    print(f"{'='*64}\n")

    for symbol in coins:
        for tf in tfs:
            done += 1
            elapsed = time.time() - start_time
            eta     = (elapsed / done * (total_combos - done)) if done > 1 else 0
            eta_str = f"{eta/60:.1f}m" if eta > 60 else f"{eta:.0f}s"

            df_check  = data.get(symbol, {}).get(tf)
            n_candles = len(df_check) if df_check is not None else 0

            print(f"  [{done:>3}/{total_combos}] {symbol:<8} {tf:<4}  "
                  f"(ETA: {eta_str}, {n_candles:,} candles)", end="", flush=True)

            if df_check is None or n_candles < MIN_WINDOW[tf] + 20:
                print(f"  SKIP — data tidak cukup ({n_candles} < {MIN_WINDOW[tf]+20})",
                      flush=True)
                continue

            t0          = time.time()
            raw_signals = scan_coin_tf(engine, symbol, tf, data, verbose)

            if not raw_signals:
                print(f"  0 signals ({time.time()-t0:.1f}s)", flush=True)
                continue

            results = simulate_all(raw_signals, tf)
            for r in results:
                r.symbol = symbol
                r.tf     = tf
            all_results.extend(results)

            n   = len(results)
            tp2 = sum(1 for r in results if r.outcome == "TP2_HIT")
            sl  = sum(1 for r in results if r.outcome == "SL_HIT")
            wr  = tp2 / n * 100 if n > 0 else 0
            print(f"  {n} signals  WR:{wr:.0f}%  TP2:{tp2} SL:{sl}  "
                  f"({time.time()-t0:.1f}s)", flush=True)

    total_elapsed = time.time() - start_time
    print(f"\n  Selesai dalam {total_elapsed/60:.1f} menit")
    print(f"  Total signals: {len(all_results)}")

    if len(all_results) < 100:
        print(f"\n  CATATAN: {len(all_results)} signals masih sedikit (target >=100).")
        print(f"  Coba turunkan threshold di config.py:")
        print(f"    score_moderate: {SP.get('score_moderate')} -> {max(1, SP.get('score_moderate',4)-1)}")
        print(f"    score_ideal   : {SP.get('score_ideal')} -> {SP.get('score_ideal',10)-1}")
    elif len(all_results) >= 200:
        print(f"\n  Bagus! {len(all_results)} signals — cukup untuk analisa statistik.")

    return all_results


def main():
    parser = argparse.ArgumentParser(description="Crypto Signal Bot Backtester v4")
    parser.add_argument("--days",        type=int,   default=180)
    parser.add_argument("--coins",       nargs="+",  default=None)
    parser.add_argument("--tf",          nargs="+",  default=["15m","1h","4h"])
    parser.add_argument("--fetch-only",  action="store_true")
    parser.add_argument("--no-fetch",    action="store_true")
    parser.add_argument("--force-fetch", action="store_true")
    parser.add_argument("--report",      type=str,   default=None)
    parser.add_argument("--verbose",     action="store_true")
    parser.add_argument("--label",       type=str,   default="default")
    args = parser.parse_args()

    if args.report:
        print(f"Loading: {args.report}")
        trades = load_trades(args.report)
        report = compute_report(trades)
        print_report(report, trades)
        return

    tfs = [tf for tf in args.tf if tf in SUPPORTED_TFS]
    if not tfs:
        tfs = SUPPORTED_TFS

    coins = args.coins if args.coins else SCAN_POOL

    print(f"\nBacktest Config:")
    print(f"  Coins     : {len(coins)} ({', '.join(coins[:5])}{'...' if len(coins)>5 else ''})")
    print(f"  Timeframes: {tfs}")
    print(f"  Period    : {args.days} hari")
    print(f"  Scan step : { {tf: SCAN_EVERY[tf] for tf in tfs} }")
    print(f"  Min window: { {tf: MIN_WINDOW[tf] for tf in tfs} }")
    print(f"  Data source: Binance public API")

    # Selalu fetch HTF yang dibutuhkan signal generator (detect_key_levels butuh 4h)
    htf_extras = set()
    for tf in tfs:
        if tf in ("15m", "1h"):
            htf_extras.add("4h")
        if tf == "15m":
            htf_extras.add("1h")
    fetch_tfs = sorted(set(tfs) | htf_extras | {"1d"})

    if not args.no_fetch:
        print(f"\n{'='*64}")
        print(f"  FASE 1: Fetching data dari Binance")
        print(f"{'='*64}")
        fetcher = DataFetcher()
        data    = fetcher.fetch_all(
            coins=coins,
            days=args.days,
            tfs=fetch_tfs,
            force_refresh=args.force_fetch,
        )
    else:
        if not CACHE_FILE.exists():
            print("ERROR: Tidak ada cached data.")
            print("Jalankan: python backtesting/run_backtest.py --force-fetch")
            sys.exit(1)
        print(f"\nLoading cached data...")
        with open(CACHE_FILE, "rb") as f:
            data = pickle.load(f)
        print(f"Loaded {len(data)} coins dari cache")

        # Validasi: cache harus punya 4h data kalau scan 1h/15m
        if htf_extras:
            sample_coin = next(iter(data), None)
            missing_htf = [tf for tf in htf_extras if sample_coin and tf not in data.get(sample_coin, {})]
            if missing_htf:
                print(f"\nWARN: Cache tidak punya data {missing_htf} yang dibutuhkan signal generator!")
                print(f"Signal akan 0 tanpa HTF data. Solusi:")
                print(f"  python backtesting/run_backtest.py --tf {' '.join(tfs)} --force-fetch")
                print(f"Melanjutkan dengan data yang ada...\n")

    print_data_coverage(data, tfs)

    if args.fetch_only:
        print("Done (fetch-only). Data siap di backtesting/cache/")
        return

    all_trades = run_backtest(data=data, coins=coins, tfs=tfs, verbose=args.verbose)

    if not all_trades:
        print("\nTidak ada signal ditemukan.")
        print("Kemungkinan filter terlalu ketat — coba turunkan threshold di config.py")
        return

    report = compute_report(all_trades)
    print_report(report, all_trades)
    save_results(all_trades, report, label=args.label)

    print("\n=== STATUS THRESHOLD (berdasarkan data) ===")
    by_q = report.get("by_quality", {})
    for q in ["IDEAL", "GOOD", "MODERATE"]:
        if q not in by_q:
            continue
        s  = by_q[q]
        ci = s.get("ci95", "?")
        n  = s["n"]
        if n < 30:
            status = f"SAMPLE KECIL (n={n}, butuh >=30 untuk valid)"
        elif s["wr_tp2"] >= 65:
            status = "EXCELLENT — sudah bisa dipakai live"
        elif s["wr_tp2"] >= 55:
            status = "BAGUS — perlu sedikit tuning lagi"
        elif s["wr_tp2"] >= 45:
            status = "BORDERLINE — masih perlu dioptimasi"
        else:
            status = "NEEDS WORK — turunkan/naikkan threshold"
        print(f"  {q:<10} WR:{s['wr_tp2']:.1f}%+-{ci}%  "
              f"EV:{s['exp_val']:+.2f}R  n={n}  -> {status}")

    print()
    print("Untuk tune: edit SIGNAL_PARAMS di config.py lalu:")
    print("  python backtesting/run_backtest.py --no-fetch")


if __name__ == "__main__":
    main()