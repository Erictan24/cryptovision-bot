# 🎯 LEVEL 1 — Paper Trade Quick Start

**Zero risk. Zero uang real. Full validasi.**

## Apa Itu Level 1?

Bot jalan di laptop kamu dengan mode **simulasi**:
- ✅ Scan coin setiap 5 menit
- ✅ Generate signal dengan analisa lengkap
- ✅ Kirim ke Telegram dengan tag `[PAPER]`
- ✅ "Eksekusi" trade di database (bukan real order)
- ✅ Monitor harga per 2 menit
- ✅ Record TP/SL hit di database
- ✅ Daily stats report
- ❌ TIDAK pakai uang real
- ❌ TIDAK sentuh Bitunix account

## Setup (5 Menit)

### 1. Setup Telegram Bot

Kalau belum punya bot Telegram:

1. Buka Telegram, search `@BotFather`
2. Kirim `/newbot`
3. Kasih nama: `CryptoScalp Test Bot` (atau terserah)
4. Kasih username: `crypto_scalp_test_bot` (akhiran `_bot`)
5. Copy **TOKEN** yang diberikan BotFather

### 2. Create Telegram Group (untuk menerima signal)

1. Telegram → **New Group**
2. Nama: "Bot 2 Paper Test"
3. Add bot kamu ke group (pakai username dari step 1)
4. Kirim pesan apapun di group (supaya bot detect group ID)

### 3. Setup Environment Variables

Edit file `.env` di folder bot (buat kalau belum ada):

```bash
TELEGRAM_BOT_TOKEN=paste_token_dari_botfather
```

### 4. Install Dependencies (kalau belum)

Buka terminal di folder bot:

```bash
pip install -r requirements.txt
```

### 5. Verify Config

Pastikan config sudah benar:

```bash
python -c "from config import SCALP_CONFIG; print('Paper mode:', SCALP_CONFIG['paper_mode'])"
```

Output harus: `Paper mode: True`

## Run Paper Trade Bot

Tinggal 1 command:

```bash
python main_scalp.py
```

Bot akan:
1. Connect ke Telegram
2. Start scan setiap 5 menit
3. Monitor paper trades setiap 2 menit
4. Kirim daily summary jam 23:00

## Apa yang Akan Kamu Lihat

### Di Terminal:
```
============================================================
 Bot 2 — SCALPING v4.3 PRODUCTION
============================================================
Inisialisasi...
Learning modules loaded — 112 trades in journal
PAPER TRADE MODE AKTIF (Level 1)
Bot 2 v4.3 siap untuk production!
Scalp scan scheduler aktif
Paper monitor scheduler aktif
...
2026-04-14 12:05:30 | Scalp scan dimulai...
2026-04-14 12:05:32 | BTC bias: BULLISH
2026-04-14 12:05:45 | [BTC] V3 SIGNAL: LONG GOOD | trend=UPTREND ...
2026-04-14 12:06:01 | Scalp scan selesai: 2 signal
2026-04-14 12:06:05 | PAPER OPEN #1: BTC LONG entry=65234 SL=64812 TP1=65656
```

### Di Telegram (saat signal muncul):
```
[PAPER MODE]
🟢 [SCALP] BTC LONG [GOOD]
==============================
Entry : 65234
SL    : 64812 (0.65%)
TP1   : 65656 (RR 1:1.2)
TP2   : 66078 (RR 1:2.0)
TP3   : 66500

Strategy: Trend-Following Pullback
Trend 1H: UPTREND | Pullback: IDEAL
Macro 4H: BULL | Session: LONDON
Vol pressure: BUYING
SMC BOS: BULLISH_BOS
Score: 16 | RSI: 48.5

Reasons:
  - Trend UPTREND: EMA bull, ADX 28
  - Pullback IDEAL
  - EMA9 reclaim
  - RSI 48 rising
  - 2 bullish candles
  - Volume spike 1.7x
  - Rejection wick 2.1x
```

### Saat paper trade open:
```
📝 PAPER TRADE OPENED #1
BTC LONG [GOOD]
Entry: 65234
SL: 64812
Risk: $1 (simulated)
```

### Saat paper trade closed:
```
[PAPER] CLOSED #1
==============================
🟢 BTC LONG
Outcome: TP2_HIT
Entry: 65234
Close: 66078
PnL: +2.00R ($+2.00)
```

### Daily Summary (jam 23:00):
```
DAILY SUMMARY — Bot 2 SCALP [PAPER MODE]
============================
Date: 2026-04-14
Uptime: 10.5h
Signals generated: 8
Daily loss hit: NO
Errors: 0

PAPER TRADING STATS
============================
Closed trades: 5
Open trades: 2
Win Rate: 60.0%
W/L/BEP: 3/1/1
Total PnL: +3.20R ($+3.20)
Avg PnL: +0.64R/trade
Capital: $103.20
ROI: 🟢 +3.20%
```

## Monitoring Stats

### Cek stats kapan saja via command line:
```bash
python -c "
from scalp_paper_trader import PaperTrader
pt = PaperTrader()
print(pt.format_stats_msg())
"
```

### Cek recent paper trades:
```bash
python -c "
from scalp_paper_trader import PaperTrader
pt = PaperTrader()
trades = pt.get_recent_trades(10)
for t in trades:
    print(f\"{t['opened_at'][:16]} {t['symbol']:6s} {t['direction']:5s} \"
          f\"{t['status']:6s} pnl={t.get('pnl_r', 0):+.2f}R\")
"
```

### Cek open trades:
```bash
python -c "
from scalp_paper_trader import PaperTrader
pt = PaperTrader()
open_trades = pt.get_open_trades()
print(f'Open: {len(open_trades)}')
for t in open_trades:
    print(f\"  #{t['id']} {t['symbol']} {t['direction']} entry={t['entry_price']}\")
"
```

## Stop Bot

Tekan `Ctrl+C` di terminal. Bot akan kirim shutdown notification ke Telegram.

## What to Watch For (Observasi 1-2 Minggu Pertama)

### Metrics yang Harus Kamu Track:

1. **Signal Frequency** (berapa signal per hari?)
   - Target: 3-8 signal per hari (sehat)
   - < 1/hari = terlalu ketat, perlu longgar
   - > 15/hari = terlalu noisy, perlu ketat

2. **Signal Quality** (subjective observation)
   - Apakah entry masuk akal di chart?
   - SL ditaruh di tempat yang logis?
   - TP1/TP2 achievable?

3. **Win Rate Paper** (vs backtest 46.2%)
   - Target: dalam ±10% dari backtest
   - > 55%: bot sangat bagus
   - 40-55%: bot OK (sesuai ekspektasi)
   - < 40%: investigate, mungkin market beda

4. **Expected Value (EV)**
   - Backtest: +0.12R/trade
   - Live target: > 0R (positive EV = profitable)
   - Kalau negatif setelah 30+ trade: perlu tuning

5. **Drawdown**
   - Worst streak losses?
   - Kapan bot paling struggle?

## Decision Point Setelah 1-2 Minggu

Setelah 2 minggu paper trade, evaluasi:

### ✅ LULUS ke Level 2 jika:
- [ ] Signal frequency 3-10/hari
- [ ] WR paper >= 40%
- [ ] EV positif (walaupun kecil)
- [ ] Max drawdown < $10 (simulated)
- [ ] Bot jalan stabil, crash < 2 kali
- [ ] Kamu understand signal behavior

### ❌ Perlu Tuning jika:
- [ ] Signal terlalu sedikit (< 5/minggu)
- [ ] WR paper < 35%
- [ ] EV negatif setelah 30+ trade
- [ ] Sering kena SL terus menerus

### ❌ BATAL jika:
- [ ] WR paper < 30% setelah 50+ trade
- [ ] EV sangat negatif
- [ ] Banyak bug/crash

## Troubleshooting

### Bot tidak kirim signal ke Telegram
- Cek `.env` punya TELEGRAM_BOT_TOKEN yang benar
- Pastikan bot sudah di-add ke group
- Kirim `/start` ke bot untuk register chat_id

### Bot crash saat start
- Check log error di `bot_scalp.log`
- Pastikan dependencies installed: `pip install -r requirements.txt`
- Pastikan Python >= 3.10

### No signal coming
- Ini normal di awal — market harus kondisi trending
- Bot hanya trigger saat ADX 1H >= 22 + trend strength >= 50
- Dalam kondisi sideways, bot akan diam (GOOD behavior)

### Banyak "SKIP: weak trend strength"
- Normal — bot filter strict untuk kualitas
- Cek beberapa jam atau esok hari
- Jangan lower threshold terlalu cepat

## Tips Selama Paper Trade

1. **Jangan intervensi** — biarkan bot jalan organik
2. **Jangan ubah config** — konsistensi data penting
3. **Screenshot notable trades** — untuk content channel nanti
4. **Buat journal harian** — observasi di Notion/Google Doc
5. **Share ke channel 1300 follower** — content building phase!

### Content Ideas dari Paper Trade:
- "Hari ke-1 paper trade — 3 signal, 2 win"
- "Bot detect bullish BOS di BTC, ini hasilnya"
- "Weekly recap: 15 paper trades, WR 53%"
- "Kenapa bot saya skip signal ini? Karena..."

Ini semua content gratis dari paper trade data kamu.

## Next Steps After Level 1 Success

Setelah 2 minggu paper trade menunjukkan result yang OK:

1. Switch `auto_trade_enabled: True` di config
2. Fund Bitunix $100
3. Setup API key
4. Run di laptop dengan **uang real kecil** (Level 2)
5. Monitor 1-2 minggu lagi
6. Deploy ke VPS (Level 3)
7. Mulai content marketing serius

## Summary

**Untuk start Level 1 sekarang:**

1. Dapatkan Telegram bot token dari @BotFather
2. Buat `.env` dengan token
3. Run: `python main_scalp.py`
4. Watch Telegram untuk signal

**Zero risk. Unlimited learning. Start now.**
