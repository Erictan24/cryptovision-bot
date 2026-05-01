"""
Test: swing + chart pattern + momentum sebagai fallback — OPTIMIZED.

Single pass per coin: collect semua 3 jenis signal sekaligus.
Lalu filter per config untuk compare.

Kondisi dibandingkan:
A) BASELINE  — swing saja
B) +CHART    — swing + chart fallback
C) +MOMENTUM — swing + momentum fallback
D) +BOTH     — swing + chart + momentum
"""
import pickle
import sys
import os
import logging
logging.disable(logging.CRITICAL)

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backtesting'))
from backtesting.replay_engine import BacktestEngine
from backtesting.simulator import simulate_outcome
from chart_pattern_signals import detect_chart_pattern_signal
from momentum_detector import detect_momentum
from indicators import calc_atr

SWING_MIN_WINDOW_1H = 200
SWING_MAX_BARS_1H   = 72
DEDUP_HOURS_SWING   = 8

# Batasi test ke 30 coin teratas (BTC, ETH, dll) untuk speed
TEST_COIN_LIMIT = 30


def map_chart_to_signal(sig):
    if sig is None: return None
    return {
        'direction'       : sig['direction'],
        'quality'         : 'GOOD',
        'entry'           : sig['entry'],
        'sl'              : sig['sl'],
        'tp1'             : sig['tp1'],
        'tp2'             : sig['tp2'],
        'rr1'             : sig.get('rr1', 1.5),
        'rr2'             : sig.get('rr2', 2.5),
        'confluence_score': int(sig.get('pattern_score', 70)),
    }


def map_momentum_to_signal(sig):
    if sig is None: return None
    return {
        'direction'       : sig['direction'],
        'quality'         : 'GOOD',
        'entry'           : sig['entry'],
        'sl'              : sig['sl'],
        'tp1'             : sig['tp1'],
        'tp2'             : sig['tp2'],
        'rr1'             : sig.get('rr1', 1.5),
        'rr2'             : sig.get('rr2', 2.5),
        'confluence_score': int(sig.get('momentum_score', 65)),
    }


def collect_all_signals(coins, data, historical_data):
    """
    Single pass: capture SWING + CHART + MOMENTUM signal per candle.
    Return list of (symbol, timestamp, source, signal, df_future).
    """
    engine = BacktestEngine(historical_data)
    all_records = []

    for idx, coin in enumerate(coins):
        if coin not in data or '1h' not in data.get(coin, {}):
            continue
        print(f"  [{idx+1}/{len(coins)}] {coin}...", flush=True)
        df_1h = data[coin].get('1h')
        df_4h = data[coin].get('4h')
        df_15m = data[coin].get('15m')
        if df_1h is None or len(df_1h) < SWING_MIN_WINDOW_1H + 20:
            continue

        n = len(df_1h)
        for i in range(SWING_MIN_WINDOW_1H, n - 10, 1):
            try:
                df_future = df_1h.iloc[i+1:i+1+200].reset_index(drop=True)
                if len(df_future) < 5:
                    continue
                ts = df_1h.iloc[i]['timestamp']
                price = float(df_1h.iloc[i]['close'])
                df_1h_slice = df_1h.iloc[:i+1]

                # ── SWING (main) ────────────────────
                swing_sig = None
                try:
                    engine.set_context(coin, '1h', i)
                    result, err = engine.analyze_coin(coin, '1h')
                    if not err and result is not None:
                        s = result.get("signal")
                        if s is None or s.get("quality") == "WAIT":
                            s = result.get("limit_signal")
                        if s is not None and s.get("quality") != "WAIT":
                            swing_sig = s
                except Exception:
                    pass

                # ── CHART ────────────────────────────
                chart_sig = None
                try:
                    atr = float(calc_atr(df_1h_slice.tail(50), 14).iloc[-1])
                    if atr > 0:
                        df_4h_slice = df_4h.iloc[:max(1, i//4+1)] if df_4h is not None else None
                        raw = detect_chart_pattern_signal(df_1h_slice, df_4h_slice,
                                                           price, atr, coin)
                        chart_sig = map_chart_to_signal(raw)
                except Exception:
                    pass

                # ── MOMENTUM ─────────────────────────
                mom_sig = None
                try:
                    atr = float(calc_atr(df_1h_slice.tail(50), 14).iloc[-1])
                    if atr > 0:
                        df_4h_slice = df_4h.iloc[:max(1, i//4+1)] if df_4h is not None else None
                        df_15m_slice = df_15m.iloc[:i*4+1] if df_15m is not None else None
                        raw = detect_momentum(df_1h_slice, df_4h_slice, df_15m_slice,
                                              price, atr, coin)
                        mom_sig = map_momentum_to_signal(raw)
                except Exception:
                    pass

                if swing_sig is not None:
                    all_records.append((coin, ts, 'SWING', swing_sig, df_future))
                if chart_sig is not None:
                    all_records.append((coin, ts, 'CHART', chart_sig, df_future))
                if mom_sig is not None:
                    all_records.append((coin, ts, 'MOMENTUM', mom_sig, df_future))

            except Exception:
                continue

    return all_records


def filter_config(records, allow_sources):
    """Filter signal per config, apply per-coin+direction dedup."""
    # Priority: SWING > CHART > MOMENTUM
    # Group by (coin, ts), pilih source tertinggi prioritas yang allowed
    by_candle = {}
    for coin, ts, src, sig, df_f in records:
        if src not in allow_sources:
            continue
        key = (coin, ts)
        prio = {'SWING': 3, 'CHART': 2, 'MOMENTUM': 1}[src]
        existing = by_candle.get(key)
        if existing is None or prio > existing[0]:
            by_candle[key] = (prio, src, sig, df_f)

    # Flatten
    flat = []
    for (coin, ts), (_, src, sig, df_f) in by_candle.items():
        flat.append((coin, ts, src, sig, df_f))

    # Dedup per coin+direction in 8 hours
    flat.sort(key=lambda x: (x[0], x[1]))
    last = {}
    dedup = []
    for coin, ts, src, sig, df_f in flat:
        k = (coin, sig['direction'])
        if k in last:
            hours = (ts - last[k]).total_seconds() / 3600
            if hours < DEDUP_HOURS_SWING:
                continue
        last[k] = ts
        dedup.append((coin, ts, src, sig, df_f))
    return dedup


def simulate_batch(filtered):
    trades = []
    for coin, ts, src, sig, df_f in filtered:
        try:
            outcome, bars, pnl_r = simulate_outcome(
                sig, df_f, '1h', max_bars=SWING_MAX_BARS_1H)
        except Exception:
            continue
        trades.append({
            'symbol': coin, 'source': src,
            'direction': sig['direction'],
            'outcome': outcome, 'pnl_r': pnl_r,
        })
    return trades


def summarize(trades, label):
    n = len(trades)
    if n == 0:
        return {'label': label, 'n': 0, 'wr': 0, 'ev': 0, 'total': 0,
                'sources': {}}
    wins = sum(1 for t in trades if t['pnl_r'] > 0)
    total = sum(t['pnl_r'] for t in trades)
    sources = {}
    for t in trades:
        s = t.get('source', 'UNKNOWN')
        sources.setdefault(s, {'n': 0, 'wins': 0, 'pnl': 0})
        sources[s]['n'] += 1
        if t['pnl_r'] > 0:
            sources[s]['wins'] += 1
        sources[s]['pnl'] += t['pnl_r']
    return {
        'label': label, 'n': n, 'wr': wins/n*100, 'ev': total/n,
        'total': total, 'sources': sources,
    }


if __name__ == '__main__':
    print("Loading cache...", flush=True)
    with open('backtesting/cache/unified_data.pkl', 'rb') as f:
        data = pickle.load(f)
    all_coins = [c for c in data.keys() if c != '_meta' and '1h' in data[c]]
    # Batasi ke TEST_COIN_LIMIT coin (urutan default = by volume)
    coins = all_coins[:TEST_COIN_LIMIT]
    print(f"{len(coins)} coins (limited from {len(all_coins)})", flush=True)

    historical_data = {c: data[c] for c in coins}

    print("\n=== COLLECTING SIGNALS (single pass) ===", flush=True)
    records = collect_all_signals(coins, data, historical_data)
    print(f"\nTotal signal records: {len(records)}", flush=True)
    src_count = {}
    for _, _, src, _, _ in records:
        src_count[src] = src_count.get(src, 0) + 1
    print(f"  By source: {src_count}", flush=True)

    # Save cache
    try:
        with open('backtesting/cache/swing_fallback_cache.pkl', 'wb') as f:
            pickle.dump(records, f)
        print("Saved cache to backtesting/cache/swing_fallback_cache.pkl",
              flush=True)
    except Exception as e:
        print(f"Save failed: {e}", flush=True)

    print("\n=== SIMULATING 4 CONFIGS ===", flush=True)
    configs = [
        ('A) BASELINE ', {'SWING'}),
        ('B) +CHART   ', {'SWING', 'CHART'}),
        ('C) +MOMENTUM', {'SWING', 'MOMENTUM'}),
        ('D) +BOTH    ', {'SWING', 'CHART', 'MOMENTUM'}),
    ]

    results = []
    for label, srcs in configs:
        filtered = filter_config(records, srcs)
        trades = simulate_batch(filtered)
        stat = summarize(trades, label)
        results.append(stat)
        if stat['n'] > 0:
            print(f"\n  {label} n={stat['n']} WR={stat['wr']:.1f}% "
                  f"EV={stat['ev']:+.2f}R Total={stat['total']:+.1f}R",
                  flush=True)
            for src, d in stat['sources'].items():
                src_wr = d['wins']/d['n']*100 if d['n'] else 0
                print(f"    [{src}] n={d['n']} WR={src_wr:.1f}% "
                      f"PnL={d['pnl']:+.1f}R", flush=True)

    print("\n" + "="*60, flush=True)
    print(" SUMMARY — SWING FALLBACK TEST", flush=True)
    print("="*60, flush=True)
    for s in results:
        print(f"{s['label']:18s} n={s['n']:3d}  WR={s['wr']:5.1f}%  "
              f"EV={s['ev']:+.2f}R  Total={s['total']:+6.1f}R  "
              f"$mo={s['total']/3:+5.1f}", flush=True)
