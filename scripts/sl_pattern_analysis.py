"""Analisis SL pattern di 692 SWING trades — cari penyebab loss di ENGINE level."""
import pickle
from collections import defaultdict, Counter
from datetime import datetime

RESULT = 'backtesting/results/unified_top200_20260513_2036.pkl'

with open(RESULT, 'rb') as f:
    payload = pickle.load(f)

trades = payload['trades']
swing = [t for t in trades if t.get('engine', '').upper() == 'SWING']

print(f"Total SWING trades: {len(swing)}")
print()


def stats(t_list, label=''):
    n = len(t_list)
    if n == 0:
        return (0, 0, 0, 0, 0)
    wins = sum(1 for t in t_list if t.get('pnl_r', 0) > 0)
    total_r = sum(t.get('pnl_r', 0) for t in t_list)
    sl = sum(1 for t in t_list if t.get('outcome', '').upper() in ('SL', 'SL_HIT'))
    tp2 = sum(1 for t in t_list if t.get('outcome', '').upper() in ('TP2', 'TP2_HIT'))
    return (n, wins/n*100, total_r/n, total_r, sl/n*100)


def print_table(title, rows, header=('Group', 'N', 'WR%', 'EV', 'TotalR', 'SL%')):
    print('=' * 80)
    print(f' {title}')
    print('=' * 80)
    print(f"{header[0]:<25} {header[1]:>6} {header[2]:>7} {header[3]:>8} {header[4]:>9} {header[5]:>7}")
    print('-' * 80)
    for label, t_list in rows:
        n, wr, ev, tot, slp = stats(t_list)
        if n > 0:
            print(f"{label:<25} {n:>6} {wr:>6.1f}% {ev:>+7.2f}R {tot:>+8.1f}R {slp:>6.1f}%")
        else:
            print(f"{label:<25} {0:>6} {'-':>7} {'-':>8} {'-':>9} {'-':>7}")
    print()


# ============================================================
# 1. SCORE BREAKDOWN
# ============================================================
score_groups = [
    ('Score < 18 (sub-thr)', [t for t in swing if t.get('score', 0) < 18]),
    ('Score 18-19', [t for t in swing if 18 <= t.get('score', 0) <= 19]),
    ('Score 20-21', [t for t in swing if 20 <= t.get('score', 0) <= 21]),
    ('Score 22-23', [t for t in swing if 22 <= t.get('score', 0) <= 23]),
    ('Score 24-25', [t for t in swing if 24 <= t.get('score', 0) <= 25]),
    ('Score 26+', [t for t in swing if t.get('score', 0) >= 26]),
]
print_table('1. WR by SCORE — apakah score tinggi = WR tinggi?', score_groups)

# ============================================================
# 2. KILLS BREAKDOWN
# ============================================================
kills_groups = [
    ('0 kills', [t for t in swing if t.get('kills', 0) == 0]),
    ('1 kill', [t for t in swing if t.get('kills', 0) == 1]),
    ('2 kills', [t for t in swing if t.get('kills', 0) == 2]),
    ('3+ kills', [t for t in swing if t.get('kills', 0) >= 3]),
]
print_table('2. WR by KILLS — apakah lebih banyak kills = lebih jelek?', kills_groups)

# ============================================================
# 3. QUALITY TIER BREAKDOWN
# ============================================================
qual_groups = [
    ('GOOD', [t for t in swing if t.get('quality', '').upper() == 'GOOD']),
    ('MODERATE', [t for t in swing if t.get('quality', '').upper() == 'MODERATE']),
    ('WAIT', [t for t in swing if t.get('quality', '').upper() == 'WAIT']),
]
print_table('3. WR by QUALITY tier', qual_groups)

# ============================================================
# 4. TIMEFRAME BREAKDOWN
# ============================================================
tf_groups = [
    ('1h', [t for t in swing if t.get('tf', '') == '1h']),
    ('4h', [t for t in swing if t.get('tf', '') == '4h']),
]
print_table('4. WR by TIMEFRAME', tf_groups)

# ============================================================
# 5. DIRECTION x QUALITY
# ============================================================
dq_groups = []
for d in ('LONG', 'SHORT'):
    for q in ('GOOD', 'MODERATE', 'WAIT'):
        sub = [t for t in swing if t.get('direction') == d and t.get('quality', '').upper() == q]
        if sub:
            dq_groups.append((f'{d} {q}', sub))
print_table('5. DIRECTION x QUALITY — di mana LONG paling jelek?', dq_groups)

# ============================================================
# 6. RR1 BREAKDOWN
# ============================================================
rr_groups = [
    ('RR1 < 1.2', [t for t in swing if t.get('rr1', 0) < 1.2]),
    ('RR1 1.2-1.5', [t for t in swing if 1.2 <= t.get('rr1', 0) < 1.5]),
    ('RR1 1.5-2.0', [t for t in swing if 1.5 <= t.get('rr1', 0) < 2.0]),
    ('RR1 2.0+', [t for t in swing if t.get('rr1', 0) >= 2.0]),
]
print_table('6. WR by RR1 — apakah RR ketat lebih win?', rr_groups)

# ============================================================
# 7. SESSION/HOUR
# ============================================================
def hour_of(t):
    try:
        ts = t.get('timestamp', '')
        return datetime.fromisoformat(ts).hour
    except Exception:
        return -1

session_groups = [
    ('Asia (00-07 UTC)', [t for t in swing if 0 <= hour_of(t) < 8]),
    ('London (08-15 UTC)', [t for t in swing if 8 <= hour_of(t) < 16]),
    ('NY (16-23 UTC)', [t for t in swing if 16 <= hour_of(t) < 24]),
]
print_table('7. WR by SESSION (entry hour UTC)', session_groups)

# ============================================================
# 8. REASONS / SETUP TAG analysis
# ============================================================
print('=' * 80)
print(' 8. REASONS analysis — tag yang SERING di SL vs sering di WIN')
print('=' * 80)
tag_in_win = Counter()
tag_in_sl = Counter()
for t in swing:
    is_sl = t.get('outcome', '').upper() in ('SL', 'SL_HIT')
    is_win = t.get('pnl_r', 0) > 0
    for r in t.get('reasons', []):
        # Strip non-ASCII (emoji) and extract first 4 words
        clean = ''.join(ch for ch in r if ord(ch) < 128).strip()
        tag = ' '.join(clean.split()[:4])[:35]
        if not tag:
            continue
        if is_sl:
            tag_in_sl[tag] += 1
        if is_win:
            tag_in_win[tag] += 1

# Find tags with high SL bias vs win
combined = set(tag_in_win.keys()) | set(tag_in_sl.keys())
biases = []
for tag in combined:
    win = tag_in_win[tag]
    sl = tag_in_sl[tag]
    total = win + sl
    if total >= 30:  # require minimum sample
        sl_rate = sl / total * 100
        biases.append((tag, total, win, sl, sl_rate))

# Sort by SL rate descending (highest = most "jebakan")
biases.sort(key=lambda x: -x[4])
print(f"{'TAG (4 first words)':<40} {'Total':>6} {'Win':>5} {'SL':>4} {'SL%':>7}")
print('-' * 80)
print('  --- Top 10 JEBAKAN (highest SL rate) ---')
for tag, total, win, sl, slrate in biases[:10]:
    print(f"{tag:<40} {total:>6} {win:>5} {sl:>4} {slrate:>6.1f}%")
print()
print('  --- Top 10 JUARA (lowest SL rate) ---')
for tag, total, win, sl, slrate in sorted(biases, key=lambda x: x[4])[:10]:
    print(f"{tag:<40} {total:>6} {win:>5} {sl:>4} {slrate:>6.1f}%")

# ============================================================
# 9. LONG vs SHORT deep dive
# ============================================================
print()
print('=' * 80)
print(' 9. LONG vs SHORT — kenapa LONG WR drop?')
print('=' * 80)
long_t = [t for t in swing if t.get('direction') == 'LONG']
short_t = [t for t in swing if t.get('direction') == 'SHORT']

def avg(t_list, key):
    vals = [t.get(key, 0) for t in t_list if isinstance(t.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else 0

print(f"LONG  N={len(long_t)}  WR={sum(1 for t in long_t if t.get('pnl_r',0)>0)/len(long_t)*100:.1f}%  avgScore={avg(long_t,'score'):.1f}  avgKills={avg(long_t,'kills'):.2f}  avgRR1={avg(long_t,'rr1'):.2f}")
print(f"SHORT N={len(short_t)}  WR={sum(1 for t in short_t if t.get('pnl_r',0)>0)/len(short_t)*100:.1f}%  avgScore={avg(short_t,'score'):.1f}  avgKills={avg(short_t,'kills'):.2f}  avgRR1={avg(short_t,'rr1'):.2f}")

# Hours faster to SL
long_sl = [t for t in long_t if t.get('outcome','').upper() in ('SL','SL_HIT')]
short_sl = [t for t in short_t if t.get('outcome','').upper() in ('SL','SL_HIT')]
print(f"\nLONG  SL avg bars-to-outcome: {avg(long_sl, 'bars_to_outcome'):.1f}")
print(f"SHORT SL avg bars-to-outcome: {avg(short_sl, 'bars_to_outcome'):.1f}")

# ============================================================
# 10. BARS TO OUTCOME (speed)
# ============================================================
btw_groups = [
    ('Fast (<10 bars)', [t for t in swing if t.get('bars_to_outcome', 0) < 10]),
    ('Mid (10-25 bars)', [t for t in swing if 10 <= t.get('bars_to_outcome', 0) < 25]),
    ('Slow (25-50 bars)', [t for t in swing if 25 <= t.get('bars_to_outcome', 0) < 50]),
    ('Very slow (50+ bars)', [t for t in swing if t.get('bars_to_outcome', 0) >= 50]),
]
print()
print_table('10. WR by BARS-TO-OUTCOME (speed of resolution)', btw_groups)
