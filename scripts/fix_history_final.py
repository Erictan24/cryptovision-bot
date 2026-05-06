#!/usr/bin/env python3
"""Final fix trade_history: hapus PI lama + add ARB #2 missed.

Setelah audit Bitunix:
- 19 closed positions di Bitunix
- 5 metode LAMA (pre-04-25): BTC #2 #3, UNI #2, LINK #2 #3 → tidak masuk
- 2 partial close (LDO/VANA TP1 partial): bukan closed trade
- 12 metode BARU yang valid

Local punya 12 = 11 metode baru + 1 PI metode lama. Plus ARB #2 04-29 missed.

Action:
1. Hapus PI (id-29ish, opened 2026-04-07)
2. Tambah ARB #2 (04-29 BUY -0.24 USD, SL hit)
3. Result: 12 metode baru closed entries

Usage:
    python3 scripts/fix_history_final.py [--dry-run]
"""
import os
import sys
import json
import time
import hmac
import hashlib
import argparse
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(str(ROOT / ".env"))

from bitunix_trader import BitunixTrader

TRADE_HIST_FILE = ROOT / "data" / "trade_history.json"
ENV  = ROOT / ".env"

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

# Backup
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
if not args.dry_run:
    backup = TRADE_HIST_FILE.with_suffix(f".json.bak_finalfix_{ts}")
    backup.write_bytes(TRADE_HIST_FILE.read_bytes())
    print(f"Backup: {backup.name}\n")

history = json.loads(TRADE_HIST_FILE.read_text())
print(f"Before: {len(history)} entries")

# ── 1. REMOVE PI (metode lama) ─────────────────────────────────────
pi_entries = [t for t in history if t.get('symbol') == 'PI']
if pi_entries:
    print(f"\nREMOVE: {len(pi_entries)} PI entries")
    for t in pi_entries:
        print(f"  PI {t.get('status')} pnl={t.get('result_pnl')}R opened={t.get('timestamp')}")
    history = [t for t in history if t.get('symbol') != 'PI']

# ── 2. FETCH ARB #2 dari Bitunix history ───────────────────────────
trader = BitunixTrader()
data = trader._get('/api/v1/futures/position/get_history_positions',
                   {'symbol': 'ARBUSDT', 'limit': 5})
arb_records = []
if data and data.get('code') == 0:
    result = data.get('data', {})
    if isinstance(result, dict):
        arb_records = result.get('positionList') or result.get('list') or []
    elif isinstance(result, list):
        arb_records = result

# Find the 04-29 record (older, not 05-02)
arb_29 = None
for p in arb_records:
    ctime_ms = int(p.get('ctime', 0) or 0)
    if not ctime_ms:
        continue
    opened_dt = datetime.fromtimestamp(ctime_ms / 1000)
    if opened_dt.month == 4 and opened_dt.day == 29:
        arb_29 = p
        break

if not arb_29:
    print("\nWARN: ARB #2 (04-29) not found in Bitunix history. Skip add.")
else:
    pnl_usd = float(arb_29.get('realizedPNL', 0) or 0)
    ctime_ms = int(arb_29.get('ctime', 0) or 0)
    mtime_ms = int(arb_29.get('mtime', 0) or 0)
    side = arb_29.get('side', 'BUY')
    direction = 'LONG' if side == 'BUY' else 'SHORT'
    avg_entry = float(arb_29.get('avgOpenPrice', 0) or 0)
    avg_close = float(arb_29.get('avgClosePrice', 0) or arb_29.get('avgClosedPrice', 0) or 0)
    qty = float(arb_29.get('qty', 0) or 0)

    opened_at = datetime.fromtimestamp(ctime_ms / 1000).strftime('%Y-%m-%d %H:%M:%S') if ctime_ms else ''
    closed_at = datetime.fromtimestamp(mtime_ms / 1000).strftime('%Y-%m-%d %H:%M:%S') if mtime_ms else ''

    # Estimate sl/tp1/tp2 (we don't have signal data) — use avg ± reasonable
    # For -0.24 PnL on ARB, this was SL hit. Use avg_close as exit_price.
    if direction == 'LONG':
        sl_est = avg_entry * 0.985
        tp1_est = avg_entry * 1.015
        tp2_est = avg_entry * 1.025
    else:
        sl_est = avg_entry * 1.015
        tp1_est = avg_entry * 0.985
        tp2_est = avg_entry * 0.975

    # Determine outcome from PnL sign
    if pnl_usd > 0:
        status = "TP1_HIT" if pnl_usd < 0.5 else "TP2_HIT"
        outcome = "PROFIT"
    elif pnl_usd < -0.05:
        status = "SL_HIT"
        outcome = "LOSS"
    else:
        status = "BEP"
        outcome = "BEP"

    # pnl_r = realized / risk_initial. Without signal sl_initial, estimate from
    # standard 1R structure: if SL ~1.5% of entry, risk_amount = qty * 0.015 * entry
    risk_amount_est = qty * abs(avg_entry - sl_est) if (qty > 0 and avg_entry > 0) else max(abs(pnl_usd), 0.001)
    pnl_r = round(pnl_usd / risk_amount_est, 2) if risk_amount_est > 0 else 0.0
    # Sanity clamp -3 .. +3
    pnl_r = max(-3.0, min(3.0, pnl_r))

    next_id = max((t.get('id', 0) for t in history), default=0) + 1
    new_entry = {
        "id"          : next_id,
        "symbol"      : "ARB",
        "direction"   : direction,
        "quality"     : "GOOD",
        "entry"       : avg_entry,
        "sl"          : sl_est,
        "tp1"         : tp1_est,
        "tp2"         : tp2_est,
        "confluence"  : 0,
        "rr1"         : 1.0,
        "rr2"         : 2.0,
        "timestamp"   : opened_at,
        "status"      : status,
        "result_pnl"  : pnl_r,
        "closed_at"   : closed_at,
    }
    history.append(new_entry)
    print(f"\nADD: ARB #2 {direction} {status} pnl=${pnl_usd:+.4f} ({pnl_r:+.2f}R) "
          f"opened={opened_at} closed={closed_at}")

# Sort by timestamp ascending
history.sort(key=lambda t: t.get('timestamp', ''))

print(f"\nAfter: {len(history)} entries\n")
for t in history:
    print(f"  {t.get('symbol'):8s} {t.get('status'):8s} pnl={t.get('result_pnl'):+.2f}R | opened={str(t.get('timestamp',''))[:19]}")

if args.dry_run:
    print("\n[DRY-RUN] no changes applied")
    sys.exit(0)

# ── Save local ──────────────────────────────────────────────────────
TRADE_HIST_FILE.write_text(json.dumps(history, indent=2))
print(f"\nLocal saved: {len(history)} entries")

# ── DELETE PI dari web ──────────────────────────────────────────────
if pi_entries and token:
    secret = hmac.new(token.encode(), b'PI', hashlib.sha256).hexdigest()
    try:
        r = requests.delete(f"{web_url}/api/trades",
                            params={"symbol": "PI", "secret": secret,
                                    "hours": "10000", "limit": "5"},
                            timeout=10)
        print(f"WEB DELETE PI: status={r.status_code}")
    except Exception as e:
        print(f"WEB DELETE PI error: {e}")

# ── POST ARB #2 ke web ──────────────────────────────────────────────
if arb_29 and token:
    secret = hmac.new(token.encode(), b'ARB', hashlib.sha256).hexdigest()
    body = {
        "symbol"     : "ARB",
        "direction"  : new_entry['direction'],
        "strategy"   : "swing",
        "quality"    : "GOOD",
        "entry"      : new_entry['entry'],
        "exit_price" : avg_close or new_entry['sl'],
        "sl"         : new_entry['sl'],
        "tp1"        : new_entry['tp1'],
        "tp2"        : new_entry['tp2'],
        "pnl_usd"    : round(pnl_usd, 4),
        "pnl_r"      : new_entry['result_pnl'],
        "outcome"    : outcome,
        "bep_done"   : status in ('TP1_HIT', 'TP2_HIT'),
        "opened_at"  : new_entry['timestamp'],
        "closed_at"  : new_entry['closed_at'],
        "secret"     : secret,
    }
    try:
        r = requests.post(f"{web_url}/api/trades", json=body, timeout=10)
        print(f"WEB POST ARB #2: status={r.status_code}")
    except Exception as e:
        print(f"WEB POST error: {e}")

print("\nDONE")
