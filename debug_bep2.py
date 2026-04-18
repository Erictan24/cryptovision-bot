"""
debug_bep2.py — Cek TPSL order aktif dan paksa geser SL ke BEP.
Jalankan: python debug_bep2.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bitunix_trader import BitunixTrader

t = BitunixTrader()

print("=" * 60)
print("DEBUG BEP — CEK TPSL ORDER DAN PAKSA GESER SL")
print("=" * 60)

# 1. Cek semua TPSL order aktif
print("\n[1] TPSL Order aktif di exchange:")
data = t._get("/api/v1/futures/tpsl/get_pending_orders")
print(f"  Raw response: {json.dumps(data, indent=2)[:500]}")

# 2. Posisi FET
print("\n[2] Posisi FET detail:")
pos = t.get_open_position('FET')
if pos:
    print(json.dumps(pos, indent=2))
    entry  = float(pos.get('avgOpenPrice', 0))
    pos_id = pos.get('positionId', '')
    print(f"\n  entry={entry}, positionId={pos_id}")

    # 3. Coba modify dengan berbagai format
    print("\n[3] Test modify SL dengan MARK_PRICE:")
    body1 = {
        "symbol"     : "FETUSDT",
        "positionId" : str(pos_id),
        "slPrice"    : str(entry),
        "slStopType" : "MARK_PRICE",
    }
    r1 = t._post("/api/v1/futures/tpsl/position/modify_order", body1)
    print(f"  MARK_PRICE: {r1}")

    print("\n[4] Test modify SL dengan LAST_PRICE:")
    body2 = {
        "symbol"     : "FETUSDT",
        "positionId" : str(pos_id),
        "slPrice"    : str(entry),
        "slStopType" : "LAST_PRICE",
    }
    r2 = t._post("/api/v1/futures/tpsl/position/modify_order", body2)
    print(f"  LAST_PRICE: {r2}")

    print("\n[5] Test place NEW TPSL order (bukan modify):")
    body3 = {
        "symbol"      : "FETUSDT",
        "positionId"  : str(pos_id),
        "slPrice"     : str(entry),
        "slStopType"  : "MARK_PRICE",
        "slOrderType" : "MARKET",
    }
    r3 = t._post("/api/v1/futures/tpsl/position/place_order", body3)
    print(f"  place_order: {r3}")

else:
    print("  FET tidak ada posisi aktif")

print("\n" + "=" * 60)
