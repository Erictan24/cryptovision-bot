#!/usr/bin/env python3
"""Recovery PIPPIN setelah bot monitor stop akibat DNS error 2026-05-06.

Context:
- Bot monitor PIPPINUSDT stop di 04:58:45 karena 3x recheck Bitunix gagal
  (DNS error 'fapi.bitunix.com' temporary failure).
- PIPPIN actually SUDAH closed di Bitunix via TP2 limit order yang di-place
  dari awal (exchange-side execution).
- Tapi bot tidak update state: active_positions.json masih show open,
  trade_history.json missing entry, web dashboard show open, no Telegram notif.

Script ini sync state lokal + web + kirim notif Telegram retroactive.

Usage di VPS:
    cd /home/eric/cryptovision-bot
    python3 scripts/recover_pippin.py
"""
import os
import sys
import json
import hmac
import hashlib
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
ENV  = ROOT / ".env"
ACTIVE_POS_FILE = ROOT / "data" / "active_positions.json"
TRADE_HIST_FILE = ROOT / "data" / "trade_history.json"

SYMBOL = "PIPPIN"

# ── Load env ──────────────────────────────────────────────────────────
if not ENV.exists():
    print(f"ERROR: {ENV} not found")
    sys.exit(1)

env_vars = {}
for line in ENV.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env_vars[k.strip()] = v.strip().strip('"').strip("'")

token   = env_vars.get("TELEGRAM_BOT_TOKEN", "")
chat_id = env_vars.get("TELEGRAM_CHAT_ID", "")
web_url = env_vars.get("WEB_URL", "https://cryptovision-web.vercel.app")

if not token:
    print("ERROR: TELEGRAM_BOT_TOKEN not in .env")
    sys.exit(1)

# ── Backup state files ────────────────────────────────────────────────
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
for f in [ACTIVE_POS_FILE, TRADE_HIST_FILE]:
    if f.exists():
        backup = f.with_suffix(f"{f.suffix}.bak_{ts}")
        backup.write_bytes(f.read_bytes())
        print(f"Backup: {backup.name}")

# ── Load active_positions.json ────────────────────────────────────────
active = json.loads(ACTIVE_POS_FILE.read_text())
if SYMBOL not in active:
    print(f"ERROR: {SYMBOL} not in {ACTIVE_POS_FILE}")
    print(f"Available: {list(active.keys())}")
    sys.exit(1)

pos = active[SYMBOL]
direction = pos.get("direction", "SHORT")
entry     = float(pos.get("entry", 0))
tp1       = float(pos.get("tp1", 0))
tp2       = float(pos.get("tp2", 0))
qty       = float(pos.get("qty", 0))
leverage  = int(pos.get("leverage", 10))
opened_at = pos.get("opened_at", "")

# Original SL untuk hitung R-multiple. Untuk SHORT, SL > entry, dan TP1 = entry - X.
# Logic bot: original_sl - entry = entry - tp1 (mirror). Verify:
# Kalau pos['sl'] sudah BEP-shifted (= entry), pakai tp1 sebagai mirror.
sl_now = float(pos.get("sl", 0))
if direction == "SHORT":
    original_sl = entry + (entry - tp1) if sl_now == entry or sl_now == tp1 else sl_now
    risk_per_unit = original_sl - entry  # positive
    r_at_tp2 = (entry - tp2) / risk_per_unit if risk_per_unit > 0 else 0
else:  # LONG
    original_sl = entry - (tp1 - entry) if sl_now == entry or sl_now == tp1 else sl_now
    risk_per_unit = entry - original_sl  # positive
    r_at_tp2 = (tp2 - entry) / risk_per_unit if risk_per_unit > 0 else 0

# Avg R untuk partial close: 50% qty hit TP1 (+1R), 50% hit TP2 (+r_at_tp2)
avg_r = round(0.5 * 1.0 + 0.5 * r_at_tp2, 2)

# Hitung PnL USD: risk_amount per trade × R
# qty bot pakai initial qty (sebelum TP1 partial). Risk USD = (entry - SL) × qty
risk_usd = abs(entry - original_sl) * qty
pnl_usd  = round(avg_r * risk_usd, 2)

# Exit price = TP2 untuk avg
exit_price = tp2

closed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

print("=" * 60)
print(f"PIPPIN Recovery")
print("=" * 60)
print(f"Direction    : {direction}")
print(f"Entry        : {entry}")
print(f"Original SL  : {original_sl}")
print(f"TP1          : {tp1}")
print(f"TP2          : {tp2}")
print(f"Qty          : {qty}")
print(f"Leverage     : {leverage}x")
print(f"Opened       : {opened_at}")
print(f"Closed       : {closed_at}")
print(f"Risk USD     : ${risk_usd:.4f}")
print(f"R at TP2     : {r_at_tp2:.2f}R (full)")
print(f"Avg R        : {avg_r:+.2f}R (partial TP1+TP2)")
print(f"Est. PnL USD : ${pnl_usd:+.2f}")
print("=" * 60)

# ── Append ke trade_history.json ──────────────────────────────────────
history = []
if TRADE_HIST_FILE.exists():
    history = json.loads(TRADE_HIST_FILE.read_text())

next_id = max((t.get("id", 0) for t in history), default=0) + 1

closed_entry = {
    "id"          : next_id,
    "symbol"      : SYMBOL,
    "direction"   : direction,
    "quality"     : pos.get("quality", "GOOD"),
    "entry"       : entry,
    "sl"          : original_sl,
    "tp1"         : tp1,
    "tp2"         : tp2,
    "confluence"  : pos.get("confluence", 0),
    "rr1"         : 1.0,
    "rr2"         : round(r_at_tp2, 2),
    "timestamp"   : opened_at,
    "status"      : "TP2_HIT",
    "result_pnl"  : avg_r,
    "closed_at"   : closed_at,
}
history.append(closed_entry)
TRADE_HIST_FILE.write_text(json.dumps(history, indent=2))
print(f"\nLOCAL: Added to trade_history.json (id={next_id})")

# ── Remove dari active_positions.json ─────────────────────────────────
del active[SYMBOL]
ACTIVE_POS_FILE.write_text(json.dumps(active, indent=2))
print(f"LOCAL: Removed PIPPIN from active_positions.json")
print(f"       Remaining: {list(active.keys())}")

# ── Push trade closed ke web ──────────────────────────────────────────
secret = hmac.new(token.encode(), SYMBOL.encode(), hashlib.sha256).hexdigest()

trade_body = {
    "symbol"     : SYMBOL,
    "direction"  : direction,
    "strategy"   : "swing",
    "quality"    : pos.get("quality", "GOOD"),
    "entry"      : entry,
    "exit_price" : exit_price,
    "sl"         : original_sl,
    "tp1"        : tp1,
    "tp2"        : tp2,
    "pnl_usd"    : pnl_usd,
    "pnl_r"      : avg_r,
    "outcome"    : "TP2",
    "bep_done"   : True,
    "opened_at"  : opened_at,
    "closed_at"  : closed_at,
    "secret"     : secret,
}

print(f"\nWEB: POST {web_url}/api/trades")
try:
    r = requests.post(f"{web_url}/api/trades", json=trade_body, timeout=10)
    print(f"     Status: {r.status_code} | Response: {r.text[:150]}")
except Exception as e:
    print(f"     ERROR: {e}")

# ── Delete position dari web ──────────────────────────────────────────
print(f"\nWEB: DELETE {web_url}/api/positions?symbol={SYMBOL}")
try:
    r = requests.delete(
        f"{web_url}/api/positions",
        params={"symbol": SYMBOL, "secret": secret},
        timeout=10,
    )
    print(f"     Status: {r.status_code} | Response: {r.text[:150]}")
except Exception as e:
    print(f"     ERROR: {e}")

# ── Send Telegram notif retroactive ───────────────────────────────────
if chat_id:
    msg = (
        f"✅ <b>{SYMBOL} {direction} TP2 HIT</b> (recovery)\n\n"
        f"Entry  : {entry:g}\n"
        f"TP2    : {tp2:g}\n"
        f"PnL    : <b>{avg_r:+.2f}R</b> (~${pnl_usd:+.2f})\n"
        f"Opened : {opened_at}\n"
        f"Closed : {closed_at} (recovered)\n\n"
        f"⚠️ Notif terlambat — bot monitor stop di 04:58 akibat DNS error VPS.\n"
        f"Posisi sudah closed via TP2 limit order Bitunix dari awal."
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
print("Recovery selesai.")
print("Cek dashboard /history dan /positions di web.")
print("=" * 60)
