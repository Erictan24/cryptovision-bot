# SCALP Removal — Audit File & Cross-Imports
Generated: 2026-05-13

## Files to DELETE (12 files SCALP-specific)

### Code Files
1. `scalping_signal_engine.py` (137 KB, signal engine)
2. `scalping_signal_engine.py.backup_pre_range_mode` (109 KB, backup pre-RANGE mode)
3. `scalp_live_runner.py` (live scan executor)
4. `scalp_paper_trader.py` (paper trader for testing)
5. `scalp_session_filter.py` (session-based filter)
6. `scalp_trade_journal.py` (SCALP trade DB writer)
7. `scalp_coin_learning.py` (per-coin learning engine)
8. `main_scalp.py` (SCALP-only entry point)
9. `main_unified.py` (dual-engine entry point, no longer needed)
10. `trading_engine_scalp.py` (SCALP trading wrapper)
11. `backtest_scalp.py` (SCALP backtest)
12. `diag_scalp.py` (SCALP diagnostic tool)
13. `dashboard_api.py` (SCALP-only stats dashboard)

### Data Files
- `data/scalp_trades.db`
- `data/scalp_paper.db`
- `data/scalp_coin_params.json`
- `data/scalp_coin_params.json.backup`

### Backtest Artifacts (35+ files)
- `backtesting/results/scalp_backtest_*.pkl` (35+ files dari 23 April - 11 May)
- `backtesting/cache/scalp_data*.pkl` (4 files)

### Pycache
- `__pycache__/scalp_*.pyc`
- `__pycache__/scalping_signal_engine.pyc`

## Files to MODIFY (4 files)

### 1. `main.py`
**Hapus 2 lokasi:**
- **Line 69-70:** call `self._start_scalp_paper()`
  ```python
  # Start scalp paper trade scanner (15 menit interval)
  self._start_scalp_paper()
  ```
- **Line 199-232:** method `_start_scalp_paper()` lengkap (33 baris)

### 2. `telegram_bot.py`
**Hapus 4 lokasi:**
- **Line 280:** command registration
  ```python
  ("scalp_stats", self.cmd_scalp_stats),
  ```
- **Line 448-451, 463:** help text references
  ```
  "─── 📝 PAPER TRADE SCALP ───────────\n"
  "  /scalp_stats         — Rekap signal scalp paper\n"
  ...
  "  • /scalp_stats — cek paper trade scalp\n"
  ```
- **Line 553-569:** method `cmd_scalp_stats` lengkap
- **Line 2667-2676:** Per-strategy breakdown (KEEP — bisa tetap tampil SWING aja)

### 3. `config.py`
**Hapus 3 block:**
- **Line 287-396:** SCALP_CONFIG dict (~110 baris)
- **Line 402-415:** UNIFIED_CONFIG scalp_enabled / tag_scalp_signal refs
- Cleanup global_max_positions logic (kalau perlu)

### 4. `backtest_unified.py`
**Modifikasi:**
- Remove `from scalping_signal_engine import generate_scalping_signal`
- Remove `run_scalp_backtest` function
- Remove `scalp` dari `--engines` choices
- Simplify menjadi swing-only

## Files to KEEP (shared infrastructure — JANGAN sentuh)

### Core SWING engine
- `signal_generator.py` (SINGLE SOURCE OF TRUTH SWING)
- `trading_engine.py`
- `main.py` (modify saja, jangan delete)
- `telegram_bot.py` (modify saja)
- `config.py` (modify saja)

### Bitunix integration
- `bitunix_trader.py` (shared, JANGAN sentuh)

### Shared utilities
- `database.py`, `chart_generator.py`, `news_filter.py`
- `indicators.py`, `candle_patterns.py`, `chart_patterns.py`, `chart_pattern_signals.py`
- `session_filter.py` (BEDA dari scalp_session_filter — keep)
- `volume_delta.py`, `whale_analyzer.py`, `smc_analyzer.py`, `sr_detector.py`
- `learning_engine.py`, `level_memory.py`, `historical_trainer.py`
- `ai_analyzer.py`, `analyze_expired.py`, `compare_rr_variants.py`
- `momentum_detector.py`, `reversal_detector.py`
- `clean_signal.py`, `signal_arbitrator.py` (review usage, mungkin bisa di-prune lebih)
- `position_manager.py`
- `risk_manager.py`
- `smart_coin_selector.py`
- `backtesting/` folder (modify backtest_unified.py)

### Data to KEEP
- `data/trades.db` (SWING signal log)
- `data/learning.db` (review — might have SCALP rows but mixed; keep filter)
- `data/unified_positions.db` (SHARED — pakai `DELETE WHERE engine='SCALP'`)
- `data/active_positions.db` (kalau ada, sama treatment)
- `backtesting/results/unified_*.pkl` (kontain SWING data juga)
- `backtesting/cache/unified_data.pkl` (universe coin, dipakai SWING)

## Cross-Import Map

Files that import SCALP modules (sumber import → harus di-bersihkan):

| File | Imports SCALP from | Action |
|------|---------------------|--------|
| `main.py` | `scalp_live_runner` (line 208) | Hapus method + call |
| `telegram_bot.py` | `scalp_live_runner` (line 556) | Hapus method + command |
| `backtest_unified.py` | `scalping_signal_engine` (line 40) | Hapus import + function |
| `backtest_scalp.py` | `scalping_signal_engine` (line 34) | DELETE seluruh file |
| `diag_scalp.py` | `scalp_live_runner` (line 10) | DELETE seluruh file |
| `trading_engine_scalp.py` | `scalping_signal_engine` (line 19) | DELETE seluruh file |
| `scalp_*.py` (5 files) | Saling import | DELETE semua |

## Web Database (Neon Postgres)

Sesuai schema di `website/src/lib/db.ts`:
- `trades` table → `DELETE WHERE strategy='scalp'`
- `signals` table → `DELETE WHERE strategy='scalp'`
- `positions` table → `DELETE WHERE strategy='scalp'`

SWING records pakai `strategy='swing'` → tetap utuh.

## Verification Post-Deploy

```bash
# 1. Cek scan log VPS — cuma SWING
ssh VPS "grep -i scalp /home/eric/cryptovision-bot/logs/bot.log | tail -5"
# Expected: 0 line (atau cuma legacy/historical lines)

# 2. Cek file SCALP udah hilang
ls /home/eric/cryptovision-bot/scalp_*.py 2>&1
# Expected: No such file

# 3. Cek web stats — cuma SWING numbers
curl -s https://cryptovision-web.vercel.app/api/stats | python -m json.tool

# 4. Cek active positions
curl -s https://cryptovision-web.vercel.app/api/positions | python -c "import json,sys; d=json.load(sys.stdin); print([p['strategy'] for p in d['positions']])"
# Expected: ['swing', 'swing', ...] only
```
