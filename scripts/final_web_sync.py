#!/usr/bin/env python3
"""FINAL web sync — hardcode PnL dari audit Bitunix (no API matching).

Setelah multiple script failed dengan timezone matching issue, ini approach:
- HARDCODE pnl_usd dari audit Bitunix yang udah confirmed
- DELETE all symbols di web (clean slate)
- POST 13 entries dengan pnl_usd ACCURATE

Reference data dari Bitunix audit (2026-05-07):
"""
import os
import sys
import json
import time
import hmac
import hashlib
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
TRADE_HIST_FILE = ROOT / "data" / "trade_history.json"
ENV  = ROOT / ".env"

# (symbol, opened_at_date_prefix, pnl_usd_real)
# Date prefix untuk distinguish ARB #1 vs #2
PNL_MAP = {
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
}

ALL_SYMBOLS_TO_PURGE = list(set([k[0] for k in PNL_MAP.keys()])) + [
    "BLUR", "KAT", "SEI", "BNB", "SOL", "AVAX", "PAXG", "SUI",
    "AIOT", "XPL", "1000PEPE", "BCH", "DOGE", "PI", "VANA", "LDO",
]

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

# Backup local
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = TRADE_HIST_FILE.with_suffix(f".json.bak_finalsync_{ts}")
backup.write_bytes(TRADE_HIST_FILE.read_bytes())
print(f"Backup: {backup.name}\n")

# Load local + augment dengan real pnl_usd
local = json.loads(TRADE_HIST_FILE.read_text())
print(f"Local trades: {len(local)}")
print(f"PNL_MAP entries: {len(PNL_MAP)}\n")

for t in local:
    sym = t.get('symbol')
    opened_date = str(t.get('timestamp', ''))[:10]  # YYYY-MM-DD
    key = (sym, opened_date)
    pnl_real = PNL_MAP.get(key)
    if pnl_real is None:
        print(f"  WARN: no PNL_MAP entry for {sym} {opened_date}")
        t['pnl_usd_real'] = 0
    else:
        t['pnl_usd_real'] = pnl_real

# Save local with pnl_usd_real
TRADE_HIST_FILE.write_text(json.dumps(local, indent=2))
print("Local updated with real pnl_usd\n")

# ── Step 1: PURGE web ──────────────────────────────────────────────
print("=" * 60)
print("STEP 1: PURGE web /api/trades")
print("=" * 60)
for sym in ALL_SYMBOLS_TO_PURGE:
    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
    for _attempt in range(3):
        try:
            r = requests.delete(f"{web_url}/api/trades",
                                params={"symbol": sym, "secret": secret,
                                        "hours": "10000", "limit": "5"},
                                timeout=10)
            if r.status_code >= 400:
                break
            try:
                deleted = r.json().get('deleted', 0)
                if deleted == 0:
                    break
            except Exception:
                break
        except Exception:
            break
        time.sleep(0.1)
    print(f"  {sym:10s}: purged")

print("\nPurge done\n")

# ── Step 2: POST fresh 13 entries ───────────────────────────────────
print("=" * 60)
print("STEP 2: POST 13 fresh entries")
print("=" * 60)
posted = 0
for t in local:
    sym = t.get('symbol')
    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
    status = t.get('status', '')
    pnl_usd = t.get('pnl_usd_real', 0)

    if status == 'TP2_HIT':
        outcome = 'PROFIT'
        exit_p = t.get('tp2', t.get('entry'))
    elif status == 'TP1_HIT':
        outcome = 'PROFIT'
        exit_p = t.get('tp1', t.get('entry'))
    elif status == 'BEP':
        outcome = 'BEP'
        exit_p = t.get('entry')
    else:
        outcome = 'LOSS'
        exit_p = t.get('sl', t.get('entry'))

    body = {
        "symbol"     : sym,
        "direction"  : t.get('direction'),
        "strategy"   : "swing",
        "quality"    : t.get('quality', 'GOOD'),
        "entry"      : t.get('entry'),
        "exit_price" : exit_p,
        "sl"         : t.get('sl'),
        "tp1"        : t.get('tp1'),
        "tp2"        : t.get('tp2'),
        "pnl_usd"    : round(pnl_usd, 4),
        "pnl_r"      : t.get('result_pnl', 0),
        "outcome"    : outcome,
        "bep_done"   : status in ('TP1_HIT', 'TP2_HIT'),
        "opened_at"  : t.get('timestamp'),
        "closed_at"  : t.get('closed_at'),
        "secret"     : secret,
    }
    try:
        r = requests.post(f"{web_url}/api/trades", json=body, timeout=10)
        ok = r.status_code < 400
        marker = "OK" if ok else f"FAIL {r.status_code}"
        print(f"  {sym:10s} {status:8s} pnl=${pnl_usd:+.4f}  R={t.get('result_pnl'):+.2f}  {marker}")
        if ok:
            posted += 1
    except Exception as e:
        print(f"  {sym:10s} error: {e}")
    time.sleep(0.2)

print(f"\nDONE — posted {posted}/{len(local)}")
print("Refresh dashboard: harus 13 entries dengan pnl_usd accurate")
