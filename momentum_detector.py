"""
momentum_detector.py — Deteksi momentum entry yang kuat.

Filosofi: Daripada banyak filter yang memblok,
lebih baik satu sistem yang AKTIF mencari peluang terbaik.

Setup yang dicari:
1. Breakout dengan volume — harga menembus level kunci + volume meledak
2. Pullback ke EMA — trend jelas, pullback ke EMA, bounce
3. Range breakout — konsolidasi panjang, breakout dengan momentum
4. Momentum divergence — harga baru high tapi RSI tidak → reversal
"""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def detect_momentum(df_1h: pd.DataFrame,
                    df_4h: pd.DataFrame,
                    df_15m: pd.DataFrame,
                    price: float,
                    atr: float,
                    symbol: str = '') -> dict:
    """
    Deteksi setup momentum terbaik.
    Return signal dict atau None.
    """
    if df_1h is None or len(df_1h) < 50:
        return None

    results = []

    # Cek semua setup
    for setup_fn in [
        _breakout_volume_setup,
        _ema_pullback_setup,
        _range_breakout_setup,
        _momentum_divergence_setup,
    ]:
        try:
            sig = setup_fn(df_1h, df_4h, df_15m, price, atr, symbol)
            if sig:
                results.append(sig)
        except Exception as e:
            logger.debug(f"momentum setup error: {e}")

    if not results:
        return None

    # Pilih signal dengan score tertinggi
    best = max(results, key=lambda x: x.get('momentum_score', 0))
    if best.get('momentum_score', 0) < 60:
        return None

    # Enrich dengan candle pattern
    if _CP and df_1h is not None and len(df_1h) >= 3:
        try:
            cp = get_candle_signal(
                df_1h['open'].values[-5:],
                df_1h['high'].values[-5:],
                df_1h['low'].values[-5:],
                df_1h['close'].values[-5:],
                atr,
                best.get('direction', '')
            )
            if cp.get('found') and cp['strength'] >= 1:
                candle_str = format_candle_signal(cp)
                if candle_str:
                    best['reasons'].insert(0, candle_str)
                # Bonus score kalau searah
                if cp['direction'] == best.get('direction') and cp['strength'] >= 2:
                    best['momentum_score'] = min(best['momentum_score'] + 10, 100)
                # Blok kalau berlawanan dan kuat
                elif cp['direction'] not in (best.get('direction'), 'NEUTRAL') and cp['strength'] >= 2:
                    logger.debug(f"Momentum diblok candle pattern {cp['pattern']}")
                    return None
        except Exception:
            pass

    return best


def _breakout_volume_setup(df_1h, df_4h, df_15m, price, atr, symbol) -> dict:
    """
    Setup 1: Breakout level kunci dengan volume spike.

    Kondisi LONG:
    - Harga break di atas resistance yang sudah dites 2+ kali
    - Volume candle breakout 2x+ rata-rata
    - Candle close di atas resistance (bukan sekedar wick)

    Kondisi SHORT: kebalikannya
    """
    closes = df_1h['close'].values.astype(float)
    highs  = df_1h['high'].values.astype(float)
    lows   = df_1h['low'].values.astype(float)
    vols   = df_1h['volume'].values.astype(float) if 'volume' in df_1h.columns else None
    n      = len(closes)

    if vols is None:
        return None

    score = 0

    # Cari level resistance/support yang pernah dites
    window = min(50, n)
    seg_h  = highs[-window:]
    seg_l  = lows[-window:]
    seg_c  = closes[-window:]
    seg_v  = vols[-window:]

    # Cluster resistance (level yang disentuh 2+ kali)
    resistance_levels = []
    support_levels    = []
    tolerance         = atr * 0.5

    for i in range(len(seg_h) - 5):
        level = seg_h[i]
        touches = sum(1 for j in range(len(seg_h))
                      if abs(seg_h[j] - level) < tolerance)
        if touches >= 2:
            resistance_levels.append(level)

    for i in range(len(seg_l) - 5):
        level = seg_l[i]
        touches = sum(1 for j in range(len(seg_l))
                      if abs(seg_l[j] - level) < tolerance)
        if touches >= 2:
            support_levels.append(level)

    avg_vol = np.mean(seg_v[-20:-1])
    cur_vol = vols[-1]
    vol_ratio = cur_vol / max(avg_vol, 0.001)

    # Cek LONG breakout
    for res in resistance_levels:
        if (closes[-1] > res and          # close di atas resistance
            closes[-2] <= res and          # candle sebelumnya masih di bawah
            vol_ratio >= 1.8):             # volume spike
            score = 70 + min(vol_ratio * 5, 20)
            entry = closes[-1]
            sl    = res - atr * 0.5        # SL di bawah resistance yang baru jadi support
            risk  = abs(entry - sl)
            tp1   = entry + risk * 1.5
            tp2   = entry + risk * 2.5

            return {
                'direction'     : 'LONG',
                'order_type'    : 'MARKET',
                'quality'       : 'GOOD' if score >= 80 else 'LIMIT',
                'entry'         : round(entry, 8),
                'sl'            : round(sl, 8),
                'tp1'           : round(tp1, 8),
                'tp2'           : round(tp2, 8),
                'rr1'           : 1.5, 'rr2': 2.5, 'rr': 2.5,
                'sl_pct'        : round(risk/max(entry,0.001)*100, 2),
                'momentum_score': score,
                'confluence_score': score,
                'at_zone'       : True,
                'momentum_setup': 'Breakout Volume',
                'reasons'       : [
                    f"🚀 BREAKOUT: Close {entry:.4f} di atas resistance {res:.4f}",
                    f"📊 Volume {vol_ratio:.1f}x rata-rata — konfirmasi kuat",
                    f"SL: {sl:.4f} | TP1: {tp1:.4f} | TP2: {tp2:.4f}",
                ],
                'zone_low'  : sl,
                'zone_high' : entry,
                'zone_price': entry,
            }

    # Cek SHORT breakdown
    for sup in support_levels:
        if (closes[-1] < sup and
            closes[-2] >= sup and
            vol_ratio >= 1.8):
            score = 70 + min(vol_ratio * 5, 20)
            entry = closes[-1]
            sl    = sup + atr * 0.5
            risk  = abs(sl - entry)
            tp1   = entry - risk * 1.5
            tp2   = entry - risk * 2.5

            return {
                'direction'     : 'SHORT',
                'order_type'    : 'MARKET',
                'quality'       : 'GOOD' if score >= 80 else 'LIMIT',
                'entry'         : round(entry, 8),
                'sl'            : round(sl, 8),
                'tp1'           : round(tp1, 8),
                'tp2'           : round(tp2, 8),
                'rr1'           : 1.5, 'rr2': 2.5, 'rr': 2.5,
                'sl_pct'        : round(risk/max(entry,0.001)*100, 2),
                'momentum_score': score,
                'confluence_score': score,
                'at_zone'       : True,
                'momentum_setup': 'Breakdown Volume',
                'reasons'       : [
                    f"📉 BREAKDOWN: Close {entry:.4f} di bawah support {sup:.4f}",
                    f"📊 Volume {vol_ratio:.1f}x rata-rata",
                    f"SL: {sl:.4f} | TP1: {tp1:.4f} | TP2: {tp2:.4f}",
                ],
                'zone_low'  : entry,
                'zone_high' : sl,
                'zone_price': entry,
            }

    return None


def _ema_pullback_setup(df_1h, df_4h, df_15m, price, atr, symbol) -> dict:
    """
    Setup 2: Trend jelas + pullback ke EMA 21 + bounce.

    LONG:
    - Trend 4H bullish (harga di atas EMA 50 4H)
    - 1H pullback ke EMA 21 (dalam 1% dari EMA)
    - Candle terbaru bounce (close > open, lower wick panjang)

    SHORT: kebalikannya
    """
    closes = df_1h['close'].values.astype(float)
    highs  = df_1h['high'].values.astype(float)
    lows   = df_1h['low'].values.astype(float)
    opens  = df_1h['open'].values.astype(float)
    n      = len(closes)

    # Hitung EMA 21 1H
    ema21 = closes[0]
    for v in closes:
        ema21 = v * (2/22) + ema21 * (20/22)

    # Hitung EMA 50 1H
    ema50 = closes[0]
    for v in closes:
        ema50 = v * (2/51) + ema50 * (49/51)

    # Cek trend 4H
    trend_bullish = trend_bearish = False
    if df_4h is not None and len(df_4h) >= 20:
        c4 = df_4h['close'].values.astype(float)
        ema50_4h = c4[0]
        for v in c4:
            ema50_4h = v * (2/51) + ema50_4h * (49/51)
        trend_bullish = c4[-1] > ema50_4h * 1.02
        trend_bearish = c4[-1] < ema50_4h * 0.98

    c0 = closes[-1]; o0 = opens[-1]; h0 = highs[-1]; l0 = lows[-1]
    body   = abs(c0 - o0)
    lower  = min(c0, o0) - l0
    upper  = h0 - max(c0, o0)

    # LONG setup
    if (trend_bullish and
            abs(price - ema21) / max(ema21, 0.001) < 0.015 and  # dalam 1.5% dari EMA21
            price > ema50 and                                      # di atas EMA50
            c0 > o0 and                                            # candle hijau
            lower >= body * 1.5):                                  # ada lower wick

        score = 65
        if lower >= body * 2.5: score += 10  # hammer kuat
        if trend_bullish: score += 10

        entry = closes[-1]
        sl    = l0 - atr * 0.3
        risk  = abs(entry - sl)
        if risk <= 0: return None
        tp1   = entry + risk * 1.5
        tp2   = entry + risk * 3.0  # trend following = TP lebih jauh

        return {
            'direction'     : 'LONG',
            'order_type'    : 'MARKET',
            'quality'       : 'GOOD' if score >= 75 else 'LIMIT',
            'entry'         : round(entry, 8),
            'sl'            : round(sl, 8),
            'tp1'           : round(tp1, 8),
            'tp2'           : round(tp2, 8),
            'rr1'           : 1.5, 'rr2': 3.0, 'rr': 3.0,
            'sl_pct'        : round(risk/max(entry,0.001)*100, 2),
            'momentum_score': score,
            'confluence_score': score,
            'at_zone'       : True,
            'momentum_setup': 'EMA Pullback',
            'reasons'       : [
                f"📈 PULLBACK ke EMA21 ({ema21:.4f}) dalam uptrend 4H",
                f"🔨 Bounce candle — lower wick {lower/max(body,0.001):.1f}x body",
                f"SL: {sl:.4f} | TP1: {tp1:.4f} | TP2: {tp2:.4f} (RR 1:3)",
            ],
            'zone_low'  : sl,
            'zone_high' : entry,
            'zone_price': entry,
        }

    # SHORT setup
    if (trend_bearish and
            abs(price - ema21) / max(ema21, 0.001) < 0.015 and
            price < ema50 and
            c0 < o0 and
            upper >= body * 1.5):

        score = 65
        if upper >= body * 2.5: score += 10
        if trend_bearish: score += 10

        entry = closes[-1]
        sl    = h0 + atr * 0.3
        risk  = abs(sl - entry)
        if risk <= 0: return None
        tp1   = entry - risk * 1.5
        tp2   = entry - risk * 3.0

        return {
            'direction'     : 'SHORT',
            'order_type'    : 'MARKET',
            'quality'       : 'GOOD' if score >= 75 else 'LIMIT',
            'entry'         : round(entry, 8),
            'sl'            : round(sl, 8),
            'tp1'           : round(tp1, 8),
            'tp2'           : round(tp2, 8),
            'rr1'           : 1.5, 'rr2': 3.0, 'rr': 3.0,
            'sl_pct'        : round(risk/max(entry,0.001)*100, 2),
            'momentum_score': score,
            'confluence_score': score,
            'at_zone'       : True,
            'momentum_setup': 'EMA Pullback',
            'reasons'       : [
                f"📉 PULLBACK ke EMA21 ({ema21:.4f}) dalam downtrend 4H",
                f"⭐ Rejection candle — upper wick {upper/max(body,0.001):.1f}x body",
                f"SL: {sl:.4f} | TP1: {tp1:.4f} | TP2: {tp2:.4f} (RR 1:3)",
            ],
            'zone_low'  : entry,
            'zone_high' : sl,
            'zone_price': entry,
        }

    return None


def _range_breakout_setup(df_1h, df_4h, df_15m, price, atr, symbol) -> dict:
    """
    Setup 3: Konsolidasi panjang + breakout.
    Range sempit 10+ candle → breakout dengan volume.
    """
    closes = df_1h['close'].values.astype(float)
    highs  = df_1h['high'].values.astype(float)
    lows   = df_1h['low'].values.astype(float)
    vols   = df_1h['volume'].values.astype(float) if 'volume' in df_1h.columns else None
    n      = len(closes)

    if vols is None or n < 20:
        return None

    # Cek konsolidasi 10-25 candle terakhir (tidak termasuk candle terbaru)
    for range_len in [10, 15, 20]:
        if n < range_len + 2:
            continue

        seg_c = closes[-(range_len+1):-1]
        seg_h = highs[-(range_len+1):-1]
        seg_l = lows[-(range_len+1):-1]
        seg_v = vols[-(range_len+1):-1]

        range_high = float(np.max(seg_h))
        range_low  = float(np.min(seg_l))
        range_size = range_high - range_low
        range_pct  = range_size / max(np.mean(seg_c), 0.001) * 100

        # Konsolidasi: range sempit (< 4% dari harga)
        if range_pct > 4.0:
            continue

        avg_vol  = np.mean(seg_v)
        cur_vol  = vols[-1]
        vol_ratio = cur_vol / max(avg_vol, 0.001)

        # Breakout LONG
        if (closes[-1] > range_high and
                closes[-2] <= range_high and
                vol_ratio >= 1.5):
            score = 60 + min(range_len * 2, 20) + min(vol_ratio * 5, 15)
            entry = closes[-1]
            sl    = range_low
            risk  = abs(entry - sl)
            if risk <= 0: continue
            tp1 = entry + range_size * 1.0  # TP = lebar range
            tp2 = entry + range_size * 2.0

            if abs(tp2 - entry) / max(risk, 0.001) < 1.5:
                continue

            return {
                'direction'     : 'LONG',
                'order_type'    : 'MARKET',
                'quality'       : 'GOOD' if score >= 75 else 'LIMIT',
                'entry'         : round(entry, 8),
                'sl'            : round(sl, 8),
                'tp1'           : round(tp1, 8),
                'tp2'           : round(tp2, 8),
                'rr1'           : round(abs(tp1-entry)/max(risk,0.001), 2),
                'rr2'           : round(abs(tp2-entry)/max(risk,0.001), 2),
                'rr'            : round(abs(tp2-entry)/max(risk,0.001), 2),
                'sl_pct'        : round(risk/max(entry,0.001)*100, 2),
                'momentum_score': score,
                'confluence_score': score,
                'at_zone'       : True,
                'momentum_setup': f'Range Breakout ({range_len}c)',
                'reasons'       : [
                    f"📦 RANGE BREAKOUT: {range_len} candle konsolidasi {range_pct:.1f}%",
                    f"⬆️ Breakout di atas {range_high:.4f} | Volume {vol_ratio:.1f}x",
                    f"SL: {sl:.4f} | TP1: {tp1:.4f} | TP2: {tp2:.4f}",
                ],
                'zone_low'  : sl, 'zone_high': entry, 'zone_price': entry,
            }

        # Breakdown SHORT
        if (closes[-1] < range_low and
                closes[-2] >= range_low and
                vol_ratio >= 1.5):
            score = 60 + min(range_len * 2, 20) + min(vol_ratio * 5, 15)
            entry = closes[-1]
            sl    = range_high
            risk  = abs(sl - entry)
            if risk <= 0: continue
            tp1 = entry - range_size * 1.0
            tp2 = entry - range_size * 2.0

            if abs(tp2 - entry) / max(risk, 0.001) < 1.5:
                continue

            return {
                'direction'     : 'SHORT',
                'order_type'    : 'MARKET',
                'quality'       : 'GOOD' if score >= 75 else 'LIMIT',
                'entry'         : round(entry, 8),
                'sl'            : round(sl, 8),
                'tp1'           : round(tp1, 8),
                'tp2'           : round(tp2, 8),
                'rr1'           : round(abs(tp1-entry)/max(risk,0.001), 2),
                'rr2'           : round(abs(tp2-entry)/max(risk,0.001), 2),
                'rr'            : round(abs(tp2-entry)/max(risk,0.001), 2),
                'sl_pct'        : round(risk/max(entry,0.001)*100, 2),
                'momentum_score': score,
                'confluence_score': score,
                'at_zone'       : True,
                'momentum_setup': f'Range Breakdown ({range_len}c)',
                'reasons'       : [
                    f"📦 RANGE BREAKDOWN: {range_len} candle konsolidasi {range_pct:.1f}%",
                    f"⬇️ Breakdown di bawah {range_low:.4f} | Volume {vol_ratio:.1f}x",
                    f"SL: {sl:.4f} | TP1: {tp1:.4f} | TP2: {tp2:.4f}",
                ],
                'zone_low'  : entry, 'zone_high': sl, 'zone_price': entry,
            }

    return None


def _momentum_divergence_setup(df_1h, df_4h, df_15m, price, atr, symbol) -> dict:
    """
    Setup 4: RSI Divergence.

    Bullish divergence: harga buat lower low, RSI buat higher low → LONG
    Bearish divergence: harga buat higher high, RSI buat lower high → SHORT
    """
    closes = df_1h['close'].values.astype(float)
    highs  = df_1h['high'].values.astype(float)
    lows   = df_1h['low'].values.astype(float)
    opens  = df_1h['open'].values.astype(float)
    n      = len(closes)

    if n < 30:
        return None

    # Hitung RSI
    delta  = np.diff(closes)
    gain   = np.where(delta > 0, delta, 0)
    loss   = np.where(delta < 0, -delta, 0)
    ag     = np.convolve(gain, np.ones(14)/14, 'valid')
    al     = np.convolve(loss, np.ones(14)/14, 'valid')
    rsi    = 100 - 100/(1 + ag/np.where(al==0, 0.001, al))
    pad    = np.full(n - len(rsi), 50.0)
    rsi    = np.concatenate([pad, rsi])

    # Cek 20 candle terakhir untuk divergence
    window = 20
    seg_l  = lows[-window:]
    seg_h  = highs[-window:]
    seg_r  = rsi[-window:]
    seg_c  = closes[-window:]

    # Bullish divergence: price LL tapi RSI HL
    min1_idx = int(np.argmin(seg_l))
    if min1_idx > 3:
        # Cari low sebelumnya
        prev_seg_l = seg_l[:min1_idx]
        min2_idx   = int(np.argmin(prev_seg_l))

        price_ll = seg_l[min1_idx] < seg_l[min2_idx]    # price lower low
        rsi_hl   = seg_r[min1_idx] > seg_r[min2_idx]    # RSI higher low
        rsi_oversold = seg_r[-1] < 45                     # RSI masih rendah
        recent   = min1_idx >= window - 4                 # terjadi baru-baru ini

        if price_ll and rsi_hl and rsi_oversold and recent:
            score = 68
            c0 = closes[-1]; o0 = opens[-1]; l0 = lows[-1]
            body  = abs(c0 - o0)
            lower = min(c0, o0) - l0
            if c0 > o0 and lower >= body: score += 10  # konfirmasi candle

            entry = closes[-1]
            sl    = seg_l[min1_idx] - atr * 0.3
            risk  = abs(entry - sl)
            if risk <= 0: return None
            tp1   = entry + risk * 1.5
            tp2   = entry + risk * 2.5

            return {
                'direction'     : 'LONG',
                'order_type'    : 'MARKET',
                'quality'       : 'GOOD' if score >= 75 else 'LIMIT',
                'entry'         : round(entry, 8),
                'sl'            : round(sl, 8),
                'tp1'           : round(tp1, 8),
                'tp2'           : round(tp2, 8),
                'rr1'           : 1.5, 'rr2': 2.5, 'rr': 2.5,
                'sl_pct'        : round(risk/max(entry,0.001)*100, 2),
                'momentum_score': score,
                'confluence_score': score,
                'at_zone'       : True,
                'momentum_setup': 'RSI Bullish Divergence',
                'reasons'       : [
                    f"📊 BULLISH DIVERGENCE: Price LL tapi RSI HL",
                    f"RSI sekarang: {seg_r[-1]:.0f} (oversold area)",
                    f"SL: {sl:.4f} | TP1: {tp1:.4f} | TP2: {tp2:.4f}",
                ],
                'zone_low'  : sl, 'zone_high': entry, 'zone_price': entry,
            }

    # Bearish divergence: price HH tapi RSI LH
    max1_idx = int(np.argmax(seg_h))
    if max1_idx > 3:
        prev_seg_h = seg_h[:max1_idx]
        max2_idx   = int(np.argmax(prev_seg_h))

        price_hh = seg_h[max1_idx] > seg_h[max2_idx]
        rsi_lh   = seg_r[max1_idx] < seg_r[max2_idx]
        rsi_ob   = seg_r[-1] > 55
        recent   = max1_idx >= window - 4

        if price_hh and rsi_lh and rsi_ob and recent:
            score = 68
            c0 = closes[-1]; o0 = opens[-1]; h0 = highs[-1]
            body  = abs(c0 - o0)
            upper = h0 - max(c0, o0)
            if c0 < o0 and upper >= body: score += 10

            entry = closes[-1]
            sl    = seg_h[max1_idx] + atr * 0.3
            risk  = abs(sl - entry)
            if risk <= 0: return None
            tp1   = entry - risk * 1.5
            tp2   = entry - risk * 2.5

            return {
                'direction'     : 'SHORT',
                'order_type'    : 'MARKET',
                'quality'       : 'GOOD' if score >= 75 else 'LIMIT',
                'entry'         : round(entry, 8),
                'sl'            : round(sl, 8),
                'tp1'           : round(tp1, 8),
                'tp2'           : round(tp2, 8),
                'rr1'           : 1.5, 'rr2': 2.5, 'rr': 2.5,
                'sl_pct'        : round(risk/max(entry,0.001)*100, 2),
                'momentum_score': score,
                'confluence_score': score,
                'at_zone'       : True,
                'momentum_setup': 'RSI Bearish Divergence',
                'reasons'       : [
                    f"📊 BEARISH DIVERGENCE: Price HH tapi RSI LH",
                    f"RSI sekarang: {seg_r[-1]:.0f} (overbought area)",
                    f"SL: {sl:.4f} | TP1: {tp1:.4f} | TP2: {tp2:.4f}",
                ],
                'zone_low'  : entry, 'zone_high': sl, 'zone_price': entry,
            }

    return None