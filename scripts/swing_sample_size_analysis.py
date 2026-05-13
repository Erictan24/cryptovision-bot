"""Bukti statistik: coin 'losers' kemungkinan besar cuma random variance, bukan edge negatif."""
import pickle
from collections import defaultdict
from math import comb

RESULT = 'backtesting/results/unified_top200_20260513_2036.pkl'

with open(RESULT, 'rb') as f:
    payload = pickle.load(f)

trades = payload['trades']
swing = [t for t in trades if t.get('engine', '').upper() == 'SWING']

per_coin = defaultdict(list)
for t in swing:
    sym = t.get('symbol', '').upper()
    per_coin[sym].append(t)

BASE_WR = 0.649  # overall SWING WR


def coin_stats(t_list):
    n = len(t_list)
    w = sum(1 for t in t_list if t.get('pnl_r', 0) > 0)
    total_r = sum(t.get('pnl_r', 0) for t in t_list)
    return n, w, total_r


def binomial_prob_at_most_w(n, w, p):
    """P(getting <= w wins out of n trades, given true WR p)."""
    total = 0
    for k in range(0, w + 1):
        total += comb(n, k) * (p ** k) * ((1 - p) ** (n - k))
    return total


print("=" * 80)
print(" SELECTION BIAS PROOF — apakah 'losers' beneran bad atau cuma unlucky?")
print("=" * 80)
print(f"Base WR (overall SWING): {BASE_WR*100:.1f}%")
print()

# Group by sample size buckets
buckets = {
    'n=1-3 (noise)': (1, 3),
    'n=4-7 (weak)': (4, 7),
    'n=8-14 (moderate)': (8, 14),
    'n=15+ (reliable)': (15, 1000),
}

print(f"{'Bucket':<22} {'Coins':>6} {'Trades':>7} {'Wins':>5} {'WR%':>6} {'PnL R':>8}")
print("-" * 80)
for label, (lo, hi) in buckets.items():
    coins_in_bucket = [(c, coin_stats(tl)) for c, tl in per_coin.items() if lo <= len(tl) <= hi]
    if not coins_in_bucket:
        continue
    total_n = sum(n for _, (n, _, _) in coins_in_bucket)
    total_w = sum(w for _, (_, w, _) in coins_in_bucket)
    total_r = sum(r for _, (_, _, r) in coins_in_bucket)
    wr = total_w / total_n * 100 if total_n else 0
    print(f"{label:<22} {len(coins_in_bucket):>6} {total_n:>7} {total_w:>5} {wr:>5.1f}% {total_r:>+7.1f}R")

print()
print("=" * 80)
print(" 'LOSER' COINS — apakah RANDOM VARIANCE or SIGNIFICANT EDGE NEGATIF?")
print("=" * 80)
print(f"Untuk setiap loser, hitung: probability dapet WR ini atau lebih jelek")
print(f"kalau coin sebenarnya punya WR base 64.9% (null hypothesis).")
print(f"Kalau probabilitas > 5% -> kemungkinan cuma random variance, bukan edge negatif beneran.")
print()
print(f"{'Coin':<10} {'N':>4} {'W':>3} {'WR%':>6} {'PnL':>7} {'P(<=W | true=64.9%)':>22} {'Verdict':<25}")
print("-" * 80)

# Find losers (WR < 50% or PnL < 0)
losers = []
for c, t_list in per_coin.items():
    n, w, total_r = coin_stats(t_list)
    if n >= 1 and (w / n < 0.5 or total_r < 0):
        p_value = binomial_prob_at_most_w(n, w, BASE_WR)
        losers.append((c, n, w, total_r, p_value))

losers.sort(key=lambda x: (x[4], -x[1]))  # by p-value ascending, prefer higher N
for c, n, w, r, p in losers:
    wr_pct = w / n * 100
    verdict = "RANDOM VARIANCE" if p > 0.05 else ("BORDERLINE" if p > 0.01 else "SIGNIFICANT (real edge -)")
    print(f"{c:<10} {n:>4} {w:>3} {wr_pct:>5.1f}% {r:>+6.1f}R {p*100:>20.1f}%  {verdict}")

print()
print("=" * 80)
print(" KESIMPULAN")
print("=" * 80)
n_loser = len(losers)
random = sum(1 for *_, p in losers if p > 0.05)
borderline = sum(1 for *_, p in losers if 0.01 < p <= 0.05)
significant = sum(1 for *_, p in losers if p <= 0.01)
print(f"Total 'losers' (WR<50% or PnL<0): {n_loser}")
print(f"  - RANDOM VARIANCE (p>5%):  {random} coin — kemungkinan besar JUST UNLUCKY, gak edge negatif")
print(f"  - BORDERLINE (1-5%):        {borderline} coin — perlu data lebih banyak")
print(f"  - SIGNIFICANT (p<=1%):      {significant} coin — bener-bener edge negatif konsisten")
print()
print("=" * 80)
print(" ALTERNATIF FILTER YANG LEBIH ROBUST (block hanya yang STATISTICALLY SIGNIFICANT)")
print("=" * 80)


def apply_smart_filter(min_n, max_wr_to_block):
    """Block coin yang n besar DAN WR jelek (bukti statistik kuat)."""
    block_coins = set()
    for c, t_list in per_coin.items():
        n, w, _ = coin_stats(t_list)
        if n >= min_n and (w / n) < max_wr_to_block:
            block_coins.add(c)
    keep = [t for t in swing if t.get('symbol', '').upper() not in block_coins]
    n = len(keep)
    if n == 0:
        return block_coins, 0, 0, 0, 0
    wins = sum(1 for t in keep if t.get('pnl_r', 0) > 0)
    total_r = sum(t.get('pnl_r', 0) for t in keep)
    return block_coins, n, wins / n * 100, total_r / n, total_r


print(f"{'Filter':<40} {'Block':>6} {'Trades':>7} {'WR%':>6} {'EV':>7} {'TotalR':>8}")
print("-" * 80)
print(f"{'BASELINE (no filter)':<40} {0:>6} {692:>7} {64.9:>5.1f}% {0.75:>+6.2f}R {516.4:>+7.1f}R")

scenarios = [
    ("Block n>=10 AND WR<40%", 10, 0.40),
    ("Block n>=15 AND WR<50%", 15, 0.50),
    ("Block n>=20 AND WR<55%", 20, 0.55),
    ("Block n>=8 AND WR<35%", 8, 0.35),
]
for label, mn, mw in scenarios:
    block, n, wr, ev, tot = apply_smart_filter(mn, mw)
    print(f"{label:<40} {len(block):>6} {n:>7} {wr:>5.1f}% {ev:>+6.2f}R {tot:>+7.1f}R  blocked={list(block) if block else '[]'}")
