"""
analyze_expired.py — Analisis pattern di EXPIRED outcomes (38% leak).

Load saved baseline backtest pickle, segment EXPIRED trades, identify
correlations: coin, session, ADX, score, direction, etc.
"""
import pickle
import sys
from collections import Counter, defaultdict

# Import TradeResult so pickle can deserialize
from backtest_scalp import TradeResult  # noqa: F401


def load_trades(pkl_path: str) -> list:
    with open(pkl_path, 'rb') as f:
        data = pickle.load(f)
    return data['trades']


def analyze(trades: list):
    print(f"\n{'='*70}")
    print(f" ANALISIS EXPIRED (sample: {len(trades)} trades)")
    print(f"{'='*70}")

    # Segment by outcome
    by_outcome = defaultdict(list)
    for t in trades:
        by_outcome[t.outcome].append(t)

    expired = by_outcome.get('EXPIRED', [])
    sl = by_outcome.get('SL', [])
    bep = by_outcome.get('BEP', [])
    tp1 = by_outcome.get('TP1', [])
    tp2 = by_outcome.get('TP2', [])
    tp3 = by_outcome.get('TP3', []) + by_outcome.get('TP3_TRAIL', [])

    print(f"\n  Outcome distribusi:")
    for outcome, n in sorted(Counter(t.outcome for t in trades).items(),
                             key=lambda x: -x[1]):
        pct = n / len(trades) * 100
        print(f"    {outcome:12s}: {n:4d} ({pct:.1f}%)")

    if not expired:
        print("  Tidak ada EXPIRED trade.")
        return

    # EXPIRED: rata-rata pnl_r dan distribusi
    expired_pnls = [t.pnl_r for t in expired]
    print(f"\n  EXPIRED stats:")
    print(f"    n            : {len(expired)}")
    print(f"    avg pnl      : {sum(expired_pnls)/len(expired_pnls):+.3f}R")
    print(f"    win (>0)     : {sum(1 for p in expired_pnls if p > 0)}")
    print(f"    loss (<0)    : {sum(1 for p in expired_pnls if p < 0)}")
    print(f"    flat (=0)    : {sum(1 for p in expired_pnls if p == 0)}")
    print(f"    range        : [{min(expired_pnls):+.2f}, "
          f"{max(expired_pnls):+.2f}]")

    # Per-coin EXPIRED rate
    print(f"\n  --- EXPIRED RATE per coin (sample ≥ 8 trades) ---")
    coin_total = defaultdict(int)
    coin_expired = defaultdict(int)
    for t in trades:
        coin_total[t.symbol] += 1
        if t.outcome == 'EXPIRED':
            coin_expired[t.symbol] += 1

    rows = []
    for coin, total in coin_total.items():
        if total >= 8:
            exp = coin_expired[coin]
            rate = exp / total * 100
            rows.append((coin, total, exp, rate))
    rows.sort(key=lambda r: -r[3])
    print(f"  {'Coin':8s} {'Total':>6s} {'Exp':>5s} {'Rate':>7s}")
    for coin, total, exp, rate in rows[:15]:
        bar = '#' * int(rate / 5)
        print(f"  {coin:8s} {total:>6d} {exp:>5d} {rate:>6.1f}% {bar}")

    # Per-session
    print(f"\n  --- EXPIRED RATE per session ---")
    session_total = defaultdict(int)
    session_expired = defaultdict(int)
    for t in trades:
        sess = getattr(t, 'session', '') or 'UNKNOWN'
        session_total[sess] += 1
        if t.outcome == 'EXPIRED':
            session_expired[sess] += 1

    print(f"  {'Session':10s} {'Total':>6s} {'Exp':>5s} {'Rate':>7s}")
    for sess, total in sorted(session_total.items(),
                              key=lambda x: -session_expired[x[0]]):
        if total < 5:
            continue
        exp = session_expired[sess]
        rate = exp / total * 100
        bar = '#' * int(rate / 5)
        print(f"  {sess:10s} {total:>6d} {exp:>5d} {rate:>6.1f}% {bar}")

    # Per-direction
    print(f"\n  --- EXPIRED RATE per direction ---")
    for direction in ['LONG', 'SHORT']:
        d_trades = [t for t in trades if t.direction == direction]
        d_exp = [t for t in d_trades if t.outcome == 'EXPIRED']
        if d_trades:
            rate = len(d_exp) / len(d_trades) * 100
            print(f"  {direction:8s}: {len(d_trades)} trades, "
                  f"EXPIRED {len(d_exp)} ({rate:.1f}%)")

    # Per-quality
    print(f"\n  --- EXPIRED RATE per quality ---")
    for qual in ['GOOD', 'MODERATE', 'WAIT']:
        q_trades = [t for t in trades if t.quality == qual]
        q_exp = [t for t in q_trades if t.outcome == 'EXPIRED']
        if q_trades:
            rate = len(q_exp) / len(q_trades) * 100
            print(f"  {qual:10s}: {len(q_trades)} trades, "
                  f"EXPIRED {len(q_exp)} ({rate:.1f}%)")

    # Score range
    print(f"\n  --- EXPIRED RATE per score range ---")
    score_buckets = [(0, 9, '0-9'), (10, 12, '10-12'), (13, 15, '13-15'),
                     (16, 18, '16-18'), (19, 100, '19+')]
    for lo, hi, label in score_buckets:
        b_trades = [t for t in trades if lo <= t.score <= hi]
        b_exp = [t for t in b_trades if t.outcome == 'EXPIRED']
        if b_trades:
            rate = len(b_exp) / len(b_trades) * 100
            avg_pnl = sum(t.pnl_r for t in b_trades) / len(b_trades)
            print(f"  Score {label:6s}: {len(b_trades):4d} trades, "
                  f"EXPIRED {len(b_exp):3d} ({rate:5.1f}%), "
                  f"avg pnl {avg_pnl:+.3f}R")

    # Pullback quality
    print(f"\n  --- EXPIRED RATE per pullback quality ---")
    pb_total = defaultdict(int)
    pb_exp = defaultdict(int)
    for t in trades:
        pb = getattr(t, 'pullback_quality', '') or 'UNKNOWN'
        pb_total[pb] += 1
        if t.outcome == 'EXPIRED':
            pb_exp[pb] += 1
    for pb, total in sorted(pb_total.items(), key=lambda x: -x[1]):
        if total < 5:
            continue
        exp = pb_exp[pb]
        rate = exp / total * 100
        print(f"  {pb:12s}: {total:4d} trades, EXPIRED {exp:3d} "
              f"({rate:5.1f}%)")

    # Trend state
    print(f"\n  --- EXPIRED RATE per trend state ---")
    ts_total = defaultdict(int)
    ts_exp = defaultdict(int)
    for t in trades:
        ts = getattr(t, 'trend_state', '') or 'UNKNOWN'
        ts_total[ts] += 1
        if t.outcome == 'EXPIRED':
            ts_exp[ts] += 1
    for ts, total in sorted(ts_total.items(), key=lambda x: -x[1]):
        if total < 5:
            continue
        exp = ts_exp[ts]
        rate = exp / total * 100
        print(f"  {ts:12s}: {total:4d} trades, EXPIRED {exp:3d} "
              f"({rate:5.1f}%)")

    # WR antara EXPIRED vs non-EXPIRED
    print(f"\n  --- IMPACT EXPIRED ke EV ---")
    expired_total_r = sum(t.pnl_r for t in expired)
    non_expired = [t for t in trades if t.outcome != 'EXPIRED']
    non_expired_total = sum(t.pnl_r for t in non_expired)
    print(f"  Expired trades : {len(expired)} trades, total {expired_total_r:+.1f}R "
          f"(avg {expired_total_r/len(expired):+.3f}R)")
    print(f"  Non-expired    : {len(non_expired)} trades, total {non_expired_total:+.1f}R "
          f"(avg {non_expired_total/len(non_expired):+.3f}R)")
    print(f"  Total          : {len(trades)} trades, "
          f"total {expired_total_r + non_expired_total:+.1f}R")

    # Counterfactual: kalau EXPIRED di-skip
    if non_expired:
        without_exp_avg = non_expired_total / len(non_expired)
        wr_without_exp = sum(1 for t in non_expired if t.pnl_r > 0) / len(non_expired) * 100
        print(f"\n  KALAU SEMUA EXPIRED DI-SKIP (counterfactual):")
        print(f"    n trades   : {len(non_expired)} (turun {len(expired)})")
        print(f"    WR         : {wr_without_exp:.1f}%")
        print(f"    EV         : {without_exp_avg:+.3f}R")
        print(f"    Total R    : {non_expired_total:+.1f}R "
              f"(vs current {expired_total_r + non_expired_total:+.1f}R)")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        # Default to latest baseline
        import glob
        files = sorted(glob.glob('backtesting/results/scalp_backtest_rr_baseline_*.pkl'))
        if not files:
            print("ERROR: no baseline pickle found")
            sys.exit(1)
        path = files[-1]
        print(f"Loading: {path}")
    else:
        path = sys.argv[1]

    trades = load_trades(path)
    analyze(trades)
