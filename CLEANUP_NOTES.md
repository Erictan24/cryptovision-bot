# Crypto Signal Bot — Cleanup Notes

## Ringkasan Perubahan

### Sebelum vs Sesudah

| Metric | Sebelum | Sesudah |
|--------|---------|---------|
| Total baris kode | ~5,800 | 3,261 |
| File utama (trading_engine.py) | 4,133 baris | 895 baris |
| generate_entry_signal() | 790 baris | 197 baris |
| Magic numbers | 513 | 98 (semuanya di config.py) |
| Patch markers (# was) | 9 | 0 |
| Duplikasi LONG/SHORT | 100% mirror | 0% — satu fungsi |

### Arsitektur Baru

```
config.py           → Semua konstanta + SIGNAL_PARAMS (98 threshold)
indicators.py       → Pure math: EMA, RSI, ATR, ADX, ema_trend, structure
sr_detector.py      → S&R detection: swings, OB, flip zones, cluster+score
smc_analyzer.py     → SMC: BOS/CHoCH, phase, liquidity, OB, FVG, dll
signal_generator.py → generate_entry_signal() — tidak ada duplikasi
trading_engine.py   → Data fetching, caching, orchestration only
```

### Perubahan Kunci

**1. SIGNAL_PARAMS di config.py**
Semua 98 threshold ada di satu tempat. Untuk backtesting nanti:
- Tidak perlu gali 4000 baris kode
- Cukup iterasi dict ini
- Setiap param punya komentar: default, range yang masuk akal

**2. generate_entry_signal: 790 → 197 baris**
Sebelum: LONG scoring (~350 baris) copy-paste ke SHORT scoring (~350 baris)
Sesudah: `_score_direction(direction, ...)` satu fungsi — LONG dan SHORT
adalah mirror. Ketika ada bug, fix di satu tempat.

**3. Patch markers → 0**
Semua `# was X` dan `# raised` dihapus. Nilai lama hilang karena
sudah tidak relevan — semua threshold ada di SIGNAL_PARAMS dengan komentar.

**4. File terpisah per concern**
- `indicators.py` = pure math, tidak ada side effects, mudah di-test
- `sr_detector.py` = S&R logic terisolasi
- `smc_analyzer.py` = SMC terisolasi
Masing-masing bisa di-import ke backtesting engine tanpa membawa
dependency ke network atau database.

## Magic Numbers yang Masih Ada (416 sisa)

Sisa float literals di luar config.py adalah:
- Geometry calculations (0.5, 1.0, 2.0) yang kontekstual jelas
- Leverage percentages (0.18, 0.09 = 18%, 9%) di liquidation zones
- Candle pattern ratios (0.3 = 30% body ratio) yang sudah documented

Tidak semua angka perlu nama — hanya yang bisa di-tune berdasarkan data.
Threshold sudah dipindah ke SIGNAL_PARAMS.

## File Yang Tidak Diubah

- `database.py`   — tidak ada issue
- `risk_manager.py` — tidak ada issue  
- `main.py`       — tidak ada issue
- `telegram_bot.py` — tidak ada issue (perlu update import saja)
- `whale_analyzer.py` — tidak ada issue

## Langkah Berikutnya: Backtesting Engine

Setelah cleanup ini, langkah berikutnya adalah:

```python
# backtesting/backtest.py
from indicators import calc_ema, calc_rsi, calc_atr, calc_adx, analyze_ema_trend
from sr_detector import detect_key_levels
from smc_analyzer import build_smc_analysis
from signal_generator import generate_entry_signal
from config import SIGNAL_PARAMS

# Test semua threshold dari SIGNAL_PARAMS terhadap data historis
# Tidak perlu gali source code — semua di satu dict
```

Target fase berikutnya:
- Build backtesting engine sederhana
- Fetch 6 bulan data historis dari CryptoCompare
- Test IDEAL/GOOD/MODERATE signal: berapa yang hit TP2? SL?
- Temukan threshold optimal dari DATA, bukan dari feeling
