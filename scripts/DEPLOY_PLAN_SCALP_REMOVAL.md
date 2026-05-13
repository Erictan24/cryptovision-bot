# SCALP Removal — Final Deploy Plan

**Prep date:** 2026-05-13 (selesai 23:58 local)
**Eksekusi:** Setelah backtest `tp1_rr_max 1.5` validate

## Pre-Conditions Checklist (sebelum eksekusi)

- [ ] Backtest `swing_tp1_validate.log` selesai
- [ ] Hasil backtest divalidate vs baseline (WR ≥ 60%, EV ≥ +0.5R)
- [ ] Cek Bitunix dashboard: 0 open SCALP position (sudah confirmed 2026-05-13: hanya ada 2 SWING — ACE LONG + LYN SHORT)
- [ ] Backup folder ada: `scripts/scalp_backup_20260513_2358/` (4576 trades + config tersimpan)

## Step-by-Step Execution

### Step 1: Validate backtest result
```bash
tail -30 swing_tp1_validate.log
# Cek COMPARISON vs BASELINE section
# Decision:
#   WR/EV naik    → GO deploy
#   WR/EV stabil  → GO deploy (fix solve psikologis stress di problem zone)
#   WR/EV turun   → REVERT config.py:204 dulu, skip deploy fix
```

### Step 2: Create git branch
```bash
git checkout -b remove-scalp
git status  # confirm tp1_rr_max change masih ada
```

### Step 3: Commit 1 — config fix
```bash
git add config.py
git commit -m "feat: cap tp1_rr_max at 1.5 to avoid wide TP1 trap zone

Backtest 90d top200 nunjukin trade RR1 1.5-2.0 WR cuma 32.5% (n=40):
- TP1_HIT 7.5% (vs 39.7% di healthy zone)
- SL_HIT 62.5% (vs 26.7%)
- Setup dominan: SHORT counter-trend di Discount Zone

Fix: cap di 1.5, default TP1 1.2R tetap, TP2 path independent."
```

### Step 4: Commit 2 — clean up main.py + telegram_bot.py refs
Edit files (lihat SCALP_REMOVAL_AUDIT.md untuk detail line):
- `main.py`: hapus line 69-70 + method `_start_scalp_paper` (line 199-232)
- `telegram_bot.py`: hapus command + help text + method `cmd_scalp_stats`
- `config.py`: hapus SCALP_CONFIG dict + UNIFIED_CONFIG SCALP refs

```bash
git add main.py telegram_bot.py config.py
git commit -m "chore: remove SCALP references from main entry points"
```

### Step 5: Commit 3 — delete SCALP code files
```bash
git rm \
  scalping_signal_engine.py \
  scalping_signal_engine.py.backup_pre_range_mode \
  scalp_live_runner.py \
  scalp_paper_trader.py \
  scalp_session_filter.py \
  scalp_trade_journal.py \
  scalp_coin_learning.py \
  main_scalp.py \
  main_unified.py \
  trading_engine_scalp.py \
  backtest_scalp.py \
  diag_scalp.py \
  dashboard_api.py

# Modify backtest_unified.py — remove SCALP paths
# (manual edit, then git add)

git commit -m "chore: remove SCALP engine files

Files deleted:
- scalping_signal_engine.py + backup
- scalp_*.py (5 helpers)
- main_scalp.py, main_unified.py
- trading_engine_scalp.py, backtest_scalp.py, diag_scalp.py
- dashboard_api.py (SCALP-only dashboard)

Total: 13 code files, ~415 KB removed."
```

### Step 6: Commit 4 — delete local data
```bash
python scripts/delete_scalp_data.py --execute

# Akan delete:
# - data/scalp_*.db (2 files)
# - data/scalp_coin_params.json + backup
# - 67 backtest artifact files (362 MB!)
# - 13 pycache files
# - 0 rows di unified_positions.db (no SCALP positions)

git add -A
git commit -m "chore: delete SCALP data files

Backup ada di scripts/scalp_backup_20260513_2358/
- 4576 SCALP trades backed up
- 362 MB backtest cache cleared"
```

### Step 7: Merge + push
```bash
git checkout main
git merge remove-scalp --no-ff -m "Remove SCALP engine, focus full SWING"
git push origin main
git branch -d remove-scalp
```

### Step 8: Deploy to VPS
```bash
ssh eric@VPS_IP
cd /home/eric/cryptovision-bot
git pull
# Restart bot
sudo systemctl restart cryptovision-bot
# atau: pkill -f main.py && nohup python main.py > bot.log 2>&1 &
tail -f bot.log  # verify no error, only SWING signals
```

### Step 9: Web Postgres cleanup (di Neon console)
Buka https://console.neon.tech, pilih project, open SQL Editor:

```sql
-- 1. Count dulu (safety check)
SELECT COUNT(*) FROM trades   WHERE strategy='scalp';
SELECT COUNT(*) FROM signals  WHERE strategy='scalp';
SELECT COUNT(*) FROM positions WHERE strategy='scalp';

-- 2. DELETE kalau count sesuai expectation (~4500 trade)
DELETE FROM trades   WHERE strategy='scalp';
DELETE FROM signals  WHERE strategy='scalp';
DELETE FROM positions WHERE strategy='scalp';

-- 3. Verify SWING count masih utuh
SELECT COUNT(*) FROM trades WHERE strategy='swing';
```

### Step 10: Verify deployment

```bash
# 1. Bot log clean (no scalp)
ssh VPS "grep -i scalp /home/eric/cryptovision-bot/logs/bot.log | tail -5"
# Expected: 0 line atau cuma historical

# 2. Web stats — cuma SWING numbers
curl -s https://cryptovision-web.vercel.app/api/stats | python -m json.tool

# 3. Web positions cuma SWING
curl -s https://cryptovision-web.vercel.app/api/positions | python -m json.tool
```

## Rollback Plan (kalau ada issue)

```bash
# Step A: Revert git
git revert HEAD~4..HEAD  # revert 4 commits sekaligus
git push

# Step B: Restore data from backup
python -c "
import json, sqlite3
backup = 'scripts/scalp_backup_20260513_2358'
# (write restore logic from backup JSON files)
"

# Step C: Re-enable di VPS
ssh VPS "cd /home/eric/cryptovision-bot && git pull && sudo systemctl restart cryptovision-bot"
```

## Total Impact

- **Files deleted:** 13 code files + 67 backtest artifacts + 13 pycache = 93 files
- **Size freed:** ~365 MB local
- **DB rows:** 4576 SCALP trades (local + web)
- **VPS impact:** No SCALP scan, only SWING (cleaner logs)
- **Web stats:** Pure SWING numbers
- **Code complexity:** Reduce ~300 KB of code maintenance
