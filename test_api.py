"""
test_api.py — Test koneksi API dari komputer kamu.
Jalankan: python test_api.py
"""
import requests, json, time

def test(name, url, params=None):
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            print(f"  ✅ {name}: OK")
            return True
        else:
            print(f"  ❌ {name}: HTTP {r.status_code}")
            return False
    except Exception as e:
        print(f"  ❌ {name}: {e}")
        return False

print("=" * 50)
print("TEST KONEKSI API")
print("=" * 50)
print()

print("[1] Binance Public API:")
b1 = test("Ping",         "https://api.binance.com/api/v3/ping")
b2 = test("ETH Price",    "https://api.binance.com/api/v3/ticker/24hr", {"symbol":"ETHUSDT"})
b3 = test("ETH Klines",   "https://api.binance.com/api/v3/klines",     {"symbol":"ETHUSDT","interval":"1h","limit":"5"})
print()

print("[2] Binance Global (kalau .com diblok):")
b4 = test("Ping",         "https://api1.binance.com/api/v3/ping")
b5 = test("ETH Price",    "https://api1.binance.com/api/v3/ticker/24hr", {"symbol":"ETHUSDT"})
print()

print("[3] Bybit (alternatif):")
c1 = test("Ping",         "https://api.bybit.com/v5/market/time")
c2 = test("ETH Price",    "https://api.bybit.com/v5/market/tickers", {"category":"spot","symbol":"ETHUSDT"})
print()

print("[4] OKX (alternatif):")
d1 = test("ETH Price",    "https://www.okx.com/api/v5/market/ticker", {"instId":"ETH-USDT"})
print()

print("[5] Gate.io (alternatif):")
e1 = test("ETH Price",    "https://api.gateio.ws/api/v4/spot/tickers", {"currency_pair":"ETH_USDT"})
print()

print("=" * 50)
results = [b1,b2,b3,b4,b5,c1,c2,d1,e1]
if b1 and b2:
    print("✅ Binance .com OK — pakai ini")
elif b4 and b5:
    print("✅ Binance api1 OK — pakai ini")
elif c1 and c2:
    print("✅ Bybit OK — akan diganti ke Bybit")
elif d1:
    print("✅ OKX OK — akan diganti ke OKX")
elif e1:
    print("✅ Gate.io OK — akan diganti ke Gate.io")
else:
    print("❌ Semua API gagal — coba aktifkan VPN dulu")
print("=" * 50)
