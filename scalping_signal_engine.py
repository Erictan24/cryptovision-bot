"""
scalping_signal_engine.py — Signal engine Bot 2 Scalping 15m.

v5.9 — Forensic-driven upgrade dari data 43 trades v5.8:
  v4.3 base: Multi-TF trend-following + self-learning
  v5.7: Confirmation candle gate, TP realistic (0.5/1.0/1.5R)
  v5.8: LONG penalty, volume threshold 1.8x, candle pattern disabled
  v5.9.1 (data-driven fixes dari forensic 43 trades):
    - TP 0.65/1.0/1.5R, BOS +1, volume 2.0x+, wick sweet spot
  v5.9.2 (top 100 coin forensic — coin quality fix):
    - Fix 8: Coin whitelist 28 coin | Fix 9: Whipsaw detector
  v5.9.3 (volume boost — longgarkan filter ketat):
    - Fix 10: ADX min 22->18 (unlock 8900 signals)
    - Fix 11: ADX death zone 35-44->40-44 (allow 35-39)
    - Fix 12: Whipsaw 3/5->4/5 (hanya block very choppy)
    - Fix 13: Session DEAD: hard block->soft penalty -3 score
    - Fix 14: Confirmation body 40%->30%
"""

import numpy as np
import pandas as pd
import logging

from indicators import calc_ema, calc_atr, calc_adx
from candle_patterns import detect_candle_pattern

# Learning modules (optional — fallback ke default kalau tidak tersedia)
try:
    import scalp_coin_learning as coin_learn
    import scalp_session_filter as session_filter
    _LEARNING_AVAILABLE = True
except ImportError:
    _LEARNING_AVAILABLE = False

logger = logging.getLogger(__name__)


# =========================================================
#  1. BOLLINGER BANDS (20, 2)
# =========================================================
def calc_bollinger_bands(df: pd.DataFrame, period: int = 20,
                         std_mult: float = 2.0,
                         lookback: int = 3) -> dict:
    """
    Hitung Bollinger Bands dan deteksi posisi harga terhadap band.

    lookback: cek touch dalam N candle terakhir (bukan hanya 1 candle).
    Ini penting karena BB touch dan re-entry jarang terjadi di candle
    yang sama persis — biasanya touch di candle X, re-entry di X+1/X+2.

    Returns:
        dict dengan keys:
          upper, middle, lower  — nilai band terakhir
          touch_lower  — True jika ada candle dalam lookback yang sentuh lower BB
          touch_upper  — True jika ada candle dalam lookback yang sentuh upper BB
          inside_now   — True jika harga sekarang sudah kembali ke dalam BB
          bb_width_pct — lebar BB sebagai % dari middle (volatilitas)
    """
    if df is None or len(df) < period + lookback + 1:
        return None

    close = df['close']
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()

    upper = sma + std_mult * std
    lower = sma - std_mult * std

    curr_close = close.iloc[-1]
    curr_upper = upper.iloc[-1]
    curr_lower = lower.iloc[-1]
    curr_middle = sma.iloc[-1]

    # Cek touch dalam lookback candle terakhir (bukan hanya 1)
    touch_lower = False
    touch_upper = False
    for i in range(2, lookback + 2):  # candle -2 sampai -(lookback+1)
        if i > len(df):
            break
        candle_low = df['low'].iloc[-i]
        candle_high = df['high'].iloc[-i]
        band_lower = lower.iloc[-i]
        band_upper = upper.iloc[-i]
        if not np.isnan(band_lower) and candle_low <= band_lower:
            touch_lower = True
        if not np.isnan(band_upper) and candle_high >= band_upper:
            touch_upper = True

    # Harga sekarang sudah kembali ke dalam BB
    inside_now = curr_lower <= curr_close <= curr_upper

    # BB width sebagai % — ukuran volatilitas
    bb_width_pct = ((curr_upper - curr_lower) / curr_middle * 100
                    if curr_middle > 0 else 0)

    return {
        'upper': curr_upper,
        'middle': curr_middle,
        'lower': curr_lower,
        'touch_lower': touch_lower,
        'touch_upper': touch_upper,
        'inside_now': inside_now,
        'bb_width_pct': bb_width_pct,
    }


# =========================================================
#  2. RSI 14 + SMA 14 CROSSOVER
# =========================================================
def calc_rsi_with_sma(df: pd.DataFrame, rsi_period: int = 14,
                      sma_period: int = 14,
                      lookback: int = 3) -> dict:
    """
    Hitung RSI dan SMA-nya, deteksi arah dan crossover.

    lookback: cek kondisi oversold/overbought dalam N candle terakhir.
    RSI bisa sudah oversold 2 candle lalu, lalu sekarang mulai naik —
    itu valid sebagai setup karena kita menangkap AWAL reversal.

    Returns:
        dict dengan keys:
          rsi          — RSI terakhir
          rsi_prev     — RSI sebelumnya
          rsi_sma      — SMA dari RSI terakhir
          rising       — True jika RSI trending naik (2+ candle naik)
          falling      — True jika RSI trending turun (2+ candle turun)
          sma_cross_up   — RSI cross ke atas SMA dalam lookback
          sma_cross_down — RSI cross ke bawah SMA dalam lookback
          was_oversold   — RSI pernah < 35 dalam lookback candle
          was_overbought — RSI pernah > 65 dalam lookback candle
          oversold     — RSI sekarang < 35
          overbought   — RSI sekarang > 65
    """
    if df is None or len(df) < rsi_period + sma_period + 5:
        return None

    close = df['close']
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(rsi_period).mean()
    rs = gain / loss
    rsi_series = 100 - (100 / (1 + rs))

    # SMA dari RSI
    rsi_sma = rsi_series.rolling(sma_period).mean()

    rsi_now = rsi_series.iloc[-1]
    rsi_prev = rsi_series.iloc[-2]
    sma_now = rsi_sma.iloc[-1]
    sma_prev = rsi_sma.iloc[-2]

    if any(np.isnan(x) for x in [rsi_now, rsi_prev, sma_now, sma_prev]):
        return None

    # Rising/falling: cek 2+ candle berturut-turut naik/turun
    rising = False
    falling = False
    if len(rsi_series) >= 3:
        r0 = rsi_series.iloc[-1]
        r1 = rsi_series.iloc[-2]
        r2 = rsi_series.iloc[-3]
        if not any(np.isnan(x) for x in [r0, r1, r2]):
            rising = r0 > r1 and r1 > r2   # 2 candle naik berturut
            falling = r0 < r1 and r1 < r2   # 2 candle turun berturut
    # Fallback: minimal 1 candle naik/turun
    if not rising and not falling:
        rising = rsi_now > rsi_prev
        falling = rsi_now < rsi_prev

    # Cek oversold/overbought dalam lookback window
    was_oversold = False
    was_overbought = False
    for i in range(1, lookback + 2):  # current + lookback candle lalu
        if i > len(rsi_series):
            break
        val = rsi_series.iloc[-i]
        if not np.isnan(val):
            if val < 35:
                was_oversold = True
            if val > 65:
                was_overbought = True

    # SMA crossover dalam lookback window
    sma_cross_up = False
    sma_cross_down = False
    for i in range(1, lookback + 1):
        if i + 1 > len(rsi_series):
            break
        r_i = rsi_series.iloc[-i]
        r_prev = rsi_series.iloc[-i - 1]
        s_i = rsi_sma.iloc[-i]
        s_prev = rsi_sma.iloc[-i - 1]
        if any(np.isnan(x) for x in [r_i, r_prev, s_i, s_prev]):
            continue
        if r_prev <= s_prev and r_i > s_i:
            sma_cross_up = True
        if r_prev >= s_prev and r_i < s_i:
            sma_cross_down = True

    return {
        'rsi': rsi_now,
        'rsi_prev': rsi_prev,
        'rsi_sma': sma_now,
        'rising': rising,
        'falling': falling,
        'sma_cross_up': sma_cross_up,
        'sma_cross_down': sma_cross_down,
        'was_oversold': was_oversold,
        'was_overbought': was_overbought,
        'oversold': rsi_now < 35,
        'overbought': rsi_now > 65,
    }


# =========================================================
#  3. MACD (12, 26, 9)
# =========================================================
def calc_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26,
              signal_period: int = 9, lookback: int = 3) -> dict:
    """
    Hitung MACD dan deteksi crossover + histogram direction.

    lookback: cek crossover dalam N candle terakhir.
    MACD crossover adalah event sesaat — hanya terjadi di 1 candle.
    Dengan lookback, kita menangkap crossover yang baru terjadi
    meskipun bukan di candle paling akhir.

    Returns:
        dict dengan keys:
          macd_line     — MACD line terakhir
          signal_line   — Signal line terakhir
          histogram     — Histogram terakhir
          cross_up      — MACD cross ke atas signal dalam lookback candle
          cross_down    — MACD cross ke bawah signal dalam lookback candle
          macd_above    — MACD line di atas signal line sekarang
          macd_below    — MACD line di bawah signal line sekarang
          hist_positive — Histogram > 0
          hist_negative — Histogram < 0
          hist_growing  — |histogram| membesar (momentum bertambah)
    """
    if df is None or len(df) < slow + signal_period + 5:
        return None

    close = df['close']
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line

    m_now = macd_line.iloc[-1]
    s_now = signal_line.iloc[-1]
    h_now = histogram.iloc[-1]
    h_prev = histogram.iloc[-2]

    if np.isnan(m_now) or np.isnan(s_now):
        return None

    # Cek crossover dalam lookback candle terakhir
    cross_up = False
    cross_down = False
    for i in range(1, lookback + 1):
        if i + 1 > len(macd_line):
            break
        m_i = macd_line.iloc[-i]
        m_prev = macd_line.iloc[-i - 1]
        s_i = signal_line.iloc[-i]
        s_prev = signal_line.iloc[-i - 1]
        if np.isnan(m_i) or np.isnan(m_prev) or \
           np.isnan(s_i) or np.isnan(s_prev):
            continue
        if m_prev <= s_prev and m_i > s_i:
            cross_up = True
        if m_prev >= s_prev and m_i < s_i:
            cross_down = True

    return {
        'macd_line': m_now,
        'signal_line': s_now,
        'histogram': h_now,
        'cross_up': cross_up,
        'cross_down': cross_down,
        'macd_above': m_now > s_now,
        'macd_below': m_now < s_now,
        'hist_positive': h_now > 0,
        'hist_negative': h_now < 0,
        'hist_growing': abs(h_now) > abs(h_prev),
    }


# =========================================================
#  4. WEDGE / CHANNEL DETECTION
# =========================================================
def _find_swing_points(df: pd.DataFrame, window: int = 3,
                       max_points: int = 30) -> tuple:
    """
    Cari swing highs dan swing lows dari data OHLC.
    Setiap swing point = (index_posisi, harga).

    window: jumlah candle kiri/kanan untuk konfirmasi swing.
    max_points: ambil N terakhir saja (yang paling relevan).
    """
    highs = df['high'].values
    lows = df['low'].values
    n = len(highs)

    swing_highs = []
    swing_lows = []

    for i in range(window, n - window):
        if highs[i] >= max(highs[i - window:i]) and \
           highs[i] >= max(highs[i + 1:i + window + 1]):
            swing_highs.append((i, highs[i]))

        if lows[i] <= min(lows[i - window:i]) and \
           lows[i] <= min(lows[i + 1:i + window + 1]):
            swing_lows.append((i, lows[i]))

    return swing_highs[-max_points:], swing_lows[-max_points:]


def _fit_trendline(points: list) -> tuple:
    """
    Fit linear regression ke list of (index, price).
    Returns (slope, intercept) atau None jika tidak cukup data.
    """
    if len(points) < 2:
        return None
    x = np.array([p[0] for p in points])
    y = np.array([p[1] for p in points])
    try:
        coeffs = np.polyfit(x, y, 1)
        return float(coeffs[0]), float(coeffs[1])
    except (np.linalg.LinAlgError, ValueError):
        return None


def detect_wedge_channel(df: pd.DataFrame, lookback: int = 50,
                         swing_window: int = 3) -> dict:
    """
    Deteksi wedge dan channel dari swing points.

    Syarat minimum: 2 swing highs + 2 swing lows (4 titik total).

    Patterns:
      - falling_wedge   : kedua trendline turun, converging → LONG breakout
      - rising_wedge    : kedua trendline naik, converging → SHORT breakdown
      - desc_channel    : kedua trendline turun, roughly parallel → LONG breakout
      - asc_channel     : kedua trendline naik, roughly parallel → SHORT breakdown

    Returns:
        dict dengan keys:
          pattern      — 'falling_wedge'|'rising_wedge'|'desc_channel'|'asc_channel'|None
          direction    — 'LONG' atau 'SHORT' (arah breakout yang diharapkan)
          upper_slope  — slope trendline atas
          lower_slope  — slope trendline bawah
          upper_bound  — nilai trendline atas saat ini
          lower_bound  — nilai trendline bawah saat ini
          breakout     — True jika harga sudah breakout dari pattern
          confidence   — 0-100 confidence berdasarkan jumlah touch points
    """
    result = {
        'pattern': None, 'direction': None,
        'upper_slope': 0, 'lower_slope': 0,
        'upper_bound': 0, 'lower_bound': 0,
        'breakout': False, 'confidence': 0,
    }

    if df is None or len(df) < lookback:
        return result

    # Ambil data lookback terakhir
    df_look = df.iloc[-lookback:]
    swing_highs, swing_lows = _find_swing_points(df_look, swing_window)

    # Minimum 2 highs + 2 lows
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return result

    # Fit trendlines dari 4 titik terakhir masing-masing
    upper_line = _fit_trendline(swing_highs[-4:])
    lower_line = _fit_trendline(swing_lows[-4:])

    if upper_line is None or lower_line is None:
        return result

    upper_slope, upper_intercept = upper_line
    lower_slope, lower_intercept = lower_line

    # Hitung nilai trendline di candle terakhir
    last_idx = len(df_look) - 1
    upper_at_now = upper_slope * last_idx + upper_intercept
    lower_at_now = lower_slope * last_idx + lower_intercept

    # Hitung trendline di awal lookback untuk cek converging
    upper_at_start = upper_slope * 0 + upper_intercept
    lower_at_start = lower_slope * 0 + lower_intercept

    width_start = upper_at_start - lower_at_start
    width_now = upper_at_now - lower_at_now

    # Avoid division by zero
    if width_start == 0:
        return result

    converging = width_now < width_start * 0.75  # Channel menyempit > 25%
    parallel = 0.5 <= (width_now / width_start) <= 1.2  # Kurang lebih paralel

    both_down = upper_slope < 0 and lower_slope < 0
    both_up = upper_slope > 0 and lower_slope > 0

    price_now = df['close'].iloc[-1]

    pattern = None
    direction = None

    if both_down and converging:
        pattern = 'falling_wedge'
        direction = 'LONG'
    elif both_up and converging:
        pattern = 'rising_wedge'
        direction = 'SHORT'
    elif both_down and parallel:
        pattern = 'desc_channel'
        direction = 'LONG'
    elif both_up and parallel:
        pattern = 'asc_channel'
        direction = 'SHORT'

    if pattern is None:
        return result

    # Cek breakout: harga menembus trendline
    breakout = False
    if direction == 'LONG' and price_now > upper_at_now:
        breakout = True
    elif direction == 'SHORT' and price_now < lower_at_now:
        breakout = True

    # Confidence berdasarkan jumlah touch points
    total_points = len(swing_highs[-4:]) + len(swing_lows[-4:])
    confidence = min(100, total_points * 15)  # 4 pts = 60, 8 pts = 100

    result.update({
        'pattern': pattern,
        'direction': direction,
        'upper_slope': upper_slope,
        'lower_slope': lower_slope,
        'upper_bound': upper_at_now,
        'lower_bound': lower_at_now,
        'breakout': breakout,
        'confidence': confidence,
    })
    return result


# =========================================================
#  5. CANDLE KONFIRMASI (dari candle_patterns.py existing)
# =========================================================

# Pattern yang diterima untuk LONG
LONG_CONFIRM_PATTERNS = {
    'Hammer', 'Bullish Engulfing', 'Inverted Hammer',
    'Morning Star', 'Morning Doji Star', 'Piercing Line',
    'Dragonfly Doji', 'Three White Soldiers', 'Bullish Harami',
    'Tweezer Bottom', 'Three Inside Up', 'Kicker Bullish',
}

# Pattern yang diterima untuk SHORT
SHORT_CONFIRM_PATTERNS = {
    'Shooting Star', 'Bearish Engulfing', 'Hanging Man',
    'Evening Star', 'Evening Doji Star', 'Dark Cloud Cover',
    'Gravestone Doji', 'Three Black Crows', 'Bearish Harami',
    'Tweezer Top', 'Three Inside Down', 'Kicker Bearish',
}


def check_candle_confirmation(df: pd.DataFrame, atr: float,
                              direction: str,
                              lookback: int = 2) -> dict:
    """
    Cek apakah ada candle konfirmasi yang sesuai arah.

    lookback: cek pattern di N posisi terakhir.
    Misalnya lookback=2: cek candle terakhir DAN 1 candle sebelumnya.
    Ini menangkap konfirmasi yang terjadi di candle sebelum trigger.

    Returns:
        dict:
          confirmed — True jika ada candle pattern yang cocok
          pattern   — nama pattern
          strength  — kekuatan pattern (1-3)
    """
    result = {'confirmed': False, 'pattern': None, 'strength': 0}

    if df is None or len(df) < 6 or atr <= 0:
        return result

    target_patterns = LONG_CONFIRM_PATTERNS if direction == 'LONG' \
        else SHORT_CONFIRM_PATTERNS
    target_dir = 'BULLISH' if direction == 'LONG' else 'BEARISH'

    # Cek pattern di beberapa posisi terakhir
    best = result.copy()
    for offset in range(lookback):
        end_idx = len(df) - offset
        if end_idx < 5:
            break

        opens = df['open'].iloc[end_idx - 5:end_idx].tolist()
        highs = df['high'].iloc[end_idx - 5:end_idx].tolist()
        lows = df['low'].iloc[end_idx - 5:end_idx].tolist()
        closes = df['close'].iloc[end_idx - 5:end_idx].tolist()

        pattern = detect_candle_pattern(opens, highs, lows, closes, atr)

        if not pattern.get('found'):
            continue

        name = pattern.get('pattern', '')
        pat_dir = pattern.get('direction', '')
        strength = pattern.get('strength', 0)

        if pat_dir == target_dir and name in target_patterns:
            if strength > best['strength']:
                best = {'confirmed': True, 'pattern': name,
                        'strength': strength}

    return best


# =========================================================
#  6. SL / TP CALCULATION
# =========================================================
def calc_sl_tp(price: float, atr: float, direction: str,
               wedge: dict, bb: dict) -> dict:
    """
    Hitung Stop Loss dan Take Profit levels.

    SL logic:
      - Jika ada wedge/channel: SL di luar batas pattern + 0.3% buffer
      - Jika dari BB: SL di luar BB + ATR * 0.3
      - Ambil yang lebih protektif (SL lebih jauh dari entry)

    TP logic:
      - TP1: 1.0x jarak SL dari entry
      - TP2: 1.8x jarak SL dari entry
      - TP3: 2.5x jarak SL dari entry

    Returns:
        dict: sl, tp1, tp2, tp3, risk (jarak entry-SL)
    """
    sl_candidates = []

    # --- SL dari wedge/channel boundary ---
    if wedge.get('pattern'):
        buffer_pct = 0.003  # 0.3%
        if direction == 'LONG':
            wedge_sl = wedge['lower_bound'] * (1 - buffer_pct)
            sl_candidates.append(wedge_sl)
        else:
            wedge_sl = wedge['upper_bound'] * (1 + buffer_pct)
            sl_candidates.append(wedge_sl)

    # --- SL dari Bollinger Bands + ATR buffer ---
    if bb:
        atr_buffer = atr * 0.3
        if direction == 'LONG':
            bb_sl = bb['lower'] - atr_buffer
            sl_candidates.append(bb_sl)
        else:
            bb_sl = bb['upper'] + atr_buffer
            sl_candidates.append(bb_sl)

    # --- Fallback: ATR-based SL ---
    if not sl_candidates:
        if direction == 'LONG':
            sl_candidates.append(price - atr * 1.5)
        else:
            sl_candidates.append(price + atr * 1.5)

    # Ambil SL yang paling protektif (terjauh dari entry)
    if direction == 'LONG':
        sl = min(sl_candidates)
    else:
        sl = max(sl_candidates)

    risk = abs(price - sl)

    # Safety: risk terlalu kecil → skip
    if risk < price * 0.0005:  # minimal 0.05% dari harga
        risk = price * 0.005  # default 0.5%
        if direction == 'LONG':
            sl = price - risk
        else:
            sl = price + risk

    # TP levels — kembali ke 1.0R (0.8R bikin terlalu banyak BEP)
    if direction == 'LONG':
        tp1 = price + risk * 1.0
        tp2 = price + risk * 1.8
        tp3 = price + risk * 2.5
    else:
        tp1 = price - risk * 1.0
        tp2 = price - risk * 1.8
        tp3 = price - risk * 2.5

    return {
        'sl': sl,
        'tp1': tp1,
        'tp2': tp2,
        'tp3': tp3,
        'risk': risk,
    }


# =========================================================
#  7. ADVANCED FILTERS
# =========================================================
def check_volume_spike(df: pd.DataFrame, lookback_avg: int = 20,
                       spike_mult: float = 1.5) -> dict:
    """
    Cek apakah ada volume spike pada candle terakhir / sebelumnya.

    Volume tinggi saat harga di zona reversal = capitulation/climax.
    Ini menandakan seller/buyer sudah "habis" → reversal lebih reliable.

    Returns:
        spike    — True jika volume > spike_mult × average
        ratio    — volume / average (misal 2.3 = 2.3x average)
        dead     — True jika volume < 0.5x average (pasar mati)
    """
    if df is None or len(df) < lookback_avg + 2:
        return {'spike': False, 'ratio': 1.0, 'dead': False}

    vol = df['volume'].values
    avg_vol = float(np.mean(vol[-lookback_avg - 1:-1]))  # avg tanpa candle terakhir

    if avg_vol <= 0:
        return {'spike': False, 'ratio': 1.0, 'dead': False}

    # Cek 2 candle terakhir (touch + current)
    max_recent_vol = max(vol[-1], vol[-2])
    ratio = max_recent_vol / avg_vol

    return {
        'spike': ratio >= spike_mult,
        'ratio': round(ratio, 2),
        'dead': ratio < 0.5,
    }


def check_momentum_exhaustion(df: pd.DataFrame, direction: str) -> dict:
    """
    Cek apakah momentum arah sebelumnya sudah melemah (exhaustion).

    Untuk LONG: cek apakah candle bearish terakhir body-nya lebih kecil
    dari candle bearish sebelumnya → seller kehilangan tenaga.

    Untuk SHORT: mirror — candle bullish mengecil.

    Returns:
        exhausted — True jika momentum melemah
        desc      — deskripsi singkat
    """
    if df is None or len(df) < 5:
        return {'exhausted': False, 'desc': ''}

    bodies = []
    for i in range(-4, 0):
        o = df['open'].iloc[i]
        c = df['close'].iloc[i]
        body = c - o  # positif = bullish, negatif = bearish
        bodies.append(body)

    if direction == 'LONG':
        # Cari candle bearish (body negatif) dan cek apakah mengecil
        bear_bodies = [abs(b) for b in bodies if b < 0]
        if len(bear_bodies) >= 2:
            # Body bearish terakhir lebih kecil dari sebelumnya
            if bear_bodies[-1] < bear_bodies[-2]:
                return {'exhausted': True,
                        'desc': 'Bearish body shrinking (seller exhausting)'}
        # Alternatif: ada candle hijau di antara candle merah terakhir
        last_3_dirs = ['bull' if b > 0 else 'bear' for b in bodies[-3:]]
        if 'bull' in last_3_dirs:
            return {'exhausted': True,
                    'desc': 'Buying pressure emerging (green among reds)'}

    elif direction == 'SHORT':
        # Mirror: cari candle bullish yang mengecil
        bull_bodies = [abs(b) for b in bodies if b > 0]
        if len(bull_bodies) >= 2:
            if bull_bodies[-1] < bull_bodies[-2]:
                return {'exhausted': True,
                        'desc': 'Bullish body shrinking (buyer exhausting)'}
        last_3_dirs = ['bear' if b < 0 else 'bull' for b in bodies[-3:]]
        if 'bear' in last_3_dirs:
            return {'exhausted': True,
                    'desc': 'Selling pressure emerging (red among greens)'}

    return {'exhausted': False, 'desc': ''}


def check_price_overextension(df: pd.DataFrame, atr: float,
                              direction: str,
                              threshold_atr: float = 1.5) -> dict:
    """
    Cek apakah harga overextended (jauh) dari EMA21.

    Harga yang jauh dari EMA = genuine oversold/overbought,
    bukan noise random. Ini meningkatkan probabilitas reversal.

    threshold_atr: harga harus minimal 1.5 ATR dari EMA21.

    Returns:
        overextended — True jika harga cukup jauh dari EMA21
        distance_atr — jarak dalam ATR units
    """
    if df is None or len(df) < 25 or atr <= 0:
        return {'overextended': False, 'distance_atr': 0}

    close = df['close']
    ema21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
    price = close.iloc[-1]

    distance = abs(price - ema21)
    distance_atr = distance / atr

    if direction == 'LONG':
        # Untuk LONG, harga harus DI BAWAH EMA21
        overextended = price < ema21 and distance_atr >= threshold_atr
    else:
        # Untuk SHORT, harga harus DI ATAS EMA21
        overextended = price > ema21 and distance_atr >= threshold_atr

    return {
        'overextended': overextended,
        'distance_atr': round(distance_atr, 2),
    }


# =========================================================
#  7B. S&R PROXIMITY + STRONG CANDLE CLOSE
# =========================================================
def detect_sr_levels(df: pd.DataFrame, window: int = 5,
                     max_levels: int = 5) -> dict:
    """
    Deteksi support dan resistance dari swing high/low.

    Swing bot WR 63.8% karena entry hanya di S&R level.
    Scalping bot harus punya awareness yang sama:
    BB touch DI DEKAT support = reversal sungguhan.
    BB touch TANPA support = tebak-tebakan.

    Returns:
        supports    — list of recent support prices (swing lows)
        resistances — list of recent resistance prices (swing highs)
    """
    if df is None or len(df) < window * 2 + 5:
        return {'supports': [], 'resistances': []}

    highs = df['high'].values
    lows = df['low'].values
    n = len(highs)

    supports = []
    resistances = []

    for i in range(window, n - window):
        # Swing low = support
        if lows[i] <= min(lows[i - window:i]) and \
           lows[i] <= min(lows[i + 1:i + window + 1]):
            supports.append(float(lows[i]))

        # Swing high = resistance
        if highs[i] >= max(highs[i - window:i]) and \
           highs[i] >= max(highs[i + 1:i + window + 1]):
            resistances.append(float(highs[i]))

    return {
        'supports': supports[-max_levels:],
        'resistances': resistances[-max_levels:],
    }


def check_sr_proximity(price: float, atr: float, direction: str,
                       sr_levels: dict,
                       proximity_atr: float = 1.0) -> dict:
    """
    Cek apakah harga dekat dengan S&R level.

    Untuk LONG: harga harus dekat support (swing low).
    Untuk SHORT: harga harus dekat resistance (swing high).

    proximity_atr: jarak maksimal dalam ATR units.

    Returns:
        near_level  — True jika dekat S&R
        level_price — harga S&R terdekat
        distance    — jarak dalam ATR
    """
    if atr <= 0:
        return {'near_level': False, 'level_price': 0, 'distance': 99}

    levels = sr_levels.get('supports', []) if direction == 'LONG' \
        else sr_levels.get('resistances', [])

    if not levels:
        return {'near_level': False, 'level_price': 0, 'distance': 99}

    # Cari level terdekat
    best_dist = 99
    best_level = 0
    for lv in levels:
        dist = abs(price - lv) / atr
        if dist < best_dist:
            best_dist = dist
            best_level = lv

    return {
        'near_level': best_dist <= proximity_atr,
        'level_price': best_level,
        'distance': round(best_dist, 2),
    }


def check_rejection_wick(df: pd.DataFrame, direction: str,
                         wick_ratio: float = 1.5) -> dict:
    """
    Cek apakah candle punya rejection wick kuat.

    LONG: lower wick >= wick_ratio × body (hammer-like)
    SHORT: upper wick >= wick_ratio × body (shooting-star-like)

    Ini filter KUNCI: wick panjang = smart money rejected level.
    Tanpa wick = harga cuma drift, bukan reversal.
    """
    if df is None or len(df) < 3:
        return {'has_rejection': False, 'wick_ratio': 0}

    best_ratio = 0
    has_rejection = False

    for idx in [-1, -2]:
        o = df['open'].iloc[idx]
        h = df['high'].iloc[idx]
        l = df['low'].iloc[idx]
        c = df['close'].iloc[idx]

        body = abs(c - o)
        if body <= 0:
            body = (h - l) * 0.1

        if direction == 'LONG':
            lower_wick = min(o, c) - l
            ratio = lower_wick / body if body > 0 else 0
        else:
            upper_wick = h - max(o, c)
            ratio = upper_wick / body if body > 0 else 0

        if ratio >= wick_ratio:
            has_rejection = True
        if ratio > best_ratio:
            best_ratio = ratio

    return {
        'has_rejection': has_rejection,
        'wick_ratio': round(best_ratio, 2),
    }


def check_volume_climax(df: pd.DataFrame,
                        direction: str,
                        avg_period: int = 20,
                        climax_mult: float = 2.0) -> dict:
    """
    Detect volume climax — smart money accumulation/distribution signal.

    Climax = volume spike > 2x average DI CANDLE REVERSAL (wick besar).
    Ini tanda whale buy/sell di level tertentu.

    Untuk LONG: bullish climax = big down candle dengan wick bawah panjang + volume
    Untuk SHORT: bearish climax = big up candle dengan wick atas panjang + volume

    Returns:
        has_climax: bool — climax detected dalam 3 candle terakhir
        ratio: volume / avg_volume (e.g. 2.5x)
        candle_type: 'SELLER_EXHAUSTION' | 'BUYER_EXHAUSTION' | 'NONE'
    """
    result = {
        'has_climax': False,
        'ratio': 1.0,
        'candle_type': 'NONE',
    }

    if df is None or len(df) < avg_period + 5:
        return result

    # Average volume excluding last 3 candles
    avg_vol = float(df['volume'].iloc[-avg_period - 3:-3].mean())
    if avg_vol <= 0:
        return result

    # Check last 3 candles for climax
    for idx in [-1, -2, -3]:
        vol = float(df['volume'].iloc[idx])
        if vol < avg_vol * climax_mult:
            continue

        o = float(df['open'].iloc[idx])
        h = float(df['high'].iloc[idx])
        l = float(df['low'].iloc[idx])
        c = float(df['close'].iloc[idx])
        body = abs(c - o)
        total = h - l

        if total <= 0:
            continue

        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)

        if direction == 'LONG':
            # Seller exhaustion: long lower wick + volume (whale bought the dip)
            if lower_wick > body * 1.5 and lower_wick > total * 0.4:
                result.update({
                    'has_climax': True,
                    'ratio': vol / avg_vol,
                    'candle_type': 'SELLER_EXHAUSTION',
                })
                return result

        elif direction == 'SHORT':
            # Buyer exhaustion: long upper wick + volume (whale sold the rip)
            if upper_wick > body * 1.5 and upper_wick > total * 0.4:
                result.update({
                    'has_climax': True,
                    'ratio': vol / avg_vol,
                    'candle_type': 'BUYER_EXHAUSTION',
                })
                return result

    return result


def find_latest_swing(df: pd.DataFrame, lookback: int = 50,
                      swing_window: int = 3) -> dict:
    """
    Find latest significant swing high and swing low.

    Returns:
        swing_high: (idx, price) or None
        swing_low: (idx, price) or None
        swing_range: (high - low) / low * 100 (dalam %)
        direction: 'UP' | 'DOWN' | 'UNCLEAR'
    """
    if df is None or len(df) < lookback:
        return {'swing_high': None, 'swing_low': None,
                'swing_range': 0, 'direction': 'UNCLEAR'}

    df_slice = df.iloc[-lookback:].reset_index(drop=True)
    highs = df_slice['high'].values
    lows = df_slice['low'].values
    n = len(df_slice)

    swing_highs = []
    swing_lows = []

    for i in range(swing_window, n - swing_window):
        if highs[i] >= max(highs[i - swing_window:i]) and \
           highs[i] >= max(highs[i + 1:i + swing_window + 1]):
            swing_highs.append((i, float(highs[i])))
        if lows[i] <= min(lows[i - swing_window:i]) and \
           lows[i] <= min(lows[i + 1:i + swing_window + 1]):
            swing_lows.append((i, float(lows[i])))

    if not swing_highs or not swing_lows:
        return {'swing_high': None, 'swing_low': None,
                'swing_range': 0, 'direction': 'UNCLEAR'}

    # Get latest
    latest_high = swing_highs[-1]
    latest_low = swing_lows[-1]

    # Determine direction based on which is more recent
    if latest_high[0] > latest_low[0]:
        # Swing high lebih recent = last move was UP
        direction = 'UP'
        swing_range = (latest_high[1] - latest_low[1]) / latest_low[1] * 100
    else:
        # Swing low lebih recent = last move was DOWN
        direction = 'DOWN'
        swing_range = (latest_high[1] - latest_low[1]) / latest_low[1] * 100

    return {
        'swing_high': latest_high,
        'swing_low': latest_low,
        'swing_range': swing_range,
        'direction': direction,
    }


def calculate_fib_levels(swing_low: float, swing_high: float) -> dict:
    """
    Calculate Fibonacci retracement levels.

    Standard Fib levels: 0.236, 0.382, 0.5, 0.618, 0.786
    Sweet spot untuk entry: 0.5 - 0.618 (golden pocket).
    """
    if swing_high <= swing_low:
        return {}

    diff = swing_high - swing_low
    return {
        '0.0':    swing_high,           # full retest
        '0.236':  swing_high - diff * 0.236,
        '0.382':  swing_high - diff * 0.382,
        '0.5':    swing_high - diff * 0.5,
        '0.618':  swing_high - diff * 0.618,  # golden ratio
        '0.786':  swing_high - diff * 0.786,  # deep pullback
        '1.0':    swing_low,            # full retrace
    }


def check_fib_pullback(df: pd.DataFrame, direction: str,
                       min_swing_pct: float = 1.0) -> dict:
    """
    Cek apakah harga sedang di Fibonacci retracement level yang valid.

    UPTREND (LONG):
      - Find last swing: low → high
      - Price harus retrace ke 0.382 - 0.786 (sweet spot 0.5-0.618)
      - Target TP: return ke 0.0 (swing high)

    DOWNTREND (SHORT):
      - Find last swing: high → low
      - Price rally ke 0.382 - 0.786 dari swing
      - Target TP: return ke swing low

    Returns:
        valid: bool — price di Fib level yang acceptable
        level: nama level (e.g. '0.5', '0.618')
        swing_pct: size of swing dalam %
        quality: 'GOLDEN' (0.5-0.618) | 'OK' (0.382-0.786) | 'NONE'
    """
    result = {
        'valid': False,
        'level': None,
        'swing_pct': 0,
        'quality': 'NONE',
        'fib_levels': {},
    }

    swing = find_latest_swing(df, lookback=50, swing_window=3)
    if not swing['swing_high'] or not swing['swing_low']:
        return result

    swing_pct = swing['swing_range']
    if swing_pct < min_swing_pct:
        return result  # swing terlalu kecil, not meaningful

    swing_high_price = swing['swing_high'][1]
    swing_low_price = swing['swing_low'][1]
    swing_high_idx = swing['swing_high'][0]
    swing_low_idx = swing['swing_low'][0]

    fib_levels = calculate_fib_levels(swing_low_price, swing_high_price)
    result['fib_levels'] = fib_levels
    result['swing_pct'] = swing_pct

    price = float(df['close'].iloc[-1])

    if direction == 'LONG':
        # Must be UPTREND swing (low → high)
        if swing_low_idx >= swing_high_idx:
            return result  # last move was DOWN, not pullback in UP

        # Price must be below high but not below the low
        if not (swing_low_price < price < swing_high_price):
            return result

        # Check which fib level price is at (with tolerance)
        tolerance = (swing_high_price - swing_low_price) * 0.05

        for level_name, level_price in fib_levels.items():
            if level_name in ('0.0', '1.0'):
                continue
            if abs(price - level_price) <= tolerance:
                # Classify quality
                if level_name in ('0.5', '0.618'):
                    quality = 'GOLDEN'
                elif level_name in ('0.382', '0.786'):
                    quality = 'OK'
                else:
                    quality = 'WEAK'

                result.update({
                    'valid': True,
                    'level': level_name,
                    'quality': quality,
                })
                return result

    elif direction == 'SHORT':
        # Must be DOWNTREND swing (high → low)
        if swing_high_idx >= swing_low_idx:
            return result

        # Price rally from low, not yet back to high
        if not (swing_low_price < price < swing_high_price):
            return result

        # For downtrend pullback, fib levels are measured from low up
        diff = swing_high_price - swing_low_price
        fib_short = {
            '0.236': swing_low_price + diff * 0.236,
            '0.382': swing_low_price + diff * 0.382,
            '0.5':   swing_low_price + diff * 0.5,
            '0.618': swing_low_price + diff * 0.618,
            '0.786': swing_low_price + diff * 0.786,
        }

        tolerance = diff * 0.05
        for level_name, level_price in fib_short.items():
            if abs(price - level_price) <= tolerance:
                if level_name in ('0.5', '0.618'):
                    quality = 'GOLDEN'
                elif level_name in ('0.382', '0.786'):
                    quality = 'OK'
                else:
                    quality = 'WEAK'

                result.update({
                    'valid': True,
                    'level': level_name,
                    'quality': quality,
                    'fib_levels': fib_short,
                })
                return result

    return result


def detect_order_blocks_scalp(df: pd.DataFrame, atr: float) -> dict:
    """
    Order Block detection adapted from Bot 1 SMC for 15m scalping.

    Order Block = candle terakhir sebelum impulse move.
    - Bullish OB: bearish candle sebelum impulse naik (institusi BUY)
    - Bearish OB: bullish candle sebelum impulse turun (institusi SELL)

    Valid OB requires:
      1. Impulse move >= 1.5x ATR within 3 candles
      2. OB candle volume >= 80% average
      3. Fresh (belum fully mitigated)

    Returns:
        bull_obs: list of bullish order blocks
        bear_obs: list of bearish order blocks
    """
    if df is None or len(df) < 20 or atr <= 0:
        return {'bull_obs': [], 'bear_obs': []}

    o = df['open'].values
    h = df['high'].values
    l = df['low'].values
    c = df['close'].values
    v = df['volume'].values if 'volume' in df.columns else np.ones(len(c))
    n = len(c)

    avg_vol = float(np.mean(v[-50:])) if n >= 50 else float(np.mean(v))
    min_move = atr * 1.5
    min_vol = avg_vol * 0.8

    bull_obs = []
    bear_obs = []

    for i in range(1, n - 3):
        ob_vol = float(v[i])
        if ob_vol < min_vol:
            continue

        # Bullish OB: bearish candle → impulse naik
        if c[i] < o[i]:
            future_slice = c[i + 1:min(i + 4, n)]
            if len(future_slice) == 0:
                continue
            move_up = max(future_slice) - h[i]
            if move_up >= min_move:
                ob_low = min(o[i], c[i])
                ob_high = max(o[i], c[i])
                ob_mid = (ob_low + ob_high) / 2
                fresh = all(l[j] > ob_mid for j in range(i + 1, n))
                bull_obs.append({
                    'low': ob_low,
                    'high': ob_high,
                    'mid': ob_mid,
                    'idx': i,
                    'age': n - i,
                    'impulse': move_up,
                    'vol_ratio': ob_vol / max(avg_vol, 1),
                    'fresh': fresh,
                })

        # Bearish OB: bullish candle → impulse turun
        if c[i] > o[i]:
            future_slice = c[i + 1:min(i + 4, n)]
            if len(future_slice) == 0:
                continue
            move_down = l[i] - min(future_slice)
            if move_down >= min_move:
                ob_low = min(o[i], c[i])
                ob_high = max(o[i], c[i])
                ob_mid = (ob_low + ob_high) / 2
                fresh = all(h[j] < ob_mid for j in range(i + 1, n))
                bear_obs.append({
                    'low': ob_low,
                    'high': ob_high,
                    'mid': ob_mid,
                    'idx': i,
                    'age': n - i,
                    'impulse': move_down,
                    'vol_ratio': ob_vol / max(avg_vol, 1),
                    'fresh': fresh,
                })

    # Keep only last 5 of each, sorted by age (nearest first)
    bull_obs = sorted(bull_obs, key=lambda x: x['age'])[:5]
    bear_obs = sorted(bear_obs, key=lambda x: x['age'])[:5]

    return {'bull_obs': bull_obs, 'bear_obs': bear_obs}


def check_order_block_test(df: pd.DataFrame, direction: str,
                           atr: float) -> dict:
    """
    Cek apakah harga SEKARANG sedang test order block.

    Untuk LONG: cek apakah harga di dalam zone bullish OB (retest).
    Untuk SHORT: cek apakah harga di dalam zone bearish OB.

    Returns:
        at_ob: True kalau harga test OB
        ob: order block dict
        fresh: True kalau OB masih fresh
    """
    if df is None or atr <= 0:
        return {'at_ob': False, 'ob': None, 'fresh': False}

    obs = detect_order_blocks_scalp(df, atr)
    price = float(df['close'].iloc[-1])

    candidates = obs['bull_obs'] if direction == 'LONG' else obs['bear_obs']

    for ob in candidates:
        # Price within OB zone (with ATR tolerance)
        tolerance = atr * 0.3
        if direction == 'LONG':
            if ob['low'] - tolerance <= price <= ob['high'] + tolerance:
                return {
                    'at_ob': True,
                    'ob': ob,
                    'fresh': ob['fresh'],
                }
        else:
            if ob['low'] - tolerance <= price <= ob['high'] + tolerance:
                return {
                    'at_ob': True,
                    'ob': ob,
                    'fresh': ob['fresh'],
                }

    return {'at_ob': False, 'ob': None, 'fresh': False}


def detect_rsi_divergence(df: pd.DataFrame, direction: str,
                          lookback: int = 30,
                          swing_window: int = 3) -> dict:
    """
    Deteksi RSI divergence (classic bullish/bearish).

    BULLISH DIVERGENCE (for LONG):
      - Price makes lower low
      - RSI makes higher low
      - Berarti: momentum melemah di sell side, reversal incoming

    BEARISH DIVERGENCE (for SHORT):
      - Price makes higher high
      - RSI makes lower high
      - Berarti: momentum melemah di buy side

    Ini SINGLE INDICATOR paling powerful untuk reversal detection.
    Historical WR: 60-70% kalau clear divergence.

    Returns:
        has_divergence: bool
        strength: 'STRONG' | 'MODERATE' | 'WEAK' | 'NONE'
        price_points: list of (idx, price)
        rsi_points: list of (idx, rsi)
    """
    result = {
        'has_divergence': False,
        'strength': 'NONE',
        'price_points': [],
        'rsi_points': [],
    }

    if df is None or len(df) < lookback + 5:
        return result

    # Compute RSI 14
    close = df['close']
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    # Take last N candles
    df_slice = df.iloc[-lookback:].reset_index(drop=True)
    rsi_slice = rsi.iloc[-lookback:].reset_index(drop=True)
    n = len(df_slice)

    if n < 15:
        return result

    lows = df_slice['low'].values
    highs = df_slice['high'].values
    rsi_vals = rsi_slice.values

    # Find swing points
    swing_lows = []
    swing_highs = []
    for i in range(swing_window, n - swing_window):
        if lows[i] <= min(lows[i - swing_window:i]) and \
           lows[i] <= min(lows[i + 1:i + swing_window + 1]):
            if not np.isnan(rsi_vals[i]):
                swing_lows.append((i, float(lows[i]), float(rsi_vals[i])))
        if highs[i] >= max(highs[i - swing_window:i]) and \
           highs[i] >= max(highs[i + 1:i + swing_window + 1]):
            if not np.isnan(rsi_vals[i]):
                swing_highs.append((i, float(highs[i]), float(rsi_vals[i])))

    if direction == 'LONG':
        # Need at least 2 swing lows
        if len(swing_lows) < 2:
            return result

        # Get last 2 swing lows
        sw1 = swing_lows[-2]  # earlier
        sw2 = swing_lows[-1]  # later

        price1, rsi1 = sw1[1], sw1[2]
        price2, rsi2 = sw2[1], sw2[2]

        # Must be significant gap (>= 5 bars apart)
        if sw2[0] - sw1[0] < 5:
            return result

        # Bullish divergence: price lower low, RSI higher low
        price_lower = price2 < price1 * 0.995  # at least 0.5% lower
        rsi_higher = rsi2 > rsi1 + 2  # at least 2 points higher

        if price_lower and rsi_higher:
            # Strength based on gap magnitude
            price_drop = (price1 - price2) / price1 * 100
            rsi_rise = rsi2 - rsi1

            if price_drop > 2 and rsi_rise > 5:
                strength = 'STRONG'
            elif price_drop > 1 and rsi_rise > 3:
                strength = 'MODERATE'
            else:
                strength = 'WEAK'

            result.update({
                'has_divergence': True,
                'strength': strength,
                'price_points': [(sw1[0], price1), (sw2[0], price2)],
                'rsi_points': [(sw1[0], rsi1), (sw2[0], rsi2)],
            })

    elif direction == 'SHORT':
        if len(swing_highs) < 2:
            return result

        sw1 = swing_highs[-2]
        sw2 = swing_highs[-1]

        price1, rsi1 = sw1[1], sw1[2]
        price2, rsi2 = sw2[1], sw2[2]

        if sw2[0] - sw1[0] < 5:
            return result

        # Bearish divergence: price higher high, RSI lower high
        price_higher = price2 > price1 * 1.005
        rsi_lower = rsi2 < rsi1 - 2

        if price_higher and rsi_lower:
            price_rise = (price2 - price1) / price1 * 100
            rsi_drop = rsi1 - rsi2

            if price_rise > 2 and rsi_drop > 5:
                strength = 'STRONG'
            elif price_rise > 1 and rsi_drop > 3:
                strength = 'MODERATE'
            else:
                strength = 'WEAK'

            result.update({
                'has_divergence': True,
                'strength': strength,
                'price_points': [(sw1[0], price1), (sw2[0], price2)],
                'rsi_points': [(sw1[0], rsi1), (sw2[0], rsi2)],
            })

    return result


def check_strong_candle_close(df: pd.DataFrame,
                              direction: str) -> bool:
    """
    Cek apakah candle terakhir close KUAT ke arah yang diharapkan.

    LONG: candle terakhir harus close DI ATAS high candle sebelumnya.
    Ini menandakan buyer berhasil "reclaim" level — bukan cuma wick.

    SHORT: candle terakhir harus close DI BAWAH low candle sebelumnya.

    Ini filter paling sederhana tapi paling powerful untuk buang
    false reversal: kalau harga tidak bisa close di atas/bawah level
    sebelumnya, reversal belum terkonfirmasi.
    """
    if df is None or len(df) < 3:
        return False

    curr_close = df['close'].iloc[-1]
    prev_high = df['high'].iloc[-2]
    prev_low = df['low'].iloc[-2]
    prev_close = df['close'].iloc[-2]

    if direction == 'LONG':
        # Close di atas high sebelumnya ATAU close bullish yang kuat
        # (close > open dan close > prev close)
        strong_close = curr_close > prev_high
        bullish_reclaim = (curr_close > df['open'].iloc[-1] and
                           curr_close > prev_close)
        return strong_close or bullish_reclaim

    else:  # SHORT
        strong_close = curr_close < prev_low
        bearish_reclaim = (curr_close < df['open'].iloc[-1] and
                           curr_close < prev_close)
        return strong_close or bearish_reclaim


# =========================================================
#  7D. ADVANCED FILTERS (v4.3)
# =========================================================
def aggregate_1h_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate 1H data ke 4H (untuk macro trend check).

    Setiap 4 candle 1H = 1 candle 4H.
    Kita tidak butuh tepat aligned — cukup untuk trend bias.
    """
    if df_1h is None or len(df_1h) < 20:
        return None

    # Group per 4 candles
    df = df_1h.copy().reset_index(drop=True)
    n = len(df)
    rows = []
    for i in range(0, n - 3, 4):
        group = df.iloc[i:i + 4]
        rows.append({
            'timestamp': group['timestamp'].iloc[0] if 'timestamp' in group.columns else i,
            'open': group['open'].iloc[0],
            'high': group['high'].max(),
            'low': group['low'].min(),
            'close': group['close'].iloc[-1],
            'volume': group['volume'].sum(),
        })

    if len(rows) < 15:
        return None
    return pd.DataFrame(rows)


def get_4h_trend_bias(df_4h: pd.DataFrame) -> str:
    """
    Cek bias tren di 4H (dari aggregated 1H).

    Simple: EMA21 slope + price vs EMA21.
    Returns: 'BULL' | 'BEAR' | 'NEUTRAL'
    """
    if df_4h is None or len(df_4h) < 25:
        return 'NEUTRAL'

    close = df_4h['close']
    ema21 = close.ewm(span=21, adjust=False).mean()
    price = close.iloc[-1]
    ema_now = ema21.iloc[-1]
    ema_5_ago = ema21.iloc[-5]

    slope_pct = (ema_now - ema_5_ago) / ema_now * 100 if ema_now > 0 else 0

    if price > ema_now and slope_pct > 0.2:
        return 'BULL'
    if price < ema_now and slope_pct < -0.2:
        return 'BEAR'
    return 'NEUTRAL'


def check_volume_pressure(df: pd.DataFrame, lookback: int = 10) -> dict:
    """
    Analisa buying vs selling pressure di recent candles.

    Proxy: bullish candle volume vs bearish candle volume.
    Bullish candle = close > open = mostly buyers
    Bearish candle = close < open = mostly sellers

    Returns:
        buy_pressure_pct: 0-100 (persentase dari total volume yang "buying")
        direction: 'BUYING' | 'SELLING' | 'NEUTRAL'
        strength: 'STRONG' | 'MODERATE' | 'WEAK'
    """
    if df is None or len(df) < lookback + 2:
        return {
            'buy_pressure_pct': 50,
            'direction': 'NEUTRAL',
            'strength': 'WEAK',
        }

    recent = df.iloc[-lookback:]
    buy_vol = 0
    sell_vol = 0

    for i in range(len(recent)):
        o = recent['open'].iloc[i]
        c = recent['close'].iloc[i]
        v = recent['volume'].iloc[i]
        if c > o:
            buy_vol += v
        elif c < o:
            sell_vol += v

    total = buy_vol + sell_vol
    if total <= 0:
        return {
            'buy_pressure_pct': 50,
            'direction': 'NEUTRAL',
            'strength': 'WEAK',
        }

    buy_pct = buy_vol / total * 100

    if buy_pct >= 65:
        direction = 'BUYING'
        strength = 'STRONG' if buy_pct >= 75 else 'MODERATE'
    elif buy_pct <= 35:
        direction = 'SELLING'
        strength = 'STRONG' if buy_pct <= 25 else 'MODERATE'
    else:
        direction = 'NEUTRAL'
        strength = 'WEAK'

    return {
        'buy_pressure_pct': round(buy_pct, 1),
        'direction': direction,
        'strength': strength,
    }


def detect_smc_bos(df: pd.DataFrame, swing_window: int = 5) -> dict:
    """
    SMC-lite: Break of Structure detection.

    Uptrend BOS: harga break above last significant swing high
    Downtrend BOS: harga break below last significant swing low

    Returns:
        bos: 'BULLISH_BOS' | 'BEARISH_BOS' | None
        level_broken: harga yang dipecahkan
        age_bars: berapa bar yang lalu BOS terjadi
    """
    if df is None or len(df) < 30:
        return {'bos': None, 'level_broken': 0, 'age_bars': 0}

    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    n = len(highs)

    # Cari swing highs di 20 bar sebelum 5 bar terakhir
    swing_highs = []
    swing_lows = []

    for i in range(swing_window, n - swing_window - 5):
        if highs[i] >= max(highs[i - swing_window:i]) and \
           highs[i] >= max(highs[i + 1:i + swing_window + 1]):
            swing_highs.append((i, highs[i]))
        if lows[i] <= min(lows[i - swing_window:i]) and \
           lows[i] <= min(lows[i + 1:i + swing_window + 1]):
            swing_lows.append((i, lows[i]))

    # Cek apakah 5 bar terakhir break swing high/low
    recent_max = max(highs[-5:])
    recent_min = min(lows[-5:])

    # Bullish BOS: recent_max > last swing high
    if swing_highs:
        last_swing_high = swing_highs[-1][1]
        swing_high_idx = swing_highs[-1][0]
        if recent_max > last_swing_high:
            return {
                'bos': 'BULLISH_BOS',
                'level_broken': float(last_swing_high),
                'age_bars': n - swing_high_idx,
            }

    # Bearish BOS: recent_min < last swing low
    if swing_lows:
        last_swing_low = swing_lows[-1][1]
        swing_low_idx = swing_lows[-1][0]
        if recent_min < last_swing_low:
            return {
                'bos': 'BEARISH_BOS',
                'level_broken': float(last_swing_low),
                'age_bars': n - swing_low_idx,
            }

    return {'bos': None, 'level_broken': 0, 'age_bars': 0}


# =========================================================
#  7C. TREND DETECTION & PULLBACK (v3)
# =========================================================
def detect_trend_state(df: pd.DataFrame, adx: float) -> dict:
    """
    Deteksi kondisi tren utama — STRONG filter untuk entry.

    Paradigm v3: Trend-following dengan pullback.
    Kita HANYA trade searah tren. Sideways = skip.

    Syarat UPTREND:
      - EMA9 > EMA21 > EMA50 (bullish stack)
      - Harga di atas EMA21
      - ADX >= 20 (trend kuat)
      - Market structure bukan DOWNTREND

    Syarat DOWNTREND:
      - EMA9 < EMA21 < EMA50 (bearish stack)
      - Harga di bawah EMA21
      - ADX >= 20
      - Market structure bukan UPTREND

    Returns:
        state: 'UPTREND' | 'DOWNTREND' | 'SIDEWAYS'
        strength: 0-100
        reason: deskripsi
    """
    if df is None or len(df) < 55:
        return {'state': 'SIDEWAYS', 'strength': 0, 'reason': 'not enough data'}

    close = df['close']
    ema9 = calc_ema(close, 9).iloc[-1]
    ema21 = calc_ema(close, 21).iloc[-1]
    ema50 = calc_ema(close, 50).iloc[-1]
    price = close.iloc[-1]

    # Hitung slope EMA21 (arah tren) — butuh slope KUAT
    ema21_10_ago = calc_ema(close, 21).iloc[-10]
    ema21_slope = (ema21 - ema21_10_ago) / ema21 * 100 if ema21 > 0 else 0

    # EMA9 vs EMA21 gap — harus minimal 0.3% untuk tren kuat
    ema_gap_pct = abs(ema9 - ema21) / ema21 * 100 if ema21 > 0 else 0

    # Cek swing structure dari 20 candle terakhir
    recent_highs = df['high'].iloc[-20:].values
    recent_lows = df['low'].iloc[-20:].values

    # Higher highs + higher lows (simplified)
    mid = 10
    hh_recent = max(recent_highs[mid:])
    hh_older = max(recent_highs[:mid])
    hl_recent = min(recent_lows[mid:])
    hl_older = min(recent_lows[:mid])

    structure_bull = hh_recent >= hh_older * 0.995 and hl_recent >= hl_older * 0.995
    structure_bear = hh_recent <= hh_older * 1.005 and hl_recent <= hl_older * 1.005

    # UPTREND detection — STRICT (v3.1)
    bull_stack = ema9 > ema21 > ema50
    price_above = price > ema21
    slope_up = ema21_slope > 0.15  # STRICT: slope minimal 0.15%
    strong_adx = adx >= 25  # STRICT: ADX minimal 25 (bukan 20)
    ema_gap_ok = ema_gap_pct > 0.3  # EMA9-21 gap minimal 0.3%

    if bull_stack and price_above and strong_adx and slope_up and \
       ema_gap_ok and structure_bull:
        strength = min(100, int(adx * 2) + int(ema21_slope * 100))
        return {
            'state': 'UPTREND',
            'strength': strength,
            'reason': f'EMA bull, ADX {adx:.0f}, slope +{ema21_slope:.2f}%, gap {ema_gap_pct:.2f}%',
        }

    # DOWNTREND detection — STRICT
    bear_stack = ema9 < ema21 < ema50
    price_below = price < ema21
    slope_down = ema21_slope < -0.15

    if bear_stack and price_below and strong_adx and slope_down and \
       ema_gap_ok and structure_bear:
        strength = min(100, int(adx * 2) + int(abs(ema21_slope) * 100))
        return {
            'state': 'DOWNTREND',
            'strength': strength,
            'reason': f'EMA bear, ADX {adx:.0f}, slope {ema21_slope:.2f}%, gap {ema_gap_pct:.2f}%',
        }

    return {
        'state': 'SIDEWAYS',
        'strength': int(adx),
        'reason': f'No clear trend (ADX {adx:.0f})',
    }


def detect_pullback(df: pd.DataFrame, trend_state: str,
                    rsi_data: dict) -> dict:
    """
    Deteksi pullback dalam tren — INI KUNCI trend-following.

    UPTREND pullback:
      - Harga retrace ke EMA9/EMA21 (tidak jauh di bawah)
      - RSI turun ke zone 40-55 (bukan oversold ekstrem)
      - 2-3 candle bearish kecil/moderate (koreksi normal)

    DOWNTREND pullback:
      - Harga rally ke EMA9/EMA21 (tidak jauh di atas)
      - RSI naik ke zone 45-60
      - 2-3 candle bullish kecil

    Kenapa ini powerful:
      Pullback = diskon di tren yang sudah jelas.
      Entry saat pullback selesai = entry dengan risk kecil,
      reward besar (tren dilanjutkan).

    Returns:
        in_pullback: bool
        depth: seberapa dalam (0-1, 1 = ke EMA21)
        quality: 'SHALLOW' | 'IDEAL' | 'DEEP' | 'BROKEN'
    """
    if df is None or len(df) < 25:
        return {'in_pullback': False, 'depth': 0, 'quality': 'NONE'}

    close = df['close']
    ema9 = calc_ema(close, 9)
    ema21 = calc_ema(close, 21)
    price = close.iloc[-1]
    ema9_now = ema9.iloc[-1]
    ema21_now = ema21.iloc[-1]

    rsi_now = rsi_data.get('rsi', 50) if rsi_data else 50

    if trend_state == 'UPTREND':
        # Cek apakah harga pernah turun ke area EMA9-EMA21 dalam 5 candle
        recent_lows = df['low'].iloc[-5:].values
        min_recent = min(recent_lows)

        # Pullback valid: min pernah sentuh EMA9 area tapi tidak pecah EMA21 dalam
        touched_ema9_area = min_recent <= ema9_now * 1.001
        above_ema21 = min_recent >= ema21_now * 0.996  # v3.1: lebih strict

        # STRICT pullback zone (40-50 sweet spot)
        rsi_pullback_zone = 40 <= rsi_now <= 52

        # Syarat BARU: minimal 2 candle bearish selama pullback
        recent_opens = df['open'].iloc[-4:].values
        recent_closes = df['close'].iloc[-4:].values
        bearish_candles = sum(1 for i in range(4) if recent_closes[i] < recent_opens[i])
        has_pullback_candles = bearish_candles >= 2

        if touched_ema9_area and above_ema21 and rsi_pullback_zone and has_pullback_candles:
            # Hitung depth: seberapa dekat ke EMA21
            ema21_prev = ema21.iloc[-5]
            depth = 1.0 if min_recent <= ema21_prev else 0.5

            # Quality
            if rsi_now < 40:
                quality = 'BROKEN'  # terlalu dalam, mungkin reversal
            elif 40 <= rsi_now <= 48:
                quality = 'IDEAL'  # sweet spot
            elif 48 < rsi_now <= 55:
                quality = 'SHALLOW'  # agak shallow tapi ok
            else:
                quality = 'IDEAL'

            return {
                'in_pullback': True,
                'depth': depth,
                'quality': quality,
            }

    elif trend_state == 'DOWNTREND':
        recent_highs = df['high'].iloc[-5:].values
        max_recent = max(recent_highs)

        touched_ema9_area = max_recent >= ema9_now * 0.999
        below_ema21 = max_recent <= ema21_now * 1.004  # v3.1: strict

        # STRICT pullback zone (50-60 sweet spot)
        rsi_pullback_zone = 48 <= rsi_now <= 60

        # Syarat BARU: minimal 2 candle bullish selama rally
        recent_opens = df['open'].iloc[-4:].values
        recent_closes = df['close'].iloc[-4:].values
        bullish_candles = sum(1 for i in range(4) if recent_closes[i] > recent_opens[i])
        has_rally_candles = bullish_candles >= 2

        if touched_ema9_area and below_ema21 and rsi_pullback_zone and has_rally_candles:
            ema21_prev = ema21.iloc[-5]
            depth = 1.0 if max_recent >= ema21_prev else 0.5

            if rsi_now > 60:
                quality = 'BROKEN'
            elif 52 <= rsi_now <= 60:
                quality = 'IDEAL'
            elif 45 <= rsi_now < 52:
                quality = 'SHALLOW'
            else:
                quality = 'IDEAL'

            return {
                'in_pullback': True,
                'depth': depth,
                'quality': quality,
            }

    return {'in_pullback': False, 'depth': 0, 'quality': 'NONE'}


def detect_continuation_trigger(df: pd.DataFrame, trend_state: str,
                                rsi_data: dict, macd: dict) -> dict:
    """
    Deteksi trigger continuation setelah pullback.

    UPTREND continuation:
      - Candle close di atas EMA9 (reclaim)
      - RSI mulai rising (setelah drop di pullback)
      - MACD histogram naik (momentum kembali)

    DOWNTREND continuation:
      - Candle close di bawah EMA9 (rejection)
      - RSI mulai falling
      - MACD histogram turun

    Returns:
        triggered: bool
        strength: 1-5
        reasons: list
    """
    if df is None or len(df) < 15:
        return {'triggered': False, 'strength': 0, 'reasons': []}

    close = df['close']
    ema9 = calc_ema(close, 9)
    ema9_now = ema9.iloc[-1]
    price = close.iloc[-1]

    reasons = []
    strength = 0

    if trend_state == 'UPTREND':
        # Trigger 1: Harga close DI ATAS EMA9 (reclaim)
        if price > ema9_now and close.iloc[-2] <= ema9.iloc[-2]:
            strength += 2
            reasons.append("EMA9 reclaim (close above)")
        elif price > ema9_now:
            strength += 1
            reasons.append("Above EMA9")

        # Trigger 2: RSI turning up
        if rsi_data.get('rising') and rsi_data.get('rsi', 50) > 45:
            strength += 2
            reasons.append(f"RSI {rsi_data['rsi']:.0f} rising")

        # Trigger 3: MACD histogram positif
        if macd.get('histogram', 0) > 0:
            strength += 1
            reasons.append("MACD hist positif")

        # v4.1: 2 CONSECUTIVE BULLISH CANDLES (wajib)
        # Candle terakhir DAN candle sebelumnya harus bullish (close > open)
        last_bull = close.iloc[-1] > df['open'].iloc[-1]
        prev_bull = close.iloc[-2] > df['open'].iloc[-2]
        if last_bull and prev_bull:
            strength += 2
            reasons.append("2 bullish candles (confirmed)")
        elif last_bull and close.iloc[-1] > close.iloc[-2] * 1.002:
            # Atau 1 candle bullish yang kuat (>0.2% move)
            strength += 1
            reasons.append("Strong bullish close")

    elif trend_state == 'DOWNTREND':
        # Trigger 1: Harga close DI BAWAH EMA9 (rejection)
        if price < ema9_now and close.iloc[-2] >= ema9.iloc[-2]:
            strength += 2
            reasons.append("EMA9 rejection (close below)")
        elif price < ema9_now:
            strength += 1
            reasons.append("Below EMA9")

        # Trigger 2: RSI turning down
        if rsi_data.get('falling') and rsi_data.get('rsi', 50) < 55:
            strength += 2
            reasons.append(f"RSI {rsi_data['rsi']:.0f} falling")

        # Trigger 3: MACD histogram negatif
        if macd.get('histogram', 0) < 0:
            strength += 1
            reasons.append("MACD hist negatif")

        # v4.1: 2 CONSECUTIVE BEARISH CANDLES (wajib)
        last_bear = close.iloc[-1] < df['open'].iloc[-1]
        prev_bear = close.iloc[-2] < df['open'].iloc[-2]
        if last_bear and prev_bear:
            strength += 2
            reasons.append("2 bearish candles (confirmed)")
        elif last_bear and close.iloc[-1] < close.iloc[-2] * 0.998:
            strength += 1
            reasons.append("Strong bearish close")

    # v3.1: STRICT — strength >= 4 dan harus ada EMA reclaim
    has_ema_reclaim = any('reclaim' in r or 'rejection' in r for r in reasons)
    triggered = strength >= 4 and has_ema_reclaim

    return {
        'triggered': triggered,
        'strength': strength,
        'reasons': reasons,
    }


# =========================================================
#  8. HTF BIAS CHECK (1H untuk konfirmasi arah)
# =========================================================
def get_htf_bias(df_1h: pd.DataFrame) -> str:
    """
    Tentukan bias arah dari timeframe 1H.
    Digunakan sebagai filter, bukan entry trigger.

    Returns: 'BULLISH' | 'BEARISH' | 'SIDEWAYS'
    """
    if df_1h is None or len(df_1h) < 55:
        return 'SIDEWAYS'

    close = df_1h['close']
    ema_9 = calc_ema(close, 9).iloc[-1]
    ema_21 = calc_ema(close, 21).iloc[-1]
    ema_50 = calc_ema(close, 50).iloc[-1]
    price = close.iloc[-1]

    if price > ema_9 > ema_21 > ema_50:
        return 'BULLISH'
    elif price < ema_9 < ema_21 < ema_50:
        return 'BEARISH'
    elif price > ema_21:
        return 'BULLISH'
    elif price < ema_21:
        return 'BEARISH'
    return 'SIDEWAYS'


# =========================================================
#  9. MAIN ENTRY — generate_scalping_signal() v3
#     PARADIGM SHIFT: Trend-following dengan pullback
#     Follow the trend, jangan lawan arus.
# =========================================================
def generate_scalping_signal_v2(
    price: float,
    atr: float,
    ema_trend: str,
    structure: str,
    ks: dict,
    kr: dict,
    res_mtf: list,
    sup_mtf: list,
    smc: dict,
    rsi: float = 50.0,
    htf_ema: str = 'SIDEWAYS',
    df_main: pd.DataFrame = None,
    symbol: str = '',
    adx: float = 20.0,
    signal_cache: dict = None,
) -> dict:
    """
    Signal engine scalping 15m v2 — Scoring System.

    Perubahan dari v1: tidak lagi butuh 4 kondisi bersamaan.
    Sekarang pakai Setup + Momentum + Bonus scoring.

    Butuh minimal:
      1 Setup  (BB/Wedge/S&R/EMA bounce)
      1 Momentum (RSI/MACD crossover/MACD turning)
      Score >= threshold

    Returns:
        Signal dict kompatibel dengan format existing, atau None.
    """
    if df_main is None or len(df_main) < 60:
        return None

    # --- Hitung semua indikator ---
    bb = calc_bollinger_bands(df_main, period=20, std_mult=2.0)
    rsi_data = calc_rsi_with_sma(df_main, rsi_period=14, sma_period=14)
    macd = calc_macd(df_main, fast=12, slow=26, signal_period=9)
    wedge = detect_wedge_channel(df_main, lookback=50, swing_window=3)

    if bb is None or rsi_data is None or macd is None:
        return None

    # --- Pre-filters (hard block) ---
    if adx < 15:
        return None

    # BB width minimum
    if bb['bb_width_pct'] < 1.5:
        return None

    # --- Detect S&R levels ---
    sr_levels = detect_sr_levels(df_main, window=5, max_levels=5)

    # --- EMA21 untuk bounce detection ---
    ema21 = df_main['close'].ewm(span=21, adjust=False).mean()
    ema21_now = ema21.iloc[-1]

    # ============================================
    #  SCORE KEDUA ARAH SECARA PARALEL
    # ============================================
    results = {}

    for direction in ('LONG', 'SHORT'):
        score = 0
        kills = 0
        reasons = []
        has_setup = False
        has_momentum = False
        has_wedge_setup = False

        # ── SETUP LAYER (butuh minimal 1) ──

        # Setup 1: BB touch + re-entry
        if direction == 'LONG' and bb['touch_lower'] and bb['inside_now']:
            score += 3
            has_setup = True
            reasons.append("BB lower touch + re-entry")
        elif direction == 'SHORT' and bb['touch_upper'] and bb['inside_now']:
            score += 3
            has_setup = True
            reasons.append("BB upper touch + re-entry")

        # Setup 2: Wedge/Channel breakout
        if direction == 'LONG' and \
           wedge['pattern'] in ('falling_wedge', 'desc_channel') and \
           wedge['breakout']:
            pts = 4 if wedge['pattern'] == 'falling_wedge' else 3
            score += pts
            has_setup = True
            has_wedge_setup = True
            reasons.append(f"{wedge['pattern']} breakout "
                           f"(conf {wedge['confidence']}%)")
        elif direction == 'SHORT' and \
             wedge['pattern'] in ('rising_wedge', 'asc_channel') and \
             wedge['breakout']:
            pts = 4 if wedge['pattern'] == 'rising_wedge' else 3
            score += pts
            has_setup = True
            has_wedge_setup = True
            reasons.append(f"{wedge['pattern']} breakdown "
                           f"(conf {wedge['confidence']}%)")

        # Setup 3: S&R proximity
        sr_prox = check_sr_proximity(price, atr, direction, sr_levels, 1.5)
        if sr_prox['near_level']:
            score += 3
            has_setup = True
            reasons.append(f"Near S&R {sr_prox['level_price']:.6g} "
                           f"({sr_prox['distance']:.1f} ATR)")

        # EMA21 bounce DIHAPUS — tidak terbukti meningkatkan WR di Run 9-12

        # ── MOMENTUM LAYER (butuh minimal 1) ──

        # Momentum 1: RSI extreme + direction
        if direction == 'LONG' and rsi_data['was_oversold'] and \
           rsi_data['rising'] and rsi_data['rsi'] < 50:
            score += 3
            has_momentum = True
            reasons.append(f"RSI {rsi_data['rsi']:.1f} oversold recovery")
            if rsi_data['sma_cross_up']:
                score += 1
                reasons.append("RSI x SMA cross UP")
        elif direction == 'SHORT' and rsi_data['was_overbought'] and \
             rsi_data['falling'] and rsi_data['rsi'] > 50:
            score += 3
            has_momentum = True
            reasons.append(f"RSI {rsi_data['rsi']:.1f} overbought reversal")
            if rsi_data['sma_cross_down']:
                score += 1
                reasons.append("RSI x SMA cross DOWN")

        # Momentum 2: MACD crossover + histogram
        if direction == 'LONG' and macd['cross_up'] and \
           macd['hist_positive']:
            score += 3
            has_momentum = True
            reasons.append("MACD cross UP + histogram positif")
        elif direction == 'LONG' and macd['cross_up'] and \
             macd['macd_above']:
            score += 2
            has_momentum = True
            reasons.append("MACD cross UP (above signal)")
        elif direction == 'SHORT' and macd['cross_down'] and \
             macd['hist_negative']:
            score += 3
            has_momentum = True
            reasons.append("MACD cross DOWN + histogram negatif")
        elif direction == 'SHORT' and macd['cross_down'] and \
             macd['macd_below']:
            score += 2
            has_momentum = True
            reasons.append("MACD cross DOWN (below signal)")

        # MACD histogram turning DIHAPUS — terlalu dini, menambah noise

        # ── GATE: harus punya Setup + Momentum ──
        if not has_setup or not has_momentum:
            results[direction] = None
            continue

        # ── CONFIRMATION LAYER (bonus score) ──

        # Candle pattern
        candle = check_candle_confirmation(df_main, atr, direction)
        if candle['confirmed']:
            score += candle['strength']
            reasons.append(f"Candle: {candle['pattern']} "
                           f"(str {candle['strength']})")

        # Strong candle close
        if check_strong_candle_close(df_main, direction):
            score += 2
            reasons.append("Strong candle close")

        # Rejection wick — MANDATORY untuk signal berkualitas tinggi
        # Filter KUNCI: smart money reject the level
        wick_check = check_rejection_wick(df_main, direction, wick_ratio=1.5)
        if wick_check['has_rejection']:
            score += 3
            reasons.append(f"Rejection wick {wick_check['wick_ratio']:.1f}x body")

        # Volume spike
        vol = check_volume_spike(df_main, 20, 1.5)
        if vol['spike']:
            score += 2
            reasons.append(f"Volume spike {vol['ratio']:.1f}x")
        elif vol['dead']:
            kills += 1
            reasons.append("KILL: Volume mati")

        # Momentum exhaustion
        exh = check_momentum_exhaustion(df_main, direction)
        if exh['exhausted']:
            score += 1
            reasons.append(f"Exhaustion: {exh['desc']}")

        # Price overextension
        oe = check_price_overextension(df_main, atr, direction, 1.0)
        if oe['overextended']:
            score += 2
            reasons.append(f"Overextended {oe['distance_atr']:.1f} ATR")

        # ── TREND ALIGNMENT ──

        # HTF
        htf_bull = htf_ema in ('STRONG_UP', 'UP')
        htf_bear = htf_ema in ('STRONG_DOWN', 'DOWN')
        if direction == 'LONG' and htf_bear:
            kills += 1
            reasons.append("KILL: HTF bearish")
        elif direction == 'SHORT' and htf_bull:
            kills += 1
            reasons.append("KILL: HTF bullish")
        elif direction == 'LONG' and htf_bull:
            score += 2
            reasons.append("HTF aligned bullish")
        elif direction == 'SHORT' and htf_bear:
            score += 2
            reasons.append("HTF aligned bearish")

        # EMA 15m
        if direction == 'LONG' and ema_trend == 'STRONG_DOWN':
            kills += 1
            reasons.append("KILL: EMA STRONG_DOWN")
        elif direction == 'SHORT' and ema_trend == 'STRONG_UP':
            kills += 1
            reasons.append("KILL: EMA STRONG_UP")
        elif direction == 'LONG' and ema_trend in ('STRONG_UP', 'UP'):
            score += 1
            reasons.append("EMA 15m aligned")
        elif direction == 'SHORT' and ema_trend in ('STRONG_DOWN', 'DOWN'):
            score += 1
            reasons.append("EMA 15m aligned")

        # Structure
        if direction == 'LONG' and structure == 'UPTREND':
            score += 1
            reasons.append("Structure aligned")
        elif direction == 'SHORT' and structure == 'DOWNTREND':
            score += 1
            reasons.append("Structure aligned")

        # ── BB-only tanpa S&R gate ──
        if not has_wedge_setup and not sr_prox['near_level']:
            # BB-only tanpa wedge dan tanpa S&R → reject
            results[direction] = None
            continue

        results[direction] = {
            'score': score, 'kills': kills, 'reasons': reasons,
            'candle': candle,
        }

    # ── Pilih arah terbaik ──
    long_r = results.get('LONG')
    short_r = results.get('SHORT')

    direction = None
    if long_r and short_r:
        if long_r['score'] >= short_r['score']:
            direction = 'LONG'
        else:
            direction = 'SHORT'
    elif long_r:
        direction = 'LONG'
    elif short_r:
        direction = 'SHORT'
    else:
        return None

    chosen = results[direction]
    score = chosen['score']
    kills = chosen['kills']
    reasons = chosen['reasons']
    candle_info = chosen['candle']

    # --- Anti-flip gate ---
    if signal_cache and symbol in signal_cache:
        cached = signal_cache[symbol]
        if cached.get('dir') and cached['dir'] != direction:
            import time
            age_hours = (time.time() - cached.get('ts', 0)) / 3600
            if age_hours < 2:
                return None

    # --- Quality determination ---
    quality = _determine_scalp_quality(score, kills, direction)
    if quality is None:
        return None

    # --- Hitung SL/TP ---
    levels = calc_sl_tp(price, atr, direction, wedge, bb)
    sl, tp1, tp2, tp3, risk = (levels['sl'], levels['tp1'],
                                levels['tp2'], levels['tp3'],
                                levels['risk'])

    sl_pct = risk / price * 100
    if sl_pct > 3.0:
        return None

    rr1 = abs(tp1 - price) / risk if risk > 0 else 0
    rr2 = abs(tp2 - price) / risk if risk > 0 else 0
    if rr1 < 0.8:
        return None

    reasons.append(f"TP1={tp1:.2f} | TP2={tp2:.2f} | TP3={tp3:.2f}")

    level_price = sr_levels.get('supports', [price])[-1] \
        if direction == 'LONG' \
        else sr_levels.get('resistances', [price])[-1] \
        if sr_levels.get('resistances') else price

    signal = {
        'direction': direction,
        'quality': quality,
        'entry': price,
        'sl': sl,
        'tp1': tp1,
        'tp2': tp2,
        'rr1': round(rr1, 2),
        'rr2': round(rr2, 2),
        'rr': round(rr2, 2),
        'sl_pct': round(sl_pct, 2),
        'reasons': reasons,
        'level_used': 'SUPPORT' if direction == 'LONG' else 'RESISTANCE',
        'confluence_score': score,
        'kill_count': kills,
        'entry_low': price,
        'entry_high': price,
        'tp': tp2,
        'tp_max': tp3,
        'rr_max': round(abs(tp3 - price) / risk if risk > 0 else 0, 2),
        'level_price': level_price,
        'tp3': tp3,
        'engine': 'scalping',
        'bb': {
            'upper': round(bb['upper'], 8),
            'middle': round(bb['middle'], 8),
            'lower': round(bb['lower'], 8),
        },
        'macd_state': {
            'cross_up': macd['cross_up'],
            'cross_down': macd['cross_down'],
            'histogram': round(macd['histogram'], 8),
        },
        'rsi_state': {
            'value': round(rsi_data['rsi'], 1),
            'rising': rsi_data['rising'],
            'falling': rsi_data['falling'],
        },
        'wedge': {
            'pattern': wedge['pattern'],
            'breakout': wedge['breakout'],
            'confidence': wedge['confidence'],
        },
        'candle_confirm': candle_info.get('pattern'),
    }

    logger.info(
        f"[{symbol}] SCALP SIGNAL: {direction} {quality} | "
        f"score={score} kills={kills} | entry={price} "
        f"SL={sl:.2f} TP1={tp1:.2f} TP2={tp2:.2f} TP3={tp3:.2f}")

    return signal


# =========================================================
#  QUALITY DETERMINATION — v2
# =========================================================
def _determine_scalp_quality(score: int, kills: int,
                             direction: str = '') -> str:
    """
    Quality tier — v3 trend-following (legacy).

    Engine v3 sudah punya trend gate + pullback gate + trigger gate.
    Yang lolos ke sini sudah berkualitas tinggi.

    GOOD: score >= 10, kills == 0
    WAIT: score >= 7, kills == 0
    """
    if kills >= 1:
        return None

    if score >= 10:
        return 'GOOD'
    if score >= 7:
        return 'WAIT'
    return None


def _determine_scalp_quality_v43(score: int, kills: int,
                                 direction: str = '',
                                 good_threshold: int = 10,
                                 wait_threshold: int = 7) -> str:
    """
    Quality tier v5.9.1 — strict kill gate (reverted from v5.9).

    v5.9 tried kills=1 as WAIT → data: kills=1 PnL -1.98R dari 25 trades.
    Scalping 15m terlalu sensitif — 1 kill sudah fatal.
    Reverted: kills>=1 = reject (original behavior).
    """
    if kills >= 1:
        return None

    if score >= good_threshold:
        return 'GOOD'
    if score >= wait_threshold:
        return 'WAIT'
    return None


# =========================================================
#  10. V3 ENTRY POINT — Trend-Following Engine
# =========================================================
def generate_scalping_signal(
    price: float,
    atr: float,
    ema_trend: str,
    structure: str,
    ks: dict,
    kr: dict,
    res_mtf: list,
    sup_mtf: list,
    smc: dict,
    rsi: float = 50.0,
    htf_ema: str = 'SIDEWAYS',
    df_main: pd.DataFrame = None,
    symbol: str = '',
    adx: float = 20.0,
    signal_cache: dict = None,
) -> dict:
    """
    V3 — Trend-following dengan pullback entry.

    Filosofi:
      1. Deteksi tren utama dulu (EMA alignment + ADX + structure)
      2. SKIP kalau sideways — tidak ada tren = tidak trade
      3. Tunggu pullback (harga retrace ke EMA9/EMA21)
      4. Tunggu continuation trigger (harga reclaim level + momentum kembali)
      5. Entry searah tren

    Ini BUKAN mean-reversion (tidak catch falling knives).
    Ini trend-following (surf the wave).
    """
    if df_main is None or len(df_main) < 60:
        return None

    # --- v5.9.2: COIN QUALITY FILTER ---
    # Data 142 trades dari 100 coin backtest:
    # GOOD coins (22): WR 82.9%, EV +0.514R, PnL +31.4R
    # BAD coins (14): WR ~10%, 56% SL rate, 50% SL hit dalam ≤2 bars
    # Score BUKAN pembeda (avg 14.2 vs 14.1) — coin quality yang menentukan.
    # Coin kecil/illiquid terlalu noisy di 15m → instant reversal.
    #
    # Strategi: WHITELIST approach — hanya trade coin yang terbukti profitable.
    # List ini bisa di-expand setelah coin baru lolos backtest.
    SCALP_COIN_WHITELIST = {
        # Tier 1: Top scalp performers (WR 75-100%, PnL > +1.5R)
        'BTC', 'DOGE', 'APT', 'AVAX', 'DOT', 'XRP', 'WLD', 'ZEC',
        'LINA', 'WAVES',
        # Tier 2: Solid scalp performers (WR 50-100%, PnL > +0.5R)
        'BNB', 'ARB', 'CRV', 'ENJ', 'BIO', 'TRUMP', 'BLUR', 'OCEAN',
        'DGB', 'STRAX', 'LDO', 'WLFI',
        # Tier 3: Neutral scalp (WR >= 50%)
        'LTC', 'SOL', 'SUI', 'SEI', 'ORDI', 'LINK',
        # v5.9.4: Expanded — validated in SCALP backtest (PnL > 0)
        # Dropped: AAVE(-3.2R), INJ(-2R), ARKM, ALPHA, MBOX, NEAR, WIF
        'ADA', 'ETH', 'ENA', 'TAO', 'BCH', 'BNX', 'PAXG', 'TRX',
    }
    if symbol and symbol.upper().replace('USDT', '') not in SCALP_COIN_WHITELIST:
        # Strip USDT suffix jika ada
        clean = symbol.upper().replace('USDT', '').replace('_', '')
        if clean not in SCALP_COIN_WHITELIST:
            logger.debug(f"[{symbol}] v5.9.2 SKIP: not in scalp whitelist")
            return None

    # --- Hitung indikator dasar ---
    rsi_data = calc_rsi_with_sma(df_main, rsi_period=14, sma_period=14)
    macd = calc_macd(df_main, fast=12, slow=26, signal_period=9)
    bb = calc_bollinger_bands(df_main, period=20, std_mult=2.0)

    if rsi_data is None or macd is None or bb is None:
        return None

    # --- STEP 0: SESSION FILTER (v5.9.3: soft penalty, bukan hard block) ---
    # v5.9.3: session DEAD dulu hard block → sekarang soft penalty (score -3).
    # Data: session filter block 5952 signals (13%). Terlalu banyak — banyak
    # signal valid di session "rendah" yang terbuang. Penalty lebih fair.
    session_name = 'UNKNOWN'
    session_mod = 0
    if _LEARNING_AVAILABLE:
        try:
            ts_latest = df_main['timestamp'].iloc[-1] if 'timestamp' in df_main.columns else None
            if ts_latest is not None:
                session_name = session_filter.get_session_from_timestamp(ts_latest)
                gate = session_filter.get_session_gate()
                allow, reason, session_mod = gate.should_trade(session_name)
                if not allow:
                    # v5.9.3: soft penalty instead of hard block
                    session_mod = -3  # will reduce score later
                    logger.debug(f"[{symbol}] v5.9.3 SESSION PENALTY: {reason}")
        except Exception:
            pass

    # --- STEP 0.5: COIN LEARNING — per-coin adaptive params ---
    # v5: Higher thresholds karena bonus dari divergence/OB/Fib/climax
    # Score range baru bisa 0-30+ (dari 0-20 di v4.3)
    adapt_params = {
        'score_good_threshold': 16,  # F6: naik dari 14. Score 10-14 WR 43%, 15+ selektif
        'score_wait_threshold': 12,  # F6: naik dari 10
        'min_trend_strength': 50,
    }
    if _LEARNING_AVAILABLE:
        try:
            learning = coin_learn.get_learning()
            if not learning.should_trade(symbol):
                logger.debug(f"[{symbol}] v4.3 COIN BLOCKED by learning")
                return None
            coin_params = learning.get_params(symbol)
            adapt_params.update({
                'score_good_threshold': coin_params.get('score_good_threshold', 10),
                'score_wait_threshold': coin_params.get('score_wait_threshold', 7),
                'min_trend_strength': coin_params.get('min_trend_strength', 50),
            })
        except Exception:
            pass

    # --- v5.9.2: WHIPSAW DETECTION ---
    # Data: 50% BAD coin SL hit dalam ≤2 bars = instant reversal.
    # Root cause: 15m candles punya wick besar dua arah (whipsaw).
    # Filter: cek 5 candle terakhir. Jika >60% punya wick > body = choppy market.
    if len(df_main) >= 7:
        whipsaw_count = 0
        for idx in range(-5, 0):
            c_open = float(df_main['open'].iloc[idx])
            c_high = float(df_main['high'].iloc[idx])
            c_low = float(df_main['low'].iloc[idx])
            c_close = float(df_main['close'].iloc[idx])
            body = abs(c_close - c_open)
            total_range = c_high - c_low
            if total_range > 0 and body / total_range < 0.3:
                whipsaw_count += 1
        # v5.9.3: 3/5 → 4/5 (longgarkan). Data: whipsaw block 5312 signals (12%).
        # 3/5 terlalu agresif — banyak momen valid terbuang.
        # 4/5 = hanya block saat BENAR-BENAR choppy.
        if whipsaw_count >= 4:  # 4+ dari 5 candle = very choppy
            logger.debug(f"[{symbol}] v5.9.3 SKIP: whipsaw detected "
                         f"({whipsaw_count}/5 candles have body < 30% range)")
            return None

    # --- STEP 1: DETECT TREND (v4: pakai 1H HTF!) ---
    df_1h = smc.get('df_1h') if smc else None

    if df_1h is None or len(df_1h) < 55:
        return None

    try:
        adx_1h = calc_adx(df_1h, 14)
    except Exception:
        adx_1h = 0

    # v5.9.3: ADX min 22→18. Data: ADX 18-21 = 20% sinyal diblock,
    # trend masih valid di 15m scalping (beda dari swing yang butuh ADX kuat).
    if adx_1h < 18:
        logger.debug(f"[{symbol}] v4.3 SKIP: 1H ADX {adx_1h:.0f} < 18")
        return None

    # v5.9.3: ADX death zone 35-44 → 40-44 (narrowed).
    # Data scalp: ADX 35-39 block 1800+ signals. Di 15m trend-following,
    # ADX 35-39 masih tradeable (beda dari swing 1H yang butuh range ADX ketat).
    # ADX 40-44 tetap block (terlalu volatile). ADX >= 55 block (extreme).
    if 40 <= adx_1h < 45:
        logger.debug(f"[{symbol}] F7 SKIP: ADX {adx_1h:.0f} in death zone 40-44")
        return None
    if adx_1h >= 55:
        logger.debug(f"[{symbol}] F7 SKIP: ADX {adx_1h:.0f} too extreme (>=55)")
        return None

    trend = detect_trend_state(df_1h, adx_1h)

    if trend['state'] == 'SIDEWAYS':
        logger.debug(f"[{symbol}] v4.3 SKIP: 1H {trend['reason']}")
        return None

    # Adaptive trend strength threshold (per-coin)
    if trend['strength'] < adapt_params['min_trend_strength']:
        logger.debug(f"[{symbol}] v4.3 SKIP: trend strength "
                     f"{trend['strength']} < {adapt_params['min_trend_strength']}")
        return None

    # --- STEP 1.5: 4H MACRO TREND (v4.3) ---
    # Trend 1H harus agree dengan macro 4H bias
    df_4h = aggregate_1h_to_4h(df_1h)
    macro_bias = get_4h_trend_bias(df_4h) if df_4h is not None else 'NEUTRAL'

    # Hard gate: jangan LONG saat macro BEAR, jangan SHORT saat macro BULL
    if trend['state'] == 'UPTREND' and macro_bias == 'BEAR':
        logger.debug(f"[{symbol}] v4.3 SKIP: 1H UP vs 4H BEAR conflict")
        return None
    if trend['state'] == 'DOWNTREND' and macro_bias == 'BULL':
        logger.debug(f"[{symbol}] v4.3 SKIP: 1H DOWN vs 4H BULL conflict")
        return None

    direction = 'LONG' if trend['state'] == 'UPTREND' else 'SHORT'

    # --- STEP 2: DETECT PULLBACK ---
    pullback = detect_pullback(df_main, trend['state'], rsi_data)

    if not pullback['in_pullback']:
        logger.debug(f"[{symbol}] v3 SKIP: not in pullback "
                     f"(trend={trend['state']}, RSI={rsi_data['rsi']:.0f})")
        return None

    if pullback['quality'] == 'BROKEN':
        logger.debug(f"[{symbol}] v3 SKIP: pullback too deep (broken)")
        return None

    # --- STEP 3: DETECT CONTINUATION TRIGGER ---
    trigger = detect_continuation_trigger(df_main, trend['state'],
                                          rsi_data, macd)

    if not trigger['triggered']:
        logger.debug(f"[{symbol}] v3 SKIP: no continuation trigger")
        return None

    # --- STEP 4: BUILD SCORE & REASONS ---
    score = 0
    kills = 0
    reasons = []

    # Trend strength
    score += 3
    reasons.append(f"Trend {trend['state']}: {trend['reason']}")

    # Pullback quality
    if pullback['quality'] == 'IDEAL':
        score += 3
        reasons.append("Pullback IDEAL (RSI di sweet spot)")
    elif pullback['quality'] == 'SHALLOW':
        score += 2
        reasons.append("Pullback shallow")
    else:
        score += 1
        reasons.append(f"Pullback {pullback['quality']}")

    # Continuation trigger strength
    score += trigger['strength']
    reasons.extend(trigger['reasons'])

    # --- BONUS: Volume confirmation ---
    # v5.9: threshold 1.8→2.0. Data 43 trades: spike 1.8x masih di BEP trades.
    # Spike ≥2.5x HANYA ada di TP2/TP3 (100% win). Extra bonus untuk ≥2.5x.
    vol = check_volume_spike(df_main, 20, 2.0)
    if vol['spike']:
        if vol['ratio'] >= 2.5:
            score += 3
            reasons.append(f"Volume spike STRONG {vol['ratio']:.1f}x")
        else:
            score += 2
            reasons.append(f"Volume spike {vol['ratio']:.1f}x")
    elif vol['dead']:
        kills += 1
        reasons.append("KILL: Volume mati")

    # --- BONUS: Rejection wick (di direction of trend continuation) ---
    # F5: Extreme wick = TRAP, bukan confirmation.
    # v5.9: Data 43 trades: rejection wick di TP trades 27% vs losing 14%.
    # Wick 1.5-1.9x = sweet spot (DOGE TP2 punya wick 1.8x).
    # Wick ≥2.0x tetap KILL (trap).
    wick_check = check_rejection_wick(df_main, direction, wick_ratio=1.0)
    if wick_check['has_rejection']:
        if wick_check['wick_ratio'] >= 2.0:
            kills += 1
            reasons.append(f"KILL: Extreme wick {wick_check['wick_ratio']:.1f}x "
                           f"(trap signal)")
        elif wick_check['wick_ratio'] >= 1.5:
            score += 3  # v5.9: sweet spot wick, strong confirmation
            reasons.append(f"Rejection wick STRONG {wick_check['wick_ratio']:.1f}x")
        else:
            score += 2
            reasons.append(f"Rejection wick {wick_check['wick_ratio']:.1f}x")

    # --- BONUS: HTF alignment ---
    htf_bull = htf_ema in ('STRONG_UP', 'UP')
    htf_bear = htf_ema in ('STRONG_DOWN', 'DOWN')
    if direction == 'LONG' and htf_bull:
        score += 2
        reasons.append("HTF 1H bullish aligned")
    elif direction == 'SHORT' and htf_bear:
        score += 2
        reasons.append("HTF 1H bearish aligned")
    elif direction == 'LONG' and htf_bear:
        kills += 1
        reasons.append("KILL: HTF 1H bearish (against trend)")
    elif direction == 'SHORT' and htf_bull:
        kills += 1
        reasons.append("KILL: HTF 1H bullish (against trend)")

    # --- BONUS: Candle pattern --- DISABLED v5.8
    # Data 413 trades: Morning Star(-48%), Bullish Engulfing(-21.7%),
    # Dark Cloud Cover(-75%), Piercing Line(-60%) semua SL-biased di 15m.
    # v5.7 confirmation gate sudah handle candle direction check.
    candle = check_candle_confirmation(df_main, atr, direction)
    # score += candle['strength']  # DISABLED: candle patterns = noise di 15m

    # --- BONUS v4.3: Volume pressure analysis ---
    vp = check_volume_pressure(df_main, lookback=10)
    if direction == 'LONG' and vp['direction'] == 'BUYING':
        score += 2 if vp['strength'] == 'STRONG' else 1
        reasons.append(f"Volume pressure BUYING {vp['buy_pressure_pct']}%")
    elif direction == 'SHORT' and vp['direction'] == 'SELLING':
        score += 2 if vp['strength'] == 'STRONG' else 1
        reasons.append(f"Volume pressure SELLING {vp['buy_pressure_pct']}%")
    elif direction == 'LONG' and vp['direction'] == 'SELLING' and \
         vp['strength'] == 'STRONG':
        kills += 1
        reasons.append(f"KILL: Strong selling pressure "
                       f"({vp['buy_pressure_pct']}%)")
    elif direction == 'SHORT' and vp['direction'] == 'BUYING' and \
         vp['strength'] == 'STRONG':
        kills += 1
        reasons.append(f"KILL: Strong buying pressure "
                       f"({vp['buy_pressure_pct']}%)")

    # --- BONUS v5.4: VOLUME CLIMAX (smart money signal) ---
    # F6: Score deflation. Data 413 trades: score 20-24 WR 33% (worse!).
    # Bonus +4/+5 per factor inflate score tanpa improve WR di 15m.
    # Cap semua advanced bonus ke +2 max.
    climax = check_volume_climax(df_main, direction,
                                 avg_period=20, climax_mult=2.0)
    has_climax = climax['has_climax']
    if has_climax:
        score += 2  # F6: was +4, capped to +2
        reasons.append(
            f"Volume CLIMAX {climax['ratio']:.1f}x "
            f"({climax['candle_type']})")

    # --- BONUS v5.3: FIBONACCI RETRACEMENT (real pullback) ---
    # Golden pocket (0.5-0.618) = historical WR 58-63%
    fib_check = check_fib_pullback(df_main, direction, min_swing_pct=1.0)
    has_fib = fib_check['valid']
    if has_fib:
        fib_q = fib_check['quality']
        fib_level = fib_check['level']
        if fib_q == 'GOLDEN':
            score += 2  # F6: was +4, capped to +2
            reasons.append(f"Fib {fib_level} GOLDEN pocket")
        elif fib_q == 'OK':
            score += 1  # F6: was +2
            reasons.append(f"Fib {fib_level} level")
        else:
            score += 1
            reasons.append(f"Fib {fib_level} weak")

    # --- BONUS v5.2: ORDER BLOCK TEST (SMC signal) ---
    # Historical WR di OB retest: 58-65% (from Bot 1 data)
    ob_check = check_order_block_test(df_main, direction, atr)
    has_ob = ob_check['at_ob']
    if has_ob:
        ob = ob_check['ob']
        vol_ratio = ob['vol_ratio']
        if ob_check['fresh'] and vol_ratio >= 1.5:
            score += 2  # F6: was +5, capped to +2
            reasons.append(f"At FRESH {direction} OB (vol {vol_ratio:.1f}x)")
        elif ob_check['fresh']:
            score += 1  # F6: was +3
            reasons.append(f"At fresh {direction} OB")
        else:
            score += 1  # mitigated OB = weaker
            reasons.append(f"At mitigated {direction} OB")

    # --- BONUS v5.1: RSI DIVERGENCE (powerful signal) ---
    # Historical WR: STRONG divergence 65-70%, MODERATE 55-60%
    divergence = detect_rsi_divergence(df_main, direction,
                                       lookback=30, swing_window=3)
    has_divergence = divergence['has_divergence']
    if has_divergence:
        div_strength = divergence['strength']
        if div_strength == 'STRONG':
            score += 2  # F6: was +5, capped to +2
            reasons.append(f"RSI {direction} divergence STRONG")
        elif div_strength == 'MODERATE':
            score += 1  # F6: was +3
            reasons.append(f"RSI {direction} divergence MODERATE")
        elif div_strength == 'WEAK':
            score += 1  # F6: was +2
            reasons.append(f"RSI {direction} divergence WEAK")

    # v5.6 HARD GATE REMOVED in v5.7
    # Reason: filter for kebetulan-divergence/OB/Fib/climax, bukan quality.
    # EXPIRED naik dari 43% ke 48% karena filter pick early/predictive signals.
    # v5.7 approach: confirmation candle requirement instead.

    # --- BONUS v4.3: SMC-lite BOS confirmation ---
    # v5.9: +3→+1. Data 43 trades: BOS di losing trades 18% vs winning 7%.
    # BOS di 15m = entry terlambat setelah breakout, sering sudah extended.
    # Tetap tracked tapi score dikurangi drastis.
    bos = detect_smc_bos(df_main, swing_window=5)
    if direction == 'LONG' and bos['bos'] == 'BULLISH_BOS':
        score += 1
        reasons.append(f"BULLISH BOS broke {bos['level_broken']:.4g}")
    elif direction == 'SHORT' and bos['bos'] == 'BEARISH_BOS':
        score += 1
        reasons.append(f"BEARISH BOS broke {bos['level_broken']:.4g}")

    # --- BONUS v4.3: 4H macro alignment ---
    if direction == 'LONG' and macro_bias == 'BULL':
        score += 2
        reasons.append("4H macro BULL aligned")
    elif direction == 'SHORT' and macro_bias == 'BEAR':
        score += 2
        reasons.append("4H macro BEAR aligned")

    # --- v5.8: LONG PENALTY ---
    # Data 413 trades: LONG WR 39.6% vs SHORT 41.7%
    # Semua LONG-specific factors SL-biased (EMA reclaim -17.6%,
    # HTF bullish -17.6%, bullish candles -20 to -23%)
    # Crypto: "naik tangga, turun lift" — pullback di uptrend lebih tajam
    # LONG butuh extra confluence untuk justify entry
    if direction == 'LONG':
        score -= 2
        reasons.append("LONG penalty -2 (data: LONG SL bias)")

    # --- F8: RSI DIRECTIONAL PRECISION ---
    # Data 413 trades forensic:
    #   RSI 47 rising + LONG = WR 36% (SL trap, pullback belum selesai)
    #   RSI 50 rising + LONG = WR 42% (mediocre)
    #   RSI 48 falling + SHORT = WR 82% (excellent)
    #   RSI 55 falling + SHORT = WR 82% (excellent)
    # Logic: LONG di RSI declining near 50 = pullback masih aktif.
    #        SHORT di RSI declining 48-55 = momentum drop = ideal short.
    rsi_val = rsi_data['rsi']
    rsi_falling = rsi_data.get('falling', False)
    rsi_rising = rsi_data.get('rising', False)

    if direction == 'LONG' and rsi_falling and rsi_val < 50:
        kills += 1
        reasons.append(f"KILL: RSI {rsi_val:.0f} falling < 50 "
                       f"(LONG trap, pullback aktif)")
    elif direction == 'SHORT' and rsi_falling and 45 <= rsi_val <= 55:
        score += 2
        reasons.append(f"RSI {rsi_val:.0f} falling (ideal SHORT zone)")

    # --- Anti-flip ---
    if signal_cache and symbol in signal_cache:
        cached = signal_cache[symbol]
        if cached.get('dir') and cached['dir'] != direction:
            import time
            age_hours = (time.time() - cached.get('ts', 0)) / 3600
            if age_hours < 2:
                return None

    # ═══════════════════════════════════════════════════════
    # v5.7 CONFIRMATION GATE — Last candle harus confirm direction
    # ═══════════════════════════════════════════════════════
    # Filter v5.6 (divergence/OB/Fib/climax) bersifat PREDIKTIF.
    # Tanpa konfirmasi candle, signal sering EXPIRED (48% di v5.6).
    # v5.7 wajib: candle terakhir close di arah signal dengan body kuat.
    if len(df_main) >= 2:
        last_o = float(df_main['open'].iloc[-1])
        last_h = float(df_main['high'].iloc[-1])
        last_l = float(df_main['low'].iloc[-1])
        last_c = float(df_main['close'].iloc[-1])
        last_body = abs(last_c - last_o)
        last_total = last_h - last_l

        if last_total <= 0:
            logger.debug(f"[{symbol}] v5.7 SKIP: invalid last candle")
            return None

        # v5.9.3: Body min 40%→30%. Data: confirmation gate block ~400 signals.
        # 30% masih cukup untuk confirm direction tanpa terlalu ketat.
        body_ratio = last_body / last_total

        if direction == 'LONG':
            # Last candle harus bullish DAN body >= 40%
            is_bullish = last_c > last_o
            close_strength = (last_c - last_l) / last_total

            if not is_bullish:
                logger.debug(f"[{symbol}] v5.7 SKIP: last candle not bullish")
                return None
            if body_ratio < 0.3:
                logger.debug(
                    f"[{symbol}] v5.7 SKIP: body too small "
                    f"({body_ratio:.2f})")
                return None
            if close_strength < 0.6:
                # Close di lower 60% dari range = lemah
                logger.debug(
                    f"[{symbol}] v5.7 SKIP: weak close position")
                return None

        elif direction == 'SHORT':
            is_bearish = last_c < last_o
            close_strength = (last_h - last_c) / last_total

            if not is_bearish:
                logger.debug(f"[{symbol}] v5.7 SKIP: last candle not bearish")
                return None
            if body_ratio < 0.3:
                logger.debug(
                    f"[{symbol}] v5.7 SKIP: body too small "
                    f"({body_ratio:.2f})")
                return None
            if close_strength < 0.6:
                logger.debug(
                    f"[{symbol}] v5.7 SKIP: weak close position")
                return None

    # --- Quality determination (ADAPTIVE per-coin) ---
    quality = _determine_scalp_quality_v43(
        score, kills, direction,
        good_threshold=adapt_params['score_good_threshold'],
        wait_threshold=adapt_params['score_wait_threshold'],
    )

    # Session modifier: downgrade quality kalau session history buruk
    if session_mod < 0 and quality == 'GOOD':
        quality = 'WAIT'  # downgrade
        reasons.append(f"Quality downgraded (session {session_name})")

    if quality is None:
        return None

    # --- Hitung SL dari CONFIRMATION CANDLE (bukan pullback) ---
    # Core Fix 1: Root cause — SL dari pullback + 1.0 ATR terlalu lebar (avg 1.17%).
    # Data 413 trades: SL hit di bar 7.6 avg, TP di bar 14.9 avg.
    # Harga bergerak melawan DULU karena SL jauh = room terlalu besar.
    # Fix: SL di bawah/atas 2 candle terakhir (confirmation zone) + 0.3 ATR buffer.
    # Ini bikin SL lebih ketat → risk kecil → TP lebih achievable.
    candle_lookback = 3  # 3 candle terakhir (confirmation zone)
    if direction == 'LONG':
        recent_low = df_main['low'].iloc[-candle_lookback:].min()
        sl = recent_low - atr * 0.3
    else:
        recent_high = df_main['high'].iloc[-candle_lookback:].max()
        sl = recent_high + atr * 0.3

    risk = abs(price - sl)

    # Safety — v5.9.1: floor REVERTED ke 0.4%
    # v5.9 floor 0.6% bikin banyak trade kena fallback 1.2% → SL/TP terlalu lebar.
    # Floor 0.4% + fallback 1.2% = balance antara noise protection dan achievable TP.
    if risk < price * 0.004:  # minimal 0.4%
        risk = price * 0.012
        if direction == 'LONG':
            sl = price - risk
        else:
            sl = price + risk

    # v5.9.1: TP MODERATE — sweet spot dari 2x backtest.
    # v5.7 (0.5/1.0/1.5R): BEP 35% — TP1 terlalu ketat, noise trigger.
    # v5.9 (0.75/1.3/2.0R): SL 17%, EXPIRED 32% — TP terlalu jauh.
    # v5.9.1 (0.65/1.0/1.5R): TP1 sedikit lebih jauh dari noise,
    # tapi TP2/TP3 tetap achievable. TP1 0.65R = ~0.5% move (genuine).
    if direction == 'LONG':
        tp1 = price + risk * 0.65
        tp2 = price + risk * 1.0
        tp3 = price + risk * 1.5
    else:
        tp1 = price - risk * 0.65
        tp2 = price - risk * 1.0
        tp3 = price - risk * 1.5

    sl_pct = risk / price * 100
    if sl_pct > 3.5:  # slightly wider for trend following
        return None

    rr1 = abs(tp1 - price) / risk if risk > 0 else 0
    rr2 = abs(tp2 - price) / risk if risk > 0 else 0

    reasons.append(f"TP1={tp1:.4g} | TP2={tp2:.4g} | TP3={tp3:.4g}")

    signal = {
        'direction': direction,
        'quality': quality,
        'entry': price,
        'sl': sl,
        'tp1': tp1,
        'tp2': tp2,
        'rr1': round(rr1, 2),
        'rr2': round(rr2, 2),
        'rr': round(rr2, 2),
        'sl_pct': round(sl_pct, 2),
        'reasons': reasons,
        'level_used': 'SUPPORT' if direction == 'LONG' else 'RESISTANCE',
        'confluence_score': score,
        'kill_count': kills,
        'entry_low': price,
        'entry_high': price,
        'tp': tp2,
        'tp_max': tp3,
        'rr_max': round(abs(tp3 - price) / risk if risk > 0 else 0, 2),
        'level_price': price,
        'tp3': tp3,
        'engine': 'scalping_v4.3',
        'strategy': 'trend_following_pullback',
        'trend_state': trend['state'],
        'trend_strength': trend['strength'],
        'pullback_quality': pullback['quality'],
        'session': session_name,
        'macro_4h_bias': macro_bias,
        'adx_1h': round(adx_1h, 1),
        'volume_pressure': vp.get('direction'),
        'buy_pressure_pct': vp.get('buy_pressure_pct'),
        'smc_bos': bos.get('bos'),
        'coin_confidence': adapt_params.get('confidence', 'default'),
        'bb': {
            'upper': round(bb['upper'], 8),
            'middle': round(bb['middle'], 8),
            'lower': round(bb['lower'], 8),
        },
        'macd_state': {
            'cross_up': macd['cross_up'],
            'cross_down': macd['cross_down'],
            'histogram': round(macd['histogram'], 8),
        },
        'rsi_state': {
            'value': round(rsi_data['rsi'], 1),
            'rising': rsi_data['rising'],
            'falling': rsi_data['falling'],
        },
        'wedge': {'pattern': None, 'breakout': False, 'confidence': 0},
        'candle_confirm': candle.get('pattern') if candle.get('confirmed') else None,
    }

    logger.info(
        f"[{symbol}] V3 SIGNAL: {direction} {quality} | "
        f"trend={trend['state']} pullback={pullback['quality']} "
        f"score={score} | entry={price} SL={sl:.4g} "
        f"TP1={tp1:.4g} TP2={tp2:.4g}")

    return signal


# =========================================================
#  CONVENIENCE: Quick test function
# =========================================================
def test_with_dataframe(df: pd.DataFrame, symbol: str = 'TEST') -> dict:
    """
    Helper untuk test cepat dengan DataFrame OHLCV.
    Hitung semua indikator secara mandiri dan panggil generate_scalping_signal.
    """
    from indicators import calc_atr, calc_rsi, calc_adx, analyze_ema_trend

    atr_series = calc_atr(df, 14)
    atr = float(atr_series.iloc[-1]) if atr_series is not None else 0

    rsi_val = float(calc_rsi(df, 14).iloc[-1])
    adx_val = calc_adx(df, 14)
    ema_trend, _, _ = analyze_ema_trend(df)

    price = float(df['close'].iloc[-1])

    return generate_scalping_signal(
        price=price,
        atr=atr,
        ema_trend=ema_trend,
        structure='SIDEWAYS',
        ks=None,
        kr=None,
        res_mtf=[],
        sup_mtf=[],
        smc={},
        rsi=rsi_val,
        htf_ema='SIDEWAYS',
        df_main=df,
        symbol=symbol,
        adx=adx_val,
        signal_cache=None,
    )
