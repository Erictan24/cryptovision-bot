"""
smart_coin_selector.py — Pilih coin berdasarkan hasil training historis.

Cara kerja:
  1. Baca data/training_results.json (dibuat setelah /train)
  2. Kembalikan daftar coin yang terbukti profitable
  3. Kalau belum ada training results, fallback ke get_top_coins(100)

Kriteria coin LAYAK di-trade:
  - PnL positif dalam training (tidak merugi)
  - Win Rate >= 45%
  - Minimal 20 signal dalam training (data cukup)
"""

import json
import os
import logging

logger = logging.getLogger(__name__)

RESULTS_PATH = 'data/training_results.json'


def get_profitable_coins(min_wr: float = 45.0, min_pnl: float = 0.0,
                          min_signals: int = 20, top_n: int = None) -> list:
    """
    Kembalikan daftar coin profitable berdasarkan training terakhir.

    Args:
        min_wr      : Minimum win rate (%) — default 45%
        min_pnl     : Minimum PnL ($) — default $0 (tidak merugi)
        min_signals : Minimum jumlah signal dalam training
        top_n       : Ambil top N saja berdasarkan PnL (None = semua)

    Returns:
        List of coin symbols, sorted by PnL descending.
        Kosong jika belum ada training results.
    """
    if not os.path.exists(RESULTS_PATH):
        logger.warning("training_results.json belum ada — jalankan /train dulu")
        return []

    try:
        with open(RESULTS_PATH) as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Gagal baca training_results.json: {e}")
        return []

    per_coin = data.get('per_coin', {})
    if not per_coin:
        return []

    filtered = [
        (sym, info) for sym, info in per_coin.items()
        if info.get('total', 0) >= min_signals
        and info.get('wr', 0) >= min_wr
        and info.get('pnl', 0) >= min_pnl
    ]

    # Sort by PnL tertinggi
    filtered.sort(key=lambda x: x[1]['pnl'], reverse=True)

    coins = [sym for sym, _ in filtered]
    if top_n:
        coins = coins[:top_n]

    trained_at = data.get('trained_at', 'unknown')
    logger.info(f"Smart selector: {len(coins)} coin profitable (training: {trained_at})")
    return coins


def get_avoid_coins() -> list:
    """Coin yang terbukti merugi dalam training — sebaiknya dihindari."""
    if not os.path.exists(RESULTS_PATH):
        return []
    try:
        with open(RESULTS_PATH) as f:
            data = json.load(f)
        return data.get('avoid_coins', [])
    except Exception:
        return []


def get_training_summary() -> dict:
    """Ringkasan hasil training terakhir untuk ditampilkan di /learn."""
    if not os.path.exists(RESULTS_PATH):
        return {}
    try:
        with open(RESULTS_PATH) as f:
            data = json.load(f)
        return {
            'trained_at'   : data.get('trained_at', '-'),
            'total_signals': data.get('total_signals', 0),
            'overall_wr'   : data.get('overall_wr', 0),
            'overall_pnl'  : data.get('overall_pnl', 0),
            'ev_per_trade' : data.get('ev_per_trade', 0),
            'breakeven_wr' : data.get('breakeven_wr', 0),
            'surplus_wr'   : data.get('surplus_wr', 0),
            'n_profitable' : len(data.get('profitable_coins', [])),
            'n_avoid'      : len(data.get('avoid_coins', [])),
            'profitable'   : data.get('profitable_coins', []),
            'avoid'        : data.get('avoid_coins', []),
        }
    except Exception:
        return {}
