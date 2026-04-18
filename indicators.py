"""
indicators.py — Pure math indicator functions.

Semua fungsi di sini:
  - Tidak punya side effects
  - Tidak akses network / file / cache
  - Mudah di-test secara individual
  - Mudah di-import ke backtesting engine

Tidak ada TradingEngine di sini — hanya fungsi.
"""

import numpy as np
import pandas as pd


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    period = max(2, min(period, len(series) - 1))
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    period = max(2, min(period, len(df) - 1))
    delta = df['close'].diff()
    gain  = delta.where(delta > 0, 0).rolling(period).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    period = max(2, min(period, len(df) - 1))
    hl = df['high'] - df['low']
    hc = np.abs(df['high'] - df['close'].shift())
    lc = np.abs(df['low']  - df['close'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_adx(df: pd.DataFrame, period: int = 14) -> float:
    """
    Average Directional Index — kekuatan trend, bukan arah.
    ADX > 25 = trending
    ADX < 18 = ranging/choppy
    """
    if df is None or len(df) < period * 3:
        return 20.0

    h, l, c = df['high'].values, df['low'].values, df['close'].values
    n = len(h)

    tr  = np.zeros(n)
    dmp = np.zeros(n)
    dmn = np.zeros(n)

    for i in range(1, n):
        tr[i]  = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
        up, dn = h[i] - h[i-1], l[i-1] - l[i]
        dmp[i] = up if (up > dn and up > 0) else 0
        dmn[i] = dn if (dn > up and dn > 0) else 0

    atr_s = np.zeros(n)
    dip   = np.zeros(n)
    din   = np.zeros(n)
    atr_s[period] = np.mean(tr[1:period+1])
    dip[period]   = np.mean(dmp[1:period+1])
    din[period]   = np.mean(dmn[1:period+1])

    for i in range(period + 1, n):
        atr_s[i] = (atr_s[i-1] * (period - 1) + tr[i])  / period
        dip[i]   = (dip[i-1]   * (period - 1) + dmp[i]) / period
        din[i]   = (din[i-1]   * (period - 1) + dmn[i]) / period

    with np.errstate(divide='ignore', invalid='ignore'):
        pdi = np.where(atr_s > 0, dip / atr_s * 100, 0)
        ndi = np.where(atr_s > 0, din / atr_s * 100, 0)
        dx  = np.where((pdi + ndi) > 0, np.abs(pdi - ndi) / (pdi + ndi) * 100, 0)

    adx_arr = np.zeros(n)
    adx_arr[period * 2] = np.mean(dx[period:period * 2 + 1])
    for i in range(period * 2 + 1, n):
        adx_arr[i] = (adx_arr[i-1] * (period - 1) + dx[i]) / period

    return float(adx_arr[-1]) if not np.isnan(adx_arr[-1]) else 20.0


def analyze_ema_trend(df: pd.DataFrame,
                      fast: int = 9,
                      mid: int = 21,
                      slow: int = 50):
    """
    Hitung EMA alignment dan beri label trend.

    Returns:
        trend   : 'STRONG_UP' | 'UP' | 'WEAK_UP' | 'SIDEWAYS' | 'WEAK_DOWN' | 'DOWN' | 'STRONG_DOWN'
        emas    : dict dengan nilai EMA terakhir
        desc    : deskripsi teks
    """
    if df is None or len(df) < slow + 5:
        return 'SIDEWAYS', {}, ''

    close = df['close']
    ema_f = calc_ema(close, fast).iloc[-1]
    ema_m = calc_ema(close, mid).iloc[-1]
    ema_s = calc_ema(close, slow).iloc[-1]
    price = close.iloc[-1]

    emas = {
        'fast': round(ema_f, 8),
        'mid' : round(ema_m, 8),
        'slow': round(ema_s, 8),
    }

    bull_stack = ema_f > ema_m > ema_s
    bear_stack = ema_f < ema_m < ema_s

    price_above_all = price > ema_f and price > ema_m and price > ema_s
    price_below_all = price < ema_f and price < ema_m and price < ema_s

    if bull_stack and price_above_all:
        trend, desc = 'STRONG_UP', 'EMA tersusun bullish, harga di atas semua EMA'
    elif bull_stack:
        trend, desc = 'UP', 'EMA bullish'
    elif bear_stack and price_below_all:
        trend, desc = 'STRONG_DOWN', 'EMA tersusun bearish, harga di bawah semua EMA'
    elif bear_stack:
        trend, desc = 'DOWN', 'EMA bearish'
    elif price > ema_m:
        trend, desc = 'WEAK_UP', 'EMA campur, slight bullish'
    elif price < ema_m:
        trend, desc = 'WEAK_DOWN', 'EMA campur, slight bearish'
    else:
        trend, desc = 'SIDEWAYS', 'EMA flat / sideways'

    return trend, emas, desc


def detect_market_structure(df: pd.DataFrame, window: int = 3) -> str:
    """
    Identifikasi struktur pasar dari swing high/low.

    Returns: 'UPTREND' | 'DOWNTREND' | 'SIDEWAYS'
    """
    if df is None or len(df) < window * 4 + 5:
        return 'SIDEWAYS'

    h, l = df['high'].values, df['low'].values
    n    = len(h)

    swing_hi, swing_lo = [], []
    for i in range(window, n - window):
        if h[i] >= max(h[i-window:i]) and h[i] >= max(h[i+1:i+window+1]):
            swing_hi.append(h[i])
        if l[i] <= min(l[i-window:i]) and l[i] <= min(l[i+1:i+window+1]):
            swing_lo.append(l[i])

    if len(swing_hi) < 2 or len(swing_lo) < 2:
        return 'SIDEWAYS'

    hh = swing_hi[-1] > swing_hi[-2]
    hl = swing_lo[-1] > swing_lo[-2]
    lh = swing_hi[-1] < swing_hi[-2]
    ll = swing_lo[-1] < swing_lo[-2]

    if hh and hl:
        return 'UPTREND'
    if lh and ll:
        return 'DOWNTREND'
    return 'SIDEWAYS'


def fmt_price(price: float) -> str:
    """Format harga sesuai magnitude."""
    if price >= 1000:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:.4f}"
    if price >= 0.01:
        return f"${price:.6f}"
    return f"${price:.8f}"
