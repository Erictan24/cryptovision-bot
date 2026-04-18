# Bot 2 — CryptoVision Scalping v4.3

Production-ready trading bot untuk scalping crypto di 15m timeframe dengan self-learning mechanism.

## Quick Start

```bash
# 1. Setup
pip install -r requirements.txt

# 2. Configure .env dengan credentials
cp .env.example .env
nano .env

# 3. Run bot (development)
python main_scalp.py

# 4. Run dashboard (optional)
python dashboard_api.py
# Open http://localhost:8080
```

## Arsitektur

### Engine Stack
```
Bot 2 (Scalping 15m)
├── scalping_signal_engine.py   ← Signal generation (v4.3)
├── trading_engine_scalp.py      ← Wrapper + data fetcher
├── main_scalp.py                ← Entry point + scheduler
├── dashboard_api.py             ← Public stats API
│
├── scalp_trade_journal.py       ← SQLite trade history
├── scalp_coin_learning.py       ← Adaptive per-coin params
├── scalp_session_filter.py      ← Session-based filter
│
├── backtest_scalp.py            ← Bot 2 backtest
└── backtest_unified.py          ← Bot 1 + Bot 2 combined
```

### Signal Pipeline (v4.3)
```
INPUT: 15m OHLCV + 1H + 4H
  ↓
[1] Session filter (block DEAD session)
[2] Per-coin adaptive params (from learning)
[3] 1H trend detection (EMA stack + ADX + slope)
[4] 4H macro trend agreement
[5] 15m pullback detection (RSI 40-52)
[6] Continuation trigger (EMA reclaim + RSI turn)
[7] Score bonus: volume pressure, SMC BOS, rejection wick, candle
[8] Kill factors: volume dead, counter-trend
[9] Quality determination (adaptive threshold)
[10] SL/TP from pullback swing
  ↓
OUTPUT: Signal dict with quality GOOD/WAIT
```

## Config Produksi

Di `config.py` → `SCALP_CONFIG`:

```python
SCALP_CONFIG = {
    # Risk (untuk $100 capital)
    'risk_per_trade_usd': 1.0,
    'daily_loss_limit': 10.0,
    'max_positions': 3,
    'max_same_direction': 2,
    'leverage': 10,

    # Timeframes
    'main_tf': '15m',
    'confirm_tf': '1h',

    # Scan
    'scan_interval_minutes': 5,

    # Auto trade
    'auto_trade_enabled': True,
    'auto_trade_min_quality': 'GOOD',

    # Learning
    'learning_enabled': True,
    'weekly_learning_refresh': True,
}
```

## Self-Learning Mechanism

### Trade Journal
Setiap trade yang executed (live atau backtest) di-record ke `data/scalp_trades.db`:
- Entry context (price, SL, TP)
- Features (score, trend_state, pullback_quality, session, hour)
- Outcome (TP1/TP2/TP3/SL/EXPIRED)
- PnL in R units

### Coin Learning
Setiap minggu (Senin 02:00 UTC), bot refresh per-coin params:
- **WR >= 55%**: HIGH confidence → lower threshold (more signals)
- **WR 45-54%**: OK → default
- **WR 35-44%**: LOW → stricter
- **WR < 30% OR EV < 0**: BLOCKED

### Session Learning
- Hard block: DEAD session (02-06 UTC)
- Learned block: session dengan WR < 30% historical

## Safety Features

### Daily Loss Limit
- Max $10 daily loss
- Otomatis stop trading kalau hit
- Reset di 00:00 UTC

### Error Recovery
- Scan error tidak crash bot
- 5x consecutive error → Telegram alert
- Graceful restart setelah cooldown

### Anti-Flip
- Signal direction locked 2 jam per coin
- Mencegah flip-flop di ranging market

### Health Monitoring
- Daily summary 23:00 UTC
- Weekly learning refresh Senin
- Startup/shutdown notification

## Backtest Workflow

### Bot 2 only
```bash
python backtest_scalp.py --days 90
python backtest_scalp.py --no-fetch --days 90 --two-pass  # with learning
```

### Unified (Bot 1 + Bot 2)
```bash
python backtest_unified.py --days 90                # fetch + run
python backtest_unified.py --no-fetch --days 90     # use cache
python backtest_unified.py --engines scalp          # scalp only
python backtest_unified.py --engines swing          # swing only
```

## Dashboard

### Local test
```bash
python dashboard_api.py
# Open http://localhost:8080
```

### Endpoints
- `GET /` — HTML dashboard
- `GET /api/stats?engine=SCALP&days=30` — Performance stats
- `GET /api/recent-trades?limit=20` — Trade history
- `GET /api/per-coin` — Per-coin breakdown
- `GET /api/equity-curve?limit=500` — Equity curve data
- `GET /api/health` — Health check

## Deployment

See [DEPLOY_GUIDE.md](DEPLOY_GUIDE.md) for VPS setup step-by-step.

## Performance Benchmarks (Backtest 90 days, 20 coins)

### Run 19 v4.3 Two-Pass Learning
- **Total Trades**: 112
- **Win Rate**: 51.8%
- **Avg PnL**: +0.25R per trade
- **Total PnL**: +27.5R
- **Max DD**: 5.0R
- **LONG WR**: 60%
- **SHORT WR**: 48%
- **$Profit estimate**: $9/month on $100 capital

### Run 17 v4.1 (baseline)
- **Total Trades**: 196
- **Win Rate**: 43.4%
- **Avg PnL**: +0.17R
- **Total PnL**: +33.0R

### Top Performing Coins
- TAO 80% WR
- ARB 75% WR
- TON 58% WR
- BTC 53% WR
- ETH 50% WR

## File Overview

| File | Purpose | Lines |
|------|---------|:-:|
| `scalping_signal_engine.py` | Signal engine v4.3 | ~2000 |
| `main_scalp.py` | Production entry point | ~500 |
| `trading_engine_scalp.py` | Wrapper untuk data fetch | ~200 |
| `scalp_trade_journal.py` | SQLite journal + queries | ~300 |
| `scalp_coin_learning.py` | Adaptive per-coin params | ~250 |
| `scalp_session_filter.py` | Session filter + learning | ~150 |
| `backtest_scalp.py` | Bot 2 standalone backtest | ~900 |
| `backtest_unified.py` | Unified Bot 1 + Bot 2 | ~500 |
| `dashboard_api.py` | Public stats API + HTML | ~600 |

## License & Disclaimer

**Educational purposes only.**

Past performance does not guarantee future results. Cryptocurrency trading involves
substantial risk of loss. This software is provided AS-IS with no warranty.

Not financial advice. Trade at your own risk.
