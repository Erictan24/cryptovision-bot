#!/usr/bin/env python3
"""Relabel outcome trade berdasarkan kline historis Binance.

Rule (per user 2026-05-07):
- Price reach TP2 → TP2
- Else price reach TP1 → TP1
- Else SL

Untuk LONG: cek max(high) di range opened_at → closed_at vs tp2/tp1
Untuk SHORT: cek min(low) vs tp2/tp1

Untuk trade ARB #2 (04-29) — tp1/tp2 di local itu estimasi.
PnL = -$0.24 negative → forced SL.

Workflow:
1. Loop trade_history.json
2. Fetch Binance kline 15m antara opened_at → closed_at
3. Determine outcome via price action
4. Update local status
5. DELETE old di web + POST new (re-sync web)

Usage:
    python3 scripts/relabel_from_kline.py --dry-run
    python3 scripts/relabel_from_kline.py
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
    backup = TRADE_HIST_FILE.with_suffix(f".json.bak_relabel_{ts}")
    backup.write_bytes(TRADE_HIST_FILE.read_bytes())
    print(f"Backup: {backup.name}\n")


def parse_dt(s: str) -> int:
    """Parse ISO/space ts → unix ms."""
    if not s:
        return 0
    s = str(s).replace("T", " ").split(".")[0].split("+")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s.strip(), fmt).timestamp() * 1000)
        except ValueError:
            continue
    return 0


def fetch_kline_extreme(symbol: str, start_ms: int, end_ms: int):
    """Get (max_high, min_low) dari Binance kline 15m antara start & end."""
    sym = symbol.upper().replace('/USDT', '').replace('USDT', '') + 'USDT'
    url = 'https://fapi.binance.com/fapi/v1/klines'
    # Add buffer +5min before & after to capture wick
    params = {
        'symbol'   : sym,
        'interval' : '15m',
        'startTime': str(start_ms - 5 * 60 * 1000),
        'endTime'  : str(end_ms + 5 * 60 * 1000),
        'limit'    : '1500',
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return (0, 0)
        klines = r.json()
        if not klines or not isinstance(klines, list):
            return (0, 0)
        highs = [float(k[2]) for k in klines]
        lows  = [float(k[3]) for k in klines]
        return (max(highs) if highs else 0, min(lows) if lows else 0)
    except Exception as e:
        print(f"  kline fetch error: {e}")
        return (0, 0)


def determine_outcome(direction: str, max_high: float, min_low: float,
                      tp1: float, tp2: float) -> str:
    """Return TP2_HIT, TP1_HIT, atau SL_HIT."""
    if direction == 'LONG':
        if max_high >= tp2 > 0:
            return 'TP2_HIT'
        if max_high >= tp1 > 0:
            return 'TP1_HIT'
        return 'SL_HIT'
    else:  # SHORT
        if min_low <= tp2 and tp2 > 0:
            return 'TP2_HIT'
        if min_low <= tp1 and tp1 > 0:
            return 'TP1_HIT'
        return 'SL_HIT'


# ── Process ────────────────────────────────────────────────────────
history = json.loads(TRADE_HIST_FILE.read_text())
print(f"Processing {len(history)} entries\n")
print("=" * 80)

changes = []
for t in history:
    sym       = t.get('symbol', '')
    direction = t.get('direction', 'LONG')
    tp1       = float(t.get('tp1', 0))
    tp2       = float(t.get('tp2', 0))
    pnl_r     = float(t.get('result_pnl', 0))
    old_status = t.get('status', '')
    opened_at = t.get('timestamp', '')
    closed_at = t.get('closed_at', '')

    start_ms = parse_dt(opened_at)
    end_ms   = parse_dt(closed_at)

    if not start_ms or not end_ms or end_ms <= start_ms:
        print(f"{sym:8s} {direction:5s}: SKIP (invalid timestamps)")
        continue

    # Special case: trades with negative PnL = guaranteed SL
    if pnl_r < -0.1:
        new_status = 'SL_HIT'
        max_high, min_low = 0, 0
        ext_str = "(skip kline — pnl negatif = SL)"
    else:
        max_high, min_low = fetch_kline_extreme(sym, start_ms, end_ms)
        if max_high <= 0 or min_low <= 0:
            print(f"{sym:8s} {direction:5s}: SKIP (kline fetch fail)")
            continue
        new_status = determine_outcome(direction, max_high, min_low, tp1, tp2)
        ext_str = f"max_high={max_high:.6g} min_low={min_low:.6g}"

    marker = " ← CHANGE" if new_status != old_status else ""
    print(f"{sym:8s} {direction:5s} {old_status:8s} → {new_status:8s} "
          f"pnl={pnl_r:+.2f}R | tp1={tp1:.6g} tp2={tp2:.6g} {ext_str}{marker}")

    if new_status != old_status:
        changes.append((t, old_status, new_status))
    t['status'] = new_status
    time.sleep(0.15)  # rate limit Binance

print("=" * 80)
print(f"\nChanges: {len(changes)}")

if args.dry_run:
    print("\n[DRY-RUN] no changes applied")
    sys.exit(0)

# Save local
TRADE_HIST_FILE.write_text(json.dumps(history, indent=2))
print(f"\nLocal saved.")

# Re-sync web: DELETE old + POST new untuk yang berubah
if not changes:
    print("No web sync needed.")
    sys.exit(0)

print(f"\nRe-syncing {len(changes)} changed entries to web...")
for entry, old_status, new_status in changes:
    sym = entry['symbol']
    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()

    # DELETE old (1 entry, wide window)
    try:
        requests.delete(f"{web_url}/api/trades",
                        params={"symbol": sym, "secret": secret,
                                "hours": "10000", "limit": "5"},
                        timeout=10)
    except Exception:
        pass
    time.sleep(0.2)

    # POST new dengan status baru
    pnl_r = float(entry.get('result_pnl', 0))
    if new_status == 'TP2_HIT':
        outcome = 'PROFIT'
    elif new_status == 'TP1_HIT':
        outcome = 'PROFIT'
    elif new_status == 'BEP':
        outcome = 'BEP'
    else:
        outcome = 'LOSS'

    body = {
        "symbol"     : sym,
        "direction"  : entry.get('direction'),
        "strategy"   : "swing",
        "quality"    : entry.get('quality', 'GOOD'),
        "entry"      : entry.get('entry'),
        "exit_price" : entry.get('tp2') if new_status == 'TP2_HIT' else entry.get('tp1') if new_status == 'TP1_HIT' else entry.get('sl'),
        "sl"         : entry.get('sl'),
        "tp1"        : entry.get('tp1'),
        "tp2"        : entry.get('tp2'),
        "pnl_usd"    : 0,  # web compute from R
        "pnl_r"      : pnl_r,
        "outcome"    : outcome,
        "bep_done"   : new_status in ('TP1_HIT', 'TP2_HIT'),
        "opened_at"  : entry.get('timestamp'),
        "closed_at"  : entry.get('closed_at'),
        "secret"     : secret,
    }
    try:
        r = requests.post(f"{web_url}/api/trades", json=body, timeout=10)
        print(f"  {sym}: {old_status}→{new_status} POST status={r.status_code}")
    except Exception as e:
        print(f"  {sym}: error {e}")
    time.sleep(0.2)

print("\nDONE — cek dashboard /history dan /statistics")
