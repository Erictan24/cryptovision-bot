"""
session_filter.py — Trading session detection dan quality adjustment.

Crypto market punya tiga session utama (UTC):
  Asia   : 00:00 - 08:00 UTC  (liquidity rendah, mudah dimanipulasi)
  London : 07:00 - 16:00 UTC  (liquidity tinggi, setup lebih reliable)
  NY     : 13:00 - 21:00 UTC  (liquidity tertinggi, volume besar)
  Overlap: 13:00 - 16:00 UTC  (London + NY overlap = terbaik)

Kenapa session penting:
  Signal yang terbentuk di Asia session saat London dan NY tutup rentan
  stop hunt karena market maker bisa lebih mudah menggerakkan harga
  dengan volume kecil. Setup yang sama di London/NY open lebih reliable
  karena ada institutional participation yang sesungguhnya.

Data historis crypto:
  Asia session   : 40-45% false breakout rate
  London open    : false breakout turun ke 25-30%
  NY open        : false breakout turun ke 20-25%
  London-NY overlap: false breakout terendah ~18%
"""

from datetime import datetime, timezone


def get_current_session(dt: datetime = None) -> dict:
    """
    Deteksi session trading berdasarkan waktu UTC.

    Returns:
        dict dengan:
          'session'   : str  — 'OVERLAP', 'LONDON', 'NY', 'ASIA', 'DEAD'
          'quality'   : str  — 'PREMIUM', 'GOOD', 'MODERATE', 'LOW'
          'max_tier'  : str  — tier maksimum yang diizinkan di session ini
          'desc'      : str  — deskripsi untuk signal reasons
          'multiplier': float — score multiplier (1.0 = no change)
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    hour = dt.hour

    # London-NY overlap: 13:00-16:00 UTC — terbaik
    if 13 <= hour < 16:
        return {
            'session'   : 'OVERLAP',
            'quality'   : 'PREMIUM',
            'max_tier'  : 'GOOD',     # Tidak ada batasan
            'desc'      : 'London-NY overlap — likuiditas tertinggi',
            'multiplier': 1.0,
            'restrict'  : False,
        }

    # London session: 07:00-16:00 UTC
    if 7 <= hour < 16:
        return {
            'session'   : 'LONDON',
            'quality'   : 'GOOD',
            'max_tier'  : 'GOOD',     # Tidak ada batasan
            'desc'      : 'London session — likuiditas tinggi',
            'multiplier': 1.0,
            'restrict'  : False,
        }

    # NY session: 13:00-21:00 UTC (sudah tercakup overlap di atas)
    if 16 <= hour < 21:
        return {
            'session'   : 'NY',
            'quality'   : 'GOOD',
            'max_tier'  : 'GOOD',     # Tidak ada batasan
            'desc'      : 'NY session — volume tinggi',
            'multiplier': 1.0,
            'restrict'  : False,
        }

    # Asia session: 00:00-07:00 UTC
    if 0 <= hour < 7:
        return {
            'session'   : 'ASIA',
            'quality'   : 'LOW',
            'max_tier'  : 'MODERATE', # Max MODERATE di Asia session
            'desc'      : 'Asia session — likuiditas rendah, risiko stop hunt',
            'multiplier': 0.9,
            'restrict'  : True,       # Apply restriction
        }

    # Dead zone: 21:00-00:00 UTC — terendah
    return {
        'session'   : 'DEAD',
        'quality'   : 'LOW',
        'max_tier'  : 'MODERATE',     # Max MODERATE
        'desc'      : 'Dead zone (21:00-00:00 UTC) — likuiditas sangat rendah',
        'multiplier': 0.85,
        'restrict'  : True,
    }


def apply_session_filter(q: str, conf: list, df_main=None) -> tuple:
    """
    Apply session filter ke quality signal.

    Di Asia session dan dead zone:
      GOOD → MODERATE (restrict)
      IDEAL → MODERATE (restrict)

    Di London/NY/Overlap:
      Tidak ada perubahan
    """
    if df_main is None or not hasattr(df_main.index, 'tzinfo'):
        return q, conf

    try:
        last_ts = df_main.index[-1]
        if hasattr(last_ts, 'to_pydatetime'):
            last_ts = last_ts.to_pydatetime()
        session_info = get_current_session(last_ts)
    except Exception:
        return q, conf

    if not session_info['restrict']:
        return q, conf

    # Apply restriction
    max_tier = session_info['max_tier']
    tier_order = ['WAIT', 'MODERATE', 'GOOD', 'IDEAL']

    if q and q in tier_order:
        current_idx = tier_order.index(q)
        max_idx     = tier_order.index(max_tier)

        if current_idx > max_idx:
            q = max_tier
            conf.append(f"{session_info['desc']} — max {max_tier}")

    return q, conf


def get_session_from_df(df) -> dict:
    """Helper untuk get session info dari DataFrame index."""
    try:
        last_ts = df.index[-1]
        if hasattr(last_ts, 'to_pydatetime'):
            last_ts = last_ts.to_pydatetime()
        return get_current_session(last_ts)
    except Exception:
        return get_current_session(datetime(2000, 1, 1, 12, 0, tzinfo=timezone.utc))
