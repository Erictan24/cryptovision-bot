"""
debug_daily.py — Cek data daily loss dari exchange.
Jalankan: python debug_daily.py
"""
import sys, os, json
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bitunix_trader import BitunixTrader

t = BitunixTrader()

print("=" * 60)
print("DEBUG DAILY LOSS")
print("=" * 60)

now = datetime.now()
print(f"Waktu sekarang : {now.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"Trade date     : {t._get_trade_date()}")

# Hitung reset time (jam 8 pagi)
if now.hour < 8:
    reset_time = now.replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(days=1)
else:
    reset_time = now.replace(hour=8, minute=0, second=0, microsecond=0)

print(f"Reset sejak    : {reset_time.strftime('%Y-%m-%d %H:%M:%S')}")
today_start = int(reset_time.timestamp() * 1000)
print(f"Timestamp ms   : {today_start}")

print("\n[1] Fetch history positions sejak jam 8...")
data = t._get("/api/v1/futures/position/get_history_positions", {
    "startTime": str(today_start),
    "limit"    : "50",
})
print(f"Code: {data.get('code')} msg: {data.get('msg','')}")

raw = data.get('data', [])
if isinstance(raw, dict):
    positions = raw.get('positionList', raw.get('list', []))
elif isinstance(raw, list):
    positions = raw
else:
    positions = []

print(f"Jumlah posisi  : {len(positions)}")
print()

total_loss = 0.0
total_profit = 0.0
for pos in positions:
    pnl    = float(pos.get('realizedPNL', 0))
    sym    = pos.get('symbol', '')
    ctime  = pos.get('ctime', 0)
    mtime  = pos.get('mtime', 0)
    close_time = datetime.fromtimestamp(int(mtime)/1000).strftime('%Y-%m-%d %H:%M') if mtime else '?'
    open_time  = datetime.fromtimestamp(int(ctime)/1000).strftime('%Y-%m-%d %H:%M') if ctime else '?'
    print(f"  {sym}: PnL={pnl:+.4f} | open={open_time} | close={close_time}")
    if pnl < 0:
        total_loss += abs(pnl)
    elif pnl > 0:
        total_profit += pnl

net = max(0.0, total_loss - total_profit)
print(f"\nTotal loss  : ${total_loss:.2f}")
print(f"Total profit: ${total_profit:.2f}")
print(f"Net loss    : ${net:.2f}")
print(f"\nNilai di bot: ${t._daily_loss_usd:.2f}")

print("\n[2] Paksa sync ulang...")
t.sync_daily_loss_from_exchange()
print(f"Setelah sync: ${t._daily_loss_usd:.2f}")
print("=" * 60)
