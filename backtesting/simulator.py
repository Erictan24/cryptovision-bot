"""
simulator.py v2 — Forward simulate trade outcomes.

FIXES:
  - Dedup window diperpanjang jadi 8 jam (dari 4 jam)
  - Dedup per (symbol, tf, direction) — lebih spesifik
  - EXPIRED dihitung -0.5R (opportunity cost)
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional


OUTCOME_TP2     = "TP2_HIT"
OUTCOME_TP1     = "TP1_HIT"
OUTCOME_SL      = "SL_HIT"
OUTCOME_EXPIRED = "EXPIRED"

# Max bars untuk tunggu outcome per TF
MAX_BARS = {
    "15m": 96,     # 24 jam
    "1h" : 72,     # 3 hari
    "4h" : 30,     # 5 hari
    "1d" : 10,     # 10 hari
}


@dataclass
class TradeResult:
    symbol          : str
    tf              : str
    direction       : str
    quality         : str
    entry           : float
    sl              : float
    tp1             : float
    tp2             : float
    rr1             : float
    rr2             : float
    confluence_score: int
    kill_count      : int
    scan_timestamp  : pd.Timestamp
    outcome         : str
    bars_to_outcome : int
    pnl_r           : float
    reasons         : list = field(default_factory=list)

    @property
    def is_win_tp2(self):  return self.outcome == OUTCOME_TP2
    @property
    def is_win_tp1(self):  return self.outcome in (OUTCOME_TP1, OUTCOME_TP2)
    @property
    def is_loss(self):     return self.outcome == OUTCOME_SL
    @property
    def is_expired(self):  return self.outcome == OUTCOME_EXPIRED


def simulate_outcome(signal: dict, df_future: pd.DataFrame,
                     tf: str, max_bars: int = None) -> tuple[str, int, float]:
    """
    Scan forward dari candle berikutnya setelah signal.
    Return: (outcome, bars_elapsed, pnl_r)
    """
    direction = signal["direction"]
    entry     = signal["entry"]
    sl        = signal["sl"]
    tp1       = signal.get("tp1", entry)
    tp2       = signal.get("tp2", entry)
    rr1       = signal.get("rr1", 1.0)
    rr2       = signal.get("rr2", 2.0)

    if max_bars is None:
        max_bars = MAX_BARS.get(tf, 48)

    if df_future is None or len(df_future) == 0:
        return OUTCOME_EXPIRED, 0, -0.5

    limit   = min(len(df_future), max_bars)
    h       = df_future["high"].values[:limit]
    l       = df_future["low"].values[:limit]
    tp1_hit = False

    # ── LIMIT ORDER: tunggu harga sampai ke entry dulu ──
    # Kalau order_type == 'LIMIT', harga belum di entry.
    # Scan forward sampai harga menyentuh entry level, baru mulai simulasi.
    start_bar = 0
    if signal.get('order_type') == 'LIMIT' and not signal.get('at_zone', False):
        filled = False
        for i in range(limit):
            if direction == 'LONG' and l[i] <= entry:
                filled = True
                start_bar = i + 1
                break
            elif direction == 'SHORT' and h[i] >= entry:
                filled = True
                start_bar = i + 1
                break
        if not filled:
            return OUTCOME_EXPIRED, limit, 0  # Harga tidak pernah sampai ke level

    for i in range(start_bar, limit):
        if direction == "LONG":
            # SL check dulu (worst case)
            if l[i] <= sl:
                if tp1_hit:
                    return OUTCOME_TP1, i + 1, rr1
                return OUTCOME_SL, i + 1, -1.0

            if h[i] >= tp2:
                return OUTCOME_TP2, i + 1, rr2

            if h[i] >= tp1 and not tp1_hit:
                tp1_hit = True
                sl      = entry  # geser SL ke breakeven

        else:  # SHORT
            if h[i] >= sl:
                if tp1_hit:
                    return OUTCOME_TP1, i + 1, rr1
                return OUTCOME_SL, i + 1, -1.0

            if l[i] <= tp2:
                return OUTCOME_TP2, i + 1, rr2

            if l[i] <= tp1 and not tp1_hit:
                tp1_hit = True
                sl      = entry

    if tp1_hit:
        return OUTCOME_TP1, limit, rr1
    return OUTCOME_EXPIRED, limit, -0.5


def simulate_all(signals_with_future: list, tf: str) -> list[TradeResult]:
    """Simulate semua signals sekaligus."""
    results = []
    for signal, df_future, scan_ts in signals_with_future:
        outcome, bars, pnl = simulate_outcome(signal, df_future, tf)
        results.append(TradeResult(
            symbol           = signal.get("_symbol", ""),
            tf               = tf,
            direction        = signal["direction"],
            quality          = signal["quality"],
            entry            = signal["entry"],
            sl               = signal["sl"],
            tp1              = signal.get("tp1", 0),
            tp2              = signal.get("tp2", 0),
            rr1              = signal.get("rr1", 1.0),
            rr2              = signal.get("rr2", 2.0),
            confluence_score = signal.get("confluence_score", 0),
            kill_count       = signal.get("kill_count", 0),
            scan_timestamp   = scan_ts,
            outcome          = outcome,
            bars_to_outcome  = bars,
            pnl_r            = pnl,
            reasons          = signal.get("reasons", []),
        ))
    return results


def dedup_signals(signals: list) -> list:
    """
    Hapus sinyal duplikat: arah+coin+tf sama dalam 8 jam.
    FIX: window diperpanjang 4→8 jam, key lebih spesifik.
    """
    last_signal: dict = {}
    LOCK_HOURS = 8     # FIX: was 4
    deduplicated = []

    for item in signals:
        signal, df_future, scan_ts = item
        symbol    = signal.get("_symbol", "")
        tf        = signal.get("_tf", "")
        direction = signal["direction"]
        # FIX: key include tf sekarang untuk dedup yang lebih tepat
        key = (symbol, tf, direction)

        last_ts = last_signal.get(key)
        if last_ts is not None:
            hours_since = (scan_ts - last_ts).total_seconds() / 3600
            if hours_since < LOCK_HOURS:
                continue

        last_signal[key] = scan_ts
        deduplicated.append(item)

    return deduplicated
