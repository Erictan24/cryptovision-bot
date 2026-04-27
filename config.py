import os
from dotenv import load_dotenv

load_dotenv()

# =============================================
# API KEYS
# =============================================
TELEGRAM_BOT_TOKEN    = os.getenv('TELEGRAM_BOT_TOKEN')
CRYPTOCOMPARE_API_KEY = os.getenv('CRYPTOCOMPARE_API_KEY', 'ff78fd3f7447f36a678ca4c956e218650fc9c1f508246694b11f5dd2348e3691')

# =============================================
# PAIR YANG AKAN DI-SCAN
#
# Keputusan berdasarkan data 20+ run backtest:
#   BTC  : WR 32% — dikeluarkan (noise terlalu besar)
#   ETH  : WR 50% — dipertahankan, 1h saja
#   XRP  : WR 67% — terbaik, pertahankan
#   TIA  : WR 16-28% — dikeluarkan (manipulatif)
#   SOL  : WR 20% — dikeluarkan
# =============================================
# TRADING_PAIRS dan SCAN_POOL dipertahankan untuk kompatibilitas modul lain.
# Scan aktual sekarang menggunakan get_top_coins(100) dari Binance Futures.
TRADING_PAIRS = ['ETH/USDT', 'XRP/USDT', 'ATOM/USDT', 'UNI/USDT', 'ALGO/USDT']

# 30 coin pilihan — update 2026-04-10
# Kriteria: likuid di Bitunix Futures, karakter trend bagus, tidak terlalu noisy
# Coin dihindari: BTC (noisy WR 32%), NEAR, STO, LINK, TRUMP, STRAX, UNI
SCAN_POOL = [
    # === 15 coin terbukti aktif dari training ===
    'SUI', 'FET', 'LTC', 'ADA', 'ALGO',
    'XLM', 'BLUR', 'DGB', 'XRP', 'ONG',
    'TON', 'ZEC', 'AVAX', 'TAO', 'SOL',
    # === 15 coin tambahan — likuid & trend-friendly ===
    'ETH',   # blue chip, wajib ada
    'ATOM',  # mid-cap, trend jelas
    'DOT',   # pola S/R bagus
    'DOGE',  # volume tinggi, trend kuat
    'TRX',   # stabil, likuid
    'ARB',   # L2, volatilitas sehat
    'OP',    # L2, karakter mirip ARB
    'INJ',   # DeFi kuat, trend bersih
    'APT',   # volatilitas cukup, pola bagus
    'SEI',   # altcoin aktif
    'JUP',   # volume baik
    'WLD',   # trendy, volatil
    'PENDLE', # DeFi niche tapi trend jelas
    'FIL',   # storage coin, siklus jelas
    'POL',   # ex-MATIC, masih likuid di Binance Futures
]

# Tidak ada blacklist — bot belajar dari semua coin lewat historical training
COIN_BLACKLIST = set()

# =============================================
# TIMEFRAME
# Hanya 1h — 15m terlalu noisy (ETH 15m WR 33%)
# Data: 1h EV +0.73 vs 15m EV +0.48
# =============================================
TIMEFRAMES = {
    'main'  : '1h',
    'higher': '4h',
    'lower' : '15m',
}

# =============================================
# DAILY AUTO SIGNAL
# =============================================
DAILY_SIGNAL = {
    'hour'        : 8,
    'minute'      : 0,
    'max_signals' : 10,   # Top 10 signal terbaik dari seluruh market
    'scan_delay'  : 2,    # Lebih cepat — Binance tidak perlu delay panjang
    'min_quality' : 'MODERATE',  # 2026-04-27: balikin ke MODERATE — match backtest fe64503 (244 trade/60d WR 60%). GOOD-only bikin volume drop drastis (rejection gate downgrade GOOD→MODERATE saat no Pin Bar).
}

# =============================================
# REAL-TIME MONITOR
# =============================================
REALTIME = {
    'enabled'        : False,
    'interval_sec'   : 180,
    'watchlist'      : ['ETH', 'XRP'],
    'timeframes'     : ['1h'],
    'min_quality'    : 'GOOD',
    'alert_cooldown' : 1800,
    'max_alerts_hour': 10,
}

# =============================================
# PARAMETER STRATEGI
# =============================================
STRATEGY = {
    'name': 'Price Action S&R + SNR Rejection Gate',
    'indicators': {
        'ema_fast'  : 9,
        'ema_mid'   : 21,
        'ema_slow'  : 50,
        'rsi_period': 14,
        'atr_period': 14,
    },
}

# =============================================
# RISK MANAGEMENT
# =============================================
RISK_CONFIG = {
    'risk_per_trade'            : 0.02,
    'max_risk_per_day'          : 0.06,
    'risk_reward_ratio'         : 2.0,
    'max_open_trades'           : 3,
    'stop_loss_atr_multiplier'  : 2.0,
    'take_profit_atr_multiplier': 4.0,
}

# =============================================
# DATABASE & LOG
# =============================================
DATABASE_PATH = 'data/trades.db'
LOG_LEVEL     = 'INFO'
LOG_FILE      = 'bot.log'

# =============================================
# SIGNAL PARAMS
#
# FINAL — berdasarkan 20+ run backtest + 3.612 trades historis
#
# PELAJARAN UTAMA:
#   1. 1h lebih baik dari 15m — 1h EV +0.73 vs 15m EV +0.48
#   2. Rejection gate wajib strength >= 3
#   3. Score 24+ selalu dead zone — hard reject
#   4. IDEAL dinonaktifkan — selalu underperform
#   5. Kill factor >= 2 → WR 17.5%, hard reject (dari analisis 3.612 trades)
#   6. RSI ekstrem (<28 atau >72) = jebakan, bukan peluang — WR hanya 19%
#   7. Sesi DEAD/NY → WR 19%, downgrade GOOD ke MODERATE
#   8. Sesi OVERLAP → WR 29.2%, volume tertinggi
# =============================================
SIGNAL_PARAMS = {

    # ADX
    # FIX #5 + OPSI B (2026-04-12): Precision ADX — block range death zone saja.
    # Data WR per ADX band (179 trades):
    #   25-29: 72% | 30-34: 67% | 35-39: 20% | 40-44: 33% | 45-49: 75% | 50+: 0%
    # Block: 35-44 (death zone) dan >=50 (terlalu ekstrem).
    # Allow: 45-49 (sweet spot kedua — historis WR 75%).
    'adx_ranging_block'      : 12,
    'adx_trending'           : 18,
    'adx_strong_trend'       : 25,
    'adx_death_zone_low'     : 35,   # Block kalau 35 <= adx < 45
    'adx_death_zone_high'    : 45,
    'adx_too_extreme'        : 50,   # Block kalau adx >= 50
    'adx_extreme'            : 999,  # Kompatibilitas (sebelumnya 35, sekarang tidak dipakai)

    # RSI
    # Data training: RSI <30 dan >70 WR hanya 19-20% (LEBIH BURUK dari rata-rata)
    # RSI zona tengah 40-60 justru WR 25%+ — jangan trade saat RSI ekstrem parah
    'rsi_very_oversold'  : 25,
    'rsi_oversold'       : 35,
    'rsi_near_overbought': 65,
    'rsi_overbought'     : 75,
    'rsi_extreme_low'    : 32,   # FIX #3 (2026-04-11): 30→32 (buffer zone, cegah jebakan near-oversold)
    'rsi_extreme_high'   : 68,   # FIX #3 (2026-04-11): 70→68 (buffer zone, cegah jebakan near-overbought)

    # S&R zone quality
    'sr_score_weak'       : 10,
    'sr_score_strong'     : 20,
    'sr_score_very_strong': 30,

    # S&R geometry
    'flip_zone_atr_mult'     : 0.3,
    'sweep_atr_mult'         : 0.15,
    'rejection_body_atr_mult': 0.7,
    'zone_margin_atr_mult'   : 0.6,   # Diperlebar — zona lebih mudah terdeteksi
    'freshness_retest_atr'   : 0.6,   # Diperlebar mengikuti zone width

    # Signal quality thresholds
    # Volume-first philosophy (2026-04-24): Total Profit = EV × Volume.
    # WR target turun ke 50-60%, volume target naik 3-4x.
    # score_good=16, score_moderate=14 (dari 17) — allow score 14-15 masuk MODERATE
    # min_confluence_score=14 (dari 17) — gate absolut ikut turun
    'score_ideal'      : 999,  # Nonaktif
    'score_good'       : 16,
    'score_moderate'   : 14,   # RELAX 17→14: allow signal score 14-15 (volume up)
    'score_wait'       : 1,
    'score_cap_good'   : 24,   # Cap: score 24+ dead zone, hard reject
    'score_hard_reject'   : 24,
    'min_confluence_score': 14,   # RELAX 17→14: ikut score_moderate

    # Kill factors
    # Data: kills=0 WR 67%, kills=1 WR 70%, kills=2 WR 60% — semua masih EV positif
    # max_kills_moderate 1→2: allow kills=2 masuk MODERATE (WR 60% acceptable)
    # max_kills_hard_reject 2→3: konsisten dengan max_kills_moderate=2
    'max_kills_hard_reject': 3,   # RELAX 2→3: >= 3 kills = hard reject
    'max_kills_ideal'      : 0,
    'max_kills_good'       : 1,
    'max_kills_moderate'   : 2,   # RELAX 1→2: kills=2 WR 60%, masih profitable

    # Entry / SL / TP geometry
    'entry_depth_pct'   : 0.3,
    'sl_atr_buffer'     : 1.5,   # Dinaikkan 1.0→1.5: 33% SL kena wick, perlu ruang lebih
    'max_sl_pct'        : 6.0,
    'min_rr_tp2'        : 1.0,   # Dikembalikan — SL sudah lebih lebar, jangan dobel filter
    'tp1_rr_min'        : 1.2,   # Dikembalikan — resistance cap lebih dominan dari setting ini
    'tp1_rr_max'        : 2.0,
    'tp2_rr_min'        : 2.0,   # Dikembalikan — TP2 default tetap 2R
    'tp2_rr_max'        : 3.5,
    'rr2_warn_threshold': 2.0,

    # Score per confluence factor
    'score_ema_strong'       : 2,
    'score_ema_slight'       : 1,
    'score_structure'        : 2,
    'score_sr_very_strong'   : 5,
    'score_sr_strong'        : 5,
    'score_sr_weak'          : 1,
    'score_fresh_level'      : 2,
    'score_htf_mtf_level'    : 2,
    'score_bos'              : 2,
    'score_choch'            : 3,
    'score_htf_bos'          : 2,
    'score_htf_choch'        : 5,
    'score_market_phase'     : 1,
    'score_market_phase_sub' : 1,
    'score_order_flow'       : 1,
    'score_pd_zone'          : 2,
    'score_pd_zone_slight'   : 1,
    'score_vol_div'          : 2,
    'score_vol_confirm'      : 1,
    'score_rsi_very_oversold': 3,
    'score_rsi_oversold'     : 1,
    'score_htf_ema'          : 2,
    'score_rsi_div'          : 3,
    'score_hidden_div'       : 2,
    'score_adx_strong'       : 2,
    'score_adx_trending'     : 1,
    'score_derivatives_lsr'  : 2,
    'score_derivatives_oi'   : 1,
    'score_derivatives_fund' : 1,
    'score_fvg'              : 1,
    'score_liq_zones'        : 1,
    'score_candle_pattern'   : 3,
    'score_order_block'      : 3,

    # Anti-flip & zone persistence
    'signal_lock_hours'  : 8,   # 8h — cegah flip-flop arah di coin yang sama
    'zone_persist_hours' : 12,

    # LTF trigger
    'ltf_strong_upgrade_score'  : 4,
    'ltf_moderate_upgrade_score': 3,

    # BTC correlation filter
    'btc_bear_change_hard'  : -7.0,
    'btc_bear_change_soft'  : -3.0,
    'btc_bull_change_soft'  : 2.5,
    'btc_bear_evidence_min' : 2,
    'btc_bull_evidence_min' : 2,

    # SMC
    'smc_confidence_min' : 50,

    # HTF ceiling/floor
    'htf_ceiling_atr_mult' : 1.5,
    'htf_block_atr_mult'   : 0.5,

    # Order flow
    'order_flow_bias_threshold': 25,

    # Volume divergence
    'vol_div_weak_ratio'  : 0.7,
    'vol_div_strong_ratio': 1.3,

    # Premium/Discount zone
    'pd_premium_pct'        : 70,
    'pd_discount_pct'       : 30,
    'pd_slight_premium_pct' : 55,
    'pd_slight_discount_pct': 45,

    # S&R cluster
    'sr_htf_mtf_bonus'  : 10,
    'sr_fresh_bonus'    : 12,
    'sr_min_final_score': 10,
}


# =============================================
# BOT 2 — SCALPING CONFIG (15m)
#
# Engine: scalping_signal_engine.py (v4.3)
# Teknik: Trend-following + pullback entry
# Multi-timeframe: 15m main, 1h HTF, 4h macro
# Self-learning: per-coin + session adaptive
# =============================================
SCALP_CONFIG = {
    # Timeframes
    'main_tf'   : '15m',
    'confirm_tf': '1h',

    # Scan interval
    'scan_interval_minutes': 5,

    # Risk management (PRODUCTION v4.3)
    # Capital: $100, risk $1 per trade = 1% per trade
    # Max daily loss: $10 = 10% max daily drawdown
    'risk_per_trade_usd': 1.0,
    'daily_loss_limit'  : 10.0,
    'max_positions'     : 3,
    'leverage'          : 10,
    'max_same_direction': 2,
    'initial_capital_usd': 100.0,

    # Bollinger Bands
    'bb_period'  : 20,
    'bb_std_mult': 2.0,

    # RSI
    'rsi_period'    : 14,
    'rsi_sma_period': 14,
    'rsi_oversold'  : 35,
    'rsi_overbought': 65,

    # MACD
    'macd_fast'  : 12,
    'macd_slow'  : 26,
    'macd_signal': 9,

    # Wedge/Channel detection (legacy, dipakai di v1 engine)
    'wedge_lookback'    : 50,
    'wedge_swing_window': 3,

    # SL/TP
    'sl_buffer_pct': 0.003,
    'sl_atr_mult'  : 0.3,
    'tp1_rr'       : 1.0,
    'tp2_rr'       : 1.8,
    'tp3_rr'       : 2.5,
    'max_sl_pct'   : 3.0,

    # Quality thresholds (adaptive di v4.3)
    'score_good'     : 8,
    'score_moderate' : 6,
    'score_wait'     : 4,
    'max_kills_good' : 0,
    'max_kills_mod'  : 1,
    'hard_reject_kills': 2,

    # Anti-flip
    'signal_lock_hours': 2,

    # BB volatility filter
    'min_bb_width_pct': 1.0,

    # Session filter (v4.3)
    'session_filter_enabled': True,

    # Coin pool
    'use_main_scan_pool': True,

    # COIN WHITELIST (v4.3 production)
    # Berdasarkan unified backtest (90 hari):
    # hanya coin dengan SCALP WR >= 50% yang di-trade
    'scalp_whitelist': [
        'BTC', 'ETH', 'SOL', 'SUI', 'TON', 'OP',
        'BNB', 'APT', 'DOGE', 'ARB', 'TAO',
    ],
    'use_whitelist': True,

    # Auto trade
    'auto_trade_min_quality': 'GOOD',
    'auto_trade_enabled'    : False,  # FALSE = paper mode

    # Paper mode (Level 1 validation)
    'paper_mode': True,

    # Learning (v4.3)
    'learning_enabled'      : True,
    'weekly_learning_refresh': True,
}


# =============================================
# UNIFIED BOT CONFIG — Bot 1 (SWING) + Bot 2 (SCALP)
# =============================================
UNIFIED_CONFIG = {
    # Engines enabled
    'swing_enabled': True,    # Bot 1 SWING 1H
    'scalp_enabled': True,    # Bot 2 SCALP 15m

    # Shared risk limits (combined across both engines)
    'global_max_positions': 4,     # Max 4 posisi total (swing + scalp)
    'global_max_daily_loss_usd': 10.0,  # $10 daily limit combined
    'global_max_same_direction': 3,     # Max 3 posisi arah yang sama

    # Conflict resolution
    'conflict_mode': 'swing_priority',  # 'swing_priority' | 'first_come' | 'higher_score'
    'block_opposite_direction': True,   # Block SHORT kalau sudah LONG di coin sama
    'block_duplicate_coin': True,       # Block signal baru kalau coin sama sudah open

    # Signal tagging
    'tag_scalp_signal': '[SCALP]',
    'tag_swing_signal': '[SWING]',
}