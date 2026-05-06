#!/usr/bin/env python3
"""NUCLEAR resync: hapus SEMUA trade di web → re-POST 13 fresh dari local.

Context (2026-05-07): Multiple recovery script jalan, web jadi messy:
- Duplicates (IO 2x)
- BEP $0.00 (relabel POST dengan pnl_usd=0 — bug)
- Residue metode lama (cleanup_web_stats belum ke-run)

Solusi: nuke + rebuild. DELETE ALL trades untuk semua symbol yang
mungkin ada di web. POST 13 fresh dengan pnl_usd REAL dari Bitunix
realizedPNL (re-fetch).

Usage:
    python3 scripts/web_full_resync.py --dry-run
    python3 scripts/web_full_resync.py
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

# Semua symbol yang mungkin pernah masuk web (metode lama + baru + dangling)
ALL_SYMBOLS_TO_PURGE = [
    # Metode baru (akan di-re-POST)
    "BTC", "PIPPIN", "ARB", "RENDER", "BASED", "UNI",
    "LINK", "OPG", "OP", "TIA", "ALGO", "IO",
    # Metode lama (residue dari recover_dangling)
    "BLUR", "KAT", "SEI", "BNB", "SOL", "AVAX", "PAXG", "SUI",
    "AIOT", "XPL", "1000PEPE", "BCH", "DOGE", "PI",
    # Lain (kalau ada)
    "VANA", "LDO",
]

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true",
                    help="Preview")
args = parser.parse_args()

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

local = json.loads(TRADE_HIST_FILE.read_text())
print(f"Local trade_history.json: {len(local)} entries")
print(f"Will purge web for {len(ALL_SYMBOLS_TO_PURGE)} symbols")
print(f"Then re-POST {len(local)} entries fresh\n")

if args.dry_run:
    print("[DRY-RUN]")
    print("\nLocal entries to re-POST:")
    for t in local:
        print(f"  {t.get('symbol'):8s} {t.get('direction'):5s} {t.get('status'):8s} pnl_r={t.get('result_pnl'):+.2f}R")
    sys.exit(0)

# ── Step 1: PURGE web — DELETE all trades for known symbols ─────────
print("=" * 60)
print("STEP 1: PURGE web /api/trades")
print("=" * 60)
trader = BitunixTrader()  # for fetching realizedPNL

for sym in ALL_SYMBOLS_TO_PURGE:
    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
    # DELETE multiple times to clear all entries (limit=5 per call)
    for attempt in range(3):
        try:
            r = requests.delete(f"{web_url}/api/trades",
                                params={"symbol": sym, "secret": secret,
                                        "hours": "10000", "limit": "5"},
                                timeout=10)
            if r.status_code >= 400:
                print(f"  {sym:10s} attempt {attempt+1}: status={r.status_code} {r.text[:80]}")
                break
            # Check response — kalau sukses, lanjut delete lagi sampai habis
            try:
                deleted = r.json().get('deleted', 0)
                if deleted == 0:
                    break
            except Exception:
                break
        except Exception as e:
            print(f"  {sym:10s} error: {e}")
            break
        time.sleep(0.15)

print("\nPurge done\n")

# ── Step 2: Fetch realizedPNL real dari Bitunix per symbol ─────────
print("=" * 60)
print("STEP 2: Fetch real PnL USD dari Bitunix")
print("=" * 60)

def get_real_pnl_usd(sym: str, opened_at: str) -> float:
    """Fetch realizedPNL dari Bitunix history match by ctime ≈ opened_at."""
    full = sym.upper() + 'USDT'
    data = trader._get('/api/v1/futures/position/get_history_positions',
                       {'symbol': full, 'limit': 20})
    if not data or data.get('code') != 0:
        return 0.0
    result = data.get('data', {})
    if isinstance(result, dict):
        positions = result.get('positionList') or result.get('list') or []
    else:
        positions = result if isinstance(result, list) else []

    target_ts = 0
    s = str(opened_at).replace("T", " ").split(".")[0]
    try:
        target_ts = int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
    except Exception:
        return 0.0

    # Find closest by ctime (open time)
    best_match = None
    min_diff = 60 * 60 * 1000  # 1 hour tolerance
    for p in positions:
        ctime = int(p.get('ctime', 0) or 0)
        diff = abs(ctime - target_ts)
        if diff < min_diff:
            min_diff = diff
            best_match = p
    if not best_match:
        return 0.0
    return float(best_match.get('realizedPNL', 0) or 0)

# Enrich each local entry dengan real PnL USD
for t in local:
    sym = t.get('symbol')
    opened = t.get('timestamp', '')
    pnl_usd = get_real_pnl_usd(sym, opened)
    t['_real_pnl_usd'] = round(pnl_usd, 4)
    print(f"  {sym:10s} opened={opened[:19]} → pnl_usd=${pnl_usd:+.4f}")
    time.sleep(0.15)

# ── Step 3: POST fresh 13 entries to web ──────────────────────────
print("\n" + "=" * 60)
print("STEP 3: POST fresh entries")
print("=" * 60)

posted = 0
for t in local:
    sym = t.get('symbol')
    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
    status = t.get('status', '')

    if status == 'TP2_HIT':
        outcome = 'PROFIT'
        exit_p = t.get('tp2', t.get('entry'))
    elif status == 'TP1_HIT':
        outcome = 'PROFIT'
        exit_p = t.get('tp1', t.get('entry'))
    elif status == 'BEP':
        outcome = 'BEP'
        exit_p = t.get('entry')
    else:  # SL_HIT
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
        "pnl_usd"    : t.get('_real_pnl_usd', 0),
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
        print(f"  {sym:10s} {status:8s} ${t.get('_real_pnl_usd', 0):+.4f} → status={r.status_code}")
        if ok:
            posted += 1
    except Exception as e:
        print(f"  {sym:10s} error: {e}")
    time.sleep(0.2)

print(f"\nDONE — posted {posted}/{len(local)} trades")
print("Cek dashboard: harus tinggal 13 entries (no duplicate, no BEP $0.00)")
