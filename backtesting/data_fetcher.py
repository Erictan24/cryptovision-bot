"""
data_fetcher.py v4 — Binance public API (tidak perlu API key).

KENAPA BINANCE:
  - Gratis, tidak perlu daftar atau API key
  - 15m: bisa dapat 3+ tahun data ke belakang
  - 1H: sama, tidak terbatas
  - 4H: sama
  - 1000 candles per call, pagination dengan startTime/endTime
  - Rate limit: 1200 req/menit (lebih dari cukup)

CARA PAGINATION BINANCE:
  GET /api/v3/klines?symbol=BTCUSDT&interval=15m&endTime=<ts>&limit=1000
  → Dapat 1000 candles SEBELUM endTime
  → Set endTime = candle terlama - 1ms untuk halaman berikutnya
  → Ulangi sampai dapat semua data yang dibutuhkan
"""

import os
import time
import pickle
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import SCAN_POOL

BINANCE_BASE = "https://api.binance.com/api/v3/klines"
CACHE_DIR    = Path(__file__).parent / "cache"
CACHE_FILE   = CACHE_DIR / "historical_data.pkl"
META_FILE    = CACHE_DIR / "fetch_meta.pkl"

# Candles per hari per TF
CANDLES_PER_DAY = {
    "15m": 96,
    "1h" : 24,
    "4h" :  6,
    "1d" :  1,
}

# Binance interval string
BINANCE_INTERVAL = {
    "15m": "15m",
    "1h" : "1h",
    "4h" : "4h",
    "1d" : "1d",
}

MAX_PER_CALL     = 1000   # Binance max per call
MAX_CALLS_PER_MIN = 60    # Conservative (Binance limit: 1200/min)


class DataFetcher:
    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0"})
        self._call_times = []
        self._lock       = threading.Lock()

    # ----------------------------------------------------------
    # Rate limiter
    # ----------------------------------------------------------
    def _wait(self):
        with self._lock:
            now = time.time()
            self._call_times = [t for t in self._call_times if now - t < 60]
            if len(self._call_times) >= MAX_CALLS_PER_MIN:
                wait = 60 - (now - self._call_times[0]) + 0.5
                if wait > 0:
                    print(f"  [rate limit] tunggu {wait:.0f}s...", flush=True)
                    time.sleep(wait)
                    self._call_times = [t for t in self._call_times if time.time() - t < 60]
            self._call_times.append(time.time())

    def _get(self, params: dict, retries: int = 3):
        for attempt in range(retries):
            self._wait()
            try:
                r = self._session.get(BINANCE_BASE, params=params, timeout=15)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    print(f"  [429] tunggu 30s...", flush=True)
                    time.sleep(30)
                    continue
                if r.status_code == 418:
                    print(f"  [418] IP banned, tunggu 60s...", flush=True)
                    time.sleep(60)
                    continue
                print(f"  [HTTP {r.status_code}]", flush=True)
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(3)
                else:
                    print(f"  [error] {e}", flush=True)
        return None

    # ----------------------------------------------------------
    # Fetch satu TF untuk satu symbol
    # ----------------------------------------------------------
    def fetch_ohlcv(self, symbol: str, tf_key: str,
                    days: int = 180) -> pd.DataFrame | None:
        """
        Fetch historical OHLCV dari Binance.

        Binance symbol format: BTCUSDT (tidak ada slash)
        Pagination: endTime mundur dari sekarang ke masa lalu
        """
        if tf_key not in CANDLES_PER_DAY:
            return None

        interval      = BINANCE_INTERVAL[tf_key]
        total_needed  = int(CANDLES_PER_DAY[tf_key] * days) + 100
        binance_symbol = symbol.upper() + "USDT"

        all_candles = []
        end_time    = None    # None = sekarang (candle terbaru)
        max_pages   = int(total_needed / MAX_PER_CALL) + 3

        for page in range(max_pages):
            params = {
                "symbol"  : binance_symbol,
                "interval": interval,
                "limit"   : MAX_PER_CALL,
            }
            if end_time is not None:
                params["endTime"] = int(end_time)

            data = self._get(params)
            if not data or len(data) == 0:
                break

            # Binance format: [openTime, open, high, low, close, vol,
            #                   closeTime, quoteVol, trades, ...]
            # Prepend ke all_candles (data ini lebih lama)
            all_candles = data + all_candles

            # Set end_time ke openTime candle pertama - 1ms
            oldest_open_time = data[0][0]
            end_time = oldest_open_time - 1

            # Cek apakah sudah cukup
            if len(all_candles) >= total_needed:
                break

            # Kalau dapat lebih sedikit dari limit → data habis
            if len(data) < MAX_PER_CALL:
                break

        if not all_candles or len(all_candles) < 10:
            return None

        try:
            df = pd.DataFrame(all_candles, columns=[
                "timestamp","open","high","low","close","volume",
                "close_time","quote_vol","trades","taker_buy_base",
                "taker_buy_quote","ignore"
            ])
            df = df[["timestamp","open","high","low","close","volume"]].copy()
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            for col in ["open","high","low","close","volume"]:
                df[col] = df[col].astype(float)

            # Deduplicate dan sort
            df = df.drop_duplicates("timestamp").sort_values("timestamp")
            df = df[df["close"] > 0].reset_index(drop=True)

            # Filter ke target hari
            cutoff = pd.Timestamp.now() - pd.Timedelta(days=days + 2)
            df     = df[df["timestamp"] >= cutoff].reset_index(drop=True)

            # Hapus candle terakhir (belum close)
            if len(df) > 5:
                df = df.iloc[:-1].reset_index(drop=True)

            return df if len(df) >= 20 else None

        except Exception as e:
            print(f"  [parse error] {symbol} {tf_key}: {e}", flush=True)
            return None

    # ----------------------------------------------------------
    # Fetch semua data
    # ----------------------------------------------------------
    def fetch_all(self, coins: list = None, days: int = 180,
                  tfs: list = None,
                  force_refresh: bool = False) -> dict:
        """
        Fetch semua data untuk semua coins dan TFs.
        Default TFs: 15m, 1h, 4h
        """
        if coins is None:
            coins = SCAN_POOL
        if tfs is None:
            tfs = ["15m", "1h", "4h"]

        # Cek cache
        if not force_refresh and CACHE_FILE.exists() and META_FILE.exists():
            with open(META_FILE, "rb") as f:
                meta = pickle.load(f)
            age_h        = (time.time() - meta.get("ts", 0)) / 3600
            cached_days  = meta.get("days", 0)
            cached_coins = set(meta.get("coins", []))
            cached_tfs   = set(meta.get("tfs", []))

            if (age_h < 23 and cached_days >= days
                    and set(coins).issubset(cached_coins)
                    and set(tfs).issubset(cached_tfs)):
                print(f"Loading cached data ({age_h:.1f}h old, {cached_days} hari)...",
                      flush=True)
                with open(CACHE_FILE, "rb") as f:
                    return pickle.load(f)
            else:
                if age_h >= 23:
                    reason = f"cache terlalu lama ({age_h:.1f}h)"
                elif cached_days < days:
                    reason = f"period tidak cukup ({cached_days} < {days} hari)"
                elif not set(tfs).issubset(cached_tfs):
                    reason = f"TF baru: {set(tfs) - cached_tfs}"
                else:
                    reason = "coins berubah"
                print(f"Cache tidak valid ({reason}), re-fetching...", flush=True)

        total      = len(coins) * len(tfs)
        done       = 0
        start_time = time.time()
        data       = {}

        print(f"\nFetching {len(coins)} coins × {len(tfs)} TFs × {days} hari")
        print(f"Source: Binance public API (tidak perlu API key)")
        print(f"Estimasi: ~{total * 2 / 60:.0f} menit\n")

        for symbol in coins:
            data[symbol] = {}
            for tf in tfs:
                done += 1
                elapsed = time.time() - start_time
                eta     = (elapsed / done * (total - done)) if done > 1 else 0
                eta_str = f"{eta/60:.1f}m" if eta > 60 else f"{eta:.0f}s"

                print(f"  [{done:>3}/{total}] {symbol:<8} {tf:<4}  "
                      f"(ETA: {eta_str})", end="", flush=True)

                df = self.fetch_ohlcv(symbol, tf, days)
                if df is not None and len(df) >= 20:
                    data[symbol][tf] = df
                    span = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).days
                    print(f"  {len(df):,} candles / {span} hari OK", flush=True)
                else:
                    print(f"  SKIP (data tidak tersedia)", flush=True)

        # Simpan cache
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(data, f)
        with open(META_FILE, "wb") as f:
            pickle.dump({
                "ts"    : time.time(),
                "days"  : days,
                "coins" : coins,
                "tfs"   : tfs,
                "source": "binance",
            }, f)

        elapsed_min = (time.time() - start_time) / 60
        print(f"\nData fetched: {len(data)} coins, {elapsed_min:.1f} menit")
        self._print_coverage(data, tfs)
        return data

    def _print_coverage(self, data: dict, tfs: list):
        """Tampilkan ringkasan data yang berhasil di-fetch."""
        print("\n=== COVERAGE SUMMARY ===")
        ok = miss = 0
        for sym in sorted(data.keys()):
            parts = []
            for tf in tfs:
                df = data[sym].get(tf)
                if df is not None:
                    span = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).days
                    parts.append(f"{tf}:{len(df):,}c/{span}d")
                    ok += 1
                else:
                    parts.append(f"{tf}:MISS")
                    miss += 1
            print(f"  {sym:<8} {' | '.join(parts)}")
        print(f"\n  Hasil: {ok} dataset OK, {miss} MISS")
        if miss > 0:
            print(f"  Coin dengan MISS mungkin tidak listed di Binance (cek nama)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",   type=int,  default=180)
    parser.add_argument("--force",  action="store_true")
    parser.add_argument("--coins",  nargs="+", default=None)
    parser.add_argument("--tf",     nargs="+", default=["15m","1h","4h"])
    args = parser.parse_args()

    coins = args.coins if args.coins else SCAN_POOL
    print(f"Fetching {len(coins)} coins, TFs: {args.tf}, {args.days} hari")

    fetcher = DataFetcher()
    data    = fetcher.fetch_all(
        coins=coins, days=args.days, tfs=args.tf, force_refresh=args.force
    )
    total_ds = sum(len(v) for v in data.values())
    print(f"\nDone. {total_ds} dataset tersimpan di cache.")
