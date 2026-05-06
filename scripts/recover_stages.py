#!/usr/bin/env python3
"""Recovery Stage 2/3 trailing untuk position yang missed akibat risk_dist bug.

Bug context (2026-05-07):
- Bot internal risk_dist = abs(tp1 - entry) — salah asumsi RR=1
- Untuk swing trade RR 2-5x, Stage 2 trigger jadi jauh lebih tinggi
  dari yang seharusnya, akibatnya never fire
- Contoh LDO: bot trigger 0.396 instead of 0.371 (real +1.5R) →
  Stage 2 SL stuck di BEP padahal harga sudah lewat +1.5R sejak lama

Script ini:
1. Tambah field `sl_initial` ke semua position di active_positions.json
2. Loop semua position bep_done, hitung +1.5R/+2.0R pakai sl_initial benar
3. Cek price extreme 24 jam — kalau pernah lewat trigger, AUTO-PROMOTE
   (move SL ke +0.5R atau +1R, set stage2_done/stage3_done)
4. Update local + Bitunix exchange + web

Usage di VPS:
    cd /home/eric/cryptovision-bot
    python3 scripts/recover_stages.py [--dry-run]
"""
import os
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(str(ROOT / ".env"))

from bitunix_trader import BitunixTrader

ACTIVE_POS_FILE = ROOT / "data" / "active_positions.json"

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true",
                    help="Preview changes tanpa apply")
parser.add_argument("--symbol", type=str, default=None,
                    help="Recover 1 symbol saja (testing)")
args = parser.parse_args()

# ── Backup state ──────────────────────────────────────────────────────
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = ACTIVE_POS_FILE.with_suffix(f".json.bak_{ts}")
if not args.dry_run:
    backup.write_bytes(ACTIVE_POS_FILE.read_bytes())
    print(f"Backup: {backup.name}\n")

active = json.loads(ACTIVE_POS_FILE.read_text())
trader = BitunixTrader()

print("=" * 70)
print(f"Recovery Stage 2/3 — {len(active)} positions to evaluate")
print("=" * 70)

migrated_count   = 0
auto_promoted    = 0
already_correct  = 0
no_action_count  = 0
not_in_exchange  = 0

for sym, pos in list(active.items()):
    if args.symbol and sym.upper() != args.symbol.upper():
        continue

    direction = pos.get("direction", "LONG")
    entry     = float(pos.get("entry", 0))
    tp1       = float(pos.get("tp1", 0))
    tp2       = float(pos.get("tp2", 0))
    sl_curr   = float(pos.get("sl", 0))
    sl_init   = float(pos.get("sl_initial", 0))
    bep_done_state    = pos.get("bep_done")
    stage2_done_state = pos.get("stage2_done")
    stage3_done_state = pos.get("stage3_done")

    print(f"\n--- {sym} ({direction}) ---")
    print(f"  entry={entry} tp1={tp1} sl_curr={sl_curr} sl_init={sl_init or 'MISSING'}")
    print(f"  state: bep={bep_done_state} s2={stage2_done_state} s3={stage3_done_state}")

    # Verify position exists at exchange
    bitunix_pos = trader.get_open_position(sym)
    if not bitunix_pos:
        print(f"  SKIP: position not in Bitunix (dangling state)")
        not_in_exchange += 1
        continue

    # ── Step 1: migrate sl_initial if missing ────────────────────────
    if sl_init <= 0:
        # Heuristik: kalau Stage 1/2/3 belum done, sl_curr === sl_initial
        # Kalau ada stage shift, sl_curr sudah moved → cant infer original.
        # Fallback: hitung dari TP1 + RR ratio kalau ada.
        if not stage2_done_state and not stage3_done_state:
            # SL belum di-shift, sl_curr == original
            sl_init = sl_curr
            print(f"  MIGRATE sl_initial = {sl_init} (from sl, untouched)")
        else:
            # SL sudah shifted, original lost. Pakai default RR1 (=tp1-entry):
            sl_init = entry - abs(tp1 - entry) if direction == "LONG" else entry + abs(tp1 - entry)
            print(f"  MIGRATE sl_initial = {sl_init} (estimated from tp1, RR=1 fallback)")

        if not args.dry_run:
            pos["sl_initial"] = sl_init
        migrated_count += 1

    # ── Step 2: PRECONDITION — TP1 harus hit dulu sebelum Stage 2/3 ─
    # Stage 2/3 secara semantik butuh TP1 hit (qty partial closed) dulu.
    # Tanpa precondition ini, script bisa set SL di profit zone saat trade
    # masih running negative — bahaya (kasus IO 2026-05-07: SL ke-set 0
    # akibat crash, plus auto-promote berbasis extreme yang misleading).
    initial_qty = float(pos.get("qty", 0))
    actual_qty  = float(bitunix_pos.get("qty", 0))
    qty_reduced = initial_qty > 0 and actual_qty < initial_qty * 0.7
    tp1_hit_flag = bool(pos.get("tp1_hit", False))
    if not (tp1_hit_flag or qty_reduced):
        print(f"  SKIP Stage promotion: TP1 belum hit "
              f"(initial_qty={initial_qty}, actual_qty={actual_qty}, tp1_hit={tp1_hit_flag})")
        no_action_count += 1
        continue

    # ── Step 3: hitung trigger +1.5R / +2R yang BENAR ────────────────
    risk_dist = abs(entry - sl_init)
    if risk_dist <= 0:
        print(f"  SKIP: risk_dist invalid ({risk_dist})")
        no_action_count += 1
        continue

    is_long = direction == "LONG"
    trigger_2 = entry + 1.5 * risk_dist if is_long else entry - 1.5 * risk_dist
    trigger_3 = entry + 2.0 * risk_dist if is_long else entry - 2.0 * risk_dist
    sl_lock_2 = entry + 0.5 * risk_dist if is_long else entry - 0.5 * risk_dist
    sl_lock_3 = entry + 1.0 * risk_dist if is_long else entry - 1.0 * risk_dist

    # ── Step 3: cek price extreme 24h ────────────────────────────────
    extreme_24h = trader._get_price_extreme_since(f"{sym}USDT", 24, direction)
    current_price = trader._get_current_price(f"{sym}USDT")
    print(f"  risk_dist={risk_dist:.6g} +1.5R={trigger_2:.6g} +2R={trigger_3:.6g}")
    print(f"  current={current_price} extreme_24h={extreme_24h}")

    # Pernah lewat Stage 3?
    ext_hit_s3 = (is_long and extreme_24h >= trigger_3) or (not is_long and extreme_24h > 0 and extreme_24h <= trigger_3)
    # Pernah lewat Stage 2?
    ext_hit_s2 = (is_long and extreme_24h >= trigger_2) or (not is_long and extreme_24h > 0 and extreme_24h <= trigger_2)

    target_stage = None
    target_sl    = None
    if ext_hit_s3 and not stage3_done_state:
        target_stage = 3
        target_sl = sl_lock_3
    elif ext_hit_s2 and not stage2_done_state and not stage3_done_state:
        target_stage = 2
        target_sl = sl_lock_2

    if target_stage is None:
        print(f"  no action — price not yet hit Stage 2 trigger")
        no_action_count += 1
        continue

    # Cek apakah SL exchange sudah di posisi yang diinginkan (idempotency)
    sym_full = f"{sym}USDT"
    pos_id = bitunix_pos.get('positionId', '')
    exchange_sl = trader._get_current_sl(sym_full, str(pos_id))

    # Tolerance 0.5%
    if exchange_sl > 0 and abs(exchange_sl - target_sl) / target_sl < 0.005:
        print(f"  ALREADY CORRECT — exchange SL {exchange_sl} ≈ target {target_sl}, just sync state")
        if not args.dry_run:
            if target_stage >= 2:
                pos["bep_done"] = True
                pos["stage2_done"] = True
            if target_stage >= 3:
                pos["stage3_done"] = True
            pos["sl"] = target_sl
        already_correct += 1
        continue

    # ── Step 4: AUTO-PROMOTE — move SL to target ─────────────────────
    print(f"  AUTO-PROMOTE Stage {target_stage}: SL {exchange_sl} → {target_sl} (+{(target_stage-1)*0.5:.1f}R locked)")
    if args.dry_run:
        print(f"  [dry-run] skip move_sl_trailing")
    else:
        r = trader.move_sl_trailing(sym, target_sl)
        if r.get('ok'):
            print(f"  OK move_sl_trailing")
            pos["bep_done"] = True
            pos["stage2_done"] = True
            if target_stage >= 3:
                pos["stage3_done"] = True
            pos["sl"] = target_sl
            # Patch web
            try:
                trader._patch_position_state(sym, sl=target_sl)
            except Exception as e:
                print(f"  patch web error: {e}")
            auto_promoted += 1
        else:
            print(f"  FAIL move_sl_trailing: {r.get('msg')}")

# ── Save updated active_positions.json ────────────────────────────────
if not args.dry_run:
    ACTIVE_POS_FILE.write_text(json.dumps(active, indent=2))
    print(f"\nSaved active_positions.json")

print("\n" + "=" * 70)
print(f"Summary:")
print(f"  Migrated sl_initial : {migrated_count}")
print(f"  Auto-promoted Stage : {auto_promoted}")
print(f"  Already correct     : {already_correct}")
print(f"  No action needed    : {no_action_count}")
print(f"  Not in exchange     : {not_in_exchange}")
print("=" * 70)
if args.dry_run:
    print("\n[DRY-RUN] No changes applied. Re-run without --dry-run to apply.")
