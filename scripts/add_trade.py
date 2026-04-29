#!/usr/bin/env python3
"""POST manual trade history ke /api/trades.

Untuk import trade lama yang missing di history web (misal trade
sebelum bot push ke web di-implement, atau trade yang ke-skip karena
bug). Wajib HMAC auth.

Usage:
  python3 scripts/add_trade.py BTC SHORT \\
    --entry 95000 --exit 92000 --sl 96000 --tp1 94000 --tp2 92000 \\
    --qty 0.01 --leverage 10 \\
    --pnl-usd 30 --pnl-r 3.0 --outcome PROFIT \\
    --opened "2026-04-28 14:00:00" --closed "2026-04-29 02:00:00" \\
    --strategy swing --quality GOOD

Field opsional: --bep-done (flag), --strategy default swing,
--quality default GOOD.
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
parser.add_argument("symbol", help="Coin (e.g. BTC)")
parser.add_argument("direction", choices=["LONG", "SHORT"])
parser.add_argument("--strategy", default="swing", choices=["swing", "scalp"])
parser.add_argument("--quality", default="GOOD")
parser.add_argument("--entry",   type=float, required=True)
parser.add_argument("--exit",    dest="exit_price", type=float, required=True,
                    help="Exit price (saat close)")
parser.add_argument("--sl",      type=float, required=True)
parser.add_argument("--tp1",     type=float, required=True)
parser.add_argument("--tp2",     type=float, required=True)
parser.add_argument("--qty",     type=float, default=0)
parser.add_argument("--leverage", type=int, default=10)
parser.add_argument("--pnl-usd", type=float, required=True)
parser.add_argument("--pnl-r",   type=float, required=True)
parser.add_argument("--outcome", default="PROFIT",
                    choices=["PROFIT", "LOSS", "BEP"])
parser.add_argument("--bep-done", action="store_true",
                    help="Flag: TP1 sudah kena (BEP active)")
parser.add_argument("--opened",  required=True,
                    help="Opened timestamp ISO (e.g. '2026-04-28 14:00:00')")
parser.add_argument("--closed",  default=None,
                    help="Closed timestamp ISO (default: sekarang)")
args = parser.parse_args()

symbol = args.symbol.upper()

trade = {
    "symbol"     : symbol,
    "direction"  : args.direction,
    "strategy"   : args.strategy,
    "quality"    : args.quality,
    "entry"      : args.entry,
    "exit_price" : args.exit_price,
    "sl"         : args.sl,
    "tp1"        : args.tp1,
    "tp2"        : args.tp2,
    "pnl_usd"    : args.pnl_usd,
    "pnl_r"      : args.pnl_r,
    "outcome"    : args.outcome,
    "bep_done"   : bool(args.bep_done),
    "opened_at"  : args.opened,
}
if args.closed:
    trade["closed_at"] = args.closed

secret = hmac.new(token.encode(), symbol.encode(), hashlib.sha256).hexdigest()
trade["secret"] = secret

web_url = os.getenv("WEB_URL", "https://cryptovision-web.vercel.app")

print(f"POST {web_url}/api/trades")
print(f"  {symbol} {args.direction} entry={args.entry} exit={args.exit_price}")
print(f"  PnL ${args.pnl_usd:+.2f} ({args.pnl_r:+.2f}R) outcome={args.outcome}")
print(f"  opened={args.opened}")

r = requests.post(f"{web_url}/api/trades", json=trade, timeout=10)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:200]}")

if r.status_code >= 400:
    print("\nFAIL — trade tidak masuk history.")
    sys.exit(1)

print("\nDONE — cek dashboard /history di web")
