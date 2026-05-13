"""Simulasi filter berbasis kills + kombinasi, dari data backtest existing."""
import pickle
from collections import defaultdict, Counter

RESULT = 'backtesting/results/unified_top200_20260513_2036.pkl'

with open(RESULT, 'rb') as f:
    payload = pickle.load(f)

swing = [t for t in payload['trades'] if t.get('engine', '').upper() == 'SWING']

print(f"Total SWING trades: {len(swing)}")
print()


def stats(trades):
    n = len(trades)
    if n == 0:
        return None
    wins = sum(1 for t in trades if t.get('pnl_r', 0) > 0)
    total_r = sum(t.get('pnl_r', 0) for t in trades)
    sl = sum(1 for t in trades if t.get('outcome', '').upper() in ('SL', 'SL_HIT'))
    tp2 = sum(1 for t in trades if t.get('outcome', '').upper() in ('TP2', 'TP2_HIT'))
    longs = [t for t in trades if t.get('direction') == 'LONG']
    shorts = [t for t in trades if t.get('direction') == 'SHORT']
    return {
        'n': n, 'wr': wins / n * 100, 'ev': total_r / n,
        'total_r': total_r, 'sl_rate': sl / n * 100, 'tp2_rate': tp2 / n * 100,
        'n_long': len(longs), 'n_short': len(shorts),
        'long_wr': (sum(1 for t in longs if t.get('pnl_r',0)>0)/len(longs)*100) if longs else 0,
        'short_wr': (sum(1 for t in shorts if t.get('pnl_r',0)>0)/len(shorts)*100) if shorts else 0,
    }


def fmt(s):
    if s is None:
        return f"{'-':>5} {'-':>6} {'-':>7} {'-':>8} {'-':>6}"
    return f"{s['n']:>5} {s['wr']:>5.1f}% {s['ev']:>+6.2f}R {s['total_r']:>+7.1f}R {s['sl_rate']:>5.1f}%"


# ============================================================
# DEEP DIVE: Kills breakdown by kill TYPE
# ============================================================
print('=' * 80)
print(' KILLS DEEP DIVE: Tipe kill mana yang paling sering di SL trades?')
print('=' * 80)
# Pisahkan trades yang punya kills > 0
kill_trades = [t for t in swing if t.get('kills', 0) > 0]
print(f"Trades dengan kills > 0: {len(kill_trades)} (61% dari total)")
print()

# Hitung kill tag in WIN vs SL untuk yang kills > 0
kill_tag_win = Counter()
kill_tag_sl = Counter()
for t in kill_trades:
    is_sl = t.get('outcome', '').upper() in ('SL', 'SL_HIT')
    is_win = t.get('pnl_r', 0) > 0
    for r in t.get('reasons', []):
        clean = ''.join(ch for ch in r if ord(ch) < 128).strip()
        if clean.startswith('KILL:') or 'melawan' in clean.lower() or 'tunggu' in clean.lower() or 'belum' in clean.lower():
            tag = ' '.join(clean.split()[:5])[:40]
            if is_sl:
                kill_tag_sl[tag] += 1
            if is_win:
                kill_tag_win[tag] += 1

print(f"{'Kill-type tag':<42} {'Total':>6} {'Win':>5} {'SL':>5} {'SL%':>7}")
print('-' * 80)
# Sort by SL rate desc, min sample 10
combined_tags = set(kill_tag_win.keys()) | set(kill_tag_sl.keys())
items = []
for tag in combined_tags:
    w, s = kill_tag_win[tag], kill_tag_sl[tag]
    if w + s >= 10:
        items.append((tag, w + s, w, s, s / (w + s) * 100))
items.sort(key=lambda x: -x[4])
for tag, total, w, s, slr in items[:15]:
    print(f"{tag:<42} {total:>6} {w:>5} {s:>5} {slr:>6.1f}%")

# ============================================================
# FILTER COMBINATIONS — simulasi
# ============================================================
print()
print('=' * 80)
print(' FILTER COMBINATIONS — simulasi WR/EV/Volume tradeoff')
print('=' * 80)


def filt(swing, f):
    return [t for t in swing if f(t)]


print(f"{'Filter':<55} {'N':>5} {'WR':>6} {'EV':>7} {'TotalR':>8} {'SL%':>6}")
print('-' * 90)

baseline = stats(swing)
print(f"{'BASELINE (no filter)':<55} {fmt(baseline)}")

# Single filters
filters_single = [
    ('F1: kills == 0', lambda t: t.get('kills', 0) == 0),
    ('F2: kills <= 1', lambda t: t.get('kills', 0) <= 1),
    ('F3: block SHORT GOOD', lambda t: not (t.get('direction') == 'SHORT' and t.get('quality', '').upper() == 'GOOD')),
    ('F4: block RR1 1.5-2.0', lambda t: not (1.5 <= t.get('rr1', 0) < 2.0)),
    ('F5: cap score <= 23', lambda t: t.get('score', 0) <= 23),
    ('F6: block NY session (16-23 UTC)', lambda t: int(t.get('timestamp', '0000-00-00T00:00:00')[11:13]) < 16),
    ('F7: only quality MODERATE', lambda t: t.get('quality', '').upper() == 'MODERATE'),
]
for name, f in filters_single:
    print(f"{name:<55} {fmt(stats(filt(swing, f)))}")

print()
print(' --- Filter COMBINATIONS ---')

filters_combo = [
    ('C1: kills==0 + RR1<1.5',
     lambda t: t.get('kills', 0) == 0 and t.get('rr1', 0) < 1.5),
    ('C2: kills==0 + block SHORT GOOD',
     lambda t: t.get('kills', 0) == 0 and not (t.get('direction') == 'SHORT' and t.get('quality', '').upper() == 'GOOD')),
    ('C3: MODERATE + kills==0',
     lambda t: t.get('quality', '').upper() == 'MODERATE' and t.get('kills', 0) == 0),
    ('C4: MODERATE only (any kills)',
     lambda t: t.get('quality', '').upper() == 'MODERATE'),
    ('C5: SHORT MODERATE only (champion)',
     lambda t: t.get('quality', '').upper() == 'MODERATE' and t.get('direction') == 'SHORT'),
    ('C6: kills==0 + block NY + RR1<1.5',
     lambda t: (t.get('kills', 0) == 0 and t.get('rr1', 0) < 1.5
                and int(t.get('timestamp', '0000-00-00T00:00:00')[11:13]) < 16)),
    ('C7: kills<=1 + block SHORT GOOD + RR1<1.5',
     lambda t: (t.get('kills', 0) <= 1 and t.get('rr1', 0) < 1.5
                and not (t.get('direction') == 'SHORT' and t.get('quality', '').upper() == 'GOOD'))),
    ('C8: HOLY GRAIL (everything)',
     lambda t: (t.get('kills', 0) == 0 and t.get('rr1', 0) < 1.5
                and t.get('score', 0) <= 23
                and not (t.get('direction') == 'SHORT' and t.get('quality', '').upper() == 'GOOD')
                and int(t.get('timestamp', '0000-00-00T00:00:00')[11:13]) < 16)),
]
for name, f in filters_combo:
    print(f"{name:<55} {fmt(stats(filt(swing, f)))}")

# ============================================================
# Monthly volume projection
# ============================================================
print()
print('=' * 80)
print(' MONTHLY VOLUME PROJECTION (90 hari -> per bulan)')
print('=' * 80)
print(f"{'Filter':<55} {'Trd/mo':>7} {'PnL/mo':>8} {'$1 risk':>10}")
print('-' * 90)


def proj(s):
    if s is None:
        return ('-', '-', '-')
    months = 90 / 30
    return (f"{s['n'] / months:.1f}", f"{s['total_r'] / months:+.1f}R", f"${s['total_r'] / months:+.2f}")


for name, f in [('BASELINE', lambda t: True)] + filters_single + filters_combo:
    s = stats(filt(swing, f))
    if s is None:
        continue
    tm, pm, dm = proj(s)
    print(f"{name:<55} {tm:>7} {pm:>8} {dm:>10}")
