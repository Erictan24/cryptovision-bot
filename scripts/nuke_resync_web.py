#!/usr/bin/env python3
"""NUKE total: DELETE all trade symbols di web → POST exactly N entries dari local.

Garansi web match local 1:1.

Workflow:
1. List unique symbols dari local trade_history
2. Plus tambah list metode lama symbols (likely junk di web)
3. DELETE 5x per symbol (clear all duplicates)
4. POST exactly satu entry per row di local
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
TRADE_HIST_FILE = ROOT / "data" / "trade_history.json"
ENV  = ROOT / ".env"

env_vars = {}
for line in ENV.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env_vars[k.strip()] = v.strip().strip('"').strip("'")

token   = env_vars.get("TELEGRAM_BOT_TOKEN", "")
web_url = env_vars.get("WEB_URL", "https://cryptovision-web.vercel.app")

local = json.loads(TRADE_HIST_FILE.read_text())
print(f"Local trade_history: {len(local)} entries")

# Symbols dari local + likely junk yang pernah di-POST
local_syms = list(set(t.get('symbol') for t in local if t.get('symbol')))
junk_syms = [
    "BLUR", "KAT", "SEI", "BNB", "SOL", "AVAX", "PAXG", "SUI",
    "AIOT", "XPL", "1000PEPE", "BCH", "DOGE", "PI", "MKR", "FET",
    "ONDO", "JUP", "TIA", "VIRTUAL", "XLM", "VET", "CAKE", "TON",
]
all_purge = list(set(local_syms + junk_syms))

print(f"Symbols to DELETE (purge): {len(all_purge)}")
print(f"Symbols local will re-POST: {sorted(local_syms)}")
print()

# ── Step 1: PURGE web ─────────────────────────────────────────────
print("=" * 60)
print("STEP 1: NUKE web /api/trades")
print("=" * 60)

for sym in all_purge:
    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
    total_deleted = 0
    for _ in range(10):  # up to 10 calls × limit=5 = 50 entries cleared
        try:
            r = requests.delete(f"{web_url}/api/trades",
                                params={"symbol": sym, "secret": secret,
                                        "hours": "10000", "limit": "5"},
                                timeout=10)
            try:
                deleted = r.json().get('deleted', 0)
                total_deleted += deleted
                if deleted == 0:
                    break
            except Exception:
                break
        except Exception:
            break
        time.sleep(0.1)
    if total_deleted > 0:
        print(f"  {sym:12s}: deleted {total_deleted}")
print()

# ── Step 2: POST exactly N entries dari local ─────────────────────
print("=" * 60)
print(f"STEP 2: POST {len(local)} entries dari local")
print("=" * 60)

# Real PnL USD untuk 16 trades
PNL_USD_MAP = {
    ("BTC",    "2026-04-25"): 0.3197,
    ("PIPPIN", "2026-04-27"): 0.4415,
    ("ARB",    "2026-04-29"): -0.2384,
    ("RENDER", "2026-04-30"): -0.2380,
    ("BASED",  "2026-04-30"): 0.4433,
    ("ARB",    "2026-05-03"): -0.2686,
    ("UNI",    "2026-05-03"): 0.1174,
    ("LINK",   "2026-05-03"): 0.4166,
    ("OPG",    "2026-05-03"): 0.1457,
    ("OP",     "2026-05-03"): -0.2531,
    ("TIA",    "2026-05-03"): 0.1082,
    ("ALGO",   "2026-05-04"): 0.4102,
    ("IO",     "2026-05-06"): -0.2628,
    ("LDO",    "2026-05-04"): 0.3385,
    ("VANA",   "2026-04-28"): 0.4015,
    ("IP",     "2026-05-09"): 0.2349,
}

posted = 0
for h in local:
    sym = h.get('symbol')
    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
    status = h.get('status', '')

    if status == 'TP2_HIT':
        outcome, exit_p = "PROFIT", h.get('tp2', h.get('entry'))
    elif status == 'TP1_HIT':
        outcome, exit_p = "PROFIT", h.get('tp1', h.get('entry'))
    elif status == 'BEP':
        outcome, exit_p = "BEP", h.get('entry')
    else:
        outcome, exit_p = "LOSS", h.get('sl', h.get('entry'))

    # Match opened_at date prefix untuk PNL_USD_MAP
    opened_date = str(h.get('timestamp', ''))[:10]
    pnl_usd = PNL_USD_MAP.get((sym, opened_date), 0)

    body = {
        "symbol"     : sym,
        "direction"  : h.get('direction', 'LONG'),
        "strategy"   : "swing",
        "quality"    : h.get('quality', 'GOOD'),
        "entry"      : h.get('entry'),
        "exit_price" : exit_p,
        "sl"         : h.get('sl'),
        "tp1"        : h.get('tp1'),
        "tp2"        : h.get('tp2'),
        "pnl_usd"    : round(pnl_usd, 4),
        "pnl_r"      : h.get('result_pnl', 0),
        "outcome"    : outcome,
        "bep_done"   : status in ("TP1_HIT", "TP2_HIT"),
        "opened_at"  : h.get('timestamp'),
        "closed_at"  : h.get('closed_at'),
        "secret"     : secret,
    }
    try:
        r = requests.post(f"{web_url}/api/trades", json=body, timeout=10)
        ok = r.status_code < 400
        marker = "OK" if ok else f"FAIL {r.status_code}"
        print(f"  {sym:8s} {status:8s} ${pnl_usd:+.4f}  {marker}")
        if ok:
            posted += 1
    except Exception as e:
        print(f"  {sym:8s} error: {e}")
    time.sleep(0.2)

print(f"\nDONE — posted {posted}/{len(local)}")
print(f"Refresh dashboard. Harus tinggal {len(local)} entries (cocok local).")
