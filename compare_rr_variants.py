"""
compare_rr_variants.py — Parse + bandingkan output backtest 4 variant RR.

Usage:
  python compare_rr_variants.py \
      backtest_rr_baseline.log backtest_rr_A.log \
      backtest_rr_B.log backtest_rr_C.log
"""
import re
import sys
from pathlib import Path


def parse_log(path: str) -> dict:
    """Extract key metrics dari log file."""
    text = Path(path).read_text(encoding='utf-8', errors='replace')

    out = {'file': path, 'variant': Path(path).stem.replace('backtest_rr_', '')}

    # Total trades
    m = re.search(r'Total Trades\s*:\s*(\d+)', text)
    out['n'] = int(m.group(1)) if m else 0

    # Win rate
    m = re.search(r'Win Rate\s*:\s*([\d.]+)%', text)
    out['wr'] = float(m.group(1)) if m else 0.0

    # Avg PnL/trade (EV)
    m = re.search(r'Avg PnL/trade:\s*([+\-][\d.]+)R', text)
    out['ev'] = float(m.group(1)) if m else 0.0

    # Total PnL
    m = re.search(r'Total PnL\s*:\s*([+\-][\d.]+)R', text)
    out['total_r'] = float(m.group(1)) if m else 0.0

    # Max drawdown
    m = re.search(r'Max Drawdown\s*:\s*([\d.]+)R', text)
    out['max_dd'] = float(m.group(1)) if m else 0.0

    # Monthly $
    m = re.search(r'Monthly est\s*:\s*\$([+\-][\d.]+)/bulan', text)
    out['monthly_usd'] = float(m.group(1)) if m else 0.0

    # Trades/week
    m = re.search(r'Trades/week\s*:\s*([\d.]+)', text)
    out['per_week'] = float(m.group(1)) if m else 0.0

    # Outcome distribution
    out['outcomes'] = {}
    for label in ['TP3', 'TP2', 'TP1', 'BEP', 'SL', 'EXPIRED']:
        m = re.search(rf'{label}\s*:\s*(\d+)', text)
        if m:
            out['outcomes'][label] = int(m.group(1))

    # Per-coin (semua coin yang ada di --- TOP COINS / COIN LEARNING SUMMARY ---)
    out['by_coin'] = {}
    # Cari section "COIN LEARNING SUMMARY" yang lebih lengkap (semua coin, bukan top 15)
    m = re.search(r'COIN LEARNING SUMMARY.*?(?=\n=+\s*\n)',
                  text, re.DOTALL)
    if m:
        for line in m.group(0).split('\n'):
            mc = re.match(r'^([A-Z0-9]+)\s+([\d.]+)%\s+([+\-][\d.]+)R\s+(\d+)',
                          line)
            if mc:
                out['by_coin'][mc.group(1)] = {
                    'wr': float(mc.group(2)),
                    'ev': float(mc.group(3)),
                    'n': int(mc.group(4)),
                }

    # Long/Short
    m = re.search(r'LONG\s*:\s*(\d+)\s+trades,\s+WR\s+([\d.]+)%,\s+avg\s+([+\-][\d.]+)R', text)
    if m:
        out['long_n'] = int(m.group(1))
        out['long_wr'] = float(m.group(2))
        out['long_ev'] = float(m.group(3))
    m = re.search(r'SHORT\s*:\s*(\d+)\s+trades,\s+WR\s+([\d.]+)%,\s+avg\s+([+\-][\d.]+)R', text)
    if m:
        out['short_n'] = int(m.group(1))
        out['short_wr'] = float(m.group(2))
        out['short_ev'] = float(m.group(3))

    return out


def calc_avg_win(stats: dict) -> float:
    """avg_win = (total_pnl + losses) / wins. Asumsi avg_loss = -1R full SL only.
    Approx: pakai outcome distribution."""
    n = stats['n']
    if n == 0:
        return 0.0
    sl_count = stats['outcomes'].get('SL', 0)
    win_count = n - sl_count - stats['outcomes'].get('EXPIRED', 0) - stats['outcomes'].get('BEP', 0)
    if win_count <= 0:
        return 0.0
    # Total PnL = wins * avg_win + losses_full_sl + bep_outcome + expired_outcome
    # Approx ignore expired/bep contributions (small)
    total = stats['total_r']
    # losses_R approx: SL = -1.0 * sl_count (without quality multiplier).
    # Simplification: avg_win ≈ (total_r - (-1.0 * sl_count)) / win_count
    avg_win = (total + 1.0 * sl_count) / win_count
    return avg_win


def print_comparison(variants: list):
    """Print comparison table."""
    print("\n" + "=" * 100)
    print(" RR VARIANT COMPARISON")
    print("=" * 100)

    # Main metrics
    print(f"\n{'Variant':10s} {'N':>6s} {'WR':>7s} {'EV':>9s} "
          f"{'Total R':>9s} {'MaxDD':>8s} {'Monthly $':>10s} {'/wk':>6s}")
    print("-" * 100)
    for v in variants:
        avg_win_calc = calc_avg_win(v)
        print(f"{v['variant']:10s} {v['n']:>6d} {v['wr']:>6.1f}% "
              f"{v['ev']:>+8.2f}R {v['total_r']:>+8.1f}R "
              f"{v['max_dd']:>7.1f}R "
              f"${v['monthly_usd']:>+8.1f} {v['per_week']:>5.1f}")

    # Outcome distribution
    print(f"\n{'Variant':10s} {'TP3':>6s} {'TP2':>6s} {'TP1':>6s} "
          f"{'BEP':>6s} {'SL':>6s} {'EXP':>6s}")
    print("-" * 100)
    for v in variants:
        o = v['outcomes']
        print(f"{v['variant']:10s} "
              f"{o.get('TP3', 0):>6d} {o.get('TP2', 0):>6d} "
              f"{o.get('TP1', 0):>6d} {o.get('BEP', 0):>6d} "
              f"{o.get('SL', 0):>6d} {o.get('EXPIRED', 0):>6d}")

    # Avg win calc
    print(f"\n{'Variant':10s} {'Avg Win':>10s} {'Avg Loss':>10s} {'RR efektif':>12s}")
    print("-" * 100)
    for v in variants:
        aw = calc_avg_win(v)
        al = -1.0  # simplified
        rr = aw / abs(al) if al else 0
        print(f"{v['variant']:10s} {aw:>+9.2f}R {al:>+9.2f}R {rr:>11.2f}:1")

    # Long vs Short per variant
    print(f"\n{'Variant':10s} {'LONG N':>7s} {'LONG WR':>9s} {'LONG EV':>9s} "
          f"{'SHORT N':>8s} {'SHORT WR':>10s} {'SHORT EV':>10s}")
    print("-" * 100)
    for v in variants:
        if 'long_n' in v and 'short_n' in v:
            print(f"{v['variant']:10s} "
                  f"{v['long_n']:>7d} {v['long_wr']:>8.1f}% {v['long_ev']:>+8.2f}R "
                  f"{v['short_n']:>8d} {v['short_wr']:>9.1f}% {v['short_ev']:>+9.2f}R")

    # Per-coin shift (kalau ada baseline + variants)
    baseline = next((v for v in variants if v['variant'] == 'baseline'), None)
    if baseline and baseline['by_coin']:
        print(f"\n--- PER-COIN EV SHIFT vs BASELINE ---")
        # Header
        var_names = [v['variant'] for v in variants if v['variant'] != 'baseline']
        head = f"{'Coin':8s} {'Base WR':>8s} {'Base EV':>9s}"
        for vn in var_names:
            head += f" {f'{vn} ΔEV':>10s}"
        print(head)
        print("-" * 100)
        # All coins from baseline
        all_coins = sorted(baseline['by_coin'].keys(),
                          key=lambda c: baseline['by_coin'][c]['ev'],
                          reverse=True)
        for coin in all_coins:
            b = baseline['by_coin'][coin]
            line = f"{coin:8s} {b['wr']:>7.1f}% {b['ev']:>+8.2f}R"
            for v in variants:
                if v['variant'] == 'baseline':
                    continue
                if coin in v['by_coin']:
                    delta = v['by_coin'][coin]['ev'] - b['ev']
                    sign = '+' if delta >= 0 else ''
                    line += f" {sign}{delta:>9.2f}R"
                else:
                    line += f" {'—':>10s}"
            print(line)

    # Verdict
    print(f"\n--- VERDICT ---")
    best_ev = max(variants, key=lambda v: v['ev'])
    best_total = max(variants, key=lambda v: v['total_r'])
    print(f"  Best EV       : {best_ev['variant']:10s} ({best_ev['ev']:+.2f}R)")
    print(f"  Best Total R  : {best_total['variant']:10s} ({best_total['total_r']:+.1f}R)")
    print(f"  Best Monthly $: {best_total['variant']:10s} (${best_total['monthly_usd']:+.1f})")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python compare_rr_variants.py <log1> <log2> ...")
        sys.exit(1)

    variants = [parse_log(f) for f in sys.argv[1:]]
    print_comparison(variants)
