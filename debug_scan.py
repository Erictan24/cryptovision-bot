"""
debug_scan.py — Debug kenapa tidak ada signal GOOD.
Jalankan: python debug_scan.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine import TradingEngine
from dotenv import load_dotenv
load_dotenv()

engine = TradingEngine()

print("=" * 60)
print("SCAN SEMUA COIN — DETAIL")
print("=" * 60)

# Ambil top coins
coins = engine.get_top_coins(30)
print(f"\nCoin yang akan discan ({len(coins)}): {coins}\n")
print("-" * 60)

good_count   = 0
limit_count  = 0
wait_count   = 0
nozone_count = 0

for symbol in coins:
    result, err = engine.analyze_coin(symbol, '1h')
    if err or result is None:
        print(f"{symbol:10} ❌ Error: {err}")
        continue

    sig = result.get('signal')
    if not sig:
        nozone_count += 1
        print(f"{symbol:10} ⚪ Tidak di area S&R")
        continue

    q     = sig.get('quality', '')
    d     = sig.get('direction', '')
    score = sig.get('confluence_score', 0)
    at_z  = sig.get('at_zone', False)
    otype = sig.get('order_type', '')
    rr2   = sig.get('rr2', 0)

    if q == 'GOOD':
        good_count += 1
        marker = "✅ GOOD — AKAN AUTO TRADE"
    elif q == 'LIMIT' and at_z:
        limit_count += 1
        marker = "🔔 LIMIT at_zone — AKAN NOTIF"
    elif q == 'LIMIT':
        limit_count += 1
        marker = "📌 LIMIT — tunggu entry"
    else:
        wait_count += 1
        marker = f"⏳ {q}"

    print(f"{symbol:10} {d:6} {q:8} score={score:2} RR={rr2:.1f} {marker}")

print("\n" + "=" * 60)
print(f"GOOD (auto trade): {good_count}")
print(f"LIMIT             : {limit_count}")
print(f"Tidak di zona     : {nozone_count}")
print(f"WAIT/lain         : {wait_count}")
print("=" * 60)

if good_count == 0:
    print("\n⚠️  Tidak ada GOOD signal saat ini.")
    print("   Kondisi market sedang tidak ada setup yang valid.")
    print("   Normal — tunggu kondisi yang tepat.")