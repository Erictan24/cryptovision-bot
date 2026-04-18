"""
chart_patterns.py — Deteksi chart pattern klasik dengan syarat ketat.

Prinsip: lebih baik tidak detect daripada false positive.
Setiap pattern butuh syarat geometri yang jelas dan terukur.
"""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def detect_patterns(df: pd.DataFrame, direction: str, atr: float) -> dict:
    """
    Deteksi pattern relevan. Return block/warning kalau ada pattern berlawanan.
    Threshold tinggi — hanya detect kalau benar-benar jelas.
    """
    result = {'block': False, 'pattern': None, 'confidence': 0, 'reason': '', 'warning': ''}

    if df is None or len(df) < 40:
        return result

    try:
        closes = df['close'].values.astype(float)
        highs  = df['high'].values.astype(float)
        lows   = df['low'].values.astype(float)
        vols   = df['volume'].values.astype(float) if 'volume' in df.columns else None

        bearish = _detect_bearish_strict(closes, highs, lows, vols, atr)
        bullish = _detect_bullish_strict(closes, highs, lows, vols, atr)

        if direction == 'LONG' and bearish['confidence'] >= 70:
            result['block']      = True
            result['pattern']    = bearish['pattern']
            result['confidence'] = bearish['confidence']
            result['reason']     = f"⛔ {bearish['pattern']} ({bearish['confidence']}%): {bearish['desc']} — skip LONG"
        elif direction == 'SHORT' and bullish['confidence'] >= 70:
            result['block']      = True
            result['pattern']    = bullish['pattern']
            result['confidence'] = bullish['confidence']
            result['reason']     = f"⛔ {bullish['pattern']} ({bullish['confidence']}%): {bullish['desc']} — skip SHORT"
        elif direction == 'LONG' and bearish['confidence'] >= 55:
            result['warning'] = f"⚠️ {bearish['pattern']} ({bearish['confidence']}%) — hati-hati LONG"
        elif direction == 'SHORT' and bullish['confidence'] >= 55:
            result['warning'] = f"⚠️ {bullish['pattern']} ({bullish['confidence']}%) — hati-hati SHORT"

    except Exception as e:
        logger.debug(f"Pattern detection error: {e}")

    return result


def _find_peaks(arr, min_prominence_pct=0.02):
    """
    Cari puncak lokal yang signifikan.
    min_prominence_pct: puncak harus lebih tinggi dari sekitarnya minimal X% dari harga.
    """
    peaks = []
    n = len(arr)
    for i in range(3, n - 3):
        if arr[i] > arr[i-1] and arr[i] > arr[i-2] and arr[i] > arr[i-3] and \
           arr[i] > arr[i+1] and arr[i] > arr[i+2] and arr[i] > arr[i+3]:
            # Cek prominence — harus menonjol dari sekitarnya
            left_min  = min(arr[max(0, i-10):i])
            right_min = min(arr[i+1:min(n, i+11)])
            prominence = min(arr[i] - left_min, arr[i] - right_min)
            prom_pct   = prominence / max(arr[i], 0.000001)
            if prom_pct >= min_prominence_pct:
                peaks.append((i, arr[i], prom_pct))
    return peaks


def _find_troughs(arr, min_prominence_pct=0.02):
    """Cari lembah lokal yang signifikan."""
    troughs = []
    n = len(arr)
    for i in range(3, n - 3):
        if arr[i] < arr[i-1] and arr[i] < arr[i-2] and arr[i] < arr[i-3] and \
           arr[i] < arr[i+1] and arr[i] < arr[i+2] and arr[i] < arr[i+3]:
            left_max  = max(arr[max(0, i-10):i])
            right_max = max(arr[i+1:min(n, i+11)])
            prominence = min(left_max - arr[i], right_max - arr[i])
            prom_pct   = prominence / max(left_max, 0.000001)
            if prom_pct >= min_prominence_pct:
                troughs.append((i, arr[i], prom_pct))
    return troughs


def _detect_bearish_strict(closes, highs, lows, vols, atr) -> dict:
    """Deteksi pattern bearish dengan syarat ketat."""
    n = len(closes)
    best = {'pattern': None, 'confidence': 0, 'desc': ''}

    # ── HEAD & SHOULDERS ──────────────────────────────────────
    # Syarat ketat:
    # 1. Head harus 3%+ lebih tinggi dari shoulders
    # 2. Shoulders harus simetris dalam 5%
    # 3. Harga harus sudah breakdown neckline
    # 4. Jarak antar peak harus proporsional
    try:
        peaks = _find_peaks(highs, min_prominence_pct=0.025)
        if len(peaks) >= 3:
            for i in range(len(peaks) - 2):
                p1, p2, p3 = peaks[i], peaks[i+1], peaks[i+2]
                left_h, head_h, right_h = p1[1], p2[1], p3[1]

                # Head harus tertinggi dengan margin yang jelas
                head_above_left  = (head_h - left_h)  / max(left_h, 0.000001)
                head_above_right = (head_h - right_h) / max(right_h, 0.000001)

                if head_above_left < 0.03 or head_above_right < 0.03:
                    continue  # Head tidak cukup tinggi

                # Shoulders harus simetris
                shoulder_diff = abs(left_h - right_h) / max(head_h, 0.000001)
                if shoulder_diff > 0.05:
                    continue  # Terlalu asimetris

                # Spacing antar peak harus proporsional (tidak terlalu rapat)
                left_span  = p2[0] - p1[0]
                right_span = p3[0] - p2[0]
                if left_span < 5 or right_span < 5:
                    continue  # Terlalu rapat

                span_ratio = min(left_span, right_span) / max(left_span, right_span)
                if span_ratio < 0.5:
                    continue  # Terlalu tidak proporsional

                # Neckline
                neck = min(closes[p1[0]:p2[0]+1].min(), closes[p2[0]:p3[0]+1].min())
                current = closes[-1]

                # Harga harus dekat atau sudah breakdown neckline
                near_neckline = current <= neck * 1.03

                if near_neckline:
                    conf = 65
                    if shoulder_diff < 0.03:     conf += 10
                    if head_above_left >= 0.05:  conf += 10
                    if current < neck:           conf += 10  # sudah breakdown
                    if vols is not None and len(vols) > p3[0]:
                        # Volume naik saat breakdown
                        avg_vol = np.mean(vols[max(0, -20):])
                        if vols[-1] > avg_vol * 1.3: conf += 5

                    if conf > best['confidence']:
                        best = {
                            'pattern'   : 'HEAD & SHOULDERS',
                            'confidence': min(conf, 88),
                            'desc'      : f"H={head_h:.4f} neck={neck:.4f} shoulder_diff={shoulder_diff*100:.1f}%",
                        }
    except Exception:
        pass

    # ── DOUBLE TOP ────────────────────────────────────────────
    # Syarat: dua puncak hampir sama (dalam 2%), valley cukup dalam (3%+)
    try:
        peaks = _find_peaks(highs, min_prominence_pct=0.03)
        if len(peaks) >= 2:
            p1, p2 = peaks[-2], peaks[-1]
            diff = abs(p1[1] - p2[1]) / max(p1[1], 0.000001)

            if diff < 0.02:  # ketat: dalam 2%
                valley_arr = closes[p1[0]:p2[0]+1]
                if len(valley_arr) >= 5:
                    valley   = valley_arr.min()
                    drop_pct = (p1[1] - valley) / max(p1[1], 0.000001)

                    if drop_pct >= 0.04:  # valley harus cukup dalam
                        current    = closes[-1]
                        breakdown  = current <= valley * 1.02

                        if breakdown:
                            conf = 65
                            if diff < 0.01:    conf += 15
                            if drop_pct >= 0.07: conf += 10
                            if current < valley: conf += 10

                            if conf > best['confidence']:
                                best = {
                                    'pattern'   : 'DOUBLE TOP',
                                    'confidence': min(conf, 88),
                                    'desc'      : f"Puncak {p1[1]:.4f}/{p2[1]:.4f} diff={diff*100:.1f}% valley={valley:.4f}",
                                }
    except Exception:
        pass

    # ── BEARISH FLAG ──────────────────────────────────────────
    # Syarat: flagpole naik 8%+, konsolidasi 3-15 candle, range sempit <3%
    try:
        window = 35
        seg    = closes[-window:]
        vols_w = vols[-window:] if vols is not None else None

        # Cari flagpole: kenaikan tajam di separuh pertama
        mid    = window // 2
        pole_i = int(np.argmax(seg[:mid]))

        if pole_i >= 4:
            pole_rise = (seg[pole_i] - seg[0]) / max(seg[0], 0.000001)

            if pole_rise >= 0.08:  # Ketat: naik minimal 8%
                flag_seg = seg[pole_i:]
                if 4 <= len(flag_seg) <= 18:
                    flag_range = (max(flag_seg) - min(flag_seg)) / max(seg[pole_i], 0.000001)
                    flag_slope = (flag_seg[-1] - flag_seg[0]) / max(len(flag_seg), 1)
                    flag_slope_pct = flag_slope / max(seg[pole_i], 0.000001)

                    # Flag: range sempit <3%, sedikit turun atau sideways
                    if flag_range < 0.03 and flag_slope_pct <= 0.001:
                        conf = 55
                        if pole_rise >= 0.12:    conf += 15
                        if flag_range < 0.015:   conf += 10
                        if vols_w is not None:
                            vol_pole = np.mean(vols_w[:pole_i+1])
                            vol_flag = np.mean(vols_w[pole_i:])
                            if vol_flag < vol_pole * 0.6: conf += 10  # volume turun di flag

                        if conf > best['confidence']:
                            best = {
                                'pattern'   : 'BEARISH FLAG',
                                'confidence': min(conf, 85),
                                'desc'      : f"Pole +{pole_rise*100:.1f}% flag range {flag_range*100:.1f}%",
                            }
    except Exception:
        pass

    return best


def _detect_bullish_strict(closes, highs, lows, vols, atr) -> dict:
    """Deteksi pattern bullish dengan syarat ketat."""
    n = len(closes)
    best = {'pattern': None, 'confidence': 0, 'desc': ''}

    # ── INVERSE HEAD & SHOULDERS ─────────────────────────────
    try:
        troughs = _find_troughs(lows, min_prominence_pct=0.025)
        if len(troughs) >= 3:
            for i in range(len(troughs) - 2):
                t1, t2, t3 = troughs[i], troughs[i+1], troughs[i+2]
                left_l, head_l, right_l = t1[1], t2[1], t3[1]

                head_below_left  = (left_l - head_l)  / max(left_l, 0.000001)
                head_below_right = (right_l - head_l) / max(right_l, 0.000001)

                if head_below_left < 0.03 or head_below_right < 0.03:
                    continue

                shoulder_diff = abs(left_l - right_l) / max(head_l, 0.000001) if head_l > 0 else 1
                if shoulder_diff > 0.05:
                    continue

                left_span  = t2[0] - t1[0]
                right_span = t3[0] - t2[0]
                if left_span < 5 or right_span < 5:
                    continue

                neck = max(closes[t1[0]:t2[0]+1].max(), closes[t2[0]:t3[0]+1].max())
                current  = closes[-1]
                breakout = current >= neck * 0.97

                if breakout:
                    conf = 65
                    if shoulder_diff < 0.03:       conf += 10
                    if head_below_left >= 0.05:    conf += 10
                    if current > neck:             conf += 10

                    if conf > best['confidence']:
                        best = {
                            'pattern'   : 'INV HEAD & SHOULDERS',
                            'confidence': min(conf, 88),
                            'desc'      : f"H={head_l:.4f} neck={neck:.4f}",
                        }
    except Exception:
        pass

    # ── DOUBLE BOTTOM ─────────────────────────────────────────
    try:
        troughs = _find_troughs(lows, min_prominence_pct=0.03)
        if len(troughs) >= 2:
            t1, t2 = troughs[-2], troughs[-1]
            diff = abs(t1[1] - t2[1]) / max(t1[1], 0.000001)

            if diff < 0.02:
                peak_arr = closes[t1[0]:t2[0]+1]
                if len(peak_arr) >= 5:
                    peak     = peak_arr.max()
                    rise_pct = (peak - t1[1]) / max(t1[1], 0.000001)

                    if rise_pct >= 0.04:
                        current  = closes[-1]
                        breakout = current >= peak * 0.98

                        if breakout:
                            conf = 65
                            if diff < 0.01:      conf += 15
                            if rise_pct >= 0.07: conf += 10
                            if current > peak:   conf += 10

                            if conf > best['confidence']:
                                best = {
                                    'pattern'   : 'DOUBLE BOTTOM',
                                    'confidence': min(conf, 88),
                                    'desc'      : f"Lembah {t1[1]:.4f}/{t2[1]:.4f} diff={diff*100:.1f}%",
                                }
    except Exception:
        pass

    # ── BULLISH FLAG ──────────────────────────────────────────
    try:
        window = 35
        seg    = closes[-window:]
        vols_w = vols[-window:] if vols is not None else None

        mid    = window // 2
        pole_i = int(np.argmin(seg[:mid]))

        if pole_i >= 4:
            pole_drop = (seg[0] - seg[pole_i]) / max(seg[0], 0.000001)

            if pole_drop >= 0.08:
                flag_seg  = seg[pole_i:]
                if 4 <= len(flag_seg) <= 18:
                    flag_range = (max(flag_seg) - min(flag_seg)) / max(abs(seg[pole_i]), 0.000001)
                    flag_slope_pct = (flag_seg[-1] - flag_seg[0]) / max(abs(seg[pole_i]), 0.000001) / max(len(flag_seg), 1)

                    if flag_range < 0.03 and flag_slope_pct >= -0.001:
                        conf = 55
                        if pole_drop >= 0.12:  conf += 15
                        if flag_range < 0.015: conf += 10
                        if vols_w is not None:
                            vol_pole = np.mean(vols_w[:pole_i+1])
                            vol_flag = np.mean(vols_w[pole_i:])
                            if vol_flag < vol_pole * 0.6: conf += 10

                        if conf > best['confidence']:
                            best = {
                                'pattern'   : 'BULLISH FLAG',
                                'confidence': min(conf, 85),
                                'desc'      : f"Pole -{pole_drop*100:.1f}% flag range {flag_range*100:.1f}%",
                            }
    except Exception:
        pass

    return best


def generate_pattern_signal(df: pd.DataFrame, price: float, atr: float, symbol: str = '') -> dict:
    """Generate signal dari pattern yang terkonfirmasi kuat (confidence >= 75%)."""
    if df is None or len(df) < 40:
        return None

    try:
        closes = df['close'].values.astype(float)
        highs  = df['high'].values.astype(float)
        lows   = df['low'].values.astype(float)
        vols   = df['volume'].values.astype(float) if 'volume' in df.columns else None

        bearish = _detect_bearish_strict(closes, highs, lows, vols, atr)
        bullish = _detect_bullish_strict(closes, highs, lows, vols, atr)

        # Minimum confidence 75% untuk generate signal
        best_bear = bearish if bearish['confidence'] >= 75 else None
        best_bull = bullish if bullish['confidence'] >= 75 else None

        chosen    = None
        direction = None

        if best_bear and best_bull:
            if best_bear['confidence'] >= best_bull['confidence']:
                chosen, direction = best_bear, 'SHORT'
            else:
                chosen, direction = best_bull, 'LONG'
        elif best_bear:
            chosen, direction = best_bear, 'SHORT'
        elif best_bull:
            chosen, direction = best_bull, 'LONG'

        if not chosen:
            return None

        # Entry, SL, TP berdasarkan ATR
        if direction == 'SHORT':
            entry = price
            sl    = price + atr * 1.5
            risk  = abs(entry - sl)
            tp1   = entry - risk
            tp2   = entry - risk * 2
        else:
            entry = price
            sl    = price - atr * 1.5
            risk  = abs(entry - sl)
            tp1   = entry + risk
            tp2   = entry + risk * 2

        if risk <= 0:
            return None

        quality = 'GOOD' if chosen['confidence'] >= 82 else 'LIMIT'

        return {
            'direction'       : direction,
            'order_type'      : 'MARKET',
            'quality'         : quality,
            'entry'           : round(entry, 8),
            'sl'              : round(sl, 8),
            'tp1'             : round(tp1, 8),
            'tp2'             : round(tp2, 8),
            'rr1'             : 1.0,
            'rr2'             : 2.0,
            'rr'              : 2.0,
            'sl_pct'          : round(risk / max(entry, 0.000001) * 100, 2),
            'confluence_score': chosen['confidence'],
            'confidence'      : chosen['confidence'],
            'at_zone'         : True,
            'pattern_signal'  : True,
            'pattern_name'    : chosen['pattern'],
            'reasons': [
                f"{'🔴' if direction=='SHORT' else '🟢'} {chosen['pattern']} ({chosen['confidence']}% conf)",
                chosen['desc'],
                f"Entry @ {_fmt(entry)} | SL: {_fmt(sl)}",
                f"TP1: {_fmt(tp1)} | TP2: {_fmt(tp2)} (RR 1:2)",
            ],
            'zone_low'  : min(entry, sl),
            'zone_high' : max(entry, sl),
            'zone_price': entry,
        }

    except Exception as e:
        logger.debug(f"generate_pattern_signal error: {e}")
        return None


def _fmt(p):
    if p >= 10000: return f"{p:,.1f}"
    if p >= 100:   return f"{p:,.2f}"
    if p >= 1:     return f"{p:,.4f}"
    return f"{p:.6f}"