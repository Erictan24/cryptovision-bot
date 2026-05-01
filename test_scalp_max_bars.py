"""
Test: extend MAX_TRADE_BARS scalp dari 10 ke 15 ke 20.
Capture signal sekali, simulate dengan 3 bar limit berbeda.
"""
import pickle
import logging
logging.disable(logging.CRITICAL)

import pandas as pd
import backtest_scalp as bs
from backtest_scalp import TradeResult, _make_result


# Override df_future capture range — kita butuh sampai 25 bars
EXTENDED_BARS = 25


def collect_signals_extended(coins, data):
    """Capture signals + df_future sampai EXTENDED_BARS."""
    captured = []

    # Patch simulate_trade — capture signal + extended df_future
    def capture(signal, df_future):
        # df_future yang dikirim sudah di-slice ke MAX_TRADE_BARS
        # Kita butuh full 25 bars, jadi hack: re-slice dari aslinya via symbol
        captured.append((signal.copy(), df_future.copy()))
        return _make_result(signal, 'TP2', 1, 2.0)

    # Patch run_backtest_coin agar df_future-nya extended
    original_sim = bs.simulate_trade
    original_max = bs.MAX_TRADE_BARS
    bs.simulate_trade = capture
    bs.MAX_TRADE_BARS = EXTENDED_BARS  # kasih lebih banyak bars supaya bisa test 15, 20
    try:
        for i, coin in enumerate(coins):
            if coin not in data or '15m' not in data.get(coin, {}):
                continue
            print(f"  [{i+1}/{len(coins)}] {coin}...", flush=True)
            df_15m = data[coin]['15m']
            df_1h  = data[coin].get('1h')
            bs.run_backtest_coin(coin, df_15m, df_1h, verbose=False)
    finally:
        bs.simulate_trade = original_sim
        bs.MAX_TRADE_BARS = original_max

    return captured


def simulate_with_max_bars(signal, df_future, max_bars_override):
    """
    Simulator original, tapi pakai max_bars_override instead of MAX_TRADE_BARS.
    Logic sama persis dengan backtest_scalp.simulate_trade.
    """
    direction = signal['direction']
    entry = signal['entry']
    sl    = signal['sl']
    tp1   = signal['tp1']
    tp2   = signal['tp2']
    tp3   = signal.get('tp3', tp2)
    risk  = abs(entry - sl)
    rr1   = signal.get('rr1', abs(tp1-entry)/risk if risk else 1.0)
    rr2   = signal.get('rr2', abs(tp2-entry)/risk if risk else 2.0)
    rr3   = signal.get('rr3', abs(tp3-entry)/risk if risk else 3.0)

    tp1_hit = False
    tp2_hit = False
    bep_sl = entry
    current_sl = sl
    max_bars = min(max_bars_override, len(df_future))

    for i in range(max_bars):
        h = df_future['high'].iloc[i]
        l = df_future['low'].iloc[i]

        if direction == 'LONG':
            if l <= current_sl:
                if tp2_hit:
                    return _make_result(signal, 'TP2', i+1, rr2)
                elif tp1_hit:
                    return _make_result(signal, 'BEP', i+1, 0.0)
                return _make_result(signal, 'SL', i+1, -1.0)
            if h >= tp3 and tp2_hit:
                return _make_result(signal, 'TP3', i+1, rr3)
            if h >= tp2 and not tp2_hit:
                tp2_hit = True
                current_sl = tp1
            if h >= tp1 and not tp1_hit:
                tp1_hit = True
                current_sl = bep_sl
        else:  # SHORT
            if h >= current_sl:
                if tp2_hit:
                    return _make_result(signal, 'TP2', i+1, rr2)
                elif tp1_hit:
                    return _make_result(signal, 'BEP', i+1, 0.0)
                return _make_result(signal, 'SL', i+1, -1.0)
            if l <= tp3 and tp2_hit:
                return _make_result(signal, 'TP3', i+1, rr3)
            if l <= tp2 and not tp2_hit:
                tp2_hit = True
                current_sl = tp1
            if l <= tp1 and not tp1_hit:
                tp1_hit = True
                current_sl = bep_sl

    if tp2_hit:
        return _make_result(signal, 'TP2', max_bars, rr2)
    elif tp1_hit:
        return _make_result(signal, 'TP1', max_bars, rr1)
    last_close = df_future['close'].iloc[max_bars-1]
    unreal = (last_close - entry) / risk if direction == 'LONG' \
             else (entry - last_close) / risk
    return _make_result(signal, 'EXPIRED', max_bars, round(unreal, 2))


def run_sim(cached, max_bars, label):
    results = [simulate_with_max_bars(s, f, max_bars) for s, f in cached]
    n = len(results)
    wins = sum(1 for r in results if r.pnl_r > 0)
    total = sum(r.pnl_r for r in results)
    outcomes = {}
    for r in results:
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
    return {
        'label': label, 'max_bars': max_bars, 'n': n,
        'wr': wins/n*100 if n else 0,
        'ev': total/n if n else 0,
        'total': total,
        'outcomes': outcomes,
    }


if __name__ == '__main__':
    print("Loading cache...", flush=True)
    with open('backtesting/cache/unified_data.pkl', 'rb') as f:
        data = pickle.load(f)
    coins = [c for c in data.keys() if c != '_meta' and '15m' in data[c]]
    print(f"{len(coins)} coins", flush=True)

    print(f"\nCapturing signals (with df_future={EXTENDED_BARS} bars)...", flush=True)
    cached = collect_signals_extended(coins, data)
    print(f"\nTotal captured: {len(cached)} signals", flush=True)

    # Save cache for future tests
    try:
        with open('backtesting/cache/scalp_signal_cache.pkl', 'wb') as f:
            pickle.dump(cached, f)
        print("Saved cache to backtesting/cache/scalp_signal_cache.pkl", flush=True)
    except Exception as e:
        print(f"Save cache failed: {e}", flush=True)

    print("\nSimulating with 3 MAX_TRADE_BARS values...\n", flush=True)
    results = []
    for bars, label in [(10, 'BAR_10 (baseline)'), (15, 'BAR_15'), (20, 'BAR_20')]:
        s = run_sim(cached, bars, label)
        results.append(s)
        print(f"  {label:20s} WR={s['wr']:5.1f}%  EV={s['ev']:+.2f}R  "
              f"Total={s['total']:+6.1f}R  n={s['n']}", flush=True)
        print(f"    Outcomes: {s['outcomes']}", flush=True)

    print("\n" + "="*60, flush=True)
    print(" COMPARISON SUMMARY (extend MAX_TRADE_BARS)", flush=True)
    print("="*60, flush=True)
    for s in results:
        pnl_diff = (s['total'] - results[0]['total']) / results[0]['total'] * 100 \
                   if results[0]['total'] else 0
        print(f"{s['label']:20s} WR={s['wr']:5.1f}%  EV={s['ev']:+.2f}R  "
              f"Total={s['total']:+6.1f}R  ({pnl_diff:+.1f}% vs baseline)", flush=True)
