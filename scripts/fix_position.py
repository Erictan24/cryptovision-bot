#!/usr/bin/env python3
"""Manual update field posisi di web (untuk fix legacy positions yang ke-push tanpa rr/leverage).

Usage:
  python3 scripts/fix_position.py PIPPIN leverage=20 rr=2.7
  python3 scripts/fix_position.py BTC sl=99000
"""
import os
import sys
import hmac
import hashlib
from pathlib import Path

env_path = Path(__file__).parent.parent / ".env"
if not env_path.exists():
    print(f"ERROR: {env_path} not found")
    sys.exit(1)

token = ""
for line in env_path.read_text().splitlines():
    if line.startswith("TELEGRAM_BOT_TOKEN="):
        token = line.split("=", 1)[1].strip().strip('"').strip("'")
        break

if not token:
    print("ERROR: TELEGRAM_BOT_TOKEN not found in .env")
    sys.exit(1)

if len(sys.argv) < 2:
    print("Usage: python3 fix_position.py <SYMBOL> [leverage=N] [rr=X.X] [sl=X.X]")
    print("Example: python3 fix_position.py PIPPIN leverage=20 rr=2.7")
    sys.exit(1)

import requests

symbol = sys.argv[1].upper()
opts = {"symbol": symbol}

for arg in sys.argv[2:]:
    if "=" not in arg:
        continue
    k, v = arg.split("=", 1)
    try:
        v = float(v) if "." in v else int(v)
    except ValueError:
        pass
    opts[k] = v

secret = hmac.new(token.encode(), symbol.encode(), hashlib.sha256).hexdigest()
opts["secret"] = secret

web_url = os.getenv("WEB_URL", "https://cryptovision-web.vercel.app")
print(f"PATCH {web_url}/api/positions")
print(f"Body: {opts}")

r = requests.patch(f"{web_url}/api/positions", json=opts, timeout=10)
print(f"Status: {r.status_code}")
print(f"Response: {r.text}")
