#!/usr/bin/env python3
"""Record 3 closed trades (LDO/VANA/IP) dengan user-specified outcome.

Per user 2026-05-10:
- LDO closed 05-07 → TP1 (bukan TP2)
- VANA closed 05-07 → TP2 (correct)
- IP closed 05-10 → TP1 (bukan TP2)

Workflow:
1. Read active_positions.json untuk signal data per coin
2. Fetch Bitunix history untuk realizedPNL real
3. Compute pnl_r = realizedPNL / risk_amount
4. Add ke trade_history.json dengan status user-specified
5. Remove dari active_positions.json
6. POST ke web /api/trades + DELETE position
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

from bitunix_trader import BitunixTrader

ACTIVE_POS_FILE = ROOT / "data" / "active_positions.json"
TRADE_HIST_FILE = ROOT / "data" / "trade_history.json"
ENV  = ROOT / ".env"

# User-specified outcomes
TARGETS = [
    ("LDO",  "TP1_HIT"),
    ("VANA", "TP2_HIT"),
    ("IP",   "TP1_HIT"),
]

env_vars = {}
for line in ENV.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env_vars[k.strip()] = v.strip().strip('"').strip("'")

token   = env_vars.get("TELEGRAM_BOT_TOKEN", "")
web_url = env_vars.get("WEB_URL", "https://cryptovision-web.vercel.app")

# Backup
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
for f in [ACTIVE_POS_FILE, TRADE_HIST_FILE]:
    if f.exists():
        backup = f.with_suffix(f"{f.suffix}.bak_userclose_{ts}")
        backup.write_bytes(f.read_bytes())
        print(f"Backup: {backup.name}")
print()

active = json.loads(ACTIVE_POS_FILE.read_text())
history = json.loads(TRADE_HIST_FILE.read_text())
trader = BitunixTrader()


def fetch_bitunix_close(sym: str):
    """Get latest closed position dari Bitunix history."""
    data = trader._get('/api/v1/futures/position/get_history_positions',
                       {'symbol': sym + 'USDT', 'limit': 5})
    if not data or data.get('code') != 0:
        return None
    result = data.get('data', {})
    if isinstance(result, dict):
        positions = result.get('positionList') or result.get('list') or []
    elif isinstance(result, list):
        positions = result
    else:
        positions = []
    return positions[0] if positions else None


for sym, target_status in TARGETS:
    print(f"=== {sym} → {target_status} ===")

    # Read signal data dari active_positions
    pos = active.get(sym)
    if not pos:
        print(f"  ERROR: {sym} not in active_positions.json — skip")
        continue

    direction = pos.get("direction", "LONG")
    entry     = float(pos.get("entry", 0))
    sl_init   = float(pos.get("sl_initial", pos.get("sl", 0)))
    tp1       = float(pos.get("tp1", 0))
    tp2       = float(pos.get("tp2", 0))
    qty       = float(pos.get("qty", 0))
    quality   = pos.get("quality", "GOOD")
    strategy  = pos.get("_strategy", "swing")
    opened_at = pos.get("opened_at", "")

    # Fetch realizedPNL dari Bitunix
    bitunix_record = fetch_bitunix_close(sym)
    if not bitunix_record:
        print(f"  ERROR: no Bitunix history for {sym} — skip")
        continue

    pnl_usd = float(bitunix_record.get('realizedPNL', 0) or 0)
    mtime_ms = int(bitunix_record.get('mtime', 0) or 0)
    closed_at = datetime.fromtimestamp(mtime_ms / 1000).strftime('%Y-%m-%d %H:%M:%S') if mtime_ms else ''

    # Compute pnl_r
    risk_amount = abs(entry - sl_init) * qty if (entry and sl_init and qty) else 0
    if risk_amount > 0:
        pnl_r = round(pnl_usd / risk_amount, 2)
    else:
        # Fallback: estimate from sign
        pnl_r = 1.0 if pnl_usd > 0 else (-1.0 if pnl_usd < 0 else 0)

    # Determine outcome label & exit_price
    if target_status == "TP2_HIT":
        outcome = "PROFIT"
        exit_p = tp2
    elif target_status == "TP1_HIT":
        outcome = "PROFIT"
        exit_p = tp1
    elif target_status == "BEP":
        outcome = "BEP"
        exit_p = entry
    else:  # SL_HIT
        outcome = "LOSS"
        exit_p = sl_init

    next_id = max((t.get('id', 0) for t in history), default=0) + 1
    closed_entry = {
        "id"         : next_id,
        "symbol"     : sym,
        "direction"  : direction,
        "quality"    : quality,
        "entry"      : entry,
        "sl"         : sl_init,
        "tp1"        : tp1,
        "tp2"        : tp2,
        "confluence" : pos.get("confluence", pos.get("score", 0)),
        "rr1"        : 1.0,
        "rr2"        : float(pos.get("rr", 2.0)),
        "timestamp"  : opened_at,
        "status"     : target_status,
        "result_pnl" : pnl_r,
        "closed_at"  : closed_at,
    }
    history.append(closed_entry)

    print(f"  PnL Bitunix: ${pnl_usd:+.4f}")
    print(f"  PnL R      : {pnl_r:+.2f}R")
    print(f"  Status     : {target_status}")
    print(f"  Closed at  : {closed_at}")

    # Remove dari active
    if sym in active:
        del active[sym]
        print(f"  LOCAL: removed dari active_positions.json")

    # POST web
    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
    body = {
        "symbol"     : sym,
        "direction"  : direction,
        "strategy"   : strategy,
        "quality"    : quality,
        "entry"      : entry,
        "exit_price" : exit_p,
        "sl"         : sl_init,
        "tp1"        : tp1,
        "tp2"        : tp2,
        "pnl_usd"    : round(pnl_usd, 4),
        "pnl_r"      : pnl_r,
        "outcome"    : outcome,
        "bep_done"   : target_status in ("TP1_HIT", "TP2_HIT"),
        "opened_at"  : opened_at,
        "closed_at"  : closed_at,
        "secret"     : secret,
    }
    try:
        r = requests.post(f"{web_url}/api/trades", json=body, timeout=10)
        print(f"  WEB POST trade: status={r.status_code}")
    except Exception as e:
        print(f"  WEB POST error: {e}")

    # DELETE position dari web
    try:
        r = requests.delete(f"{web_url}/api/positions",
                            params={"symbol": sym, "secret": secret}, timeout=10)
        print(f"  WEB DELETE position: status={r.status_code}")
    except Exception as e:
        print(f"  WEB DELETE error: {e}")

    time.sleep(0.3)
    print()

# Save local
ACTIVE_POS_FILE.write_text(json.dumps(active, indent=2))
TRADE_HIST_FILE.write_text(json.dumps(history, indent=2))

print(f"Saved local files.")
print(f"trade_history.json: {len(history)} entries total")
print(f"active_positions.json: {len(active)} entries remaining")
