"""
test_engine.py — Test langsung apakah trading engine bisa ambil data.
Jalankan: python test_engine.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=" * 55)
print("TEST TRADING ENGINE")
print("=" * 55)

# 1. Cek versi trading_engine
print("\n[1] Cek versi trading_engine.py")
import inspect
from trading_engine import TradingEngine
src = inspect.getsource(TradingEngine.get_price)
if 'binance.com/api/v3/ticker' in src:
    print("  ✅ get_price sudah pakai Binance")
elif 'cryptocompare' in src.lower():
    print("  ❌ get_price MASIH pakai CryptoCompare — file belum diupdate!")
else:
    print(f"  ⚠️  get_price pakai: unknown")

src_k = inspect.getsource(TradingEngine.get_klines)
if 'binance.com/api/v3/klines' in src_k:
    print("  ✅ get_klines sudah pakai Binance")
elif 'cryptocompare' in src_k.lower():
    print("  ❌ get_klines MASIH pakai CryptoCompare — file belum diupdate!")

# 2. Test ambil data langsung
print("\n[2] Test ambil data ETH")
engine = TradingEngine()

price = engine.get_price('ETH')
if price:
    print(f"  ✅ get_price ETH: ${price['price']:.2f} ({price['change_24h']:+.2f}%)")
else:
    print("  ❌ get_price ETH: None — gagal ambil data")

df = engine.get_klines('ETH', '1h')
if df is not None:
    print(f"  ✅ get_klines ETH 1h: {len(df)} candles, last close=${df['close'].iloc[-1]:.2f}")
else:
    print("  ❌ get_klines ETH 1h: None — gagal ambil candle")

# 3. Test full analyze_coin
print("\n[3] Test analyze_coin ETH 1h")
try:
    result, err = engine.analyze_coin('ETH', '1h', force_fresh=True)
    if err:
        print(f"  ❌ Error: {err}")
    elif result is None:
        print("  ❌ Result: None")
    else:
        print(f"  ✅ analyze_coin OK")
        print(f"     Price: ${result['price']:.2f}")
        print(f"     RSI: {result['rsi']:.1f}")
        sig = result.get('signal')
        if sig:
            print(f"     Signal: {sig['direction']} {sig['quality']}")
        else:
            print(f"     Signal: Tidak ada (normal)")
except Exception as e:
    print(f"  ❌ Exception: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 55)
