"""debug2.py — Cek full traceback dari error 'price'"""
import sys, pickle, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from backtesting.data_fetcher import CACHE_FILE
from backtesting.replay_engine import BacktestEngine

print("Loading cached data...")
with open(CACHE_FILE, "rb") as f:
    data = pickle.load(f)

engine = BacktestEngine(data)

# Test idx=200 dulu (NO_SIGNAL)
print("\n=== BTC 1h idx=200 (NO_SIGNAL case) ===")
engine.set_context("BTC", "1h", 200)

# Cek get_price langsung
price = engine.get_price("BTC")
print(f"get_price result: {price}")

# Cek get_klines
klines = engine.get_klines("BTC", "1h")
print(f"get_klines main: {len(klines) if klines is not None else None} candles")
klines_h = engine.get_klines("BTC", "1h", is_higher=True)
print(f"get_klines HTF : {len(klines_h) if klines_h is not None else None} candles")
klines_l = engine.get_klines("BTC", "1h", is_lower=True)
print(f"get_klines LTF : {len(klines_l) if klines_l is not None else None} candles")

# Full analyze dengan traceback
print("\n=== BTC 1h idx=200 full analyze ===")
try:
    result, err = engine.analyze_coin("BTC", "1h")
    print(f"result: {type(result)}, err: {err}")
    if result:
        sig = result.get("signal")
        print(f"signal: {sig}")
except Exception as e:
    traceback.print_exc()

# Test idx=500 (EXCEPTION case)
print("\n=== BTC 1h idx=500 (EXCEPTION case) ===")
engine.set_context("BTC", "1h", 500)

price = engine.get_price("BTC")
print(f"get_price result: {price}")

print("\nFull analyze BTC 1h idx=500:")
try:
    result, err = engine.analyze_coin("BTC", "1h")
    print(f"result: {type(result)}, err: {err}")
except Exception as e:
    traceback.print_exc()

# Cek format data di trading_engine
print("\n=== Cek trading_engine.analyze_coin source ===")
import inspect
from trading_engine import TradingEngine
# Cari method yang pakai 'price'
src = inspect.getsource(TradingEngine.analyze_coin)
lines = src.split('\n')
for i, line in enumerate(lines):
    if 'price' in line.lower() and ('get_price' in line or "['price']" in line or '["price"]' in line):
        print(f"  line {i:3}: {line.rstrip()}")
