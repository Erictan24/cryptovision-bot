#!/usr/bin/env python3
"""Update outcome label baris trade existing.

Use case: legacy trade ke-push dengan outcome generic (PROFIT/LOSS) dan ke-bucket
salah di stats page karena pnl_r threshold tidak match user mental model.

Bot mental model:
  TP2 = price actually hit TP2 target (full close di profit)
  TP1 = TP1 hit + BEP active, exit di BEP/Stage trail
  BEP = TP1 belum kena, exit di breakeven
  SL  = TP1 belum kena, net loss

Usage:
  python3 scripts/relabel_trade.py BTC TP2          # 1 entry terbaru
  python3 scripts/relabel_trade.py ETH TP1 --hours 72
  python3 scripts/relabel_trade.py SOL SL --limit 2 --hours 24
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
parser.add_argument("symbol", help="Coin symbol (e.g. BTC)")
parser.add_argument("outcome", choices=["TP1", "TP2", "BEP", "SL"],
                    help="New outcome label")
parser.add_argument("--hours", type=int, default=48,
                    help="Window lookback (default 48)")
parser.add_argument("--limit", type=int, default=1,
                    help="Max baris updated (default 1, max 5)")
args = parser.parse_args()

symbol = args.symbol.upper()
secret = hmac.new(token.encode(), symbol.encode(), hashlib.sha256).hexdigest()

web_url = os.getenv("WEB_URL", "https://cryptovision-web.vercel.app")
payload = {
    "symbol" : symbol,
    "secret" : secret,
    "outcome": args.outcome,
    "hours"  : args.hours,
    "limit"  : args.limit,
}

print(f"PATCH {web_url}/api/trades")
print(f"  symbol={symbol} outcome={args.outcome} hours={args.hours} limit={args.limit}")

r = requests.patch(f"{web_url}/api/trades", json=payload, timeout=10)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:200]}")

if r.status_code >= 400:
    print("\nFAIL — entry tidak ter-update.")
    sys.exit(1)

print("\nDONE — cek dashboard /statistik di web (refresh, cache 60 detik).")
