"""
clean_tpsl.py — Hapus SL duplikat dan pasang SL BEP yang benar.
Jalankan: python clean_tpsl.py
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bitunix_trader import BitunixTrader

t = BitunixTrader()

print("=" * 60)
print("CLEAN TPSL — HAPUS DUPLIKAT DAN PASANG BEP BERSIH")
print("=" * 60)

positions = t.get_positions()
if not positions:
    print("Tidak ada posisi aktif")
    exit()

for pos in positions:
    sym    = pos.get('symbol', '')
    entry  = float(pos.get('avgOpenPrice', 0))
    pos_id = pos.get('positionId', '')
    qty    = float(pos.get('qty', 0))
    pnl    = float(pos.get('unrealizedPNL', 0))

    print(f"\n{'='*40}")
    print(f"{sym} | Entry={entry} | Qty={qty} | PnL={pnl:+.4f}")

    tpsl_data = t._get("/api/v1/futures/tpsl/get_pending_orders", {"symbol": sym})
    tpsl_list = tpsl_data.get('data', [])
    if not isinstance(tpsl_list, list):
        tpsl_list = []

    pos_tpsl = [o for o in tpsl_list if o.get('positionId') == pos_id]
    print(f"  TPSL aktif: {len(pos_tpsl)} order")
    for o in pos_tpsl:
        print(f"    id={o.get('id')} sl={o.get('slPrice')} tp={o.get('tpPrice')}")

    sl_orders = [(o.get('id'), o.get('slPrice')) for o in pos_tpsl if o.get('slPrice')]

    # Cancel endpoint yang benar: /tpsl/cancel_order (singular), orderId string
    print(f"\n  >> Cancel {len(sl_orders)} SL order lama...")
    for oid, sl_price in sl_orders:
        r = t._post("/api/v1/futures/tpsl/cancel_order", {
            "symbol" : sym,
            "orderId": str(oid),
        })
        print(f"    Cancel sl={sl_price} id={oid}: code={r.get('code')} msg={r.get('msg','')}")
        time.sleep(0.3)

    time.sleep(1)

    print(f"\n  >> Pasang SL BEP @ {entry}...")
    body = {
        "symbol"      : sym,
        "positionId"  : str(pos_id),
        "slPrice"     : str(round(entry, 8)),
        "slStopType"  : "MARK_PRICE",
        "slOrderType" : "MARKET",
    }
    r = t._post("/api/v1/futures/tpsl/position/place_order", body)
    print(f"  place BEP: code={r.get('code')} msg={r.get('msg')} orderId={r.get('data',{}).get('orderId','')}")

    time.sleep(1)
    verify = t._get("/api/v1/futures/tpsl/get_pending_orders", {"symbol": sym})
    v_list = verify.get('data', [])
    if isinstance(v_list, list):
        v_pos = [o for o in v_list if o.get('positionId') == pos_id]
        print(f"\n  Verifikasi ({len(v_pos)} TPSL aktif):")
        for o in v_pos:
            print(f"    id={o.get('id')} sl={o.get('slPrice')} tp={o.get('tpPrice')}")

print("\n" + "=" * 60)
print("Selesai. Cek Bitunix untuk konfirmasi.")
print("=" * 60)