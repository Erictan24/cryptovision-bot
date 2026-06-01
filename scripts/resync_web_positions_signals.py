#!/usr/bin/env python3
"""Resync web positions + signals (extension dari resync_web_post_downtime.py).

Strategy:
1. POSITIONS — query learning.db untuk OPEN positions, POST ke web.
   DELETE stale positions yang udah ga OPEN.
2. SIGNALS — push semua signals sejak --since ke web /api/signals.
   Cleanup signals yang udah lewat (>30 hari) optional.

Usage di VPS:
    cd ~/cryptovision-bot
    python3 scripts/resync_web_positions_signals.py --dry-run     # preview
    python3 scripts/resync_web_positions_signals.py               # execute
"""
import argparse
import hashlib
import hmac
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests


def load_env(env_path):
    env_vars = {}
    if not env_path.exists():
        print(f"ERROR: .env not found at {env_path}")
        sys.exit(1)
    for line in env_path.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env_vars[k.strip()] = v.strip().strip('"').strip("'")
    return env_vars


def hmac_sec(token: str, symbol: str) -> str:
    return hmac.new(token.encode(), symbol.encode(), hashlib.sha256).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--since', default='2026-05-17')
    args = parser.parse_args()

    ROOT = Path(__file__).parent.parent.resolve()
    env = load_env(ROOT / '.env')
    token = env.get('TELEGRAM_BOT_TOKEN', '')
    web_url = env.get('WEB_URL', 'https://cryptovision-web.vercel.app')

    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not in .env")
        sys.exit(1)

    db = ROOT / 'data' / 'learning.db'
    con = sqlite3.connect(str(db))
    cur = con.cursor()

    # OPEN positions
    cur.execute("""
        SELECT symbol, direction, quality, confluence_score, kill_count,
               pnl_usd, entry_time, entry_price, sl_price, tp1_price, tp2_price, rr2
        FROM trade_log
        WHERE entry_time >= ? AND outcome = 'OPEN'
        ORDER BY entry_time
    """, (args.since,))
    open_positions = cur.fetchall()

    # ALL signals (open + closed) untuk push ke /api/signals
    cur.execute("""
        SELECT symbol, direction, quality, confluence_score, kill_count, outcome,
               pnl_usd, entry_time, close_time, entry_price, sl_price,
               tp1_price, tp2_price, rr2
        FROM trade_log
        WHERE entry_time >= ?
        ORDER BY entry_time
    """, (args.since,))
    all_signals = cur.fetchall()
    con.close()

    # Symbols dengan trade tapi udah closed (untuk purge dari /api/positions)
    closed_symbols = set(s[0] for s in all_signals if s[5] not in (None, 'OPEN'))
    open_symbols = set(p[0] for p in open_positions)
    stale_position_symbols = closed_symbols - open_symbols

    print(f"Web URL: {web_url}")
    print(f"OPEN positions: {len(open_positions)}")
    print(f"All signals (since {args.since}): {len(all_signals)}")
    print(f"Symbols to DELETE from /api/positions (closed): {len(stale_position_symbols)}")

    if args.dry_run:
        print("\n[DRY-RUN] OPEN positions yang akan di-POST ke /api/positions:")
        for p in open_positions:
            print(f"  {p[0]:10s} {p[1]:5s} {p[2]:8s} score={p[3]} entry={p[7]} sl={p[8]} tp1={p[9]} tp2={p[10]}")
        print(f"\n[DRY-RUN] Sample 5 signals yang akan di-POST ke /api/signals:")
        for s in all_signals[:5]:
            print(f"  {s[7][:10]} {s[0]:10s} {s[1]:5s} {s[5]:6s} entry={s[9]}")
        print(f"\n[DRY-RUN] Symbols to DELETE from positions: {sorted(stale_position_symbols)[:20]}")
        return

    # ── Step 1: DELETE stale positions ─────────────────────────────
    print("\n" + "=" * 60)
    print(f"STEP 1: DELETE stale positions ({len(stale_position_symbols)} symbols)")
    print("=" * 60)
    deleted = 0
    for sym in sorted(stale_position_symbols):
        secret = hmac_sec(token, sym)
        try:
            r = requests.delete(
                f"{web_url}/api/positions",
                params={"symbol": sym, "secret": secret},
                timeout=10,
            )
            ok = r.status_code < 400
            if ok: deleted += 1
            print(f"  {sym:10s}: {'DEL OK' if ok else f'FAIL {r.status_code}'}")
        except Exception as e:
            print(f"  {sym:10s}: ERROR {e}")
        time.sleep(0.1)
    print(f"\nDeleted: {deleted}/{len(stale_position_symbols)}")

    # ── Step 2: POST current OPEN positions ─────────────────────────
    print("\n" + "=" * 60)
    print(f"STEP 2: POST {len(open_positions)} OPEN positions")
    print("=" * 60)
    posted_pos = 0
    for p in open_positions:
        sym, dir_, qual, score, kills, _pnl, e_time, entry, sl, tp1, tp2, rr2 = p
        secret = hmac_sec(token, sym)
        body = {
            "symbol"     : sym,
            "direction"  : dir_,
            "strategy"   : "swing",
            "quality"    : qual or 'GOOD',
            "entry"      : entry,
            "sl"         : sl,
            "tp1"        : tp1,
            "tp2"        : tp2,
            "rr2"        : rr2,
            "confluence" : score,
            "kills"      : kills,
            "opened_at"  : e_time,
            "secret"     : secret,
        }
        try:
            r = requests.post(f"{web_url}/api/positions", json=body, timeout=10)
            ok = r.status_code < 400
            if ok: posted_pos += 1
            marker = "OK" if ok else f"FAIL {r.status_code}"
            print(f"  {sym:10s} {dir_:5s} score={score} {marker}")
        except Exception as e:
            print(f"  {sym:10s} ERROR: {e}")
        time.sleep(0.15)
    print(f"\nPosted: {posted_pos}/{len(open_positions)}")

    # ── Step 3: POST all signals fresh ───────────────────────────────
    print("\n" + "=" * 60)
    print(f"STEP 3: POST {len(all_signals)} signals fresh")
    print("=" * 60)
    posted_sig = 0
    failed_sig = 0
    for s in all_signals:
        sym, dir_, qual, score, kills, outcome, pnl_usd, e_time, c_time, entry, sl, tp1, tp2, rr2 = s
        secret = hmac_sec(token, sym)
        status = 'open' if outcome == 'OPEN' else 'closed'
        body = {
            "symbol"     : sym,
            "direction"  : dir_,
            "strategy"   : "swing",
            "quality"    : qual or 'GOOD',
            "entry"      : entry,
            "sl"         : sl,
            "tp1"        : tp1,
            "tp2"        : tp2,
            "rr2"        : rr2,
            "confluence" : score,
            "kills"      : kills,
            "status"     : status,
            "outcome"    : outcome,
            "pnl_usd"    : round(pnl_usd or 0, 4),
            "timestamp"  : e_time,
            "closed_at"  : c_time,
            "secret"     : secret,
        }
        try:
            r = requests.post(f"{web_url}/api/signals", json=body, timeout=10)
            ok = r.status_code < 400
            if ok: posted_sig += 1
            else: failed_sig += 1
        except Exception:
            failed_sig += 1
        time.sleep(0.05)  # faster, less verbose

    print(f"Signals posted: {posted_sig}/{len(all_signals)} (failed: {failed_sig})")
    print(f"\nVerify:")
    print(f"  {web_url}/dashboard/positions")
    print(f"  {web_url}/dashboard/signals")


if __name__ == '__main__':
    main()
