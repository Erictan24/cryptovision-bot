"""
force_bep.py — Paksa geser SL ke BEP untuk semua posisi aktif.
Jalankan: python force_bep.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bitunix_trader import BitunixTrader

t = BitunixTrader()

print("=" * 60)
print("FORCE BEP — GESER SL KE ENTRY SEKARANG")
print("=" * 60)

positions = t.get_positions()
if not positions:
    print("Tidak ada posisi aktif")
    exit()

for pos in positions:
    sym    = pos.get('symbol', '')
    sym_c  = sym.replace('USDT', '')
    entry  = float(pos.get('avgOpenPrice', 0))
    qty    = float(pos.get('qty', 0))
    pnl    = float(pos.get('unrealizedPNL', 0))
    pos_id = pos.get('positionId', '')
    side   = pos.get('side', '')

    print(f"\n{'='*40}")
    print(f"{sym} {side} | Entry={entry} | Qty={qty} | PnL={pnl:+.4f}")
    print(f"positionId={pos_id}")

    # Cek TPSL aktif untuk posisi ini
    tpsl_data = t._get("/api/v1/futures/tpsl/get_pending_orders",
                       {"symbol": sym})
    tpsl_list = tpsl_data.get('data', [])
    if isinstance(tpsl_list, list):
        for o in tpsl_list:
            if o.get('positionId') == pos_id:
                print(f"  TPSL: sl={o.get('slPrice')} tp={o.get('tpPrice')}")

    print(f"\n  >> Pasang SL BEP @ {entry}...")

    # Coba place_order dulu (buat TPSL baru)
    body = {
        "symbol"      : sym,
        "positionId"  : str(pos_id),
        "slPrice"     : str(round(entry, 8)),
        "slStopType"  : "MARK_PRICE",
        "slOrderType" : "MARKET",
    }
    r = t._post("/api/v1/futures/tpsl/position/place_order", body)
    print(f"  place_order: code={r.get('code')} msg={r.get('msg')} orderId={r.get('data',{}).get('orderId','')}")

    if r.get('code') != 0:
        # Coba modify
        body2 = {
            "symbol"     : sym,
            "positionId" : str(pos_id),
            "slPrice"    : str(round(entry, 8)),
            "slStopType" : "MARK_PRICE",
        }
        r2 = t._post("/api/v1/futures/tpsl/position/modify_order", body2)
        print(f"  modify_order: code={r2.get('code')} msg={r2.get('msg')}")

print("\n" + "=" * 60)
print("Selesai. Cek Bitunix untuk konfirmasi SL sudah terpasang.")
print("=" * 60)
