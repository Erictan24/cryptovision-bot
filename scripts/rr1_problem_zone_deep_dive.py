"""Investigate 40 trades di RR1 1.5-2.0 zone — kenapa WR jeblok 32.5%?"""
import pickle
from collections import Counter, defaultdict

RESULT = 'backtesting/results/unified_top200_20260513_2036.pkl'

with open(RESULT, 'rb') as f:
    payload = pickle.load(f)

swing = [t for t in payload['trades'] if t.get('engine', '').upper() == 'SWING']

problem_zone = [t for t in swing if 1.5 <= t.get('rr1', 0) < 2.0]
healthy_zone = [t for t in swing if t.get('rr1', 0) < 1.2]
mid_zone = [t for t in swing if 1.2 <= t.get('rr1', 0) < 1.5]

print(f"Problem zone (RR1 1.5-2.0): {len(problem_zone)} trades")
print(f"Healthy zone (RR1 <1.2):    {len(healthy_zone)} trades")
print(f"Mid zone (RR1 1.2-1.5):     {len(mid_zone)} trades")
print()

def avg(t_list, key):
    vals = [t.get(key, 0) for t in t_list if isinstance(t.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else 0


# ============================================================
# COMPARE problem zone vs healthy zone — dimensi yg beda
# ============================================================
print('=' * 80)
print(' PROBLEM ZONE vs HEALTHY ZONE — apa yang beda?')
print('=' * 80)

print(f"{'Dimension':<25} {'Problem (RR 1.5-2)':<22} {'Healthy (RR <1.2)':<22}")
print('-' * 80)
for key in ['rr1', 'rr2', 'score', 'kills', 'bars_to_outcome', 'pnl_r']:
    p = avg(problem_zone, key)
    h = avg(healthy_zone, key)
    print(f"{key:<25} {p:<22.3f} {h:<22.3f}")

# Direction split
p_long = sum(1 for t in problem_zone if t.get('direction') == 'LONG')
p_short = sum(1 for t in problem_zone if t.get('direction') == 'SHORT')
h_long = sum(1 for t in healthy_zone if t.get('direction') == 'LONG')
h_short = sum(1 for t in healthy_zone if t.get('direction') == 'SHORT')
print(f"{'LONG %':<25} {p_long/len(problem_zone)*100:<22.1f} {h_long/len(healthy_zone)*100:<22.1f}")
print(f"{'SHORT %':<25} {p_short/len(problem_zone)*100:<22.1f} {h_short/len(healthy_zone)*100:<22.1f}")

# Quality split
for q in ('GOOD', 'MODERATE'):
    p_q = sum(1 for t in problem_zone if t.get('quality','').upper()==q)
    h_q = sum(1 for t in healthy_zone if t.get('quality','').upper()==q)
    print(f"{q+' %':<25} {p_q/len(problem_zone)*100:<22.1f} {h_q/len(healthy_zone)*100:<22.1f}")

# ============================================================
# Direction x quality in problem zone
# ============================================================
print()
print('=' * 80)
print(' PROBLEM ZONE breakdown by DIRECTION x QUALITY')
print('=' * 80)
print(f"{'Combo':<22} {'N':>4} {'WR%':>6} {'EV':>7} {'TotalR':>8}")
print('-' * 60)
for d in ('LONG', 'SHORT'):
    for q in ('GOOD', 'MODERATE'):
        sub = [t for t in problem_zone if t.get('direction')==d and t.get('quality','').upper()==q]
        if sub:
            n = len(sub)
            wins = sum(1 for t in sub if t.get('pnl_r',0)>0)
            tot = sum(t.get('pnl_r',0) for t in sub)
            print(f"{d+' '+q:<22} {n:>4} {wins/n*100:>5.1f}% {tot/n:>+6.2f}R {tot:>+7.1f}R")

# ============================================================
# Coin distribution in problem zone
# ============================================================
print()
print('=' * 80)
print(' COIN distribution di problem zone (max 15)')
print('=' * 80)
coin_in_problem = Counter(t.get('symbol','') for t in problem_zone)
for c, n in coin_in_problem.most_common(15):
    sub = [t for t in problem_zone if t.get('symbol','')==c]
    wins = sum(1 for t in sub if t.get('pnl_r',0)>0)
    tot = sum(t.get('pnl_r',0) for t in sub)
    print(f"  {c:<10} n={n} WR={wins/n*100:.0f}% TotalR={tot:+.1f}R")

# ============================================================
# OUTCOME breakdown
# ============================================================
print()
print('=' * 80)
print(' OUTCOME breakdown')
print('=' * 80)
outcomes_p = Counter(t.get('outcome','UNKNOWN') for t in problem_zone)
outcomes_h = Counter(t.get('outcome','UNKNOWN') for t in healthy_zone)
all_outcomes = set(outcomes_p.keys()) | set(outcomes_h.keys())
print(f"{'Outcome':<15} {'Problem %':>12} {'Healthy %':>12}")
print('-' * 45)
for o in all_outcomes:
    pp = outcomes_p.get(o, 0) / len(problem_zone) * 100
    hp = outcomes_h.get(o, 0) / len(healthy_zone) * 100
    print(f"{o:<15} {pp:>11.1f}% {hp:>11.1f}%")

# ============================================================
# RR1 vs RR2 ratio — apakah TP2 wajar atau juga jauh?
# ============================================================
print()
print('=' * 80)
print(' RR1 vs RR2 ratio di problem zone (apakah SL ketat atau TP1 jauh?)')
print('=' * 80)
print(f"{'#':>3} {'Coin':<10} {'Dir':<6} {'Q':<10} {'RR1':>5} {'RR2':>5} {'Sc':>3} {'K':>2} {'Out':<6} {'PnL':>7}")
print('-' * 80)
for i, t in enumerate(problem_zone[:20]):
    print(f"{i+1:>3} {t.get('symbol',''):<10} {t.get('direction',''):<6} {t.get('quality',''):<10} "
          f"{t.get('rr1',0):>5.2f} {t.get('rr2',0):>5.2f} {t.get('score',0):>3} {t.get('kills',0):>2} "
          f"{t.get('outcome','')[:6]:<6} {t.get('pnl_r',0):>+6.2f}R")

# ============================================================
# Sample reasons di problem zone — tag dominan
# ============================================================
print()
print('=' * 80)
print(' TAG dominan di problem zone (yang TIDAK ada di healthy zone)')
print('=' * 80)
prob_tags = Counter()
heal_tags = Counter()
for t in problem_zone:
    for r in t.get('reasons', []):
        clean = ''.join(ch for ch in r if ord(ch) < 128).strip()
        tag = ' '.join(clean.split()[:4])[:35]
        prob_tags[tag] += 1
for t in healthy_zone:
    for r in t.get('reasons', []):
        clean = ''.join(ch for ch in r if ord(ch) < 128).strip()
        tag = ' '.join(clean.split()[:4])[:35]
        heal_tags[tag] += 1

# Tags that appear MUCH MORE in problem zone
diffs = []
for tag, p_count in prob_tags.items():
    p_rate = p_count / len(problem_zone)
    h_rate = heal_tags.get(tag, 0) / len(healthy_zone)
    if p_count >= 5 and p_rate > h_rate * 1.5:
        diffs.append((tag, p_count, p_rate*100, h_rate*100, p_rate - h_rate))
diffs.sort(key=lambda x: -x[4])
print(f"{'Tag':<40} {'P_count':>8} {'P_rate%':>8} {'H_rate%':>8}")
print('-' * 70)
for tag, pc, pr, hr, _ in diffs[:15]:
    print(f"{tag:<40} {pc:>8} {pr:>7.1f}% {hr:>7.1f}%")
