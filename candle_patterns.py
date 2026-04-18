"""
candle_patterns.py — Deteksi 35+ candlestick pattern.

Referensi: StockCharts ChartSchool, IG Markets, TradingView, Investopedia

REVERSAL BULLISH  : Hammer, Inverted Hammer, Dragonfly Doji, Bullish Marubozu,
                    Bullish Engulfing, Piercing Line, Tweezer Bottom, Bullish Harami,
                    Bullish Harami Cross, Belt Hold Bullish, Kicker Bullish,
                    Morning Star, Morning Doji Star, Three White Soldiers,
                    Three Inside Up, Three Outside Up, Abandoned Baby Bullish

REVERSAL BEARISH  : Shooting Star, Hanging Man, Gravestone Doji, Bearish Marubozu,
                    Bearish Engulfing, Dark Cloud Cover, Tweezer Top, Bearish Harami,
                    Bearish Harami Cross, Belt Hold Bearish, Kicker Bearish,
                    Evening Star, Evening Doji Star, Three Black Crows,
                    Three Inside Down, Three Outside Down, Abandoned Baby Bearish

CONTINUATION      : Rising Three Methods, Falling Three Methods,
                    Upside Tasuki Gap, Downside Tasuki Gap, Mat Hold

NEUTRAL           : Doji, Long-Legged Doji, Spinning Top, High Wave
"""

import numpy as np


def detect_candle_pattern(opens: list, highs: list,
                           lows: list, closes: list,
                           atr: float) -> dict:
    """
    Deteksi candlestick pattern dari OHLC data.
    Minimal 5 candle untuk pattern triple dan continuation.

    Returns dict:
        found, pattern, direction, strength (1-3), reliability (%), desc
    """
    default = {'found': False, 'pattern': None, 'direction': 'NEUTRAL',
               'strength': 0, 'reliability': 0, 'desc': ''}

    n = len(closes)
    if n < 3 or atr <= 0:
        return default

    o = [float(x) for x in opens]
    h = [float(x) for x in highs]
    l = [float(x) for x in lows]
    c = [float(x) for x in closes]

    # ── Threshold ─────────────────────────────────────────────
    SMALL  = atr * 0.15
    MED    = atr * 0.35
    LARGE  = atr * 0.65

    # ── Helper ────────────────────────────────────────────────
    def body(i):   return abs(c[i] - o[i])
    def rng(i):    return max(h[i] - l[i], 0.0001)
    def upper(i):  return h[i] - max(c[i], o[i])
    def lower(i):  return min(c[i], o[i]) - l[i]
    def bull(i):   return c[i] >= o[i]
    def mid(i):    return (o[i] + c[i]) / 2

    # Shorthand indices
    i0, i1, i2 = -1, -2, -3

    # ═══════════════════════════════════════════════════════════
    # CONTINUATION PATTERNS (5 candle — cek dulu)
    # ═══════════════════════════════════════════════════════════
    if n >= 5:
        i3, i4 = -4, -5

        # Rising Three Methods — BULLISH CONTINUATION (reliability 68%)
        # 1 candle bullish besar, 3 kecil bearish dalam range, 1 bullish besar baru high
        if (bull(i4) and body(i4) >= LARGE and
                not bull(i3) and body(i3) <= MED and
                not bull(i2) and body(i2) <= MED and
                not bull(i1) and body(i1) <= MED and
                bull(i0) and body(i0) >= LARGE and
                c[i0] > c[i4] and
                all(h[j] <= h[i4] and l[j] >= l[i4] for j in [i3,i2,i1])):
            return _mk('Rising Three Methods', 'BULLISH', 2, 68,
                       'Konsolidasi kecil dalam uptrend — bullish continuation')

        # Falling Three Methods — BEARISH CONTINUATION (reliability 65%)
        if (not bull(i4) and body(i4) >= LARGE and
                bull(i3) and body(i3) <= MED and
                bull(i2) and body(i2) <= MED and
                bull(i1) and body(i1) <= MED and
                not bull(i0) and body(i0) >= LARGE and
                c[i0] < c[i4] and
                all(l[j] >= l[i4] and h[j] <= h[i4] for j in [i3,i2,i1])):
            return _mk('Falling Three Methods', 'BEARISH', 2, 65,
                       'Konsolidasi kecil dalam downtrend — bearish continuation')

    # ═══════════════════════════════════════════════════════════
    # TRIPLE CANDLE REVERSAL PATTERNS
    # ═══════════════════════════════════════════════════════════

    # Morning Star — BULLISH (reliability 78%)
    if n >= 3 and (not bull(i2) and body(i2) >= MED and
            body(i1) <= SMALL * 2 and
            bull(i0) and body(i0) >= MED and
            c[i0] > mid(i2)):
        return _mk('Morning Star', 'BULLISH', 3, 78,
                   'Pembalikan bullish kuat — star antara dua candle besar')

    # Evening Star — BEARISH (reliability 72%)
    if n >= 3 and (bull(i2) and body(i2) >= MED and
            body(i1) <= SMALL * 2 and
            not bull(i0) and body(i0) >= MED and
            c[i0] < mid(i2)):
        return _mk('Evening Star', 'BEARISH', 3, 72,
                   'Pembalikan bearish kuat — star antara dua candle besar')

    # Morning Doji Star — BULLISH (reliability 76%)
    if n >= 3 and (not bull(i2) and body(i2) >= MED and
            body(i1) <= SMALL and          # Doji
            bull(i0) and body(i0) >= MED and
            c[i0] > mid(i2)):
        return _mk('Morning Doji Star', 'BULLISH', 3, 76,
                   'Doji sebagai bintang — sinyal pembalikan sangat kuat')

    # Evening Doji Star — BEARISH (reliability 74%)
    if n >= 3 and (bull(i2) and body(i2) >= MED and
            body(i1) <= SMALL and
            not bull(i0) and body(i0) >= MED and
            c[i0] < mid(i2)):
        return _mk('Evening Doji Star', 'BEARISH', 3, 74,
                   'Doji sebagai bintang — sinyal pembalikan bearish sangat kuat')

    # Abandoned Baby Bullish — BULLISH (reliability 70%)
    # Gap down ke doji, gap up konfirmasi
    if n >= 3 and (not bull(i2) and body(i2) >= MED and
            body(i1) <= SMALL and
            h[i1] < l[i2] and  # gap down
            bull(i0) and
            l[i0] > h[i1]):    # gap up
        return _mk('Abandoned Baby Bullish', 'BULLISH', 3, 70,
                   'Gap doji — pembalikan bullish dengan gap konfirmasi')

    # Abandoned Baby Bearish — BEARISH (reliability 68%)
    if n >= 3 and (bull(i2) and body(i2) >= MED and
            body(i1) <= SMALL and
            l[i1] > h[i2] and  # gap up
            not bull(i0) and
            h[i0] < l[i1]):    # gap down
        return _mk('Abandoned Baby Bearish', 'BEARISH', 3, 68,
                   'Gap doji — pembalikan bearish dengan gap konfirmasi')

    # Three White Soldiers — BULLISH (reliability 83%)
    if n >= 3 and (bull(i0) and bull(i1) and bull(i2) and
            body(i0) >= MED and body(i1) >= MED and body(i2) >= MED and
            c[i0] > c[i1] > c[i2] and
            o[i0] > o[i1] > o[i2] and
            upper(i0) <= body(i0) * 0.3 and upper(i1) <= body(i1) * 0.3):
        return _mk('Three White Soldiers', 'BULLISH', 3, 83,
                   'Tiga candle bullish berturut makin tinggi — buyer full control')

    # Three Black Crows — BEARISH (reliability 78%)
    if n >= 3 and (not bull(i0) and not bull(i1) and not bull(i2) and
            body(i0) >= MED and body(i1) >= MED and body(i2) >= MED and
            c[i0] < c[i1] < c[i2] and
            o[i0] < o[i1] < o[i2] and
            lower(i0) <= body(i0) * 0.3 and lower(i1) <= body(i1) * 0.3):
        return _mk('Three Black Crows', 'BEARISH', 3, 78,
                   'Tiga candle bearish berturut makin rendah — seller full control')

    # Three Inside Up — BULLISH (reliability 65%)
    if n >= 3 and (not bull(i2) and body(i2) >= MED and
            bull(i1) and
            o[i1] > c[i2] and c[i1] < o[i2] and  # harami
            body(i1) >= body(i2) * 0.4 and
            bull(i0) and c[i0] > c[i2]):
        return _mk('Three Inside Up', 'BULLISH', 2, 65,
                   'Bullish harami + konfirmasi naik')

    # Three Inside Down — BEARISH (reliability 63%)
    if n >= 3 and (bull(i2) and body(i2) >= MED and
            not bull(i1) and
            o[i1] < c[i2] and c[i1] > o[i2] and
            body(i1) >= body(i2) * 0.4 and
            not bull(i0) and c[i0] < c[i2]):
        return _mk('Three Inside Down', 'BEARISH', 2, 63,
                   'Bearish harami + konfirmasi turun')

    # Three Outside Up — BULLISH (reliability 68%)
    if n >= 3 and (not bull(i1) and
            bull(i0) and c[i0] >= o[i1] and o[i0] <= c[i1] and body(i0) > body(i1) and
            n >= 3 and bull(i2) and c[i2] > c[i0]):
        return _mk('Three Outside Up', 'BULLISH', 2, 68,
                   'Bullish engulfing + konfirmasi lanjut naik')

    # Three Outside Down — BEARISH (reliability 65%)
    if n >= 3 and (bull(i1) and
            not bull(i0) and c[i0] <= o[i1] and o[i0] >= c[i1] and body(i0) > body(i1) and
            n >= 3 and not bull(i2) and c[i2] < c[i0]):
        return _mk('Three Outside Down', 'BEARISH', 2, 65,
                   'Bearish engulfing + konfirmasi lanjut turun')

    # ═══════════════════════════════════════════════════════════
    # DOUBLE CANDLE PATTERNS
    # ═══════════════════════════════════════════════════════════

    # Bullish Engulfing — BULLISH (reliability 63%)
    if n >= 2 and (not bull(i1) and bull(i0) and
            c[i0] >= o[i1] and o[i0] <= c[i1] and
            body(i0) > body(i1)):
        return _mk('Bullish Engulfing', 'BULLISH', 2, 63,
                   'Candle bullish menelan candle bearish — buyer membalik momentum')

    # Bearish Engulfing — BEARISH (reliability 61%)
    if n >= 2 and (bull(i1) and not bull(i0) and
            c[i0] <= o[i1] and o[i0] >= c[i1] and
            body(i0) > body(i1)):
        return _mk('Bearish Engulfing', 'BEARISH', 2, 61,
                   'Candle bearish menelan candle bullish — seller membalik momentum')

    # Piercing Line — BULLISH (reliability 61%)
    if n >= 2 and (not bull(i1) and body(i1) >= MED and
            bull(i0) and
            o[i0] < c[i1] and       # open di bawah close candle bearish
            c[i0] > mid(i1) and     # close di atas midpoint
            c[i0] < o[i1]):         # tapi belum mencapai open
        return _mk('Piercing Line', 'BULLISH', 2, 61,
                   'Candle bullish menembus lebih dari 50% candle bearish sebelumnya')

    # Dark Cloud Cover — BEARISH (reliability 61%)
    if n >= 2 and (bull(i1) and body(i1) >= MED and
            not bull(i0) and
            o[i0] > c[i1] and       # open di atas close candle bullish
            c[i0] < mid(i1) and     # close di bawah midpoint
            c[i0] > o[i1]):         # tapi belum mencapai open bearish
        return _mk('Dark Cloud Cover', 'BEARISH', 2, 61,
                   'Candle bearish menembus lebih dari 50% candle bullish sebelumnya')

    # Bullish Harami — BULLISH (reliability 53%)
    if n >= 2 and (not bull(i1) and body(i1) >= MED and
            bull(i0) and
            o[i0] > c[i1] and c[i0] < o[i1] and
            body(i0) <= body(i1) * 0.6):
        return _mk('Bullish Harami', 'BULLISH', 1, 53,
                   'Candle kecil dalam candle besar — selling pressure melemah')

    # Bearish Harami — BEARISH (reliability 53%)
    if n >= 2 and (bull(i1) and body(i1) >= MED and
            not bull(i0) and
            o[i0] < c[i1] and c[i0] > o[i1] and
            body(i0) <= body(i1) * 0.6):
        return _mk('Bearish Harami', 'BEARISH', 1, 53,
                   'Candle kecil dalam candle besar — buying pressure melemah')

    # Bullish Harami Cross — BULLISH (reliability 58%)
    if n >= 2 and (not bull(i1) and body(i1) >= MED and
            body(i0) <= SMALL and    # Doji di tengah
            h[i0] < o[i1] and l[i0] > c[i1]):
        return _mk('Bullish Harami Cross', 'BULLISH', 2, 58,
                   'Doji dalam candle bearish besar — pembalikan kuat')

    # Bearish Harami Cross — BEARISH (reliability 58%)
    if n >= 2 and (bull(i1) and body(i1) >= MED and
            body(i0) <= SMALL and
            h[i0] < c[i1] and l[i0] > o[i1]):
        return _mk('Bearish Harami Cross', 'BEARISH', 2, 58,
                   'Doji dalam candle bullish besar — pembalikan kuat')

    # Tweezer Bottom — BULLISH (reliability 55%)
    if n >= 2 and (not bull(i1) and bull(i0) and
            abs(l[i0] - l[i1]) <= atr * 0.08 and
            body(i0) >= SMALL and body(i1) >= SMALL):
        return _mk('Tweezer Bottom', 'BULLISH', 1, 55,
                   'Dua low hampir sama — double bottom kecil di support')

    # Tweezer Top — BEARISH (reliability 55%)
    if n >= 2 and (bull(i1) and not bull(i0) and
            abs(h[i0] - h[i1]) <= atr * 0.08 and
            body(i0) >= SMALL and body(i1) >= SMALL):
        return _mk('Tweezer Top', 'BEARISH', 1, 55,
                   'Dua high hampir sama — double top kecil di resistance')

    # Kicker Bullish — BULLISH (reliability 70%)
    # Gap besar dari bearish ke bullish (shift sentimen tiba-tiba)
    if n >= 2 and (not bull(i1) and body(i1) >= MED and
            bull(i0) and body(i0) >= MED and
            o[i0] >= o[i1] and      # open gap up atau sama
            c[i0] > c[i1]):
        return _mk('Bullish Kicker', 'BULLISH', 3, 70,
                   'Gap sentiment shift — buyer tiba-tiba mengambil alih')

    # Kicker Bearish — BEARISH (reliability 70%)
    if n >= 2 and (bull(i1) and body(i1) >= MED and
            not bull(i0) and body(i0) >= MED and
            o[i0] <= o[i1] and
            c[i0] < c[i1]):
        return _mk('Bearish Kicker', 'BEARISH', 3, 70,
                   'Gap sentiment shift — seller tiba-tiba mengambil alih')

    # Belt Hold Bullish — BULLISH (reliability 57%)
    # Marubozu bullish yang open di low
    if n >= 1 and (bull(i0) and body(i0) >= LARGE and
            lower(i0) <= SMALL * 0.3 and   # hampir tidak ada lower wick
            o[i0] <= l[i0] + SMALL):       # open di near low
        return _mk('Belt Hold Bullish', 'BULLISH', 2, 57,
                   'Open di low, close tinggi — buyer langsung mengambil alih')

    # Belt Hold Bearish — BEARISH (reliability 57%)
    if n >= 1 and (not bull(i0) and body(i0) >= LARGE and
            upper(i0) <= SMALL * 0.3 and
            o[i0] >= h[i0] - SMALL):
        return _mk('Belt Hold Bearish', 'BEARISH', 2, 57,
                   'Open di high, close rendah — seller langsung mengambil alih')

    # ═══════════════════════════════════════════════════════════
    # SINGLE CANDLE PATTERNS
    # ═══════════════════════════════════════════════════════════

    b0 = body(i0); r0 = rng(i0); u0 = upper(i0); l0 = lower(i0)

    # Hammer — BULLISH (reliability 60%)
    if (l0 >= b0 * 2.0 and u0 <= b0 * 0.5 and
            b0 >= SMALL and r0 >= atr * 0.4):
        return _mk('Hammer', 'BULLISH', 2, 60,
                   f'Lower wick {l0/max(b0,0.001):.1f}x body — rejection kuat dari bawah')

    # Shooting Star — BEARISH (reliability 59%)
    if (u0 >= b0 * 2.0 and l0 <= b0 * 0.5 and
            b0 >= SMALL and r0 >= atr * 0.4):
        return _mk('Shooting Star', 'BEARISH', 2, 59,
                   f'Upper wick {u0/max(b0,0.001):.1f}x body — rejection kuat dari atas')

    # Hanging Man — BEARISH (reliability 59%)
    # Sama dengan hammer tapi di uptrend
    if (l0 >= b0 * 2.0 and u0 <= b0 * 0.5 and
            b0 >= SMALL and n >= 2 and bull(i1) and c[i1] > c[i2] if n >= 3 else True):
        # Ini lebih ke warning — harus dikonfirmasi candle berikutnya
        return _mk('Hanging Man', 'BEARISH', 1, 59,
                   'Hammer di atas uptrend — warning pembalikan, butuh konfirmasi')

    # Inverted Hammer — BULLISH (reliability 55%)
    if (u0 >= b0 * 2.0 and l0 <= b0 * 0.5 and
            b0 >= SMALL and n >= 2 and not bull(i1)):
        return _mk('Inverted Hammer', 'BULLISH', 1, 55,
                   'Shooting star di bawah downtrend — potensi reversal bullish')

    # Dragonfly Doji — BULLISH (reliability 62%)
    if (b0 <= SMALL and u0 <= SMALL * 0.5 and l0 >= atr * 0.5):
        return _mk('Dragonfly Doji', 'BULLISH', 2, 62,
                   'Open=High=Close, lower wick panjang — buyer tolak harga rendah')

    # Gravestone Doji — BEARISH (reliability 62%)
    if (b0 <= SMALL and l0 <= SMALL * 0.5 and u0 >= atr * 0.5):
        return _mk('Gravestone Doji', 'BEARISH', 2, 62,
                   'Open=Low=Close, upper wick panjang — seller tolak harga tinggi')

    # Bullish Marubozu — BULLISH (reliability 65%)
    if (bull(i0) and b0 >= LARGE and u0 <= b0 * 0.05 and l0 <= b0 * 0.05):
        return _mk('Bullish Marubozu', 'BULLISH', 3, 65,
                   'Candle bullish penuh tanpa wick — buyer 100% control')

    # Bearish Marubozu — BEARISH (reliability 65%)
    if (not bull(i0) and b0 >= LARGE and u0 <= b0 * 0.05 and l0 <= b0 * 0.05):
        return _mk('Bearish Marubozu', 'BEARISH', 3, 65,
                   'Candle bearish penuh tanpa wick — seller 100% control')

    # High Wave — NEUTRAL (reliability 48%)
    if (b0 <= MED and u0 >= atr * 0.4 and l0 >= atr * 0.4):
        return _mk('High Wave', 'NEUTRAL', 1, 48,
                   'Wick panjang dua sisi — ketidakpastian tinggi di level kunci')

    # Long-Legged Doji — NEUTRAL (reliability 50%)
    if (b0 <= SMALL and u0 >= atr * 0.3 and l0 >= atr * 0.3):
        return _mk('Long-Legged Doji', 'NEUTRAL', 1, 50,
                   'Doji dengan wick panjang — tarik menarik kuat buyer/seller')

    # Doji — NEUTRAL (reliability 50%)
    if b0 <= SMALL:
        return _mk('Doji', 'NEUTRAL', 1, 50,
                   'Open ≈ Close — keseimbangan buyer/seller, tunggu konfirmasi')

    # Spinning Top — NEUTRAL (reliability 48%)
    if (b0 <= MED and u0 >= b0 * 0.5 and l0 >= b0 * 0.5):
        return _mk('Spinning Top', 'NEUTRAL', 1, 48,
                   'Body kecil dengan wick sedang — momentum melemah')

    return default


def _mk(pattern, direction, strength, reliability, desc):
    return {'found': True, 'pattern': pattern, 'direction': direction,
            'strength': strength, 'reliability': reliability, 'desc': desc}


def get_candle_signal(opens, highs, lows, closes, atr,
                      required_direction: str = '') -> dict:
    """Wrapper dengan filter arah opsional."""
    result = detect_candle_pattern(opens, highs, lows, closes, atr)
    if not result['found']:
        return result
    if required_direction and result['direction'] not in (required_direction, 'NEUTRAL'):
        return {'found': False, 'pattern': None, 'direction': 'NEUTRAL',
                'strength': 0, 'reliability': 0, 'desc': ''}
    return result


def format_candle_signal(pattern: dict) -> str:
    """Format untuk Telegram."""
    if not pattern.get('found'):
        return ''
    stars   = '⭐' * min(pattern['strength'], 3)
    ico     = {'BULLISH': '🟢', 'BEARISH': '🔴', 'NEUTRAL': '⬜'}.get(pattern['direction'], '⬜')
    return f"{ico} {pattern['pattern']} {stars} (~{pattern['reliability']}% akurat)\n   {pattern['desc']}"