"""
debug_full.py — Cek kondisi keseluruhan bot.
Jalankan: python debug_full.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

print("=" * 60)
print("FULL BOT HEALTH CHECK")
print("=" * 60)

# 1. Settings
print("\n[1] SETTINGS:")
enabled  = os.getenv('TRADE_ENABLED','false')
risk     = os.getenv('TRADE_RISK_PER_TRADE','?')
leverage = os.getenv('TRADE_LEVERAGE','?')
maxpos   = os.getenv('TRADE_MAX_POSITIONS','?')
maxloss  = os.getenv('TRADE_MAX_DAILY_LOSS','?')
print(f"  TRADE_ENABLED        = {enabled}")
print(f"  TRADE_RISK_PER_TRADE = {risk}%")
print(f"  TRADE_LEVERAGE       = {leverage}x")
print(f"  TRADE_MAX_POSITIONS  = {maxpos}")
print(f"  TRADE_MAX_DAILY_LOSS = {maxloss}%")

# 2. Chat IDs
print("\n[2] CHAT IDs:")
chat_file = 'data/chat_ids.json'
if os.path.exists(chat_file):
    with open(chat_file) as f:
        ids = json.load(f)
    print(f"  ✅ {len(ids)} user terdaftar: {ids}")
else:
    print("  ❌ Tidak ada — ketik /start di Telegram!")

# 3. Bitunix connection
print("\n[3] BITUNIX CONNECTION:")
from bitunix_trader import BitunixTrader
trader = BitunixTrader()
if not trader.api_key:
    print("  ❌ API key tidak ada")
else:
    print(f"  API Key: {trader.api_key[:8]}...")
    balance = trader.get_balance()
    if balance is not None:
        print(f"  ✅ Balance: ${balance:.2f} USDT")
        risk_amount = balance * float(risk) / 100
        print(f"  Risk per trade: ${risk_amount:.2f}")
    else:
        print("  ❌ Gagal ambil balance")

    positions = trader.get_positions()
    print(f"  Posisi aktif: {len(positions)}/{maxpos}")

# 4. Scan sample
print("\n[4] SCAN 5 COIN SAMPLE:")
from trading_engine import TradingEngine
engine = TradingEngine()

test_coins = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP']
will_trade = []

for coin in test_coins:
    result, err = engine.analyze_coin(coin, '1h')
    if err or result is None:
        print(f"  {coin:6} ❌ {err}")
        continue

    chg24 = result.get('change_24h', 0)
    sig   = result.get('signal')

    if not sig:
        print(f"  {coin:6} ⚪ Tidak di zona S&R | chg24={chg24:+.1f}%")
        continue

    q       = sig.get('quality','')
    d       = sig.get('direction','')
    at_zone = sig.get('at_zone', False)
    score   = sig.get('confluence_score', 0)
    is_pump = abs(chg24) > 20

    # Cek apakah akan dieksekusi (hanya GOOD/IDEAL)
    will_exec = q in ('GOOD', 'IDEAL') and not is_pump

    status = "✅ AKAN DIEKSEKUSI" if will_exec else f"❌ Skip ({('pump' if is_pump else f'quality={q}')})"
    print(f"  {coin:6} {d:6} {q:8} score={score:3} at_zone={at_zone} chg={chg24:+.1f}% | {status}")

    if will_exec:
        will_trade.append(f"{coin} {d}")

# 5. Summary
print("\n[5] SUMMARY:")
print(f"  Bot enabled: {enabled}")
print(f"  Balance OK : {'Ya' if balance else 'Tidak'}")
print(f"  Siap trade : {'Ya' if enabled=='true' and balance else 'Tidak'}")
if will_trade:
    print(f"  Coin siap eksekusi sekarang: {', '.join(will_trade)}")
else:
    print("  Tidak ada coin yang memenuhi kriteria eksekusi saat ini")
    print("  → Normal, tunggu kondisi market yang tepat")

print("\n" + "=" * 60)
print("Jalankan lagi tiap 30 menit untuk pantau kondisi")
print("=" * 60)
