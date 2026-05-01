"""
Fase 2: Test scalp + chart pattern + momentum sebagai fallback.

Kondisi:
A) BASELINE   — scalp only (MAX_BARS=15)
B) +CHART     — scalp + chart pattern di 15m candle
C) +MOMENTUM  — scalp + momentum di 15m candle
D) +BOTH      — scalp + chart + momentum

Memory-efficient: simpan (coin, i, source, signal) saja, not df_future.
Lookup df_future saat simulation via index reference.
"""
import pickle
import sys
import os
import logging
logging.disable(logging.CRITICAL)

import pandas as pd
import backtest_scalp as bs
from backtest_scalp import _make_result, TradeResult
from chart_pattern_signals import detect_chart_pattern_signal
from momentum_detector import detect_momentum
from indicators import calc_atr

SCALP_MAX_BARS = 15  # dari hasil Fase 1
DEDUP_HOURS    = 4

# Batasi test
TEST_COIN_LIMIT = 30


def map_chart(sig):
    if sig is None:
        return None
    return {
        'direction': sig['direction'], 'entry': sig['entry'],
        'sl': sig['sl'], 'tp1': sig['tp1'], 'tp2': sig['tp2'],
        'rr1': sig.get('rr1', 1.5), 'rr2': sig.get('rr2', 2.5),
    }


def map_momentum(sig):
    if sig is None:
        return None
    return {
        'direction': sig['direction'], 'entry': sig['entry'],
        'sl': sig['sl'], 'tp1': sig['tp1'], 'tp2': sig['tp2'],
        'rr1': sig.get('rr1', 1.5), 'rr2': sig.get('rr2', 2.5),
    }


CHECKPOINT_FILE = 'backtesting/cache/fase2_checkpoint.pkl'


def _load_checkpoint():
    """Load checkpoint kalau ada — skip coin yang sudah diproses."""
    if not os.path.exists(CHECKPOINT_FILE):
        return [], set()
    try:
        with open(CHECKPOINT_FILE, 'rb') as f:
            d = pickle.load(f)
        return d.get('records', []), set(d.get('done_coins', []))
    except Exception:
        return [], set()


def _save_checkpoint(records, done_coins):
    try:
        os.makedirs(os.path.dirname(CHECKPOINT_FILE), exist_ok=True)
        with open(CHECKPOINT_FILE, 'wb') as f:
            pickle.dump({'records': records, 'done_coins': list(done_coins)}, f)
    except Exception as e:
        print(f"  Checkpoint save failed: {e}", flush=True)


def collect_signals(coins, data):
    """
    Single pass. Untuk tiap coin, scan 15m candles dan record:
    (coin, i, source, signal_dict) — tidak simpan dataframe.

    Auto-save checkpoint per-coin → kalau crash/restart bisa resume.
    """
    all_records, done_coins = _load_checkpoint()
    if done_coins:
        print(f"  Resume dari checkpoint: {len(done_coins)} coin done, "
              f"{len(all_records)} records", flush=True)

    for idx, coin in enumerate(coins):
        if coin in done_coins:
            print(f"  [{idx+1}/{len(coins)}] {coin} (skip, already done)", flush=True)
            continue
        if coin not in data or '15m' not in data.get(coin, {}):
            done_coins.add(coin)
            continue
        print(f"  [{idx+1}/{len(coins)}] {coin}...", flush=True)

        df_15m = data[coin]['15m']
        df_1h = data[coin].get('1h')
        df_4h = data[coin].get('4h')

        # ── SCALP: run scalp engine, capture (coin, i, signal) ──
        scalp_ids = {}  # i → signal
        captured = []
        def capture(signal, df_future):
            captured.append(signal.copy())
            return _make_result(signal, 'TP2', 1, 2.0)  # dummy

        original_sim = bs.simulate_trade
        original_max = bs.MAX_TRADE_BARS
        bs.simulate_trade = capture
        bs.MAX_TRADE_BARS = SCALP_MAX_BARS
        try:
            # Track via timestamp → i mapping
            ts_to_i = {}
            for i in range(len(df_15m)):
                ts = df_15m.iloc[i]['timestamp']
                ts_to_i[ts] = i

            bs.run_backtest_coin(coin, df_15m, df_1h, verbose=False)

            for sig in captured:
                ts_str = sig.get('_timestamp', '')
                # Parse timestamp to find i
                try:
                    ts = pd.Timestamp(ts_str)
                    i = ts_to_i.get(ts)
                    if i is not None:
                        all_records.append((coin, i, 'SCALP', sig))
                except Exception:
                    pass
        finally:
            bs.simulate_trade = original_sim
            bs.MAX_TRADE_BARS = original_max

        # ── CHART + MOMENTUM: scan tiap 15m candle (dgn step 4 = tiap 1h) ──
        if df_15m is None or len(df_15m) < 400:
            continue

        # Step 4: hanya scan tiap 1h (tiap 4 candle 15m) — chart/momentum 1h TF
        for i in range(400, len(df_15m) - SCALP_MAX_BARS - 1, 4):
            try:
                ts = df_15m.iloc[i]['timestamp']
                price = float(df_15m.iloc[i]['close'])

                # df_1h slice sampai timestamp ini
                if df_1h is None:
                    continue
                df_1h_slice = df_1h[df_1h['timestamp'] <= ts].tail(100).reset_index(drop=True)
                if len(df_1h_slice) < 50:
                    continue
                df_4h_slice = df_4h[df_4h['timestamp'] <= ts].tail(50).reset_index(drop=True) if df_4h is not None else None
                df_15m_slice = df_15m.iloc[:i+1].reset_index(drop=True)

                try:
                    atr_1h = float(calc_atr(df_1h_slice, 14).iloc[-1])
                except Exception:
                    atr_1h = price * 0.01
                if atr_1h <= 0:
                    continue

                # Chart pattern
                try:
                    raw = detect_chart_pattern_signal(df_1h_slice, df_4h_slice,
                                                      price, atr_1h, coin)
                    sig = map_chart(raw)
                    if sig is not None:
                        all_records.append((coin, i, 'CHART', sig))
                except Exception:
                    pass

                # Momentum
                try:
                    raw = detect_momentum(df_1h_slice, df_4h_slice, df_15m_slice,
                                          price, atr_1h, coin)
                    sig = map_momentum(raw)
                    if sig is not None:
                        all_records.append((coin, i, 'MOMENTUM', sig))
                except Exception:
                    pass

            except Exception:
                continue

        # Save checkpoint setelah coin selesai
        done_coins.add(coin)
        _save_checkpoint(all_records, done_coins)

    return all_records


def simulate_record(coin, i, sig, data):
    """Simulate satu record, return pnl_r, outcome."""
    df_15m = data[coin]['15m']
    df_future = df_15m.iloc[i+1:i+1+SCALP_MAX_BARS].reset_index(drop=True)
    if len(df_future) < 3:
        return None
    direction = sig['direction']
    entry, sl, tp1, tp2 = sig['entry'], sig['sl'], sig['tp1'], sig['tp2']
    risk = abs(entry - sl)
    rr1 = sig.get('rr1', abs(tp1-entry)/risk if risk else 1.0)
    rr2 = sig.get('rr2', abs(tp2-entry)/risk if risk else 2.0)

    tp1_hit = False
    tp2_hit = False
    current_sl = sl
    max_bars = min(SCALP_MAX_BARS, len(df_future))

    for j in range(max_bars):
        h = df_future['high'].iloc[j]
        l = df_future['low'].iloc[j]
        if direction == 'LONG':
            if l <= current_sl:
                if tp2_hit: return {'outcome': 'TP2', 'pnl_r': rr2}
                if tp1_hit: return {'outcome': 'BEP', 'pnl_r': 0.0}
                return {'outcome': 'SL', 'pnl_r': -1.0}
            if h >= tp2 and not tp2_hit:
                tp2_hit = True
                current_sl = tp1
            if h >= tp1 and not tp1_hit:
                tp1_hit = True
                current_sl = entry
        else:
            if h >= current_sl:
                if tp2_hit: return {'outcome': 'TP2', 'pnl_r': rr2}
                if tp1_hit: return {'outcome': 'BEP', 'pnl_r': 0.0}
                return {'outcome': 'SL', 'pnl_r': -1.0}
            if l <= tp2 and not tp2_hit:
                tp2_hit = True
                current_sl = tp1
            if l <= tp1 and not tp1_hit:
                tp1_hit = True
                current_sl = entry

    if tp2_hit: return {'outcome': 'TP2', 'pnl_r': rr2}
    if tp1_hit: return {'outcome': 'TP1', 'pnl_r': rr1}
    # Expired — hitung unreal PnL
    last = df_future['close'].iloc[max_bars-1]
    unreal = (last - entry) / risk if direction == 'LONG' else (entry - last) / risk
    return {'outcome': 'EXPIRED', 'pnl_r': round(unreal, 2)}


def filter_config(records, allow_sources):
    """Priority SCALP > CHART > MOMENTUM."""
    by_candle = {}
    for coin, i, src, sig in records:
        if src not in allow_sources:
            continue
        key = (coin, i)
        prio = {'SCALP': 3, 'CHART': 2, 'MOMENTUM': 1}[src]
        existing = by_candle.get(key)
        if existing is None or prio > existing[0]:
            by_candle[key] = (prio, src, sig)

    flat = []
    for (coin, i), (_, src, sig) in by_candle.items():
        flat.append((coin, i, src, sig))
    flat.sort(key=lambda x: (x[0], x[1]))

    # Dedup per coin+direction dalam 4 jam (16 × 15m candle)
    last = {}
    dedup = []
    for coin, i, src, sig in flat:
        k = (coin, sig['direction'])
        if k in last:
            if i - last[k] < 16:  # 4 jam
                continue
        last[k] = i
        dedup.append((coin, i, src, sig))
    return dedup


def run_config(filtered, data, label):
    results = []
    for coin, i, src, sig in filtered:
        r = simulate_record(coin, i, sig, data)
        if r is None:
            continue
        r['source'] = src
        r['coin'] = coin
        r['direction'] = sig['direction']
        results.append(r)

    n = len(results)
    if n == 0:
        return {'label': label, 'n': 0, 'wr': 0, 'ev': 0, 'total': 0, 'sources': {}}
    wins = sum(1 for r in results if r['pnl_r'] > 0)
    total = sum(r['pnl_r'] for r in results)
    sources = {}
    for r in results:
        s = r['source']
        sources.setdefault(s, {'n': 0, 'wins': 0, 'pnl': 0})
        sources[s]['n'] += 1
        if r['pnl_r'] > 0:
            sources[s]['wins'] += 1
        sources[s]['pnl'] += r['pnl_r']
    return {
        'label': label, 'n': n, 'wr': wins/n*100, 'ev': total/n,
        'total': total, 'sources': sources,
    }


if __name__ == '__main__':
    print("Loading cache...", flush=True)
    with open('backtesting/cache/unified_data.pkl', 'rb') as f:
        data = pickle.load(f)
    all_coins = [c for c in data.keys() if c != '_meta' and '15m' in data[c]]
    coins = all_coins[:TEST_COIN_LIMIT]
    print(f"{len(coins)} coins (of {len(all_coins)})", flush=True)

    print("\n=== COLLECTING SIGNALS ===", flush=True)
    records = collect_signals(coins, data)

    src_count = {}
    for _, _, src, _ in records:
        src_count[src] = src_count.get(src, 0) + 1
    print(f"\nTotal: {len(records)} records", flush=True)
    print(f"By source: {src_count}", flush=True)

    print("\n=== SIMULATING 4 CONFIGS ===", flush=True)
    configs = [
        ('A) BASELINE ', {'SCALP'}),
        ('B) +CHART   ', {'SCALP', 'CHART'}),
        ('C) +MOMENTUM', {'SCALP', 'MOMENTUM'}),
        ('D) +BOTH    ', {'SCALP', 'CHART', 'MOMENTUM'}),
    ]

    results = []
    for label, srcs in configs:
        filtered = filter_config(records, srcs)
        stat = run_config(filtered, data, label)
        results.append(stat)
        if stat['n'] > 0:
            print(f"\n  {label} n={stat['n']} WR={stat['wr']:.1f}% "
                  f"EV={stat['ev']:+.2f}R Total={stat['total']:+.1f}R",
                  flush=True)
            for src, d in stat['sources'].items():
                swr = d['wins']/d['n']*100 if d['n'] else 0
                print(f"    [{src:9s}] n={d['n']:3d} WR={swr:5.1f}% "
                      f"PnL={d['pnl']:+.1f}R", flush=True)

    print("\n" + "="*60, flush=True)
    print(" SCALP FASE 2 SUMMARY", flush=True)
    print("="*60, flush=True)
    for s in results:
        print(f"{s['label']:16s} n={s['n']:3d}  WR={s['wr']:5.1f}%  "
              f"EV={s['ev']:+.2f}R  Total={s['total']:+6.1f}R  "
              f"$mo={s['total']/3:+5.1f}", flush=True)
