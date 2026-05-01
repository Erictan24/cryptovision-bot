"""
Test 3 opsi trailing stop untuk scalp — OPTIMIZED.
Generate signal sekali (cache), simulate 4 kali dengan logic berbeda.
"""
import pickle
import logging
logging.disable(logging.CRITICAL)

import pandas as pd
import backtest_scalp as bs
from backtest_scalp import TradeResult, _make_result, MAX_TRADE_BARS


# ── Collect signals + future windows ──────────────────────────
def collect_signals(coins, data):
    """Run scalp signal engine, cache (signal, df_future) per trade."""
    cached = []  # list of (signal, df_future)

    # Hack: patch simulate_trade untuk capture signal + df_future
    captured = []
    def capture(signal, df_future):
        captured.append((signal.copy(), df_future.copy()))
        # Return dummy — kita buang hasilnya
        return _make_result(signal, 'TP2', 1, 2.0)

    original = bs.simulate_trade
    bs.simulate_trade = capture
    try:
        for i, coin in enumerate(coins):
            if coin not in data or '15m' not in data.get(coin, {}):
                continue
            print(f"  [{i+1}/{len(coins)}] {coin}...", flush=True)
            df_15m = data[coin]['15m']
            df_1h  = data[coin].get('1h')
            bs.run_backtest_coin(coin, df_15m, df_1h, verbose=False)
    finally:
        bs.simulate_trade = original

    return captured


# ── Simulators ──────────────────────────────────────────────

def sim_A(signal, df_future):
    """No trail — SL fix sampai TP2."""
    direction = signal['direction']
    entry, sl, tp1, tp2 = signal['entry'], signal['sl'], signal['tp1'], signal['tp2']
    tp3 = signal.get('tp3', tp2)
    risk = abs(entry - sl)
    rr1 = abs(tp1 - entry) / risk if risk else 1.0
    rr2 = abs(tp2 - entry) / risk if risk else 2.0
    rr3 = abs(tp3 - entry) / risk if risk else 3.0

    tp1_hit = False
    max_bars = min(MAX_TRADE_BARS, len(df_future))
    for i in range(max_bars):
        h, l = df_future['high'].iloc[i], df_future['low'].iloc[i]
        if direction == 'LONG':
            if l <= sl:
                return _make_result(signal, 'SL', i+1, -1.0)
            if h >= tp2:
                return _make_result(signal, 'TP2', i+1, rr2)
            if h >= tp1:
                tp1_hit = True
        else:
            if h >= sl:
                return _make_result(signal, 'SL', i+1, -1.0)
            if l <= tp2:
                return _make_result(signal, 'TP2', i+1, rr2)
            if l <= tp1:
                tp1_hit = True

    if tp1_hit:
        return _make_result(signal, 'TP1', max_bars, rr1)
    last_close = df_future['close'].iloc[max_bars-1]
    unreal = (last_close - entry) / risk if direction == 'LONG' else (entry - last_close) / risk
    return _make_result(signal, 'EXPIRED', max_bars, round(unreal, 2))


def sim_B(signal, df_future):
    """Trail ke entry ± 0.3R (buffer BEP)."""
    direction = signal['direction']
    entry, sl, tp1, tp2 = signal['entry'], signal['sl'], signal['tp1'], signal['tp2']
    tp3 = signal.get('tp3', tp2)
    risk = abs(entry - sl)
    rr1 = abs(tp1 - entry) / risk if risk else 1.0
    rr2 = abs(tp2 - entry) / risk if risk else 2.0

    bep_buffer = entry - 0.3*risk if direction == 'LONG' else entry + 0.3*risk

    tp1_hit = False
    tp2_hit = False
    current_sl = sl
    max_bars = min(MAX_TRADE_BARS, len(df_future))
    for i in range(max_bars):
        h, l = df_future['high'].iloc[i], df_future['low'].iloc[i]
        if direction == 'LONG':
            if l <= current_sl:
                if tp2_hit:
                    return _make_result(signal, 'TP2', i+1, rr2)
                if tp1_hit:
                    return _make_result(signal, 'BEP', i+1, -0.3)
                return _make_result(signal, 'SL', i+1, -1.0)
            if h >= tp2 and not tp2_hit:
                tp2_hit = True
                current_sl = tp1
            if h >= tp1 and not tp1_hit:
                tp1_hit = True
                current_sl = bep_buffer
        else:
            if h >= current_sl:
                if tp2_hit:
                    return _make_result(signal, 'TP2', i+1, rr2)
                if tp1_hit:
                    return _make_result(signal, 'BEP', i+1, -0.3)
                return _make_result(signal, 'SL', i+1, -1.0)
            if l <= tp2 and not tp2_hit:
                tp2_hit = True
                current_sl = tp1
            if l <= tp1 and not tp1_hit:
                tp1_hit = True
                current_sl = bep_buffer

    if tp2_hit:
        return _make_result(signal, 'TP2', max_bars, rr2)
    if tp1_hit:
        return _make_result(signal, 'TP1', max_bars, rr1)
    last_close = df_future['close'].iloc[max_bars-1]
    unreal = (last_close - entry) / risk if direction == 'LONG' else (entry - last_close) / risk
    return _make_result(signal, 'EXPIRED', max_bars, round(unreal, 2))


def sim_C(signal, df_future):
    """Trail ke BEP setelah 2 candle close above TP1."""
    direction = signal['direction']
    entry, sl, tp1, tp2 = signal['entry'], signal['sl'], signal['tp1'], signal['tp2']
    tp3 = signal.get('tp3', tp2)
    risk = abs(entry - sl)
    rr1 = abs(tp1 - entry) / risk if risk else 1.0
    rr2 = abs(tp2 - entry) / risk if risk else 2.0

    tp1_hit = False
    tp2_hit = False
    bars_above = 0
    current_sl = sl
    max_bars = min(MAX_TRADE_BARS, len(df_future))
    for i in range(max_bars):
        h, l, c = df_future['high'].iloc[i], df_future['low'].iloc[i], df_future['close'].iloc[i]
        if direction == 'LONG':
            if l <= current_sl:
                if tp2_hit:
                    return _make_result(signal, 'TP2', i+1, rr2)
                if tp1_hit and bars_above >= 2:
                    return _make_result(signal, 'BEP', i+1, 0.0)
                return _make_result(signal, 'SL', i+1, -1.0)
            if h >= tp2 and not tp2_hit:
                tp2_hit = True
                current_sl = tp1
            if h >= tp1 and not tp1_hit:
                tp1_hit = True
            if tp1_hit and c > tp1:
                bars_above += 1
                if bars_above >= 2:
                    current_sl = entry
        else:
            if h >= current_sl:
                if tp2_hit:
                    return _make_result(signal, 'TP2', i+1, rr2)
                if tp1_hit and bars_above >= 2:
                    return _make_result(signal, 'BEP', i+1, 0.0)
                return _make_result(signal, 'SL', i+1, -1.0)
            if l <= tp2 and not tp2_hit:
                tp2_hit = True
                current_sl = tp1
            if l <= tp1 and not tp1_hit:
                tp1_hit = True
            if tp1_hit and c < tp1:
                bars_above += 1
                if bars_above >= 2:
                    current_sl = entry

    if tp2_hit:
        return _make_result(signal, 'TP2', max_bars, rr2)
    if tp1_hit:
        return _make_result(signal, 'TP1', max_bars, rr1)
    last_close = df_future['close'].iloc[max_bars-1]
    unreal = (last_close - entry) / risk if direction == 'LONG' else (entry - last_close) / risk
    return _make_result(signal, 'EXPIRED', max_bars, round(unreal, 2))


def simulate_all(cached, sim_fn, label):
    results = [sim_fn(s, f) for s, f in cached]
    n = len(results)
    if n == 0:
        return {'label': label, 'n': 0}
    wins = sum(1 for r in results if r.pnl_r > 0)
    total = sum(r.pnl_r for r in results)
    outcomes = {}
    for r in results:
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
    return {
        'label': label, 'n': n,
        'wr': wins/n*100, 'ev': total/n, 'total': total,
        'outcomes': outcomes,
    }


if __name__ == '__main__':
    print("Loading cache...", flush=True)
    with open('backtesting/cache/unified_data.pkl', 'rb') as f:
        data = pickle.load(f)
    coins = [c for c in data.keys() if c != '_meta' and '15m' in data[c]]
    print(f"{len(coins)} coins", flush=True)

    print("\nCollecting signals (single pass)...", flush=True)
    cached = collect_signals(coins, data)
    print(f"\nTotal signals captured: {len(cached)}", flush=True)

    print("\nSimulating 4 options on cached signals...", flush=True)
    results = []
    for label, fn in [
        ('ORIGINAL  ', bs.simulate_trade),
        ('A (NoTrail)', sim_A),
        ('B (BufBEP) ', sim_B),
        ('C (2bar Cfm)', sim_C),
    ]:
        s = simulate_all(cached, fn, label)
        results.append(s)
        print(f"  {label} → n={s['n']}, WR={s['wr']:.1f}%, EV={s['ev']:+.2f}R, "
              f"Total={s['total']:+.1f}R, {s['outcomes']}", flush=True)

    print("\n" + "="*60, flush=True)
    print(" COMPARISON SUMMARY", flush=True)
    print("="*60, flush=True)
    for s in results:
        print(f"{s['label']:<15} WR={s['wr']:5.1f}%  EV={s['ev']:+.2f}R  "
              f"Total={s['total']:+6.1f}R  ($/mo@$1={s['total']/3:+.1f})",
              flush=True)
