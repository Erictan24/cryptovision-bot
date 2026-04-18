"""
clean_signal.py — Signal engine baru yang bersih dan fokus.

SATU SETUP: 3 kondisi harus terpenuhi semua.
Tidak ada kompromi. Kalau 1 kondisi tidak terpenuhi → tidak trade.

KONDISI LONG:
  1. Struktur 4H bullish (minimal sideways, tidak downtrend aktif)
  2. Harga pullback ke area Fibonacci 0.5-0.786 dari swing terakhir
  3. Candle konfirmasi di level fib (hammer, engulfing, atau pin bar)

KONDISI SHORT:
  1. Struktur 4H bearish (minimal sideways, tidak uptrend aktif)
  2. Harga bounce ke area Fibonacci 0.5-0.786
  3. Candle konfirmasi (shooting star, bearish engulfing)

BONUS (meningkatkan kualitas ke GOOD):
  + Volume > 1.5x rata-rata saat candle konfirmasi
  + RSI mendukung (oversold untuk LONG, overbought untuk SHORT)
  + 15m BOS searah
"""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# Minimum score untuk generate signal
MIN_CONDITIONS = 3   # semua 3 kondisi harus terpenuhi
BONUS_FOR_GOOD = 2   # minimal 2 bonus untuk upgrade ke GOOD


def generate_clean_signal(df_1h: pd.DataFrame,
                           df_4h: pd.DataFrame,
                           df_15m: pd.DataFrame,
                           price: float,
                           atr: float,
                           symbol: str = '') -> dict:
    """
    Generate signal bersih berdasarkan 3 kondisi ketat.
    Return signal dict atau None.
    """
    if df_1h is None or len(df_1h) < 50:
        return None

    try:
        closes_1h = df_1h['close'].values.astype(float)
        highs_1h  = df_1h['high'].values.astype(float)
        lows_1h   = df_1h['low'].values.astype(float)
        opens_1h  = df_1h['open'].values.astype(float)
        vols_1h   = df_1h['volume'].values.astype(float) if 'volume' in df_1h.columns else None

        # ── Cek kedua arah ──────────────────────────────────────
        for direction in ('LONG', 'SHORT'):
            result = _check_setup(
                direction, closes_1h, highs_1h, lows_1h, opens_1h, vols_1h,
                df_4h, df_15m, price, atr, symbol
            )
            if result:
                return result

    except Exception as e:
        logger.debug(f"clean_signal error {symbol}: {e}")

    return None


def _check_setup(direction, closes, highs, lows, opens, vols,
                 df_4h, df_15m, price, atr, symbol):
    """Cek 3 kondisi + bonus untuk satu arah."""

    conditions_met = 0
    bonus_met      = 0
    reasons        = []
    warnings       = []
    is_long        = direction == 'LONG'

    # ════════════════════════════════════════════════════════
    # KONDISI 1: STRUKTUR 4H
    # ════════════════════════════════════════════════════════
    struct_ok, struct_desc = _check_4h_structure(df_4h, direction, price)
    if struct_ok:
        conditions_met += 1
        reasons.append(f"✅ {struct_desc}")
    else:
        # Struktur berlawanan = hard block
        return None

    # ════════════════════════════════════════════════════════
    # KONDISI 2: FIBONACCI PULLBACK/BOUNCE
    # ════════════════════════════════════════════════════════
    fib_ok, fib_desc, fib_entry, fib_sl, fib_tp1, fib_tp2 = _check_fib_zone(
        closes, highs, lows, price, atr, direction
    )
    if fib_ok:
        conditions_met += 1
        reasons.append(f"✅ {fib_desc}")
    else:
        warnings.append(f"⚪ {fib_desc}")

    # ════════════════════════════════════════════════════════
    # KONDISI 3: CANDLE KONFIRMASI
    # ════════════════════════════════════════════════════════
    candle_ok, candle_desc = _check_candle_confirmation(
        closes, highs, lows, opens, direction, atr
    )
    if candle_ok:
        conditions_met += 1
        reasons.append(f"✅ {candle_desc}")
    else:
        warnings.append(f"⚪ {candle_desc}")

    # Harus 3/3 kondisi terpenuhi
    if conditions_met < MIN_CONDITIONS:
        return None

    # ════════════════════════════════════════════════════════
    # BONUS: Volume, RSI, 15m BOS
    # ════════════════════════════════════════════════════════
    vol_bonus, vol_desc = _check_volume(vols)
    if vol_bonus:
        bonus_met += 1
        reasons.append(f"⭐ {vol_desc}")

    rsi_bonus, rsi_desc = _check_rsi(closes, direction)
    if rsi_bonus:
        bonus_met += 1
        reasons.append(f"⭐ {rsi_desc}")

    bos_bonus, bos_desc = _check_15m_bos(df_15m, direction)
    if bos_bonus:
        bonus_met += 1
        reasons.append(f"⭐ {bos_desc}")

    # Quality berdasarkan bonus
    quality = 'GOOD' if bonus_met >= BONUS_FOR_GOOD else 'LIMIT'

    # Kalau tidak ada fib entry, skip
    if not fib_entry or not fib_sl or not fib_tp1 or not fib_tp2:
        return None

    # Validasi RR minimal 1:1.5
    risk   = abs(fib_entry - fib_sl)
    rr1    = abs(fib_tp1 - fib_entry) / max(risk, 0.000001)
    rr2    = abs(fib_tp2 - fib_entry) / max(risk, 0.000001)

    if rr2 < 1.5:
        return None  # RR terlalu kecil

    score = conditions_met * 20 + bonus_met * 10  # max 100

    ico = '🟢' if is_long else '🔴'
    reasons.insert(0, f"{ico} {direction} setup — {conditions_met}/3 kondisi + {bonus_met} bonus")

    return {
        'direction'       : direction,
        'order_type'      : 'LIMIT' if abs(price - fib_entry) / max(price, 0.000001) > 0.003 else 'MARKET',
        'quality'         : quality,
        'entry'           : round(fib_entry, 8),
        'sl'              : round(fib_sl, 8),
        'tp1'             : round(fib_tp1, 8),
        'tp2'             : round(fib_tp2, 8),
        'rr1'             : round(rr1, 2),
        'rr2'             : round(rr2, 2),
        'rr'              : round(rr2, 2),
        'sl_pct'          : round(risk / max(fib_entry, 0.000001) * 100, 2),
        'confluence_score': score,
        'confidence'      : score,
        'at_zone'         : abs(price - fib_entry) / max(price, 0.000001) <= 0.015,
        'clean_signal'    : True,
        'conditions_met'  : conditions_met,
        'bonus_met'       : bonus_met,
        'reasons'         : reasons + warnings,
        'zone_low'        : min(fib_entry, fib_sl),
        'zone_high'       : max(fib_entry, fib_sl),
        'zone_price'      : fib_entry,
    }


# ════════════════════════════════════════════════════════════
# KONDISI 1: STRUKTUR 4H
# ════════════════════════════════════════════════════════════

def _check_4h_structure(df_4h, direction, price) -> tuple:
    """
    Cek struktur 4H. Harus tidak berlawanan dengan arah trade.
    Return (ok, description)
    """
    if df_4h is None or len(df_4h) < 20:
        return True, "4H data tidak tersedia — skip cek struktur"

    closes = df_4h['close'].values.astype(float)
    highs  = df_4h['high'].values.astype(float)
    lows   = df_4h['low'].values.astype(float)
    opens  = df_4h['open'].values.astype(float)

    # Swing high/low 4H
    window = min(30, len(closes))
    seg_h  = highs[-window:]
    seg_l  = lows[-window:]
    seg_c  = closes[-window:]

    # Cari 2 swing high dan 2 swing low terakhir
    sh = _find_pivot_highs(seg_h)
    sl = _find_pivot_lows(seg_l)

    is_long = direction == 'LONG'

    if len(sh) >= 2 and len(sl) >= 2:
        lh = sh[-1] < sh[-2]   # lower high
        ll = sl[-1] < sl[-2]   # lower low
        hh = sh[-1] > sh[-2]   # higher high
        hl = sl[-1] > sl[-2]   # higher low

        if is_long:
            if lh and ll:
                # Downtrend aktif — blok LONG
                return False, f"4H downtrend (LH+LL) — LONG tidak valid"
            elif hh and hl:
                return True, f"4H uptrend (HH+HL) — struktur mendukung LONG"
            elif hl and not lh:
                return True, f"4H sideways dengan HL — LONG acceptable"
            else:
                return True, f"4H sideways — LONG acceptable dengan konfirmasi"
        else:
            if hh and hl:
                # Uptrend aktif — blok SHORT
                return False, f"4H uptrend (HH+HL) — SHORT tidak valid"
            elif lh and ll:
                return True, f"4H downtrend (LH+LL) — struktur mendukung SHORT"
            elif lh and not ll:
                return True, f"4H sideways dengan LH — SHORT acceptable"
            else:
                return True, f"4H sideways — SHORT acceptable dengan konfirmasi"

    # Fallback: cek EMA saja
    ema20 = _calc_ema_simple(seg_c, min(20, len(seg_c)))
    last_ema = ema20[-1]
    last_c   = seg_c[-1]

    if is_long:
        if last_c > last_ema * 1.01:
            return True, f"4H di atas EMA20 ({last_ema:.4f}) — bias bullish"
        elif last_c < last_ema * 0.98:
            return False, f"4H di bawah EMA20 — bias bearish, skip LONG"
        else:
            return True, f"4H dekat EMA20 — netral"
    else:
        if last_c < last_ema * 0.99:
            return True, f"4H di bawah EMA20 — bias bearish"
        elif last_c > last_ema * 1.02:
            return False, f"4H di atas EMA20 — bias bullish, skip SHORT"
        else:
            return True, f"4H dekat EMA20 — netral"


# ════════════════════════════════════════════════════════════
# KONDISI 2: FIBONACCI ZONE
# ════════════════════════════════════════════════════════════

def _check_fib_zone(closes, highs, lows, price, atr, direction) -> tuple:
    """
    Cek apakah harga di area Fibonacci 0.5-0.786.
    Return (ok, desc, entry, sl, tp1, tp2)
    """
    is_long = direction == 'LONG'
    window  = min(50, len(closes))

    seg_h = highs[-window:]
    seg_l = lows[-window:]

    swing_high = float(np.max(seg_h))
    swing_low  = float(np.min(seg_l))
    rng        = swing_high - swing_low

    if rng <= 0:
        return False, "Range terlalu kecil untuk Fib", None, None, None, None

    # Hitung level Fib
    if is_long:
        # Pullback dari high ke low — entry di area 0.5-0.786 dari retracement
        fib_50  = swing_high - 0.5   * rng
        fib_618 = swing_high - 0.618 * rng
        fib_786 = swing_high - 0.786 * rng

        # Harga harus di antara 0.5 dan 0.786
        in_zone = fib_786 <= price <= fib_50 * 1.01

        if in_zone:
            # Entry di harga sekarang atau fib 0.618 (lebih konservatif)
            entry = max(fib_618, price - atr * 0.1)
            sl    = fib_786 - atr * 0.5   # di bawah fib 0.786
            risk  = abs(entry - sl)
            tp1   = entry + risk * 1.5    # RR 1:1.5
            tp2   = entry + risk * 2.5    # RR 1:2.5

            # Pastikan TP tidak melewati swing high
            tp1 = min(tp1, swing_high * 0.995)
            tp2 = min(tp2, swing_high * 1.01)

            dist = abs(price - fib_618) / max(price, 0.000001) * 100
            desc = f"Fib golden zone 0.5-0.786 ({fib_618:.4f}), dist {dist:.1f}%"
            return True, desc, entry, sl, tp1, tp2

        # Harga menuju zone tapi belum sampai
        elif price > fib_50:
            dist = (price - fib_50) / max(price, 0.000001) * 100
            desc = f"Menuju Fib zone ({fib_50:.4f}), masih {dist:.1f}% jauh"
            return False, desc, fib_618, None, None, None
        else:
            desc = f"Harga di bawah Fib 0.786 ({fib_786:.4f}) — sudah terlalu dalam"
            return False, desc, None, None, None, None

    else:  # SHORT
        # Bounce dari low ke high — entry di area 0.5-0.786
        fib_50  = swing_low + 0.5   * rng
        fib_618 = swing_low + 0.618 * rng
        fib_786 = swing_low + 0.786 * rng

        in_zone = fib_50 * 0.99 <= price <= fib_786

        if in_zone:
            entry = min(fib_618, price + atr * 0.1)
            sl    = fib_786 + atr * 0.5
            risk  = abs(sl - entry)
            tp1   = entry - risk * 1.5
            tp2   = entry - risk * 2.5

            tp1 = max(tp1, swing_low * 1.005)
            tp2 = max(tp2, swing_low * 0.99)

            dist = abs(price - fib_618) / max(price, 0.000001) * 100
            desc = f"Fib golden zone 0.5-0.786 ({fib_618:.4f}), dist {dist:.1f}%"
            return True, desc, entry, sl, tp1, tp2

        elif price < fib_50:
            dist = (fib_50 - price) / max(price, 0.000001) * 100
            desc = f"Menuju Fib zone ({fib_50:.4f}), masih {dist:.1f}% jauh"
            return False, desc, fib_618, None, None, None
        else:
            desc = f"Harga di atas Fib 0.786 ({fib_786:.4f}) — sudah terlalu tinggi"
            return False, desc, None, None, None, None


# ════════════════════════════════════════════════════════════
# KONDISI 3: CANDLE KONFIRMASI
# ════════════════════════════════════════════════════════════

def _check_candle_confirmation(closes, highs, lows, opens, direction, atr) -> tuple:
    """Cek candle konfirmasi di area saat ini."""
    is_long = direction == 'LONG'

    c0 = closes[-1]; o0 = opens[-1]; h0 = highs[-1]; l0 = lows[-1]
    c1 = closes[-2]; o1 = opens[-2]; h1 = highs[-2]; l1 = lows[-2]

    body0  = abs(c0 - o0)
    rng0   = max(h0 - l0, 0.000001)
    upper  = h0 - max(c0, o0)
    lower  = min(c0, o0) - l0

    if is_long:
        # Hammer: lower wick minimal 2x body, upper wick kecil
        if lower >= body0 * 2 and upper <= body0 * 0.5 and rng0 > atr * 0.3:
            return True, f"Hammer — rejection kuat dari bawah (lower wick {lower/rng0*100:.0f}%)"

        # Bullish engulfing
        if c0 > o0 and c1 < o1 and c0 >= o1 and o0 <= c1:
            return True, f"Bullish engulfing — buyer ambil alih"

        # Bullish candle besar (>60% dari ATR)
        if c0 > o0 and body0 >= atr * 0.5 and lower >= upper:
            return True, f"Bullish candle kuat ({body0/atr*100:.0f}% ATR)"

        # Pin bar bullish
        if lower >= rng0 * 0.6 and body0 <= rng0 * 0.3:
            return True, f"Pin bar bullish — rejection kuat"

        return False, "Belum ada candle konfirmasi bullish"

    else:  # SHORT
        # Shooting star: upper wick minimal 2x body
        if upper >= body0 * 2 and lower <= body0 * 0.5 and rng0 > atr * 0.3:
            return True, f"Shooting star — rejection kuat dari atas (upper wick {upper/rng0*100:.0f}%)"

        # Bearish engulfing
        if c0 < o0 and c1 > o1 and c0 <= o1 and o0 >= c1:
            return True, f"Bearish engulfing — seller ambil alih"

        # Bearish candle besar
        if c0 < o0 and body0 >= atr * 0.5 and upper >= lower:
            return True, f"Bearish candle kuat ({body0/atr*100:.0f}% ATR)"

        # Pin bar bearish
        if upper >= rng0 * 0.6 and body0 <= rng0 * 0.3:
            return True, f"Pin bar bearish — rejection kuat"

        return False, "Belum ada candle konfirmasi bearish"


# ════════════════════════════════════════════════════════════
# BONUS CHECKS
# ════════════════════════════════════════════════════════════

def _check_volume(vols) -> tuple:
    if vols is None or len(vols) < 20:
        return False, ""
    avg_vol = np.mean(vols[-20:-1])
    cur_vol = vols[-1]
    ratio   = cur_vol / max(avg_vol, 0.000001)
    if ratio >= 1.5:
        return True, f"Volume {ratio:.1f}x rata-rata — konfirmasi kuat"
    return False, ""


def _check_rsi(closes, direction) -> tuple:
    try:
        rsi = _calc_rsi(closes, 14)[-1]
        if direction == 'LONG' and rsi <= 40:
            return True, f"RSI {rsi:.0f} — area oversold mendukung LONG"
        elif direction == 'SHORT' and rsi >= 60:
            return True, f"RSI {rsi:.0f} — area overbought mendukung SHORT"
    except Exception:
        pass
    return False, ""


def _check_15m_bos(df_15m, direction) -> tuple:
    if df_15m is None or len(df_15m) < 15:
        return False, ""
    try:
        closes = df_15m['close'].values.astype(float)
        highs  = df_15m['high'].values.astype(float)
        lows   = df_15m['low'].values.astype(float)

        if direction == 'LONG':
            prev_high = max(highs[-10:-1])
            bos = closes[-1] > prev_high
            if bos:
                return True, f"15m BOS bullish — konfirmasi timing"
        else:
            prev_low = min(lows[-10:-1])
            bos = closes[-1] < prev_low
            if bos:
                return True, f"15m BOS bearish — konfirmasi timing"
    except Exception:
        pass
    return False, ""


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════

def _find_pivot_highs(arr, lookback=3):
    pivots = []
    for i in range(lookback, len(arr) - lookback):
        if all(arr[i] >= arr[i-j] for j in range(1, lookback+1)) and \
           all(arr[i] >= arr[i+j] for j in range(1, lookback+1)):
            pivots.append(arr[i])
    return pivots


def _find_pivot_lows(arr, lookback=3):
    pivots = []
    for i in range(lookback, len(arr) - lookback):
        if all(arr[i] <= arr[i-j] for j in range(1, lookback+1)) and \
           all(arr[i] <= arr[i+j] for j in range(1, lookback+1)):
            pivots.append(arr[i])
    return pivots


def _calc_ema_simple(arr, period):
    ema = np.zeros(len(arr))
    ema[0] = arr[0]
    k = 2 / (period + 1)
    for i in range(1, len(arr)):
        ema[i] = arr[i] * k + ema[i-1] * (1 - k)
    return ema


def _calc_rsi(closes, period=14):
    delta = np.diff(closes)
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    ag    = np.convolve(gain, np.ones(period)/period, 'valid')
    al    = np.convolve(loss, np.ones(period)/period, 'valid')
    rs    = ag / np.where(al == 0, 0.000001, al)
    rsi   = 100 - (100 / (1 + rs))
    pad   = np.full(len(closes) - len(rsi), 50.0)
    return np.concatenate([pad, rsi])