"""
test_expired_filters.py — Apply Path 3 filter rules ke existing baseline trades.

Tidak perlu re-run backtest — load saved trades, apply filter, recompute EV.
Test multiple filter combinations untuk identify yang paling efficient.
"""
import pickle
import sys
import glob
from collections import defaultdict

from backtest_scalp import TradeResult  # noqa: F401


def load_baseline_trades():
    files = sorted(glob.glob(
        'backtesting/results/scalp_backtest_rr_baseline_*.pkl'))
    if not files:
        print("ERROR: no baseline pickle")
        sys.exit(1)
    # Take latest dengan sample > 100 (skip smoke tests)
    for f in reversed(files):
        with open(f, 'rb') as fh:
            data = pickle.load(fh)
        if len(data['trades']) >= 100:
            print(f"Loaded: {f} ({len(data['trades'])} trades)")
            return data['trades']
    print("ERROR: no large baseline pickle found")
    sys.exit(1)


def apply_filter(trades, filter_fn):
    """Filter trades dengan filter_fn(trade) → True/False (keep)."""
    kept = [t for t in trades if filter_fn(t)]
    skipped = [t for t in trades if not filter_fn(t)]
    return kept, skipped


def compute_metrics(trades):
    if not trades:
        return None
    n = len(trades)
    pnls = [t.pnl_r for t in trades]
    wins = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r < 0]
    return {
        'n': n,
        'wr': len(wins) / n * 100,
        'ev': sum(pnls) / n,
        'total_r': sum(pnls),
        'win_n': len(wins),
        'loss_n': len(losses),
    }


def report(label, kept, skipped, baseline_trades):
    """Print filter result vs baseline."""
    base = compute_metrics(baseline_trades)
    cur = compute_metrics(kept)
    skp = compute_metrics(skipped)
    if not cur or not skp:
        return
    delta_n = cur['n'] - base['n']
    delta_total = cur['total_r'] - base['total_r']
    delta_ev = cur['ev'] - base['ev']
    delta_wr = cur['wr'] - base['wr']
    delta_pct = delta_total / base['total_r'] * 100 if base['total_r'] else 0

    # Estimasi monthly $1 risk × 30/14 hari extrapolation
    days = 14
    monthly = cur['total_r'] * (30 / days)

    print(f"\n  {label}")
    print(f"  {'-' * 60}")
    print(f"  Filter keeps  : {cur['n']}/{base['n']} ({delta_n:+d})")
    print(f"  Filter skips  : {skp['n']} trades, "
          f"avg {skp['ev']:+.3f}R "
          f"(saved {-skp['total_r']:+.1f}R from being lost)")
    print(f"  WR            : {cur['wr']:5.1f}% (Δ {delta_wr:+.1f}pp)")
    print(f"  EV            : {cur['ev']:+.3f}R (Δ {delta_ev:+.3f}R)")
    print(f"  Total R       : {cur['total_r']:+.1f}R (Δ {delta_total:+.1f}R, "
          f"{delta_pct:+.1f}%)")
    print(f"  Monthly est   : ${monthly:+.0f} ($1 risk)")


def main():
    trades = load_baseline_trades()
    base = compute_metrics(trades)
    print(f"\n{'='*70}")
    print(f" BASELINE — {base['n']} trades")
    print(f" WR {base['wr']:.1f}%, EV {base['ev']:+.3f}R, "
          f"Total {base['total_r']:+.1f}R")
    print(f"{'='*70}")

    # ───── F1: Block TRX only ─────
    kept, skipped = apply_filter(
        trades, lambda t: t.symbol != 'TRX')
    report("F1 — Block TRX only", kept, skipped, trades)

    # ───── F2: Skip ASIA + DEAD sessions ─────
    kept, skipped = apply_filter(
        trades, lambda t: getattr(t, 'session', '')
        not in ('ASIA', 'DEAD'))
    report("F2 — Skip ASIA + DEAD sessions", kept, skipped, trades)

    # ───── F3: Skip ASIA + DEAD + EVENING ─────
    kept, skipped = apply_filter(
        trades, lambda t: getattr(t, 'session', '')
        not in ('ASIA', 'DEAD', 'EVENING'))
    report("F3 — Skip ASIA+DEAD+EVENING (only LONDON/NY/OVERLAP)",
           kept, skipped, trades)

    # ───── F4: Skip score 16-18 ─────
    kept, skipped = apply_filter(
        trades, lambda t: not (16 <= t.score <= 18))
    report("F4 — Skip score 16-18 (paradox zone)", kept, skipped, trades)

    # ───── F5: Skip SHORT di non-OVERLAP/NY ─────
    def f5(t):
        if t.direction == 'SHORT':
            sess = getattr(t, 'session', '')
            return sess in ('OVERLAP', 'NY', 'LONDON')
        return True
    kept, skipped = apply_filter(trades, f5)
    report("F5 — SHORT only di LONDON/NY/OVERLAP", kept, skipped, trades)

    # ───── F6: COMBO — TRX block + ASIA+DEAD skip ─────
    kept, skipped = apply_filter(
        trades, lambda t: t.symbol != 'TRX' and getattr(t, 'session', '')
        not in ('ASIA', 'DEAD'))
    report("F6 — COMBO: TRX block + skip ASIA/DEAD", kept, skipped, trades)

    # ───── F7: COMBO — TRX + ASIA+DEAD + score 16-18 ─────
    kept, skipped = apply_filter(
        trades,
        lambda t: t.symbol != 'TRX'
        and getattr(t, 'session', '') not in ('ASIA', 'DEAD')
        and not (16 <= t.score <= 18))
    report("F7 — COMBO: TRX + ASIA/DEAD + score 16-18", kept, skipped, trades)

    # ───── F8: AGGRESSIVE — TRX + only OVERLAP/NY ─────
    kept, skipped = apply_filter(
        trades,
        lambda t: t.symbol != 'TRX'
        and getattr(t, 'session', '') in ('OVERLAP', 'NY', 'LONDON'))
    report("F8 — AGGRESSIVE: TRX block + only LONDON/NY/OVERLAP",
           kept, skipped, trades)

    # ───── F9: 11-coin block list (Path 1) ─────
    block_5m = {'AVAX', 'SUI', 'SEI', 'XRP', 'SOL', 'LINK',
                'LTC', 'BCH', 'BLUR', 'LDO', 'TRX'}
    kept, skipped = apply_filter(
        trades, lambda t: t.symbol not in block_5m)
    report("F9 — 11-coin block list (Path 1)", kept, skipped, trades)

    # ───── F10: ULTRA — Path 1 block + ASIA/DEAD ─────
    kept, skipped = apply_filter(
        trades,
        lambda t: t.symbol not in block_5m
        and getattr(t, 'session', '') not in ('ASIA', 'DEAD'))
    report("F10 — ULTRA: 11-coin block + skip ASIA/DEAD",
           kept, skipped, trades)

    # ───── F11: F9 + skip score 16-18 ─────
    kept, skipped = apply_filter(
        trades,
        lambda t: t.symbol not in block_5m
        and not (16 <= t.score <= 18))
    report("F11 — F9 + skip score 16-18", kept, skipped, trades)

    # ───── F12: F9 + SHORT only OVERLAP/NY/LONDON ─────
    def f12(t):
        if t.symbol in block_5m:
            return False
        if t.direction == 'SHORT':
            sess = getattr(t, 'session', '')
            return sess in ('OVERLAP', 'NY', 'LONDON')
        return True
    kept, skipped = apply_filter(trades, f12)
    report("F12 — F9 + SHORT-only LON/NY/OVERLAP", kept, skipped, trades)

    # ───── F13: F9 + F4 + F5 (TRIPLE COMBO) ─────
    def f13(t):
        if t.symbol in block_5m:
            return False
        if 16 <= t.score <= 18:
            return False
        if t.direction == 'SHORT':
            sess = getattr(t, 'session', '')
            if sess not in ('OVERLAP', 'NY', 'LONDON'):
                return False
        return True
    kept, skipped = apply_filter(trades, f13)
    report("F13 — F9 + score 16-18 + SHORT session filter",
           kept, skipped, trades)

    # ───── F11: SCORE-only (just remove paradox zone) ─────
    kept, skipped = apply_filter(
        trades, lambda t: not (16 <= t.score <= 18))
    # Already F4 — skip duplicate

    # Summary table
    print(f"\n{'='*70}")
    print(" FILTER SUMMARY")
    print(f"{'='*70}")
    print(f"\n  {'Filter':50s} {'N':>5s} {'WR':>6s} {'EV':>9s} {'Total':>8s} "
          f"{'$/mo':>7s}")
    print(f"  {'-'*50} {'-'*5} {'-'*6} {'-'*9} {'-'*8} {'-'*7}")

    filters_summary = [
        ('Baseline (no filter)', lambda t: True),
        ('F1 — Block TRX', lambda t: t.symbol != 'TRX'),
        ('F2 — Skip ASIA+DEAD',
         lambda t: getattr(t, 'session', '') not in ('ASIA', 'DEAD')),
        ('F3 — Skip ASIA+DEAD+EVENING',
         lambda t: getattr(t, 'session', '')
         not in ('ASIA', 'DEAD', 'EVENING')),
        ('F4 — Skip score 16-18',
         lambda t: not (16 <= t.score <= 18)),
        ('F5 — SHORT only LON/NY/OVERLAP', f5),
        ('F6 — TRX + ASIA/DEAD',
         lambda t: t.symbol != 'TRX'
         and getattr(t, 'session', '') not in ('ASIA', 'DEAD')),
        ('F7 — TRX + ASIA/DEAD + score 16-18',
         lambda t: t.symbol != 'TRX'
         and getattr(t, 'session', '') not in ('ASIA', 'DEAD')
         and not (16 <= t.score <= 18)),
        ('F8 — TRX + only LON/NY/OVERLAP',
         lambda t: t.symbol != 'TRX'
         and getattr(t, 'session', '') in ('OVERLAP', 'NY', 'LONDON')),
        ('F9 — 11-coin block (Path 1)',
         lambda t: t.symbol not in block_5m),
        ('F10 — 11-coin block + ASIA/DEAD',
         lambda t: t.symbol not in block_5m
         and getattr(t, 'session', '') not in ('ASIA', 'DEAD')),
        ('F11 — F9 + skip score 16-18',
         lambda t: t.symbol not in block_5m
         and not (16 <= t.score <= 18)),
        ('F12 — F9 + SHORT-only LON/NY/OVERLAP', f12),
        ('F13 — F9 + score 16-18 + SHORT filter', f13),
    ]
    for label, fn in filters_summary:
        k, _ = apply_filter(trades, fn)
        if k:
            m = compute_metrics(k)
            monthly = m['total_r'] * (30 / 14)
            print(f"  {label:50s} {m['n']:5d} {m['wr']:5.1f}% "
                  f"{m['ev']:+8.3f}R {m['total_r']:+7.1f}R "
                  f"${monthly:+6.0f}")


if __name__ == '__main__':
    main()
