#!/usr/bin/env python3
"""Fix label LDO/VANA/IP — bot auto-pushed dengan status salah, user koreksi.

Per user 2026-05-10:
- LDO closed → label web salah (TP2), HARUSNYA TP1
- VANA closed → label web TP2 (CORRECT, skip)
- IP closed → label web salah (TP2), HARUSNYA TP1

Workflow:
1. DELETE entry web /api/trades untuk LDO + IP (clear yang label salah)
2. POST fresh dengan status TP1_HIT + pnl_r computed dari Bitunix realizedPNL
3. Sync local trade_history.json (add entries kalau belum ada)
4. Remove dari active_positions.json (closed)
5. VANA biarin (label sudah benar)
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

# (symbol, target_status, action)
# action: 'fix_label' = DELETE old web + POST new
#         'sync_local' = add local kalau belum ada (sudah benar di web)
TARGETS = [
    ("LDO",  "TP1_HIT", "fix_label"),
    ("VANA", "TP2_HIT", "sync_local"),
    ("IP",   "TP1_HIT", "fix_label"),
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
        backup = f.with_suffix(f"{f.suffix}.bak_relabel_{ts}")
        backup.write_bytes(f.read_bytes())
        print(f"Backup: {backup.name}")
print()

active = json.loads(ACTIVE_POS_FILE.read_text())
history = json.loads(TRADE_HIST_FILE.read_text())
trader = BitunixTrader()


def fetch_bitunix_close(sym: str):
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


for sym, target_status, action in TARGETS:
    print(f"=== {sym} → {target_status} ({action}) ===")

    pos = active.get(sym)
    if not pos:
        print(f"  WARN: {sym} not in active_positions.json — try fetch from saved data anyway")
        # Continue with empty pos data — fetch from Bitunix only
        pos = {}

    direction = pos.get("direction", "LONG")
    entry     = float(pos.get("entry", 0))
    sl_init   = float(pos.get("sl_initial", pos.get("sl", 0)))
    tp1       = float(pos.get("tp1", 0))
    tp2       = float(pos.get("tp2", 0))
    qty       = float(pos.get("qty", 0))
    quality   = pos.get("quality", "GOOD")
    strategy  = pos.get("_strategy", "swing")
    opened_at = pos.get("opened_at", "")

    bitunix_record = fetch_bitunix_close(sym)
    if not bitunix_record:
        print(f"  ERROR: no Bitunix history — skip")
        continue

    pnl_usd = float(bitunix_record.get('realizedPNL', 0) or 0)
    mtime_ms = int(bitunix_record.get('mtime', 0) or 0)
    closed_at = datetime.fromtimestamp(mtime_ms / 1000).strftime('%Y-%m-%d %H:%M:%S') if mtime_ms else ''

    risk_amount = abs(entry - sl_init) * qty if (entry and sl_init and qty) else 0
    if risk_amount > 0:
        pnl_r = round(pnl_usd / risk_amount, 2)
    else:
        pnl_r = 1.0 if pnl_usd > 0 else (-1.0 if pnl_usd < 0 else 0)

    if target_status == "TP2_HIT":
        outcome, exit_p = "PROFIT", tp2
    elif target_status == "TP1_HIT":
        outcome, exit_p = "PROFIT", tp1
    elif target_status == "BEP":
        outcome, exit_p = "BEP", entry
    else:
        outcome, exit_p = "LOSS", sl_init

    print(f"  PnL Bitunix: ${pnl_usd:+.4f}")
    print(f"  PnL R      : {pnl_r:+.2f}R")
    print(f"  Closed at  : {closed_at}")
    print(f"  Action     : {action}")

    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()

    # ── 1. fix_label: DELETE old entry di web ────────────────────
    if action == "fix_label":
        try:
            r = requests.delete(f"{web_url}/api/trades",
                                params={"symbol": sym, "secret": secret,
                                        "hours": "10000", "limit": "5"},
                                timeout=10)
            print(f"  WEB DELETE old trade: status={r.status_code}")
        except Exception as e:
            print(f"  WEB DELETE error: {e}")
        time.sleep(0.2)

    # ── 2. POST trade dengan status correct ──────────────────────
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
    time.sleep(0.2)

    # ── 3. DELETE position di web (kalau masih show running) ─────
    try:
        r = requests.delete(f"{web_url}/api/positions",
                            params={"symbol": sym, "secret": secret}, timeout=10)
        print(f"  WEB DELETE position: status={r.status_code}")
    except Exception as e:
        print(f"  WEB DELETE position error: {e}")

    # ── 4. Sync local trade_history kalau belum ada ──────────────
    existing = [h for h in history if h.get('symbol') == sym and h.get('closed_at') == closed_at]
    if existing:
        # Update existing entry status
        for h in existing:
            h['status'] = target_status
            h['result_pnl'] = pnl_r
        print(f"  LOCAL: updated existing entry status → {target_status}")
    else:
        # Add new entry
        next_id = max((t.get('id', 0) for t in history), default=0) + 1
        history.append({
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
        })
        print(f"  LOCAL: added new entry status={target_status}")

    # ── 5. Remove dari active ────────────────────────────────────
    if sym in active:
        del active[sym]
        print(f"  LOCAL: removed from active_positions")

    print()

ACTIVE_POS_FILE.write_text(json.dumps(active, indent=2))
TRADE_HIST_FILE.write_text(json.dumps(history, indent=2))

print(f"trade_history.json: {len(history)} entries total")
print(f"active_positions.json: {len(active)} entries remaining")
