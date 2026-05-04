#!/usr/bin/env python3
"""List semua trade closed dari web API untuk audit outcome label.

Usage:
  python3 scripts/list_trades.py
  python3 scripts/list_trades.py --hours 720    # 30 hari window
  python3 scripts/list_trades.py --limit 50
"""
import argparse
import os
import sys
import requests


parser = argparse.ArgumentParser()
parser.add_argument("--hours", type=int, default=720, help="Window lookback (default 720h = 30d)")
parser.add_argument("--limit", type=int, default=50, help="Max rows (default 50)")
args = parser.parse_args()

web_url = os.getenv("WEB_URL", "https://cryptovision-web.vercel.app")
url = f"{web_url}/api/trades?hours={args.hours}&limit={args.limit}"

print(f"GET {url}")
print()

try:
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        print(f"ERROR: status {r.status_code}: {r.text[:200]}")
        sys.exit(1)
    data = r.json()
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

trades = data.get("trades", [])
if not trades:
    print("No trades found.")
    sys.exit(0)

print(f"Found {len(trades)} trades:")
print()
print(f"{'Symbol':<8} {'Dir':<5} {'Entry':<12} {'Exit':<12} {'TP2':<12} {'PnL_R':<8} {'PnL_$':<10} {'Outcome':<8} {'Closed':<20}")
print("-" * 120)

for t in trades:
    sym     = t.get("symbol", "?")
    dirn    = t.get("direction", "?")
    entry   = str(t.get("entry", "?"))[:11]
    exit_p  = str(t.get("exit_price", "?"))[:11]
    tp2     = str(t.get("tp2", "?"))[:11]
    pnl_r   = t.get("pnl_r", "?")
    pnl_usd = t.get("pnl_usd", "?")
    outcome = t.get("outcome", "?")
    closed  = (t.get("closed_at", "") or "")[:19]

    pnl_r_str   = f"{float(pnl_r):.2f}" if pnl_r not in ("?", None) else "?"
    pnl_usd_str = f"${float(pnl_usd):.2f}" if pnl_usd not in ("?", None) else "?"

    print(f"{sym:<8} {dirn:<5} {entry:<12} {exit_p:<12} {tp2:<12} {pnl_r_str:<8} {pnl_usd_str:<10} {outcome:<8} {closed:<20}")
