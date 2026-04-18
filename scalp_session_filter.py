"""
scalp_session_filter.py — Session-based filter dan learning.

Session mapping (UTC hours):
  ASIA    : 00-07 UTC  (Tokyo/Sydney/HK)
  LONDON  : 07-13 UTC  (London open session)
  OVERLAP : 13-16 UTC  (London-NY overlap — highest vol)
  NY      : 16-21 UTC  (NY session)
  EVENING : 21-24 UTC  (post-NY, early Asia)

Special: 02-06 UTC = DEAD zone (low liquidity).

Learning mechanism:
  - Kalau session tertentu WR < 40% di trade journal → downgrade quality
  - Kalau WR < 30% → block total
  - OVERLAP biasanya highest vol tapi juga highest manipulation
"""

import logging
from typing import Optional
from datetime import datetime

import scalp_trade_journal as journal

logger = logging.getLogger(__name__)


def get_session(hour_utc: int) -> str:
    """Map UTC hour ke session name."""
    if 2 <= hour_utc < 6:
        return 'DEAD'
    elif 0 <= hour_utc < 7:
        return 'ASIA'
    elif 7 <= hour_utc < 13:
        return 'LONDON'
    elif 13 <= hour_utc < 16:
        return 'OVERLAP'
    elif 16 <= hour_utc < 21:
        return 'NY'
    else:  # 21-24
        return 'EVENING'


def get_session_from_timestamp(ts) -> str:
    """
    Terima pandas Timestamp / datetime / string ISO, return session.
    """
    if hasattr(ts, 'hour'):
        return get_session(ts.hour)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            return get_session(dt.hour)
        except Exception:
            return 'UNKNOWN'
    return 'UNKNOWN'


# =========================================================
#  SESSION QUALITY GATE
# =========================================================
class SessionGate:
    """
    Session-based filter dengan learning.

    Default blocks DEAD session.
    Additionally blocks session dengan historical WR < 30%.
    """

    def __init__(self):
        self._session_stats = {}
        self._refreshed = False
        self.refresh()

    def refresh(self):
        """Load session stats dari trade journal."""
        try:
            self._session_stats = journal.get_session_stats(min_trades=10)
            self._refreshed = True
            if self._session_stats:
                logger.info(f"Session stats loaded: "
                            f"{list(self._session_stats.keys())}")
        except Exception as e:
            logger.debug(f"Session refresh failed: {e}")
            self._session_stats = {}

    def should_trade(self, session: str) -> tuple:
        """
        Apakah boleh trade di session ini?

        Returns:
            (allow: bool, reason: str, quality_modifier: int)

        quality_modifier: 0 = keep quality, -1 = downgrade by one tier
        """
        # Hard block DEAD session
        if session == 'DEAD':
            return False, 'DEAD session (low liquidity)', -99

        # Kalau belum ada data stats, pakai default rules
        if not self._session_stats or session not in self._session_stats:
            return True, f'{session} (no history)', 0

        stats = self._session_stats[session]
        wr = stats['wr']
        ev = stats['ev_r']

        # WR < 30 atau EV negatif → block
        if wr < 30 or ev < 0:
            return False, f'{session} WR {wr:.0f}% EV {ev:+.2f}R (blocked)', -99

        # WR 30-40 → downgrade quality
        if wr < 40:
            return True, f'{session} WR {wr:.0f}% (downgrade)', -1

        # WR 40-50 → keep
        if wr < 50:
            return True, f'{session} WR {wr:.0f}% (ok)', 0

        # WR >= 50 → keep (good session)
        return True, f'{session} WR {wr:.0f}% (good)', 0

    def get_stats(self, session: str) -> Optional[dict]:
        return self._session_stats.get(session)

    def print_summary(self):
        """Print ringkasan session stats."""
        if not self._session_stats:
            print("No session stats yet")
            return

        print("\n" + "=" * 60)
        print(" SESSION PERFORMANCE")
        print("=" * 60)
        print(f"{'Session':<10} {'N':>6} {'WR':>7} {'EV':>8} {'Total':>8}")
        print("-" * 60)

        for sess in ['ASIA', 'LONDON', 'OVERLAP', 'NY', 'EVENING']:
            if sess not in self._session_stats:
                continue
            s = self._session_stats[sess]
            print(f"{sess:<10} {s['n_trades']:>6} "
                  f"{s['wr']:>6.1f}% {s['ev_r']:>+7.2f}R "
                  f"{s['total_pnl_r']:>+7.1f}R")


# Singleton
_gate_instance = None


def get_session_gate() -> SessionGate:
    global _gate_instance
    if _gate_instance is None:
        _gate_instance = SessionGate()
    return _gate_instance
