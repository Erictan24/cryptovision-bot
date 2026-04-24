"""
scalp_coin_learning.py — Adaptive per-coin parameters.

Bot belajar dari trade journal:
  - Coin dengan WR tinggi → more permissive (lower score threshold)
  - Coin dengan WR rendah → stricter (higher threshold) atau blocked
  - Coin tanpa history → default (conservative)

Cache di memory + persist ke JSON untuk speed.
"""

import os
import json
import logging
from typing import Optional

import scalp_trade_journal as journal

logger = logging.getLogger(__name__)

CACHE_PATH = 'data/scalp_coin_params.json'
# Opsi C (2026-04-24): threshold diperketat untuk block coin jelek lebih cepat.
# Data 100 coin: SOL/SUI/SEI/VIRTUAL drag 5-8R per coin sebelum di-block.
# Min trades turun 15→10 biar adapt lebih cepat.
MIN_TRADES_FOR_ADAPTATION = 10


# Default params (coin tanpa history)
DEFAULT_PARAMS = {
    'score_good_threshold': 10,
    'score_wait_threshold': 7,
    'min_trend_strength': 50,
    'allow_trading': True,
    'confidence': 'default',
}


class CoinLearning:
    """Adaptive parameters per coin berdasarkan trade history."""

    def __init__(self):
        self._cache = {}
        self._load_cache()

    def _load_cache(self):
        """Load cached params dari disk."""
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH, 'r') as f:
                    self._cache = json.load(f)
            except Exception as e:
                logger.warning(f"Load coin cache failed: {e}")
                self._cache = {}

    def _save_cache(self):
        """Save params ke disk."""
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        try:
            with open(CACHE_PATH, 'w') as f:
                json.dump(self._cache, f, indent=2)
        except Exception as e:
            logger.warning(f"Save coin cache failed: {e}")

    def refresh(self, coins: list = None):
        """
        Recalculate params untuk semua coin (atau specific list).
        Panggil setelah backtest selesai atau weekly di production.
        """
        if coins is None:
            # Ambil semua coin unique dari journal
            import sqlite3
            journal._ensure_db()
            conn = sqlite3.connect(journal.DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT symbol FROM trades")
            coins = [r[0] for r in cur.fetchall()]
            conn.close()

        updated = 0
        for coin in coins:
            stats = journal.get_coin_stats(coin, min_trades=MIN_TRADES_FOR_ADAPTATION)
            if stats is None:
                continue

            params = self._compute_params(stats)
            self._cache[coin] = {
                'params': params,
                'stats': stats,
                'last_updated': None,  # will be set by journal
            }
            updated += 1

        self._save_cache()
        logger.info(f"Coin learning refreshed: {updated} coins updated")
        return updated

    def _compute_params(self, stats: dict) -> dict:
        """
        Compute adaptive params dari stats.

        Aturan:
          WR >= 55% : confidence HIGH, lower threshold (more trades)
          WR 45-54%: confidence OK, default threshold
          WR 35-44%: confidence LOW, raise threshold (fewer trades)
          WR < 35% : confidence POOR, BLOCK trading

        Pertimbangkan juga EV:
          EV < 0    : BLOCK regardless of WR
          EV 0-0.1  : conservative
          EV > 0.3  : aggressive

        Recent form (last 10 trades):
          Kalau last_10_wr jauh lebih rendah dari overall → warning
        """
        wr = stats['wr']
        ev = stats['ev_r']
        last_10_wr = stats.get('last_10_wr', wr)

        # Hard block kalau EV negatif
        if ev < 0:
            return {
                'score_good_threshold': 999,  # effectively blocked
                'score_wait_threshold': 999,
                'min_trend_strength': 100,
                'allow_trading': False,
                'confidence': 'BLOCKED',
                'reason': f'EV negative ({ev:.2f}R)',
            }

        # Hard block kalau WR < 40% (was 30%) — Opsi C (2026-04-24):
        # lebih agresif block coin jelek setelah ≥10 trade.
        if wr < 40:
            return {
                'score_good_threshold': 999,
                'score_wait_threshold': 999,
                'min_trend_strength': 100,
                'allow_trading': False,
                'confidence': 'BLOCKED',
                'reason': f'WR too low ({wr:.0f}%)',
            }

        # Hard block kalau EV < 0.1R (marginal coin = tidak worth it)
        if ev < 0.1:
            return {
                'score_good_threshold': 999,
                'score_wait_threshold': 999,
                'min_trend_strength': 100,
                'allow_trading': False,
                'confidence': 'BLOCKED',
                'reason': f'EV marginal ({ev:.2f}R)',
            }

        # Recent form check
        form_warning = last_10_wr < wr - 15

        if wr >= 55:
            # High confidence — permissive
            return {
                'score_good_threshold': 8,
                'score_wait_threshold': 6,
                'min_trend_strength': 40,
                'allow_trading': True,
                'confidence': 'HIGH',
                'reason': f'WR {wr:.0f}%, EV +{ev:.2f}R',
                'form_warning': form_warning,
            }
        elif wr >= 45:
            # Default
            return {
                'score_good_threshold': 10,
                'score_wait_threshold': 7,
                'min_trend_strength': 50,
                'allow_trading': True,
                'confidence': 'OK',
                'reason': f'WR {wr:.0f}%, EV +{ev:.2f}R',
                'form_warning': form_warning,
            }
        elif wr >= 35:
            # Low confidence — strict
            return {
                'score_good_threshold': 13,
                'score_wait_threshold': 10,
                'min_trend_strength': 60,
                'allow_trading': True,
                'confidence': 'LOW',
                'reason': f'WR {wr:.0f}% (restricted)',
                'form_warning': form_warning,
            }
        else:
            # WR 30-35, very strict
            return {
                'score_good_threshold': 16,
                'score_wait_threshold': 13,
                'min_trend_strength': 70,
                'allow_trading': True,
                'confidence': 'POOR',
                'reason': f'WR {wr:.0f}% (very strict)',
                'form_warning': form_warning,
            }

    def get_params(self, coin: str) -> dict:
        """
        Ambil adaptive params untuk satu coin.
        Return default kalau coin belum ada history.
        """
        if coin in self._cache:
            return self._cache[coin].get('params', DEFAULT_PARAMS)
        return DEFAULT_PARAMS.copy()

    def should_trade(self, coin: str) -> bool:
        """Quick check: boleh trade coin ini?"""
        params = self.get_params(coin)
        return params.get('allow_trading', True)

    def get_stats(self, coin: str) -> Optional[dict]:
        """Ambil stats untuk display."""
        if coin in self._cache:
            return self._cache[coin].get('stats')
        return None

    def print_summary(self):
        """Print ringkasan semua coin params."""
        if not self._cache:
            print("No coin learning data yet — run refresh() first")
            return

        print("\n" + "=" * 70)
        print(" COIN LEARNING SUMMARY")
        print("=" * 70)
        print(f"{'Coin':<8} {'WR':>6} {'EV':>7} {'N':>5} {'Conf':<10} {'Threshold':<10}")
        print("-" * 70)

        sorted_coins = sorted(
            self._cache.items(),
            key=lambda x: x[1].get('stats', {}).get('wr', 0),
            reverse=True)

        for coin, data in sorted_coins:
            stats = data.get('stats', {})
            params = data.get('params', {})
            wr = stats.get('wr', 0)
            ev = stats.get('ev_r', 0)
            n = stats.get('n_trades', 0)
            conf = params.get('confidence', '-')
            thr = params.get('score_good_threshold', '-')
            status = '✓' if params.get('allow_trading') else '✗'
            print(f"{coin:<8} {wr:>5.1f}% {ev:>+6.2f}R {n:>5} "
                  f"{conf:<10} {thr:>3} {status}")


# Singleton untuk import-time efficiency
_learning_instance = None


def get_learning() -> CoinLearning:
    global _learning_instance
    if _learning_instance is None:
        _learning_instance = CoinLearning()
    return _learning_instance
