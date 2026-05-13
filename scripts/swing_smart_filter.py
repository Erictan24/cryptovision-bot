"""SWING smart filter: dari top 200, pilih coin dengan WR>=60% + profit positif + min trades."""
import pickle
from collections import defaultdict

RESULT = 'backtesting/results/unified_top200_20260513_2036.pkl'

with open(RESULT, 'rb') as f:
    payload = pickle.load(f)

trades = payload['trades']
coins_ranked = payload['coins']
swing = [t for t in trades if t.get('engine', '').upper() == 'SWING']

per_coin = defaultdict(list)
for t in swing:
    sym = t.get('symbol', '').upper()
    per_coin[sym].append(t)


def stats(trades):
    n = len(trades)
    if n == 0:
        return None
    wins = sum(1 for t in trades if t.get('pnl_r', 0) > 0)
    total_r = sum(t.get('pnl_r', 0) for t in trades)
    longs = [t for t in trades if t.get('direction') == 'LONG']
    shorts = [t for t in trades if t.get('direction') == 'SHORT']
    long_wr = (sum(1 for t in longs if t.get('pnl_r', 0) > 0) / len(longs) * 100) if longs else None
    short_wr = (sum(1 for t in shorts if t.get('pnl_r', 0) > 0) / len(shorts) * 100) if shorts else None
    return {
        'n': n, 'wr': wins / n * 100, 'ev': total_r / n, 'total_r': total_r,
        'long_wr': long_wr, 'short_wr': short_wr,
        'n_long': len(longs), 'n_short': len(shorts),
    }


print("=" * 75)
print(" SWING — Per-coin breakdown (sorted by Total R desc)")
print("=" * 75)
print(f"{'Coin':<10} {'N':>4} {'WR%':>6} {'EV':>7} {'TotalR':>8} {'LongWR':>7} {'ShortWR':>8}")
print("-" * 75)

rows = []
for coin, t_list in per_coin.items():
    s = stats(t_list)
    rows.append((coin, s))

rows.sort(key=lambda x: x[1]['total_r'], reverse=True)
for coin, s in rows[:30]:  # top 30 by PnL
    lw = f"{s['long_wr']:.0f}%" if s['long_wr'] is not None else "-"
    sw = f"{s['short_wr']:.0f}%" if s['short_wr'] is not None else "-"
    print(f"{coin:<10} {s['n']:>4} {s['wr']:>5.1f}% {s['ev']:>+6.2f}R {s['total_r']:>+7.1f}R {lw:>7} {sw:>8}")

print()
print("=" * 75)
print(" FILTER SCENARIOS — pilih threshold yang paling masuk akal")
print("=" * 75)


def apply_filter(min_wr, min_trades, min_total_r):
    keep_coins = [c for c, s in rows if s['wr'] >= min_wr and s['n'] >= min_trades and s['total_r'] >= min_total_r]
    kept_trades = [t for t in swing if t.get('symbol', '').upper() in set(keep_coins)]
    rejected = [t for t in swing if t.get('symbol', '').upper() not in set(keep_coins)]
    s_kept = stats(kept_trades)
    s_rej = stats(rejected) if rejected else None
    return keep_coins, s_kept, s_rej


scenarios = [
    ('A. WR>=60%, N>=5, PnL>0', 60, 5, 0.01),
    ('B. WR>=65%, N>=5, PnL>0', 65, 5, 0.01),
    ('C. WR>=60%, N>=8, PnL>=+3R', 60, 8, 3.0),
    ('D. WR>=60%, N>=10, PnL>=+5R', 60, 10, 5.0),
    ('E. WR>=55%, N>=5, PnL>0', 55, 5, 0.01),
]

print(f"\n{'Scenario':<32} {'Coins':>6} {'Trades':>7} {'WR%':>6} {'EV':>7} {'TotalR':>8}")
print("-" * 75)
print(f"{'BASELINE (Top 200, all)':<32} {97:>6} {692:>7} {64.9:>5.1f}% {0.75:>+6.2f}R {516.4:>+7.1f}R")

for name, mwr, mn, mr in scenarios:
    coins, s_kept, s_rej = apply_filter(mwr, mn, mr)
    if s_kept:
        print(f"{name:<32} {len(coins):>6} {s_kept['n']:>7} {s_kept['wr']:>5.1f}% {s_kept['ev']:>+6.2f}R {s_kept['total_r']:>+7.1f}R")

print()
print("=" * 75)
print(" REJECTED COINS (Scenario A): coin yang DI-BLOCK by filter")
print("=" * 75)
coins_a, s_a, s_rej_a = apply_filter(60, 5, 0.01)
if s_rej_a:
    print(f"Rejected: {s_rej_a['n']} trades, WR {s_rej_a['wr']:.1f}%, EV {s_rej_a['ev']:+.2f}R, Total {s_rej_a['total_r']:+.1f}R")
    rej_coins = set(c for c, _ in rows) - set(coins_a)
    rej_rows = [(c, s) for c, s in rows if c in rej_coins]
    rej_rows.sort(key=lambda x: x[1]['total_r'])
    print("\nWorst rejected coins (10 paling rugi/loser):")
    for c, s in rej_rows[:10]:
        print(f"  {c:<10} {s['n']:>3} trades, WR {s['wr']:>5.1f}%, Total {s['total_r']:>+6.1f}R")

print()
print("=" * 75)
print(" RECOMMENDATION SUMMARY (Scenario A applied)")
print("=" * 75)
print(f"Whitelist {len(coins_a)} coin: {', '.join(sorted(coins_a))}")
