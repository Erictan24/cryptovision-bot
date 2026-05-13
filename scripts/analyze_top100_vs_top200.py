"""Analyze SWING top 100 vs top 200 sweet spot from unified backtest result."""
import pickle
from collections import defaultdict

RESULT = 'backtesting/results/unified_top200_20260513_2036.pkl'

with open(RESULT, 'rb') as f:
    payload = pickle.load(f)

trades = payload['trades']
coins_ranked = payload['coins']

print(f"Total trades: {len(trades)}")
print(f"Total coins in cache: {len(coins_ranked)}")
print(f"Top 5 by volume: {coins_ranked[:5]}")
print(f"Rank 96-105: {coins_ranked[95:105]}")
print()

top100 = set(coins_ranked[:100])
top200 = set(coins_ranked)


def stats(label, trades):
    if not trades:
        print(f"{label}: 0 trades")
        return
    n = len(trades)
    wins = sum(1 for t in trades if t.get('pnl_r', 0) > 0)
    total_r = sum(t.get('pnl_r', 0) for t in trades)
    longs = [t for t in trades if t.get('direction') == 'LONG']
    shorts = [t for t in trades if t.get('direction') == 'SHORT']
    long_wr = (sum(1 for t in longs if t.get('pnl_r', 0) > 0) / len(longs) * 100) if longs else 0
    short_wr = (sum(1 for t in shorts if t.get('pnl_r', 0) > 0) / len(shorts) * 100) if shorts else 0
    tp2 = sum(1 for t in trades if t.get('outcome', '').upper() in ('TP2', 'TP2_HIT'))
    sl = sum(1 for t in trades if t.get('outcome', '').upper() in ('SL', 'SL_HIT'))
    coins_used = len(set(t.get('symbol', '') for t in trades))
    print(f"{label}:")
    print(f"  Trades:   {n}")
    print(f"  Coins:    {coins_used}")
    print(f"  WR:       {wins/n*100:.1f}%")
    print(f"  EV/trade: {total_r/n:+.3f}R")
    print(f"  Total R:  {total_r:+.1f}R")
    print(f"  LONG WR:  {long_wr:.1f}% ({len(longs)} trades)")
    print(f"  SHORT WR: {short_wr:.1f}% ({len(shorts)} trades)")
    print(f"  TP2:      {tp2} ({tp2/n*100:.1f}%)")
    print(f"  SL:       {sl} ({sl/n*100:.1f}%)")
    print()


swing = [t for t in trades if t.get('engine', '').upper() == 'SWING']
scalp = [t for t in trades if t.get('engine', '').upper() == 'SCALP']

# Fallback if engine field not set
if not swing:
    swing = [t for t in trades if 'tf' in t and t.get('tf') in ('1h', '4h')]
if not scalp:
    scalp = [t for t in trades if 'tf' in t and t.get('tf') in ('5m', '15m')]

print(f"Sample trade keys: {list(trades[0].keys()) if trades else 'none'}")
print(f"Sample trade: {trades[0] if trades else 'none'}")
print()
print(f"SWING trades: {len(swing)}, SCALP trades: {len(scalp)}")
print()

print("=" * 60)
print(" SWING — TOP 100 vs TOP 200")
print("=" * 60)
swing_top100 = [t for t in swing if t.get('symbol', '').upper() in top100]
swing_top101_200 = [t for t in swing if t.get('symbol', '').upper() in top200 and t.get('symbol', '').upper() not in top100]
swing_all = swing

stats("SWING Top 100 only", swing_top100)
stats("SWING Rank 101-200 only", swing_top101_200)
stats("SWING Top 200 (all)", swing_all)

# Top 50 also for reference
top50 = set(coins_ranked[:50])
swing_top50 = [t for t in swing if t.get('symbol', '').upper() in top50]
stats("SWING Top 50 only", swing_top50)

print("=" * 60)
print(" SCALP — coin breakdown")
print("=" * 60)
scalp_top100 = [t for t in scalp if t.get('symbol', '').upper() in top100]
scalp_top101_200 = [t for t in scalp if t.get('symbol', '').upper() in top200 and t.get('symbol', '').upper() not in top100]
stats("SCALP Top 100", scalp_top100)
stats("SCALP Rank 101-200", scalp_top101_200)
