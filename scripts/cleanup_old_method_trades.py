#!/usr/bin/env python3
"""Cleanup trade history dari metode LAMA — keep mulai dari BTC short ke depan.

Context (2026-05-07):
- Bot upgrade besar: signal entry priority, Stage 4 disabled, risk_dist fix
- Trade sebelum BTC TP2 (2026-04-25 16:38:17) pakai metode LAMA
- User want fresh start stats dari metode baru saja

Cutoff: BTC TP2_HIT 2026-04-25 16:38:17
- KEEP: trade dengan closed_at >= cutoff (8 trades: BTC, ARB, RENDER, BASED,
  OPG, OP, TIA, ALGO)
- DELETE: trade lebih lama (18 trades)

Workflow:
1. Backup trade_history.json
2. Split entries: keep vs delete (by closed_at vs cutoff)
3. Save filtered local
4. DELETE setiap old trade dari web /api/trades

Usage:
    python3 scripts/cleanup_old_method_trades.py --dry-run
    python3 scripts/cleanup_old_method_trades.py
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
TRADE_HIST_FILE = ROOT / "data" / "trade_history.json"
ENV  = ROOT / ".env"

CUTOFF_STR = "2026-04-25 16:38:17"  # BTC TP2_HIT close time

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true",
                    help="Preview tanpa apply changes")
parser.add_argument("--cutoff", type=str, default=CUTOFF_STR,
                    help=f"Cutoff timestamp (default: {CUTOFF_STR})")
args = parser.parse_args()

# ── Load env ──────────────────────────────────────────────────────────
env_vars = {}
for line in ENV.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env_vars[k.strip()] = v.strip().strip('"').strip("'")

token   = env_vars.get("TELEGRAM_BOT_TOKEN", "")
web_url = env_vars.get("WEB_URL", "https://cryptovision-web.vercel.app")

# ── Backup ────────────────────────────────────────────────────────────
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
if not args.dry_run:
    backup = TRADE_HIST_FILE.with_suffix(f".json.bak_old_method_{ts}")
    backup.write_bytes(TRADE_HIST_FILE.read_bytes())
    print(f"Backup: {backup.name}\n")

# ── Parse cutoff ──────────────────────────────────────────────────────
def parse_dt(s: str) -> float:
    """Parse ISO/space-separated timestamp ke unix epoch seconds."""
    if not s:
        return 0
    s = str(s).replace("T", " ").split(".")[0].split("+")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).timestamp()
        except ValueError:
            continue
    return 0

cutoff_ts = parse_dt(args.cutoff)
if cutoff_ts <= 0:
    print(f"ERROR: invalid cutoff '{args.cutoff}'")
    sys.exit(1)

print(f"Cutoff: {args.cutoff} ({cutoff_ts})")

# ── Load history ──────────────────────────────────────────────────────
history = json.loads(TRADE_HIST_FILE.read_text())
print(f"Total trades: {len(history)}\n")

# ── Split ─────────────────────────────────────────────────────────────
keep = []
delete = []
for t in history:
    closed_at = t.get('closed_at') or t.get('timestamp', '')
    closed_ts = parse_dt(closed_at)
    if closed_ts >= cutoff_ts:
        keep.append(t)
    else:
        delete.append(t)

print(f"KEEP ({len(keep)}):")
for t in keep:
    print(f"  {t.get('symbol')} {t.get('status')} {t.get('result_pnl'):+.2f}R closed={t.get('closed_at')}")

print(f"\nDELETE ({len(delete)}):")
for t in delete:
    print(f"  {t.get('symbol')} {t.get('status')} {t.get('result_pnl'):+.2f}R closed={t.get('closed_at')}")

if args.dry_run:
    print("\n[DRY-RUN] no changes applied")
    sys.exit(0)

# ── Save filtered local ───────────────────────────────────────────────
TRADE_HIST_FILE.write_text(json.dumps(keep, indent=2))
print(f"\nLocal saved: {len(keep)} entries kept (was {len(history)})")

# ── DELETE old trades dari web ────────────────────────────────────────
if not token:
    print("\nWARN: TELEGRAM_BOT_TOKEN not found, skip web delete")
    sys.exit(0)

print(f"\nDeleting {len(delete)} entries dari web /api/trades...")
for t in delete:
    sym = t.get('symbol')
    closed_at = t.get('closed_at') or ''
    if not sym:
        continue
    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
    # delete_trade.py default: hours=48 lookback, limit=1
    # Untuk trade lama (>48h ago), perlu hours yang besar
    closed_ts = parse_dt(closed_at)
    hours_ago = max(48, int((time.time() - closed_ts) / 3600) + 24)
    params = {
        "symbol": sym,
        "secret": secret,
        "hours" : str(hours_ago),
        "limit" : "5",  # max delete per call
    }
    try:
        r = requests.delete(f"{web_url}/api/trades", params=params, timeout=10)
        print(f"  {sym} ({closed_at[:10]}): status={r.status_code}")
        if r.status_code >= 400:
            print(f"    error: {r.text[:100]}")
    except Exception as e:
        print(f"  {sym}: error {e}")
    time.sleep(0.2)  # rate limit

print(f"\nDONE — cek dashboard /history (harus tinggal {len(keep)} trades)")
