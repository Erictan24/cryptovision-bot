"""
debug_bitunix.py — Cek raw response API Bitunix.
Jalankan: python debug_bitunix.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bitunix_trader import BitunixTrader

t = BitunixTrader()

if not t.api_key:
    print("ERROR: BITUNIX_API_KEY tidak ada di .env")
    exit()

print(f"API Key: {t.api_key[:8]}...")
print()

print("[1] Test balance endpoint:")
r1 = t._get("/api/v1/futures/account", {"marginCoin": "USDT"})
print(f"  Response: {r1}")
print()

print("[2] Test positions endpoint:")
r2 = t._get("/api/v1/futures/position/get_pending_positions")
print(f"  Response: {r2}")
print()

print("[3] Test ticker endpoint (public):")
r3 = t._get("/api/v1/futures/market/tickers", {"symbols": "BTCUSDT"})
print(f"  Response code: {r3.get('code')}, data type: {type(r3.get('data'))}")
