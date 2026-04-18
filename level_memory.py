"""
level_memory.py — Memori historis pengujian level harga.

Bot "mengingat" setiap kali harga menyentuh suatu level:
  - Apakah level bertahan (bounce) atau tembus (break)?
  - Berapa kali sudah ditest?
  - Kapan terakhir ditest?

Manfaat:
  1. Level yang sudah 3x bertahan = sangat kuat → bonus score +8
  2. Level yang baru saja tembus = flip zone → JANGAN trade ke arah lama
  3. Level yang selalu tembus = tidak ada gunanya, skip

Data disimpan di data/level_memory.json — persisten antar sesi.
"""

import json
import os
import time
import math
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

MEMORY_FILE = os.path.join(os.path.dirname(__file__), 'data', 'level_memory.json')

# Berapa lama data level disimpan (hari)
MEMORY_RETENTION_DAYS = 90

# Jarak maksimal untuk menganggap level "sama" (pct dari harga)
LEVEL_MATCH_PCT = 0.008  # 0.8% — level dalam range ini dianggap satu level


class LevelMemory:
    """
    Menyimpan dan menganalisis riwayat pengujian level harga.
    """

    def __init__(self, path: str = MEMORY_FILE):
        self.path = path
        self._data: dict = {}
        self._dirty = False
        self._load()

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, 'r') as f:
                    self._data = json.load(f)
            else:
                self._data = {}
        except Exception as e:
            logger.debug(f"level_memory load error: {e}")
            self._data = {}

    def save(self):
        """Simpan ke disk jika ada perubahan."""
        if not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, 'w') as f:
                json.dump(self._data, f, indent=2)
            self._dirty = False
        except Exception as e:
            logger.debug(f"level_memory save error: {e}")

    def _cleanup_old(self):
        """Hapus data level yang sudah > MEMORY_RETENTION_DAYS hari."""
        cutoff = time.time() - MEMORY_RETENTION_DAYS * 86400
        removed = 0
        for sym in list(self._data.keys()):
            levels = self._data[sym]
            before = len(levels)
            self._data[sym] = {
                k: v for k, v in levels.items()
                if v.get('last_test', 0) >= cutoff
            }
            removed += before - len(self._data[sym])
            if not self._data[sym]:
                del self._data[sym]
        if removed > 0:
            self._dirty = True

    # ── Matching ─────────────────────────────────────────────────────────────

    def _get_key(self, symbol: str, level_price: float) -> str:
        """Buat key string dari symbol + level price (rounded)."""
        # Round ke 4 significant figures
        if level_price > 0:
            magnitude = 10 ** math.floor(math.log10(level_price))
            rounded = round(level_price / magnitude * 1000) / 1000 * magnitude
        else:
            rounded = round(level_price, 6)
        return f"{symbol.upper()}:{rounded:.6g}"

    def _find_nearest_key(self, symbol: str, level_price: float) -> Optional[str]:
        """Cari key yang paling dekat dengan level_price (dalam LEVEL_MATCH_PCT)."""
        if symbol.upper() not in self._data:
            return None
        best_key = None
        best_dist = float('inf')
        threshold = level_price * LEVEL_MATCH_PCT
        for key in self._data[symbol.upper()]:
            try:
                stored_price = float(key.split(':')[1])
                dist = abs(stored_price - level_price)
                if dist <= threshold and dist < best_dist:
                    best_dist = dist
                    best_key  = key
            except Exception:
                continue
        return best_key

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_test(self, symbol: str, level_price: float,
                    result: str, direction: str = ''):
        """
        Catat hasil pengujian level.

        Args:
          symbol      : coin symbol
          level_price : harga level yang ditest
          result      : 'held' (level bertahan/bounce) atau 'broke' (level tembus)
          direction   : 'LONG' atau 'SHORT' (signal yang masuk saat level ditest)
        """
        sym = symbol.upper()
        if sym not in self._data:
            self._data[sym] = {}

        key = self._get_key(sym, level_price)

        if key not in self._data[sym]:
            self._data[sym][key] = {
                'price'      : level_price,
                'held_count' : 0,
                'broke_count': 0,
                'last_test'  : 0,
                'last_result': '',
                'tests'      : []
            }

        entry = self._data[sym][key]
        now   = time.time()

        if result == 'held':
            entry['held_count']  += 1
        elif result == 'broke':
            entry['broke_count'] += 1

        entry['last_test']   = now
        entry['last_result'] = result

        # Simpan detail 10 test terakhir
        entry['tests'].append({
            'ts'       : now,
            'result'   : result,
            'direction': direction,
        })
        entry['tests'] = entry['tests'][-10:]  # keep last 10

        self._dirty = True

    # ── Analysis ─────────────────────────────────────────────────────────────

    def get_level_stats(self, symbol: str, level_price: float) -> dict:
        """
        Ambil statistik historis untuk suatu level.

        Return:
          held_count  : berapa kali level bertahan
          broke_count : berapa kali level tembus
          total_tests : total pengujian
          hold_rate   : persentase bertahan (0.0-1.0)
          last_result : 'held' | 'broke' | ''
          days_since  : berapa hari sejak test terakhir
          fresh       : apakah level baru ditest < 7 hari
        """
        empty = {
            'held_count': 0, 'broke_count': 0, 'total_tests': 0,
            'hold_rate': 0.5, 'last_result': '', 'days_since': 999, 'fresh': False
        }
        sym = symbol.upper()
        key = self._find_nearest_key(sym, level_price)
        if not key:
            return empty

        entry     = self._data[sym][key]
        held      = entry.get('held_count',  0)
        broke     = entry.get('broke_count', 0)
        total     = held + broke
        hold_rate = held / total if total > 0 else 0.5
        now       = time.time()
        days_since = (now - entry.get('last_test', 0)) / 86400

        return {
            'held_count'  : held,
            'broke_count' : broke,
            'total_tests' : total,
            'hold_rate'   : hold_rate,
            'last_result' : entry.get('last_result', ''),
            'days_since'  : round(days_since, 1),
            'fresh'       : days_since < 7,
        }

    def get_score_modifier(self, symbol: str, level_price: float,
                            atr: float, direction: str) -> tuple:
        """
        Hitung bonus/penalty score untuk level berdasarkan riwayat.

        Return: (score_mod: int, desc: str)
          +8  : Level sangat teruji, selalu bertahan → sangat kuat
          +5  : Level teruji, sering bertahan
          +3  : Level pernah bertahan
           0  : Tidak ada data historis
          -5  : Level baru saja tembus → flip zone
          -8  : Level selalu tembus → jangan trade
        """
        stats = self.get_level_stats(symbol, level_price)
        total = stats['total_tests']

        if total == 0:
            return 0, ''

        hold_rate   = stats['hold_rate']
        last_result = stats['last_result']
        held        = stats['held_count']
        broke       = stats['broke_count']
        days_since  = stats['days_since']

        # Level baru saja tembus = flip zone → JANGAN trade ke arah lama
        if last_result == 'broke' and days_since < 14:
            if direction == 'LONG' and total >= 2:
                return -5, f'Level baru tembus {days_since:.0f} hari lalu — flip zone, hindari LONG'
            if direction == 'SHORT' and total >= 2:
                return -5, f'Level baru tembus {days_since:.0f} hari lalu — flip zone, hindari SHORT'

        # Level selalu tembus → lemah
        if total >= 3 and hold_rate < 0.3:
            return -8, f'Level ini {broke}x tembus dari {total}x test — sangat lemah'

        # Level sering bertahan → kuat
        if total >= 5 and hold_rate >= 0.75:
            return 8, f'Level teruji {total}x, {held}x bertahan ({hold_rate:.0%}) — sangat kuat'
        if total >= 3 and hold_rate >= 0.70:
            return 5, f'Level teruji {total}x, {held}x bertahan ({hold_rate:.0%}) — kuat'
        if total >= 2 and hold_rate >= 0.60:
            return 3, f'Level teruji {total}x, {held}x bertahan — cukup terpercaya'

        return 0, ''

    # ── Auto-record from trade results ────────────────────────────────────────

    def auto_record_from_signal(self, symbol: str, level_price: float,
                                 entry_price: float, direction: str,
                                 result_pnl: float):
        """
        Otomatis catat hasil level setelah trade selesai.
        Dipanggil dari learning_engine atau bitunix_trader setelah trade close.

        Logic:
          - Trade profit → level 'held' (harga respek level)
          - Trade loss (SL kena) → level 'broke' (harga tembus level)
        """
        if result_pnl > 0:
            self.record_test(symbol, level_price, 'held', direction)
        else:
            self.record_test(symbol, level_price, 'broke', direction)
        self.save()

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.save()


# Singleton instance
_instance: Optional[LevelMemory] = None

def get_level_memory() -> LevelMemory:
    global _instance
    if _instance is None:
        _instance = LevelMemory()
    return _instance
