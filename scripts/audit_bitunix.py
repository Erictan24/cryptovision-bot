#!/usr/bin/env python3
"""Audit Bitunix history vs local trade_history.json.

Untuk verify completeness: berapa closed position di Bitunix per coin,
vs berapa entry di local. Identify missed records.

Usage di VPS:
    python3 scripts/audit_bitunix.py
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

# Coin list yang user confirm appear di Bitunix
COINS = ['BTC', 'ARB', 'RENDER', 'BASED', 'UNI', 'TIA', 'OP', 'LINK',
         'ALGO', 'PIPPIN', 'VANA', 'LDO', 'OPG', 'IO']

t = BitunixTrader()
trade_hist_file = ROOT / "data" / "trade_history.json"
local = json.loads(trade_hist_file.read_text()) if trade_hist_file.exists() else []

local_by_sym = {}
for tr in local:
    sym = tr.get('symbol')
    local_by_sym.setdefault(sym, []).append(tr)

print(f"Local history: {len(local)} entries\n")

header = f"{'COIN':10s} {'#':3s} {'SIDE':6s} {'OPEN':14s} {'CLOSE':14s} {'PnL_USD':>10s} {'NOTE'}"
print("=" * 90)
print(header)
print("=" * 90)

bitunix_total_closed = 0

for sym in COINS:
    full = sym + 'USDT'
    data = t._get('/api/v1/futures/position/get_history_positions',
                  {'symbol': full, 'limit': 20})

    positions = []
    if data and data.get('code') == 0:
        result = data.get('data', {})
        if isinstance(result, dict):
            positions = result.get('positionList') or result.get('list') or []
        elif isinstance(result, list):
            positions = result

    local_count = len(local_by_sym.get(sym, []))

    if not positions:
        print(f"{sym:10s} -   {'?':6s} {'-':14s} {'-':14s} {'-':>10s}   "
              f"NO HISTORY (still open or new) | local={local_count}")
        continue

    bitunix_total_closed += len(positions)

    for i, p in enumerate(positions):
        pnl = float(p.get('realizedPNL', 0) or 0)
        ctime = int(p.get('ctime', 0) or 0)
        mtime = int(p.get('mtime', 0) or 0)
        side = p.get('side', '?')
        opened = datetime.fromtimestamp(ctime / 1000).strftime('%m-%d %H:%M') if ctime else '?'
        closed = datetime.fromtimestamp(mtime / 1000).strftime('%m-%d %H:%M') if mtime else '?'

        # Mark MISSED kalau index >= local_count untuk coin ini
        note = ""
        if i >= local_count:
            note = "[MISSED in local]"

        print(f"{sym:10s} #{i+1:<2d} {side:6s} {opened:14s} {closed:14s} ${pnl:+.4f} {note}")

print("=" * 90)
print(f"\nSUMMARY:")
print(f"  Bitunix total closed positions    : {bitunix_total_closed}")
print(f"  Local trade_history.json entries  : {len(local)}")
print(f"  Difference (likely missed)        : {bitunix_total_closed - len(local)}")
