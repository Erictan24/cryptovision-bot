
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bitunix_trader import BitunixTrader

t = BitunixTrader()
print("=== STATUS MONITOR ===")
print(f"Active monitors: {t._active_monitors}")
print()

print("=== POSISI AKTIF ===")
positions = t.get_positions()
for pos in positions:
    sym   = pos.get('symbol','')
    entry = pos.get('avgOpenPrice','0')
    qty   = pos.get('qty','0')
    pnl   = pos.get('unrealizedPNL','0')
    side  = pos.get('side','')
    print(f"{sym} {side} entry={entry} qty={qty} pnl={pnl}")

print()
print("=== TEST BEP FET ===")
# Test geser SL FET ke BEP sekarang
pos_fet = t.get_open_position('FET')
if pos_fet:
    entry = float(pos_fet.get('avgOpenPrice', 0))
    print(f"FET posisi ditemukan, entry={entry}")
    result = t.move_sl_to_bep('FET', entry)
    print(f"BEP result: {result}")
else:
    print("FET tidak ada posisi terbuka (mungkin sudah close)")
