"""
debug_positions.py — Lihat raw data posisi dari Bitunix.
Jalankan: python debug_positions.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bitunix_trader import BitunixTrader

t = BitunixTrader()
data = t._get("/api/v1/futures/position/get_pending_positions")
print("Raw response:")
print(json.dumps(data, indent=2))
