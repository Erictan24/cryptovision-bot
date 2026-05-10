#!/usr/bin/env python3
"""Cek Bitunix history untuk recently closed positions + status di local.

Useful saat ada trade closed tapi label di local/web salah.

Usage:
    python3 scripts/check_recent_closes.py LDO VANA IP IO
"""
import os
import sys
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(str(ROOT / ".env"))

from bitunix_trader import BitunixTrader

ACTIVE_POS_FILE = ROOT / "data" / "active_positions.json"
TRADE_HIST_FILE = ROOT / "data" / "trade_history.json"

t = BitunixTrader()

# Default coins kalau gak kasih arg
coins = sys.argv[1:] if len(sys.argv) > 1 else ['LDO', 'VANA', 'IP', 'IO', 'STORJ', 'ACE']

# Load local
active = json.loads(ACTIVE_POS_FILE.read_text()) if ACTIVE_POS_FILE.exists() else {}
history = json.loads(TRADE_HIST_FILE.read_text()) if TRADE_HIST_FILE.exists() else []

# Active Bitunix positions
open_now = t.get_positions() or []
open_syms = set()
for p in open_now:
    raw = p.get('symbol', '').upper()
    clean = raw.replace('USDT', '')
    if clean:
        open_syms.add(clean)

print(f"Bitunix OPEN now: {sorted(open_syms)}\n")

for s in coins:
    s = s.upper()
    print(f"=== {s} ===")
    in_active = s in active
    in_history = [h for h in history if h.get('symbol') == s]
    in_bitunix_open = s in open_syms

    print(f"  Local active : {'YES' if in_active else 'no'}")
    print(f"  Local history: {len(in_history)} entries — {[h.get('status') for h in in_history]}")
    print(f"  Bitunix open : {'YES' if in_bitunix_open else 'no'}")

    # Fetch Bitunix history
    data = t._get('/api/v1/futures/position/get_history_positions',
                  {'symbol': s + 'USDT', 'limit': 5})
    if not data or data.get('code') != 0:
        print(f"  Bitunix history: API fail")
        print()
        continue

    result = data.get('data', {})
    if isinstance(result, dict):
        positions = result.get('positionList') or result.get('list') or []
    elif isinstance(result, list):
        positions = result
    else:
        positions = []

    if not positions:
        print(f"  Bitunix history: no records")
    else:
        print(f"  Bitunix history: {len(positions)} records")
        for i, p in enumerate(positions):
            ctime = int(p.get('ctime', 0) or 0)
            mtime = int(p.get('mtime', 0) or 0)
            pnl = float(p.get('realizedPNL', 0) or 0)
            side = p.get('side', '?')
            entry = float(p.get('avgOpenPrice', 0) or 0)
            close_price = float(p.get('avgClosePrice', 0) or p.get('avgClosedPrice', 0) or 0)
            opened = datetime.fromtimestamp(ctime / 1000).strftime('%m-%d %H:%M') if ctime else '?'
            closed = datetime.fromtimestamp(mtime / 1000).strftime('%m-%d %H:%M') if mtime else '?'
            print(f"    #{i+1} {side:5s} entry={entry} close={close_price} pnl=${pnl:+.4f} {opened}→{closed}")
    print()
