#!/usr/bin/env python3
"""DELETE aggressive (multiple calls) + POST 1 fresh entry untuk LDO/VANA/IP.

Bug previous script: VANA cuma POST (no DELETE) → duplicate. Plus LDO/IP
DELETE single call mungkin gak catch semua duplicates dari POST sebelumnya.

Workflow per coin:
1. DELETE 5x (limit=5 each = up to 25 entries cleared)
2. POST 1 fresh dengan correct status
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
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(str(ROOT / ".env"))

ENV  = ROOT / ".env"
TRADE_HIST_FILE = ROOT / "data" / "trade_history.json"

env_vars = {}
for line in ENV.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env_vars[k.strip()] = v.strip().strip('"').strip("'")

token   = env_vars.get("TELEGRAM_BOT_TOKEN", "")
web_url = env_vars.get("WEB_URL", "https://cryptovision-web.vercel.app")

# Read local history untuk dapat trade data per symbol
history = json.loads(TRADE_HIST_FILE.read_text())

# Pick latest entry per symbol
TARGETS = ["LDO", "VANA", "IP"]
trades_by_sym = {}
for h in history:
    sym = h.get('symbol')
    if sym in TARGETS:
        # Latest by closed_at
        existing = trades_by_sym.get(sym)
        if not existing or str(h.get('closed_at','')) > str(existing.get('closed_at','')):
            trades_by_sym[sym] = h

if len(trades_by_sym) < 3:
    print(f"WARN: only found {list(trades_by_sym.keys())} in local history")

for sym in TARGETS:
    h = trades_by_sym.get(sym)
    if not h:
        print(f"=== {sym}: SKIP (not in local history)")
        continue

    print(f"=== {sym} ({h.get('status')}, pnl={h.get('result_pnl'):+.2f}R) ===")

    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()

    # ── DELETE aggressive: 5x calls limit=5 each ───────────
    print(f"  DELETE all entries...")
    for _ in range(5):
        try:
            r = requests.delete(f"{web_url}/api/trades",
                                params={"symbol": sym, "secret": secret,
                                        "hours": "10000", "limit": "5"},
                                timeout=10)
            try:
                deleted = r.json().get('deleted', 0)
                if deleted == 0:
                    break
            except Exception:
                break
        except Exception as e:
            print(f"    error: {e}")
            break
        time.sleep(0.15)
    print(f"  DELETE done")

    # ── POST 1 fresh entry ─────────────────────────────────
    status = h.get('status', 'TP1_HIT')
    if status == 'TP2_HIT':
        outcome, exit_p = "PROFIT", h.get('tp2', h.get('entry'))
    elif status == 'TP1_HIT':
        outcome, exit_p = "PROFIT", h.get('tp1', h.get('entry'))
    elif status == 'BEP':
        outcome, exit_p = "BEP", h.get('entry')
    else:
        outcome, exit_p = "LOSS", h.get('sl', h.get('entry'))

    pnl_r = float(h.get('result_pnl', 0))
    # Estimate pnl_usd from R (we have actual from Bitunix earlier)
    # For now just use pnl_r * estimated risk_amount or 0.34 USD for LDO etc
    pnl_usd_estimates = {"LDO": 0.3385, "VANA": 0.4015, "IP": 0.2349}
    pnl_usd = pnl_usd_estimates.get(sym, 0)

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
        "pnl_usd"    : pnl_usd,
        "pnl_r"      : pnl_r,
        "outcome"    : outcome,
        "bep_done"   : status in ("TP1_HIT", "TP2_HIT"),
        "opened_at"  : h.get('timestamp'),
        "closed_at"  : h.get('closed_at'),
        "secret"     : secret,
    }
    try:
        r = requests.post(f"{web_url}/api/trades", json=body, timeout=10)
        print(f"  POST status={r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"  POST error: {e}")
    print()
    time.sleep(0.3)

print("DONE — refresh /history. Harusnya tinggal 16 entries (13 + 3 fresh).")
