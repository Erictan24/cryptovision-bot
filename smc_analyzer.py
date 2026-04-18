"""
smc_analyzer.py — Smart Money Concepts analysis.

Semua fungsi analisa SMC:
  - BOS / CHoCH
  - Market phase
  - Liquidity mapping
  - Order flow
  - Premium/Discount
  - Volume divergence
  - RSI divergence
  - Candlestick patterns
  - FVG
  - Liquidation zones
  - build_smc_analysis() — master function

Dipisah dari trading_engine.py agar:
  1. Mudah di-test
  2. Mudah di-tune per komponen
  3. trading_engine.py lebih bersih
"""

import numpy as np
import pandas as pd
from config import SIGNAL_PARAMS as SP
from indicators import calc_rsi


# ============================================================
# BOS / CHoCH
# ============================================================

def detect_bos_choch(df: pd.DataFrame, window: int = 3) -> dict:
    """
    Break of Structure dan Change of Character.
    BOS  = konfirmasi trend lanjut
    CHoCH = sinyal reversal
    """
    empty = {'bos': None, 'choch': None, 'swing_highs': [], 'swing_lows': [],
             'last_bos_idx': 0, 'last_choch_idx': 0}

    if df is None or len(df) < window * 2 + 5:
        return empty

    h, l, c = df['high'].values, df['low'].values, df['close'].values
    n = len(df)

    sh, sl_pts = [], []
    for i in range(window, n - window):
        if h[i] >= max(h[i-window:i]) and h[i] >= max(h[i+1:i+window+1]):
            sh.append({'price': h[i], 'idx': i})
        if l[i] <= min(l[i-window:i]) and l[i] <= min(l[i+1:i+window+1]):
            sl_pts.append({'price': l[i], 'idx': i})

    if len(sh) < 2 or len(sl_pts) < 2:
        return {**empty, 'swing_highs': sh, 'swing_lows': sl_pts}

    rsh = sh[-3:]
    rsl = sl_pts[-3:]

    making_hh = len(rsh) >= 2 and rsh[-1]['price'] > rsh[-2]['price']
    making_hl = len(rsl) >= 2 and rsl[-1]['price'] > rsl[-2]['price']
    making_lh = len(rsh) >= 2 and rsh[-1]['price'] < rsh[-2]['price']
    making_ll = len(rsl) >= 2 and rsl[-1]['price'] < rsl[-2]['price']

    uptrend   = making_hh and making_hl
    downtrend = making_lh and making_ll

    bos = choch = None
    last_bos_idx = last_choch_idx = 0
    last_close = c[-1]

    if uptrend:
        if last_close > rsh[-1]['price']:
            bos = 'BULLISH'; last_bos_idx = n - 1
        if last_close < rsl[-1]['price']:
            choch = 'BEARISH'; last_choch_idx = n - 1
    elif downtrend:
        if last_close < rsl[-1]['price']:
            bos = 'BEARISH'; last_bos_idx = n - 1
        if last_close > rsh[-1]['price']:
            choch = 'BULLISH'; last_choch_idx = n - 1

    return {'bos': bos, 'choch': choch,
            'swing_highs': sh, 'swing_lows': sl_pts,
            'last_bos_idx': last_bos_idx, 'last_choch_idx': last_choch_idx}


# ============================================================
# MARKET PHASE
# ============================================================

def detect_market_phase(df: pd.DataFrame, atr: float) -> dict:
    """Wyckoff-inspired phase detection."""
    if df is None or len(df) < 30:
        return {'phase': 'TRANSITION', 'confidence': 0.3, 'desc': 'Data tidak cukup',
                'vol_increase': False, 'range_ratio': 1.0}

    c = df['close'].values
    v = df['volume'].values if 'volume' in df.columns else np.ones(len(df))
    h = df['high'].values
    l = df['low'].values
    n = len(c)

    split    = n * 2 // 3
    prev_c, rec_c = c[:split], c[split:]
    prev_v, rec_v = v[:split], v[split:]
    prev_range    = np.mean(h[:split] - l[:split])
    rec_range     = np.mean(h[split:] - l[split:])

    price_change  = (rec_c[-1] - rec_c[0]) / rec_c[0] if rec_c[0] > 0 else 0
    range_ratio   = rec_range / max(prev_range, 0.001)
    avg_prev_v    = np.mean(prev_v) if len(prev_v) > 0 else 1
    avg_rec_v     = np.mean(rec_v) if len(rec_v) > 0 else 1
    vol_increase  = avg_rec_v > avg_prev_v * SP['vol_div_strong_ratio']

    if price_change < -0.02 and range_ratio > 1.2:
        phase, desc, conf = 'MARKDOWN',     'Harga turun agresif, momentum bearish kuat', min(abs(price_change) * 20, 1.0)
    elif price_change > 0.02 and range_ratio > 1.2:
        phase, desc, conf = 'MARKUP',       'Harga naik kuat, momentum bullish',          min(abs(price_change) * 20, 1.0)
    elif range_ratio < 0.7 and price_change <= 0:
        phase, desc, conf = 'ACCUMULATION', 'Range menyempit di bawah, smart money akumulasi', 0.6 + (0.2 if vol_increase else 0)
    elif range_ratio < 0.7 and price_change > 0:
        phase, desc, conf = 'DISTRIBUTION', 'Range menyempit di atas, smart money distribusi',  0.6 + (0.2 if vol_increase else 0)
    else:
        phase, desc, conf = 'TRANSITION',   'Fase transisi, menunggu konfirmasi', 0.3

    return {'phase': phase, 'confidence': round(conf, 2), 'desc': desc,
            'vol_increase': vol_increase, 'range_ratio': round(range_ratio, 2)}


# ============================================================
# LIQUIDITY MAPPING
# ============================================================

def map_liquidity(df: pd.DataFrame, atr: float) -> dict:
    """Equal Highs (EQH) dan Equal Lows (EQL) = liquidity pools."""
    if df is None or len(df) < 20 or atr <= 0:
        return {'eqh': [], 'eql': [], 'buy_liq': 0, 'sell_liq': 0}

    h, l = df['high'].values, df['low'].values
    n    = len(df)
    tol  = atr * 0.2
    recent = min(50, n)

    eqh, eql = [], []
    for i in range(n - recent, n):
        for j in range(i + 2, min(i + 15, n)):
            if abs(h[i] - h[j]) <= tol:
                eqh.append({'price': (h[i] + h[j]) / 2, 'count': 2,
                             'idx_start': i, 'idx_end': j})
            if abs(l[i] - l[j]) <= tol:
                eql.append({'price': (l[i] + l[j]) / 2, 'count': 2,
                             'idx_start': i, 'idx_end': j})

    eqh = _cluster_eq_levels(eqh, tol)
    eql = _cluster_eq_levels(eql, tol)

    price    = df['close'].iloc[-1]
    buy_liq  = sum(1 for e in eqh if e['price'] > price)
    sell_liq = sum(1 for e in eql if e['price'] < price)

    return {'eqh': eqh[:5], 'eql': eql[:5],
            'buy_liq': buy_liq, 'sell_liq': sell_liq}


def _cluster_eq_levels(levels: list, tol: float) -> list:
    if not levels:
        return []
    levels.sort(key=lambda x: x['price'])
    clustered = []
    current = levels[0].copy()
    for i in range(1, len(levels)):
        if abs(levels[i]['price'] - current['price']) <= tol * 2:
            current['count'] += levels[i]['count']
            current['price']  = (current['price'] + levels[i]['price']) / 2
        else:
            clustered.append(current)
            current = levels[i].copy()
    clustered.append(current)
    return sorted(clustered, key=lambda x: x['count'], reverse=True)


# ============================================================
# ORDER FLOW
# ============================================================

def analyze_order_flow(df: pd.DataFrame, lookback: int = 20) -> dict:
    """Tekanan beli vs jual dari price action dan volume."""
    if df is None or len(df) < lookback:
        return {'bias': 'NEUTRAL', 'score': 0, 'bull_pct': 50, 'details': ''}

    recent = df.iloc[-lookback:]
    o, c, h, l = (recent['open'].values, recent['close'].values,
                  recent['high'].values, recent['low'].values)
    v = recent['volume'].values if 'volume' in recent.columns else np.ones(len(c))

    bull_candles = sum(1 for i in range(len(c)) if c[i] > o[i])
    bear_candles = len(c) - bull_candles

    close_positions = []
    for i in range(len(c)):
        rng = h[i] - l[i]
        close_positions.append((c[i] - l[i]) / rng if rng > 0 else 0.5)
    avg_close_pos = np.mean(close_positions[-10:])

    bull_vol  = sum(v[i] for i in range(len(c)) if c[i] > o[i])
    bear_vol  = sum(v[i] for i in range(len(c)) if c[i] <= o[i])
    total_vol = bull_vol + bear_vol
    bull_vol_pct = (bull_vol / max(total_vol, 1)) * 100

    score  = 0
    score += (bull_candles - bear_candles) * 3
    score += (avg_close_pos - 0.5) * 40
    score += (bull_vol_pct - 50) * 0.5
    score  = max(-100, min(100, score))

    threshold = SP['order_flow_bias_threshold']
    if score > threshold:
        bias = 'BULLISH';  details = f'Tekanan beli dominan ({bull_candles}/{len(c)} bullish)'
    elif score < -threshold:
        bias = 'BEARISH'; details = f'Tekanan jual dominan ({bear_candles}/{len(c)} bearish)'
    else:
        bias = 'NEUTRAL'; details = f'Seimbang ({bull_candles}/{len(c)} bullish)'

    return {'bias': bias, 'score': round(score), 'bull_pct': round(bull_vol_pct),
            'bear_pct': round(100 - bull_vol_pct), 'avg_close_pos': round(avg_close_pos, 2),
            'details': details}


# ============================================================
# PREMIUM / DISCOUNT
# ============================================================

def calc_premium_discount(df: pd.DataFrame, price: float) -> dict:
    """
    Di atas 50% dealing range = PREMIUM (ideal SHORT)
    Di bawah 50% = DISCOUNT (ideal LONG)
    """
    if df is None or len(df) < 20:
        return {'zone': 'EQUILIBRIUM', 'pct': 50, 'range_high': 0, 'range_low': 0}

    recent    = df.iloc[-50:]
    range_hi  = recent['high'].max()
    range_lo  = recent['low'].min()
    total_rng = range_hi - range_lo

    if total_rng <= 0:
        return {'zone': 'EQUILIBRIUM', 'pct': 50, 'range_high': range_hi, 'range_low': range_lo}

    position = (price - range_lo) / total_rng * 100

    if position > SP['pd_premium_pct']:          zone = 'PREMIUM'
    elif position < SP['pd_discount_pct']:        zone = 'DISCOUNT'
    elif position > SP['pd_slight_premium_pct']:  zone = 'SLIGHT_PREMIUM'
    elif position < SP['pd_slight_discount_pct']: zone = 'SLIGHT_DISCOUNT'
    else:                                          zone = 'EQUILIBRIUM'

    return {'zone': zone, 'pct': round(position, 1),
            'range_high': range_hi, 'range_low': range_lo,
            'eq_level': (range_hi + range_lo) / 2}


# ============================================================
# VOLUME DIVERGENCE
# ============================================================

def detect_volume_divergence(df: pd.DataFrame) -> dict:
    """Harga naik/turun tapi volume melawan = divergence."""
    if df is None or len(df) < 20:
        return {'divergence': None, 'desc': ''}

    c = df['close'].values
    v = df['volume'].values if 'volume' in df.columns else np.ones(len(df))
    n = len(c)
    mid = n // 2

    price_trend = c[-1] - c[mid]
    vol_prev    = np.mean(v[max(0, mid-5):mid+5])
    vol_recent  = np.mean(v[-10:])

    weak   = SP['vol_div_weak_ratio']
    strong = SP['vol_div_strong_ratio']

    if price_trend > 0 and vol_recent < vol_prev * weak:
        return {'divergence': 'BEARISH',      'desc': 'Harga naik tapi volume melemah — potensi reversal turun'}
    if price_trend < 0 and vol_recent < vol_prev * weak:
        return {'divergence': 'BULLISH',      'desc': 'Harga turun tapi volume mengering — seller habis'}
    if price_trend > 0 and vol_recent > vol_prev * strong:
        return {'divergence': 'CONFIRM_BULL', 'desc': 'Harga naik dengan volume tinggi — trend kuat'}
    if price_trend < 0 and vol_recent > vol_prev * strong:
        return {'divergence': 'CONFIRM_BEAR', 'desc': 'Harga turun dengan volume tinggi — tekanan jual kuat'}
    return {'divergence': None, 'desc': 'Volume normal'}


# ============================================================
# RSI DIVERGENCE
# ============================================================

def detect_rsi_divergence(df: pd.DataFrame, period: int = 14) -> dict:
    """
    Bullish Div  : price LL, RSI HL → reversal UP
    Bearish Div  : price HH, RSI LH → reversal DOWN
    Hidden Bull  : price HL, RSI LL → continuation UP
    Hidden Bear  : price LH, RSI HH → continuation DOWN
    """
    if df is None or len(df) < period + 20:
        return {'type': None, 'desc': ''}

    c    = df['close'].values
    rsi_ = calc_rsi(df, period)
    if rsi_ is None:
        return {'type': None, 'desc': ''}
    rsi  = rsi_.values
    n    = len(c)

    lookback = min(30, n - 5)
    seg_c = c[-lookback:]
    seg_r = rsi[-lookback:]
    seg_l = df['low'].values[-lookback:]
    seg_h = df['high'].values[-lookback:]

    lows_p, lows_r, highs_p, highs_r = [], [], [], []
    for i in range(2, len(seg_c) - 2):
        if seg_l[i] <= min(seg_l[i-2:i]) and seg_l[i] <= min(seg_l[i+1:i+3]):
            lows_p.append(seg_l[i]); lows_r.append(seg_r[i])
        if seg_h[i] >= max(seg_h[i-2:i]) and seg_h[i] >= max(seg_h[i+1:i+3]):
            highs_p.append(seg_h[i]); highs_r.append(seg_r[i])

    if len(lows_p) >= 2:
        if lows_p[-1] < lows_p[-2] and lows_r[-1] > lows_r[-2]:
            return {'type': 'BULLISH_DIV', 'desc': 'RSI Bullish Divergence'}
        if lows_p[-1] > lows_p[-2] and lows_r[-1] < lows_r[-2]:
            return {'type': 'HIDDEN_BULL',  'desc': 'Hidden Bullish Div — trend continuation naik'}
    if len(highs_p) >= 2:
        if highs_p[-1] > highs_p[-2] and highs_r[-1] < highs_r[-2]:
            return {'type': 'BEARISH_DIV', 'desc': 'RSI Bearish Divergence'}
        if highs_p[-1] < highs_p[-2] and highs_r[-1] > highs_r[-2]:
            return {'type': 'HIDDEN_BEAR',  'desc': 'Hidden Bearish Div — trend continuation turun'}

    return {'type': None, 'desc': ''}


# ============================================================
# CONFIRMATION CANDLE
# ============================================================

def detect_snr_rejection(df: pd.DataFrame, level_low: float,
                          level_high: float, direction: str,
                          atr: float, confirmed_only: bool = True) -> dict:
    """
    Deteksi rejection pattern di S&R zone — GATE KERAS sebelum entry.

    Ini adalah fungsi utama yang menentukan apakah bot boleh entry.
    Harus ada minimal 1 rejection pattern yang valid di zone sebelum entry.

    Pattern yang dideteksi (strength 1-5):
      Strength 5 — Konfirmasi sangat kuat:
        - Bullish/Bearish Engulfing di zone + volume tinggi
        - Pin Bar kuat (wick > 3x body) + close kuat
        - Morning/Evening Star di zone

      Strength 4 — Konfirmasi kuat:
        - Engulfing tanpa volume boost
        - Pin Bar sedang (wick > 2x body) + close bersih
        - Tweezer Bottom/Top di zone

      Strength 3 — Konfirmasi cukup:
        - Pin Bar lemah (wick > 1.5x body)
        - Rejection wick + close balik ke dalam zone
        - Hammer/Shooting Star di zone
        - Inside bar breakout dari zone

      Strength 2 — Konfirmasi minimum:
        - Rejection wick biasa
        - Bearish/Bullish close tepat di batas zone
        - Doji + candle arah benar setelah

      Strength 1 — Terlalu lemah, tidak diterima:
        - Body kecil saja tanpa wick
        - Candle belum konfirmasi

    Returns:
        dict dengan:
          'confirmed'  : bool  — True jika strength >= min_strength
          'pattern'    : str   — nama pattern terbaik
          'strength'   : int   — 1-5
          'detail'     : str   — deskripsi lengkap untuk reasons
          'candle_idx' : int   — berapa candle yang lalu (0=current, 1=1 candle lalu)
    """
    no_confirm = {
        'confirmed': False, 'pattern': 'Menunggu rejection',
        'strength': 0, 'detail': 'Belum ada rejection di zone', 'candle_idx': -1
    }

    if df is None or len(df) < 4 or atr <= 0:
        return no_confirm

    # Hitung avg volume
    has_vol = 'volume' in df.columns
    avg_vol = 0.0
    if has_vol and len(df) >= 20:
        avg_vol = float(df['volume'].rolling(20).mean().iloc[-1])
    if avg_vol <= 0:
        has_vol = False

    h_arr = df['high'].values
    l_arr = df['low'].values
    o_arr = df['open'].values
    c_arr = df['close'].values
    v_arr = df['volume'].values if has_vol else None
    n     = len(df)

    best_strength = 0
    best_pattern  = 'none'
    best_detail   = ''
    best_idx      = -1

    def vol_factor(idx):
        """Volume multiplier: >1.5x avg = strong, <0.7x = weak."""
        if not has_vol or avg_vol <= 0: return 1.0
        cv = float(v_arr[idx])
        if cv > avg_vol * 2.0: return 1.5
        if cv > avg_vol * 1.5: return 1.2
        if cv < avg_vol * 0.7: return 0.7
        return 1.0

    def touches_zone(idx, direction):
        """Apakah candle ini menyentuh zone S&R?"""
        if direction == 'LONG':
            return l_arr[idx] <= level_high * 1.003  # sedikit toleransi
        else:
            return h_arr[idx] >= level_low  * 0.997

    # Scan candle terakhir:
    # confirmed_only=True → mulai dari candle ke-1 (1 candle sebelum current)
    # Ini penting untuk live trading — candle ke-0 mungkin masih forming
    # confirmed_only=False → boleh pakai candle ke-0 (cocok untuk backtest)
    start_offset = 1 if confirmed_only else 0
    for offset in range(start_offset, start_offset + 3):
        idx = n - 1 - offset
        if idx < 2:
            break

        o, c, h, l = o_arr[idx], c_arr[idx], h_arr[idx], l_arr[idx]
        full_range  = h - l
        if full_range < atr * 0.1:
            continue

        body       = abs(c - o)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        is_bull_c  = c > o
        is_bear_c  = c < o

        # Prev candles
        o1, c1, h1, l1 = o_arr[idx-1], c_arr[idx-1], h_arr[idx-1], l_arr[idx-1]
        o2, c2, h2, l2 = o_arr[idx-2], c_arr[idx-2], h_arr[idx-2], l_arr[idx-2]
        body1  = abs(c1 - o1)
        body2  = abs(c2 - o2)
        vf     = vol_factor(idx)

        if not touches_zone(idx, direction):
            continue

        strength = 0
        pattern  = 'none'
        detail   = ''

        # ============================
        # LONG patterns (support rejection)
        # ============================
        if direction == 'LONG':

            # === Strength 5: Engulfing + volume ===
            if (is_bull_c and c1 < o1 and            # prev bearish
                c > o1 and o < c1 and                 # full engulf
                body > body1 and body > atr * 0.3 and
                vf >= 1.2):
                strength = 5
                pattern  = 'Bullish Engulfing (vol)'
                detail   = f'Bullish Engulfing kuat di support, vol {vf:.1f}x'

            # === Strength 5: Pin Bar sangat kuat ===
            elif (lower_wick > body * 3.5 and
                  lower_wick > atr * 0.5 and
                  upper_wick < body * 0.5 and
                  is_bull_c):
                strength = int(5 * vf)
                strength = min(5, max(4, strength))
                pattern  = 'Pin Bar Bullish Kuat'
                detail   = f'Pin Bar kuat: wick {lower_wick/atr:.1f}x ATR, close bullish'

            # === Strength 4: Engulfing tanpa vol ===
            elif (strength == 0 and
                  is_bull_c and c1 < o1 and
                  c > o1 and o < c1 and body > body1):
                strength = 4
                pattern  = 'Bullish Engulfing'
                detail   = f'Bullish Engulfing di support'

            # === Strength 4: Pin Bar sedang ===
            elif (strength == 0 and
                  lower_wick > body * 2.0 and
                  lower_wick > atr * 0.35 and
                  upper_wick < lower_wick * 0.4):
                strength = 4 if is_bull_c else 3
                pattern  = 'Pin Bar Bullish'
                detail   = f'Pin Bar: lower wick {lower_wick/atr:.1f}x ATR'

            # === Strength 4: Tweezer Bottom ===
            elif (strength == 0 and
                  abs(l - l1) < atr * 0.08 and
                  c1 < o1 and is_bull_c and
                  body > atr * 0.2):
                strength = 4
                pattern  = 'Tweezer Bottom'
                detail   = f'Tweezer Bottom: double low di {l:.4f}'

            # === Strength 4: Morning Star ===
            elif (strength == 0 and
                  c2 < o2 and body2 > atr * 0.3 and    # candle bear besar
                  body1 < atr * 0.15 and                 # doji/inside
                  is_bull_c and body > atr * 0.3 and    # candle bull besar
                  c > (o2 + c2) / 2):                   # close > midpoint c1
                strength = 4
                pattern  = 'Morning Star'
                detail   = f'Morning Star di support'

            # === Strength 3: Hammer / Pin Bar lemah ===
            elif (strength == 0 and
                  lower_wick > body * 1.5 and
                  lower_wick > atr * 0.25 and
                  upper_wick < lower_wick * 0.6):
                strength = 3
                pattern  = 'Hammer'
                detail   = f'Hammer: lower wick {lower_wick/atr:.1f}x ATR'

            # === Strength 3: Rejection wick close balik ===
            elif (strength == 0 and
                  l < level_low * 0.998 and              # sempat tembus sedikit
                  c > level_low and c > level_high * 0.97 and  # close balik di atas
                  lower_wick > body * 1.2):
                strength = 3
                pattern  = 'Rejection Wick'
                detail   = f'Rejection: tembus {l:.4f}, close kembali {c:.4f}'

            # === Strength 3: Inside bar bullish breakout ===
            elif (strength == 0 and
                  h < h1 and l > l1 and                  # inside bar
                  is_bull_c and c > (h1 + l1) / 2):     # close > midpoint induk
                strength = 3
                pattern  = 'Inside Bar Bullish'
                detail   = f'Inside Bar bullish di support'

            # === Strength 2: Rejection minimum ===
            elif (strength == 0 and
                  lower_wick > body * 1.0 and
                  lower_wick > atr * 0.2 and
                  l <= level_high):
                strength = 2
                pattern  = 'Candle Rejection'
                detail   = f'Candle rejection di support'

        # ============================
        # SHORT patterns (resistance rejection)
        # ============================
        else:

            # === Strength 5: Engulfing + volume ===
            if (is_bear_c and c1 > o1 and
                c < o1 and o > c1 and
                body > body1 and body > atr * 0.3 and
                vf >= 1.2):
                strength = 5
                pattern  = 'Bearish Engulfing (vol)'
                detail   = f'Bearish Engulfing kuat di resistance, vol {vf:.1f}x'

            # === Strength 5: Pin Bar sangat kuat ===
            elif (upper_wick > body * 3.5 and
                  upper_wick > atr * 0.5 and
                  lower_wick < body * 0.5 and
                  is_bear_c):
                strength = int(5 * vf)
                strength = min(5, max(4, strength))
                pattern  = 'Pin Bar Bearish Kuat'
                detail   = f'Pin Bar kuat: wick {upper_wick/atr:.1f}x ATR, close bearish'

            # === Strength 4: Engulfing tanpa vol ===
            elif (strength == 0 and
                  is_bear_c and c1 > o1 and
                  c < o1 and o > c1 and body > body1):
                strength = 4
                pattern  = 'Bearish Engulfing'
                detail   = f'Bearish Engulfing di resistance'

            # === Strength 4: Pin Bar sedang ===
            elif (strength == 0 and
                  upper_wick > body * 2.0 and
                  upper_wick > atr * 0.35 and
                  lower_wick < upper_wick * 0.4):
                strength = 4 if is_bear_c else 3
                pattern  = 'Pin Bar Bearish'
                detail   = f'Pin Bar: upper wick {upper_wick/atr:.1f}x ATR'

            # === Strength 4: Tweezer Top ===
            elif (strength == 0 and
                  abs(h - h1) < atr * 0.08 and
                  c1 > o1 and is_bear_c and
                  body > atr * 0.2):
                strength = 4
                pattern  = 'Tweezer Top'
                detail   = f'Tweezer Top: double high di {h:.4f}'

            # === Strength 4: Evening Star ===
            elif (strength == 0 and
                  c2 > o2 and body2 > atr * 0.3 and
                  body1 < atr * 0.15 and
                  is_bear_c and body > atr * 0.3 and
                  c < (o2 + c2) / 2):
                strength = 4
                pattern  = 'Evening Star'
                detail   = f'Evening Star di resistance'

            # === Strength 3: Shooting Star / Pin Bar lemah ===
            elif (strength == 0 and
                  upper_wick > body * 1.5 and
                  upper_wick > atr * 0.25 and
                  lower_wick < upper_wick * 0.6):
                strength = 3
                pattern  = 'Shooting Star'
                detail   = f'Shooting Star: upper wick {upper_wick/atr:.1f}x ATR'

            # === Strength 3: Rejection wick close balik ===
            elif (strength == 0 and
                  h > level_high * 1.002 and
                  c < level_high and c < level_low * 1.03 and
                  upper_wick > body * 1.2):
                strength = 3
                pattern  = 'Rejection Wick'
                detail   = f'Rejection: tembus {h:.4f}, close kembali {c:.4f}'

            # === Strength 3: Inside bar bearish breakout ===
            elif (strength == 0 and
                  h < h1 and l > l1 and
                  is_bear_c and c < (h1 + l1) / 2):
                strength = 3
                pattern  = 'Inside Bar Bearish'
                detail   = f'Inside Bar bearish di resistance'

            # === Strength 2: Rejection minimum ===
            elif (strength == 0 and
                  upper_wick > body * 1.0 and
                  upper_wick > atr * 0.2 and
                  h >= level_low):
                strength = 2
                pattern  = 'Candle Rejection'
                detail   = f'Candle rejection di resistance'

        # Volume boost/penalty untuk semua pattern
        # Strength 4+ dengan volume sangat rendah diturunkan ke strength 3
        # Threshold 0.8x — tidak terlalu ketat, tapi tetap filter fake rejection
        if strength > 0:
            if vf >= 1.5 and strength < 5:
                strength = min(5, strength + 1)
            elif vf >= 1.2 and strength == 3:
                strength = 4   # Pattern cukup + volume ok = naik ke 4
            elif vf < 0.8 and strength >= 4:
                strength = 3   # Volume sangat rendah = turunkan
            elif vf <= 0.7 and strength > 1:
                strength -= 1  # Volume sangat rendah = penalty extra

        if strength > best_strength:
            best_strength = strength
            best_pattern  = pattern
            best_detail   = detail
            best_idx      = offset

    # Konfirmasi jika strength >= 3
    # candle_idx 1 = 1 candle lalu (sudah close = lebih valid)
    # candle_idx 0 = candle sekarang (masih forming = kurang valid)
    confirmed = best_strength >= 3

    candle_age_desc = ''
    if best_idx == 1:
        candle_age_desc = '(candle terakhir yang sudah close)'
    elif best_idx == 2:
        candle_age_desc = '(2 candle lalu)'
    elif best_idx == 3:
        candle_age_desc = '(3 candle lalu — agak basi)'
    elif best_idx == 0:
        candle_age_desc = '(candle sedang forming)'

    return {
        'confirmed'     : confirmed,
        'pattern'       : best_pattern,
        'strength'      : best_strength,
        'detail'        : f"{best_detail} {candle_age_desc}".strip(),
        'candle_idx'    : best_idx,
        'candle_age'    : candle_age_desc,
    }


def detect_confirmation_candle(df: pd.DataFrame, level_low: float,
                                level_high: float, direction: str,
                                atr: float) -> dict:
    """
    Wrapper backward-compatible untuk detect_snr_rejection.
    Dipanggil dari signal_generator._score_direction() untuk scoring.
    Gate keras ada di generate_entry_signal().
    """
    result = detect_snr_rejection(df, level_low, level_high, direction, atr)
    return {
        'confirmed': result['confirmed'],
        'pattern'  : result['detail'] if result['confirmed'] else result['pattern'],
        'score'    : min(result['strength'], 3),
    }


# ============================================================
# FVG — Fair Value Gap
# ============================================================

def detect_fvg(df: pd.DataFrame, atr: float, price: float) -> dict:
    """Imbalance candles = magnet harga."""
    if df is None or len(df) < 10 or atr <= 0:
        return {'bull_fvg': [], 'bear_fvg': [], 'nearest': None}

    h, l = df['high'].values, df['low'].values
    n    = len(df)
    min_gap = atr * 0.5

    bull_fvg, bear_fvg = [], []
    for i in range(1, n - 1):
        gap_bull = l[i+1] - h[i-1]
        if gap_bull >= min_gap:
            mid = (l[i+1] + h[i-1]) / 2
            bull_fvg.append({'low': h[i-1], 'high': l[i+1], 'mid': mid, 'idx': i})

        gap_bear = l[i-1] - h[i+1]
        if gap_bear >= min_gap:
            mid = (l[i-1] + h[i+1]) / 2
            bear_fvg.append({'low': h[i+1], 'high': l[i-1], 'mid': mid, 'idx': i})

    bull_fvg = [f for f in bull_fvg if f['mid'] < price][-3:]
    bear_fvg = [f for f in bear_fvg if f['mid'] > price][:3]

    all_fvg = [('bull', f) for f in bull_fvg] + [('bear', f) for f in bear_fvg]
    nearest = min(all_fvg, key=lambda x: abs(price - x[1]['mid']),
                  default=(None, None))
    nearest_dict = {'type': nearest[0], **nearest[1]} if nearest[0] else None

    return {'bull_fvg': bull_fvg, 'bear_fvg': bear_fvg, 'nearest': nearest_dict}


# ============================================================
# CANDLESTICK PATTERNS
# ============================================================

def detect_candle_patterns(df: pd.DataFrame, atr: float = None) -> list:
    """15+ candlestick patterns dari 3 candle terakhir."""
    if df is None or len(df) < 5:
        return []

    h, l, o, c = (df['high'].values, df['low'].values,
                  df['open'].values, df['close'].values)
    n = len(df)

    if atr is None:
        atr = float(np.mean(h[-20:] - l[-20:]))
    if atr <= 0:
        atr = 1.0

    patterns = []
    i, i1, i2 = n-1, n-2, n-3

    def body(k):     return abs(c[k] - o[k])
    def uw(k):       return h[k] - max(o[k], c[k])
    def lw(k):       return min(o[k], c[k]) - l[k]
    def is_bull(k):  return c[k] > o[k]
    def is_bear(k):  return c[k] < o[k]
    def cr(k):       return h[k] - l[k]

    b0, b1, b2 = body(i), body(i1), body(i2)
    cr0 = cr(i)
    uw0, lw0 = uw(i), lw(i)
    uw1, lw1 = uw(i1), lw(i1)

    # Single candle
    if lw0 >= b0 * 2 and uw0 < b0 * 0.5 and cr0 > atr * 0.5:
        patterns.append({'pattern': 'Hammer',           'direction': 'BULL', 'strength': 2, 'desc': 'Hammer'})
    if uw0 >= b0 * 2 and lw0 < b0 * 0.5 and cr0 > atr * 0.5 and is_bear(i1):
        patterns.append({'pattern': 'Inverted Hammer',  'direction': 'BULL', 'strength': 1, 'desc': 'Inverted Hammer'})
    if uw0 >= b0 * 2 and lw0 < b0 * 0.5 and cr0 > atr * 0.5 and is_bull(i1):
        patterns.append({'pattern': 'Shooting Star',    'direction': 'BEAR', 'strength': 2, 'desc': 'Shooting Star'})
    if lw0 >= b0 * 2 and uw0 < b0 * 0.5 and is_bull(i1) and is_bull(i2):
        patterns.append({'pattern': 'Hanging Man',      'direction': 'BEAR', 'strength': 1, 'desc': 'Hanging Man'})
    if b0 < atr * 0.1 and cr0 > atr * 0.5:
        d_type = 'Dragonfly Doji' if lw0 > uw0 * 2 else ('Gravestone Doji' if uw0 > lw0 * 2 else 'Doji')
        d_dir  = 'BULL' if 'Dragonfly' in d_type else ('BEAR' if 'Gravestone' in d_type else 'NEUTRAL')
        patterns.append({'pattern': d_type, 'direction': d_dir, 'strength': 1, 'desc': d_type})
    if b0 > atr * 0.8 and uw0 < b0 * 0.1 and lw0 < b0 * 0.1:
        d = 'BULL' if is_bull(i) else 'BEAR'
        patterns.append({'pattern': f'Marubozu {d.title()}', 'direction': d, 'strength': 2, 'desc': 'Marubozu'})

    # Double candle
    if is_bear(i1) and is_bull(i) and c[i] > o[i1] and o[i] < c[i1] and b0 > b1:
        patterns.append({'pattern': 'Bullish Engulfing', 'direction': 'BULL', 'strength': 3, 'desc': 'Bullish Engulfing'})
    if is_bull(i1) and is_bear(i) and c[i] < o[i1] and o[i] > c[i1] and b0 > b1:
        patterns.append({'pattern': 'Bearish Engulfing', 'direction': 'BEAR', 'strength': 3, 'desc': 'Bearish Engulfing'})
    if abs(h[i] - h[i1]) < atr * 0.05 and is_bull(i1) and is_bear(i):
        patterns.append({'pattern': 'Tweezer Top',       'direction': 'BEAR', 'strength': 2, 'desc': 'Tweezer Top'})
    if abs(l[i] - l[i1]) < atr * 0.05 and is_bear(i1) and is_bull(i):
        patterns.append({'pattern': 'Tweezer Bottom',    'direction': 'BULL', 'strength': 2, 'desc': 'Tweezer Bottom'})
    if is_bear(i1) and is_bull(i) and o[i] < l[i1] and c[i] > (o[i1] + c[i1]) / 2:
        patterns.append({'pattern': 'Piercing Line',     'direction': 'BULL', 'strength': 2, 'desc': 'Piercing Line'})
    if is_bull(i1) and is_bear(i) and o[i] > h[i1] and c[i] < (o[i1] + c[i1]) / 2:
        patterns.append({'pattern': 'Dark Cloud Cover',  'direction': 'BEAR', 'strength': 2, 'desc': 'Dark Cloud Cover'})

    # Triple candle
    if is_bear(i2) and b1 < b2 * 0.3 and is_bull(i) and c[i] > (o[i2] + c[i2]) / 2:
        patterns.append({'pattern': 'Morning Star',        'direction': 'BULL', 'strength': 3, 'desc': 'Morning Star'})
    if is_bull(i2) and b1 < b2 * 0.3 and is_bear(i) and c[i] < (o[i2] + c[i2]) / 2:
        patterns.append({'pattern': 'Evening Star',        'direction': 'BEAR', 'strength': 3, 'desc': 'Evening Star'})
    if all(is_bull(n-1-j) for j in range(3)) and c[i] > c[i1] > c[i2] and all(body(n-1-j) > atr * 0.3 for j in range(3)):
        patterns.append({'pattern': 'Three White Soldiers','direction': 'BULL', 'strength': 3, 'desc': 'Three White Soldiers'})
    if all(is_bear(n-1-j) for j in range(3)) and c[i] < c[i1] < c[i2] and all(body(n-1-j) > atr * 0.3 for j in range(3)):
        patterns.append({'pattern': 'Three Black Crows',   'direction': 'BEAR', 'strength': 3, 'desc': 'Three Black Crows'})

    return patterns


# ============================================================
# LIQUIDATION ZONE ESTIMATION
# ============================================================

def estimate_liquidation_zones(df: pd.DataFrame, price: float,
                                atr: float, derivatives: dict = None) -> dict:
    """Estimasi leverage-based dan swing-based liquidation zones."""
    result = {
        'long_liq_zones': [], 'short_liq_zones': [],
        'nearest_long_liq': 0, 'nearest_short_liq': 0,
        'liq_bias': 'NEUTRAL'
    }
    if df is None or len(df) < 20:
        return result

    from sr_detector import find_swings
    swing_lo, swing_hi = find_swings(df, window=3)

    long_zones  = []
    short_zones = []

    for lev, pct in [(5, 0.18), (10, 0.09), (20, 0.045), (50, 0.018)]:
        long_zones.append({'price': round(price * (1 - pct), 8), 'leverage': lev,
                           'strength': 3 if lev >= 20 else (2 if lev >= 10 else 1)})
        short_zones.append({'price': round(price * (1 + pct), 8), 'leverage': lev,
                            'strength': 3 if lev >= 20 else (2 if lev >= 10 else 1)})

    for sl in swing_lo[-5:]:
        long_zones.append({'price': round(sl['price'] - atr * 0.3, 8),
                           'leverage': 0, 'strength': 2, 'note': 'swing_sl'})
    for sh in swing_hi[-5:]:
        short_zones.append({'price': round(sh['price'] + atr * 0.3, 8),
                            'leverage': 0, 'strength': 2, 'note': 'swing_sl'})

    if derivatives and derivatives.get('available'):
        oi_change = derivatives.get('oi_change_pct', 0)
        if abs(oi_change) > 5:
            for z in long_zones + short_zones:
                z['strength'] += 1

    result['long_liq_zones']  = sorted(long_zones,  key=lambda x: -x['price'])[:8]
    result['short_liq_zones'] = sorted(short_zones, key=lambda x:  x['price'])[:8]

    ll_below = [z['price'] for z in long_zones  if z['price'] < price]
    sl_above = [z['price'] for z in short_zones if z['price'] > price]

    if ll_below: result['nearest_long_liq']  = max(ll_below)
    if sl_above: result['nearest_short_liq'] = min(sl_above)

    dist_long  = price - result['nearest_long_liq']  if result['nearest_long_liq']  else price
    dist_short = result['nearest_short_liq'] - price if result['nearest_short_liq'] else price

    if dist_long < dist_short * 0.7:    result['liq_bias'] = 'LONG_VULNERABLE'
    elif dist_short < dist_long * 0.7:  result['liq_bias'] = 'SHORT_VULNERABLE'

    return result


# ============================================================
# MASTER: build_smc_analysis
# ============================================================

def build_smc_analysis(df_main: pd.DataFrame,
                       df_higher: pd.DataFrame | None,
                       price: float, atr: float,
                       symbol: str = '',
                       fetch_derivatives_fn=None) -> dict:
    """
    Gabungkan semua SMC analysis.
    fetch_derivatives_fn: callable(symbol) → dict, optional
    """
    smc = {}

    smc['bos_choch'] = detect_bos_choch(df_main)
    smc['htf_bos']   = (detect_bos_choch(df_higher, window=5)
                        if df_higher is not None and len(df_higher) >= 15
                        else {'bos': None, 'choch': None})
    smc['phase']        = detect_market_phase(df_main, atr)
    smc['liquidity']    = map_liquidity(df_main, atr)
    smc['order_flow']   = analyze_order_flow(df_main)
    smc['pd_zone']      = calc_premium_discount(df_main, price)
    smc['vol_div']      = detect_volume_divergence(df_main)
    smc['candle_patterns'] = detect_candle_patterns(df_main, atr)
    smc['rsi_div']      = detect_rsi_divergence(df_main)
    smc['fvg']          = detect_fvg(df_main, atr, price)

    # Order block proximity — apakah price dekat order block valid?
    # OB adalah zona institusi — bounce dari OB lebih reliable dari S&R biasa
    try:
        from sr_detector import find_order_blocks
        bull_ob, bear_ob = find_order_blocks(df_main, atr)
        ob_near_bull = [ob for ob in bull_ob
                        if ob.get('fresh', False)
                        and ob['low'] <= price <= ob['high'] * 1.01]
        ob_near_bear = [ob for ob in bear_ob
                        if ob.get('fresh', False)
                        and ob['low'] * 0.99 <= price <= ob['high']]
        smc['order_blocks'] = {
            'near_bull': ob_near_bull[:2],
            'near_bear': ob_near_bear[:2],
            'at_bull_ob': len(ob_near_bull) > 0,
            'at_bear_ob': len(ob_near_bear) > 0,
        }
    except Exception:
        smc['order_blocks'] = {'near_bull': [], 'near_bear': [],
                               'at_bull_ob': False, 'at_bear_ob': False}

    # Derivatives: optional external fetch
    if fetch_derivatives_fn and symbol:
        smc['derivatives'] = fetch_derivatives_fn(symbol)
    else:
        smc['derivatives'] = {'funding_rate': 0, 'funding_bias': 'NEUTRAL',
                               'oi': 0, 'oi_change_pct': 0, 'oi_bias': 'NEUTRAL',
                               'lsr': 1.0, 'lsr_bias': 'NEUTRAL',
                               'sentiment': 'NEUTRAL', 'available': False}

    smc['liquidation'] = estimate_liquidation_zones(df_main, price, atr, smc['derivatives'])

    # --- Compute smart bias ---
    bull_pts = bear_pts = 0

    bc = smc['bos_choch']
    if bc['bos'] == 'BULLISH':   bull_pts += 3
    if bc['bos'] == 'BEARISH':   bear_pts += 3
    if bc['choch'] == 'BULLISH': bull_pts += 4
    if bc['choch'] == 'BEARISH': bear_pts += 4

    hbc = smc['htf_bos']
    if hbc['bos'] == 'BULLISH':   bull_pts += 5
    if hbc['bos'] == 'BEARISH':   bear_pts += 5
    if hbc['choch'] == 'BULLISH': bull_pts += 6
    if hbc['choch'] == 'BEARISH': bear_pts += 6

    ph = smc['phase']['phase']
    phase_pts = {'ACCUMULATION': (3,0), 'MARKUP': (2,0), 'DISTRIBUTION': (0,3), 'MARKDOWN': (0,2)}
    b, be = phase_pts.get(ph, (0, 0))
    bull_pts += b; bear_pts += be

    of = smc['order_flow']['bias']
    if of == 'BULLISH': bull_pts += 2
    if of == 'BEARISH': bear_pts += 2

    pz = smc['pd_zone']['zone']
    if pz in ('DISCOUNT', 'SLIGHT_DISCOUNT'): bull_pts += 2
    if pz in ('PREMIUM',  'SLIGHT_PREMIUM'):  bear_pts += 2

    vd = smc['vol_div']['divergence']
    vd_map = {'BULLISH': (2,0), 'BEARISH': (0,2), 'CONFIRM_BULL': (1,0), 'CONFIRM_BEAR': (0,1)}
    b, be = vd_map.get(vd, (0, 0))
    bull_pts += b; bear_pts += be

    liq = smc['liquidity']
    if liq['buy_liq']  > liq['sell_liq']: bear_pts += 1
    if liq['sell_liq'] > liq['buy_liq']:  bull_pts += 1

    deriv = smc['derivatives']
    if deriv.get('available'):
        if deriv['sentiment'] == 'BULLISH': bull_pts += 2
        elif deriv['sentiment'] == 'BEARISH': bear_pts += 2
        if deriv['funding_rate'] > 0.05:  bear_pts += 2
        elif deriv['funding_rate'] < -0.03: bull_pts += 2

    cp = smc['candle_patterns']
    bull_cp = sum(p['strength'] for p in cp if p['direction'] == 'BULL')
    bear_cp = sum(p['strength'] for p in cp if p['direction'] == 'BEAR')
    if bull_cp >= 3: bull_pts += 2
    elif bull_cp >= 1: bull_pts += 1
    if bear_cp >= 3: bear_pts += 2
    elif bear_cp >= 1: bear_pts += 1

    liq_data = smc['liquidation']
    if liq_data['liq_bias'] == 'LONG_VULNERABLE':  bear_pts += 1
    elif liq_data['liq_bias'] == 'SHORT_VULNERABLE': bull_pts += 1

    rd = smc['rsi_div']['type']
    rd_map = {'BULLISH_DIV': (3,0), 'BEARISH_DIV': (0,3), 'HIDDEN_BULL': (2,0), 'HIDDEN_BEAR': (0,2)}
    b, be = rd_map.get(rd, (0, 0))
    bull_pts += b; bear_pts += be

    smc['bull_points'] = bull_pts
    smc['bear_points'] = bear_pts

    total = bull_pts + bear_pts
    if total == 0:
        smc['smart_bias'], smc['confidence'] = 'NEUTRAL', 0
    elif bull_pts > bear_pts:
        smc['smart_bias'], smc['confidence'] = 'BULLISH', round(bull_pts / total * 100)
    elif bear_pts > bull_pts:
        smc['smart_bias'], smc['confidence'] = 'BEARISH', round(bear_pts / total * 100)
    else:
        smc['smart_bias'], smc['confidence'] = 'NEUTRAL', 50

    return smc