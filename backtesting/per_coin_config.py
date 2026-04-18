"""
per_coin_config.py — Konfigurasi adaptif per coin.

Membaca coin_profiles.json dan menghasilkan override SIGNAL_PARAMS
yang spesifik untuk setiap coin.

Cara kerja:
  1. coin_profiler.py membuat coin_profiles.json
  2. per_coin_config.py membaca profil dan menghasilkan override
  3. signal_generator.py mendapat override ini saat scoring

Integrasi dengan signal_generator.py:
  from backtesting.per_coin_config import get_coin_params
  sp = get_coin_params("ETH")  # Returns SP dict yang di-override untuk ETH
"""

import json
from pathlib import Path
from functools import lru_cache

PROFILES_FILE = Path(__file__).parent / "coin_profiles.json"

# Default base params — akan di-override per coin
_BASE_PARAMS = None


def _load_base_params():
    global _BASE_PARAMS
    if _BASE_PARAMS is None:
        from config import SIGNAL_PARAMS
        _BASE_PARAMS = dict(SIGNAL_PARAMS)
    return _BASE_PARAMS


@lru_cache(maxsize=None)
def _load_profiles() -> dict:
    """Load profil coin dari file. Di-cache agar tidak re-read terus."""
    if not PROFILES_FILE.exists():
        return {}
    try:
        with open(PROFILES_FILE) as f:
            return json.load(f)
    except:
        return {}


def reload_profiles():
    """Force reload profil (panggil ini setelah profiler dijalankan ulang)."""
    _load_profiles.cache_clear()


def get_coin_params(symbol: str) -> dict:
    """
    Dapatkan SIGNAL_PARAMS yang di-override untuk coin ini.
    TIDAK pernah kembalikan None — coin di SCAN_POOL harus tetap di-scan.
    Profil hanya dipakai untuk adjust parameter, bukan untuk blok coin.
    """
    base     = dict(_load_base_params())
    profiles = _load_profiles()

    if symbol not in profiles:
        return base

    profile   = profiles[symbol]
    overrides = profile.get("config_overrides", {})
    tradeable = profile.get("tradeable", "INSUFFICIENT_DATA")

    # Apply overrides ke base params
    merged = dict(base)

    if "sl_atr_buffer" in overrides:
        merged["sl_atr_buffer"] = overrides["sl_atr_buffer"]

    # score_ideal TIDAK di-override per coin — IDEAL dinonaktifkan global (999)
    # Override score_ideal dari profil lama akan merusak filter IDEAL

    # ATURAN PENTING: Per-coin hanya boleh MEMPERKETAT threshold, tidak melonggarkan.
    # Contoh: global score_good=22, per-coin score_good=6 → pakai MAX(6,22) = 22
    # Ini mencegah profil lama (score_good=6) merusak filter baru yang lebih ketat.
    if "score_good" in overrides:
        merged["score_good"] = max(overrides["score_good"], base.get("score_good", 22))
    if "score_moderate" in overrides:
        merged["score_moderate"] = max(overrides["score_moderate"], base.get("score_moderate", 21))
    if "adx_ranging_block" in overrides:
        merged["adx_ranging_block"] = overrides["adx_ranging_block"]

    # Pastikan threshold kritis selalu ikut dari base (tidak bisa di-override per coin)
    merged["score_hard_reject"] = base.get("score_hard_reject", 24)
    merged["score_cap_good"]    = base.get("score_cap_good", 24)
    merged["score_ideal"]       = base.get("score_ideal", 999)  # Selalu dari base
    # min_confluence_score juga harus ikut base — per-coin tidak boleh longgarkan
    merged["min_confluence_score"] = base.get("min_confluence_score", 21)

    merged["_preferred_direction"] = overrides.get("preferred_direction", "BOTH")
    merged["_preferred_tf"]        = overrides.get("preferred_tf", "BOTH")
    merged["_tradeable"]           = tradeable
    merged["_profile_ev"]          = profile.get("overall_ev", 0)
    merged["_profile_wr"]          = profile.get("overall_wr", 0)

    return merged


def get_all_tradeable_coins() -> list:
    """Kembalikan list coin yang tradeable berdasarkan profil."""
    profiles = _load_profiles()
    tradeable = []
    for sym, p in profiles.items():
        if p.get("tradeable") in ("YES", "BORDERLINE"):
            tradeable.append(sym)
    return sorted(tradeable)


def get_coin_score_range(symbol: str) -> tuple:
    """
    Kembalikan (min_score, max_score) optimal untuk coin ini.
    Dipakai untuk filter signal yang lebih presisi.
    """
    profiles = _load_profiles()
    if symbol not in profiles:
        return (3, 99)  # Default: tidak ada filter

    ss = profiles[symbol].get("score_sweetspot", {})
    return (
        ss.get("sweetspot_min", 3),
        ss.get("sweetspot_max", 99)
    )


def get_preferred_direction(symbol: str) -> str:
    """LONG_PREFERRED, SHORT_PREFERRED, atau BOTH."""
    profiles = _load_profiles()
    if symbol not in profiles:
        return "BOTH"
    return profiles[symbol].get("best_direction", "BOTH")


def get_preferred_tf(symbol: str) -> str:
    """TF terbaik untuk coin ini: '15m', '1h', '4h', atau 'BOTH'."""
    profiles = _load_profiles()
    if symbol not in profiles:
        return "BOTH"
    return profiles[symbol].get("best_tf", "BOTH")


def print_summary():
    """Print ringkasan per-coin config untuk semua coin."""
    profiles = _load_profiles()
    if not profiles:
        print("Tidak ada profil. Jalankan: python backtesting/coin_profiler.py")
        return

    print(f"\n{'='*70}")
    print(f"  PER-COIN CONFIG SUMMARY — {len(profiles)} coins")
    print(f"{'='*70}")
    print(f"\n  {'Coin':<8} {'EV':>7} {'SL_buf':>7} {'Score':>12} {'Direction':<16} {'TF':<8}")
    print(f"  {'-'*65}")

    for sym in sorted(profiles.keys()):
        p  = profiles[sym]
        ov = p.get("config_overrides", {})
        ev_v = p.get("overall_ev", 0)
        trd  = p.get("tradeable", "?")
        flag = "✓" if trd == "YES" else ("~" if trd == "BORDERLINE" else "✗")

        score_range = f"{ov.get('score_moderate',3)}/{ov.get('score_good',9)}/{ov.get('score_ideal',12)}"
        print(f"  {sym:<8} {ev_v:>+.3f}R  {ov.get('sl_atr_buffer',1.0):>6.1f}  "
              f"{score_range:>12}  {ov.get('preferred_direction','BOTH'):<16} "
              f"{ov.get('preferred_tf','BOTH'):<8} {flag}")

    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    print_summary()