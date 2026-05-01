"""
test_combined.py — Test F9 (11-coin block) compounded with each variant.

Loads all variant pickles, applies F9 filter, computes combined metrics.
"""
import pickle
import glob
import sys
from collections import defaultdict

from backtest_scalp import TradeResult  # noqa: F401


BLOCK_5M = {'AVAX', 'SUI', 'SEI', 'XRP', 'SOL', 'LINK',
            'LTC', 'BCH', 'BLUR', 'LDO', 'TRX'}


def load_all_variants():
    """Load latest pickle dari semua variant."""
    pattern = 'backtesting/results/scalp_backtest_rr_*_*.pkl'
    files = sorted(glob.glob(pattern))

    by_variant = defaultdict(list)
    for f in files:
        # Extract variant: scalp_backtest_rr_<variant>_<ts>.pkl
        parts = f.replace('\\', '/').split('/')[-1].replace('.pkl', '').split('_')
        if len(parts) < 5 or parts[3] == 'all':
            continue
        variant = parts[3]
        by_variant[variant].append(f)

    # Take latest with sample > 100 (skip smoke tests)
    out = {}
    for v, file_list in by_variant.items():
        for f in reversed(sorted(file_list)):
            with open(f, 'rb') as fh:
                data = pickle.load(fh)
            if len(data['trades']) >= 100:
                out[v] = data['trades']
                print(f"Loaded {v:10s}: {f} ({len(data['trades'])} trades)")
                break

    return out


def metrics(trades):
    if not trades:
        return None
    n = len(trades)
    pnls = [t.pnl_r for t in trades]
    wins = [t for t in trades if t.pnl_r > 0]
    return {
        'n': n,
        'wr': len(wins) / n * 100 if n else 0,
        'ev': sum(pnls) / n if n else 0,
        'total_r': sum(pnls),
        'monthly_usd': sum(pnls) * (30 / 14),
    }


def main():
    variants = load_all_variants()

    print(f"\n{'='*80}")
    print(" RAW vs F9-FILTERED PER VARIANT")
    print(f"{'='*80}")
    print(f"\n{'Variant':12s} {'Mode':12s} {'N':>5s} {'WR':>6s} "
          f"{'EV':>9s} {'Total R':>9s} {'$/mo':>7s}")
    print('-' * 80)

    results = []
    for v in sorted(variants.keys()):
        trades = variants[v]
        # Raw
        raw_m = metrics(trades)
        results.append((v, 'raw', raw_m))
        print(f"{v:12s} {'raw':12s} {raw_m['n']:>5d} {raw_m['wr']:>5.1f}% "
              f"{raw_m['ev']:>+8.3f}R {raw_m['total_r']:>+8.1f}R "
              f"${raw_m['monthly_usd']:>+6.0f}")

        # F9 filtered
        filtered = [t for t in trades if t.symbol not in BLOCK_5M]
        f9_m = metrics(filtered)
        results.append((v, 'F9', f9_m))
        print(f"{v:12s} {'+F9 filter':12s} {f9_m['n']:>5d} {f9_m['wr']:>5.1f}% "
              f"{f9_m['ev']:>+8.3f}R {f9_m['total_r']:>+8.1f}R "
              f"${f9_m['monthly_usd']:>+6.0f}")
        print()

    # Find best
    print("=" * 80)
    best = max(results, key=lambda r: r[2]['total_r'])
    print(f"\n  🏆 BEST: {best[0]} ({best[1]}) — "
          f"${best[2]['monthly_usd']:+.0f}/mo, "
          f"WR {best[2]['wr']:.1f}%, EV {best[2]['ev']:+.3f}R")


if __name__ == '__main__':
    main()
