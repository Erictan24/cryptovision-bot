"""
volume_delta.py — Order flow analysis via volume delta estimation.

Prinsip:
  Setiap candle mendistribusikan volume-nya berdasarkan posisi close
  relatif terhadap range (high-low):

    buy_vol  = (close - low)  / (high - low) * volume   # semakin close ke high = lebih banyak beli
    sell_vol = (high - close) / (high - low) * volume   # semakin close ke low  = lebih banyak jual
    delta    = buy_vol - sell_vol                        # positif = net beli, negatif = net jual

Dipakai untuk:
  1. Konfirmasi entry di S/R: ada delta positif di support = beli nyata, bukan false bounce
  2. Sebagai faktor confluence tambahan di signal scoring
  3. Deteksi divergence: harga naik tapi delta turun = uptrend lemah
"""

import numpy as np


# ── Core calculation ──────────────────────────────────────────────────────────

def calc_candle_delta(df) -> np.ndarray:
    """
    Hitung buy/sell delta untuk setiap candle.
    Return array delta: positif = net beli, negatif = net jual.
    """
    try:
        h = df['high'].values.astype(float)
        l = df['low'].values.astype(float)
        c = df['close'].values.astype(float)
        v = df['volume'].values.astype(float) if 'volume' in df.columns else np.ones(len(df))

        ranges = h - l
        # Hindari division by zero
        ranges = np.where(ranges <= 0, 1e-10, ranges)

        buy_ratio  = (c - l) / ranges   # 0..1 (1 = close di high = semua beli)
        sell_ratio = (h - c) / ranges   # 0..1 (1 = close di low  = semua jual)

        buy_vol  = buy_ratio  * v
        sell_vol = sell_ratio * v
        delta    = buy_vol - sell_vol

        return delta
    except Exception:
        return np.zeros(len(df))


def calc_cumulative_delta(df, n: int = 10) -> float:
    """
    Cumulative delta N candle terakhir (tidak termasuk candle forming).
    Positif = net beli kuat, Negatif = net jual kuat.
    """
    try:
        delta = calc_candle_delta(df)
        # Skip candle terakhir (mungkin masih forming)
        relevant = delta[-n-1:-1] if len(delta) > n else delta[:-1]
        return float(np.sum(relevant))
    except Exception:
        return 0.0


# ── Zone analysis ─────────────────────────────────────────────────────────────

def analyze_delta_at_zone(df, zone_low: float, zone_high: float,
                           direction: str, atr: float) -> dict:
    """
    Analisis volume delta di area zona S/R.

    Untuk LONG di support: kita mau delta POSITIF = beli nyata di zona
    Untuk SHORT di resistance: kita mau delta NEGATIF = jual nyata di zona

    Return:
      bias        : 'STRONG_BUY' | 'BUY' | 'NEUTRAL' | 'SELL' | 'STRONG_SELL'
      score       : -3 .. +3 (confluence score)
      desc        : deskripsi
      zone_vol    : total volume di zona (absolut)
      delta_ratio : delta / total_vol (persentase net beli/jual)
    """
    no_data = {
        'bias': 'NEUTRAL', 'score': 0,
        'desc': 'Data volume tidak tersedia',
        'zone_vol': 0, 'delta_ratio': 0.0
    }

    try:
        if df is None or len(df) < 5 or 'volume' not in df.columns:
            return no_data

        h = df['high'].values.astype(float)
        l = df['low'].values.astype(float)
        c = df['close'].values.astype(float)
        v = df['volume'].values.astype(float)
        delta = calc_candle_delta(df)

        # Zona yang sedikit diperlebar untuk deteksi lebih sensitif
        margin   = atr * 0.3
        zone_ext_low  = zone_low  - margin
        zone_ext_high = zone_high + margin

        # Ambil candle yang menyentuh atau ada di zona (skip candle forming)
        zone_indices = []
        for i in range(len(df) - 1):   # skip candle terakhir (forming)
            if l[i] <= zone_ext_high and h[i] >= zone_ext_low:
                zone_indices.append(i)

        if len(zone_indices) < 2:
            return no_data

        # Gunakan 10 candle terakhir yang menyentuh zona
        recent_zone = zone_indices[-10:]

        zone_delta = float(np.sum(delta[recent_zone]))
        zone_vol   = float(np.sum(v[recent_zone]))

        if zone_vol <= 0:
            return no_data

        delta_ratio = zone_delta / zone_vol  # -1..+1

        # Klasifikasi bias
        if direction == 'LONG':
            # Untuk LONG: delta positif = bagus
            if delta_ratio > 0.35:
                bias  = 'STRONG_BUY'
                score = 3
                desc  = f'Delta beli kuat di support ({delta_ratio:.0%}) — buyer absorbing seller'
            elif delta_ratio > 0.15:
                bias  = 'BUY'
                score = 2
                desc  = f'Delta beli positif di support ({delta_ratio:.0%})'
            elif delta_ratio > -0.10:
                bias  = 'NEUTRAL'
                score = 0
                desc  = f'Delta netral di support ({delta_ratio:.0%})'
            elif delta_ratio > -0.30:
                bias  = 'SELL'
                score = -2
                desc  = f'Delta jual di support ({delta_ratio:.0%}) — seller dominan, LONG berisiko'
            else:
                bias  = 'STRONG_SELL'
                score = -3
                desc  = f'Delta jual sangat kuat ({delta_ratio:.0%}) — jangan LONG, support akan tembus'

        else:  # SHORT
            # Untuk SHORT: delta negatif = bagus
            if delta_ratio < -0.35:
                bias  = 'STRONG_SELL'
                score = 3
                desc  = f'Delta jual kuat di resistance ({delta_ratio:.0%}) — seller absorbing buyer'
            elif delta_ratio < -0.15:
                bias  = 'SELL'
                score = 2
                desc  = f'Delta jual di resistance ({delta_ratio:.0%})'
            elif delta_ratio < 0.10:
                bias  = 'NEUTRAL'
                score = 0
                desc  = f'Delta netral di resistance ({delta_ratio:.0%})'
            elif delta_ratio < 0.30:
                bias  = 'BUY'
                score = -2
                desc  = f'Delta beli di resistance ({delta_ratio:.0%}) — buyer dominan, SHORT berisiko'
            else:
                bias  = 'STRONG_BUY'
                score = -3
                desc  = f'Delta beli sangat kuat ({delta_ratio:.0%}) — jangan SHORT, resistance akan tembus'

        return {
            'bias'       : bias,
            'score'      : score,
            'desc'       : desc,
            'zone_vol'   : round(zone_vol, 2),
            'delta_ratio': round(delta_ratio, 3),
            'candle_count': len(recent_zone),
        }

    except Exception:
        return no_data


# ── Divergence detection ──────────────────────────────────────────────────────

def detect_delta_divergence(df, lookback: int = 20) -> dict:
    """
    Deteksi divergence antara harga dan volume delta.

    Bullish divergence: harga lower low TAPI delta higher (beli meningkat)
    → reversal naik kemungkinan besar

    Bearish divergence: harga higher high TAPI delta lower (jual meningkat)
    → reversal turun kemungkinan besar

    Return:
      type  : 'BULLISH_DIV' | 'BEARISH_DIV' | 'NONE'
      score : 0-3
      desc  : deskripsi
    """
    no_div = {'type': 'NONE', 'score': 0, 'desc': ''}
    try:
        if df is None or len(df) < lookback + 5:
            return no_div

        c     = df['close'].values.astype(float)[-lookback:]
        delta = calc_candle_delta(df)[-lookback:]

        # Hitung moving average delta (5 candle window)
        def smooth(arr, w=5):
            return np.convolve(arr, np.ones(w)/w, mode='valid')

        s_price = smooth(c)
        s_delta = smooth(delta)

        if len(s_price) < 6:
            return no_div

        # Bandingkan paruh pertama vs paruh kedua
        mid = len(s_price) // 2
        price_early = np.mean(s_price[:mid])
        price_late  = np.mean(s_price[mid:])
        delta_early = np.mean(s_delta[:mid])
        delta_late  = np.mean(s_delta[mid:])

        price_change = (price_late - price_early) / max(abs(price_early), 1e-10)
        delta_change = delta_late - delta_early

        # Bullish divergence: harga turun tapi delta naik
        if price_change < -0.01 and delta_change > 0 and delta_late > 0:
            return {
                'type' : 'BULLISH_DIV',
                'score': 2 if delta_change > abs(delta_early) * 0.3 else 1,
                'desc' : f'Bullish delta divergence — harga turun tapi buyer meningkat'
            }
        # Bearish divergence: harga naik tapi delta turun
        if price_change > 0.01 and delta_change < 0 and delta_late < 0:
            return {
                'type' : 'BEARISH_DIV',
                'score': 2 if abs(delta_change) > abs(delta_early) * 0.3 else 1,
                'desc' : f'Bearish delta divergence — harga naik tapi seller meningkat'
            }
        return no_div

    except Exception:
        return no_div


# ── Absorption detection ──────────────────────────────────────────────────────

def detect_absorption(df, direction: str, atr: float) -> dict:
    """
    Deteksi absorption — kondisi di mana satu pihak menyerap tekanan pihak lain.

    Bull absorption: candle bearish besar TAPI delta positif (buyer menyerap penjualan)
    Bear absorption: candle bullish besar TAPI delta negatif (seller menyerap pembelian)

    Ini adalah tanda SANGAT kuat bahwa level akan bertahan.

    Return:
      detected : bool
      strength : 0-3
      desc     : deskripsi
    """
    no_abs = {'detected': False, 'strength': 0, 'desc': ''}
    try:
        if df is None or len(df) < 5 or 'volume' not in df.columns:
            return no_abs

        delta = calc_candle_delta(df)
        o     = df['open'].values.astype(float)
        c     = df['close'].values.astype(float)
        v     = df['volume'].values.astype(float)

        avg_vol = float(np.mean(v[-20:])) if len(v) >= 20 else float(np.mean(v))

        # Scan 3 candle terakhir yang sudah close
        for offset in range(1, 4):
            idx = len(df) - 1 - offset
            if idx < 0:
                break

            body       = abs(c[idx] - o[idx])
            is_bearish = c[idx] < o[idx]
            is_bullish = c[idx] > o[idx]
            d          = delta[idx]
            vol_ratio  = v[idx] / max(avg_vol, 1)

            if direction == 'LONG':
                # Candle bearish besar TAPI delta positif = absorption bullish
                if is_bearish and body > atr * 0.5 and d > 0 and vol_ratio > 1.2:
                    strength = 3 if (d > 0.2 * v[idx] and vol_ratio > 1.5) else 2
                    return {
                        'detected': True, 'strength': strength,
                        'desc': f'Bull absorption — candle bearish tapi buyer dominan (delta +{d:.0f})'
                    }
            else:  # SHORT
                # Candle bullish besar TAPI delta negatif = absorption bearish
                if is_bullish and body > atr * 0.5 and d < 0 and vol_ratio > 1.2:
                    strength = 3 if (abs(d) > 0.2 * v[idx] and vol_ratio > 1.5) else 2
                    return {
                        'detected': True, 'strength': strength,
                        'desc': f'Bear absorption — candle bullish tapi seller dominan (delta {d:.0f})'
                    }

        return no_abs
    except Exception:
        return no_abs
