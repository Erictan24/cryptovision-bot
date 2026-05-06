#!/usr/bin/env python3
"""Recovery IO setelah limit entry monitor stop akibat API error 2026-05-06.

Context:
- Bot place LIMIT order IO 17:25:49 (entry 0.16962, qty 11.7, leverage 20x)
- Limit entry monitor stop 18:41:51 dengan reason "tidak lagi pending"
- Penyebab: API/network error 1x → get_open_position return None +
  get_pending_orders return empty → bot anggap LIMIT cancelled
- Realita: LIMIT FILLED (entry kena di Bitunix), tapi:
  * TP1 tidak di-place
  * Notif Telegram silent
  * Web dashboard masih show LIMIT
  * Risk: kalau harga turun ke SL 0.14832 = lose full risk tanpa TP

Script ini fix state:
1. Verify IO position truly filled di Bitunix
2. Place TP1 LIMIT reduce-only @ tp1
3. Push position to web (LIMIT → filled state)
4. Send Telegram notif retroactive
5. Bot restart akan auto-resume TP1 monitor

Usage di VPS:
    cd /home/eric/cryptovision-bot
    python3 scripts/recover_io.py
"""
import os
import sys
import json
import time
import hmac
import hashlib
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(str(ROOT / ".env"))

from bitunix_trader import BitunixTrader

ENV  = ROOT / ".env"
ACTIVE_POS_FILE = ROOT / "data" / "active_positions.json"

SYMBOL = "IO"
SYM_FULL = "IOUSDT"

# ── Load env ──────────────────────────────────────────────────────────
env_vars = {}
for line in ENV.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env_vars[k.strip()] = v.strip().strip('"').strip("'")

token   = env_vars.get("TELEGRAM_BOT_TOKEN", "")
chat_id = env_vars.get("TELEGRAM_CHAT_ID", "")
web_url = env_vars.get("WEB_URL", "https://cryptovision-web.vercel.app")

if not token:
    print("ERROR: TELEGRAM_BOT_TOKEN not found")
    sys.exit(1)

# ── Load state IO ─────────────────────────────────────────────────────
active = json.loads(ACTIVE_POS_FILE.read_text())
if SYMBOL not in active:
    print(f"ERROR: {SYMBOL} not in active_positions.json")
    sys.exit(1)

pos_state = active[SYMBOL]
direction = pos_state.get("direction", "LONG")
entry     = float(pos_state.get("entry", 0))
sl        = float(pos_state.get("sl", 0))
tp1       = float(pos_state.get("tp1", 0))
tp2       = float(pos_state.get("tp2", 0))
qty       = float(pos_state.get("qty", 0))
leverage  = int(pos_state.get("leverage", 10))
quality   = pos_state.get("quality", "GOOD")
strategy  = pos_state.get("_strategy", "swing")

print("=" * 60)
print(f"IO Recovery — verify state")
print("=" * 60)
print(f"Direction : {direction}")
print(f"Entry     : {entry}")
print(f"SL        : {sl}")
print(f"TP1       : {tp1}")
print(f"TP2       : {tp2}")
print(f"Qty       : {qty}")
print(f"Leverage  : {leverage}x")
print(f"Quality   : {quality}")

# ── Verify Bitunix position ───────────────────────────────────────────
trader = BitunixTrader()
bitunix_pos = trader.get_open_position(SYMBOL)

if not bitunix_pos:
    print(f"\nERROR: IO position NOT FOUND di Bitunix.")
    print("Possible: position udah closed, atau API error sekarang.")
    print("Cek manual di Bitunix UI dulu.")
    sys.exit(1)

actual_qty = float(bitunix_pos.get('qty', 0))
actual_entry = float(bitunix_pos.get('avgOpenPrice', entry))
actual_side = bitunix_pos.get('side', '')

print(f"\nBitunix actual position:")
print(f"  qty       : {actual_qty}")
print(f"  side      : {actual_side}")
print(f"  avgEntry  : {actual_entry}")
print(f"  positionId: {bitunix_pos.get('positionId', '')}")

# ── Cek apakah TP1 sudah ada di pending orders ────────────────────────
pending = trader._get("/api/v1/futures/trade/get_pending_orders", {"symbol": SYM_FULL})
if pending.get('code') == 0:
    orders_data = pending.get('data', {})
    if isinstance(orders_data, dict):
        order_list = orders_data.get('orderList', [])
    else:
        order_list = orders_data if isinstance(orders_data, list) else []

    print(f"\nPending orders untuk {SYM_FULL}: {len(order_list)}")
    for o in order_list:
        print(f"  orderId={o.get('orderId')} side={o.get('side')} "
              f"price={o.get('price')} qty={o.get('qty')} "
              f"reduceOnly={o.get('reduceOnly')} type={o.get('orderType')}")

    # Check kalau TP1 sudah ada (LIMIT reduce-only di harga TP1)
    tp1_already_placed = False
    for o in order_list:
        try:
            if (o.get('reduceOnly') and
                o.get('orderType') == 'LIMIT' and
                abs(float(o.get('price', 0)) - tp1) / tp1 < 0.01):
                tp1_already_placed = True
                print(f"  ⚠️ TP1 sudah ada (orderId={o.get('orderId')}) — skip place TP1")
                break
        except (ValueError, ZeroDivisionError):
            continue
else:
    print(f"\nWARN: cannot check pending orders (code={pending.get('code')})")
    tp1_already_placed = False

# ── Place TP1 reduce-only kalau belum ─────────────────────────────────
qty_tp1 = round(actual_qty / 2, 2)  # 50% qty for TP1 partial close
close_side = "SELL" if direction == "LONG" else "BUY"

if not tp1_already_placed:
    print(f"\nPlacing TP1 LIMIT reduce-only @ {tp1} qty={qty_tp1}")
    tp1_body = {
        "symbol"     : SYM_FULL,
        "side"       : close_side,
        "tradeSide"  : "CLOSE",
        "orderType"  : "LIMIT",
        "price"      : str(round(tp1, 8)),
        "qty"        : str(qty_tp1),
        "positionId" : str(bitunix_pos.get('positionId', '')),
        "reduceOnly" : True,
        "effect"     : "GTC",
        "clientId"   : f"tp1_recover_{int(time.time())}",
    }
    r = trader._post("/api/v1/futures/trade/place_order", tp1_body)
    if r.get('code') == 0:
        tp1_order_id = r.get('data', {}).get('orderId', '')
        print(f"  ✅ TP1 placed orderId={tp1_order_id}")
    else:
        print(f"  ❌ FAIL place TP1: {r.get('msg', r)}")
        sys.exit(1)
else:
    print(f"\nTP1 sudah terpasang — skip")

# ── Push position to web (LIMIT → filled state) ───────────────────────
secret = hmac.new(token.encode(), SYMBOL.encode(), hashlib.sha256).hexdigest()

position_body = {
    "symbol"    : SYMBOL,
    "direction" : direction,
    "strategy"  : strategy,
    "quality"   : quality,
    "entry"     : actual_entry,
    "sl"        : sl,
    "tp1"       : tp1,
    "tp2"       : tp2,
    "rr"        : pos_state.get("rr", 2.0),
    "qty"       : actual_qty,
    "leverage"  : leverage,
    "reasons"   : (pos_state.get("reasons") or [])[:6],
    "secret"    : secret,
}

print(f"\nWEB: POST {web_url}/api/positions (recover state LIMIT→filled)")
try:
    r = requests.post(f"{web_url}/api/positions", json=position_body, timeout=10)
    print(f"     Status: {r.status_code} | Response: {r.text[:150]}")
except Exception as e:
    print(f"     ERROR: {e}")

# ── Patch signal status ke filled ─────────────────────────────────────
patch_body = {"symbol": SYMBOL, "secret": secret, "status": "filled"}
print(f"\nWEB: PATCH {web_url}/api/signals (status=filled)")
try:
    r = requests.patch(f"{web_url}/api/signals", json=patch_body, timeout=10)
    print(f"     Status: {r.status_code} | Response: {r.text[:150]}")
except Exception as e:
    print(f"     ERROR: {e}")

# ── Send Telegram notif retroactive ───────────────────────────────────
if chat_id:
    ico = "🟢" if direction == "LONG" else "🔴"
    msg = (
        f"✅ <b>LIMIT ENTRY KENA</b> (recovery)\n"
        f"{'=' * 28}\n"
        f"{ico} <b>{SYMBOL} {direction}</b>\n"
        f"Entry  : {actual_entry}\n"
        f"TP1    : {tp1}\n"
        f"TP2    : {tp2}\n"
        f"SL     : {sl}\n\n"
        f"⚠️ Notif terlambat — bot limit monitor stop "
        f"akibat API error 18:41 WIB.\n"
        f"TP1 sudah di-place sekarang. Restart bot untuk resume TP1 monitor."
    )
    print(f"\nTELEGRAM: send notif")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        print(f"     Status: {r.status_code}")
    except Exception as e:
        print(f"     ERROR: {e}")

print("\n" + "=" * 60)
print("Recovery IO selesai.")
print("Next: restart bot supaya TP1 monitor resume otomatis dari")
print("      active_positions.json. Bot akan track TP1/SL trail.")
print("=" * 60)
