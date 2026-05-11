# CryptoVision Bot — Context untuk Claude Code

## Identitas Bot
Bot trading crypto **DUAL ENGINE** untuk Bitunix Futures:
- **SWING** trader: 1H + 4H timeframe
- **SCALP** trader: 15m + 5m timeframe (real money sejak 2026-04-25)

Username Telegram: CryptoVisionID
Folder: C:\Users\erict\OneDrive\crypto_bot_v2
VPS: IDCloudHost Singapore (Rp 87k/bln), path `/home/eric/cryptovision-bot`
Website: cryptovision.id (Vercel + Neon Postgres)

## Stack
- Python, Telegram Bot API, Bitunix Futures API
- Data: Binance Futures (primary), CryptoCompare (fallback)
- Web: Next.js + Neon Postgres + Vercel
- Comms: HMAC auth bot → web push

## File Utama

### Swing Engine
- main.py → entry point, auto scan 30 menit
- trading_engine.py → wrapper signal_generator
- signal_generator.py → SINGLE SOURCE OF TRUTH scoring swing
- telegram_bot.py → Telegram interface + auto trade + correlation filter
- bitunix_trader.py → koneksi Bitunix, stage trailing SL, TP1 monitor, place_order
- chart_generator.py → chart PNG signal
- news_filter.py → economic calendar filter

### Scalp Engine
- scalping_signal_engine.py → generate_scalping_signal() RANGE + TREND mode
- scalp_live_runner.py → live scan + execute scalp signal
- scalp_outcome_monitor.py → track TP1/TP2/SL outcome scalp trade
- data/scalp_coin_params.json → per-coin scalp config
- data/scalp_trades.db → SQLite scalp trade history

## Konfigurasi Trading (Status 2026-05-11)

### Swing
- TRADE_RISK_USD: $1 dari .env (validasi mode)
- Quality tier: GOOD (1.0x), MODERATE (0.7x), WAIT (0.6x)
- Daily loss limit: $15
- Max posisi: 5
- Leverage: 10x

### Scalp
- TRADE_RISK_USD: $0.25 base
- Quality tier: GOOD (1.0x = $0.25), WAIT (0.6x = $0.15)
- RANGE mode: cap WAIT only (by design)
- Whitelist 18 proven coin (mode RANGE deploy 2026-05-06)
- Scan tiap 15 menit, TF 15m main + 5m trigger

## Instruksi Komunikasi
- Selalu gunakan Bahasa Indonesia
- Jelaskan setiap perubahan: Apa → Kenapa → Dampak ke bot
- Bahasa sederhana untuk trader, bukan programmer
- Backtest dulu sebelum push config change (WAJIB)

## Target
- WR 60%+ swing (tercapai backtest 62.7-67.3%)
- WR 60%+ scalp RANGE (backtest 70.7%, 174 trades / 60d)
- EV positif per trade
- Volume: swing 20-30/bulan + scalp 5-10/hari

## Arsitektur Signal SWING

### Signal Flow
```
analyze_coin()
  → generate_entry_signal()  [signal_generator.py — PRIMARY]
      → _score_direction()    scoring 30+ faktor
      → _determine_quality()  GOOD/MODERATE/WAIT berdasarkan score + kills
      → _apply_rejection_gate()  wajib candle rejection
      → HTF Alignment Gate    GOOD wajib HTF EMA/CHoCH
      → Whale Flow Filter     block LONG saat HTF bearish tanpa reversal sign
  → clean_signal DISABLED (fallback bermasalah, WR 32%)
```

### Filter WR 60% Swing
1. **Accumulation Kill** — LONG + fase Accumulation = KILL
2. **BOS 1H Wajib HTF** — BOS 1H tanpa HTF BOS searah = KILL (false breakout)
3. **RSI Buffer Zone** — rsi_extreme_low=32, rsi_extreme_high=68
4. **ADX Precision Filter** — block ADX 35-44 (death zone) dan ADX >=50 (zero)

### Config Penting Swing (SIGNAL_PARAMS)
- score_good: 18 | score_moderate: 17
- max_kills_good: 1 | max_kills_moderate: 1
- rsi_extreme_low: 32 | rsi_extreme_high: 68
- adx_death_zone: 35-44 | adx_too_extreme: 50
- tp1_rr_min: 1.2 | tp2_rr_min: 2.0
- auto_trade min_quality: MODERATE (turun dari GOOD, data live AvgR -0.09 vs +0.03)

## Arsitektur Signal SCALP (Mode RANGE + TREND)

### Whitelist (18 coin proven, RANGE_WHITELIST 4 coin)
Filter selektif setelah forensic 7 fix. Top 50 by volume disabled (WR drop ke 60% dari 70%).

### RANGE Mode (mean-reversion)
- Trigger: price extreme BB + RSI overbought/oversold + reversal wick
- ADX < 18-30 (range condition)
- SL: `min(bb_lower - 0.5*atr, price - 1.0*atr)` (LONG safety bound)
- TP1 = mid BB, TP2 = opposite BB band
- Quality cap WAIT only
- **RANGE_WHITELIST 4 coin: BNB / DOGE / ETH / APT** (major only)
- Filosofi: mean reversion works for major coin only — altcoin lain blow-off lebih sering daripada bounce

### TREND Mode (pullback continuation)
- ADX > 25 + EMA aligned
- Pullback ke EMA21/50 + bounce confirmation
- Quality: GOOD atau WAIT

### Stage Trailing SL (bitunix_trader.py)
- Stage 1 (TP1 hit): SL → BEP
- Stage 2 (+0.5R): SL → entry + 0.25R
- Stage 3 (+1R): SL → entry + 0.5R
- Stage 4: DISABLED via env (Insiden LDO SL ketat 2026-05-08)

## Bug Kritis (April 2026) — Swing
1. Scoring Engine Ganda di trading_engine.py — Dihapus, wrapper saja
2. EMA Cross Bonus Double-Counting — Dihapus
3. BacktestEngine Missing Attributes — Fixed
4. Per-Coin Config Melonggarkan Threshold — Fix max(override, base)
5. report.py Unicode Crash Windows — ASCII only
6. Clean Signal Override Tanpa Filter — Disabled definitif

## Bug Kritis (Mei 2026) — Scalp & Trading

### Bug A — DNS Error Anggap Closed (2026-05-06)
API `_get()` swallow DNS error → return empty list → bot anggap posisi closed.
**Fix:** `check_position_status()` 3-state Optional[bool] (True/False/None), retry 3x.
Insiden PIPPIN, fix commit a5130f1.

### Bug B — risk_dist Salah (2026-05-07)
Stage 2/3 trigger pakai `abs(tp1 - entry)` instead of `abs(entry - sl_initial)`.
Trade RR > 1 → Stage 2 trigger jauh meleset.
**Fix:** persist sl_initial saat trade open, derive risk_dist dari situ.

### Bug C — Resume Pakai Bitunix avgEntry (2026-05-07)
Restart monitor pakai avgEntry Bitunix (post DCA/partial) bukan signal entry asli.
**Fix:** prefer `saved['entry']` dari local trades.

### Bug D — LIMIT Order TP1 Monitor False SL (2026-05-03)
TP1 monitor start untuk LIMIT belum filled → post-mortem detect "SL hit" palsu.
**Fix:** check `order_type == 'MARKET'` sebelum start monitor.

### Bug E — Outcome Label Tidak Akurat (2026-05-03)
Label TP1/TP2/BEP/SL derive dari pnl_r threshold (bias). Bot tracking stage flag accurate.
**Fix:** outcome derive dari tp2_hit / tp1_hit / bep_done. Web prioritas outcome string, pnl_r fallback legacy.

### Bug F — place_order qty=0 untuk MARKET (2026-05-11) ⭐ KRITIKAL
**Root cause 0 scalp signal 3 minggu:**
- scalp_live_runner pass `entry=0` ke place_order = MARKET intent
- `risk_per_unit = abs(entry - sl) = abs(0 - sl)` = HUGE → qty=0 → reject
- ALL scalp signal sejak deploy 2026-05-06 silent fail
**Fix:** detect entry=0 → fetch current price Bitunix sebelum qty calc.
Commit 9c87594.

### Bug G — RANGE LONG SL Above Entry (2026-05-11) ⭐
Edge case BB extreme: `sl = bb_lower - 0.5*atr` masih > entry untuk LONG.
**Fix:** `sl = min(bb_lower - 0.5*atr, price - 1.0*atr)` untuk LONG.
Commit 9c87594.

### Bug H — Bitunix set_leverage Silent Fail
set_leverage WAJIB include marginCoin USDT. Bug sejak awal — bot pakai leverage default Bitunix bukan TRADE_LEVERAGE env. Fix commit 510a8c8.

### Bug I — Telegram asyncio Event Loop Closed
Background thread notif pakai `asyncio.new_event_loop()` → conflict dengan Telegram event loop.
**Fix:** `_tg_send()` helper pakai HTTP requests direct.

## Penemuan Penting Swing (179 trades baseline)

### WR by Score Range
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
- LONG: WR 71-86% (setelah Accumulation kill)
- SHORT: WR 57-64% (volume lebih banyak)
- LONG kena SL 35% lebih cepat dari SHORT

### Faktor Jebakan (lebih sering di SL)
- Accumulation phase + LONG (100% SL rate!)
- BOS 1H tanpa HTF BOS (WR 22%)

### Faktor Juara (lebih sering di TP2)
- Pin Bar (+11% bias)
- RSI Divergence (+10% bias)
- HTF BOS / HTF EMA aligned (+7% bias each)

## Backtest Results History

### Swing
| Setup | Trades | WR | EV | Volume |
|-------|--------|------|------|--------|
| Baseline (sebelum fix) | 179 | 54.5% | +0.44R | - |
| 4 Fix 1h only | 97 | 67.3% | +0.62R | 16/bln |
| 1h+4h Opsi B ADX | 126 | 63.8-65.6% | +0.56R | 21/bln |
| max_kills=1 + score=18 | - | naik semua metric | naik | naik |
| zone_margin 0.6→1.0 (05-02) | 265 | 60.0% | +0.54R | +20% |

### Scalp
| Setup | Trades | WR | EV | Note |
|-------|--------|------|------|------|
| Volume upgrade (04-29) | 164 | 65.2% | +0.19R | 60d/50coin |
| RANGE mode + whitelist 18 (05-06) | 174 | 70.7% | +0.29R | 60d, +50R |

### Loosening Filter Test (2026-05-10/11) — SEMUA GAGAL
4 approach tested, semua jelek vs baseline (WR 70.7% / EV +0.29R / +50R):
| Approach | Trades | WR | EV | Verdict |
|---|---|---|---|---|
| ADX threshold 18→15 | 178 | 69.7% | +0.28R | Marginal worse, **rollback** |
| Disable whitelist + Top 50 vol | 190 | **60.0%** | +0.10R | **WAY worse** |
| RANGE WAIT 4→19 coin | 272 | 54.4% | +0.14R | FAIL |
| RANGE GOOD-only 4→18 | 270 | 54.8% | +0.08R | DD 23R, FAIL HARD |

**Rule absolut:** EXPAND filter / LOOSEN threshold = QUALITY DROP. Engine optimal at current config. Kalau market sideways → TUNGGU, bukan loosen.

### Live Scalp Trade Stats (metode baru, 16 closed sampai 2026-05-11)
- Trades: BTC, PIPPIN, BASED, LINK, ALGO, VANA (TP2) + ARB×2, RENDER, UNI, OPG, OP, TIA, LDO, IO, IP (TP1/SL)
- WR: 68.75%
- Net PnL: +11.78R
- **Engine PROVEN** — bukan WR backtest yang teori

### TP1 Enforcement & Clean Signal — REVERTED
- TP1 enforcement: 13 trades only (terlalu ketat)
- Clean signal fallback: WR 32% (bypass filter), DISABLED definitif

## Web Dashboard (cryptovision.id)
- `/dashboard/news` — Berita Crypto + Kalender Ekonomi bilingual
- `/dashboard/signals` — live signal feed
- `/dashboard/positions` — Bitunix REST poll 3s
- `/dashboard/history` — trade history (TP1/TP2/BEP/SL)
- `/dashboard/statistics` — WR/PnL aggregates
- HMAC auth bot → web push (signal/position/history)
- Welcome email via Resend
- Mobile responsive

## Hal yang Tidak Boleh Dilakukan
- JANGAN tambah scoring engine baru di trading_engine.py
- JANGAN override SIGNAL_PARAMS dengan hardcoded
- JANGAN kasih score ke Accumulation+LONG (jebakan)
- JANGAN entry ADX 35-44 atau ADX >= 50
- JANGAN per-coin config melonggarkan threshold
- JANGAN re-enable clean_signal sebelum rewrite
- JANGAN push config tanpa backtest dulu
- JANGAN naikin TRADE_RISK_USD sebelum bulan ini profit terkonfirmasi (volume/WR naik bukan justifikasi)
- JANGAN trust empty position response — wajib retry 3x (DNS error masking)
- JANGAN pass entry=0 ke place_order tanpa MARKET intent jelas
- JANGAN POST web data tanpa pikir display logic web side (label threshold)
- JANGAN multi-script tumpang tindih — bikin 1 idempotent script
- JANGAN loosen scalp filter (ADX, whitelist, RANGE coin) — 4 test 2026-05-10/11 PROVEN drop WR 10pp+
- JANGAN derive outcome label dari pnl_r threshold — pakai outcome string (tp2_hit/tp1_hit/bep_done flag bot)
- JANGAN compute pnl_usd on-the-fly untuk historical sync — hardcode dari audit Bitunix
- JANGAN re-enable Stage 4 trailing tanpa rewrite (extreme-0.5R terlalu ketat, LDO incident)

## Pencapaian Sampai 2026-05-11

### Swing
- WR backtest: 62.7-65.6%
- EV: +0.51 sampai +0.56R per trade
- Volume: 20-25 signal/bulan
- Live validation jalan, paper-to-real money confirmed

### Scalp
- WR backtest: 70.7% (RANGE mode + whitelist 18)
- Volume target: 5-10 trade/hari
- Live REAL MONEY sejak 2026-04-25
- ⚠️ 2026-05-11 baru ketauan bug F+G block ALL signal 3 minggu, fix commit 9c87594

### Infrastructure
- VPS deploy stabil (`/home/eric/cryptovision-bot`)
- Web dashboard end-to-end (signal → position → history)
- UptimeRobot monitor /api/stats interval 5 menit
- SEO/legal foundation (privacy, terms, sitemap, JSON-LD)
- Welcome email otomatis

### Recovery Scripts (17 scripts in scripts/)
Untuk recover dangling positions, dedupe web, fix label, audit Bitunix.

## Roadmap Risk Scaling (TRADE_RISK_USD)
| Fase | Kriteria | Risk Swing | Risk Scalp |
|------|----------|-----------|-----------|
| Validasi (sekarang) | - | $1 | $0.25 |
| Bulan profit confirmed | Net PnL + bulan ini | $2 | $0.50 |
| Bulan 2 profit | 40+ trade, WR ≥58% | $3 | $0.75 |
| Bulan 3+ profit | 60+ trade, WR ≥60% | $5 | $1 |
| Stabil 3 bulan | Profit konsisten | 2% balance compound | - |

## Status Sekarang (2026-05-11)
- Bot live VPS PID 370571
- Commit 9c87594 deployed (Bug F + G fix scalp)
- Waiting first scalp signal validate fix
- Swing volume normal (sudah 20+ trade bulan ini)
- Next milestone: review PnL akhir bulan untuk risk bump decision

## Workflow Wajib (Trust Damage Prevention)
Setelah marathon sesi 2026-05-09/10 (web sync 7+ iterasi sloppy):

1. **Think first, code second** — pikirin dampak ke production data sebelum push
2. **Idempotent design** — script harus aman re-run, DELETE before POST
3. **Hardcode > compute** untuk historical sync (audit Bitunix sekali, hardcode)
4. **Test 1 sample dulu** sebelum massal (--symbol XYZ flag)
5. **User verify between steps** — jangan chain 5 operations
6. **Verify web display logic** sebelum POST (label threshold, schema)
7. **Backtest dulu** sebelum push config change (incident auto-tune 2026-04-25, 2 hari zero signal)
8. **Cross-check Bitunix actual** sebelum trust local state
9. **Recovery script fetch ALL records** — bukan hist[0]
10. **Web sync wajib** lewat HMAC POST, bukan manual edit DB
