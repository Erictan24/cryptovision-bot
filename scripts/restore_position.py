#!/usr/bin/env python3
"""Re-push posisi ke web yang sudah ke-delete bug race condition.

Read dari data/active_positions.json, POST ke /api/positions.
Optional: --tp1-hit untuk mark TP1 sudah kena (BEP active, sl=entry).

Usage:
  python3 scripts/restore_position.py PIPPIN --tp1-hit
  python3 scripts/restore_position.py BTC

Field yang dipush:
  symbol, direction, strategy, quality, entry, sl, tp1, tp2,
  rr, qty, leverage, opened_at
"""
import os
import sys
import json
import hmac
import hashlib
import argparse
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
ENV  = ROOT / ".env"
POS_FILE = ROOT / "data" / "active_positions.json"

if not ENV.exists():
    print(f"ERROR: {ENV} not found")
    sys.exit(1)

# Load TELEGRAM_BOT_TOKEN dari .env
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
parser.add_argument("--strategy", default="swing",
                    choices=["swing", "scalp"],
                    help="Strategy tag (default: swing)")
parser.add_argument("--quality", default="GOOD",
                    help="Signal quality (default: GOOD)")
parser.add_argument("--tp1-hit", action="store_true",
                    help="Mark TP1 already hit, BEP active, SL=entry")
args = parser.parse_args()

symbol = args.symbol.upper()

if not POS_FILE.exists():
    print(f"ERROR: {POS_FILE} not found")
    sys.exit(1)

data = json.loads(POS_FILE.read_text())
if symbol not in data:
    print(f"ERROR: {symbol} not in {POS_FILE}")
    print(f"Available: {list(data.keys())}")
    sys.exit(1)

pos = data[symbol]
entry = float(pos.get("entry", 0))
sl    = float(pos.get("sl", 0))
tp1   = float(pos.get("tp1", 0))
tp2   = float(pos.get("tp2", 0))

# Hitung RR (TP2 vs SL)
risk_dist = abs(entry - sl)
reward2   = abs(tp2 - entry)
rr        = round(reward2 / risk_dist, 2) if risk_dist > 0 else 0

body = {
    "symbol"   : symbol,
    "direction": pos.get("direction", ""),
    "strategy" : args.strategy,
    "quality"  : args.quality,
    "entry"    : entry,
    "sl"       : sl,
    "tp1"      : tp1,
    "tp2"      : tp2,
    "rr"       : rr,
    "qty"      : float(pos.get("qty", 0)),
    "leverage" : int(pos.get("leverage", 10)),
    "reasons"  : ["Restored manual via restore_position.py"],
}

secret = hmac.new(token.encode(), symbol.encode(), hashlib.sha256).hexdigest()
body["secret"] = secret

web_url = os.getenv("WEB_URL", "https://cryptovision-web.vercel.app")

print(f"POST {web_url}/api/positions")
print(f"  {symbol} {body['direction']} qty={body['qty']} entry={entry}")
print(f"  SL={sl} TP1={tp1} TP2={tp2} RR={rr}")

r = requests.post(f"{web_url}/api/positions", json=body, timeout=10)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:200]}")

if r.status_code >= 400:
    print("\nFAIL — posisi tidak terestore. Cek error di atas.")
    sys.exit(1)

# Kalau --tp1-hit, PATCH tp1_hit=true + bep_active=true + sl=entry
if args.tp1_hit:
    print("\nPATCH tp1_hit=true bep_active=true sl=entry (BEP)")
    patch_body = {
        "symbol"     : symbol,
        "secret"     : secret,
        "tp1_hit"    : True,
        "bep_active" : True,
        "sl"         : entry,
    }
    r2 = requests.patch(f"{web_url}/api/positions",
                        json=patch_body, timeout=10)
    print(f"PATCH status: {r2.status_code}")
    print(f"Response: {r2.text[:200]}")

print("\nDONE — cek dashboard /positions di web")
