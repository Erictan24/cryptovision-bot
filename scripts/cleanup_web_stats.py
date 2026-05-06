#!/usr/bin/env python3
"""Cleanup web /api/trades — hapus metode lama yang ke-POST tidak sengaja
oleh recover_dangling.py.

Context (2026-05-07):
- recover_dangling.py POST 24 trades ke web — termasuk metode lama
  (BLUR, KAT, SEI, BNB, SOL -6.28R outlier, AVAX, PAXG, SUI, AIOT,
   XPL, 1000PEPE, BCH, DOGE)
- User want stats web HANYA metode baru (12 entries, sesuai local)
- Plus PI (4-07) yang udah ke-delete via fix_history_final

Coin yang harus DELETE dari web:
  BLUR, KAT, SEI, BNB, SOL, AVAX, PAXG, SUI, AIOT,
  XPL, 1000PEPE, BCH, DOGE

Coin yang KEEP di web (12 metode baru, match local):
  BTC, ARB (x2), RENDER, BASED, UNI, LINK, OPG, OP, TIA, ALGO, PIPPIN

Usage:
    python3 scripts/cleanup_web_stats.py --dry-run
    python3 scripts/cleanup_web_stats.py
"""
import os
import sys
import time
import hmac
import hashlib
import argparse
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
ENV  = ROOT / ".env"

# Coin metode LAMA yang ada di web tapi shouldn't (residue dari recover_dangling)
SYMBOLS_TO_DELETE = [
    "BLUR", "KAT", "SEI", "BNB", "SOL", "AVAX", "PAXG", "SUI",
    "AIOT", "XPL", "1000PEPE", "BCH", "DOGE",
]

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true",
                    help="Preview tanpa apply")
args = parser.parse_args()

# Load env
env_vars = {}
for line in ENV.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env_vars[k.strip()] = v.strip().strip('"').strip("'")

token   = env_vars.get("TELEGRAM_BOT_TOKEN", "")
web_url = env_vars.get("WEB_URL", "https://cryptovision-web.vercel.app")

if not token:
    print("ERROR: TELEGRAM_BOT_TOKEN not in .env")
    sys.exit(1)

print(f"Target web: {web_url}")
print(f"Symbols to delete: {len(SYMBOLS_TO_DELETE)}")
print(f"  {SYMBOLS_TO_DELETE}")
print()

if args.dry_run:
    print("[DRY-RUN] would DELETE these from web /api/trades")
    sys.exit(0)

# Apply DELETE
total_deleted = 0
for sym in SYMBOLS_TO_DELETE:
    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
    params = {
        "symbol": sym,
        "secret": secret,
        "hours" : "10000",  # very wide window — capture all old entries
        "limit" : "5",
    }
    try:
        r = requests.delete(f"{web_url}/api/trades",
                            params=params, timeout=10)
        body = r.text[:120]
        print(f"  {sym:12s}: status={r.status_code} | {body}")
        if r.status_code < 400:
            total_deleted += 1
    except Exception as e:
        print(f"  {sym:12s}: error {e}")
    time.sleep(0.2)

print()
print(f"DELETE attempted: {len(SYMBOLS_TO_DELETE)} symbols")
print(f"Successful (status<400): {total_deleted}")
print()
print("Cek dashboard /history dan /statistics — harus tinggal 12 entries metode baru")
