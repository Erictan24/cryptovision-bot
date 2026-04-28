"""
signal_generator.py — Entry signal generation.

FOKUS SIGNAL BERKUALITAS — BUANG HIGH RISK

ATURAN FINAL berdasarkan 20+ backtest run (15K+ trades):

SCORE DEAD ZONE (HARD REJECT):
  Score 24: EV -0.40, Score 27: EV -0.58
  Score >= 24 di-REJECT langsung kecuali:
    - 0 kills DAN rejection strength >= 4 (konfirmasi sangat kuat)
    Itupun maksimal MODERATE, tidak boleh GOOD

KILL FACTOR:
  No kills  WR 42.9% EV +0.30 — jauh lebih baik
  Has kills WR 32.9% EV -0.00
  GOOD = max 1 kill, IDEAL = 0 kill

REJECTION GATE:
  Strength 0-2 → downgrade semua tier (bukan konfirmasi nyata)
  Strength 3   → ok, score +1
  Strength 4   → kuat, score +2, GOOD→IDEAL
  Strength 5   → sangat kuat, score +3, upgrade satu level

LONG PROTECTION:
  LONG konsisten lebih buruk dari SHORT (EV -0.12 vs +0.25)
  LONG butuh rejection strength >= 3 (SHORT cukup >= 2... tapi sudah dinaikkan ke 3)
  15m LONG max MODERATE — konfirmasi HTF tidak cukup di 15m
"""

import time
import numpy as np
import pandas as pd

from config import SIGNAL_PARAMS as _GLOBAL_SP
from indicators import calc_ema, analyze_ema_trend

# Volume delta & level memory — optional, fail silently
try:
    from volume_delta import analyze_delta_at_zone, detect_delta_divergence, detect_absorption
    _DELTA_AVAILABLE = True
except ImportError:
    _DELTA_AVAILABLE = False

try:
    from level_memory import get_level_memory
    _MEMORY_AVAILABLE = True
except ImportError:
    _MEMORY_AVAILABLE = False

# SL post-mortem learned patterns — load sekali saat startup
import json as _json
import os as _os

_SL_PATTERNS: dict = {}
_IS_BACKTEST: bool = False   # diset True oleh BacktestEngine agar session filter di-skip

def set_backtest_mode(enabled: bool):
    global _IS_BACKTEST
    _IS_BACKTEST = enabled

def _load_sl_patterns():
    global _SL_PATTERNS
    try:
        path = _os.path.join(_os.path.dirname(__file__), 'data', 'sl_patterns.json')
        if _os.path.exists(path):
            with open(path) as f:
                _SL_PATTERNS = _json.load(f)
    except Exception:
        _SL_PATTERNS = {}

_load_sl_patterns()

SP = _GLOBAL_SP


def set_active_sp(override):
    global SP
    if override is None:
        SP = _GLOBAL_SP
    else:
        SP = override


# ============================================================
# FRESH EMA CROSS DETECTOR (anti-flip veto)
# ============================================================

def _detect_fresh_ema_cross(df_main, max_bars: int = 5) -> dict:
    """
    Deteksi fresh EMA 8/21 cross dalam max_bars terakhir.

    Bug LINK (2026-04-24): SHORT signal trigger meski Golden Cross fresh +
    slope EMA8 naik. Macro context bearish tapi local momentum reversed.
    Veto SHORT setelah fresh Golden Cross (dan mirror untuk LONG/Death Cross)
    untuk hindari fight nascent reversal.

    Returns:
        {'type': 'GOLDEN'/'DEATH'/None, 'bars_ago': int, 'slope_up': bool}
    """
    if df_main is None or len(df_main) < 22:
        return {'type': None, 'bars_ago': 999, 'slope_up': False, 'slope_down': False}

    cls = df_main['close']
    e8 = calc_ema(cls, 8)
    e21 = calc_ema(cls, 21)

    cross_type = None
    bars_ago = 999
    for i in range(1, min(max_bars + 1, len(e8))):
        prev_diff = e8.iloc[-i - 1] - e21.iloc[-i - 1]
        curr_diff = e8.iloc[-i] - e21.iloc[-i]
        if prev_diff <= 0 and curr_diff > 0:
            cross_type = 'GOLDEN'
            bars_ago = i
            break
        if prev_diff >= 0 and curr_diff < 0:
            cross_type = 'DEATH'
            bars_ago = i
            break

    slope_e8 = e8.iloc[-1] - e8.iloc[-3] if len(e8) >= 3 else 0
    return {
        'type': cross_type,
        'bars_ago': bars_ago,
        'slope_up': slope_e8 > 0,
        'slope_down': slope_e8 < 0,
    }


# ============================================================
# DIRECTION SCORER
# ============================================================

def _score_direction(direction, zone, ema_trend, structure, htf_ema,
                     rsi, atr, price, smc, df_main):
    score = 0
    conf  = []
    kills = []
    is_long = direction == 'LONG'

    # EMA
    if ema_trend in ("STRONG_UP","UP") if is_long else ema_trend in ("STRONG_DOWN","DOWN"):
        conf.append(f"EMA {'bullish' if is_long else 'bearish'}"); score += SP['score_ema_strong']
    elif (ema_trend == "WEAK_UP" if is_long else ema_trend == "WEAK_DOWN"):
        conf.append(f"EMA slight {'bullish' if is_long else 'bearish'}"); score += SP['score_ema_slight']
    elif ema_trend in ("STRONG_DOWN","DOWN") if is_long else ema_trend in ("STRONG_UP","UP"):
        kills.append(f"EMA {'bearish' if is_long else 'bullish'} kuat melawan {direction}")

    # Struktur
    if structure == ("UPTREND" if is_long else "DOWNTREND"):
        conf.append(f"Struktur {'HH/HL' if is_long else 'LH/LL'}"); score += SP['score_structure']
    elif structure == ("DOWNTREND" if is_long else "UPTREND"):
        kills.append(f"Struktur {'LH/LL' if is_long else 'HH/HL'} melawan {direction}")

    # S&R zone
    sr_score = zone.get('score', 0)
    sr_str   = zone.get('strength', '')
    if sr_score >= SP['sr_score_very_strong'] or 'Sangat Kuat' in sr_str:
        conf.append(sr_str); score += SP['score_sr_very_strong']
    elif sr_score >= SP['sr_score_strong'] or 'Kuat' in sr_str:
        conf.append(sr_str); score += SP['score_sr_strong']
    elif sr_score >= SP['sr_score_weak']:
        conf.append(sr_str); score += SP['score_sr_weak']

    if zone.get('is_fresh', False):
        conf.append("Fresh level (belum ditest)"); score += SP['score_fresh_level']
    if zone.get('htf_mtf', False):
        conf.append("HTF+MTF confluence level"); score += SP['score_htf_mtf_level']

    # BOS / CHoCH
    # FIX #2 (2026-04-11): BOS 1H tanpa HTF BOS = false breakout risk
    # Data post-mortem 179 trades: BOS 1H tanpa HTF BOS punya WR 22% (7 SL / 9 trades).
    # Jadi BOS 1H HANYA valid kalau HTF BOS juga searah. Kalau tidak ada HTF BOS
    # tapi BOS 1H ada, ini jadi KILL factor (false breakout warning).
    bc  = smc.get('bos_choch', {})
    hbc = smc.get('htf_bos', {})
    _bos_1h_aligned  = bc.get('bos') == ('BULLISH' if is_long else 'BEARISH')
    _htf_bos_aligned = hbc.get('bos') == ('BULLISH' if is_long else 'BEARISH')
    if _bos_1h_aligned and _htf_bos_aligned:
        conf.append(f"BOS {'Bullish' if is_long else 'Bearish'} + HTF konfirmasi"); score += SP['score_bos']
    elif _bos_1h_aligned and not _htf_bos_aligned:
        # BOS 1H saja tanpa HTF = kemungkinan besar false breakout
        kills.append(f"BOS {'Bullish' if is_long else 'Bearish'} 1H tanpa HTF — false breakout risk")
    if bc.get('choch') == ('BULLISH' if is_long else 'BEARISH'):
        conf.append(f"CHoCH {'Bullish' if is_long else 'Bearish'} — reversal!"); score += SP['score_choch']
    if bc.get('choch') == ('BEARISH' if is_long else 'BULLISH'):
        kills.append(f"CHoCH {'Bearish' if is_long else 'Bullish'} melawan {direction}")

    # HTF BOS / CHoCH
    if _htf_bos_aligned:
        conf.append(f"HTF BOS {'Bullish' if is_long else 'Bearish'}"); score += SP['score_htf_bos']
    if hbc.get('choch') == ('BULLISH' if is_long else 'BEARISH'):
        conf.append(f"HTF CHoCH {'Bullish' if is_long else 'Bearish'}"); score += SP['score_htf_choch']
    if hbc.get('bos') == ('BEARISH' if is_long else 'BULLISH'):
        kills.append(f"HTF BOS {'Bearish' if is_long else 'Bullish'} melawan {direction}")
    if hbc.get('choch') == ('BEARISH' if is_long else 'BULLISH'):
        kills.append(f"HTF CHoCH {'Bearish' if is_long else 'Bullish'} melawan {direction}")

    # Market phase
    # FIX #1 (2026-04-11): Accumulation BUKAN green light untuk LONG.
    # Data post-mortem 179 trades: 13 dari 13 LONG SL punya tag "Accumulation" (100%!).
    # Akumulasi = proses lama, harga bisa sideways/turun lama sebelum breakout.
    # HANYA MARKUP yang confirm trend naik sedang aktif. Mirror untuk SHORT.
    phase = smc.get('phase', {}).get('phase', 'UNKNOWN')
    if phase == ('MARKUP' if is_long else 'MARKDOWN'):
        conf.append(f"Fase {'Markup' if is_long else 'Markdown'} — trend aktif"); score += SP['score_market_phase']
    elif phase == ('ACCUMULATION' if is_long else 'DISTRIBUTION'):
        # Akumulasi untuk LONG / Distribusi untuk SHORT = "belum siap" = JEBAKAN
        kills.append(f"Fase {'Akumulasi' if is_long else 'Distribusi'} — belum breakout, tunggu {'Markup' if is_long else 'Markdown'}")
    elif phase == ('DISTRIBUTION' if is_long else 'ACCUMULATION'):
        kills.append(f"Fase {'Distribusi' if is_long else 'Akumulasi'} melawan {direction}")
    elif phase == ('MARKDOWN' if is_long else 'MARKUP'):
        kills.append(f"Fase {'Markdown' if is_long else 'Markup'} melawan {direction}")

    # Order flow
    of = smc.get('order_flow', {})
    of_bias  = of.get('bias', 'NEUTRAL')
    of_score = of.get('score', 0)
    if of_bias == ('BULLISH' if is_long else 'BEARISH'):
        conf.append(f"Order Flow {'Bullish' if is_long else 'Bearish'}"); score += SP['score_order_flow']
    elif of_bias == ('BEARISH' if is_long else 'BULLISH') and abs(of_score) > SP['order_flow_bias_threshold']:
        kills.append(f"Order Flow {'Bearish' if is_long else 'Bullish'} melawan {direction}")

    # Premium/Discount
    pd_zone = smc.get('pd_zone', {}).get('zone', 'EQUILIBRIUM')
    if pd_zone == ('DISCOUNT' if is_long else 'PREMIUM'):
        conf.append(f"{'Deep Discount' if is_long else 'Deep Premium'} Zone"); score += SP['score_pd_zone']
    elif pd_zone == ('SLIGHT_DISCOUNT' if is_long else 'SLIGHT_PREMIUM'):
        conf.append(f"Slight {'Discount' if is_long else 'Premium'}"); score += SP['score_pd_zone_slight']
    elif pd_zone == ('PREMIUM' if is_long else 'DISCOUNT'):
        kills.append(f"{'Premium' if is_long else 'Discount'} Zone melawan {direction}")

    # Volume divergence
    vol_div = smc.get('vol_div', {}).get('divergence')
    if vol_div == ('BULLISH' if is_long else 'BEARISH'):
        conf.append(f"Vol Divergence {'Bullish' if is_long else 'Bearish'}"); score += SP['score_vol_div']
    elif vol_div == ('CONFIRM_BULL' if is_long else 'CONFIRM_BEAR'):
        conf.append(f"Volume konfirmasi {'bullish' if is_long else 'bearish'}"); score += SP['score_vol_confirm']
    elif vol_div == ('CONFIRM_BEAR' if is_long else 'CONFIRM_BULL'):
        kills.append(f"Volume konfirmasi {'bearish' if is_long else 'bullish'} melawan {direction}")

    # RSI
    # Pelajaran dari 3.612 trades historis:
    #   RSI <28 (LONG) = jebakan oversold — momentum bearish terlalu kuat, WR 19%
    #   RSI >72 (SHORT) = jebakan overbought — momentum bullish terlalu kuat, WR 20%
    #   RSI 40-60 = zona terbaik, WR 25%+
    rsi_extreme_low  = SP.get('rsi_extreme_low', 28)
    rsi_extreme_high = SP.get('rsi_extreme_high', 72)
    rsi_trapped = False
    if rsi <= rsi_extreme_low and is_long:
        kills.append(f"RSI {rsi:.0f} — terlalu oversold, momentum bearish masih kuat")
        rsi_trapped = True
    elif rsi >= rsi_extreme_high and not is_long:
        kills.append(f"RSI {rsi:.0f} — terlalu overbought, momentum bullish masih kuat")
        rsi_trapped = True

    if not rsi_trapped:
        if (rsi <= SP['rsi_very_oversold'] and is_long) or (rsi >= 100-SP['rsi_very_oversold'] and not is_long):
            conf.append(f"RSI sangat {'oversold' if is_long else 'overbought'} ({rsi:.0f})"); score += SP['score_rsi_very_oversold']
        elif (rsi <= SP['rsi_oversold'] and is_long) or (rsi >= 100-SP['rsi_oversold'] and not is_long):
            conf.append(f"RSI {'oversold' if is_long else 'overbought'} ({rsi:.0f})"); score += SP['score_rsi_oversold']
        elif (rsi >= SP['rsi_overbought'] and is_long) or (rsi <= 100-SP['rsi_overbought'] and not is_long):
            kills.append(f"RSI {'overbought' if is_long else 'oversold'} ({rsi:.0f}) melawan {direction}")
        elif rsi >= SP['rsi_near_overbought'] and is_long:
            score -= 3
            kills.append(f"RSI {rsi:.0f} — near overbought, LONG berisiko tinggi")
        elif rsi <= (100 - SP['rsi_near_overbought']) and not is_long:
            score -= 3
            kills.append(f"RSI {rsi:.0f} — near oversold, SHORT berisiko tinggi")

    # HTF EMA
    if htf_ema in ("STRONG_UP","UP") if is_long else htf_ema in ("STRONG_DOWN","DOWN"):
        conf.append(f"HTF EMA {'bullish' if is_long else 'bearish'}"); score += SP['score_htf_ema']
    elif htf_ema in ("STRONG_DOWN","DOWN") if is_long else htf_ema in ("STRONG_UP","UP"):
        kills.append(f"HTF EMA {'bearish' if is_long else 'bullish'} melawan {direction}")

    # RSI Divergence
    rsi_div = smc.get('rsi_div', {}).get('type')
    if rsi_div == ('BULLISH_DIV' if is_long else 'BEARISH_DIV'):
        conf.append(f"RSI {'Bullish' if is_long else 'Bearish'} Divergence"); score += SP['score_rsi_div']
    elif rsi_div == ('HIDDEN_BULL' if is_long else 'HIDDEN_BEAR'):
        conf.append(f"Hidden {'Bullish' if is_long else 'Bearish'} Div"); score += SP['score_hidden_div']
    elif rsi_div == ('BEARISH_DIV' if is_long else 'BULLISH_DIV'):
        kills.append(f"RSI {'Bearish' if is_long else 'Bullish'} Divergence melawan {direction}")

    # Confirmation candle (scoring — gate keras di generate_entry_signal)
    from smc_analyzer import detect_confirmation_candle
    if df_main is not None and zone:
        cc = detect_confirmation_candle(df_main, zone['low'], zone['high'], direction, atr)
        if cc['confirmed']:
            conf.append(cc['pattern']); score += min(cc['score'], SP['score_candle_pattern'])

    # FVG
    fvg = smc.get('fvg', {})
    for fg in fvg.get('bull_fvg' if is_long else 'bear_fvg', [])[:2]:
        if abs(fg.get('mid', 0) - price) < atr * 2:
            conf.append(f"{'Bullish' if is_long else 'Bearish'} FVG nearby"); score += SP['score_fvg']; break

    # Derivatives
    deriv = smc.get('derivatives', {})
    if deriv.get('available'):
        fr = deriv.get('funding_rate', 0)
        fb = deriv.get('funding_bias', 'NEUTRAL')
        lb = deriv.get('lsr_bias', 'NEUTRAL')
        ob = deriv.get('oi_bias', 'NEUTRAL')
        fund_match = fb in ('BULLISH','SLIGHT_BULL') if is_long else fb in ('BEARISH','SLIGHT_BEAR')
        if fund_match:
            conf.append(f"Funding {'negatif' if is_long else 'positif'} ({fr:+.4f}%)"); score += SP['score_derivatives_fund']
        if (fr > 0.05 and is_long) or (fr < -0.03 and not is_long):
            kills.append(f"Funding ekstrem ({fr:+.4f}%)")
        if lb == ('CROWD_SHORT' if is_long else 'CROWD_LONG'):
            conf.append(f"LSR {deriv.get('lsr',0):.2f} — squeeze potential"); score += SP['score_derivatives_lsr']
        elif lb == ('CROWD_LONG' if is_long else 'CROWD_SHORT'):
            kills.append(f"LSR {deriv.get('lsr',0):.2f} — too many {'long' if is_long else 'short'}")
        if ob == 'RISING':
            conf.append(f"OI naik {deriv.get('oi_change_pct',0):+.1f}%"); score += SP['score_derivatives_oi']

    # Candlestick patterns
    cp           = smc.get('candle_patterns', [])
    match_pats   = [p for p in cp if p['direction'] == ('BULL' if is_long else 'BEAR')]
    against_pats = [p for p in cp if p['direction'] == ('BEAR' if is_long else 'BULL')]
    if match_pats:
        best = max(match_pats, key=lambda x: x['strength'])
        conf.append(f"{best['pattern']} detected"); score += min(best['strength'], 3)
    if against_pats and any(p['strength'] >= 3 for p in against_pats):
        best = max(against_pats, key=lambda x: x['strength'])
        kills.append(f"{'Bearish' if is_long else 'Bullish'} {best['pattern']} melawan {direction}")

    # Liquidation zones
    liq = smc.get('liquidation', {})
    if (is_long and liq.get('liq_bias') == 'SHORT_VULNERABLE') or \
       (not is_long and liq.get('liq_bias') == 'LONG_VULNERABLE'):
        conf.append(f"{'Short' if is_long else 'Long'} rentan liquidasi"); score += SP['score_liq_zones']

    # Order Block proximity — konfirmasi institusi
    # Harga di area OB = pending institutional orders = bounce lebih reliable
    ob_data = smc.get('order_blocks', {})
    if is_long and ob_data.get('at_bull_ob', False):
        near_obs = ob_data.get('near_bull', [])
        if near_obs:
            best_ob = max(near_obs, key=lambda x: x.get('vol_ratio', 1))
            vol_r   = best_ob.get('vol_ratio', 1)
            conf.append(f"Bullish Order Block — institusi beli di sini (vol {vol_r:.1f}x)")
            score += SP.get('score_order_block', 3)
    elif not is_long and ob_data.get('at_bear_ob', False):
        near_obs = ob_data.get('near_bear', [])
        if near_obs:
            best_ob = max(near_obs, key=lambda x: x.get('vol_ratio', 1))
            vol_r   = best_ob.get('vol_ratio', 1)
            conf.append(f"Bearish Order Block — institusi jual di sini (vol {vol_r:.1f}x)")
            score += SP.get('score_order_block', 3)

    return score, conf, kills


# ============================================================
# BUILD ENTRY
# ============================================================

def _build_entry(direction, zone, price, atr, res_mtf, sup_mtf, liq_pools):
    is_long    = direction == 'LONG'
    zone_range = zone['high'] - zone['low']
    if is_long:
        entry = zone['high'] - zone_range * SP['entry_depth_pct']
        sl    = max(zone['low'] - atr * SP['sl_atr_buffer'], 0)
    else:
        entry = zone['low'] + zone_range * SP['entry_depth_pct']
        sl    = zone['high'] + atr * SP['sl_atr_buffer']
    risk = abs(entry - sl) or atr * 0.5

    # ── Kumpulkan obstacles (resistance untuk LONG, support untuk SHORT) ──
    # Ini adalah level yang HARUS DIHINDARI oleh TP
    obstacles = []
    if is_long:
        for r in res_mtf:
            lvl = r['low']
            if lvl > entry:
                obstacles.append(lvl)
        obstacles.sort()  # ascending — yang paling dekat duluan
    else:
        for s in sup_mtf:
            lvl = s['high']
            if lvl < entry:
                obstacles.append(lvl)
        obstacles.sort(reverse=True)  # descending — yang paling dekat duluan

    # Resistance/support terdekat di atas entry (untuk LONG)
    nearest_obstacle = obstacles[0] if obstacles else None

    # ── TP Candidates dari level S&R ──────────────────────────
    tp_candidates = []
    if is_long:
        for r in res_mtf:
            lvl = r['low']
            if lvl <= entry:
                continue  # harus di atas entry
            rr = (lvl - entry) / risk
            # TP harus di BAWAH resistance — ambil 95% dari level
            # agar tidak hit resist dan langsung balik
            tp_price = lvl * 0.998
            if rr > 0.5:
                tp_candidates.append({'price': tp_price, 'rr': rr, 'label': 'Bawah Resist'})
        for pool in liq_pools.get('eqh', []):
            lvl = pool['price']
            if lvl <= entry:
                continue
            rr = (lvl - entry) / risk
            if rr > 0.5:
                tp_candidates.append({'price': lvl, 'rr': rr, 'label': 'Liq Pool EQH'})
    else:
        for s in sup_mtf:
            lvl = s['high']
            if lvl >= entry:
                continue  # harus di bawah entry
            rr = (entry - lvl) / risk
            # TP harus di ATAS support — ambil 100.2% dari level
            tp_price = lvl * 1.002
            if rr > 0.5:
                tp_candidates.append({'price': tp_price, 'rr': rr, 'label': 'Atas Support'})
        for pool in liq_pools.get('eql', []):
            lvl = pool['price']
            if lvl >= entry:
                continue
            rr = (entry - lvl) / risk
            if rr > 0.5:
                tp_candidates.append({'price': lvl, 'rr': rr, 'label': 'Liq Pool EQL'})

    tp_candidates.sort(key=lambda x: x['rr'])

    # ── Pilih TP1 ─────────────────────────────────────────────
    # TP1 default = tp1_rr_min, tapi bisa di-cap oleh resistance terdekat.
    # TP1 enforcement (reject kalau resistance terlalu dekat) TIDAK dipakai —
    # terlalu agresif, kill 90% signal karena S&R level biasanya dekat satu sama lain.
    _tp1_min = SP.get('tp1_rr_min', 1.5)
    _tp1_max = SP.get('tp1_rr_max', 2.0)
    tp1       = entry + risk * _tp1_min if is_long else entry - risk * _tp1_min
    tp1_label = f"{_tp1_min}R"

    for c in tp_candidates:
        if _tp1_min <= c['rr'] <= _tp1_max:
            candidate_price = c['price']
            clear_path = True
            if is_long and nearest_obstacle:
                if candidate_price >= nearest_obstacle:
                    clear_path = False
            elif not is_long and nearest_obstacle:
                if candidate_price <= nearest_obstacle:
                    clear_path = False

            if clear_path:
                tp1 = candidate_price
                tp1_label = c['label']
                break

    # Cap TP1 ke resistance/support terdekat jika melewati.
    # Tapi skip cap kalau hasilnya < 0.6R (obstacle terlalu dekat entry,
    # bikin TP1 tipis gak berguna). Lebih baik biarkan TP1 default 1.2R —
    # kalau harga stuck di obstacle, SL kena; kalau breakout, TP1 dapat profit layak.
    _min_rr_after_cap = 0.6
    if nearest_obstacle and is_long and tp1 >= nearest_obstacle:
        capped_tp1 = nearest_obstacle * 0.997
        capped_rr  = (capped_tp1 - entry) / risk
        if capped_rr >= _min_rr_after_cap:
            tp1 = capped_tp1
            tp1_label = "Bawah Resist"
    elif nearest_obstacle and not is_long and tp1 <= nearest_obstacle:
        capped_tp1 = nearest_obstacle * 1.003
        capped_rr  = (entry - capped_tp1) / risk
        if capped_rr >= _min_rr_after_cap:
            tp1 = capped_tp1
            tp1_label = "Atas Support"

    # ── Pilih TP2 ─────────────────────────────────────────────
    # TP2 boleh lebih jauh, tapi tetap harus reasonable
    tp2       = entry + risk * 2.0 if is_long else entry - risk * 2.0
    tp2_label = "2R"

    for c in tp_candidates:
        if SP['tp2_rr_min'] <= c['rr'] <= SP['tp2_rr_max']:
            candidate_price = c['price']
            # TP2 boleh di atas resistance — itu memang target jangka lebih jauh
            if candidate_price != tp1:  # tidak sama dengan TP1
                tp2 = candidate_price
                tp2_label = c['label']
                break

    # TP2 harus lebih jauh dari TP1
    if (is_long and tp2 <= tp1) or (not is_long and tp2 >= tp1):
        tp2 = (tp1 + risk) if is_long else (tp1 - risk)
        tp2_label = "TP1+1R"

    return {
        'entry': round(entry,8), 'sl': round(sl,8),
        'tp1': round(tp1,8), 'tp2': round(tp2,8),
        'rr1': round(abs(tp1-entry)/risk,2),
        'rr2': round(abs(tp2-entry)/risk,2),
        'rr':  round(abs(tp2-entry)/risk,2),
        'sl_pct': round((risk/max(price,1))*100,2),
        'tp1_label': tp1_label, 'tp2_label': tp2_label,
    }


# ============================================================
# QUALITY DETERMINATION
# ============================================================

def _determine_quality(score, n_kills):
    """
    Map score + kills → quality.

    IDEAL dinonaktifkan — semua signal bagus masuk GOOD.
    Hard reject score >= score_hard_reject (24) — dead zone konsisten.
    """
    # Hard reject PERTAMA — sebelum apapun
    # Score 24+ = faktor lemah menumpuk, bukan setup bagus
    # Data: score 24 EV -0.05 borderline, score 27 EV -1.00 selalu mati
    score_hard_reject = SP.get('score_hard_reject', 24)
    if score >= score_hard_reject:
        return None

    if n_kills >= SP['max_kills_hard_reject']:
        return None

    score_cap_good = SP.get('score_cap_good', 24)

    # IDEAL dinonaktifkan — score_ideal=999 di config
    # Semua signal masuk GOOD atau MODERATE
    if score >= SP['score_ideal'] and n_kills <= SP['max_kills_ideal']:
        q = 'IDEAL'
    elif (score >= SP['score_good'] and score < score_cap_good
          and n_kills <= SP['max_kills_good']):
        q = 'GOOD'
    elif score >= SP['score_moderate'] and n_kills <= SP['max_kills_moderate']:
        q = 'MODERATE'
    elif score >= SP['score_wait'] and n_kills == 0:
        q = 'WAIT'
    else:
        return None

    # max_kills_good = 0: GOOD wajib bersih, 1 kill langsung turun MODERATE
    # max_kills_moderate = 1: MODERATE toleransi 1 kill, 2 kills = reject
    if n_kills == 1 and q == 'IDEAL':   q = 'GOOD'
    elif n_kills == 1 and q == 'GOOD':  q = 'MODERATE'  # downgrade, meski max_kills_good=0 sudah cegah ini
    elif n_kills >= 2:                  return None      # safety net — sudah dicegah di max_kills_hard_reject
    return q


# ============================================================
# REJECTION GATE
# ============================================================

def _apply_rejection_gate(q, conf, score, rj, zone_label, direction=''):
    """
    Gate keras — wajib ada rejection nyata sebelum entry.

    ATURAN:
      Strength 0-2 → TIDAK DITERIMA
        IDEAL/GOOD → MODERATE
        MODERATE   → WAIT

      Strength 3 → DITERIMA minimal (Hammer, Shooting Star, Rejection Wick)
        score += 1
        Tier TIDAK berubah — naik tier harus dari base scoring

      Strength 4 → DITERIMA kuat (Pin Bar, Engulfing, Tweezer, Morning/Evening Star)
        score += 2
        Tier TIDAK berubah

      Strength 5 → DITERIMA sangat kuat (Engulfing + volume, Pin Bar sangat kuat)
        score += 3
        Tier TIDAK berubah

    PENTING: Rejection gate TIDAK menaikkan tier (tidak ada GOOD→IDEAL).
    Tier IDEAL hanya datang dari base scoring murni.
    Data: GOOD→IDEAL upgrade via rejection gate menghasilkan EV -0.50 konsisten.
    """
    if rj is None:
        return q, conf, score

    strength = rj.get('strength', 0)

    if not rj['confirmed'] or strength <= 2:
        # Tidak ada rejection atau terlalu lemah
        # 2026-04-29 VOLUME UPGRADE: MODERATE TIDAK lagi di-downgrade ke WAIT
        # — terlalu strict di market sideways. Rejection cuma jadi bonus untuk
        # GOOD tier saja. MODERATE bisa lolos tanpa Pin Bar (target WR 60%).
        if q in ('IDEAL', 'GOOD'):
            q = 'MODERATE'
            if strength > 0 and rj.get('pattern', 'none') != 'none':
                conf.append(f"{rj.get('detail', rj['pattern'])} (lemah — tunggu Pin Bar/Engulfing)")
            else:
                conf.append(f"Menunggu rejection di {zone_label}")
        # MODERATE tier tetap MODERATE meski tanpa rejection — score gate yang filter
    else:
        rj_str = rj.get('detail', rj['pattern'])

        # GOOD tier wajib rejection strength >= 4 (Pin Bar/Engulfing/Tweezer)
        # Strength 3 (Hammer biasa) hanya cukup untuk MODERATE
        # Data konsisten: score 18 + strength 3 = WR 25%, score 21 + strength 4+ = WR 80%
        if strength == 3 and q == 'GOOD':
            q = 'MODERATE'
            conf.append(f"{rj_str} (perlu Pin Bar/Engulfing untuk GOOD)")
        else:
            conf.insert(0, rj_str)

        if strength >= 5:
            score += 3
        elif strength >= 4:
            score += 2
        elif strength >= 3:
            score += 1

    return q, conf, score


# ============================================================
# MAIN
# ============================================================

def generate_entry_signal(
    price, atr, ema_trend, structure,
    ks, kr, res_mtf, sup_mtf, smc,
    rsi=50.0, htf_ema='SIDEWAYS', df_main=None,
    symbol='', adx=20.0, signal_cache=None,
):
    if smc is None:
        smc = {}

    is_trending     = adx >= SP['adx_trending']
    is_strong_trend = adx >= SP['adx_strong_trend']
    if adx < SP['adx_ranging_block'] and ema_trend in ('SIDEWAYS','WEAK_UP','WEAK_DOWN'):
        return None

    # FIX #5 + OPSI B (2026-04-12): Precision ADX filter.
    # Block death zone 35-44 (WR 20-33%) dan >=50 (WR 0%).
    # Allow 45-49 karena historis punya WR 75% (sweet spot kedua).
    # Sweet spot utama 25-34 (WR 67-72%).
    _adx_dz_lo = SP.get('adx_death_zone_low', 35)
    _adx_dz_hi = SP.get('adx_death_zone_high', 45)
    _adx_max   = SP.get('adx_too_extreme', 50)
    if (_adx_dz_lo <= adx < _adx_dz_hi) or adx >= _adx_max:
        return None

    bull_ev = sum([
        ema_trend in ("STRONG_UP","UP"),
        structure == "UPTREND",
        htf_ema in ("STRONG_UP","UP"),
        smc.get('smart_bias')=='BULLISH' and smc.get('confidence',0)>=SP['smc_confidence_min'],
        smc.get('bos_choch',{}).get('bos')=='BULLISH' or
        smc.get('bos_choch',{}).get('choch')=='BULLISH',
    ])
    bear_ev = sum([
        ema_trend in ("STRONG_DOWN","DOWN"),
        structure == "DOWNTREND",
        htf_ema in ("STRONG_DOWN","DOWN"),
        smc.get('smart_bias')=='BEARISH' and smc.get('confidence',0)>=SP['smc_confidence_min'],
        smc.get('bos_choch',{}).get('bos')=='BEARISH' or
        smc.get('bos_choch',{}).get('choch')=='BEARISH',
    ])

    eval_long  = not (bear_ev >= bull_ev + 2)
    eval_short = not (bull_ev >= bear_ev + 2)

    # ── FRESH EMA CROSS VETO (Bug LINK fix, 2026-04-24) ──────────────────
    # Bug: SHORT trigger meski Golden Cross fresh + EMA8 slope naik.
    # Macro context bearish (HTF, structure, SMC) tapi local 1H momentum
    # baru saja flip bullish. Entering SHORT di sini = fight nascent reversal.
    # Hard veto: fresh cross dalam 5 candle + slope searah = no trade
    # melawan arah cross.
    fresh_cross = _detect_fresh_ema_cross(df_main, max_bars=5)
    if fresh_cross['type'] == 'GOLDEN' and fresh_cross['slope_up']:
        eval_short = False
    elif fresh_cross['type'] == 'DEATH' and fresh_cross['slope_down']:
        eval_long = False

    cached      = signal_cache.get(symbol,{}) if signal_cache else {}
    hours_since = (time.time() - cached.get('ts',0)) / 3600 if cached else 999
    cached_dir  = cached.get('dir', '')
    lock_hours  = SP.get('signal_lock_hours', 4)

    # ── ANTI-FLIP FILTER ──────────────────────────────────────────────
    # Kalau ada signal aktif di arah berlawanan yang belum expired:
    # → Blok arah kebalikan
    # Contoh: LONG TRX aktif sejak 6 jam lalu (lock=4h) → SHORT TRX diblok
    # Ini mencegah kebingungan signal flip-flop di coin yang sama
    if cached_dir and hours_since < lock_hours:
        if cached_dir == 'LONG':
            eval_short = False   # ada LONG aktif → jangan SHORT
        elif cached_dir == 'SHORT':
            eval_long  = False   # ada SHORT aktif → jangan LONG

    margin          = atr * SP['zone_margin_atr_mult']
    near_support    = ks and ks['low']-margin <= price <= ks['high']+margin
    near_resistance = kr and kr['low']-margin <= price <= kr['high']+margin

    sig = None

    from smc_analyzer import detect_snr_rejection
    rj_long  = detect_snr_rejection(df_main, ks['low'], ks['high'], 'LONG', atr) \
               if (ks and near_support and eval_long and df_main is not None) else None
    rj_short = detect_snr_rejection(df_main, kr['low'], kr['high'], 'SHORT', atr) \
               if (kr and near_resistance and eval_short and df_main is not None) else None

    # Infer timeframe dari jumlah candle / interval
    is_15m = False
    if df_main is not None and len(df_main) >= 2:
        try:
            diff = (df_main.index[-1] - df_main.index[-2]).total_seconds()
            is_15m = (diff <= 900)
        except Exception:
            is_15m = False

    # ================================================================
    # WHALE FLOW FILTER — ikuti arus, jangan lawan
    #
    # Bukan blok total, tapi DOWNGRADE quality kalau melawan arus.
    # Alasan: kalau whale masih jual tapi ada rejection di support,
    # signal tetap ada tapi hanya MODERATE (tidak GOOD).
    # Trader boleh ignore MODERATE kalau tidak yakin dengan konteks.
    #
    # Logika:
    #   HTF masih bearish + belum ada CHoCH/BOS bullish
    #   → LONG max MODERATE (belum konfirmasi whale berhenti jual)
    #
    #   HTF masih bullish + belum ada CHoCH/BOS bearish
    #   → SHORT max MODERATE (belum konfirmasi whale berhenti beli)
    # ================================================================
    bc     = smc.get('bos_choch', {})
    htf_bc = smc.get('htf_bos', {})

    # Bukti whale sudah berhenti jual (bullish flip)
    # PRO TRADER: butuh MINIMUM 2 konfirmasi, BUKAN hanya 1
    # Alasan: ANY 1 dari 6 kondisi = terlalu banyak false signal
    # Data: whale_stopped_selling dengan 1 konfirmasi → WR 35%, dengan 2+ → WR 62%
    bull_confirmations = sum([
        bc.get('choch') == 'BULLISH',        # CHoCH = terkuat (institusi balik arah)
        bc.get('bos')   == 'BULLISH',        # BOS bullish = struktur pecah ke atas
        htf_bc.get('choch') == 'BULLISH',    # HTF CHoCH = sangat kuat
        htf_bc.get('bos')   == 'BULLISH',    # HTF BOS = trend HTF sudah balik
        htf_ema in ('STRONG_UP', 'UP'),      # HTF EMA bullish
        smc.get('smart_bias') == 'BULLISH' and smc.get('confidence', 0) >= 60,
    ])
    # CHoCH dari manapun = konfirmasi paling kuat, sendirian sudah cukup
    has_choch_bull = (bc.get('choch') == 'BULLISH' or htf_bc.get('choch') == 'BULLISH')
    whale_stopped_selling = has_choch_bull or bull_confirmations >= 2

    # Bukti whale sudah berhenti beli (bearish flip)
    bear_confirmations = sum([
        bc.get('choch') == 'BEARISH',
        bc.get('bos')   == 'BEARISH',
        htf_bc.get('choch') == 'BEARISH',
        htf_bc.get('bos')   == 'BEARISH',
        htf_ema in ('STRONG_DOWN', 'DOWN'),
        smc.get('smart_bias') == 'BEARISH' and smc.get('confidence', 0) >= 60,
    ])
    has_choch_bear = (bc.get('choch') == 'BEARISH' or htf_bc.get('choch') == 'BEARISH')
    whale_stopped_buying = has_choch_bear or bear_confirmations >= 2

    # HTF masih bearish = whale masih jual
    # Cukup salah satu kondisi — tidak perlu keduanya
    # Ini mencegah LONG di tengah downtrend meski ada minor support
    htf_bearish = (
        htf_ema in ('STRONG_DOWN', 'DOWN') or
        structure == 'DOWNTREND'
    )

    # HTF masih bullish = whale masih beli
    htf_bullish = (
        htf_ema in ('STRONG_UP', 'UP') or
        structure == 'UPTREND'
    )

    # ---- Evaluate LONG ----
    if ks and near_support and eval_long:
        score, conf, kills = _score_direction(
            'LONG', ks, ema_trend, structure, htf_ema, rsi, atr, price, smc, df_main
        )
        if is_strong_trend: score += SP['score_adx_strong']
        elif is_trending:   score += SP['score_adx_trending']
        if rsi > SP['rsi_overbought']:
            kills.append(f"RSI {rsi:.0f} overbought")

        # Pro trader: momentum candle terakhir harus tidak kuat berlawanan
        # Kalau candle terakhir besar bearish (body > 1.5x ATR) = momentum DOWN kuat
        # LONG di momentum bearish kuat = melawan arus, WR rendah
        if df_main is not None and len(df_main) >= 2:
            last = df_main.iloc[-1]
            body = abs(float(last.get('close', 0)) - float(last.get('open', 0)))
            if body > atr * 1.5 and float(last.get('close', 0)) < float(last.get('open', 0)):
                kills.append(f"Candle terakhir bearish kuat (body {body/atr:.1f}x ATR) — tunggu stabilisasi")

        n_kills = len(kills)
        q = _determine_quality(score, n_kills)

        if q in ('IDEAL','GOOD') and not is_trending:
            q = 'MODERATE'; conf.append(f"ADX {adx:.0f} — market tidak trending")

        q, conf, score = _apply_rejection_gate(q, conf, score, rj_long, "support", "LONG")

        # ── HTF ALIGNMENT GATE (LONG) — wajib untuk GOOD tier ────────────────
        # Data: GOOD tanpa HTF EMA aligned + tanpa CHoCH → WR turun 10-15%
        # GOOD butuh SALAH SATU: HTF EMA bullish ATAU CHoCH bullish (whale reversal)
        # Jika tidak ada keduanya → cap ke MODERATE (masih bisa trade, tapi lebih hati-hati)
        if q == 'GOOD':
            _htf_aligned_long  = htf_ema in ('STRONG_UP', 'UP')
            _has_choch_bull     = (bc.get('choch') == 'BULLISH' or
                                   htf_bc.get('choch') == 'BULLISH')
            if not _htf_aligned_long and not _has_choch_bull:
                q = 'MODERATE'
                conf.append("HTF EMA belum bullish & belum ada CHoCH — perlu HTF konfirmasi untuk GOOD")

        # Whale flow filter — ikuti trend, jangan lawan
        # Level 1 (HARD BLOCK): HTF bearish + EMA bearish = market masih downtrend kuat
        #   LONG di sini hampir selalu SL karena whale masih aktif jual
        #   Hanya lolos kalau ada CHoCH/BOS bullish = konfirmasi whale BENAR-BENAR sudah berhenti
        if htf_bearish and ema_trend in ('STRONG_DOWN', 'DOWN') and not whale_stopped_selling:
            q = None  # HARD REJECT — double bearish, jangan LONG sama sekali
        # Level 2 (DOWNGRADE): HTF bearish tapi EMA belum jelas atau mixed
        #   Masih bisa MODERATE kalau ada rejection kuat di support
        elif q == 'GOOD' and htf_bearish and not whale_stopped_selling:
            q = 'MODERATE'
            conf.append("HTF masih bearish — tunggu CHoCH/BOS bullish sebelum LONG")

        # ── VOLUME DELTA CHECK — konfirmasi buying pressure di support ──────
        if q and df_main is not None and _DELTA_AVAILABLE:
            try:
                delta_info = analyze_delta_at_zone(df_main, ks['low'], ks['high'], 'LONG', atr)
                d_score = delta_info.get('score', 0)
                d_bias  = delta_info.get('bias', 'NEUTRAL')
                if d_score >= 2:
                    score += d_score
                    conf.append(delta_info['desc'])
                elif d_score <= -2:
                    # Seller masih dominan di support = sangat berisiko LONG
                    if q in ('GOOD', 'IDEAL'):
                        q = 'MODERATE'
                    kills.append(delta_info['desc'])
                    n_kills = len(kills)
                # Absorption — tanda paling kuat
                abs_info = detect_absorption(df_main, 'LONG', atr)
                if abs_info.get('detected'):
                    score += abs_info['strength'] * 2
                    conf.insert(0, abs_info['desc'])
                # Delta divergence
                div_info = detect_delta_divergence(df_main)
                if div_info['type'] == 'BULLISH_DIV':
                    score += div_info['score']
                    conf.append(div_info['desc'])
            except Exception:
                pass

        # ── LEVEL MEMORY CHECK — riwayat level ini ───────────────────────────
        if q and _MEMORY_AVAILABLE and symbol:
            try:
                mem      = get_level_memory()
                mod, mdesc = mem.get_score_modifier(symbol, ks['price'], atr, 'LONG')
                if mod != 0:
                    score += mod
                    if mod > 0:
                        conf.append(mdesc)
                    else:
                        kills.append(mdesc)
                        n_kills = len(kills)
                    q = _determine_quality(score, n_kills)
            except Exception:
                pass

        # Hard reject score akhir
        if q and score >= SP.get('score_hard_reject', 24):
            q = None

        if q:
            geo = _build_entry('LONG', ks, price, atr, res_mtf, sup_mtf, smc.get('liquidity',{}))
            if geo['sl_pct'] > SP['max_sl_pct'] and q not in ('WAIT',):
                q = None
        if q:
            sig = {
                'direction': 'LONG', 'quality': q,
                'entry': geo['entry'], 'sl': geo['sl'],
                'tp1': geo['tp1'], 'tp2': geo['tp2'],
                'rr1': geo['rr1'], 'rr2': geo['rr2'], 'rr': geo['rr2'],
                'sl_pct': geo['sl_pct'],
                'reasons': _build_reasons(conf, kills, q, geo, is_strong_trend, adx),
                'level_used': 'SUPPORT', 'confluence_score': score, 'kill_count': n_kills,
                'entry_low': geo['entry'], 'entry_high': geo['entry'],
                'tp': geo['tp2'], 'tp_max': geo['tp2'], 'rr_max': geo['rr2'],
                'level_price': ks.get('price', 0),
            }

    # ---- Evaluate SHORT ----
    if sig is None and kr and near_resistance and eval_short:
        score, conf, kills = _score_direction(
            'SHORT', kr, ema_trend, structure, htf_ema, rsi, atr, price, smc, df_main
        )
        if is_strong_trend: score += SP['score_adx_strong']
        elif is_trending:   score += SP['score_adx_trending']
        if rsi < 100-SP['rsi_overbought']:
            kills.append(f"RSI {rsi:.0f} oversold")

        # Candle terakhir bullish kuat = momentum UP kuat → SHORT berbahaya
        if df_main is not None and len(df_main) >= 2:
            last = df_main.iloc[-1]
            body = abs(float(last.get('close', 0)) - float(last.get('open', 0)))
            if body > atr * 1.5 and float(last.get('close', 0)) > float(last.get('open', 0)):
                kills.append(f"Candle terakhir bullish kuat (body {body/atr:.1f}x ATR) — tunggu stabilisasi")

        n_kills = len(kills)
        q = _determine_quality(score, n_kills)

        if q in ('IDEAL','GOOD') and not is_trending:
            q = 'MODERATE'; conf.append(f"ADX {adx:.0f} — market tidak trending")

        q, conf, score = _apply_rejection_gate(q, conf, score, rj_short, "resistance", "SHORT")

        # ── HTF ALIGNMENT GATE (SHORT) — wajib untuk GOOD tier ───────────────
        # Mirror logic dari LONG: SHORT butuh HTF EMA bearish ATAU CHoCH bearish
        if q == 'GOOD':
            _htf_aligned_short = htf_ema in ('STRONG_DOWN', 'DOWN')
            _has_choch_bear     = (bc.get('choch') == 'BEARISH' or
                                   htf_bc.get('choch') == 'BEARISH')
            if not _htf_aligned_short and not _has_choch_bear:
                q = 'MODERATE'
                conf.append("HTF EMA belum bearish & belum ada CHoCH — perlu HTF konfirmasi untuk GOOD")

        # Whale flow filter — ikuti trend, jangan lawan
        # Level 1 (HARD BLOCK): HTF bullish + EMA bullish = market masih uptrend kuat
        #   SHORT di sini hampir selalu SL karena whale masih aktif beli
        #   Hanya lolos kalau ada CHoCH/BOS bearish = konfirmasi whale BENAR-BENAR sudah berhenti
        if htf_bullish and ema_trend in ('STRONG_UP', 'UP') and not whale_stopped_buying:
            q = None  # HARD REJECT — double bullish, jangan SHORT sama sekali
        # Level 2 (DOWNGRADE): HTF bullish tapi EMA belum jelas atau mixed
        elif q == 'GOOD' and htf_bullish and not whale_stopped_buying:
            q = 'MODERATE'
            conf.append("HTF masih bullish — tunggu CHoCH/BOS bearish sebelum SHORT")

        # ── VOLUME DELTA CHECK — konfirmasi selling pressure di resistance ───
        if q and df_main is not None and _DELTA_AVAILABLE:
            try:
                delta_info = analyze_delta_at_zone(df_main, kr['low'], kr['high'], 'SHORT', atr)
                d_score = delta_info.get('score', 0)
                if d_score >= 2:
                    score += d_score
                    conf.append(delta_info['desc'])
                elif d_score <= -2:
                    if q in ('GOOD', 'IDEAL'):
                        q = 'MODERATE'
                    kills.append(delta_info['desc'])
                    n_kills = len(kills)
                abs_info = detect_absorption(df_main, 'SHORT', atr)
                if abs_info.get('detected'):
                    score += abs_info['strength'] * 2
                    conf.insert(0, abs_info['desc'])
                div_info = detect_delta_divergence(df_main)
                if div_info['type'] == 'BEARISH_DIV':
                    score += div_info['score']
                    conf.append(div_info['desc'])
            except Exception:
                pass

        # ── LEVEL MEMORY CHECK ────────────────────────────────────────────────
        if q and _MEMORY_AVAILABLE and symbol:
            try:
                mem      = get_level_memory()
                mod, mdesc = mem.get_score_modifier(symbol, kr['price'], atr, 'SHORT')
                if mod != 0:
                    score += mod
                    if mod > 0:
                        conf.append(mdesc)
                    else:
                        kills.append(mdesc)
                        n_kills = len(kills)
                    q = _determine_quality(score, n_kills)
            except Exception:
                pass

        # Hard reject score akhir
        if q and score >= SP.get('score_hard_reject', 24):
            q = None

        if q:
            geo = _build_entry('SHORT', kr, price, atr, res_mtf, sup_mtf, smc.get('liquidity',{}))
            if geo['sl_pct'] > SP['max_sl_pct'] and q not in ('WAIT',):
                q = None
        if q:
            sig = {
                'direction': 'SHORT', 'quality': q,
                'entry': geo['entry'], 'sl': geo['sl'],
                'tp1': geo['tp1'], 'tp2': geo['tp2'],
                'rr1': geo['rr1'], 'rr2': geo['rr2'], 'rr': geo['rr2'],
                'sl_pct': geo['sl_pct'],
                'reasons': _build_reasons(conf, kills, q, geo, is_strong_trend, adx),
                'level_used': 'RESISTANCE', 'confluence_score': score, 'kill_count': n_kills,
                'entry_low': geo['entry'], 'entry_high': geo['entry'],
                'tp': geo['tp2'], 'tp_max': geo['tp2'], 'rr_max': geo['rr2'],
                'level_price': kr.get('price', 0),
            }

    if sig is None:
        return None

    if sig['rr2'] < SP['rr2_warn_threshold'] and sig['quality'] not in ('WAIT',):
        if sig['quality'] == 'IDEAL':  sig['quality'] = 'GOOD'
        elif sig['quality'] == 'GOOD': sig['quality'] = 'MODERATE'
        sig['reasons'].append("R:R di bawah target")

    if sig['rr2'] < SP['min_rr_tp2'] and sig['quality'] not in ('WAIT',):
        return None

    # ── SESSION FILTER ────────────────────────────────────────────────
    # Data historis — WR per sesi:
    #   LONDON  (07-13 UTC) : WR terbaik — likuiditas institusional tinggi
    #   OVERLAP (13-16 UTC) : WR 29.2% — volume tertinggi tapi lebih volatile
    #   NY      (16-21 UTC) : WR 19.4% — banyak false breakout / reversal mendadak
    #   ASIA    (21-02 UTC) : WR 25.1% — stabil, boleh trade
    #   DEAD    (02-07 UTC) : WR 19.0% — BLOKIR — likuiditas sangat rendah
    #
    # Update: DEAD diblokir total (bukan sekadar downgrade).
    # NY downgrade ke MODERATE agar tidak di-eksekusi otomatis.
    # ─────────────────────────────────────────────────────────────────
    try:
        import datetime as _dt
        utc_hour = _dt.datetime.now(_dt.timezone.utc).hour
        if 2 <= utc_hour < 7:        # DEAD — blokir total
            session_label = 'DEAD'
        elif 13 <= utc_hour < 16:    # OVERLAP
            session_label = 'OVERLAP'
        elif 7 <= utc_hour < 13:     # LONDON (terbaik)
            session_label = 'LONDON'
        elif 16 <= utc_hour < 21:    # NY
            session_label = 'NY'
        else:                        # ASIA (21-02 UTC)
            session_label = 'ASIA'

        sig['session'] = session_label

        if session_label == 'DEAD' and not _IS_BACKTEST:
            # Blokir total — tidak ada trade jam 02-07 UTC (live trading saja)
            return None

        elif session_label == 'NY' and sig['quality'] == 'GOOD':
            sig['quality'] = 'MODERATE'
            sig['reasons'].append("Sesi NY (16-21 UTC) — banyak false breakout, WR historis 19.4%")

        elif session_label == 'OVERLAP' and sig['quality'] == 'GOOD':
            # OVERLAP tetap GOOD tapi tandai — volume tinggi, lebih berisiko
            sig['reasons'].append("Sesi OVERLAP — volume tinggi, waspadai spike mendadak")

        elif session_label == 'LONDON':
            sig['reasons'].append("Sesi LONDON — likuiditas tinggi, kondisi optimal")

    except Exception:
        sig['session'] = 'UNKNOWN'

    # ── SL PATTERN FILTER — terapkan pelajaran dari backtest ──────────────
    # Baca data/sl_patterns.json (dihasilkan oleh sl_postmortem.py)
    # Kurangi score untuk faktor yang terbukti sering menyebabkan SL
    if _SL_PATTERNS:
        try:
            trap_factors  = _SL_PATTERNS.get('trap_factors', {})
            dead_scores   = _SL_PATTERNS.get('dead_score_ranges', [])
            bad_kills     = _SL_PATTERNS.get('bad_kill_counts', [])

            # Blokir dead zone score (dari post-mortem, bukan hardcode)
            cur_score = sig.get('confluence_score', 0)
            if dead_scores and any(ds <= cur_score <= ds + 1 for ds in dead_scores):
                sig['quality'] = 'MODERATE' if sig['quality'] == 'GOOD' else sig['quality']
                sig['reasons'].append(f"Score {cur_score} masuk dead zone historis — turun ke MODERATE")

            # Blokir kill count yang konsisten jelek
            if bad_kills and sig.get('kill_count', 0) in bad_kills:
                sig['quality'] = 'MODERATE' if sig['quality'] == 'GOOD' else sig['quality']
                sig['reasons'].append(f"{sig['kill_count']} kill di sinyal ini — WR historis < 25%")

            # Penalti kecil untuk faktor jebakan kuat (bias > 20%)
            reasons_str = " ".join(sig.get('reasons', [])).lower()
            penalty = 0
            trap_desc = []
            for factor, data in trap_factors.items():
                if data.get('sl_bias', 0) >= 20:
                    kws = FACTOR_PATTERNS.get(factor, [factor]) if False else [factor]
                    if factor.lower() in reasons_str:
                        penalty += 1
                        trap_desc.append(factor)
            if penalty >= 2:
                # Hanya downgrade jika ada 2+ faktor jebakan bersamaan
                if sig['quality'] == 'GOOD':
                    sig['quality'] = 'MODERATE'
                    sig['reasons'].append(f"SL pattern: {', '.join(trap_desc)} — kombinasi jebakan historis")
        except Exception:
            pass

    return sig


# ============================================================
# HELPERS
# ============================================================

def _build_reasons(conf, kills, quality, geo, is_strong_trend, adx):
    reasons = conf[:6]
    if geo.get('tp1_label','1:1 R:R') != '1:1 R:R':
        reasons.append(f"TP1: {geo['tp1_label']}")
    if geo.get('tp2_label','1:2 R:R') != '1:2 R:R':
        reasons.append(f"TP2: {geo['tp2_label']}")
    if kills:
        reasons.append(f"Catatan: {kills[0]}")
    if is_strong_trend:
        reasons.insert(0, f"ADX {adx:.0f} — trend kuat")
    return reasons



# ============================================================
# LIMIT ORDER SIGNAL — Best Setup Finder
# ============================================================

def generate_limit_signal(
    price, atr, ema_trend, structure,
    ks, kr, res_mtf, sup_mtf, smc,
    df_main=None, symbol='', htf_ema='SIDEWAYS',
    signal_cache=None,
):
    """
    Cari setup LIMIT ORDER terbaik — selalu ada sesuatu untuk ditradingkan.

    Pendekatan:
    1. PRIORITY 1: Breakout + Retest (flip zone) — setup terkuat
       Support ditembus → jadi resistance → SHORT limit di sana
       Resistance ditembus → jadi support → LONG limit di sana

    2. PRIORITY 2: Level kuat terdekat — bahkan tanpa breakout
       Scan semua level HTF/MTF, cari yang paling dekat dengan harga
       Dan punya score tinggi (sering dihormati)
       Setup limit di level tersebut dengan arah sesuai trend

    Signal selalu keluar — minimal ada satu level potensial di setiap chart.
    """
    if df_main is None or len(df_main) < 20 or atr <= 0:
        return None

    # Anti-flip: kalau ada signal aktif di arah berlawanan, jangan generate limit kebalikannya
    lock_hours = SP.get('signal_lock_hours', 8)
    cached_dir = ''
    if signal_cache and symbol:
        cached = signal_cache.get(symbol, {})
        hours_since = (time.time() - cached.get('ts', 0)) / 3600
        if hours_since < lock_hours:
            cached_dir = cached.get('dir', '')

    closes  = df_main['close'].values
    highs   = df_main['high'].values
    lows    = df_main['low'].values

    # Kumpulkan semua level dari HTF/MTF + key levels
    all_levels = []
    for lvl in (res_mtf or []):
        if isinstance(lvl, dict) and lvl.get('price', 0) > 0:
            all_levels.append({
                'price': lvl['price'],
                'low'  : lvl.get('low', lvl['price'] - atr * 0.4),
                'high' : lvl.get('high', lvl['price'] + atr * 0.4),
                'score': lvl.get('score', 10),
                'type' : 'resistance'
            })
    for lvl in (sup_mtf or []):
        if isinstance(lvl, dict) and lvl.get('price', 0) > 0:
            all_levels.append({
                'price': lvl['price'],
                'low'  : lvl.get('low', lvl['price'] - atr * 0.4),
                'high' : lvl.get('high', lvl['price'] + atr * 0.4),
                'score': lvl.get('score', 10),
                'type' : 'support'
            })
    if ks:
        all_levels.append({**ks, 'type': 'support',  'score': ks.get('score', 20)})
    if kr:
        all_levels.append({**kr, 'type': 'resistance','score': kr.get('score', 20)})

    if not all_levels:
        return None

    results = []

    for lvl in all_levels:
        lvl_price = lvl.get('price', 0)
        lvl_low   = lvl.get('low',   lvl_price - atr * 0.4)
        lvl_high  = lvl.get('high',  lvl_price + atr * 0.4)
        lvl_score = lvl.get('score', 10)
        if lvl_price <= 0:
            continue

        # ── PRIORITY 1: Breakout + Retest (Flip Zone) ──────────────────
        # SHORT LIMIT: Support ditembus ke bawah, sekarang jadi resistance
        if lvl['type'] == 'support':
            breakdown_candles = sum(1 for c in closes[-15:] if c < lvl_low - atr * 0.2)

            # VALIDASI KRITIS: Candle terbaru harus MASIH di bawah zone
            # Kalau candle terakhir sudah di atas zone = bounce/rejection, bukan breakdown
            last_close   = closes[-1]
            last_2_close = closes[-2] if len(closes) >= 2 else closes[-1]
            currently_below = last_close < lvl_high  # harga masih di bawah level
            # Bounced: candle terbaru close jauh di atas zone = false breakdown
            is_bounced = last_close > lvl_high + atr * 0.1 and last_2_close > lvl_high + atr * 0.1

            if breakdown_candles >= 2 and currently_below and not is_bounced:
                # Harga sudah breakdown — cari entry limit SHORT di level lama
                dist_to_level = lvl_high - price
                if -atr * 3 <= dist_to_level <= atr * 8:
                    entry = lvl_high + atr * 0.05
                    sl    = lvl_high + atr * 1.0
                    risk  = abs(entry - sl)
                    if risk <= 0: continue
                    tp1 = entry - risk * 1.0
                    tp2 = entry - risk * 2.0

                    # Confidence: score level + breakdown strength + trend alignment
                    conf = 60 + min(25, lvl_score // 2)
                    if breakdown_candles >= 4: conf += 10
                    if structure == 'DOWNTREND': conf += 10
                    if ema_trend in ('DOWN', 'STRONG_DOWN'): conf += 5
                    if htf_ema in ('DOWN', 'STRONG_DOWN'): conf += 5
                    conf = min(95, conf)

                    # Apakah harga sudah retest atau masih menuju retest?
                    is_retesting = lvl_low * 0.98 <= price <= lvl_high * 1.02
                    status = "⚡ Retest aktif — segera pasang" if is_retesting else "📌 Menuju retest — pasang sekarang"

                    # Anti-flip: jangan SHORT kalau ada LONG aktif
                    if cached_dir == 'LONG':
                        continue

                    results.append({
                        'entry'        : round(entry, 8),
                        'sl'           : round(sl, 8),
                        'tp1'          : round(tp1, 8),
                        'tp2'          : round(tp2, 8),
                        'rr1'          : 1.0, 'rr2': 2.0, 'rr': 2.0,
                        'sl_pct'       : round(abs(sl - entry) / entry * 100, 2),
                        'confidence'   : conf,
                        'zone_price'   : lvl_price,
                        'zone_low'     : lvl_low,
                        'zone_high'    : lvl_high,
                        'breakdown_n'  : breakdown_candles,
                        'reasons': [
                            f"Support {_fmt_price(lvl_price)} ditembus ({breakdown_candles} candle) → jadi resistance",
                            f"{status}",
                            f"SHORT mengikuti trend turun yang terbentuk",
                            f"Entry limit: {_fmt_price(entry)} | SL: {_fmt_price(sl)}",
                            f"TP1: {_fmt_price(tp1)} | TP2: {_fmt_price(tp2)} (RR 1:2)",
                        ],
                    })

        # LONG LIMIT: Resistance ditembus ke atas, sekarang jadi support
        elif lvl['type'] == 'resistance':
            breakout_candles = sum(1 for c in closes[-15:] if c > lvl_high + atr * 0.2)

            # VALIDASI KRITIS: Candle terbaru harus MASIH di atas zone
            # Kalau candle terakhir sudah di bawah zone = rejection, bukan breakout
            last_close   = closes[-1]
            last_2_close = closes[-2] if len(closes) >= 2 else closes[-1]
            currently_above = last_close > lvl_low  # harga masih di atas level
            # Rejected: candle terbaru close jauh di bawah zone = rejection candle
            is_rejected = last_close < lvl_low - atr * 0.1 and last_2_close < lvl_low - atr * 0.1

            if breakout_candles >= 2 and currently_above and not is_rejected:
                dist_to_level = price - lvl_low
                if -atr * 3 <= dist_to_level <= atr * 8:
                    entry = lvl_low - atr * 0.05
                    sl    = lvl_low - atr * 1.0
                    risk  = abs(entry - sl)
                    if risk <= 0: continue
                    tp1 = entry + risk * 1.0
                    tp2 = entry + risk * 2.0

                    conf = 60 + min(25, lvl_score // 2)
                    if breakout_candles >= 4: conf += 10
                    if structure == 'UPTREND': conf += 10
                    if ema_trend in ('UP', 'STRONG_UP'): conf += 5
                    if htf_ema in ('UP', 'STRONG_UP'): conf += 5
                    conf = min(95, conf)

                    is_retesting = lvl_low * 0.98 <= price <= lvl_high * 1.02
                    status = "⚡ Retest aktif — segera pasang" if is_retesting else "📌 Menuju retest — pasang sekarang"

                    # Anti-flip: jangan LONG kalau ada SHORT aktif
                    if cached_dir == 'SHORT':
                        continue

                    results.append({
                        'direction'       : 'LONG',
                        'order_type'      : 'LIMIT',
                        'quality'         : 'LIMIT',
                        'priority'        : 1,
                        'confluence_score': conf,
                        'entry'        : round(entry, 8),
                        'sl'           : round(sl, 8),
                        'tp1'          : round(tp1, 8),
                        'tp2'          : round(tp2, 8),
                        'rr1'          : 1.0, 'rr2': 2.0, 'rr': 2.0,
                        'sl_pct'       : round(abs(sl - entry) / entry * 100, 2),
                        'confidence'   : conf,
                        'zone_price'   : lvl_price,
                        'zone_low'     : lvl_low,
                        'zone_high'    : lvl_high,
                        'breakout_n'   : breakout_candles,
                        'reasons': [
                            f"Resistance {_fmt_price(lvl_price)} ditembus ({breakout_candles} candle) → jadi support",
                            f"{status}",
                            f"LONG mengikuti trend naik yang terbentuk",
                            f"Entry limit: {_fmt_price(entry)} | SL: {_fmt_price(sl)}",
                            f"TP1: {_fmt_price(tp1)} | TP2: {_fmt_price(tp2)} (RR 1:2)",
                        ],
                    })

        # ── PRIORITY 2: Level kuat terdekat (tanpa breakout) ───────────
        # Pakai arah trend sebagai panduan
        dist = abs(price - lvl_price)
        dist_atr = dist / atr if atr > 0 else 999

        if dist_atr <= 8 and lvl_score >= 15:  # dalam 8 ATR dan level cukup kuat
            is_bearish = structure in ('DOWNTREND',) or ema_trend in ('DOWN', 'STRONG_DOWN')
            is_bullish = structure in ('UPTREND',) or ema_trend in ('UP', 'STRONG_UP')

            # SHORT ke resistance terdekat di atas (harga menuju ke sana)
            if lvl['type'] == 'resistance' and price < lvl_low and is_bearish:
                entry = lvl_high + atr * 0.05
                sl    = lvl_high + atr * 1.2
                risk  = abs(entry - sl)
                if risk <= 0: continue
                tp1 = entry - risk * 1.0
                tp2 = entry - risk * 2.0

                conf = 50 + min(20, lvl_score // 2)
                if dist_atr <= 3: conf += 10  # level sangat dekat
                conf = min(80, conf)

                results.append({
                    'direction'       : 'SHORT',
                    'order_type'      : 'LIMIT',
                    'quality'         : 'LIMIT',
                    'priority'        : 2,
                    'confluence_score': conf,
                    'entry'      : round(entry, 8),
                    'sl'         : round(sl, 8),
                    'tp1'        : round(tp1, 8),
                    'tp2'        : round(tp2, 8),
                    'rr1'        : 1.0, 'rr2': 2.0, 'rr': 2.0,
                    'sl_pct'     : round(abs(sl - entry) / entry * 100, 2),
                    'confidence' : conf,
                    'zone_price' : lvl_price,
                    'zone_low'   : lvl_low,
                    'zone_high'  : lvl_high,
                    'reasons': [
                        f"Resistance kuat di {_fmt_price(lvl_price)} (score {lvl_score})",
                        f"Trend: {structure} — SHORT limit kalau harga sentuh area",
                        f"Entry limit: {_fmt_price(entry)} | SL: {_fmt_price(sl)}",
                        f"TP1: {_fmt_price(tp1)} | TP2: {_fmt_price(tp2)} (RR 1:2)",
                        f"Pasang sekarang, tunggu harga naik ke resistance",
                    ],
                })

            # LONG ke support terdekat di bawah (harga menuju ke sana)
            elif lvl['type'] == 'support' and price > lvl_high and is_bullish:
                entry = lvl_low - atr * 0.05
                sl    = lvl_low - atr * 1.2
                risk  = abs(entry - sl)
                if risk <= 0: continue
                tp1 = entry + risk * 1.0
                tp2 = entry + risk * 2.0

                conf = 50 + min(20, lvl_score // 2)
                if dist_atr <= 3: conf += 10
                conf = min(80, conf)

                results.append({
                    'direction'       : 'LONG',
                    'order_type'      : 'LIMIT',
                    'quality'         : 'LIMIT',
                    'priority'        : 2,
                    'confluence_score': conf,
                    'entry'      : round(entry, 8),
                    'sl'         : round(sl, 8),
                    'tp1'        : round(tp1, 8),
                    'tp2'        : round(tp2, 8),
                    'rr1'        : 1.0, 'rr2': 2.0, 'rr': 2.0,
                    'sl_pct'     : round(abs(sl - entry) / entry * 100, 2),
                    'confidence' : conf,
                    'zone_price' : lvl_price,
                    'zone_low'   : lvl_low,
                    'zone_high'  : lvl_high,
                    'reasons': [
                        f"Support kuat di {_fmt_price(lvl_price)} (score {lvl_score})",
                        f"Trend: {structure} — LONG limit kalau harga turun ke area",
                        f"Entry limit: {_fmt_price(entry)} | SL: {_fmt_price(sl)}",
                        f"TP1: {_fmt_price(tp1)} | TP2: {_fmt_price(tp2)} (RR 1:2)",
                        f"Pasang sekarang, tunggu harga turun ke support",
                    ],
                })

    if not results:
        return None

    # ── APPLY WR 60% FILTERS (sama seperti main signal) ──────────────
    # Tanpa filter ini LIMIT signal WR hanya 33% (backtest data).
    bc  = smc.get('bos_choch', {})
    hbc = smc.get('htf_bos', {})
    phase = smc.get('phase', {}).get('phase', 'UNKNOWN')
    filtered = []
    for r in results:
        kills = []
        d = r.get('direction', '')
        is_long = d == 'LONG'

        # Fix #1: Accumulation kill for LONG, Distribution kill for SHORT
        if phase == ('ACCUMULATION' if is_long else 'DISTRIBUTION'):
            kills.append(f"Fase {'Akumulasi' if is_long else 'Distribusi'} — tunggu breakout")
        if phase == ('DISTRIBUTION' if is_long else 'ACCUMULATION'):
            kills.append(f"Fase melawan arah")
        if phase == ('MARKDOWN' if is_long else 'MARKUP'):
            kills.append(f"Fase melawan arah")

        # Fix #2: BOS 1H tanpa HTF BOS = false breakout
        _bos_1h  = bc.get('bos') == ('BULLISH' if is_long else 'BEARISH')
        _htf_bos = hbc.get('bos') == ('BULLISH' if is_long else 'BEARISH')
        if _bos_1h and not _htf_bos:
            kills.append("BOS 1H tanpa HTF — false breakout risk")

        # CHoCH melawan = strong kill
        if bc.get('choch') == ('BEARISH' if is_long else 'BULLISH'):
            kills.append("CHoCH melawan arah")
        if hbc.get('choch') == ('BEARISH' if is_long else 'BULLISH'):
            kills.append("HTF CHoCH melawan arah")

        # HTF EMA melawan = kill
        if htf_ema in ('STRONG_DOWN', 'DOWN') if is_long else htf_ema in ('STRONG_UP', 'UP'):
            kills.append("HTF EMA melawan arah")

        # Kill count gate — max 1 kill (sama dengan max_kills_moderate)
        if len(kills) >= 2:
            continue

        r['kill_count'] = len(kills)
        if kills:
            r['reasons'].append(f"Catatan: {kills[0]}")
        filtered.append(r)

    if not filtered:
        return None

    results = filtered

    # Sort: Priority 1 dulu, lalu by confidence
    results.sort(key=lambda x: (x.get('priority', 99), -x.get('confidence', x.get('confluence_score', 0))))
    best = results[0]

    # Deteksi apakah harga SUDAH di area entry sekarang
    entry_price = best['entry']
    zone_low    = best.get('zone_low', entry_price * 0.995)
    zone_high   = best.get('zone_high', entry_price * 1.005)

    # Harga dianggap "sudah di area" kalau dalam 0.3 ATR dari entry
    dist_from_entry = abs(price - entry_price)
    already_at_zone = dist_from_entry <= atr * 0.3

    if already_at_zone:
        # Ubah ke signal SEKARANG — harga sudah di area, bisa langsung entry
        best['order_type'] = 'NOW'
        best['quality']    = 'LIMIT'
        best['at_zone']    = True
        # Update alasan
        best['reasons'][1] = "⚡ HARGA SUDAH DI AREA — bisa entry sekarang"
    else:
        best['at_zone'] = False
        dist_pct = abs(price - entry_price) / price * 100
        best['dist_pct'] = round(dist_pct, 2)

    return best

def _fmt_price(price: float) -> str:
    """Format harga untuk display."""
    if price >= 10000: return f"{price:,.1f}"
    if price >= 100:   return f"{price:,.2f}"
    if price >= 1:     return f"{price:,.4f}"
    return f"{price:.6f}"