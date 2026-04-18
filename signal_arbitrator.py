"""
signal_arbitrator.py — Signal conflict resolver.

Fungsi utama: decide signal mana yang boleh diterima,
mencegah tabrakan antar Bot 1 (SWING) dan Bot 2 (SCALP).

RULES (priority dari atas ke bawah):

1. Global max positions — kalau total open = max, REJECT semua signal baru
2. Daily loss limit — kalau hit, REJECT semua signal
3. Coin already has position:
   a. Opposite direction → REJECT (never hedge)
   b. Same direction → REJECT (prevent double-exposure)
4. Max same direction limit (max 3 LONG atau 3 SHORT total)
5. SWING priority mode: kalau SWING datang sementara SCALP ada di coin sama,
   SCALP di-close dulu, SWING masuk
6. PASS

Returns verdict: ALLOW | REJECT dengan reason.
"""

import logging
from typing import Optional
from datetime import datetime

from position_manager import get_position_manager

logger = logging.getLogger(__name__)


class SignalVerdict:
    """Verdict object dari arbitrator."""

    def __init__(self, allow: bool, reason: str = '',
                 action: str = None, tag: str = ''):
        self.allow = allow
        self.reason = reason
        self.action = action  # optional: 'CLOSE_SCALP_FIRST' etc
        self.tag = tag        # signal tag: '[SCALP]' or '[SWING]'

    def __repr__(self):
        status = 'ALLOW' if self.allow else 'REJECT'
        return f"<Verdict {status}: {self.reason}>"


class SignalArbitrator:
    """
    Decide whether a signal can proceed based on current state + rules.
    """

    def __init__(self, unified_config: dict):
        self.cfg = unified_config
        self.pm = get_position_manager()

        # Global limits
        self.max_positions = unified_config.get('global_max_positions', 4)
        self.max_same_direction = unified_config.get(
            'global_max_same_direction', 3)
        self.block_opposite = unified_config.get(
            'block_opposite_direction', True)
        self.block_duplicate = unified_config.get(
            'block_duplicate_coin', True)
        self.conflict_mode = unified_config.get(
            'conflict_mode', 'swing_priority')

        # Daily loss tracking
        self._daily_loss_usd = 0.0
        self._daily_loss_limit = unified_config.get(
            'global_max_daily_loss_usd', 10.0)
        self._daily_loss_hit = False

    # ══════════════════════════════════════════════════
    #  MAIN CHECK
    # ══════════════════════════════════════════════════
    def check_signal(self, signal: dict, engine: str) -> SignalVerdict:
        """
        Check apakah signal boleh diterima.

        Args:
            signal: signal dict (dari Bot 1 atau Bot 2)
            engine: 'SWING' atau 'SCALP'

        Returns:
            SignalVerdict
        """
        symbol = signal.get('_symbol', signal.get('symbol', ''))
        direction = signal.get('direction', '')
        quality = signal.get('quality', '')

        tag = f'[{engine}]'

        # 1. Basic validation
        if not symbol or not direction:
            return SignalVerdict(
                False, 'Invalid signal (missing symbol/direction)', tag=tag)

        if quality not in ('GOOD', 'IDEAL'):
            return SignalVerdict(
                False, f'Quality {quality} not acceptable', tag=tag)

        # 2. Daily loss limit check
        if self._daily_loss_hit:
            return SignalVerdict(
                False,
                f'Daily loss limit reached (${self._daily_loss_usd:.2f})',
                tag=tag)

        # 3. Global max positions
        total_open = self.pm.count_positions()
        if total_open >= self.max_positions:
            return SignalVerdict(
                False,
                f'Global max positions ({total_open}/{self.max_positions})',
                tag=tag)

        # 4. Max same direction
        same_dir_count = self.pm.count_by_direction(direction)
        if same_dir_count >= self.max_same_direction:
            return SignalVerdict(
                False,
                f'Max {direction} positions ({same_dir_count}/{self.max_same_direction})',
                tag=tag)

        # 5. Coin-specific conflict checks
        if self.pm.has_position(symbol):
            existing_positions = self.pm.get_positions(symbol)
            existing_dir = existing_positions[0]['direction']
            existing_engine = existing_positions[0]['engine']

            # 5a. Opposite direction
            if existing_dir != direction and self.block_opposite:
                return SignalVerdict(
                    False,
                    f'{symbol} already has {existing_engine} {existing_dir} '
                    f'position (opposite)',
                    tag=tag)

            # 5b. Same direction (prevent double-exposure)
            if existing_dir == direction and self.block_duplicate:
                # SWING priority: kalau SWING datang sementara SCALP ada,
                # action = close SCALP dulu
                if (self.conflict_mode == 'swing_priority' and
                        engine == 'SWING' and existing_engine == 'SCALP'):
                    return SignalVerdict(
                        False,
                        f'{symbol} sudah ada SCALP position (SWING priority — '
                        f'TODO: close SCALP first)',
                        action='CLOSE_SCALP_FIRST',
                        tag=tag)
                else:
                    return SignalVerdict(
                        False,
                        f'{symbol} already has {existing_engine} '
                        f'{direction} position',
                        tag=tag)

        # 6. ALL CHECKS PASSED
        return SignalVerdict(
            True,
            f'{engine} {symbol} {direction} approved',
            tag=tag)

    # ══════════════════════════════════════════════════
    #  DAILY LOSS TRACKING
    # ══════════════════════════════════════════════════
    def record_loss(self, amount_usd: float):
        """Track loss dari trade yang closed. amount_usd bisa negative."""
        if amount_usd < 0:
            self._daily_loss_usd += abs(amount_usd)
            if self._daily_loss_usd >= self._daily_loss_limit:
                self._daily_loss_hit = True
                logger.warning(
                    f"DAILY LOSS LIMIT HIT: ${self._daily_loss_usd:.2f}")

    def reset_daily(self):
        """Reset daily counters (panggil setiap 00:00 UTC)."""
        logger.info(f"Daily reset — loss was ${self._daily_loss_usd:.2f}")
        self._daily_loss_usd = 0.0
        self._daily_loss_hit = False

    def get_daily_status(self) -> dict:
        return {
            'daily_loss_usd': self._daily_loss_usd,
            'daily_loss_limit': self._daily_loss_limit,
            'daily_loss_hit': self._daily_loss_hit,
            'pct_used': self._daily_loss_usd / self._daily_loss_limit * 100 if self._daily_loss_limit > 0 else 0,
        }

    # ══════════════════════════════════════════════════
    #  CONVENIENCE
    # ══════════════════════════════════════════════════
    def get_status_summary(self) -> str:
        """Status summary untuk logging/Telegram."""
        summary = self.pm.get_summary()
        daily = self.get_daily_status()
        lines = [
            f"Arbitrator State:",
            f"  Positions: {summary['total']}/{self.max_positions} "
            f"(SWING:{summary['swing']}, SCALP:{summary['scalp']})",
            f"  LONG: {summary['longs']}, SHORT: {summary['shorts']}",
            f"  Daily loss: ${daily['daily_loss_usd']:.2f}/"
            f"${daily['daily_loss_limit']:.0f} "
            f"({daily['pct_used']:.0f}%)",
        ]
        return '\n'.join(lines)


# ══════════════════════════════════════════════════
#  SINGLETON
# ══════════════════════════════════════════════════
_instance = None


def get_arbitrator(unified_config: dict = None) -> SignalArbitrator:
    global _instance
    if _instance is None:
        if unified_config is None:
            from config import UNIFIED_CONFIG
            unified_config = UNIFIED_CONFIG
        _instance = SignalArbitrator(unified_config)
    return _instance
