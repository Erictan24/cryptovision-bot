"""
reversal_detector.py — Deteksi reversal signal dengan syarat ketat.

Syarat reversal yang valid:
  1. VOLUME SPIKE — volume candle reversal minimal 2x rata-rata
  2. BREAK OF STRUCTURE — harga break level kunci (support/resistance)
  3. CANDLE KONFIRMASI — pin bar, engulfing, atau rejection kuat di level
  4. MOMENTUM SHIFT — RSI divergence atau extreme oversold/overbought
  5. HTF ALIGNMENT — TF lebih besar setuju dengan arah reversal
  6. BELUM OVEREXTENDED — harga belum terlalu jauh dari mean (EMA)

Semua syarat dievaluasi dengan score.
Minimum 4 dari 6 syarat harus terpenuhi untuk generate reversal signal.
"""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# Score minimum untuk reversal signal
MIN_SCORE_REVERSAL = 4   # dari 6 syarat
MIN_SCORE_GOOD     = 5   # 5/6 syarat → GOOD quality


def detect_reversal(df_1h: pd.DataFrame,
                    df_4h: pd.DataFrame,
                    price: float,
                    atr: float,
                    symbol: str = '') -> dict:
    """
    Deteksi reversal signal dari kondisi market saat ini.

    Returns:
        {
            'signal': dict atau None,
            'score': int,
            'reasons': [...],
            'blocked': [...],
        }
    """
    result = {'signal': None, 'score': 0, 'reasons': [], 'blocked': []}

    if df_1h is None or len(df_1h) < 50:
        return result

    try:
        closes = df_1h['close'].values.astype(float)
        highs  = df_1h['high'].values.astype(float)
        lows   = df_1h['low'].values.astype(float)
        vols   = df_1h['volume'].values.astype(float) if 'volume' in df_1h.columns else None
        opens  = df_1h['open'].values.astype(float)

        # ─── Cek syarat untuk BULLISH REVERSAL (dari downtrend ke uptrend) ───
        bull_score, bull_reasons, bull_blocks = _check_reversal_conditions(
            closes, highs, lows, opens, vols, df_4h, price, atr, 'LONG'
        )

        # ─── Cek syarat untuk BEARISH REVERSAL (dari uptrend ke downtrend) ───
        bear_score, bear_reasons, bear_blocks = _check_reversal_conditions(
            closes, highs, lows, opens, vols, df_4h, price, atr, 'SHORT'
        )

        # Pilih yang paling kuat
        if bull_score >= MIN_SCORE_REVERSAL or bear_score >= MIN_SCORE_REVERSAL:
            if bull_score >= bear_score and bull_score >= MIN_SCORE_REVERSAL:
                direction = 'LONG'
                score     = bull_score
                reasons   = bull_reasons
                blocks    = bull_blocks
            elif bear_score >= MIN_SCORE_REVERSAL:
                direction = 'SHORT'
                score     = bear_score
                reasons   = bear_reasons
                blocks    = bear_blocks
            else:
                return result

            # Generate signal
            if direction == 'LONG':
                sl   = price - atr * 1.8
                tp1  = price + atr * 1.8
                tp2  = price + atr * 3.6
            else:
                sl   = price + atr * 1.8
                tp1  = price - atr * 1.8
                tp2  = price - atr * 3.6

            quality = 'GOOD' if score >= MIN_SCORE_GOOD else 'LIMIT'

            ico = '🟢' if direction == 'LONG' else '🔴'
            result['signal'] = {
                'direction'       : direction,
                'order_type'      : 'MARKET',
                'quality'         : quality,
                'entry'           : round(price, 8),
                'sl'              : round(sl, 8),
                'tp1'             : round(tp1, 8),
                'tp2'             : round(tp2, 8),
                'rr1'             : 1.0,
                'rr2'             : 2.0,
                'rr'              : 2.0,
                'sl_pct'          : round(abs(price - sl) / max(price, 0.000001) * 100, 2),
                'confluence_score': score * 15,  # convert ke skala 0-100
                'confidence'      : score * 15,
                'at_zone'         : True,
                'reversal_signal' : True,
                'reversal_score'  : score,
                'reasons'         : [
                    f"{ico} REVERSAL SIGNAL ({score}/{6} syarat terpenuhi)",
                ] + reasons[:5],
                'zone_low'  : min(price, sl),
                'zone_high' : max(price, sl),
                'zone_price': price,
            }
            result['score']   = score
            result['reasons'] = reasons
            result['blocked'] = blocks

    except Exception as e:
        logger.debug(f"detect_reversal error {symbol}: {e}")

    return result


def _check_reversal_conditions(closes, highs, lows, opens, vols,
                                df_4h, price, atr, direction) -> tuple:
    """
    Cek 6 syarat reversal. Return (score, reasons, blocks).
    """
    score   = 0
    reasons = []
    blocks  = []
    is_long = direction == 'LONG'
    n = len(closes)

    # ═══ SYARAT 1: VOLUME SPIKE ══════════════════════════════
    # Volume candle terbaru (atau 2-3 candle terakhir) harus 2x rata-rata
    if vols is not None and len(vols) >= 20:
        avg_vol   = np.mean(vols[-20:-1])
        cur_vol   = vols[-1]
        vol_ratio = cur_vol / max(avg_vol, 0.000001)

        if vol_ratio >= 2.0:
            score += 1
            reasons.append(f"✅ Volume spike {vol_ratio:.1f}x rata-rata — konfirmasi kuat")
        elif vol_ratio >= 1.5:
            score += 0.5
            reasons.append(f"⚡ Volume {vol_ratio:.1f}x — di atas rata-rata")
        else:
            blocks.append(f"❌ Volume rendah {vol_ratio:.1f}x — reversal lemah")

    # ═══ SYARAT 2: BREAK OF STRUCTURE ═══════════════════════
    # LONG reversal: harga break di atas high 10 candle terakhir
    # SHORT reversal: harga break di bawah low 10 candle terakhir
    lookback = 10
    if is_long:
        prev_high = max(highs[-lookback-1:-1])
        bos       = closes[-1] > prev_high
        if bos:
            score += 1
            reasons.append(f"✅ Break of Structure — close di atas high {lookback} candle ({_fmt(prev_high)})")
        else:
            # Cek apakah sedang di area support yang kuat
            prev_low = min(lows[-lookback-1:-1])
            near_low = price <= prev_low * 1.015
            if near_low:
                score += 0.5
                reasons.append(f"⚡ Harga di area support {_fmt(prev_low)} — reversal zone")
            else:
                blocks.append(f"❌ Belum ada BOS bullish")
    else:
        prev_low = min(lows[-lookback-1:-1])
        bos      = closes[-1] < prev_low
        if bos:
            score += 1
            reasons.append(f"✅ Break of Structure — close di bawah low {lookback} candle ({_fmt(prev_low)})")
        else:
            prev_high = max(highs[-lookback-1:-1])
            near_high = price >= prev_high * 0.985
            if near_high:
                score += 0.5
                reasons.append(f"⚡ Harga di area resistance {_fmt(prev_high)} — reversal zone")
            else:
                blocks.append(f"❌ Belum ada BOS bearish")

    # ═══ SYARAT 3: CANDLE KONFIRMASI ════════════════════════
    c0 = closes[-1]; o0 = opens[-1]; h0 = highs[-1]; l0 = lows[-1]
    c1 = closes[-2]; o1 = opens[-2]

    body0  = abs(c0 - o0)
    range0 = max(h0 - l0, 0.000001)
    upper_wick = h0 - max(c0, o0)
    lower_wick = min(c0, o0) - l0

    candle_confirmed = False

    if is_long:
        # Bullish candle konfirmasi
        if c0 > o0 and c1 < o1 and c0 > o1 and o0 < c1:  # bullish engulfing
            score += 1
            reasons.append("✅ Bullish engulfing — buyer ambil alih")
            candle_confirmed = True
        elif lower_wick >= body0 * 2.5 and upper_wick <= body0 * 0.5:  # hammer
            score += 1
            reasons.append("✅ Hammer — rejection kuat dari bawah")
            candle_confirmed = True
        elif c0 > o0 and (c0 - o0) >= atr * 0.5:  # candle bullish besar
            score += 0.5
            reasons.append("⚡ Candle bullish kuat")
    else:
        # Bearish candle konfirmasi
        if c0 < o0 and c1 > o1 and c0 < o1 and o0 > c1:  # bearish engulfing
            score += 1
            reasons.append("✅ Bearish engulfing — seller ambil alih")
            candle_confirmed = True
        elif upper_wick >= body0 * 2.5 and lower_wick <= body0 * 0.5:  # shooting star
            score += 1
            reasons.append("✅ Shooting star — rejection kuat dari atas")
            candle_confirmed = True
        elif c0 < o0 and (o0 - c0) >= atr * 0.5:  # candle bearish besar
            score += 0.5
            reasons.append("⚡ Candle bearish kuat")

    if not candle_confirmed:
        blocks.append("❌ Belum ada candle konfirmasi reversal")

    # ═══ SYARAT 4: MOMENTUM SHIFT (RSI) ═════════════════════
    try:
        rsi = _calc_rsi(closes, 14)
        rsi_val = rsi[-1]

        if is_long:
            if rsi_val <= 30:
                score += 1
                reasons.append(f"✅ RSI oversold {rsi_val:.0f} — momentum shift bullish")
            elif rsi_val <= 40:
                score += 0.5
                reasons.append(f"⚡ RSI {rsi_val:.0f} — area oversold")
            else:
                # Cek RSI bullish divergence (harga turun tapi RSI naik)
                price_down = closes[-1] < closes[-5]
                rsi_up     = rsi[-1] > rsi[-5]
                if price_down and rsi_up:
                    score += 1
                    reasons.append(f"✅ RSI bullish divergence — momentum membaik")
                else:
                    blocks.append(f"❌ RSI {rsi_val:.0f} — belum oversold untuk reversal LONG")
        else:
            if rsi_val >= 70:
                score += 1
                reasons.append(f"✅ RSI overbought {rsi_val:.0f} — momentum shift bearish")
            elif rsi_val >= 60:
                score += 0.5
                reasons.append(f"⚡ RSI {rsi_val:.0f} — area overbought")
            else:
                price_up = closes[-1] > closes[-5]
                rsi_down = rsi[-1] < rsi[-5]
                if price_up and rsi_down:
                    score += 1
                    reasons.append(f"✅ RSI bearish divergence — momentum melemah")
                else:
                    blocks.append(f"❌ RSI {rsi_val:.0f} — belum overbought untuk reversal SHORT")
    except Exception:
        pass

    # ═══ SYARAT 5: HTF ALIGNMENT ════════════════════════════
    if df_4h is not None and len(df_4h) >= 20:
        try:
            c4h = df_4h['close'].values.astype(float)
            h4h = df_4h['high'].values.astype(float)
            l4h = df_4h['low'].values.astype(float)

            # EMA 20 di 4h
            ema20_4h = _calc_ema(c4h, 20)
            ema_val  = ema20_4h[-1]
            cur_4h   = c4h[-1]

            if is_long:
                # LONG reversal bagus kalau 4h masih di atas EMA 20 atau baru menyentuhnya
                if cur_4h >= ema_val * 0.98:
                    score += 1
                    reasons.append(f"✅ 4H di atas EMA20 ({_fmt(ema_val)}) — bias bullish")
                else:
                    # Cek apakah ada candle reversal di 4H
                    c4h_last = c4h[-1]; o4h_last = df_4h['open'].values[-1]
                    if c4h_last > o4h_last:  # candle 4H bullish
                        score += 0.5
                        reasons.append(f"⚡ Candle 4H bullish meski di bawah EMA")
                    else:
                        blocks.append(f"❌ 4H downtrend — reversal LONG berisiko")
            else:
                if cur_4h <= ema_val * 1.02:
                    score += 1
                    reasons.append(f"✅ 4H di bawah EMA20 ({_fmt(ema_val)}) — bias bearish")
                else:
                    c4h_last = c4h[-1]; o4h_last = df_4h['open'].values[-1]
                    if c4h_last < o4h_last:  # candle 4H bearish
                        score += 0.5
                        reasons.append(f"⚡ Candle 4H bearish meski di atas EMA")
                    else:
                        blocks.append(f"❌ 4H uptrend — reversal SHORT berisiko")
        except Exception:
            pass
    else:
        blocks.append("⚠️ Data 4H tidak tersedia — HTF tidak bisa dikonfirmasi")

    # ═══ SYARAT 6: TIDAK OVEREXTENDED ════════════════════════
    # Harga tidak boleh terlalu jauh dari EMA 20 (> 5% = sudah terlalu jauh, potensi pullback dulu)
    try:
        ema20  = _calc_ema(closes, 20)
        ema_1h = ema20[-1]
        dist   = abs(price - ema_1h) / max(ema_1h, 0.000001)

        if is_long:
            # LONG reversal: harga harus sudah turun jauh tapi belum terlalu jauh
            if 0.02 <= dist <= 0.08 and price < ema_1h:
                score += 1
                reasons.append(f"✅ Harga {dist*100:.1f}% di bawah EMA20 — area reversal ideal")
            elif dist < 0.02:
                score += 0.5
                reasons.append(f"⚡ Harga dekat EMA20 — reversal zone")
            elif dist > 0.08 and price < ema_1h:
                blocks.append(f"❌ Harga {dist*100:.1f}% di bawah EMA — terlalu overextended, tunggu pullback")
        else:
            if 0.02 <= dist <= 0.08 and price > ema_1h:
                score += 1
                reasons.append(f"✅ Harga {dist*100:.1f}% di atas EMA20 — area reversal ideal")
            elif dist < 0.02:
                score += 0.5
                reasons.append(f"⚡ Harga dekat EMA20 — reversal zone")
            elif dist > 0.08 and price > ema_1h:
                blocks.append(f"❌ Harga {dist*100:.1f}% di atas EMA — terlalu overextended")
    except Exception:
        pass

    # Round score ke integer
    score = int(score)
    return score, reasons, blocks


# ─────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────

def _calc_rsi(closes, period=14):
    delta  = np.diff(closes)
    gain   = np.where(delta > 0, delta, 0)
    loss   = np.where(delta < 0, -delta, 0)
    avg_g  = np.convolve(gain, np.ones(period)/period, mode='valid')
    avg_l  = np.convolve(loss, np.ones(period)/period, mode='valid')
    rs     = avg_g / np.where(avg_l == 0, 0.000001, avg_l)
    rsi    = 100 - (100 / (1 + rs))
    # Pad awal agar panjang sama dengan input
    pad    = np.full(len(closes) - len(rsi), 50.0)
    return np.concatenate([pad, rsi])

def _calc_ema(arr, period):
    ema = np.zeros(len(arr))
    ema[0] = arr[0]
    k = 2 / (period + 1)
    for i in range(1, len(arr)):
        ema[i] = arr[i] * k + ema[i-1] * (1 - k)
    return ema

def _fmt(p):
    if p >= 10000: return f"{p:,.1f}"
    if p >= 100:   return f"{p:,.2f}"
    if p >= 1:     return f"{p:,.4f}"
    return f"{p:.6f}"