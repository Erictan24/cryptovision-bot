#!/usr/bin/env python3
"""Recovery massal untuk dangling positions — closed di Bitunix tapi local masih open.

Context (2026-05-07):
- 24 position di active_positions.json closed di Bitunix (per get_open_position)
  tapi local state masih show open
- Sama bug class PIPPIN/IO: monitor stop akibat API/DNS error mid-flow,
  trade close di exchange via TP/SL/BEP limit order, tapi state desync

Workflow:
1. Loop active_positions.json
2. Untuk tiap symbol: cek apakah ADA di Bitunix open positions
3. Kalau TIDAK ADA = dangling:
   a. Fetch position history dari Bitunix (realizedPNL + closeTime)
   b. Derive outcome:
      - PnL > +0.8 × risk_initial → TP2_HIT
      - PnL > +0.3 × risk_initial → TP1_HIT (partial close)
      - PnL ~ 0 → BEP
      - PnL < -0.3 × risk_initial → SL_HIT
      - Else UNKNOWN
   c. Move ke trade_history.json (idempotent — skip kalau sudah ada)
   d. POST /api/trades closed ke web
   e. DELETE /api/positions di web
   f. Telegram notif retroactive (optional)
4. Remove dari active_positions.json

Usage di VPS:
    cd /home/eric/cryptovision-bot
    python3 scripts/recover_dangling.py --dry-run     # preview
    python3 scripts/recover_dangling.py               # apply
    python3 scripts/recover_dangling.py --no-notif    # apply tanpa Telegram spam
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

from bitunix_trader import BitunixTrader

ACTIVE_POS_FILE = ROOT / "data" / "active_positions.json"
TRADE_HIST_FILE = ROOT / "data" / "trade_history.json"

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true",
                    help="Preview tanpa apply changes")
parser.add_argument("--no-notif", action="store_true",
                    help="Skip Telegram notif (avoid spam 24x)")
parser.add_argument("--symbol", type=str, default=None,
                    help="Recover 1 symbol saja (testing)")
args = parser.parse_args()

# ── Load env ──────────────────────────────────────────────────────────
env_vars = {}
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env_vars[k.strip()] = v.strip().strip('"').strip("'")

token   = env_vars.get("TELEGRAM_BOT_TOKEN", "")
chat_id = env_vars.get("TELEGRAM_CHAT_ID", "")
web_url = env_vars.get("WEB_URL", "https://cryptovision-web.vercel.app")

# ── Backup ────────────────────────────────────────────────────────────
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
if not args.dry_run:
    for f in [ACTIVE_POS_FILE, TRADE_HIST_FILE]:
        if f.exists():
            backup = f.with_suffix(f"{f.suffix}.bak_dangling_{ts}")
            backup.write_bytes(f.read_bytes())
            print(f"Backup: {backup.name}")
    print()

# ── Load state ────────────────────────────────────────────────────────
active = json.loads(ACTIVE_POS_FILE.read_text())
history = []
if TRADE_HIST_FILE.exists():
    history = json.loads(TRADE_HIST_FILE.read_text())

trader = BitunixTrader()

# ── Get all currently OPEN positions di Bitunix (1 call efficient) ───
open_positions_list = trader.get_positions() or []
open_symbols = set()
for p in open_positions_list:
    raw_sym = p.get('symbol', '').upper()
    clean = raw_sym.replace('USDT', '')
    if clean:
        open_symbols.add(clean)

print(f"Bitunix OPEN positions: {sorted(open_symbols)}")
print(f"Local active_positions.json: {len(active)} entries")
print()


def fetch_history(symbol: str) -> list:
    """Get position history dari Bitunix (closed positions)."""
    sym = symbol.upper().replace('USDT', '') + 'USDT'
    try:
        data = trader._get('/api/v1/futures/position/get_history_positions',
                           {'symbol': sym, 'limit': 5})
        if not data or data.get('code') != 0:
            return []
        result = data.get('data', {})
        if isinstance(result, dict):
            return result.get('positionList') or result.get('list') or []
        if isinstance(result, list):
            return result
    except Exception as e:
        print(f"  fetch_history error: {e}")
    return []


def derive_outcome(realized_pnl: float, risk_amount: float) -> tuple:
    """Return (status, outcome_label, pnl_r) dari realizedPNL + risk."""
    if risk_amount <= 0:
        # No risk reference — fall back to sign-based
        if realized_pnl > 0.001:
            return ("TP1_HIT", "PROFIT", 1.0)
        if realized_pnl < -0.001:
            return ("SL_HIT", "LOSS", -1.0)
        return ("BEP", "BEP", 0.0)

    pnl_r = realized_pnl / risk_amount
    if pnl_r >= 1.5:
        return ("TP2_HIT", "PROFIT", round(pnl_r, 2))
    if pnl_r >= 0.8:
        return ("TP2_HIT", "PROFIT", round(pnl_r, 2))
    if pnl_r >= 0.3:
        return ("TP1_HIT", "PROFIT", round(pnl_r, 2))
    if pnl_r >= -0.2:
        return ("BEP", "BEP", round(pnl_r, 2))
    return ("SL_HIT", "LOSS", round(pnl_r, 2))


# ── Main loop ─────────────────────────────────────────────────────────
recovered = 0
skipped_open = 0
no_history = 0
already_in_history = 0

for sym, pos in list(active.items()):
    if args.symbol and sym.upper() != args.symbol.upper():
        continue

    # Skip kalau STILL OPEN di Bitunix (LDO, VANA, IO untuk hari ini)
    if sym.upper() in open_symbols:
        print(f"--- {sym}: STILL OPEN di Bitunix — skip")
        skipped_open += 1
        continue

    print(f"\n--- {sym} (DANGLING) ---")
    direction = pos.get("direction", "LONG")
    entry     = float(pos.get("entry", 0))
    sl_init   = float(pos.get("sl_initial", pos.get("sl", 0)))
    tp1       = float(pos.get("tp1", 0))
    tp2       = float(pos.get("tp2", 0))
    qty       = float(pos.get("qty", 0))
    quality   = pos.get("quality", "GOOD")
    strategy  = pos.get("_strategy", "swing")
    opened_at = pos.get("opened_at", "")

    # Idempotency: skip kalau sudah ada di trade_history.json (by symbol+opened)
    already_recorded = any(
        h.get('symbol') == sym and h.get('timestamp') == opened_at
        for h in history
    )
    if already_recorded:
        print(f"  ALREADY in trade_history (by {opened_at}) — skip")
        already_in_history += 1
        # Tetap remove dari active + delete web
        if not args.dry_run:
            del active[sym]
            secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
            try:
                requests.delete(f"{web_url}/api/positions",
                                params={"symbol": sym, "secret": secret}, timeout=8)
            except Exception:
                pass
        continue

    # Fetch Bitunix history
    hist = fetch_history(sym)
    print(f"  Bitunix history: {len(hist)} record(s)")

    realized_pnl = 0.0
    close_time   = ""
    avg_close    = 0.0

    if hist:
        latest = hist[0]  # most recent
        try:
            realized_pnl = float(latest.get('realizedPNL', latest.get('realized_pnl', 0)))
            ctime_ms = int(latest.get('mtime', latest.get('closeTime', latest.get('updateTime', 0))))
            if ctime_ms > 0:
                close_time = datetime.fromtimestamp(ctime_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')
            avg_close = float(latest.get('avgClosePrice', latest.get('avgClosedPrice', 0)) or 0)
        except (ValueError, TypeError):
            pass
        print(f"  realizedPNL=${realized_pnl:.4f} closeTime={close_time} avgClose={avg_close}")
    else:
        print(f"  no history fetched (API rate limit / not available)")
        no_history += 1
        # Best effort: assume BEP
        close_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    # Derive outcome
    risk_amount = abs(entry - sl_init) * qty if (entry and sl_init and qty) else 0
    status, outcome, pnl_r = derive_outcome(realized_pnl, risk_amount)
    print(f"  derived: status={status} outcome={outcome} pnl_r={pnl_r:+.2f}R")

    # Build closed trade entry
    next_id = max((t.get('id', 0) for t in history), default=0) + 1
    if not close_time:
        close_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    if avg_close <= 0:
        avg_close = tp2 if status == 'TP2_HIT' else (tp1 if status == 'TP1_HIT' else (sl_init if status == 'SL_HIT' else entry))

    closed_entry = {
        "id"          : next_id,
        "symbol"      : sym,
        "direction"   : direction,
        "quality"     : quality,
        "entry"       : entry,
        "sl"          : sl_init,
        "tp1"         : tp1,
        "tp2"         : tp2,
        "confluence"  : pos.get("confluence", pos.get("score", 0)),
        "rr1"         : 1.0,
        "rr2"         : float(pos.get("rr", 2.0)),
        "timestamp"   : opened_at,
        "status"      : status,
        "result_pnl"  : pnl_r,
        "closed_at"   : close_time,
    }

    if args.dry_run:
        print(f"  [dry-run] would add to history (id={next_id})")
        recovered += 1
        continue

    # Apply changes
    history.append(closed_entry)

    # POST trade ke web
    secret = hmac.new(token.encode(), sym.encode(), hashlib.sha256).hexdigest()
    trade_body = {
        "symbol"     : sym,
        "direction"  : direction,
        "strategy"   : strategy,
        "quality"    : quality,
        "entry"      : entry,
        "exit_price" : avg_close,
        "sl"         : sl_init,
        "tp1"        : tp1,
        "tp2"        : tp2,
        "pnl_usd"    : round(realized_pnl, 4),
        "pnl_r"      : pnl_r,
        "outcome"    : outcome,
        "bep_done"   : status in ('TP1_HIT', 'TP2_HIT', 'BEP'),
        "opened_at"  : opened_at or close_time,
        "closed_at"  : close_time,
        "secret"     : secret,
    }
    try:
        r = requests.post(f"{web_url}/api/trades", json=trade_body, timeout=10)
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

    # Telegram notif (optional, skip kalau --no-notif)
    if not args.no_notif and chat_id and token:
        ico = "✅" if outcome == "PROFIT" else ("⚪" if outcome == "BEP" else "❌")
        msg = (
            f"{ico} <b>{sym} {direction} {status}</b> (recovery)\n"
            f"Entry: {entry} → Close: {avg_close}\n"
            f"PnL: <b>{pnl_r:+.2f}R</b> (~${realized_pnl:+.4f})\n"
            f"Opened: {opened_at}\n"
            f"Closed: {close_time}"
        )
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=8,
            )
        except Exception:
            pass
        # Throttle untuk avoid Telegram rate limit (30 msg/sec)
        time.sleep(0.3)

    # Remove dari active
    del active[sym]
    recovered += 1

# ── Persist updated state ─────────────────────────────────────────────
if not args.dry_run:
    ACTIVE_POS_FILE.write_text(json.dumps(active, indent=2))
    TRADE_HIST_FILE.write_text(json.dumps(history, indent=2))
    print()
    print(f"Saved:")
    print(f"  {ACTIVE_POS_FILE.name} — {len(active)} entries remaining")
    print(f"  {TRADE_HIST_FILE.name} — {len(history)} entries total")

print("\n" + "=" * 60)
print(f"Summary:")
print(f"  Recovered (added to history)  : {recovered}")
print(f"  Skipped (still open Bitunix)  : {skipped_open}")
print(f"  Already in history (dedup)    : {already_in_history}")
print(f"  No Bitunix history (best-eff) : {no_history}")
print("=" * 60)
if args.dry_run:
    print("\n[DRY-RUN] no changes applied")
