#!/usr/bin/env python3
"""Hapus entry trade history yang salah ke-push (recovery race condition bug).

Bug context: bot polling Bitunix kadang dapat empty result (network glitch /
rate limit). Bot interpret sebagai "posisi closed" → push fake trade ke
history (`/api/trades`). Padahal posisi masih running di exchange.

Script ini hapus baris paling baru di trades untuk symbol target.

Usage:
  python3 scripts/delete_trade.py PIPPIN              # hapus 1 entry terbaru
  python3 scripts/delete_trade.py BTC --hours 24      # window 24 jam terakhir
  python3 scripts/delete_trade.py XRP --limit 2       # hapus 2 entry terbaru
"""
import os
import sys
import hmac
import hashlib
import argparse
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
ENV  = ROOT / ".env"

if not ENV.exists():
    print(f"ERROR: {ENV} not found")
    sys.exit(1)

token = ""
for line in ENV.read_text().splitlines():
    if line.startswith("TELEGRAM_BOT_TOKEN="):
        token = line.split("=", 1)[1].strip().strip('"').strip("'")
        break

if not token:
    print("ERROR: TELEGRAM_BOT_TOKEN not found in .env")
    sys.exit(1)

parser = argparse.ArgumentParser()
parser.add_argument("symbol", help="Coin symbol (e.g. PIPPIN)")
parser.add_argument("--hours", type=int, default=48,
                    help="Window lookback (default 48)")
parser.add_argument("--limit", type=int, default=1,
                    help="Max baris yang dihapus (default 1, max 5)")
args = parser.parse_args()

symbol = args.symbol.upper()
secret = hmac.new(token.encode(), symbol.encode(), hashlib.sha256).hexdigest()

web_url = os.getenv("WEB_URL", "https://cryptovision-web.vercel.app")
params = {
    "symbol": symbol,
    "secret": secret,
    "hours" : str(args.hours),
    "limit" : str(args.limit),
}

print(f"DELETE {web_url}/api/trades")
print(f"  symbol={symbol} hours={args.hours} limit={args.limit}")

r = requests.delete(f"{web_url}/api/trades", params=params, timeout=10)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:200]}")

if r.status_code >= 400:
    print("\nFAIL — entry tidak terhapus.")
    sys.exit(1)

print("\nDONE — cek dashboard /history di web")
