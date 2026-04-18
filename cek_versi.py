"""
cek_versi.py — Cek semua file sudah terupdate + test logika kritis.
"""
import sys, os, importlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Force reload semua module yang relevan
mods = ['config', 'signal_generator', 'trading_engine', 'smc_analyzer', 'session_filter']
for m in mods:
    if m in sys.modules:
        importlib.reload(sys.modules[m])

print("=" * 60)
print("CEK VERSI FILE BOT — versi terbaru")
print("=" * 60)

# 1. config.py
print("\n[1] config.py")
from config import SIGNAL_PARAMS as SP, SCAN_POOL
sg = SP.get('score_good', '?')
sm = SP.get('score_moderate', '?')
hr = SP.get('score_hard_reject', '?')
si = SP.get('score_ideal', '?')
print(f"    SCAN_POOL       = {SCAN_POOL}")
print(f"    score_ideal     = {si}  (harus 999)")
print(f"    score_good      = {sg}  (harus 21)")
print(f"    score_moderate  = {sm}  (harus 3)")
print(f"    score_hard_reject = {hr}  (harus 24)")
ok1 = (si == 999 and sg == 21 and sm == 3 and hr == 24)
print(f"    Status: {'✅ OK' if ok1 else '❌ BELUM DIUPDATE'}")

# 2. signal_generator.py — rejection gate strength 3 cap GOOD
print("\n[2] signal_generator.py — rejection strength 3 cap GOOD")
from signal_generator import _apply_rejection_gate, _determine_quality
rj3 = {'confirmed': True, 'strength': 3, 'pattern': 'Hammer', 'detail': 'Hammer'}
rj4 = {'confirmed': True, 'strength': 4, 'pattern': 'Pin Bar', 'detail': 'Pin Bar'}
q3, _, _ = _apply_rejection_gate('GOOD', [], 21, rj3, 'support')
q4, _, _ = _apply_rejection_gate('GOOD', [], 21, rj4, 'support')
r24 = _determine_quality(24, 0)
r21 = _determine_quality(21, 0)
print(f"    strength=3 + GOOD → {q3}  (harus MODERATE)")
print(f"    strength=4 + GOOD → {q4}  (harus GOOD)")
print(f"    score=24, kills=0 → {r24}  (harus None)")
print(f"    score=21, kills=0 → {r21}  (harus GOOD)")
ok2 = (q3 == 'MODERATE' and q4 == 'GOOD' and r24 is None and r21 == 'GOOD')
print(f"    Status: {'✅ OK' if ok2 else '❌ BELUM DIUPDATE'}")

# 3. trading_engine.py — no IDEAL upgrade
print("\n[3] trading_engine.py — no IDEAL upgrade via LTF")
import inspect
from trading_engine import TradingEngine
src = inspect.getsource(TradingEngine.analyze_coin)
has_ideal_upgrade = ("entry['quality'] = 'IDEAL'" in src and 'ltf' in src.lower() and 'upgrade' in src.lower())
print(f"    GOOD→IDEAL via LTF: {'❌ MASIH ADA' if has_ideal_upgrade else '✅ SUDAH DIHAPUS'}")
ok3 = not has_ideal_upgrade

# 4. Whale flow filter
print("\n[4] signal_generator.py — whale flow filter")
from signal_generator import generate_entry_signal
sg_src = inspect.getsource(generate_entry_signal)
has_whale = 'whale_stop_selling' in sg_src
print(f"    Whale flow filter: {'✅ ADA' if has_whale else '❌ TIDAK ADA'}")
ok4 = has_whale

# 5. session_filter.py
print("\n[5] session_filter.py")
try:
    from session_filter import get_current_session
    print("    ✅ Ada")
    ok5 = True
except ImportError:
    print("    ❌ TIDAK ADA")
    ok5 = False

# Ringkasan
print("\n" + "=" * 60)
all_ok = all([ok1, ok2, ok3, ok4, ok5])
if all_ok:
    print("✅ SEMUA FILE TERUPDATE — siap backtest")
else:
    print("❌ FILE YANG PERLU DIUPDATE:")
    if not ok1: print("   → config.py")
    if not ok2: print("   → signal_generator.py (rejection gate)")
    if not ok3: print("   → trading_engine.py (LTF upgrade)")
    if not ok4: print("   → signal_generator.py (whale filter)")
    if not ok5: print("   → session_filter.py (tambahkan ke folder)")
print("=" * 60)