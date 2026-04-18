"""
chart_pattern_signals.py — Deteksi chart pattern dengan konfirmasi breakout + entry logic.

Referensi:
  - StockCharts ChartSchool
  - VT Markets Chart Patterns Guide 2025
  - ChartingLens Complete Guide 2026
  - BingX Crypto Pattern Guide
  - Alchemy Markets Technical Analysis

PATTERN YANG DIDETEKSI:
  REVERSAL BULLISH:
    Head & Shoulders (inverse)   → breakout neckline ke atas
    Double Bottom (W pattern)    → breakout neckline ke atas
    Triple Bottom                → breakout ke atas
    Falling Wedge                → breakout ke atas
    Cup & Handle                 → breakout rim ke atas

  REVERSAL BEARISH:
    Head & Shoulders             → breakdown neckline ke bawah
    Double Top (M pattern)       → breakdown neckline ke bawah
    Triple Top                   → breakdown ke bawah
    Rising Wedge                 → breakdown ke bawah

  CONTINUATION BULLISH:
    Ascending Triangle           → breakout resistance horizontal
    Bull Flag / Pennant          → breakout ke atas setelah pole
    Rectangle Breakout           → breakout konsolidasi ke atas

  CONTINUATION BEARISH:
    Descending Triangle          → breakdown support horizontal
    Bear Flag / Pennant          → breakdown setelah pole
    Rectangle Breakdown          → breakdown konsolidasi ke bawah

ENTRY LOGIC (sesuai best practice):
  1. Konfirmasi: candle CLOSE di luar batas pattern + volume spike
  2. Entry agresif: langsung setelah breakout candle konfirmasi
  3. Entry konservatif: tunggu pullback ke area breakout level
  4. Pilih berdasarkan RR: kalau RR > 2 dari breakout = agresif, RR < 1.5 = tunggu pullback
  5. SL: di balik batas pattern
  6. TP: measured move = tinggi pattern diproyeksikan dari breakout point
"""

import numpy as np
import logging

logger = logging.getLogger(__name__)

MIN_PATTERN_SCORE = 60   # Score minimum untuk generate signal
MIN_RR_AGGRESSIVE = 2.0  # RR untuk entry agresif langsung
MIN_RR_PULLBACK   = 1.5  # RR minimum untuk entry pullback


def detect_chart_pattern_signal(df_1h, df_4h, price: float,
                                 atr: float, symbol: str = '') -> dict:
    """
    Scan semua chart pattern dan generate signal kalau ada yang terkonfirmasi.

    Return signal dict atau None.
    """
    if df_1h is None or len(df_1h) < 20:
        return None

    closes = df_1h['close'].values.astype(float)
    highs  = df_1h['high'].values.astype(float)
    lows   = df_1h['low'].values.astype(float)
    vols   = df_1h['volume'].values.astype(float) if 'volume' in df_1h.columns else None
    n      = len(closes)

    # Coba semua pattern — ambil yang score tertinggi
    signals = []

    for fn in [
        _ascending_triangle,
        _descending_triangle,
        _bull_flag_pennant,
        _bear_flag_pennant,
        _double_bottom,
        _double_top,
        _triple_bottom,
        _triple_top,
        _inverse_head_shoulders,
        _head_shoulders,
        _falling_wedge,
        _rising_wedge,
        _cup_handle,
        _rectangle_breakout,
    ]:
        try:
            sig = fn(closes, highs, lows, vols, price, atr, n)
            if sig:
                signals.append(sig)
        except Exception as e:
            logger.debug(f"pattern {fn.__name__} error: {e}")

    if not signals:
        return None

    # Pilih signal terbaik
    best = max(signals, key=lambda x: x.get('pattern_score', 0))
    if best.get('pattern_score', 0) < MIN_PATTERN_SCORE:
        return None

    return _build_signal(best, price, atr, vols)


# ═══════════════════════════════════════════════════════════════════
# CONTINUATION PATTERNS
# ═══════════════════════════════════════════════════════════════════

def _ascending_triangle(closes, highs, lows, vols, price, atr, n):
    """
    Ascending Triangle — BULLISH CONTINUATION
    - Resistance horizontal (flat top)
    - Support naik (higher lows)
    - Breakout: close di atas resistance + volume spike

    Reliability: 73% (VT Markets 2025)
    """
    if n < 15:
        return None

    window = min(40, n)
    seg_h  = highs[-window:]
    seg_l  = lows[-window:]
    seg_c  = closes[-window:]
    W      = len(seg_h)

    # Resistance horizontal: top dalam 1% range
    top_prices = sorted(seg_h, reverse=True)[:5]
    resistance = np.mean(top_prices)
    top_spread = (max(top_prices) - min(top_prices)) / max(resistance, 0.001)
    if top_spread > 0.02:  # tidak flat
        return None

    # Support naik: higher lows
    lows_idx = []
    for i in range(2, W-2):
        if seg_l[i] < seg_l[i-1] and seg_l[i] < seg_l[i+1]:
            lows_idx.append((i, seg_l[i]))

    if len(lows_idx) < 2:
        return None

    l1, l2 = lows_idx[-2][1], lows_idx[-1][1]
    if l2 <= l1:  # tidak higher low
        return None

    # Breakout: candle terbaru close di atas resistance
    if seg_c[-1] <= resistance:
        return None

    # Volume konfirmasi
    vol_ok = False
    if vols is not None:
        avg_vol = np.mean(vols[-20:-1])
        vol_ok  = vols[-1] >= avg_vol * 1.3

    # Pattern height untuk measured move
    pattern_height = resistance - min(seg_l[-window:])
    score = 65 + (10 if vol_ok else 0) + (5 if top_spread < 0.01 else 0)

    return {
        'pattern'       : 'Ascending Triangle',
        'direction'     : 'LONG',
        'pattern_score' : score,
        'breakout_level': resistance,
        'pattern_height': pattern_height,
        'sl_anchor'     : min(seg_l[-5:]),  # SL di bawah low terakhir
        'reliability'   : 73,
        'type'          : 'continuation',
        'vol_confirmed' : vol_ok,
        'desc'          : f'Ascending Triangle — resistance {resistance:.4f} ditembus, target +{pattern_height:.4f}',
    }


def _descending_triangle(closes, highs, lows, vols, price, atr, n):
    """
    Descending Triangle — BEARISH CONTINUATION
    Reliability: 72%
    """
    if n < 15:
        return None

    window = min(40, n)
    seg_h  = highs[-window:]
    seg_l  = lows[-window:]
    seg_c  = closes[-window:]
    W      = len(seg_l)

    # Support horizontal (flat bottom)
    bot_prices = sorted(seg_l)[:5]
    support    = np.mean(bot_prices)
    bot_spread = (max(bot_prices) - min(bot_prices)) / max(abs(support), 0.001)
    if bot_spread > 0.02:
        return None

    # Resistance turun (lower highs)
    highs_idx = []
    for i in range(2, W-2):
        if seg_h[i] > seg_h[i-1] and seg_h[i] > seg_h[i+1]:
            highs_idx.append((i, seg_h[i]))

    if len(highs_idx) < 2:
        return None

    h1, h2 = highs_idx[-2][1], highs_idx[-1][1]
    if h2 >= h1:
        return None

    # Breakdown
    if seg_c[-1] >= support:
        return None

    vol_ok = False
    if vols is not None:
        avg_vol = np.mean(vols[-20:-1])
        vol_ok  = vols[-1] >= avg_vol * 1.3

    pattern_height = max(seg_h[-window:]) - support
    score = 63 + (10 if vol_ok else 0)

    return {
        'pattern'       : 'Descending Triangle',
        'direction'     : 'SHORT',
        'pattern_score' : score,
        'breakout_level': support,
        'pattern_height': pattern_height,
        'sl_anchor'     : max(seg_h[-5:]),
        'reliability'   : 72,
        'type'          : 'continuation',
        'vol_confirmed' : vol_ok,
        'desc'          : f'Descending Triangle — support {support:.4f} ditembus, target -{pattern_height:.4f}',
    }


def _bull_flag_pennant(closes, highs, lows, vols, price, atr, n):
    """
    Bull Flag / Pennant — BULLISH CONTINUATION
    Flagpole: naik tajam, Flag: konsolidasi paralel atau menyempit
    Reliability: 80%+ (VT Markets 2025)
    """
    if n < 15:
        return None

    # Cari flagpole (kenaikan tajam dalam 5-15 candle)
    for pole_len in [5, 8, 10, 12]:
        if n < pole_len + 5:
            continue
        pole_start  = closes[-(pole_len+5)]
        pole_end    = max(closes[-(5):-(1)])    # puncak sebelum flag
        pole_rise   = (pole_end - pole_start) / max(pole_start, 0.001)

        if pole_rise < 0.05:  # minimal naik 5%
            continue

        # Flag: konsolidasi setelah pole (5-10 candle terakhir)
        flag_highs = highs[-5:-1]
        flag_lows  = lows[-5:-1]
        flag_range = (max(flag_highs) - min(flag_lows)) / max(pole_end, 0.001)

        if flag_range > 0.04:  # flag terlalu lebar
            continue

        # Flag sedikit turun (valid) atau sideways
        flag_slope = (closes[-2] - closes[-6]) / max(abs(closes[-6]), 0.001)
        if flag_slope > 0.02:  # naik = bukan flag
            continue

        # Breakout: close di atas top of flag
        flag_top = max(flag_highs)
        if closes[-1] <= flag_top:
            continue

        vol_ok = False
        if vols is not None:
            vol_ok = vols[-1] >= np.mean(vols[-20:-1]) * 1.4

        score = 70 + (15 if vol_ok else 0) + (5 if pole_rise >= 0.10 else 0)

        return {
            'pattern'       : 'Bull Flag',
            'direction'     : 'LONG',
            'pattern_score' : score,
            'breakout_level': flag_top,
            'pattern_height': pole_end - pole_start,  # target = panjang flagpole
            'sl_anchor'     : min(flag_lows),
            'reliability'   : 80,
            'type'          : 'continuation',
            'vol_confirmed' : vol_ok,
            'desc'          : f'Bull Flag — pole +{pole_rise*100:.1f}%, breakout {flag_top:.4f}',
        }
    return None


def _bear_flag_pennant(closes, highs, lows, vols, price, atr, n):
    """Bear Flag — BEARISH CONTINUATION. Reliability: 78%"""
    if n < 15:
        return None

    for pole_len in [5, 8, 10, 12]:
        if n < pole_len + 5:
            continue
        pole_start = closes[-(pole_len+5)]
        pole_end   = min(closes[-5:-1])
        pole_drop  = (pole_start - pole_end) / max(pole_start, 0.001)

        if pole_drop < 0.05:
            continue

        flag_highs = highs[-5:-1]
        flag_lows  = lows[-5:-1]
        flag_range = (max(flag_highs) - min(flag_lows)) / max(abs(pole_end), 0.001)

        if flag_range > 0.04:
            continue

        flag_slope = (closes[-2] - closes[-6]) / max(abs(closes[-6]), 0.001)
        if flag_slope < -0.02:
            continue

        flag_bottom = min(flag_lows)
        if closes[-1] >= flag_bottom:
            continue

        vol_ok = False
        if vols is not None:
            vol_ok = vols[-1] >= np.mean(vols[-20:-1]) * 1.4

        score = 68 + (15 if vol_ok else 0) + (5 if pole_drop >= 0.10 else 0)

        return {
            'pattern'       : 'Bear Flag',
            'direction'     : 'SHORT',
            'pattern_score' : score,
            'breakout_level': flag_bottom,
            'pattern_height': pole_start - pole_end,
            'sl_anchor'     : max(flag_highs),
            'reliability'   : 78,
            'type'          : 'continuation',
            'vol_confirmed' : vol_ok,
            'desc'          : f'Bear Flag — pole -{pole_drop*100:.1f}%, breakdown {flag_bottom:.4f}',
        }
    return None


def _rectangle_breakout(closes, highs, lows, vols, price, atr, n):
    """Rectangle Breakout — CONTINUATION arah trend. Reliability: 68%"""
    if n < 20:
        return None

    window = min(30, n)
    seg_h  = highs[-window:-1]
    seg_l  = lows[-window:-1]
    seg_c  = closes[-window:-1]

    rect_high = np.mean(sorted(seg_h, reverse=True)[:4])
    rect_low  = np.mean(sorted(seg_l)[:4])
    rect_range = rect_high - rect_low
    if rect_range <= 0:
        return None

    range_pct = rect_range / max(np.mean(seg_c), 0.001)
    if range_pct > 0.06:  # terlalu lebar, bukan rectangle
        return None

    vol_ok = False
    if vols is not None:
        avg = np.mean(vols[-20:-1])
        vol_ok = vols[-1] >= avg * 1.3

    # Breakout atas (LONG)
    if closes[-1] > rect_high and closes[-2] <= rect_high:
        score = 60 + (10 if vol_ok else 0)
        return {
            'pattern'       : 'Rectangle Breakout',
            'direction'     : 'LONG',
            'pattern_score' : score,
            'breakout_level': rect_high,
            'pattern_height': rect_range,
            'sl_anchor'     : rect_low,
            'reliability'   : 68,
            'type'          : 'continuation',
            'vol_confirmed' : vol_ok,
            'desc'          : f'Rectangle breakout atas {rect_high:.4f}, range {range_pct*100:.1f}%',
        }

    # Breakdown bawah (SHORT)
    if closes[-1] < rect_low and closes[-2] >= rect_low:
        score = 60 + (10 if vol_ok else 0)
        return {
            'pattern'       : 'Rectangle Breakdown',
            'direction'     : 'SHORT',
            'pattern_score' : score,
            'breakout_level': rect_low,
            'pattern_height': rect_range,
            'sl_anchor'     : rect_high,
            'reliability'   : 68,
            'type'          : 'continuation',
            'vol_confirmed' : vol_ok,
            'desc'          : f'Rectangle breakdown bawah {rect_low:.4f}, range {range_pct*100:.1f}%',
        }
    return None


# ═══════════════════════════════════════════════════════════════════
# REVERSAL PATTERNS
# ═══════════════════════════════════════════════════════════════════

def _double_bottom(closes, highs, lows, vols, price, atr, n):
    """
    Double Bottom (W Pattern) — BULLISH REVERSAL
    Dua low hampir sama + breakout neckline
    Reliability: 70%
    """
    if n < 20:
        return None

    window = min(50, n)
    seg_l  = lows[-window:]
    seg_h  = highs[-window:]
    seg_c  = closes[-window:]
    W      = len(seg_l)

    # Cari dua bottom
    troughs = []
    for i in range(3, W-3):
        if (seg_l[i] < seg_l[i-1] and seg_l[i] < seg_l[i-2] and
                seg_l[i] < seg_l[i+1] and seg_l[i] < seg_l[i+2]):
            troughs.append((i, seg_l[i]))

    if len(troughs) < 2:
        return None

    t1, t2 = troughs[-2], troughs[-1]
    bot1, bot2 = t1[1], t2[1]

    # Dua bottom hampir sama (dalam 2%)
    diff_pct = abs(bot1 - bot2) / max(bot1, 0.001)
    if diff_pct > 0.025:
        return None

    # Neckline = high di antara dua bottom
    mid_highs = seg_h[t1[0]:t2[0]+1]
    neckline  = float(np.max(mid_highs))

    # Pullback depth cukup dalam (>3%)
    depth = (neckline - min(bot1, bot2)) / max(neckline, 0.001)
    if depth < 0.03:
        return None

    # Breakout neckline
    if seg_c[-1] <= neckline:
        return None

    vol_ok = False
    if vols is not None:
        vol_ok = vols[-1] >= np.mean(vols[-20:-1]) * 1.3

    pattern_height = neckline - min(bot1, bot2)
    score = 68 + (10 if vol_ok else 0) + (5 if diff_pct < 0.01 else 0)

    return {
        'pattern'       : 'Double Bottom',
        'direction'     : 'LONG',
        'pattern_score' : score,
        'breakout_level': neckline,
        'pattern_height': pattern_height,
        'sl_anchor'     : min(bot1, bot2) - atr * 0.3,
        'reliability'   : 70,
        'type'          : 'reversal',
        'vol_confirmed' : vol_ok,
        'desc'          : f'Double Bottom (W) — neckline {neckline:.4f} ditembus, target +{pattern_height:.4f}',
    }


def _double_top(closes, highs, lows, vols, price, atr, n):
    """Double Top (M Pattern) — BEARISH REVERSAL. Reliability: 65%"""
    if n < 20:
        return None

    window = min(50, n)
    seg_h  = highs[-window:]
    seg_l  = lows[-window:]
    seg_c  = closes[-window:]
    W      = len(seg_h)

    peaks = []
    for i in range(3, W-3):
        if (seg_h[i] > seg_h[i-1] and seg_h[i] > seg_h[i-2] and
                seg_h[i] > seg_h[i+1] and seg_h[i] > seg_h[i+2]):
            peaks.append((i, seg_h[i]))

    if len(peaks) < 2:
        return None

    p1, p2 = peaks[-2], peaks[-1]
    top1, top2 = p1[1], p2[1]

    diff_pct = abs(top1 - top2) / max(top1, 0.001)
    if diff_pct > 0.025:
        return None

    mid_lows  = seg_l[p1[0]:p2[0]+1]
    neckline  = float(np.min(mid_lows))
    depth     = (max(top1, top2) - neckline) / max(max(top1,top2), 0.001)
    if depth < 0.03:
        return None

    if seg_c[-1] >= neckline:
        return None

    vol_ok = False
    if vols is not None:
        vol_ok = vols[-1] >= np.mean(vols[-20:-1]) * 1.3

    pattern_height = max(top1, top2) - neckline
    score = 65 + (10 if vol_ok else 0) + (5 if diff_pct < 0.01 else 0)

    return {
        'pattern'       : 'Double Top',
        'direction'     : 'SHORT',
        'pattern_score' : score,
        'breakout_level': neckline,
        'pattern_height': pattern_height,
        'sl_anchor'     : max(top1, top2) + atr * 0.3,
        'reliability'   : 65,
        'type'          : 'reversal',
        'vol_confirmed' : vol_ok,
        'desc'          : f'Double Top (M) — neckline {neckline:.4f} ditembus, target -{pattern_height:.4f}',
    }


def _triple_bottom(closes, highs, lows, vols, price, atr, n):
    """Triple Bottom — BULLISH REVERSAL. Reliability: 64%"""
    if n < 30:
        return None

    window = min(60, n)
    seg_l  = lows[-window:]
    seg_h  = highs[-window:]
    seg_c  = closes[-window:]
    W      = len(seg_l)

    troughs = []
    for i in range(3, W-3):
        if (seg_l[i] < seg_l[i-1] and seg_l[i] < seg_l[i-2] and
                seg_l[i] < seg_l[i+1] and seg_l[i] < seg_l[i+2]):
            troughs.append((i, seg_l[i]))

    if len(troughs) < 3:
        return None

    t1, t2, t3 = troughs[-3], troughs[-2], troughs[-1]
    b1, b2, b3 = t1[1], t2[1], t3[1]

    # Tiga bottom hampir sama
    avg_bot = (b1 + b2 + b3) / 3
    max_dev = max(abs(b1-avg_bot), abs(b2-avg_bot), abs(b3-avg_bot)) / max(avg_bot, 0.001)
    if max_dev > 0.03:
        return None

    neckline = float(np.max(seg_h[t1[0]:t3[0]+1]))
    if seg_c[-1] <= neckline:
        return None

    vol_ok = False
    if vols is not None:
        vol_ok = vols[-1] >= np.mean(vols[-20:-1]) * 1.3

    pattern_height = neckline - avg_bot
    score = 64 + (10 if vol_ok else 0)

    return {
        'pattern'       : 'Triple Bottom',
        'direction'     : 'LONG',
        'pattern_score' : score,
        'breakout_level': neckline,
        'pattern_height': pattern_height,
        'sl_anchor'     : avg_bot - atr * 0.3,
        'reliability'   : 64,
        'type'          : 'reversal',
        'vol_confirmed' : vol_ok,
        'desc'          : f'Triple Bottom — 3x test support {avg_bot:.4f}, breakout {neckline:.4f}',
    }


def _triple_top(closes, highs, lows, vols, price, atr, n):
    """Triple Top — BEARISH REVERSAL. Reliability: 61%"""
    if n < 30:
        return None

    window = min(60, n)
    seg_h  = highs[-window:]
    seg_l  = lows[-window:]
    seg_c  = closes[-window:]
    W      = len(seg_h)

    peaks = []
    for i in range(3, W-3):
        if (seg_h[i] > seg_h[i-1] and seg_h[i] > seg_h[i-2] and
                seg_h[i] > seg_h[i+1] and seg_h[i] > seg_h[i+2]):
            peaks.append((i, seg_h[i]))

    if len(peaks) < 3:
        return None

    p1, p2, p3 = peaks[-3], peaks[-2], peaks[-1]
    t1, t2, t3 = p1[1], p2[1], p3[1]

    avg_top = (t1+t2+t3)/3
    max_dev = max(abs(t1-avg_top), abs(t2-avg_top), abs(t3-avg_top)) / max(avg_top, 0.001)
    if max_dev > 0.03:
        return None

    neckline = float(np.min(seg_l[p1[0]:p3[0]+1]))
    if seg_c[-1] >= neckline:
        return None

    vol_ok = False
    if vols is not None:
        vol_ok = vols[-1] >= np.mean(vols[-20:-1]) * 1.3

    pattern_height = avg_top - neckline
    score = 61 + (10 if vol_ok else 0)

    return {
        'pattern'       : 'Triple Top',
        'direction'     : 'SHORT',
        'pattern_score' : score,
        'breakout_level': neckline,
        'pattern_height': pattern_height,
        'sl_anchor'     : avg_top + atr * 0.3,
        'reliability'   : 61,
        'type'          : 'reversal',
        'vol_confirmed' : vol_ok,
        'desc'          : f'Triple Top — 3x test resistance {avg_top:.4f}, breakdown {neckline:.4f}',
    }


def _inverse_head_shoulders(closes, highs, lows, vols, price, atr, n):
    """
    Inverse Head & Shoulders — BULLISH REVERSAL
    Tiga lembah: left shoulder, head (terendah), right shoulder
    Reliability: 74%
    """
    if n < 25:
        return None

    window = min(60, n)
    seg_l  = lows[-window:]
    seg_h  = highs[-window:]
    seg_c  = closes[-window:]
    W      = len(seg_l)

    troughs = []
    for i in range(3, W-3):
        if (seg_l[i] < seg_l[i-1] and seg_l[i] < seg_l[i-2] and
                seg_l[i] < seg_l[i+1] and seg_l[i] < seg_l[i+2]):
            troughs.append((i, seg_l[i]))

    if len(troughs) < 3:
        return None

    ls, head, rs = troughs[-3], troughs[-2], troughs[-1]
    ls_l, h_l, rs_l = ls[1], head[1], rs[1]

    # Head harus lebih rendah dari shoulders
    if not (h_l < ls_l and h_l < rs_l):
        return None

    # Shoulders hampir simetris (dalam 5%)
    shoulder_diff = abs(ls_l - rs_l) / max(ls_l, 0.001)
    if shoulder_diff > 0.05:
        return None

    # Head harus cukup lebih rendah (minimal 2%)
    head_depth = (min(ls_l, rs_l) - h_l) / max(min(ls_l, rs_l), 0.001)
    if head_depth < 0.02:
        return None

    # Neckline = rata-rata peak di antara shoulders dan head
    neck1 = float(np.max(seg_h[ls[0]:head[0]+1]))
    neck2 = float(np.max(seg_h[head[0]:rs[0]+1]))
    neckline = (neck1 + neck2) / 2

    # Breakout neckline
    if seg_c[-1] <= neckline:
        return None

    vol_ok = False
    if vols is not None:
        vol_ok = vols[-1] >= np.mean(vols[-20:-1]) * 1.3

    pattern_height = neckline - h_l
    score = 72 + (10 if vol_ok else 0) + (5 if shoulder_diff < 0.02 else 0)

    return {
        'pattern'       : 'Inv Head & Shoulders',
        'direction'     : 'LONG',
        'pattern_score' : score,
        'breakout_level': neckline,
        'pattern_height': pattern_height,
        'sl_anchor'     : rs_l - atr * 0.3,
        'reliability'   : 74,
        'type'          : 'reversal',
        'vol_confirmed' : vol_ok,
        'desc'          : f'Inv H&S — neckline {neckline:.4f} ditembus, target +{pattern_height:.4f}',
    }


def _head_shoulders(closes, highs, lows, vols, price, atr, n):
    """
    Head & Shoulders — BEARISH REVERSAL. Reliability: 70%
    """
    if n < 25:
        return None

    window = min(60, n)
    seg_h  = highs[-window:]
    seg_l  = lows[-window:]
    seg_c  = closes[-window:]
    W      = len(seg_h)

    peaks = []
    for i in range(3, W-3):
        if (seg_h[i] > seg_h[i-1] and seg_h[i] > seg_h[i-2] and
                seg_h[i] > seg_h[i+1] and seg_h[i] > seg_h[i+2]):
            peaks.append((i, seg_h[i]))

    if len(peaks) < 3:
        return None

    ls, head, rs = peaks[-3], peaks[-2], peaks[-1]
    ls_h, h_h, rs_h = ls[1], head[1], rs[1]

    if not (h_h > ls_h and h_h > rs_h):
        return None

    shoulder_diff = abs(ls_h - rs_h) / max(ls_h, 0.001)
    if shoulder_diff > 0.05:
        return None

    head_height = (h_h - max(ls_h, rs_h)) / max(h_h, 0.001)
    if head_height < 0.02:
        return None

    neck1 = float(np.min(seg_l[ls[0]:head[0]+1]))
    neck2 = float(np.min(seg_l[head[0]:rs[0]+1]))
    neckline = (neck1 + neck2) / 2

    if seg_c[-1] >= neckline:
        return None

    vol_ok = False
    if vols is not None:
        vol_ok = vols[-1] >= np.mean(vols[-20:-1]) * 1.3

    pattern_height = h_h - neckline
    score = 70 + (10 if vol_ok else 0) + (5 if shoulder_diff < 0.02 else 0)

    return {
        'pattern'       : 'Head & Shoulders',
        'direction'     : 'SHORT',
        'pattern_score' : score,
        'breakout_level': neckline,
        'pattern_height': pattern_height,
        'sl_anchor'     : rs_h + atr * 0.3,
        'reliability'   : 70,
        'type'          : 'reversal',
        'vol_confirmed' : vol_ok,
        'desc'          : f'H&S — neckline {neckline:.4f} ditembus, target -{pattern_height:.4f}',
    }


def _falling_wedge(closes, highs, lows, vols, price, atr, n):
    """
    Falling Wedge — BULLISH REVERSAL
    Dua garis tren turun, resistance turun lebih curam dari support
    Reliability: 68%
    """
    if n < 15:
        return None

    window = min(40, n)
    seg_h  = highs[-window:]
    seg_l  = lows[-window:]
    seg_c  = closes[-window:]
    W      = len(seg_h)

    # Cari highs dan lows turun
    peak_vals = [seg_h[0], seg_h[W//3], seg_h[W//2], seg_h[-2]]
    low_vals  = [seg_l[0], seg_l[W//3], seg_l[W//2], seg_l[-2]]

    if not (peak_vals[0] > peak_vals[-1] and low_vals[0] > low_vals[-1]):
        return None

    # Range menyempit
    range_start = peak_vals[0] - low_vals[0]
    range_end   = peak_vals[-1] - low_vals[-1]
    if range_end >= range_start * 0.7:  # harus menyempit >30%
        return None

    # Breakout ke atas (close di atas resistance terbaru)
    recent_resistance = peak_vals[-1]
    if seg_c[-1] <= recent_resistance:
        return None

    vol_ok = False
    if vols is not None:
        vol_ok = vols[-1] >= np.mean(vols[-20:-1]) * 1.2

    pattern_height = peak_vals[0] - low_vals[-1]
    score = 65 + (10 if vol_ok else 0)

    return {
        'pattern'       : 'Falling Wedge',
        'direction'     : 'LONG',
        'pattern_score' : score,
        'breakout_level': recent_resistance,
        'pattern_height': pattern_height,
        'sl_anchor'     : low_vals[-1] - atr * 0.3,
        'reliability'   : 68,
        'type'          : 'reversal',
        'vol_confirmed' : vol_ok,
        'desc'          : f'Falling Wedge breakout — target +{pattern_height:.4f}',
    }


def _rising_wedge(closes, highs, lows, vols, price, atr, n):
    """Rising Wedge — BEARISH REVERSAL. Reliability: 65%"""
    if n < 15:
        return None

    window = min(40, n)
    seg_h  = highs[-window:]
    seg_l  = lows[-window:]
    seg_c  = closes[-window:]
    W      = len(seg_h)

    peak_vals = [seg_h[0], seg_h[W//3], seg_h[W//2], seg_h[-2]]
    low_vals  = [seg_l[0], seg_l[W//3], seg_l[W//2], seg_l[-2]]

    if not (peak_vals[-1] > peak_vals[0] and low_vals[-1] > low_vals[0]):
        return None

    range_start = peak_vals[0] - low_vals[0]
    range_end   = peak_vals[-1] - low_vals[-1]
    if range_end >= range_start * 0.7:
        return None

    recent_support = low_vals[-1]
    if seg_c[-1] >= recent_support:
        return None

    vol_ok = False
    if vols is not None:
        vol_ok = vols[-1] >= np.mean(vols[-20:-1]) * 1.2

    pattern_height = peak_vals[-1] - low_vals[0]
    score = 63 + (10 if vol_ok else 0)

    return {
        'pattern'       : 'Rising Wedge',
        'direction'     : 'SHORT',
        'pattern_score' : score,
        'breakout_level': recent_support,
        'pattern_height': pattern_height,
        'sl_anchor'     : peak_vals[-1] + atr * 0.3,
        'reliability'   : 65,
        'type'          : 'reversal',
        'vol_confirmed' : vol_ok,
        'desc'          : f'Rising Wedge breakdown — target -{pattern_height:.4f}',
    }


def _cup_handle(closes, highs, lows, vols, price, atr, n):
    """
    Cup & Handle — BULLISH CONTINUATION
    Cup: rounded bottom, Handle: pullback kecil, Breakout: atas rim
    Reliability: 80%
    """
    if n < 30:
        return None

    window = min(60, n)
    seg_c  = closes[-window:]
    seg_h  = highs[-window:]
    seg_l  = lows[-window:]
    W      = len(seg_c)

    # Rim: level awal cup
    rim_level = seg_c[0]

    # Cup: turun lalu kembali naik (U-shape)
    cup_bottom = float(np.min(seg_l[:W*2//3]))
    cup_depth  = (rim_level - cup_bottom) / max(rim_level, 0.001)

    if cup_depth < 0.05 or cup_depth > 0.50:  # kedalaman 5-50%
        return None

    # Harga harus recovery ke dekat rim
    recovery = (seg_c[W*2//3] - cup_bottom) / max(rim_level - cup_bottom, 0.001)
    if recovery < 0.7:  # belum recovery 70%
        return None

    # Handle: pullback kecil setelah rim (max 1/3 cup depth)
    handle_lows = seg_l[W*2//3:]
    handle_low  = float(np.min(handle_lows))
    handle_depth = (seg_c[W*2//3] - handle_low) / max(rim_level - cup_bottom, 0.001)
    if handle_depth > 0.35:  # handle terlalu dalam
        return None

    # Breakout: close di atas rim
    if seg_c[-1] <= rim_level:
        return None

    vol_ok = False
    if vols is not None:
        vol_ok = vols[-1] >= np.mean(vols[-20:-1]) * 1.4

    pattern_height = rim_level - cup_bottom
    score = 75 + (15 if vol_ok else 0)

    return {
        'pattern'       : 'Cup & Handle',
        'direction'     : 'LONG',
        'pattern_score' : score,
        'breakout_level': rim_level,
        'pattern_height': pattern_height,
        'sl_anchor'     : handle_low - atr * 0.3,
        'reliability'   : 80,
        'type'          : 'continuation',
        'vol_confirmed' : vol_ok,
        'desc'          : f'Cup & Handle — rim {rim_level:.4f} ditembus, cup depth {cup_depth*100:.1f}%',
    }


# ═══════════════════════════════════════════════════════════════════
# BUILD SIGNAL — Entry Logic
# ═══════════════════════════════════════════════════════════════════

def _build_signal(pat: dict, price: float, atr: float, vols) -> dict:
    """
    Build trading signal dari pattern yang terdeteksi.

    Entry Logic (berdasarkan ChartingLens + Tradeciety):
    1. RR dari breakout level > 2.0 → entry agresif (sekarang)
    2. RR dari pullback zone > 1.5 → tunggu pullback ke breakout level
    3. Kalau tidak ada yang feasible → skip
    """
    direction = pat['direction']
    is_long   = direction == 'LONG'
    bl        = pat['breakout_level']   # level breakout/breakdown
    height    = pat['pattern_height']   # measured move target
    sl_anchor = pat['sl_anchor']

    # SL di balik batas pattern
    sl = sl_anchor

    # TP = measured move dari breakout level
    if is_long:
        tp1 = bl + height * 0.5   # 50% measured move
        tp2 = bl + height * 1.0   # 100% measured move
    else:
        tp1 = bl - height * 0.5
        tp2 = bl - height * 1.0

    risk_from_breakout = abs(price - sl)
    if risk_from_breakout <= 0:
        return None

    rr_aggressive = abs(tp2 - price) / risk_from_breakout

    # Pullback entry: entry di breakout level (kalau harga sudah melewati)
    if is_long:
        pullback_entry = bl * 1.002  # 0.2% di atas breakout (tepat setelah retest)
        risk_pullback  = abs(pullback_entry - sl)
    else:
        pullback_entry = bl * 0.998
        risk_pullback  = abs(sl - pullback_entry)

    rr_pullback = abs(tp2 - pullback_entry) / max(risk_pullback, 0.000001)

    # Tentukan entry mode
    near_breakout = abs(price - bl) / max(bl, 0.000001) <= 0.015

    if rr_aggressive >= MIN_RR_AGGRESSIVE:
        # Entry agresif — langsung sekarang
        entry      = price
        order_type = 'MARKET'
        entry_desc = f"Entry agresif @ breakout (RR {rr_aggressive:.1f})"
    elif rr_pullback >= MIN_RR_PULLBACK and not near_breakout:
        # Tunggu pullback ke breakout level
        entry      = pullback_entry
        order_type = 'LIMIT'
        rr_aggressive = rr_pullback
        risk_from_breakout = risk_pullback
        entry_desc = f"Entry pullback @ {pullback_entry:.4f} (RR {rr_pullback:.1f})"
    elif near_breakout and rr_aggressive >= MIN_RR_PULLBACK:
        # Harga dekat breakout — entry sekarang
        entry      = price
        order_type = 'MARKET'
        entry_desc = f"Entry di area breakout (RR {rr_aggressive:.1f})"
    else:
        return None  # RR tidak layak

    risk = abs(entry - sl)
    if risk <= 0:
        return None

    # Hitung TP final dari entry
    if is_long:
        final_tp1 = entry + risk * 1.5
        final_tp2 = bl + height  # measured move dari breakout
    else:
        final_tp1 = entry - risk * 1.5
        final_tp2 = bl - height

    rr1 = abs(final_tp1 - entry) / risk
    rr2 = abs(final_tp2 - entry) / risk

    quality = 'GOOD' if pat['pattern_score'] >= 75 else 'LIMIT'

    ico = '🟢' if is_long else '🔴'
    vol_tag = '📊 Volume konfirmasi ✅' if pat.get('vol_confirmed') else '📊 Volume belum konfirmasi'

    reasons = [
        f"{ico} {pat['pattern']} (~{pat['reliability']}% reliable)",
        pat['desc'],
        vol_tag,
        entry_desc,
        f"SL: {sl:.4f} | TP1: {final_tp1:.4f} | TP2: {final_tp2:.4f}",
        f"RR: 1:{rr2:.1f} | Measured Move: {height:.4f}",
    ]

    return {
        'direction'       : direction,
        'order_type'      : order_type,
        'quality'         : quality,
        'entry'           : round(entry, 8),
        'sl'              : round(sl, 8),
        'tp1'             : round(final_tp1, 8),
        'tp2'             : round(final_tp2, 8),
        'rr1'             : round(rr1, 2),
        'rr2'             : round(rr2, 2),
        'rr'              : round(rr2, 2),
        'sl_pct'          : round(risk / max(entry, 0.000001) * 100, 2),
        'confluence_score': pat['pattern_score'],
        'confidence'      : pat['pattern_score'],
        'at_zone'         : order_type == 'MARKET',
        'chart_pattern'   : True,
        'pattern_name'    : pat['pattern'],
        'pattern_score'   : pat['pattern_score'],
        'zone_low'        : min(entry, sl),
        'zone_high'       : max(entry, sl),
        'zone_price'      : entry,
        'reasons'         : reasons,
    }