"""
auto_tune.py — Automatic parameter tuning from backtest results.

Dijalankan otomatis setiap minggu (dari main.py scheduler).
Proses:
  1. Fetch data historis 30 hari terakhir
  2. Jalankan backtest dengan parameter saat ini
  3. Analisa hasil per segmen (score range, kill count, session, dll)
  4. Update parameter yang underperform dalam batas aman
  5. Simpan ke data/learned_params.json

Prinsip penyesuaian:
  - HANYA adjust dalam batas ±20% dari nilai default
  - Butuh minimum 50 trade untuk adjust satu parameter
  - Satu kali run = maksimal 3 parameter yang diubah (hindari overfitting)
  - Semua perubahan dicatat di log agar bisa di-revert
"""

import sys
import json
import os
import time
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

LEARNED_PARAMS_FILE = os.path.join(Path(__file__).parent.parent, 'data', 'learned_params.json')
TUNE_LOG_FILE       = os.path.join(Path(__file__).parent.parent, 'data', 'auto_tune_log.json')

# Minimum trades untuk adjust parameter (avoid overfitting)
MIN_TRADES_TO_ADJUST = 50

# Batas perubahan dari default (pct)
MAX_CHANGE_PCT = 0.20

# Default parameter yang boleh di-adjust (dengan range aman)
ADJUSTABLE_PARAMS = {
    # param_name: (default, min_val, max_val, description)
    'score_good'           : (21,  18, 26, 'Score minimum GOOD signal'),
    'score_moderate'       : (21,  18, 26, 'Score minimum MODERATE signal'),
    'max_kills_good'       : (1,   0,  2,  'Max kill factors untuk GOOD'),
    'max_kills_moderate'   : (2,   1,  3,  'Max kill factors untuk MODERATE'),
    'zone_margin_atr_mult' : (0.6, 0.4, 0.9, 'Zone margin untuk near_support/resistance'),
    'adx_trending'         : (18,  15, 22, 'ADX threshold untuk trending'),
    'rsi_extreme_low'      : (28,  22, 35, 'RSI extreme low (block LONG)'),
    'rsi_extreme_high'     : (72,  65, 78, 'RSI extreme high (block SHORT)'),
}


def load_learned_params() -> dict:
    try:
        if os.path.exists(LEARNED_PARAMS_FILE):
            with open(LEARNED_PARAMS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_learned_params(params: dict):
    try:
        os.makedirs(os.path.dirname(LEARNED_PARAMS_FILE), exist_ok=True)
        with open(LEARNED_PARAMS_FILE, 'w') as f:
            json.dump(params, f, indent=2)
        logger.info(f"Learned params saved: {params}")
    except Exception as e:
        logger.error(f"Failed to save learned params: {e}")


def log_tune_event(changes: list, backtest_stats: dict):
    """Log semua perubahan parameter untuk audit trail."""
    try:
        history = []
        if os.path.exists(TUNE_LOG_FILE):
            with open(TUNE_LOG_FILE, 'r') as f:
                history = json.load(f)

        history.append({
            'timestamp' : datetime.now().isoformat(),
            'changes'   : changes,
            'stats'     : backtest_stats,
        })
        history = history[-50:]  # keep last 50 events

        with open(TUNE_LOG_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except Exception:
        pass


def analyze_backtest_results(trades: list) -> dict:
    """
    Analisa hasil backtest dan identifikasi pola yang perlu di-adjust.

    Return dict dengan insight:
      overall_wr      : win rate keseluruhan
      by_score        : WR per score range
      by_kills        : WR per kill count
      by_quality      : WR per quality tier
      recommended_adj : parameter yang direkomendasikan untuk adjustment
    """
    if not trades:
        return {}

    total  = len(trades)
    wins   = sum(1 for t in trades if t.get('won', False))
    wr     = wins / total if total > 0 else 0

    # Group by score range
    score_groups = {}
    for t in trades:
        score = t.get('confluence_score', 0)
        bucket = (score // 3) * 3   # group per 3 points
        if bucket not in score_groups:
            score_groups[bucket] = {'wins': 0, 'total': 0}
        score_groups[bucket]['total'] += 1
        if t.get('won'):
            score_groups[bucket]['wins'] += 1

    by_score = {
        k: {'wr': v['wins']/v['total'] if v['total'] > 0 else 0, 'n': v['total']}
        for k, v in score_groups.items()
    }

    # Group by kill count
    kill_groups = {}
    for t in trades:
        k = t.get('kill_count', 0)
        if k not in kill_groups:
            kill_groups[k] = {'wins': 0, 'total': 0}
        kill_groups[k]['total'] += 1
        if t.get('won'):
            kill_groups[k]['wins'] += 1

    by_kills = {
        k: {'wr': v['wins']/v['total'] if v['total'] > 0 else 0, 'n': v['total']}
        for k, v in kill_groups.items()
    }

    # Group by quality
    quality_groups = {}
    for t in trades:
        q = t.get('quality', 'UNKNOWN')
        if q not in quality_groups:
            quality_groups[q] = {'wins': 0, 'total': 0}
        quality_groups[q]['total'] += 1
        if t.get('won'):
            quality_groups[q]['wins'] += 1

    by_quality = {
        q: {'wr': v['wins']/v['total'] if v['total'] > 0 else 0, 'n': v['total']}
        for q, v in quality_groups.items()
    }

    return {
        'overall_wr' : round(wr, 3),
        'total'      : total,
        'by_score'   : by_score,
        'by_kills'   : by_kills,
        'by_quality' : by_quality,
    }


def generate_adjustments(analysis: dict, current_params: dict) -> list:
    """
    Generate rekomendasi penyesuaian parameter.

    Return list of adjustments:
      [{'param': ..., 'old_val': ..., 'new_val': ..., 'reason': ...}, ...]
    """
    from config import SIGNAL_PARAMS as SP_DEFAULT

    adjustments = []
    by_score  = analysis.get('by_score', {})
    by_kills  = analysis.get('by_kills', {})
    by_quality = analysis.get('by_quality', {})
    total     = analysis.get('total', 0)

    if total < MIN_TRADES_TO_ADJUST:
        logger.info(f"auto_tune: tidak cukup data ({total} < {MIN_TRADES_TO_ADJUST})")
        return []

    def get_current(param):
        return current_params.get(param, SP_DEFAULT.get(param, ADJUSTABLE_PARAMS[param][0]))

    def clamp(val, param):
        _, min_v, max_v, _ = ADJUSTABLE_PARAMS[param]
        return max(min_v, min(max_v, val))

    # ── Rule 1: Kalau score rendah (15-20) konsisten WR < 35%, naikkan score_good ──
    low_score_wr = []
    for score_bucket, data in by_score.items():
        if 15 <= score_bucket <= 20 and data['n'] >= 10:
            low_score_wr.append(data['wr'])

    if low_score_wr and sum(low_score_wr) / len(low_score_wr) < 0.38:
        cur = get_current('score_good')
        new_val = clamp(cur + 1, 'score_good')
        if new_val != cur:
            adjustments.append({
                'param'   : 'score_good',
                'old_val' : cur,
                'new_val' : new_val,
                'reason'  : f'Score 15-20 WR terlalu rendah ({sum(low_score_wr)/len(low_score_wr):.0%}) — naikkan threshold'
            })

    # ── Rule 2: Kalau kills=2 selalu kalah (WR < 25%), turunkan max_kills_good ──
    k2_data = by_kills.get(2, {})
    if k2_data.get('n', 0) >= 15 and k2_data.get('wr', 1) < 0.25:
        cur = get_current('max_kills_good')
        new_val = clamp(cur - 1, 'max_kills_good')
        if new_val != cur:
            adjustments.append({
                'param'   : 'max_kills_good',
                'old_val' : cur,
                'new_val' : new_val,
                'reason'  : f'2 kills WR hanya {k2_data["wr"]:.0%} dari {k2_data["n"]} trade — kurangi toleransi'
            })

    # ── Rule 3: Kalau WR keseluruhan bagus (>55%), bisa relaksasi sedikit ──
    overall_wr = analysis.get('overall_wr', 0)
    if overall_wr > 0.55 and total >= 100:
        cur = get_current('score_good')
        if cur > ADJUSTABLE_PARAMS['score_good'][1] + 1:  # tidak di minimum
            new_val = clamp(cur - 1, 'score_good')
            if new_val != cur:
                adjustments.append({
                    'param'   : 'score_good',
                    'old_val' : cur,
                    'new_val' : new_val,
                    'reason'  : f'WR keseluruhan {overall_wr:.0%} bagus — relaksasi score threshold'
                })

    # Batasi maksimal 3 perubahan per run
    return adjustments[:3]


def run_auto_tune(coins: list = None, days: int = 30, quiet: bool = True) -> dict:
    """
    Entry point utama — jalankan backtest + tune parameters.

    Return: {'adjusted': list, 'stats': dict, 'wr': float}
    """
    try:
        logger.info(f"auto_tune: starting backtest {days} hari, {len(coins or [])} coins")

        # Import backtesting components
        from backtesting.data_fetcher import DataFetcher
        from backtesting.replay_engine import BacktestEngine
        from backtesting.simulator import simulate_all, dedup_signals
        from backtesting.analyzer import analyze_trades

        if not coins:
            from config import SCAN_POOL
            coins = SCAN_POOL[:10]  # ambil 10 coin saja untuk kecepatan

        fetcher = DataFetcher()
        data    = fetcher.fetch_all(coins, tfs=['1h'], days=days)
        if not data:
            logger.warning("auto_tune: tidak ada data — skip")
            return {}

        engine = BacktestEngine()
        all_signals = []
        for sym in coins:
            sym_data = data.get(sym, {})
            df = sym_data.get('1h')
            if df is not None and len(df) >= 200:
                try:
                    sigs = engine.scan_coin(sym, '1h', df)
                    all_signals.extend(sigs)
                except Exception as e:
                    logger.debug(f"auto_tune scan {sym}: {e}")

        if len(all_signals) < MIN_TRADES_TO_ADJUST:
            logger.info(f"auto_tune: sinyal terlalu sedikit ({len(all_signals)}) — skip")
            return {'adjusted': [], 'stats': {}, 'wr': 0}

        deduped = dedup_signals(all_signals)
        trades  = simulate_all(deduped, data)

        analysis    = analyze_backtest_results(trades)
        current_lp  = load_learned_params()
        adjustments = generate_adjustments(analysis, current_lp)

        if adjustments:
            for adj in adjustments:
                current_lp[adj['param']] = adj['new_val']
                logger.info(f"auto_tune: {adj['param']} {adj['old_val']} → {adj['new_val']} | {adj['reason']}")

            save_learned_params(current_lp)
            log_tune_event(adjustments, analysis)
        else:
            logger.info(f"auto_tune: tidak ada adjustment diperlukan (WR={analysis.get('overall_wr', 0):.0%})")

        return {
            'adjusted': adjustments,
            'stats'   : analysis,
            'wr'      : analysis.get('overall_wr', 0),
        }

    except Exception as e:
        logger.error(f"auto_tune error: {e}", exc_info=True)
        return {}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    result = run_auto_tune(days=30)
    print(f"\nAuto-tune selesai:")
    print(f"  WR: {result.get('wr', 0):.1%}")
    print(f"  Adjustments: {len(result.get('adjusted', []))}")
    for adj in result.get('adjusted', []):
        print(f"    {adj['param']}: {adj['old_val']} → {adj['new_val']} ({adj['reason']})")
