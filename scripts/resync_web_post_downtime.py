#!/usr/bin/env python3
"""Resync web setelah downtime ~16 hari (5/17-6/01).

Strategy:
1. DELETE old data dari web (purge by symbol, hapus semua history)
2. POST closed trades dari learning.db (~60 trades sejak 5/17)
3. OPEN positions ga di-push (bot handle via /api/positions sync)

Idempotent — aman re-run kalau gagal di tengah.

Usage di VPS:
    cd ~/cryptovision-bot
    python3 scripts/resync_web_post_downtime.py --dry-run     # preview
    python3 scripts/resync_web_post_downtime.py               # execute
"""
import argparse
import hashlib
import hmac
import json
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview tanpa execute DELETE/POST')
    parser.add_argument('--since', default='2026-05-17',
                        help='Start date (YYYY-MM-DD), default 2026-05-17')
    args = parser.parse_args()

    ROOT = Path(__file__).parent.parent.resolve()
    env = load_env(ROOT / '.env')
    token = env.get('TELEGRAM_BOT_TOKEN', '')
    web_url = env.get('WEB_URL', 'https://cryptovision-web.vercel.app')

    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not in .env")
        sys.exit(1)

    db = ROOT / 'data' / 'learning.db'
    if not db.exists():
        print(f"ERROR: learning.db not found at {db}")
        sys.exit(1)

    # Query closed trades
    con = sqlite3.connect(str(db))
    cur = con.cursor()
    cur.execute("""
        SELECT symbol, direction, quality, confluence_score, kill_count, outcome,
               pnl_usd, entry_time, close_time, entry_price, sl_price,
               tp1_price, tp2_price, rr2
        FROM trade_log
        WHERE entry_time >= ? AND outcome != 'OPEN' AND outcome IS NOT NULL
        ORDER BY entry_time
    """, (args.since,))
    rows = cur.fetchall()
    con.close()

    print(f"Web URL: {web_url}")
    print(f"Found {len(rows)} closed trades sejak {args.since}")

    # Unique symbols untuk purge
    symbols = sorted(set(r[0] for r in rows))
    print(f"Unique symbols: {len(symbols)}")

    if args.dry_run:
        print("\n[DRY-RUN] Trades yang akan di-POST:")
        for r in rows[:10]:
            print(f"  {r[7][:10]} {r[0]:8s} {r[1]:5s} {r[2]:8s} score={r[3]} {r[5]:4s} pnl=${r[6] or 0:+.2f}")
        if len(rows) > 10:
            print(f"  ... +{len(rows)-10} more")
        print(f"\n[DRY-RUN] Symbols to PURGE first: {', '.join(symbols)}")
        return

    # ── Step 1: PURGE ─────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"STEP 1: PURGE web /api/trades untuk {len(symbols)} symbols")
    print("=" * 60)
    for sym in symbols:
        secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
        deleted_total = 0
        for _attempt in range(5):  # max 5 batch
            try:
                r = requests.delete(
                    f"{web_url}/api/trades",
                    params={"symbol": sym, "secret": secret,
                            "hours": "10000", "limit": "100"},
                    timeout=15,
                )
                if r.status_code >= 400:
                    break
                try:
                    deleted = r.json().get('deleted', 0)
                except Exception:
                    deleted = 0
                deleted_total += deleted
                if deleted == 0:
                    break
            except Exception as e:
                print(f"  {sym}: ERROR {e}")
                break
            time.sleep(0.15)
        print(f"  {sym:12s}: purged {deleted_total}")
    print("\nPurge done")

    # ── Step 2: POST fresh ────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"STEP 2: POST {len(rows)} trades fresh")
    print("=" * 60)
    posted = 0
    failed = 0
    for r in rows:
        sym, dir_, qual, score, kills, outcome, pnl_usd, e_time, c_time, entry, sl, tp1, tp2, rr2 = r
        secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()

        # Outcome mapping
        if outcome in ('TP2', 'FULL_TP'):
            out_label = 'PROFIT'; exit_p = tp2 or entry
        elif outcome == 'TP1':
            out_label = 'PROFIT'; exit_p = tp1 or entry
        elif outcome == 'BEP':
            out_label = 'BEP';    exit_p = entry
        else:  # SL
            out_label = 'LOSS';   exit_p = sl or entry

        # Compute pnl_r from pnl_usd (assume $1 risk)
        pnl_r = round((pnl_usd or 0) / 1.0, 2)

        body = {
            "symbol"     : sym,
            "direction"  : dir_,
            "strategy"   : "swing",
            "quality"    : qual or 'GOOD',
            "entry"      : entry,
            "exit_price" : exit_p,
            "sl"         : sl,
            "tp1"        : tp1,
            "tp2"        : tp2,
            "pnl_usd"    : round(pnl_usd or 0, 4),
            "pnl_r"      : pnl_r,
            "outcome"    : out_label,
            "bep_done"   : outcome in ('TP1', 'TP2', 'FULL_TP'),
            "opened_at"  : e_time,
            "closed_at"  : c_time,
            "secret"     : secret,
        }
        try:
            resp = requests.post(f"{web_url}/api/trades", json=body, timeout=15)
            ok = resp.status_code < 400
            marker = "OK" if ok else f"FAIL {resp.status_code}"
            if ok: posted += 1
            else: failed += 1
            print(f"  {sym:10s} {outcome:4s} pnl=${pnl_usd or 0:+6.2f} {marker}")
        except Exception as e:
            failed += 1
            print(f"  {sym:10s} ERROR: {e}")
        time.sleep(0.15)

    print(f"\nDONE — posted {posted}/{len(rows)} (failed: {failed})")
    print(f"Verify: {web_url}/dashboard/history")


if __name__ == '__main__':
    main()
