"""
sr_detector.py — Support & Resistance detection.

IMPROVEMENT v5 — Akurasi S&R jauh lebih baik:

MASALAH LAMA:
  - Zone terlalu dekat current price dianggap valid support/resistance
  - "14x reject" malah dapat bonus score padahal artinya zone sudah exhausted
  - Zone width fixed ±0.5 ATR, seringkali overlap dengan harga sekarang
  - Swing low tidak divalidasi — micro-oscillation dianggap swing

PERBAIKAN:
  1. find_swings(): Validasi bounce minimum — swing harus menghasilkan gerakan
     signifikan (>= 0.6 ATR), bukan sekadar micro-oscillation
  2. cluster_and_score(): Zone width dari spread cluster nyata, bukan fixed ATR.
     Exhaustion penalty lebih tegas. Hard reject zone exhausted yang terlalu
     dekat harga sekarang (< 2 ATR + 8+ touches = congestion/noise).
  3. detect_key_levels(): Filter minimum jarak — support harus minimal 1 ATR
     di bawah harga sekarang. Zone yang terlalu dekat (< 0.3 ATR buffer antara
     zone top dan harga) dibuang.
"""

import numpy as np
import pandas as pd
from config import SIGNAL_PARAMS as SP


def find_swings(df: pd.DataFrame, window: int = 5):
    """
    Deteksi swing high/low dengan validasi bounce minimum.

    PERBAIKAN v5: Swing hanya valid kalau menghasilkan gerakan >= 0.6 ATR
    setelah terbentuk. Ini mencegah micro-oscillation dihitung sebagai swing.

    Contoh: swing low di $54.58 valid HANYA kalau harga naik >= 0.6 ATR
    setelah itu — kalau tidak, itu hanya congestion, bukan level nyata.
    """
    if df is None or len(df) < window * 2 + 1:
        return [], []
    h, l = df['high'].values, df['low'].values
    n    = len(h)

    # ATR untuk validasi minimum bounce
    atr = calc_atr_for_sr(df, 14)
    if atr is None:
        atr = float(np.mean(h[-20:] - l[-20:])) if n >= 20 else float(np.mean(h - l))
    min_bounce = atr * 0.6   # Swing harus menghasilkan >= 0.6 ATR gerakan

    swing_lo, swing_hi = [], []
    for i in range(window, n - window):
        if h[i] >= max(h[i-window:i]) and h[i] >= max(h[i+1:i+window+1]):
            # Validasi: drop dari swing high harus meaningful
            look_ahead  = min(i + window * 2, n)
            future_low  = min(l[i+1:look_ahead]) if i + 1 < look_ahead else h[i]
            if h[i] - future_low >= min_bounce:
                swing_hi.append({'price': h[i], 'idx': i})

        if l[i] <= min(l[i-window:i]) and l[i] <= min(l[i+1:i+window+1]):
            # Validasi: bounce dari swing low harus meaningful
            look_ahead   = min(i + window * 2, n)
            future_high  = max(h[i+1:look_ahead]) if i + 1 < look_ahead else l[i]
            if future_high - l[i] >= min_bounce:
                swing_lo.append({'price': l[i], 'idx': i})

    return swing_lo, swing_hi


def calc_atr_for_sr(df: pd.DataFrame, period: int = 14) -> float | None:
    if df is None or len(df) < period + 2:
        return None
    h = df['high'].values
    l = df['low'].values
    c = df['close'].values
    tr_list = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
               for i in range(1, len(c))]
    if not tr_list:
        return None
    return float(np.mean(tr_list[-period:]))


def round_price(price: float, ref_price: float) -> float:
    if ref_price >= 10000: return round(price, 1)
    if ref_price >= 1000:  return round(price, 2)
    if ref_price >= 100:   return round(price, 3)
    if ref_price >= 1:     return round(price, 4)
    if ref_price >= 0.01:  return round(price, 6)
    return round(price, 8)


# ============================================================
# ORDER BLOCKS — candle sebelum impulse move (institutional)
# ============================================================

def find_order_blocks(df: pd.DataFrame, atr: float):
    """
    Order Block = candle terakhir sebelum impulse move.
    Bullish OB: bearish candle sebelum impulse naik (institusi beli di sini)
    Bearish OB: bullish candle sebelum impulse turun (institusi jual di sini)

    OB dianggap valid hanya jika:
    1. Impulse move >= 1.5x ATR
    2. Candle OB punya volume >= rata-rata (institusi harus ikut)
    3. OB belum pernah fully mitigated (harga belum tembus ke midpoint OB)
    """
    if df is None or len(df) < 10 or atr <= 0:
        return [], []

    o = df['open'].values
    h = df['high'].values
    l = df['low'].values
    c = df['close'].values
    v = df['volume'].values if 'volume' in df.columns else np.ones(len(c))
    n = len(c)

    avg_vol  = float(np.mean(v[-50:])) if n >= 50 else float(np.mean(v))
    min_move = atr * 1.5
    min_vol  = avg_vol * 0.8  # OB harus punya volume minimal 80% rata-rata

    bull_ob, bear_ob = [], []

    for i in range(1, n - 2):
        ob_vol = float(v[i])

        # Bullish OB: bearish candle diikuti impulse naik
        if c[i] < o[i] and ob_vol >= min_vol:
            move_up = max(c[i+1:min(i+4, n)]) - h[i]
            if move_up >= min_move:
                ob_low  = min(o[i], c[i])
                ob_high = max(o[i], c[i])
                ob_mid  = (ob_low + ob_high) / 2
                fresh = all(l[j] > ob_mid for j in range(i+1, n))
                partially_mitigated = not fresh and all(l[j] > ob_low for j in range(i+1, n))
                bull_ob.append({
                    'low': ob_low, 'high': ob_high, 'mid': ob_mid,
                    'idx': i, 'impulse': move_up,
                    'vol': ob_vol, 'vol_ratio': ob_vol / max(avg_vol, 1),
                    'fresh': fresh,
                    'partially_mitigated': partially_mitigated,
                })

        # Bearish OB: bullish candle diikuti impulse turun
        if c[i] > o[i] and ob_vol >= min_vol:
            move_down = l[i] - min(c[i+1:min(i+4, n)])
            if move_down >= min_move:
                ob_low  = min(o[i], c[i])
                ob_high = max(o[i], c[i])
                ob_mid  = (ob_low + ob_high) / 2
                fresh = all(h[j] < ob_mid for j in range(i+1, n))
                partially_mitigated = not fresh and all(h[j] < ob_high for j in range(i+1, n))
                bear_ob.append({
                    'low': ob_low, 'high': ob_high, 'mid': ob_mid,
                    'idx': i, 'impulse': move_down,
                    'vol': ob_vol, 'vol_ratio': ob_vol / max(avg_vol, 1),
                    'fresh': fresh,
                    'partially_mitigated': partially_mitigated,
                })

    return bull_ob, bear_ob


# ============================================================
# FLIP ZONES
# ============================================================

def find_flip_zones(df: pd.DataFrame, atr: float,
                    swing_lo: list, swing_hi: list) -> list:
    if not swing_lo or not swing_hi or atr <= 0:
        return []
    tol    = atr * SP['flip_zone_atr_mult']
    n      = len(df) if df is not None else 100
    cutoff = int(n * 0.3)
    flips  = []
    for sl in swing_lo:
        if sl['idx'] < cutoff: continue
        for sh in swing_hi:
            if sh['idx'] < cutoff: continue
            if abs(sl['price'] - sh['price']) <= tol:
                mid  = (sl['price'] + sh['price']) / 2
                role = 'support' if sl['idx'] > sh['idx'] else 'resistance'
                flips.append({
                    'price': mid,
                    'low'  : min(sl['price'], sh['price']),
                    'high' : max(sl['price'], sh['price']),
                    'role' : role, 'last_idx': max(sl['idx'], sh['idx']),
                    'strength': 15,
                })
    return flips


# ============================================================
# LIQUIDITY SWEEPS
# ============================================================

def find_liquidity_sweeps(df: pd.DataFrame, atr: float,
                          swing_lo: list, swing_hi: list) -> list:
    if df is None or len(df) < 5 or atr <= 0:
        return []
    h, l, c = df['high'].values, df['low'].values, df['close'].values
    n       = len(df)
    tol     = atr * SP['sweep_atr_mult']
    cutoff  = int(n * 0.5)
    sweeps  = []
    for sl in swing_lo:
        if sl['idx'] < cutoff: continue
        level = sl['price']
        for i in range(sl['idx'] + 2, n):
            if l[i] < level - tol:
                if c[i] > level:
                    rev_strength = (c[i] - level) / atr
                    sweeps.append({'price': level, 'sweep_idx': i,
                                   'type': 'support',
                                   'strength': 12 + min(rev_strength * 2, 6)})
                    break
    for sh in swing_hi:
        if sh['idx'] < cutoff: continue
        level = sh['price']
        for i in range(sh['idx'] + 2, n):
            if h[i] > level + tol:
                if c[i] < level:
                    rev_strength = (level - c[i]) / atr
                    sweeps.append({'price': level, 'sweep_idx': i,
                                   'type': 'resistance',
                                   'strength': 12 + min(rev_strength * 2, 6)})
                    break
    return sweeps


# ============================================================
# REJECTION COUNT + EXHAUSTION DETECTION
# ============================================================

def count_rejections(df: pd.DataFrame, level: float, atr: float):
    """
    Hitung berapa kali harga rejection di level ini.

    IMPROVEMENT v5 — Kualitas rejection lebih ketat:
    - True rejection: wick menyentuh level TAPI close jauh dari level (bounce nyata)
    - Doji/spinning top di level dihitung 0.3x (bukan rejection, itu konsolidasi)
    - Engulfing melewati level tidak dihitung (zone sudah ditembus)

    EXHAUSTION LOGIC (lebih tegas dari v4):
    Touches 1–2 : zone masih aktif — full score
    Touches 3–4 : mulai habis — 60% score
    Touches 5–7 : zone lemah — 30% score
    Touches 8+  : zone hampir mati — 10% score (banyak touch = orders habis)
    """
    if df is None or len(df) < 5 or atr <= 0:
        return 0, 0

    tol  = atr * SP['flip_zone_atr_mult']
    h, l, o, c = (df['high'].values, df['low'].values,
                  df['open'].values, df['close'].values)
    n            = len(df)
    recent_start = int(n * 0.7)

    touch_list   = []
    rejections   = 0.0

    for i in range(n):
        if l[i] <= level + tol and h[i] >= level - tol:
            weight = 1.0 if i >= recent_start else 0.5
            touch_list.append((i, weight))
            body  = abs(c[i] - o[i])
            range_ = h[i] - l[i]

            # Evaluasi kualitas rejection
            if range_ < atr * 0.1:
                # Candle terlalu kecil — bukan signal apapun
                continue

            body_ratio = body / range_ if range_ > 0 else 1.0
            is_doji = body_ratio < 0.25   # body < 25% dari range = doji/spinning top

            if is_doji:
                # Doji di level = konsolidasi, bukan rejection nyata
                rejections += weight * 0.2
            else:
                body_mid = (o[i] + c[i]) / 2
                if abs(body_mid - level) > atr * SP['rejection_body_atr_mult']:
                    rejections += weight

    touches = len(touch_list)

    # Exhaustion penalty — lebih agresif dari v4
    # Teori: setiap touch mengkonsumsi pending institutional orders
    # Setelah 8+ touch, hampir tidak ada orders tersisa di level itu
    if touches >= 8:
        effective_rejections = max(0, int(round(rejections * 0.1)))
    elif touches >= 5:
        effective_rejections = max(0, int(round(rejections * 0.3)))
    elif touches >= 3:
        effective_rejections = max(0, int(round(rejections * 0.6)))
    else:
        effective_rejections = int(round(rejections))

    return touches, effective_rejections


def get_zone_exhaustion_label(touches: int) -> str:
    """Label exhaustion untuk signal reasons."""
    if touches >= 8: return 'exhausted'
    if touches >= 5: return 'weakening'
    if touches >= 3: return 'tested'
    return 'fresh'


# ============================================================
# FRESHNESS CHECK
# ============================================================

def check_freshness(df: pd.DataFrame, level: float,
                    atr: float, creation_idx: int) -> bool:
    """Fresh = level belum pernah di-retest setelah terbentuk."""
    if df is None or len(df) < 5 or atr <= 0:
        return True
    tol = atr * SP['freshness_retest_atr']
    h   = df['high'].values
    l   = df['low'].values
    for i in range(creation_idx + 3, len(df)):
        if l[i] <= level + tol and h[i] >= level - tol:
            return False
    return True


# ============================================================
# KEY HORIZONTAL LEVELS
# ============================================================

def find_key_horizontal_levels(df: pd.DataFrame,
                                price: float, atr: float) -> list:
    levels = []
    if df is None or len(df) < 20:
        return levels
    h, l, c = df['high'].values, df['low'].values, df['close'].values
    v = df['volume'].values if 'volume' in df.columns else np.ones(len(c))
    n = len(df)
    lookback = min(50, n - 1)
    recent_h = h[-lookback:]
    recent_l = l[-lookback:]
    recent_v = v[-lookback:]

    # Highest high — volume weighted
    max_idx_rel = int(np.argmax(recent_h))
    max_price   = recent_h[max_idx_rel]
    if max_price > price:
        vol_bonus = 3 if recent_v[max_idx_rel] > np.mean(recent_v) * 1.5 else 0
        levels.append({'price': max_price,
                        'low': max_price - atr*0.2, 'high': max_price,
                        'score': 15 + vol_bonus, 'type': 'resistance',
                        'method': 'recent_high', 'idx': n - lookback + max_idx_rel})

    # Lowest low — volume weighted
    min_idx_rel = int(np.argmin(recent_l))
    min_price   = recent_l[min_idx_rel]
    if min_price < price:
        vol_bonus = 3 if recent_v[min_idx_rel] > np.mean(recent_v) * 1.5 else 0
        levels.append({'price': min_price,
                        'low': min_price, 'high': min_price + atr*0.2,
                        'score': 15 + vol_bonus, 'type': 'support',
                        'method': 'recent_low', 'idx': n - lookback + min_idx_rel})

    # Round numbers
    magnitude = 10 ** (len(str(int(price))) - 2)
    for mult in range(1, 20):
        round_level = round(magnitude * mult, 8)
        if abs(round_level - price) < atr * 3:
            role = 'resistance' if round_level > price else 'support'
            levels.append({'price': round_level,
                            'low': round_level - atr*0.1, 'high': round_level + atr*0.1,
                            'score': 10, 'type': role,
                            'method': 'round_number', 'idx': n - 1})
    return levels


# ============================================================
# STRUCTURAL S&R
# ============================================================

def find_structural_sr(df: pd.DataFrame,
                       price: float, atr: float) -> list:
    if df is None or len(df) < 30:
        return []
    swing_lo, swing_hi = find_swings(df, window=5)
    levels = []
    for i, sl in enumerate(swing_lo):
        if sl['idx'] < len(df) // 3: continue
        highs_after = [sh for sh in swing_hi if sh['idx'] > sl['idx']]
        if len(highs_after) >= 2 and highs_after[-1]['price'] > highs_after[-2]['price']:
            levels.append({'price': sl['price'],
                            'low': sl['price'] - atr*0.2, 'high': sl['price'] + atr*0.2,
                            'score': 20, 'type': 'support',
                            'method': 'structural_hl', 'idx': sl['idx']})
    for i, sh in enumerate(swing_hi):
        if sh['idx'] < len(df) // 3: continue
        lows_after = [sl for sl in swing_lo if sl['idx'] > sh['idx']]
        if len(lows_after) >= 2 and lows_after[-1]['price'] < lows_after[-2]['price']:
            levels.append({'price': sh['price'],
                            'low': sh['price'] - atr*0.2, 'high': sh['price'] + atr*0.2,
                            'score': 20, 'type': 'resistance',
                            'method': 'structural_lh', 'idx': sh['idx']})
    return levels


# ============================================================
# FIBONACCI RETRACEMENT LEVELS
# ============================================================

def find_fibonacci_levels(df: pd.DataFrame, price: float, atr: float,
                           swing_lo: list, swing_hi: list) -> list:
    """
    Hitung level Fibonacci retracement dari impulse move terbesar yang relevan.

    PENDEKATAN TRADER PROFESIONAL:
    Daripada mengandalkan swing detection (yang sering gagal menangkap base rally),
    fungsi ini mencari impulse move dengan cara:
      1. Upswing support: Temukan HIGH tertinggi terbaru → cari LOW terendah
         SEBELUM high itu (base rally) → hitung retracement dari sana
      2. Downswing resistance: Temukan LOW terendah terbaru → cari HIGH tertinggi
         SEBELUM low itu → hitung retracement dari sana

    Ini lebih akurat karena tidak bergantung pada swing detection yang bisa
    melewatkan level penting di awal data.

    Level yang didapat:
      Area sideways di dekat harga = BUKAN support (diabaikan)
      Fib 0.618 dari rally besar = support NYATA di mana institusi masuk kembali

    Scoring:
      Fib 0.618 → score 22 (golden ratio, paling sering dihormati)
      Fib 0.500 → score 18 (50% midpoint, psikologis kuat)
      Fib 0.382 → score 15 (shallow retracement, trend masih kuat)
      Fib 0.786 → score 12 (deep — warning: struktur mungkin lemah)
      Fib 0.236 → score 8  (terlalu dangkal, hanya minor support)
    """
    if df is None or len(df) < 20 or atr <= 0:
        return []

    h   = df['high'].values
    l   = df['low'].values
    n   = len(df)
    levels = []

    fib_defs = [
        (0.236, 8,  'Fib 0.236'),
        (0.382, 15, 'Fib 0.382'),
        (0.500, 18, 'Fib 0.500 (50%)'),
        (0.618, 22, 'Fib 0.618 (Golden)'),
        (0.786, 12, 'Fib 0.786'),
    ]

    # Minimum impulse: move harus >= 3 ATR (signifikan secara teknikal)
    min_impulse = atr * 3.0

    # ── UPSWING SUPPORT ──────────────────────────────────────────────────────
    # Strategi: cari HIGH tertinggi di 70% data terbaru.
    # Lalu cari LOW terendah dalam 120 candle SEBELUM high itu.
    # Ini menangkap "base of rally" bahkan kalau swing low-nya di awal data.
    search_start = max(0, n - int(n * 0.75))
    peak_idx = int(np.argmax(h[search_start:])) + search_start
    peak_price = float(h[peak_idx])

    if peak_price > price:   # peak harus di atas harga sekarang (bukan sudah dilewati)
        base_window_start = max(0, peak_idx - 120)
        base_window = l[base_window_start:peak_idx]
        if len(base_window) > 5:
            base_idx_rel = int(np.argmin(base_window))
            base_price   = float(base_window[base_idx_rel])
            move = peak_price - base_price

            if move >= min_impulse:
                for ratio, score, label in fib_defs:
                    fib_price = peak_price - move * ratio
                    # Harus jelas di bawah harga sekarang — bukan area konsolidasi
                    if fib_price < price - atr * 0.8:
                        levels.append({
                            'price'    : fib_price,
                            'low'      : fib_price - atr * 0.25,
                            'high'     : fib_price + atr * 0.25,
                            'score'    : score,
                            'type'     : 'support',
                            'method'   : f'fib_{int(ratio*1000)}',
                            'idx'      : peak_idx,
                            'fib_label': label,
                            'fib_from' : f'{round_price(base_price, price)} to {round_price(peak_price, price)}',
                        })

    # ── DOWNSWING RESISTANCE ─────────────────────────────────────────────────
    # Strategi: cari LOW terendah di 70% data terbaru.
    # Lalu cari HIGH tertinggi dalam 120 candle SEBELUM low itu.
    trough_idx = int(np.argmin(l[search_start:])) + search_start
    trough_price = float(l[trough_idx])

    if trough_price < price:   # trough harus di bawah harga sekarang
        top_window_start = max(0, trough_idx - 120)
        top_window = h[top_window_start:trough_idx]
        if len(top_window) > 5:
            top_idx_rel = int(np.argmax(top_window))
            top_price   = float(top_window[top_idx_rel])
            move = top_price - trough_price

            if move >= min_impulse:
                for ratio, score, label in fib_defs:
                    fib_price = trough_price + move * ratio
                    # Harus jelas di atas harga sekarang
                    if fib_price > price + atr * 0.8:
                        levels.append({
                            'price'    : fib_price,
                            'low'      : fib_price - atr * 0.25,
                            'high'     : fib_price + atr * 0.25,
                            'score'    : score,
                            'type'     : 'resistance',
                            'method'   : f'fib_{int(ratio*1000)}',
                            'idx'      : trough_idx,
                            'fib_label': label,
                            'fib_from' : f'{round_price(top_price, price)} to {round_price(trough_price, price)}',
                        })

    return levels


# ============================================================
# VOLUME PROFILE — estimasi volume di tiap level harga
# ============================================================

def calc_volume_at_price(df: pd.DataFrame, level_price: float,
                          zone_low: float, zone_high: float) -> float:
    """
    Estimasi total volume yang diperdagangkan di dalam zona harga.

    Prinsip: setiap candle mendistribusikan volume-nya secara merata
    antara high dan low candle itu. Bagian yang overlap dengan zona kita
    = volume yang diperdagangkan di zona itu.

    Level dengan volume tinggi = banyak posisi terbuka di sana = S/R lebih kuat.

    Return: rasio volume zona vs rata-rata candle (>1.5 = signifikan)
    """
    try:
        if df is None or len(df) < 5 or 'volume' not in df.columns:
            return 1.0

        highs  = df['high'].values.astype(float)
        lows   = df['low'].values.astype(float)
        vols   = df['volume'].values.astype(float)

        zone_vol  = 0.0
        candle_ranges = highs - lows

        for i in range(len(df)):
            candle_range = candle_ranges[i]
            if candle_range <= 0:
                continue
            # Overlap antara candle dan zona
            overlap_low  = max(lows[i],  zone_low)
            overlap_high = min(highs[i], zone_high)
            if overlap_high <= overlap_low:
                continue
            # Proporsional volume di zona ini
            overlap_pct = (overlap_high - overlap_low) / candle_range
            zone_vol   += vols[i] * overlap_pct

        # Bandingkan dengan rata-rata volume per candle di zona harga
        candle_count = max(len(df), 1)
        avg_vol_per_candle = np.sum(vols) / candle_count
        if avg_vol_per_candle <= 0:
            return 1.0

        # Normalisasi: berapa candle-equivalent volume ada di zona ini
        zone_width_pct = (zone_high - zone_low) / max(level_price, 1e-8)
        # Zona yang lebih sempit seharusnya punya volume lebih sedikit secara proporsional
        # Normalisasi ke per-0.5% zona width
        norm_factor = max(zone_width_pct / 0.005, 0.1)
        vol_ratio = zone_vol / (avg_vol_per_candle * norm_factor * candle_count * 0.05)
        return round(float(vol_ratio), 2)

    except Exception:
        return 1.0


# ============================================================
# CLUSTER AND SCORE
# ============================================================

def cluster_and_score(raw_levels: list,
                      atr_cluster: float,
                      atr_validate: float,
                      price: float,
                      df: pd.DataFrame,
                      symbol: str = '') -> list:
    """
    Cluster, score, dan apply exhaustion penalty ke semua level.

    IMPROVEMENT v5:
    - Zone width dari spread cluster nyata (bukan fixed ±0.5 ATR)
    - Exhaustion penalty lebih agresif (8+ touches = -35 poin)
    - Hard reject: zone 8+ touches DAN < 2 ATR dari harga = congestion/noise
    - Structural bonus untuk level yang berusia (sudah ada sejak lama)
    """
    if not raw_levels or atr_cluster <= 0:
        return []

    raw_levels.sort(key=lambda x: x.get('price', 0))

    # Cluster
    clusters = []
    for lvl in raw_levels:
        placed = False
        for cl in clusters:
            if abs(lvl['price'] - cl['representative']) <= atr_cluster:
                cl['members'].append(lvl)
                cl['representative'] = np.mean([m['price'] for m in cl['members']])
                placed = True
                break
        if not placed:
            clusters.append({'representative': lvl['price'], 'members': [lvl]})

    scored = []
    for cl in clusters:
        members   = cl['members']
        rep_price = cl['representative']
        base_score = sum(m.get('score', 5) for m in members)

        methods  = {m.get('method', '') for m in members}
        has_htf  = any('htf' in m.get('method', '') for m in members)
        has_mtf  = not has_htf and len(members) >= 2
        has_fib  = any('fib' in m.get('method', '') for m in members)
        is_fresh = check_freshness(df, rep_price, atr_validate,
                                   max(m.get('idx', 0) for m in members))

        if has_htf and has_mtf:
            base_score += SP['sr_htf_mtf_bonus']
        if is_fresh:
            base_score += SP['sr_fresh_bonus']

        # Fibonacci confluence bonus:
        # Kalau level Fib BERTEPATAN dengan level struktural/OB = sangat kuat
        # Contoh: Fib 0.618 di area order block = institusi DAN fibonacci sama-sama valid
        if has_fib:
            has_structural = any(m.get('method', '') in
                                 ('htf_structural_hl', 'htf_structural_lh',
                                  'htf_ob', 'mtf_ob', 'htf_flip')
                                 for m in members)
            if has_structural:
                base_score += 8   # Fibonacci + struktur = konfluens kuat

        # Order block volume bonus
        ob_members = [m for m in members if 'ob' in m.get('method', '')]
        for ob in ob_members:
            vol_ratio = ob.get('vol_ratio', 1.0)
            if vol_ratio >= 2.0:
                base_score += 5
            elif vol_ratio >= 1.5:
                base_score += 3
            if ob.get('partially_mitigated', False):
                base_score -= 3
            if not ob.get('fresh', True) and not ob.get('partially_mitigated', False):
                base_score -= 8

        touches = rejections = 0
        if df is not None:
            touches, rejections = count_rejections(df, rep_price, atr_validate)
            base_score += touches
            base_score += rejections * 2

            # ── EXHAUSTION PENALTY v5 — lebih tegas ──────────────────────
            # Setiap touch mengkonsumsi pending orders di level itu.
            # 8+ touch = orders hampir habis = level tidak bisa menahan harga lagi
            if touches >= 8:
                base_score -= 35  # Level hampir mati
            elif touches >= 5:
                base_score -= 18  # Level melemah signifikan
            elif touches >= 3:
                base_score -= 7   # Mulai terkikis

            # ── HARD REJECT: exhausted zone dekat harga sekarang ─────────
            # Zone yang sering ditest DAN berada dekat harga = congestion zone
            # bukan support/resistance nyata — itu hanya range trading biasa
            dist_to_price = abs(rep_price - price)
            if touches >= 8 and dist_to_price < atr_validate * 2.0:
                continue   # Noise — skip sepenuhnya
        else:
            touches = rejections = 0

        final_score = base_score
        if final_score < SP['sr_min_final_score']:
            continue

        # ── ZONE WIDTH dari spread cluster nyata ─────────────────────────
        # Bukan fixed ±0.5 ATR — sesuaikan dengan level yang ada di cluster
        # Ini mencegah zone terlalu lebar yang overlap dengan harga sekarang
        if len(members) > 1:
            all_lows  = [m.get('low',  m['price'] - atr_validate * 0.25) for m in members]
            all_highs = [m.get('high', m['price'] + atr_validate * 0.25) for m in members]
            zone_low  = min(all_lows)
            zone_high = max(all_highs)
            # Cap lebar zone maksimal 1.5 ATR agar tidak terlalu lebar
            max_width = atr_validate * 1.5
            if zone_high - zone_low > max_width:
                zone_low  = rep_price - atr_validate * 0.5
                zone_high = rep_price + atr_validate * 0.5
        else:
            m = members[0]
            zone_low  = m.get('low',  rep_price - atr_validate * 0.35)
            zone_high = m.get('high', rep_price + atr_validate * 0.35)
            # Single member: cap lebar ke 0.7 ATR
            if zone_high - zone_low > atr_validate * 0.7:
                zone_low  = rep_price - atr_validate * 0.35
                zone_high = rep_price + atr_validate * 0.35

        # ── VOLUME PROFILE BONUS ─────────────────────────────────────────────
        # Level dengan volume tinggi = banyak posisi institusi terbuka di sana
        # Ini adalah S/R terkuat — bukan sekadar swing high/low biasa
        vol_ratio = calc_volume_at_price(df, rep_price, zone_low, zone_high)
        if vol_ratio >= 3.0:
            final_score += 12   # Volume sangat tinggi = level institusi
        elif vol_ratio >= 2.0:
            final_score += 7    # Volume tinggi
        elif vol_ratio >= 1.5:
            final_score += 3    # Volume di atas rata-rata
        elif vol_ratio < 0.5:
            final_score -= 5    # Volume sangat rendah = level lemah

        lvl_type = members[0].get('type', 'support')
        if final_score >= SP['sr_score_very_strong']:
            strength_label = 'Sangat Kuat'
        elif final_score >= SP['sr_score_strong']:
            strength_label = 'Kuat'
        else:
            strength_label = 'Sedang'

        # Ambil label Fibonacci kalau ada — prioritaskan tampilan Fib
        fib_members = [m for m in members if 'fib' in m.get('method', '')]
        fib_label   = ''
        fib_from    = ''
        if fib_members:
            # Ambil Fib terkuat (score tertinggi)
            best_fib = max(fib_members, key=lambda x: x.get('score', 0))
            fib_label = best_fib.get('fib_label', '')
            fib_from  = best_fib.get('fib_from', '')
            # Jadikan label utama di strength
            if fib_label:
                if final_score >= SP['sr_score_very_strong']:
                    strength_label = f'{fib_label} (Sangat Kuat)'
                elif final_score >= SP['sr_score_strong']:
                    strength_label = f'{fib_label} (Kuat)'
                else:
                    strength_label = fib_label

        # Volume label untuk display
        if vol_ratio >= 3.0:   vol_label = 'Volume Sangat Tinggi'
        elif vol_ratio >= 2.0: vol_label = 'Volume Tinggi'
        elif vol_ratio >= 1.5: vol_label = 'Volume Normal+'
        elif vol_ratio < 0.5:  vol_label = 'Volume Tipis'
        else:                  vol_label = ''

        scored.append({
            'price'    : round_price(rep_price, price),
            'low'      : round_price(zone_low, price),
            'high'     : round_price(zone_high, price),
            'score'    : final_score,
            'strength' : strength_label,
            'type'     : lvl_type,
            'methods'  : list(methods),
            'is_fresh' : is_fresh,
            'htf_mtf'  : has_htf and has_mtf,
            'has_fib'  : has_fib,
            'fib_label': fib_label,
            'fib_from' : fib_from,
            'touches'  : touches,
            'rejections': rejections,
            'exhaustion': get_zone_exhaustion_label(touches),
            'vol_ratio' : vol_ratio,
            'vol_label' : vol_label,
        })

    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored


# ============================================================
# ZONE BROKEN CHECK
# ============================================================

def is_zone_broken(zone: dict | None, df: pd.DataFrame | None,
                   direction: str) -> bool:
    if zone is None or df is None or len(df) < 3:
        return False
    last_close = df['close'].iloc[-1]
    prev_close = df['close'].iloc[-2]
    if direction == 'support':
        return last_close < zone['low'] and prev_close < zone['low']
    else:
        return last_close > zone['high'] and prev_close > zone['high']


# ============================================================
# DETECT KEY LEVELS — main entry point
# ============================================================

def detect_key_levels(df_higher: pd.DataFrame,
                      df_main: pd.DataFrame,
                      price: float,
                      symbol: str = '',
                      tf: str = ''):
    if df_higher is None or len(df_higher) < 20:
        return None, None, [], []

    atr_h = calc_atr_for_sr(df_higher, 14)
    atr_m = calc_atr_for_sr(df_main, 14) if df_main is not None else None
    if atr_h is None: atr_h = price * 0.02
    if atr_m is None: atr_m = atr_h * 0.4

    swing_lo_h, swing_hi_h = find_swings(df_higher, window=5)
    bull_ob, bear_ob = find_order_blocks(df_higher, atr_h)
    flips    = find_flip_zones(df_higher, atr_h, swing_lo_h, swing_hi_h)
    sweeps   = find_liquidity_sweeps(df_higher, atr_h, swing_lo_h, swing_hi_h)
    key_hori = find_key_horizontal_levels(df_higher, price, atr_h)
    struct   = find_structural_sr(df_higher, price, atr_h)

    raw_htf = []
    for sl in swing_lo_h[-10:]:
        raw_htf.append({'price': sl['price'], 'low': sl['price']-atr_h*0.4,
                         'high': sl['price']+atr_h*0.4, 'score': 10,
                         'type': 'support', 'method': 'htf_swing_lo', 'idx': sl['idx']})
    for sh in swing_hi_h[-10:]:
        raw_htf.append({'price': sh['price'], 'low': sh['price']-atr_h*0.4,
                         'high': sh['price']+atr_h*0.4, 'score': 10,
                         'type': 'resistance', 'method': 'htf_swing_hi', 'idx': sh['idx']})

    for ob in bull_ob:
        ob_score = 12
        if ob.get('fresh', False): ob_score += 5
        elif ob.get('partially_mitigated', False): ob_score += 2
        if ob.get('vol_ratio', 1) >= 1.5: ob_score += 3
        raw_htf.append({**ob, 'price': ob.get('mid', (ob['low']+ob['high'])/2),
                        'score': ob_score, 'type': 'support', 'method': 'htf_ob'})
    for ob in bear_ob:
        ob_score = 12
        if ob.get('fresh', False): ob_score += 5
        elif ob.get('partially_mitigated', False): ob_score += 2
        if ob.get('vol_ratio', 1) >= 1.5: ob_score += 3
        raw_htf.append({**ob, 'price': ob.get('mid', (ob['low']+ob['high'])/2),
                        'score': ob_score, 'type': 'resistance', 'method': 'htf_ob'})

    for f in flips:
        raw_htf.append({**f, 'type': f.get('role', 'support'),
                        'method': 'htf_flip', 'idx': f.get('last_idx', 0)})
    for s in sweeps:
        raw_htf.append({'price': s['price'], 'low': s['price']-atr_h*0.4,
                         'high': s['price']+atr_h*0.4,
                         'score': int(s['strength']), 'type': s['type'],
                         'method': 'htf_sweep', 'idx': s.get('sweep_idx', 0)})
    for kl in key_hori:
        kl['method'] = 'htf_' + kl.get('method', 'key')
        raw_htf.append(kl)
    for sl in struct:
        sl['method'] = 'htf_' + sl.get('method', 'structural')
        raw_htf.append(sl)

    # ── FIBONACCI RETRACEMENT (HTF) ───────────────────────────────────────
    # Hitung Fibonacci dari impulse move HTF.
    # Ini adalah support/resistance PALING BERMAKNA — di mana harga retest
    # setelah impulse move, bukan area konsolidasi sideways.
    fib_htf = find_fibonacci_levels(df_higher, price, atr_h, swing_lo_h, swing_hi_h)
    for fib in fib_htf:
        fib['method'] = 'htf_' + fib['method']
        raw_htf.append(fib)

    raw_mtf = []
    if df_main is not None and len(df_main) >= 20:
        swing_lo_m, swing_hi_m = find_swings(df_main, window=3)
        key_m    = find_key_horizontal_levels(df_main, price, atr_m)
        struct_m = find_structural_sr(df_main, price, atr_m)
        bull_ob_m, bear_ob_m = find_order_blocks(df_main, atr_m)
        for sl in swing_lo_m[-8:]:
            raw_mtf.append({'price': sl['price'], 'low': sl['price']-atr_m*0.4,
                             'high': sl['price']+atr_m*0.4, 'score': 8,
                             'type': 'support', 'method': 'mtf_swing_lo', 'idx': sl['idx']})
        for sh in swing_hi_m[-8:]:
            raw_mtf.append({'price': sh['price'], 'low': sh['price']-atr_m*0.4,
                             'high': sh['price']+atr_m*0.4, 'score': 8,
                             'type': 'resistance', 'method': 'mtf_swing_hi', 'idx': sh['idx']})
        for ob in bull_ob_m:
            if ob.get('fresh', False):
                raw_mtf.append({**ob, 'price': ob.get('mid', (ob['low']+ob['high'])/2),
                                'score': 10, 'type': 'support', 'method': 'mtf_ob'})
        for ob in bear_ob_m:
            if ob.get('fresh', False):
                raw_mtf.append({**ob, 'price': ob.get('mid', (ob['low']+ob['high'])/2),
                                'score': 10, 'type': 'resistance', 'method': 'mtf_ob'})
        for kl in key_m: raw_mtf.append(kl)
        for sl in struct_m: raw_mtf.append(sl)

        # ── FIBONACCI RETRACEMENT (MTF) ───────────────────────────────────
        fib_mtf = find_fibonacci_levels(df_main, price, atr_m, swing_lo_m, swing_hi_m)
        for fib in fib_mtf:
            fib['method'] = 'mtf_' + fib['method']
            raw_mtf.append(fib)

    all_raw = raw_htf + raw_mtf
    if not all_raw:
        return None, None, [], []

    scored = cluster_and_score(
        all_raw, atr_cluster=atr_h, atr_validate=atr_m,
        price=price, df=df_main, symbol=symbol
    )

    # ── FILTER MINIMUM JARAK ──────────────────────────────────────────────
    # Support yang terlalu dekat dengan harga sekarang bukan support nyata.
    # Ini adalah penyebab bot menampilkan "support $55.04" padahal harga $55.10.
    #
    # Aturan:
    #   Support: zona top (high) harus < harga - 0.3 ATR
    #            DAN rep_price harus < harga - 1.0 ATR (jarak bermakna)
    #   Resistance: zona bottom (low) harus > harga + 0.3 ATR
    #               DAN rep_price harus > harga + 1.0 ATR
    #
    # Kalau tidak ada support yang cukup jauh, bot akan report "tidak ada level"
    # daripada memberikan level palsu yang membingungkan.
    min_meaningful_dist = max(atr_m * 1.0, price * 0.008)  # min 1 ATR atau 0.8%

    supports = sorted(
        [z for z in scored
         if z['type'] == 'support'
         and z['price'] < price - min_meaningful_dist * 0.5   # rep cukup jauh
         and z['high']  < price - atr_m * 0.3                 # zone top jelas di bawah
         ],
        key=lambda x: x['price'], reverse=True
    )

    resistances = sorted(
        [z for z in scored
         if z['type'] == 'resistance'
         and z['price'] > price + min_meaningful_dist * 0.5   # rep cukup jauh
         and z['low']   > price + atr_m * 0.3                 # zone bottom jelas di atas
         ],
        key=lambda x: x['price']
    )

    ks = supports[0]    if supports    else None
    kr = resistances[0] if resistances else None
    return ks, kr, resistances[:5], supports[:5]
