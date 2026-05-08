#!/usr/bin/env python3
"""Ad-hoc cancel pending LIMIT order untuk symbol tertentu.

Use case: harga reach TP sebelum entry hit (setup invalid),
atau struktur berubah, atau user manual decision.

Workflow:
1. Find pending LIMIT order untuk symbol di Bitunix
2. Cancel via /api/v1/futures/trade/cancel_orders
3. Remove dari local active_positions.json
4. DELETE dari web /api/positions
5. Send Telegram notif

Usage:
    python3 scripts/cancel_pending_limit.py ARB
    python3 scripts/cancel_pending_limit.py ARB --reason "price reached TP2 before entry"
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
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(str(ROOT / ".env"))

from bitunix_trader import BitunixTrader

ACTIVE_POS_FILE = ROOT / "data" / "active_positions.json"
ENV  = ROOT / ".env"

parser = argparse.ArgumentParser()
parser.add_argument("symbol", help="Coin symbol (e.g. ARB)")
parser.add_argument("--reason", default="setup invalid",
                    help="Cancel reason untuk Telegram notif")
args = parser.parse_args()

symbol = args.symbol.upper().replace('USDT', '')
sym_full = symbol + 'USDT'

# Load env
env_vars = {}
for line in ENV.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env_vars[k.strip()] = v.strip().strip('"').strip("'")

token   = env_vars.get("TELEGRAM_BOT_TOKEN", "")
chat_id = env_vars.get("TELEGRAM_CHAT_ID", "")
web_url = env_vars.get("WEB_URL", "https://cryptovision-web.vercel.app")

trader = BitunixTrader()

# ── Step 1: Find pending LIMIT order ────────────────────────────
print(f"Looking for pending LIMIT order: {sym_full}")
pending = trader._get("/api/v1/futures/trade/get_pending_orders",
                      {"symbol": sym_full})
if not pending or pending.get('code') != 0:
    print(f"ERROR: get_pending_orders failed (code={pending.get('code') if pending else 'none'})")
    sys.exit(1)

orders_data = pending.get('data', {})
if isinstance(orders_data, dict):
    order_list = orders_data.get('orderList', [])
elif isinstance(orders_data, list):
    order_list = orders_data
else:
    order_list = []

# Filter: LIMIT order, not reduce-only (i.e., entry order, not TP)
limit_entries = [
    o for o in order_list
    if o.get('orderType') == 'LIMIT' and not o.get('reduceOnly')
]

if not limit_entries:
    print(f"No pending LIMIT entry order untuk {symbol}.")
    print(f"All pending orders for {sym_full}:")
    for o in order_list:
        print(f"  {o.get('orderType')} {o.get('side')} @ {o.get('price')} qty={o.get('qty')} reduceOnly={o.get('reduceOnly')}")
    sys.exit(0)

# Pick first matching
order = limit_entries[0]
order_id = order.get('orderId', '')
side = order.get('side', '')
price = order.get('price', '')
qty = order.get('qty', '')

print(f"\nFound LIMIT entry: orderId={order_id} {side} @ {price} qty={qty}")

# ── Step 2: Cancel via Bitunix ──────────────────────────────────
print(f"\nCanceling order...")
# Try with orderList format first
cancel_body = {
    "symbol": sym_full,
    "orderList": [{"orderId": str(order_id)}],
}
r = trader._post("/api/v1/futures/trade/cancel_orders", cancel_body)
if not r or r.get('code') != 0:
    # Try simple format
    r = trader._post("/api/v1/futures/trade/cancel_orders",
                     {"symbol": sym_full, "orderId": str(order_id)})

print(f"Cancel result: code={r.get('code')} msg={r.get('msg','')}")
if r.get('code') != 0:
    print("FAIL — cek error di atas")
    sys.exit(1)

# ── Step 3: Remove dari active_positions.json ───────────────────
if ACTIVE_POS_FILE.exists():
    active = json.loads(ACTIVE_POS_FILE.read_text())
    if symbol in active:
        del active[symbol]
        ACTIVE_POS_FILE.write_text(json.dumps(active, indent=2))
        print(f"\nLOCAL: removed {symbol} dari active_positions.json")
    else:
        print(f"\nLOCAL: {symbol} not in active_positions (skip)")

# ── Step 4: Cleanup web ─────────────────────────────────────────
secret = hmac.new(token.encode(), symbol.encode(), hashlib.sha256).hexdigest()
print(f"\nWEB: DELETE /api/positions {symbol}")
try:
    r = requests.delete(f"{web_url}/api/positions",
                        params={"symbol": symbol, "secret": secret},
                        timeout=10)
    print(f"  Status: {r.status_code}")
except Exception as e:
    print(f"  Error: {e}")

# Patch signal status to cancelled
try:
    r = requests.patch(f"{web_url}/api/signals",
                       json={"symbol": symbol, "secret": secret, "status": "cancelled"},
                       timeout=10)
    print(f"WEB: PATCH /api/signals status=cancelled — {r.status_code}")
except Exception as e:
    print(f"  signals PATCH error: {e}")

# ── Step 5: Telegram notif ──────────────────────────────────────
if chat_id and token:
    msg = (
        f"🚫 <b>LIMIT CANCELLED — {symbol}</b>\n"
        f"Side: {side} @ {price}\n"
        f"Reason: {args.reason}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
        print(f"\nTELEGRAM: notif sent")
    except Exception:
        pass

print(f"\nDONE — {symbol} LIMIT entry cancelled.")
