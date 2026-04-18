# CryptoVision Bot — Context untuk Claude Code

## Identitas Bot
Bot trading crypto swing trader (1H + 4H) untuk Bitunix Futures.
Username Telegram: CryptoVisionID
Folder: C:\Users\erict\OneDrive\crypto_bot_v2

## Stack
- Python, Telegram Bot API, Bitunix Futures API
- Data: Binance Futures (primary), CryptoCompare (fallback)

## File Utama
- main.py → entry point, auto scan 30 menit
- trading_engine.py → analisa + signal + semua filter + wrapper ke signal_generator
- signal_generator.py → SINGLE SOURCE OF TRUTH scoring & filter (semua filter WR 60% di sini)
- telegram_bot.py → Telegram interface + auto trade
- bitunix_trader.py → koneksi Bitunix, BEP, TP1 monitor
- chart_generator.py → generate chart PNG untuk signal
- clean_signal.py → signal engine Fib+4H+candle (fallback kalau main signal None)
- news_filter.py → economic calendar filter
- candle_patterns.py → 35+ candlestick pattern
- chart_pattern_signals.py → 14 chart pattern
- momentum_detector.py → 4 momentum setup

## Konfigurasi Trading
- Risk per trade: $3 flat
- Daily loss limit: $15
- Max posisi: 5
- Leverage: 10x
- Timeframe: 1H (main), 4H (HTF), 15m (LTF trigger)

## Instruksi Komunikasi
- Selalu gunakan Bahasa Indonesia
- Jelaskan setiap perubahan: Apa → Kenapa → Dampak ke bot
- Bahasa sederhana untuk trader, bukan programmer

## Target
- WR 60%+ (tercapai: 63.8-67.3% di backtest)
- EV positif per trade (+0.56R terakhir)
- Volume signal: target 30+/bulan (sedang ditingkatkan)

## Arsitektur Signal (setelah upgrade April 2026)

### Signal Flow
```
analyze_coin()
  → generate_entry_signal()  [signal_generator.py — PRIMARY]
      → _score_direction()    scoring 30+ faktor
      → _determine_quality()  GOOD/MODERATE/WAIT berdasarkan score + kills
      → _apply_rejection_gate()  wajib candle rejection
      → HTF Alignment Gate    GOOD wajib HTF EMA/CHoCH
      → Whale Flow Filter     block LONG saat HTF bearish tanpa reversal sign
  → generate_clean_signal()  [clean_signal.py — FALLBACK, kalau main=None]
      → filter WR 60% diterapkan di analyze_coin sebelum dipakai
      → quality di-cap ke MODERATE
```

### Filter WR 60% (4 Fix dari Post-Mortem 179 trades)
1. **Fix #1 — Accumulation Kill** (signal_generator.py, _score_direction)
   - LONG + fase Accumulation = KILL factor (dulu: +1 score)
   - Data: 100% LONG SL rate di Accumulation tanpa Markup
   - Hanya MARKUP phase yang jadi green light untuk LONG

2. **Fix #2 — BOS 1H Wajib HTF** (signal_generator.py, _score_direction)
   - BOS 1H tanpa HTF BOS searah = KILL factor (false breakout)
   - Data: BOS 1H saja WR 22% (7 SL dari 9 closed trades)
   - BOS 1H + HTF BOS baru dikasih score

3. **Fix #3 — RSI Buffer Zone** (config.py)
   - rsi_extreme_low: 30→32, rsi_extreme_high: 70→68
   - Block near-extreme (jebakan "hampir oversold/overbought")

4. **Fix #5 — ADX Precision Filter** (config.py + signal_generator.py)
   - Block ADX 35-44 (death zone WR 20-33%) dan ADX >=50 (WR 0%)
   - Allow ADX 45-49 (sweet spot kedua, WR 75%)
   - Sweet spot utama: ADX 25-34 (WR 67-72%)

### Config Penting (SIGNAL_PARAMS)
- score_good: 20 | score_moderate: 17
- max_kills_good: 0 | max_kills_moderate: 1
- rsi_extreme_low: 32 | rsi_extreme_high: 68
- adx_death_zone: 35-44 | adx_too_extreme: 50
- tp1_rr_min: 1.2 | tp2_rr_min: 2.0

## Bug Kritis yang Diperbaiki (April 2026)

### Bug #1 — Scoring Engine Ganda
trading_engine.py punya generate_entry_signal internal 800 baris dengan scoring hardcoded
(GOOD=score>=7) yang bypass semua SIGNAL_PARAMS. Dihapus, diganti wrapper ke signal_generator.py.

### Bug #2 — EMA Cross Bonus Double-Counting
analyze_coin kasih +8/+15 bonus score untuk EMA cross, padahal EMA sudah discore di
_score_direction(). Bikin score melewati hard_reject 24. Dihapus.

### Bug #3 — BacktestEngine Missing Attributes
_whale_cache, _SIGNAL_LOCK_HOURS, _ZONE_PERSIST_HOURS, _WHALE_TTL tidak ada.
90% scan error = semua backtest sebelumnya TIDAK VALID.

### Bug #4 — Per-Coin Config Melonggarkan Threshold
per_coin_config override score_good=6 dari profil lama, bypass filter baru.
Fix: max(override, base) — per-coin hanya boleh memperketat.

### Bug #5 — report.py Unicode Crash Windows
Karakter non-ASCII crash di cp1252. Diganti ASCII.

### Bug #6 — Clean Signal Override Tanpa Filter
Clean_signal scoring 0-100 bypass filter WR 60%.
Fix: re-enable sebagai fallback saja + filter diterapkan di analyze_coin.

## Penemuan Penting dari Data

### WR by Score Range (179 trades baseline)
- Score 18-19: WR 64-67% (sweet spot)
- Score 20: WR 44% (anomali, sampel kecil)
- Score 21: WR 86-100% (terbaik!)
- Score 22-23: WR 43-83% (bervariasi)
- Score 24+: dead zone (hard reject aktif)

### WR by Kill Count
- 0 kills: WR 67% (baseline stabil)
- 1 kill: WR 70% (LEBIH TINGGI dari 0 — kill sebagai "notes" deskriptif)
- 2 kills: WR 60% (masih positif tapi edge kecil)

### WR by ADX
- ADX 25-29: WR 72% (optimal)
- ADX 30-34: WR 67% (masih bagus)
- ADX 35-44: WR 20-33% (DEATH ZONE — diblock)
- ADX 45-49: WR 75% (sweet spot kedua — allowed)
- ADX 50+: WR 0% (diblock)

### LONG vs SHORT
- LONG: WR 71-86% (setelah Accumulation kill, sangat selektif)
- SHORT: WR 57-64% (lebih banyak volume)
- LONG kena SL 35% lebih cepat dari SHORT (avg 8 bars vs 12.3 bars)

### Faktor Jebakan (lebih sering di SL)
- Accumulation phase + LONG (100% SL rate!)
- BOS 1H tanpa HTF BOS (WR 22%)
- Volume confirmation bias
- Engulfing tanpa konteks

### Faktor Juara (lebih sering di TP2)
- Pin Bar (+11% bias ke TP2)
- RSI Divergence (+10% bias)
- HTF BOS (+7% bias)
- HTF EMA aligned (+7% bias)

## Backtest Results History

### Baseline (sebelum fix)
- 179 trades, WR 54.5%, EV +0.44R

### Setelah 4 Fix (1h only, 30 coin, 180 hari)
- 97 trades, WR 67.3%, EV +0.62R, 16/bulan

### Setelah + 4h TF + Opsi B ADX (1h+4h, 30 coin, 180 hari)
- 126 trades, WR 63.8-65.6%, EV +0.56R, 21/bulan
- 1h: WR 64.6%, 4h: WR 60.0%

### TP1 Enforcement (reject kalau resistance dekat)
- TERLALU KETAT — 13 trades saja (WR 82% tapi tidak usable)
- REVERTED — TP1 tetap di-cap oleh resistance terdekat

### Clean Signal Fallback (GAGAL)
- Re-enable clean_signal sebagai fallback: 1154 signal tapi WR hanya 32% (vs main 62.7%)
- Filter superfisial di analyze_coin tidak cukup — scoring Fib 0-100 fundamentally beda
- DINONAKTIFKAN DEFINITIF — butuh rewrite total agar pakai _score_direction()

### Volume Issue
- 20 signal/bulan — penyebab: signal hanya trigger saat harga DI zona S&R + rejection
- generate_limit_signal gagal: level matching terlalu rigid (0 candidate dari 13 level)
- Volume unlock butuh rewrite clean_signal pakai scoring yang sama (Prioritas 5, bulan depan)

## Fitur Live Trading (April 2026)

### Trailing Stop — SUDAH ADA di bitunix_trader.py
3 stage otomatis di start_tp1_monitor():
- Stage 1 (TP1 hit): SL → BEP (break even)
- Stage 2 (harga +1.5R): SL → entry + 0.5R (lock profit)
- Stage 3 (harga +2.0R): SL → entry + 1.0R (lock profit penuh)
Note: Backtest TIDAK simulasikan trailing ini. WR live bisa lebih tinggi dari backtest.

### Correlation Filter — BARU (telegram_bot.py)
Cluster-aware anti-correlation:
- Max 3 posisi arah sama (general)
- Max 1 posisi per cluster coin (L1: ETH/SOL/AVAX/APT/SUI/SEI/TON/DOT/ATOM, L2: ARB/OP/POL, DEFI: INJ/PENDLE/FET/UNI, MEME: DOGE/WLD/BLUR)
- Mencegah cluster loss (misal 3 L1 coin semua SHORT → 1 saja)

### Risk Scaling — FIXED
- Dulu GOOD hardcode $1, IDEAL $2 (terlalu konservatif, terbalik)
- Sekarang semua tier pakai risk_usd dari .env ($3 default)
- Compound growth: naikkan TRADE_RISK_USD di .env seiring balance naik

## Hal yang Tidak Boleh Dilakukan
- JANGAN tambah scoring engine baru di trading_engine.py — semua lewat signal_generator.py
- JANGAN override SIGNAL_PARAMS dengan hardcoded value
- JANGAN kasih score ke Accumulation phase untuk LONG (itu jebakan)
- JANGAN percaya BOS 1H tanpa HTF BOS (false breakout)
- JANGAN entry saat ADX 35-44 (death zone) atau ADX >= 50
- JANGAN force TP1 minimum kalau resistance dekat — reject 90% signal
- JANGAN per-coin config melonggarkan threshold (hanya boleh memperketat)
- JANGAN re-enable clean_signal sebelum rewrite pakai _score_direction()

## Pencapaian (April 2026)

### Sebelum Upgrade
- WR: 35-42% (rugi)
- EV: ~0R per trade
- Filter saling tumpang tindih, banyak bug tersembunyi
- Backtest tidak valid (90% scan error tidak terdeteksi)

### Setelah Upgrade (2026-04-13)
- **WR backtest: 62.7%** (target 60% TERCAPAI)
- **EV: +0.51R per trade**
- **Volume: 20 signal/bulan** (selektif, berkualitas)
- **Profit estimasi: $31/bulan at $3 risk**
- 6 bug kritis diperbaiki
- 4 filter baru dari forensic analysis 179 trades
- Single source of truth untuk scoring (signal_generator.py)
- Backtest engine valid + reproducible
- Live trading features aktif:
  - Trailing stop 3-stage (BEP → +0.5R → +1.0R)
  - Cluster correlation filter (L1/L2/DEFI/MEME)
  - Risk scaling via TRADE_RISK_USD env

### Target Berikutnya
- Validasi WR live 2-3 minggu paper trade
- Rewrite clean_signal untuk volume 2-3x (~bulan depan)

## Fase Live Validation (2026-04-14)

### Status
- Bot LIVE di Bitunix dengan balance $296.65
- TRADE_ENABLED=true, auto scan tiap 30 menit
- 30 trade sebelum upgrade: WR 30%, Net -$23.97 (data versi lama, abaikan)
- Mulai validasi dari 0 setelah restart dengan filter WR 60% aktif

### Risk Config Validasi
- `TRADE_RISK_USD=1` di `.env` (turun dari default $3)
- 0.34% per trade dari balance $296
- Max daily loss $10 = 10 SL beruntun sebelum stop
- Profit target kecil (~$10/bulan) tapi AMAN untuk validasi

### Roadmap Risk Scaling (setelah validasi)
| Fase | Kriteria | Risk |
|------|----------|------|
| Validasi M1-2 | Sekarang | $1 |
| M3-4 | 20+ trade, WR ≥55% | $2 |
| Bulan 2 | 40+ trade, WR ≥58% | $3 |
| Bulan 3+ | 60+ trade, WR ≥60% | $5 |
| Bulan 6+ | Profit stabil 3 bulan | 2% balance (compound) |

### Bug Fix Display (2026-04-14)
- `bitunix_trader.py` line 1335 — hapus hardcode "IDEAL=$2 | GOOD=$1 per trade"
- Sekarang display reflect TRADE_RISK_USD dari .env secara akurat
- Status `/trade` sekarang menunjukkan "$1 flat per trade"

### Posisi Terbuka Saat Migrasi
- **XPLUSDT SHORT x20** entry 0.1259, mark 0.1269 (loss -15.7%)
- Dibiarkan jalan (keputusan user) — bukan hasil filter baru
