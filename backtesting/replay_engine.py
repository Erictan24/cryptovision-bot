"""
replay_engine.py v2 — BacktestEngine untuk backtesting offline.

FIXES:
  - 15m bug: TF_HIERARCHY ltf=None → ltf="15m"
  - get_price: lebih robust, coba semua TF
  - _get_slice: handle edge case
  - Disable zone cache antar scan points
"""

import sys
import pandas as pd
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from trading_engine import TradingEngine, TF_CONFIG
import signal_generator as _sg


# HTF dan LTF untuk setiap main TF
# FIX: 15m pakai ltf="15m" bukan None — ini yang menyebabkan 0 signal sebelumnya
TF_HIERARCHY = {
    "15m": {"htf": "1h",  "ltf": "15m"},
    "1h":  {"htf": "4h",  "ltf": "15m"},
    "4h":  {"htf": "1d",  "ltf": "1h"},
}


class BacktestEngine(TradingEngine):
    """TradingEngine tanpa network — semua data dari historical_data dict."""

    def __init__(self, historical_data: dict):
        self._data       = historical_data
        self._symbol     = None
        self._tf         = None
        self._scan_index = None
        self._scan_time  = None

        self.exchange        = "Backtest"
        self.ohlcv_cache     = {}
        self.price_cache     = {}
        self.sr_cache        = {}
        self.CACHE_TTL       = 9999
        self.SR_CACHE_TTL    = 9999
        self._signal_cache       = {}
        self._zone_cache         = {}
        self._whale_cache        = {}   # Diperlukan oleh _check_whale_sentiment
        self._SIGNAL_LOCK_HOURS  = 4    # Diperlukan oleh signal direction cache
        self._ZONE_PERSIST_HOURS = 12   # Diperlukan oleh zone persistence
        self._WHALE_TTL          = 1800 # Diperlukan oleh _check_whale_sentiment TTL
        self._zone_file          = None
        self._call_times     = []
        self.cc_base         = ""
        self.cc_base_v1      = ""
        self._per_coin_sp    = None  # Per-coin SIGNAL_PARAMS override

        import threading
        self._rate_lock         = threading.Lock()
        self._MAX_CALLS_PER_MIN = 999

        # Aktifkan backtest mode — session filter tidak memblokir sinyal historis
        _sg.set_backtest_mode(True)

    def set_context(self, symbol: str, tf: str, scan_index: int):
        """Set titik waktu simulasi. Dipanggil sebelum setiap analyze_coin()."""
        self._symbol     = symbol
        self._tf         = tf
        self._scan_index = scan_index

        sym_data = self._data.get(symbol, {})
        df_main  = sym_data.get(tf)
        if df_main is not None and scan_index < len(df_main):
            self._scan_time = df_main.iloc[scan_index]["timestamp"]
        else:
            self._scan_time = None

        # Clear cache agar tidak ada data leak antar scan points
        self.ohlcv_cache.clear()
        self.price_cache.clear()
        self.sr_cache.clear()
        self._zone_cache.clear()

    def _get_slice(self, symbol: str, tf_key: str,
                   min_candles: int = 10) -> pd.DataFrame | None:
        """Ambil data historis yang sudah ditruncate ke scan_time."""
        sym_data = self._data.get(symbol, {})
        df = sym_data.get(tf_key)

        if df is None or len(df) < min_candles:
            return None

        if self._scan_time is not None:
            df_sliced = df[df["timestamp"] <= self._scan_time].copy()
        else:
            end = self._scan_index if self._scan_index else len(df)
            df_sliced = df.iloc[:end].copy()

        if len(df_sliced) < min_candles:
            return None

        # Hapus candle terakhir (belum close) — simulasikan kondisi live
        return df_sliced.iloc[:-1].reset_index(drop=True)

    def get_klines(self, symbol: str, tf_key: str,
                   is_higher: bool = False, is_lower: bool = False) -> pd.DataFrame | None:
        """Override: return pre-fetched slice, tanpa network call."""
        hier = TF_HIERARCHY.get(self._tf or tf_key, {})

        if is_higher:
            target_tf = hier.get("htf", "4h")
        elif is_lower:
            target_tf = hier.get("ltf")
            if target_tf is None:
                target_tf = self._tf or tf_key  # fallback ke main TF
        else:
            target_tf = self._tf or tf_key

        return self._get_slice(symbol, target_tf)

    def get_price(self, symbol: str) -> dict | None:
        """Override: return harga historis di titik scan_time."""
        # Cek cache — trading_engine._cache_get simpan sebagai (data, timestamp) tuple
        cached = self.price_cache.get(symbol)
        if isinstance(cached, tuple) and len(cached) == 2:
            return cached[0]  # ambil data-nya saja, skip timestamp
        if isinstance(cached, dict) and cached:
            return cached

        sym_data = self._data.get(symbol, {})

        # Coba TF secara berurutan
        for try_tf in [self._tf or "1h", "1h", "4h", "15m", "1d"]:
            df = sym_data.get(try_tf)
            if df is None or len(df) < 2:
                continue

            if self._scan_time is not None:
                mask = df["timestamp"] <= self._scan_time
                if not mask.any():
                    continue
                row_idx = mask.values.nonzero()[0][-1]
            elif self._scan_index is not None:
                row_idx = min(self._scan_index, len(df) - 1)
            else:
                row_idx = len(df) - 1

            price = float(df.iloc[row_idx]["close"])
            if price <= 0:
                continue

            # 24h change
            lookback = {"15m": 96, "1h": 24, "4h": 6, "1d": 1}.get(try_tf, 24)
            prev_idx = max(0, row_idx - lookback)
            prev_price = float(df.iloc[prev_idx]["close"])
            chg24 = ((price - prev_price) / max(prev_price, 1)) * 100

            recent = df.iloc[prev_idx:row_idx + 1]
            result = {
                "price"      : price,
                "change_24h" : round(chg24, 2),
                "high_24h"   : float(recent["high"].max()),
                "low_24h"    : float(recent["low"].min()),
                "volume_24h" : 1_000_000,
                "market_cap" : 1_000_000_000,
            }
            import time as _time
            self.price_cache[symbol] = (result, _time.time())
            return result

        return None

    def fetch_derivatives(self, symbol: str) -> dict:
        """Disable: data derivatives tidak tersedia secara historis."""
        return {
            "funding_rate": 0, "funding_bias": "NEUTRAL",
            "oi": 0, "oi_change_pct": 0, "oi_bias": "NEUTRAL",
            "lsr": 1.0, "lsr_bias": "NEUTRAL",
            "sentiment": "NEUTRAL", "available": False,
        }

    def check_btc_condition(self) -> dict:
        """Gunakan historical BTC 1H data untuk filter."""
        try:
            btc_data = self._data.get("BTC", {})
            df = btc_data.get("1h")

            if df is None or len(df) < 25:
                return {"ok_long": True, "ok_short": True, "btc_bias": "NEUTRAL",
                        "btc_change": 0, "reason": "BTC data not available"}

            if self._scan_time is not None:
                df = df[df["timestamp"] <= self._scan_time].copy()
            if len(df) < 25:
                return {"ok_long": True, "ok_short": True, "btc_bias": "NEUTRAL",
                        "btc_change": 0, "reason": "BTC data insufficient"}

            from indicators import analyze_ema_trend, detect_market_structure, calc_rsi
            from config import SIGNAL_PARAMS as SP

            et, _, _  = analyze_ema_trend(df)
            structure = detect_market_structure(df)
            rsi_s     = calc_rsi(df, 14)
            rsi_val   = float(rsi_s.iloc[-1]) if rsi_s is not None and not pd.isna(rsi_s.iloc[-1]) else 50.0

            price_now  = float(df["close"].iloc[-1])
            price_24h  = float(df["close"].iloc[-25])
            btc_chg    = ((price_now - price_24h) / max(price_24h, 1)) * 100

            btc_bull = sum([et in ("STRONG_UP","UP"), structure == "UPTREND",
                            btc_chg > SP["btc_bull_change_soft"]])
            btc_bear = sum([et in ("STRONG_DOWN","DOWN"), structure == "DOWNTREND",
                            btc_chg < SP["btc_bear_change_soft"]])

            if btc_bear >= SP["btc_bear_evidence_min"] or btc_chg < SP["btc_bear_change_hard"]:
                return {"ok_long": False, "ok_short": True, "btc_bias": "BEARISH",
                        "btc_change": btc_chg, "btc_rsi": rsi_val,
                        "reason": f"BTC bearish ({btc_chg:+.1f}%)"}
            elif btc_bull >= SP["btc_bull_evidence_min"]:
                return {"ok_long": True, "ok_short": False, "btc_bias": "BULLISH",
                        "btc_change": btc_chg, "btc_rsi": rsi_val,
                        "reason": f"BTC bullish ({btc_chg:+.1f}%)"}

            return {"ok_long": True, "ok_short": True, "btc_bias": "NEUTRAL",
                    "btc_change": btc_chg, "btc_rsi": rsi_val,
                    "reason": f"BTC netral ({btc_chg:+.1f}%)"}
        except Exception as e:
            return {"ok_long": True, "ok_short": True, "btc_bias": "NEUTRAL",
                    "btc_change": 0, "reason": f"BTC check error: {e}"}

    # Disable semua method yang butuh network/file
    def _load_zone_locks(self): return {}
    def _save_zone_locks(self): pass
    def _test_url(self, url): return True
    def _wait_rate_limit(self): pass
    def _http_get(self, url, params=None, timeout=12): return None