"""
Trading Engine v10 — Binance Primary
=====================================
Data source: Binance Futures (data sama persis dengan chart Binance)

Endpoints:
  GET /fapi/v1/klines → semua timeframe

Fallback: Bybit → CryptoCompare (jika Binance gagal)
"""

import requests
import pandas as pd
import numpy as np
import time
import threading
import logging
from datetime import datetime
from config import TRADING_PAIRS, SCAN_POOL, DAILY_SIGNAL, CRYPTOCOMPARE_API_KEY
try:
    from signal_generator import (
        generate_limit_signal as _gen_limit_signal,
        generate_entry_signal as _gen_entry_signal,
        set_active_sp as _sg_set_active_sp,
    )
    _LIMIT_SIGNAL_AVAILABLE = True
except ImportError:
    _LIMIT_SIGNAL_AVAILABLE = False
    _gen_entry_signal = None
    _sg_set_active_sp = None
# ── Optional modules — semua didefinisikan di sini, tidak ada NameError ──
_news_filter            = None
_MOMENTUM_AVAILABLE     = False
_EXTRA_SIGNALS          = False
_CANDLES_AVAILABLE      = False
_CHART_PATTERNS_AVAILABLE = False

try:
    from news_filter import get_news_filter
    _news_filter = get_news_filter()
except Exception:
    pass

try:
    from momentum_detector import detect_momentum
    _MOMENTUM_AVAILABLE = True
except ImportError:
    pass

try:
    from candle_patterns import get_candle_signal, format_candle_signal
    _CANDLES_AVAILABLE = True
except ImportError:
    pass

try:
    from chart_pattern_signals import detect_chart_pattern_signal
    _CHART_PATTERNS_AVAILABLE = True
except ImportError:
    pass

try:
    from clean_signal      import generate_clean_signal
    from chart_patterns    import detect_patterns, generate_pattern_signal
    from reversal_detector import detect_reversal
    _EXTRA_SIGNALS = True
except ImportError as _ie:
    _EXTRA_SIGNALS = False
    import logging as _lg
    _lg.getLogger(__name__).warning(f"Extra signal modules not loaded: {_ie}")

logger = logging.getLogger(__name__)

# ==================================================================
# TIMEFRAME CONFIG
# ==================================================================
# Binance interval mapping
BINANCE_INTERVAL = {
    '15m': '15m',
    '30m': '30m',
    '1h' : '1h',
    '4h' : '4h',
    '1d' : '1d',
    '1w' : '1w',
}
BINANCE_LIMIT = {
    '15m': 200,
    '30m': 200,
    '1h' : 300,
    '4h' : 300,
    '1d' : 200,
    '1w' : 100,
}
# Binance HTF mapping
BINANCE_HTF = {
    '15m': '1h',
    '30m': '4h',
    '1h' : '4h',
    '4h' : '1d',
    '1d' : '1w',
}
BINANCE_LTF = {
    '15m': '5m',
    '30m': '15m',
    '1h' : '15m',
    '4h' : '1h',
    '1d' : '4h',
}

# CryptoCompare: endpoint + aggregate (fallback)
# main = timeframe utama, higher = timeframe lebih besar untuk S&R
TF_CONFIG = {
    '15m': {
        'endpoint': 'histominute', 'aggregate': 15, 'limit': 100,
        'h_endpoint': 'histohour',  'h_aggregate': 1,  'h_limit': 200,
        'l_endpoint': 'histominute', 'l_aggregate': 5, 'l_limit': 60,
        'label': '15M', 'higher_label': '1H', 'lower_label': '5M',
    },
    '30m': {
        'endpoint': 'histominute', 'aggregate': 30, 'limit': 100,
        'h_endpoint': 'histohour',  'h_aggregate': 4,  'h_limit': 200,
        'l_endpoint': 'histominute', 'l_aggregate': 15, 'l_limit': 60,
        'label': '30M', 'higher_label': '4H', 'lower_label': '15M',
    },
    '1h': {
        'endpoint': 'histohour', 'aggregate': 1, 'limit': 150,
        'h_endpoint': 'histohour', 'h_aggregate': 4, 'h_limit': 200,
        'l_endpoint': 'histominute', 'l_aggregate': 15, 'l_limit': 60,
        'label': '1H', 'higher_label': '4H', 'lower_label': '15M',
    },
    '4h': {
        'endpoint': 'histohour', 'aggregate': 4, 'limit': 150,
        'h_endpoint': 'histoday',  'h_aggregate': 1, 'h_limit': 200,
        'l_endpoint': 'histohour', 'l_aggregate': 1, 'l_limit': 60,
        'label': '4H', 'higher_label': '1D', 'lower_label': '1H',
    },
    '1d': {
        'endpoint': 'histoday', 'aggregate': 1, 'limit': 200,
        'h_endpoint': 'histoday', 'h_aggregate': 7, 'h_limit': 200,
        'l_endpoint': 'histohour', 'l_aggregate': 4, 'l_limit': 60,
        'label': '1D', 'higher_label': '1W', 'lower_label': '4H',
    },
}

VALID_TFS = list(TF_CONFIG.keys())

TF_ALIASES = {
    '15': '15m', '15min': '15m',
    '30': '30m', '30min': '30m',
    '1': '1h', '1hr': '1h', '60m': '1h', '60': '1h',
    '4': '4h', '4hr': '4h', '240m': '4h', '240': '4h',
    'd': '1d', 'daily': '1d', '1day': '1d', 'day': '1d',
}


def resolve_tf(raw):
    if not raw:
        return None
    raw = raw.lower().strip()
    if raw in TF_CONFIG:
        return raw
    return TF_ALIASES.get(raw, None)


def clean_symbol(symbol):
    """Bersihkan input user → symbol (uppercase, tanpa USDT)."""
    return symbol.upper().strip().replace('/USDT', '').replace('USDT', '').replace('/USD', '')


class TradingEngine:
    def __init__(self):
        self.cc_base = "https://min-api.cryptocompare.com/data/v2"
        self.cc_base_v1 = "https://min-api.cryptocompare.com/data"
        self.ohlcv_cache = {}
        self.price_cache = {}
        self.sr_cache = {}             # S&R cache terpisah, TTL lebih lama
        self.CACHE_TTL = 60            # 1 menit cache klines/price
        self.SR_CACHE_TTL = 3600       # 1 jam cache S&R — zone harus stabil
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'Mozilla/5.0',
            'authorization': f'Apikey {CRYPTOCOMPARE_API_KEY}',
        })
        self.exchange = "Binance"
        # Signal direction cache — prevent flip-flop
        self._signal_cache = {}  # {symbol: {dir, ts, score, quality}}
        self._SIGNAL_LOCK_HOURS = 4  # jangan ganti arah dalam 4 jam
        # Zone persistence — file-based, survives restart
        self._zone_file = 'data/zone_locks.json'
        self._zone_cache = self._load_zone_locks()
        self._ZONE_PERSIST_HOURS = 12  # zone bertahan 12 jam
        # Rate limiter: dengan API key → 50 calls/menit
        self._call_times = []
        self._rate_lock = threading.Lock()
        self._MAX_CALLS_PER_MIN = 50

        # Whale sentiment cache — TTL 30 menit per coin
        # Biar tidak perlu hit API setiap scan (lambat + quota habis)
        self._whale_cache = {}   # { 'ETH': {'score': int, 'bias': str, 'ts': float} }
        self._WHALE_TTL   = 1800  # 30 menit

        # Learning engine — muat threshold hasil belajar dan merge ke SIGNAL_PARAMS
        self._apply_learned_params()

        # Test koneksi
        if self._test_url("https://fapi.binance.com/fapi/v1/ping"):
            print("✅ Trading engine siap (Binance Futures)")
        elif self._test_url(f"{self.cc_base_v1}/price?fsym=BTC&tsyms=USD"):
            print("✅ Trading engine siap (CryptoCompare fallback)")
        else:
            print("⚠️ Semua data source timeout — cek koneksi")

    # ==================================================================
    # LEARNING ENGINE — muat threshold hasil belajar
    # ==================================================================
    def _apply_learned_params(self):
        """
        Merge parameter hasil learning ke SIGNAL_PARAMS global.
        Dipanggil saat startup dan bisa dipanggil ulang untuk reload.
        """
        try:
            from learning_engine import LearningEngine
            from config import SIGNAL_PARAMS
            learned = LearningEngine.load_learned_params()
            if not learned:
                return

            # Parameter yang boleh di-override oleh learning engine
            ALLOWED_KEYS = {
                'score_good', 'score_moderate', 'rsi_near_overbought',
                'max_kills_good', 'score_hard_reject',
            }
            applied = []
            for k, v in learned.items():
                if k.startswith('_'):
                    continue
                if k in ALLOWED_KEYS and isinstance(v, (int, float)):
                    old = SIGNAL_PARAMS.get(k)
                    SIGNAL_PARAMS[k] = v
                    applied.append(f"{k}: {old}→{v}")

            if applied:
                print(f"🧠 Learned params applied: {', '.join(applied)}")
        except Exception as e:
            logger.debug(f"_apply_learned_params: {e}")

    def reload_learned_params(self):
        """Reload parameter belajar — bisa dipanggil dari Telegram /learn."""
        self._apply_learned_params()

    # ==================================================================
    # ZONE LOCK PERSISTENCE (file-based)
    # ==================================================================
    def _load_zone_locks(self):
        try:
            import json, os
            os.makedirs('data', exist_ok=True)
            if os.path.exists(self._zone_file):
                with open(self._zone_file, 'r') as f:
                    data = json.load(f)
                # Kalau zone disimpan dari sumber berbeda → clear
                # Ini mencegah CC zone vs Binance price mismatch
                saved_source = data.get('__source__', 'CryptoCompare')
                if saved_source != self.exchange:
                    logger.info(f"Zone cache dari {saved_source}, sekarang {self.exchange} — reset zones")
                    return {}
                # Hapus metadata key sebelum return
                data.pop('__source__', None)
                return data
        except Exception:
            pass
        return {}

    def _save_zone_locks(self):
        try:
            import json, os
            os.makedirs('data', exist_ok=True)
            # Cleanup expired zones
            now = time.time()
            clean = {}
            for k, v in self._zone_cache.items():
                age_h = (now - v.get('ts', 0)) / 3600
                if age_h < self._ZONE_PERSIST_HOURS * 2:  # keep 2x period
                    clean[k] = v
            clean['__source__'] = self.exchange  # tandai sumber data
            with open(self._zone_file, 'w') as f:
                json.dump(clean, f, indent=2)
        except Exception:
            pass

    def _is_zone_broken(self, zone, price, df, direction):
        """
        Zone BROKEN = candle MTF CLOSE di balik zone.
        Wick saja TIDAK cukup — harus close.
        """
        if zone is None or df is None or len(df) < 3:
            return False
        last_close = df['close'].iloc[-1]
        prev_close = df['close'].iloc[-2]
        if direction == 'support':
            # Support broken = 2 candle terakhir CLOSE di bawah zone low
            return last_close < zone['low'] and prev_close < zone['low']
        else:  # resistance
            # Resistance broken = 2 candle terakhir CLOSE di atas zone high
            return last_close > zone['high'] and prev_close > zone['high']

    def get_locked_zones(self, symbol, tf):
        """Get current locked zones for display. Returns dict with ks, kr, all_sup, all_res."""
        zone_key = f"{symbol}_{tf}"
        data = self._zone_cache.get(zone_key, {})
        return {
            'ks': data.get('ks'),
            'kr': data.get('kr'),
            'all_sup': data.get('all_sup', []),
            'all_res': data.get('all_res', []),
            'locked_since': data.get('ts', 0),
        }

    # ==================================================================
    # HTTP + CACHE
    # ==================================================================
    def _test_url(self, url):
        try:
            resp = self._session.get(url, timeout=8)
            return resp.status_code == 200
        except:
            return False

    def _http_get(self, url, params=None, timeout=12):
        # Rate limit: tunggu jika sudah terlalu banyak call
        self._wait_rate_limit()
        try:
            resp = self._session.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                logger.warning("Rate limited, tunggu 10 detik...")
                time.sleep(10)
                self._wait_rate_limit()
                resp = self._session.get(url, params=params, timeout=timeout)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.debug(f"HTTP GET failed: {e}")
        return None

    def _wait_rate_limit(self):
        """Pastikan tidak melebihi MAX_CALLS_PER_MIN."""
        with self._rate_lock:
            now = time.time()
            # Buang call yang lebih dari 60 detik lalu
            self._call_times = [t for t in self._call_times if now - t < 60]
            if len(self._call_times) >= self._MAX_CALLS_PER_MIN:
                # Tunggu sampai call terlama expire
                wait = 60 - (now - self._call_times[0]) + 0.5
                if wait > 0:
                    logger.info(f"⏳ Rate limit, tunggu {wait:.1f}s...")
                    time.sleep(wait)
                    self._call_times = [t for t in self._call_times if time.time() - t < 60]
            self._call_times.append(time.time())

    def _cache_get(self, d, key, ttl=None):
        if ttl is None:
            ttl = self.CACHE_TTL
        if key in d:
            data, ts = d[key]
            if time.time() - ts < ttl:
                return data
        return None

    def _cache_set(self, d, key, data):
        d[key] = (data, time.time())

    def test_connections(self):
        """Untuk /test command. Return list of dicts: [{name, ok, ms, price}]"""
        import time
        results = []

        tests = [
            ('CryptoCompare', f"{self.cc_base_v1}/price?fsym=BTC&tsyms=USD"),
            ('Bybit',         "https://api.bybit.com/v5/market/time"),
            ('Binance',       "https://api.binance.com/api/v3/ping"),
        ]

        for name, url in tests:
            t0 = time.time()
            try:
                import requests as _req
                r = _req.get(url, timeout=5)
                ms = int((time.time() - t0) * 1000)
                ok = r.status_code == 200
                price = None
                if ok and 'cryptocompare' in url:
                    try:
                        price = r.json().get('USD', None)
                    except Exception:
                        pass
                results.append({'name': name, 'ok': ok, 'ms': ms, 'price': price})
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                results.append({'name': name, 'ok': False, 'ms': ms, 'error': str(e)[:60]})

        results.append({'name': f'Active exchange: {self.exchange}', 'ok': True, 'ms': 0})
        return results

    # ==================================================================
    # KLINES — CryptoCompare
    # ==================================================================
    def get_klines(self, symbol, tf_key, is_higher=False, is_lower=False):
        """
        Ambil OHLCV — Binance Futures primary, CryptoCompare fallback.
        Data sama persis dengan chart Binance/TradingView.
        """
        # Tentukan interval yang dipakai
        if is_higher:
            interval = BINANCE_HTF.get(tf_key, '4h')
        elif is_lower:
            interval = BINANCE_LTF.get(tf_key, '15m')
        else:
            interval = BINANCE_INTERVAL.get(tf_key, '1h')

        limit    = BINANCE_LIMIT.get(interval, 200)
        cache_key = f"bn_{symbol}_{interval}_{limit}"
        cached = self._cache_get(self.ohlcv_cache, cache_key)
        if cached is not None:
            return cached

        # ── PRIMARY: Binance Futures ─────────────────────────────
        df = self._get_klines_binance(symbol, interval, limit)

        # ── FALLBACK: CryptoCompare ──────────────────────────────
        if df is None:
            logger.debug(f"Binance fallback CC: {symbol} {interval}")
            df = self._get_klines_cc(symbol, tf_key, is_higher, is_lower)

        if df is not None:
            self._cache_set(self.ohlcv_cache, cache_key, df)
        return df

    def _get_klines_binance(self, symbol: str, interval: str, limit: int = 200):
        """Fetch OHLCV dari Binance Futures API."""
        try:
            pair = symbol.upper() + 'USDT'
            url  = "https://fapi.binance.com/fapi/v1/klines"
            data = self._http_get(url, params={
                'symbol'  : pair,
                'interval': interval,
                'limit'   : limit + 1,  # +1 karena kita buang candle terakhir
            })
            if not data or not isinstance(data, list) or len(data) < 5:
                return None

            rows = []
            for k in data:
                rows.append({
                    'timestamp': pd.to_datetime(int(k[0]), unit='ms'),
                    'open'     : float(k[1]),
                    'high'     : float(k[2]),
                    'low'      : float(k[3]),
                    'close'    : float(k[4]),
                    'volume'   : float(k[5]),
                })

            df = pd.DataFrame(rows)
            df = df[df['close'] > 0].reset_index(drop=True)

            # Buang candle terakhir — belum close
            if len(df) > 10:
                df = df.iloc[:-1].reset_index(drop=True)

            return df if len(df) >= 5 else None

        except Exception as e:
            logger.debug(f"Binance klines {symbol} {interval}: {e}")
            return None

    def _get_klines_cc(self, symbol: str, tf_key: str,
                        is_higher: bool = False, is_lower: bool = False):
        """Fallback: fetch OHLCV dari CryptoCompare."""
        try:
            cfg = TF_CONFIG.get(tf_key, TF_CONFIG['1h'])
            if is_higher:
                endpoint  = cfg['h_endpoint']
                aggregate = cfg['h_aggregate']
                limit     = cfg['h_limit']
            elif is_lower:
                endpoint  = cfg.get('l_endpoint', cfg['endpoint'])
                aggregate = cfg.get('l_aggregate', cfg['aggregate'])
                limit     = cfg.get('l_limit', 60)
            else:
                endpoint  = cfg['endpoint']
                aggregate = cfg['aggregate']
                limit     = cfg['limit']

            url  = f"{self.cc_base}/{endpoint}"
            data = self._http_get(url, params={
                'fsym'     : symbol,
                'tsym'     : 'USD',
                'limit'    : limit,
                'aggregate': aggregate,
            })
            if not data:
                return None

            if data.get('Response') == 'Error':
                return None

            ohlcv_data = data.get('Data', {}).get('Data', [])
            if not ohlcv_data or len(ohlcv_data) < 5:
                return None

            df = pd.DataFrame(ohlcv_data)
            df = df[['time', 'open', 'high', 'low', 'close', 'volumefrom']].copy()
            df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)
            df = df[df['close'] > 0].reset_index(drop=True)
            if len(df) > 10:
                df = df.iloc[:-1].reset_index(drop=True)
            return df if len(df) >= 5 else None

        except Exception as e:
            logger.debug(f"CC klines fallback {symbol}: {e}")
            return None

    # ==================================================================
    # PRICE — CryptoCompare pricemultifull
    # ==================================================================
    def get_price(self, symbol):
        """Ambil harga real-time + 24h change — Binance primary."""
        cached = self._cache_get(self.price_cache, symbol)
        if cached is not None:
            return cached

        # Binance 24hr ticker
        try:
            pair = symbol.upper() + 'USDT'
            data = self._http_get(
                "https://fapi.binance.com/fapi/v1/ticker/24hr",
                params={'symbol': pair})
            if data and isinstance(data, dict) and float(data.get('lastPrice', 0)) > 0:
                result = {
                    'price'     : float(data.get('lastPrice', 0)),
                    'change_24h': float(data.get('priceChangePercent', 0)),
                    'high_24h'  : float(data.get('highPrice', 0)),
                    'low_24h'   : float(data.get('lowPrice', 0)),
                    'volume_24h': float(data.get('quoteVolume', 0)),
                    'market_cap': 0,
                }
                if result['price'] > 0:
                    self._cache_set(self.price_cache, symbol, result)
                    return result
        except Exception:
            pass

        # Fallback: CryptoCompare
        data = self._http_get(
            f"{self.cc_base_v1}/pricemultifull",
            params={'fsyms': symbol, 'tsyms': 'USD'})

        if not data:
            return None

        raw = data.get('RAW', {}).get(symbol, {}).get('USD', {})
        if not raw:
            return None

        result = {
            'price'     : float(raw.get('PRICE', 0)),
            'change_24h': float(raw.get('CHANGEPCT24HOUR', 0)),
            'high_24h'  : float(raw.get('HIGH24HOUR', 0)),
            'low_24h'   : float(raw.get('LOW24HOUR', 0)),
            'volume_24h': float(raw.get('TOTALVOLUME24HTO', 0)),
            'market_cap': float(raw.get('MKTCAP', 0)),
        }

        if result['price'] <= 0:
            return None

        self._cache_set(self.price_cache, symbol, result)
        return result

    # ==================================================================
    # INDIKATOR
    # ==================================================================
    def calc_ema(self, series, period):
        period = max(2, min(period, len(series) - 1))
        return series.ewm(span=period, adjust=False).mean()

    def calc_rsi(self, df, period=14):
        period = max(2, min(period, len(df) - 1))
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def calc_atr(self, df, period=14):
        period = max(2, min(period, len(df) - 1))
        hl = df['high'] - df['low']
        hc = np.abs(df['high'] - df['close'].shift())
        lc = np.abs(df['low'] - df['close'].shift())
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def calc_adx(self, df, period=14):
        """
        ADX (Average Directional Index) — ukur KEKUATAN trend, bukan arah.
        ADX > 25 = trending → signal valid
        ADX 20-25 = transition
        ADX < 20 = ranging/choppy → HINDARI signal
        """
        if df is None or len(df) < period * 3:
            return 20.0
        h, l, c = df['high'].values, df['low'].values, df['close'].values
        n = len(h)
        tr = np.zeros(n); dmp = np.zeros(n); dmn = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
            up, dn = h[i]-h[i-1], l[i-1]-l[i]
            dmp[i] = up if (up > dn and up > 0) else 0
            dmn[i] = dn if (dn > up and dn > 0) else 0
        atr_s = np.zeros(n); dip = np.zeros(n); din = np.zeros(n)
        atr_s[period] = np.mean(tr[1:period+1])
        dip[period] = np.mean(dmp[1:period+1])
        din[period] = np.mean(dmn[1:period+1])
        for i in range(period+1, n):
            atr_s[i] = (atr_s[i-1]*(period-1)+tr[i])/period
            dip[i] = (dip[i-1]*(period-1)+dmp[i])/period
            din[i] = (din[i-1]*(period-1)+dmn[i])/period
        pdi = np.zeros(n); mdi = np.zeros(n); dx = np.zeros(n)
        for i in range(period, n):
            if atr_s[i] > 0:
                pdi[i] = dip[i]/atr_s[i]*100
                mdi[i] = din[i]/atr_s[i]*100
            s = pdi[i]+mdi[i]
            if s > 0: dx[i] = abs(pdi[i]-mdi[i])/s*100
        adx = np.zeros(n)
        start = period*2
        if start < n:
            adx[start] = np.mean(dx[period:start+1])
            for i in range(start+1, n):
                adx[i] = (adx[i-1]*(period-1)+dx[i])/period
        return round(float(adx[-1]), 1) if n > start else 20.0


    # ==================================================================
    # S&R DETECTION — Smart Money / Institutional Method
    #
    # 1. Swing Structure    : Clean reversal points dengan momentum
    # 2. Order Blocks       : Candle terakhir sebelum impulsive move
    # 3. Flip Zones         : Support jadi Resistance & sebaliknya
    # 4. Liquidity Sweep    : Price sweep past level lalu reverse
    # 5. Rejection Count    : Berapa kali price BOUNCING dari level
    # ==================================================================

    def _calc_atr_for_sr(self, df, period=14):
        if df is None or len(df) < period + 1:
            return None
        hl = df['high'] - df['low']
        hc = np.abs(df['high'] - df['close'].shift())
        lc = np.abs(df['low'] - df['close'].shift())
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        val = tr.rolling(period).mean().iloc[-1]
        return val if not pd.isna(val) and val > 0 else None

    def _round_price(self, price, ref_price):
        if ref_price >= 10000:  return round(price / 10) * 10
        elif ref_price >= 1000: return round(price, 0)
        elif ref_price >= 100:  return round(price, 1)
        elif ref_price >= 10:   return round(price, 2)
        elif ref_price >= 1:    return round(price, 3)
        elif ref_price >= 0.01: return round(price, 5)
        else:                   return round(price, 8)

    # ------------------------------------------------------------------
    # 1. SWING STRUCTURE — Clean reversal with momentum
    # ------------------------------------------------------------------
    def _find_swings(self, df, window=5):
        """
        Swing VALID: fractal + momentum + volume + recency.
        v2: Minimum 1.5x avg_range momentum, volume filter, recency decay.
        """
        if df is None or len(df) < window * 2 + 1:
            return [], []
        h, l, o, c, v = (df['high'].values, df['low'].values,
                          df['open'].values, df['close'].values,
                          df['volume'].values)
        n = len(df)
        avg_range = np.mean(h - l)
        avg_vol = np.mean(v) if np.mean(v) > 0 else 1
        if avg_range <= 0:
            avg_range = 1

        swing_hi, swing_lo = [], []

        for i in range(window, n - window):
            # === SWING HIGH ===
            if h[i] >= max(h[i-window:i]) and h[i] >= max(h[i+1:i+window+1]):
                # Momentum: setelah high, harga harus turun SIGNIFIKAN
                # 2x avg_range minimum — filter micro-swing
                move_after = h[i] - min(l[i+1:min(i+window+1, n)])
                if move_after < avg_range * 2.0:
                    continue  # swing terlalu kecil, bukan real S&R

                # Body & wick analysis
                wick_top = h[i] - max(o[i], c[i])
                body = abs(c[i] - o[i])
                rej_score = min(wick_top / max(body, avg_range * 0.1), 3.0)

                # Volume weight
                vol_ratio = v[i] / avg_vol if avg_vol > 0 else 1
                vol_score = min(vol_ratio, 3.0)  # cap at 3x

                # Recency: newer swings are more relevant
                recency = i / n  # 0 (oldest) to 1 (newest)

                swing_hi.append({
                    'price': h[i], 'idx': i, 'vol': float(v[i]),
                    'body_low': min(o[i], c[i]), 'body_high': max(o[i], c[i]),
                    'move_after': move_after, 'rejection': rej_score,
                    'vol_score': vol_score, 'recency': recency,
                })

            # === SWING LOW ===
            if l[i] <= min(l[i-window:i]) and l[i] <= min(l[i+1:i+window+1]):
                move_after = max(h[i+1:min(i+window+1, n)]) - l[i]
                if move_after < avg_range * 2.0:
                    continue  # swing terlalu kecil

                wick_bot = min(o[i], c[i]) - l[i]
                body = abs(c[i] - o[i])
                rej_score = min(wick_bot / max(body, avg_range * 0.1), 3.0)

                vol_ratio = v[i] / avg_vol if avg_vol > 0 else 1
                vol_score = min(vol_ratio, 3.0)
                recency = i / n

                swing_lo.append({
                    'price': l[i], 'idx': i, 'vol': float(v[i]),
                    'body_low': min(o[i], c[i]), 'body_high': max(o[i], c[i]),
                    'move_after': move_after, 'rejection': rej_score,
                    'vol_score': vol_score, 'recency': recency,
                })

        return swing_lo, swing_hi

    # ------------------------------------------------------------------
    # 2. ORDER BLOCKS — Institutional entry zones
    # ------------------------------------------------------------------
    def _find_order_blocks(self, df, atr):
        """
        Order Block v2:
        - Impulse harus min 2.5x ATR (lebih ketat)
        - OB body = precise zone (bukan full candle)
        - Mitigation check: skip OB yang sudah ditest ulang
        - Volume validation: impulse candle harus vol > avg
        """
        if df is None or len(df) < 10 or atr is None or atr <= 0:
            return [], []
        h, l, o, c, v = (df['high'].values, df['low'].values,
                          df['open'].values, df['close'].values,
                          df['volume'].values)
        n = len(df)
        avg_vol = np.mean(v) if np.mean(v) > 0 else 1
        bull_ob, bear_ob = [], []
        impulse_min = atr * 2.5  # stricter: was 2.0

        for i in range(2, n - 3):
            high_after = max(h[i+1:min(i+4, n)])
            low_after = min(l[i+1:min(i+4, n)])
            move_up = high_after - l[i]
            move_down = h[i] - low_after

            # === BULLISH OB ===
            if c[i] < o[i] and move_up >= impulse_min:
                # Candle setelahnya harus bullish + volume tinggi
                if i + 1 < n and c[i+1] > o[i+1]:
                    impulse_vol = max(v[i+1:min(i+4, n)]) if i+1 < n else 0
                    if impulse_vol < avg_vol * 0.8:
                        continue  # volume terlalu rendah = bukan institutional

                    ob_low = min(o[i], c[i])  # body low (precise)
                    ob_high = o[i]  # open = top of bearish candle body

                    # Mitigation check: apakah harga sudah kembali ke OB?
                    mitigated = False
                    for j in range(i + 4, n):
                        if l[j] <= ob_high and h[j] >= ob_low:
                            mitigated = True
                            break

                    if not mitigated:
                        bull_ob.append({
                            'low': ob_low, 'high': ob_high,
                            'mid': (ob_low + ob_high) / 2,
                            'idx': i, 'impulse': move_up,
                            'vol': float(v[i]), 'fresh': True
                        })
                    else:
                        bull_ob.append({
                            'low': ob_low, 'high': ob_high,
                            'mid': (ob_low + ob_high) / 2,
                            'idx': i, 'impulse': move_up,
                            'vol': float(v[i]), 'fresh': False
                        })

            # === BEARISH OB ===
            if c[i] > o[i] and move_down >= impulse_min:
                if i + 1 < n and c[i+1] < o[i+1]:
                    impulse_vol = max(v[i+1:min(i+4, n)]) if i+1 < n else 0
                    if impulse_vol < avg_vol * 0.8:
                        continue

                    ob_low = o[i]  # open = bottom of bullish candle body
                    ob_high = max(o[i], c[i])

                    mitigated = False
                    for j in range(i + 4, n):
                        if h[j] >= ob_low and l[j] <= ob_high:
                            mitigated = True
                            break

                    if not mitigated:
                        bear_ob.append({
                            'low': ob_low, 'high': ob_high,
                            'mid': (ob_low + ob_high) / 2,
                            'idx': i, 'impulse': move_down,
                            'vol': float(v[i]), 'fresh': True
                        })
                    else:
                        bear_ob.append({
                            'low': ob_low, 'high': ob_high,
                            'mid': (ob_low + ob_high) / 2,
                            'idx': i, 'impulse': move_down,
                            'vol': float(v[i]), 'fresh': False
                        })

        return bull_ob, bear_ob

    # ------------------------------------------------------------------
    # 3. FLIP ZONES — Support ↔ Resistance
    # ------------------------------------------------------------------
    def _find_flip_zones(self, df, atr, swing_lo, swing_hi):
        """
        v2: Tighter tolerance (0.3x ATR), recency filter, 
        only recent flips (within last 70% of data).
        """
        if not swing_lo or not swing_hi or atr <= 0:
            return []

        tol = atr * 0.3  # was 0.5 — stricter match
        n = len(df) if df is not None else 100
        recent_cutoff = int(n * 0.3)  # only swings in last 70%

        flips = []
        for sl in swing_lo:
            if sl['idx'] < recent_cutoff:
                continue  # too old
            for sh in swing_hi:
                if sh['idx'] < recent_cutoff:
                    continue
                if abs(sl['price'] - sh['price']) <= tol:
                    mid = (sl['price'] + sh['price']) / 2
                    if sl['idx'] > sh['idx']:
                        role = 'support'
                    else:
                        role = 'resistance'

                    flips.append({
                        'price': mid,
                        'low': min(sl['price'], sh['price']),
                        'high': max(sl['price'], sh['price']),
                        'role': role,
                        'last_idx': max(sl['idx'], sh['idx']),
                        'strength': 15
                    })

        return flips

    # ------------------------------------------------------------------
    # 4. LIQUIDITY SWEEP — Stop hunt reversal
    # ------------------------------------------------------------------
    def _find_liquidity_sweeps(self, df, atr, swing_lo, swing_hi):
        """
        v2: Only recent sweeps (last 50% of data).
        Sweep strength based on speed of reversal.
        """
        if df is None or len(df) < 5 or atr <= 0:
            return []
        h, l, c = df['high'].values, df['low'].values, df['close'].values
        n = len(df)
        sweeps = []
        tol = atr * 0.15  # tighter threshold

        # Only check recent swings
        recent_cutoff = int(n * 0.5)

        for sl in swing_lo:
            if sl['idx'] < recent_cutoff:
                continue
            level = sl['price']
            for i in range(sl['idx'] + 2, n):
                if l[i] < level - tol:
                    if c[i] > level:
                        # Measure reversal strength: how much above level did it close?
                        reversal_strength = (c[i] - level) / atr
                        base_str = 12 + min(reversal_strength * 2, 6)
                        sweeps.append({
                            'price': level, 'sweep_idx': i,
                            'type': 'support', 'strength': base_str
                        })
                        break

        for sh in swing_hi:
            if sh['idx'] < recent_cutoff:
                continue
            level = sh['price']
            for i in range(sh['idx'] + 2, n):
                if h[i] > level + tol:
                    if c[i] < level:
                        reversal_strength = (level - c[i]) / atr
                        base_str = 12 + min(reversal_strength * 2, 6)
                        sweeps.append({
                            'price': level, 'sweep_idx': i,
                            'type': 'resistance', 'strength': base_str
                        })
                        break

        return sweeps

    # ------------------------------------------------------------------
    # 5. REJECTION COUNT — Bounce validation
    # ------------------------------------------------------------------
    def _count_rejections(self, df, level, atr):
        """
        v2: Recency-weighted rejections.
        Recent rejections (last 30%) count full.
        Old rejections (first 30%) count half.
        Also: stricter rejection = body must be > 0.7x ATR from level.
        """
        if df is None or len(df) < 5 or atr <= 0:
            return 0, 0
        tol = atr * 0.3
        h, l, o, c = (df['high'].values, df['low'].values,
                       df['open'].values, df['close'].values)
        n = len(df)
        touches = 0
        rejections = 0.0  # float for weighting

        recent_start = int(n * 0.7)  # last 30% = recent

        for i in range(n):
            if l[i] <= level + tol and h[i] >= level - tol:
                # Recency weight
                weight = 1.0 if i >= recent_start else 0.5

                touches += 1
                body_mid = (o[i] + c[i]) / 2
                # Stricter: body > 0.7x ATR from level (was 0.5x)
                if abs(body_mid - level) > atr * 0.7:
                    rejections += weight

        return touches, int(round(rejections))

    # ------------------------------------------------------------------
    # 6. FRESHNESS CHECK — Fresh vs Mitigated
    # ------------------------------------------------------------------
    def _check_freshness(self, df, level, atr, creation_idx):
        """
        Fresh = level belum pernah di-retest setelah terbentuk.
        Mitigated = sudah pernah disentuh ulang.
        Fresh levels JAUH lebih kuat.
        """
        if df is None or len(df) < 5 or atr <= 0:
            return True  # assume fresh
        tol = atr * 0.4
        h, l = df['high'].values, df['low'].values

        for i in range(creation_idx + 3, len(df)):
            if l[i] <= level + tol and h[i] >= level - tol:
                return False  # sudah mitigated

        return True  # masih fresh

    # ------------------------------------------------------------------
    # KEY LEVELS: Previous Day/Week High-Low + Round Numbers
    # Level yang SEMUA trader lihat di chart
    # ------------------------------------------------------------------
    def _find_key_horizontal_levels(self, df, price, atr):
        """
        Detect level yang paling obvious dan reliable:
        1. Previous Day High/Low (PDH/PDL)
        2. Previous Week High/Low (PWH/PWL)
        3. Recent significant swing high/low (last 20-50 candles)
        4. Round numbers / psychological levels
        
        Return: list of {price, type, method, score, idx}
        """
        levels = []
        if df is None or len(df) < 20:
            return levels

        h = df['high'].values
        l = df['low'].values
        c = df['close'].values
        n = len(df)

        # === RECENT SIGNIFICANT HIGHS & LOWS ===
        # Look at last 50 candles, find THE highest high and lowest low
        lookback = min(50, n - 1)
        recent_h = h[-lookback:]
        recent_l = l[-lookback:]

        # Highest high in last 50 candles = strong resistance
        max_idx_rel = np.argmax(recent_h)
        max_idx = n - lookback + max_idx_rel
        max_price = recent_h[max_idx_rel]
        if max_price > price:
            levels.append({
                'price': max_price, 'low': max_price - atr * 0.2,
                'high': max_price,
                'score': 15, 'type': 'resistance',
                'method': 'recent_high', 'idx': max_idx
            })

        # Lowest low in last 50 candles = strong support
        min_idx_rel = np.argmin(recent_l)
        min_idx = n - lookback + min_idx_rel
        min_price = recent_l[min_idx_rel]
        if min_price < price:
            levels.append({
                'price': min_price, 'low': min_price,
                'high': min_price + atr * 0.2,
                'score': 15, 'type': 'support',
                'method': 'recent_low', 'idx': min_idx
            })

        # === PREVIOUS SESSION HIGH/LOW ===
        # Approximate: last 24 candles for 1H, last 6 for 4H
        # Find the high/low of the "previous session" (before last 20%)
        session_end = int(n * 0.8)
        session_start = max(0, session_end - min(30, n // 3))
        if session_start < session_end:
            sess_h = h[session_start:session_end]
            sess_l = l[session_start:session_end]
            if len(sess_h) > 5:
                pdh = np.max(sess_h)
                pdl = np.min(sess_l)
                pdh_idx = session_start + np.argmax(sess_h)
                pdl_idx = session_start + np.argmin(sess_l)

                if pdh > price:
                    levels.append({
                        'price': pdh, 'low': pdh - atr * 0.15,
                        'high': pdh + atr * 0.15,
                        'score': 14, 'type': 'resistance',
                        'method': 'prev_session_high', 'idx': pdh_idx
                    })
                if pdl < price:
                    levels.append({
                        'price': pdl, 'low': pdl - atr * 0.15,
                        'high': pdl + atr * 0.15,
                        'score': 14, 'type': 'support',
                        'method': 'prev_session_low', 'idx': pdl_idx
                    })

        # === ROUND NUMBERS ===
        # For BTC: $90K, $95K, $100K, $85K etc.
        # For ETH: $3000, $3500, $4000
        # For small coins: significant decimals
        if price >= 1000:
            step = 5000 if price >= 50000 else (1000 if price >= 10000 else 500)
        elif price >= 100:
            step = 50
        elif price >= 10:
            step = 5
        elif price >= 1:
            step = 0.5
        else:
            step = price * 0.05  # 5% steps for small coins

        if step > 0:
            # Find nearest round numbers above and below
            lower_round = int(price / step) * step
            upper_round = lower_round + step

            # Only add if within 5% of price
            for rn in [lower_round, upper_round, lower_round - step, upper_round + step]:
                dist_pct = abs(rn - price) / price * 100
                if 0.5 < dist_pct < 5.0 and rn > 0:
                    zone_type = 'resistance' if rn > price else 'support'
                    levels.append({
                        'price': rn, 'low': rn - atr * 0.1,
                        'high': rn + atr * 0.1,
                        'score': 10, 'type': zone_type,
                        'method': 'round_number', 'idx': n - 1
                    })

        return levels

    # ------------------------------------------------------------------
    # STRUCTURAL S&R — Cara BENAR menentukan Support & Resistance
    #
    # SUPPORT = swing low yang MENDAHULUI new high (higher high)
    #   → Harga bikin high baru? Low sebelumnya = support valid.
    #   → Ini level dimana buyer masuk dan BERHASIL push ke high baru.
    #
    # RESISTANCE = swing high yang MENDAHULUI new low (lower low)
    #   → Harga bikin low baru? High sebelumnya = resistance valid.
    #   → Ini level dimana seller masuk dan BERHASIL push ke low baru.
    #
    # Level lama yang jauh dari harga = TIDAK RELEVAN.
    # ------------------------------------------------------------------
    def _find_structural_sr(self, df, price, atr):
        """
        Find S&R based on market structure (HH/HL/LH/LL).
        Return: {
            'support': {price, low, high, idx, type},    # pullback low sebelum HH
            'resistance': {price, low, high, idx, type},  # pullback high sebelum LL
            'structure_supports': [...],   # all valid supports (sorted by recency)
            'structure_resists': [...],    # all valid resistances
        }
        """
        result = {'support': None, 'resistance': None,
                  'structure_supports': [], 'structure_resists': []}

        if df is None or len(df) < 20:
            return result

        h = df['high'].values
        l = df['low'].values
        o = df['open'].values
        c = df['close'].values
        n = len(df)

        # Step 1: Find swing highs and swing lows (window=5 for reliability)
        window = 5
        swing_highs = []  # (idx, price)
        swing_lows = []   # (idx, price)

        for i in range(window, n - window):
            if h[i] >= max(h[max(0,i-window):i]) and h[i] >= max(h[i+1:min(i+window+1,n)]):
                swing_highs.append((i, h[i]))
            if l[i] <= min(l[max(0,i-window):i]) and l[i] <= min(l[i+1:min(i+window+1,n)]):
                swing_lows.append((i, l[i]))

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return result

        # Step 2: Walk through structure — find HH and LL events
        valid_supports = []   # lows before HH
        valid_resists = []    # highs before LL

        # Track the highest high seen so far
        highest_high = swing_highs[0][1]
        highest_high_idx = swing_highs[0][0]

        # For each new swing high, check if it's a HIGHER HIGH
        for i in range(1, len(swing_highs)):
            sh_idx, sh_price = swing_highs[i]

            if sh_price > highest_high:
                # === NEW HIGH DETECTED ===
                # Find the swing low between previous highest high and this new high
                # That swing low = VALID SUPPORT (the pullback before breakout)
                best_low = None
                for sl_idx, sl_price in swing_lows:
                    if highest_high_idx < sl_idx < sh_idx:
                        # This low is between old HH and new HH
                        if best_low is None or sl_price < best_low[1]:
                            best_low = (sl_idx, sl_price)

                if best_low:
                    # Zone = candle body range at this swing low
                    idx = best_low[0]
                    zone_low = best_low[1]
                    zone_high = max(o[idx], c[idx])  # body high of the candle
                    # Minimum zone width
                    if zone_high - zone_low < atr * 0.2:
                        zone_high = zone_low + atr * 0.3

                    valid_supports.append({
                        'price': zone_low, 'low': zone_low, 'high': zone_high,
                        'mid': (zone_low + zone_high) / 2,
                        'idx': idx, 'hh_price': sh_price,
                        'type': 'structural_support'
                    })

                highest_high = sh_price
                highest_high_idx = sh_idx

        # Same for resistance: track lowest low
        lowest_low = swing_lows[0][1]
        lowest_low_idx = swing_lows[0][0]

        for i in range(1, len(swing_lows)):
            sl_idx, sl_price = swing_lows[i]

            if sl_price < lowest_low:
                # === NEW LOW DETECTED ===
                # Find swing high between old LL and this new LL
                best_high = None
                for sh_idx, sh_price in swing_highs:
                    if lowest_low_idx < sh_idx < sl_idx:
                        if best_high is None or sh_price > best_high[1]:
                            best_high = (sh_idx, sh_price)

                if best_high:
                    idx = best_high[0]
                    zone_high = best_high[1]
                    zone_low = min(o[idx], c[idx])  # body low of the candle
                    if zone_high - zone_low < atr * 0.2:
                        zone_low = zone_high - atr * 0.3

                    valid_resists.append({
                        'price': zone_high, 'low': zone_low, 'high': zone_high,
                        'mid': (zone_low + zone_high) / 2,
                        'idx': idx, 'll_price': sl_price,
                        'type': 'structural_resistance'
                    })

                lowest_low = sl_price
                lowest_low_idx = sl_idx

        # Step 3: Pick the MOST RECENT valid support/resistance below/above price
        # Support = most recent valid support BELOW current price
        below = [s for s in valid_supports if s['mid'] < price]
        if below:
            # Most recent = highest index
            result['support'] = max(below, key=lambda x: x['idx'])

        # Resistance = most recent valid resistance ABOVE current price
        above = [r for r in valid_resists if r['mid'] > price]
        if above:
            result['resistance'] = max(above, key=lambda x: x['idx'])

        # Also store all for TP targeting
        result['structure_supports'] = sorted(below, key=lambda x: -x['idx'])[:5]
        result['structure_resists'] = sorted(above, key=lambda x: -x['idx'])[:5]

        return result

    # ------------------------------------------------------------------
    # MASTER: Combine → Cluster → Score → Rank
    # ------------------------------------------------------------------
    def _build_zones(self, df, atr, price, is_htf=False):
        """Build all raw S&R levels. Structural S&R = highest priority."""
        raw = []

        # ===== PRIORITY 1: STRUCTURAL S&R (paling akurat) =====
        # Support = low sebelum new high, Resist = high sebelum new low
        struct = self._find_structural_sr(df, price, atr)
        struct_base = 25 if is_htf else 18  # HIGHEST base score

        if struct['support']:
            s = struct['support']
            raw.append({
                'price': s['price'], 'low': s['low'], 'high': s['high'],
                'score': struct_base, 'type': 'support',
                'method': 'structural', 'idx': s['idx']
            })
        # All other structural supports (for TP targeting)
        for s in struct.get('structure_supports', [])[1:3]:
            raw.append({
                'price': s['price'], 'low': s['low'], 'high': s['high'],
                'score': struct_base - 3, 'type': 'support',
                'method': 'structural', 'idx': s['idx']
            })

        if struct['resistance']:
            r = struct['resistance']
            raw.append({
                'price': r['price'], 'low': r['low'], 'high': r['high'],
                'score': struct_base, 'type': 'resistance',
                'method': 'structural', 'idx': r['idx']
            })
        for r in struct.get('structure_resists', [])[1:3]:
            raw.append({
                'price': r['price'], 'low': r['low'], 'high': r['high'],
                'score': struct_base - 3, 'type': 'resistance',
                'method': 'structural', 'idx': r['idx']
            })

        # ===== PRIORITY 2: Swing Structure (secondary confirmation) =====
        window = 8 if is_htf else 5
        swing_lo, swing_hi = self._find_swings(df, window)
        base = 10 if is_htf else 6  # lower than structural

        for s in swing_hi:
            recency_bonus = s['recency'] * 4      # 0-4 based on position
            vol_bonus = s['vol_score']             # 0-3 based on volume ratio
            rej_bonus = s['rejection'] * 2         # 0-6 based on wick rejection
            raw.append({
                'price': s['price'], 'low': s['body_low'], 'high': s['price'],
                'score': base + recency_bonus + vol_bonus + rej_bonus,
                'type': 'resistance', 'method': 'swing', 'idx': s['idx']
            })

        for s in swing_lo:
            recency_bonus = s['recency'] * 4
            vol_bonus = s['vol_score']
            rej_bonus = s['rejection'] * 2
            raw.append({
                'price': s['price'], 'low': s['price'], 'high': s['body_high'],
                'score': base + recency_bonus + vol_bonus + rej_bonus,
                'type': 'support', 'method': 'swing', 'idx': s['idx']
            })

        # 2. Order Blocks — fresh OB score lebih tinggi
        bull_ob, bear_ob = self._find_order_blocks(df, atr)
        ob_base = 16 if is_htf else 12  # S&D: OB paling penting

        for ob in bull_ob:
            imp_bonus = min(ob['impulse'] / atr, 4)
            fresh_bonus = 4 if ob.get('fresh', False) else 0  # BARU: fresh OB +4
            raw.append({
                'price': ob['mid'], 'low': ob['low'], 'high': ob['high'],
                'score': ob_base + imp_bonus + fresh_bonus,
                'type': 'support', 'method': 'order_block', 'idx': ob['idx']
            })

        for ob in bear_ob:
            imp_bonus = min(ob['impulse'] / atr, 4)
            fresh_bonus = 4 if ob.get('fresh', False) else 0
            raw.append({
                'price': ob['mid'], 'low': ob['low'], 'high': ob['high'],
                'score': ob_base + imp_bonus + fresh_bonus,
                'type': 'resistance', 'method': 'order_block', 'idx': ob['idx']
            })

        # 3. Flip Zones
        flips = self._find_flip_zones(df, atr, swing_lo, swing_hi)
        for fz in flips:
            raw.append({
                'price': fz['price'], 'low': fz['low'], 'high': fz['high'],
                'score': fz['strength'],
                'type': fz['role'], 'method': 'flip_zone', 'idx': fz['last_idx']
            })

        # 4. Liquidity Sweeps
        sweeps = self._find_liquidity_sweeps(df, atr, swing_lo, swing_hi)
        for sw in sweeps:
            tol = atr * 0.3
            raw.append({
                'price': sw['price'], 'low': sw['price'] - tol,
                'high': sw['price'] + tol, 'score': sw['strength'],
                'type': sw['type'], 'method': 'liquidity_sweep',
                'idx': sw['sweep_idx']
            })

        # 5. Key Horizontal Levels (PDH/PDL, round numbers, recent extremes)
        key_levels = self._find_key_horizontal_levels(df, price, atr)
        for kl in key_levels:
            raw.append(kl)

        return raw

    def _cluster_and_score(self, raw_levels, atr_cluster, atr_validate, price, df, side):
        """
        v2: Quality gate + HTF confluence bonus.
        - Max width = 1.5x ATR (was 2.5% harga — terlalu lebar)
        - Minimum score threshold: zone < 10 pts = dibuang
        - HTF levels yang cluster dengan MTF = bonus besar
        """
        if not raw_levels or not atr_cluster or atr_cluster <= 0:
            return []

        # Max width berdasarkan ATR (lebih presisi dari % harga)
        max_width = atr_cluster * 1.5
        min_width = atr_cluster * 0.2

        # Filter side
        filtered = []
        for rl in raw_levels:
            if rl['type'] != side and rl['type'] != 'both':
                continue
            if side == 'support' and rl['price'] < price:
                filtered.append(rl)
            elif side == 'resistance' and rl['price'] > price:
                filtered.append(rl)
        if not filtered:
            return []

        filtered.sort(key=lambda x: x['price'])

        # Cluster distance berdasarkan ATR
        # 0.8x ATR = level harus DEKAT untuk dimerge
        # Terlalu ketat (0.6) = merge level yang seharusnya separate
        cluster_dist = atr_cluster * 0.8

        clusters, cluster = [], [filtered[0]]
        for i in range(1, len(filtered)):
            if abs(filtered[i]['price'] - cluster[-1]['price']) <= cluster_dist:
                cluster.append(filtered[i])
            else:
                clusters.append(cluster)
                cluster = [filtered[i]]
        clusters.append(cluster)

        zones = []
        for c in clusters:
            all_lows = [l.get('low', l['price']) for l in c]
            all_highs = [l.get('high', l['price']) for l in c]
            lo = min(all_lows)
            hi = max(all_highs)
            mid = sum(l['price'] for l in c) / len(c)

            if (hi - lo) < min_width:
                half = min_width / 2
                lo, hi = mid - half, mid + half
            if (hi - lo) > max_width:
                half = max_width / 2
                lo, hi = mid - half, mid + half

            # Zone tidak boleh melewati harga
            if side == 'support' and hi > price:
                hi = price * 0.999
                if lo >= hi: continue
            if side == 'resistance' and lo < price:
                lo = price * 1.001
                if hi <= lo: continue

            # === SCORING — S&D PRIORITY ===
            total_score = sum(l['score'] for l in c)

            # Method diversity — institutional methods worth more
            methods = set(l['method'] for l in c)
            method_bonus = len(methods) * 5

            # S&D PRIORITY: Order Block dan Flip Zone = REAL institutional zones
            # Swing biasa = level, tapi OB/Flip = zona dimana order institusi duduk
            has_structural = 'structural' in methods
            has_ob = 'order_block' in methods
            has_flip = 'flip_zone' in methods
            has_sweep = 'liquidity_sweep' in methods
            has_pdh = 'prev_session_high' in methods or 'prev_session_low' in methods
            has_recent = 'recent_high' in methods or 'recent_low' in methods
            has_round = 'round_number' in methods
            sd_bonus = 0
            if has_structural: sd_bonus += 12  # HIGHEST priority
            if has_ob: sd_bonus += 8
            if has_flip: sd_bonus += 6
            if has_sweep: sd_bonus += 4
            if has_pdh: sd_bonus += 6
            if has_recent: sd_bonus += 5
            if has_round: sd_bonus += 3

            # HTF + MTF confluence = big bonus
            has_htf = any(l['score'] >= 10 for l in c)
            has_mtf = any(l['score'] < 10 for l in c)
            htf_mtf_bonus = 10 if (has_htf and has_mtf) else 0  # raised from 8

            # Recency-weighted rejections
            touches, rejections = self._count_rejections(df, mid, atr_validate)
            rejection_score = rejections * 6 + touches * 1

            # Freshness — fresh zone (belum di-revisit) paling valid
            earliest_idx = min(l.get('idx', 0) for l in c)
            is_fresh = self._check_freshness(df, mid, atr_validate, earliest_idx)
            fresh_bonus = 12 if is_fresh else 0  # raised from 10

            # REMOVED: Proximity bonus
            # Proximity bonus DIHAPUS karena menyebabkan zone ranking berubah
            # saat harga bergerak sedikit — ini root cause #1 zone flip-flop.
            # Zone quality harus INDEPENDEN dari posisi harga saat ini.

            final_score = total_score + method_bonus + sd_bonus + htf_mtf_bonus + rejection_score + fresh_bonus

            # === QUALITY GATE: minimum score threshold ===
            # Zone dengan score < 12 = terlalu lemah, buang
            if final_score < 15:  # raised: hanya zone kuat yang lolos
                continue

            lo = self._round_price(lo, price)
            hi = self._round_price(hi, price)
            mid = self._round_price(mid, price)

            nm = len(methods)
            strength = "Level"  # safety default

            if has_structural and (has_ob or rejections >= 1):
                strength = f"Structural + S&D ({rejections}x reject) " + chr(0x1F7E2)
            elif has_structural:
                strength = f"Structural Level " + chr(0x2705)
            elif (has_ob or has_flip or has_sweep) and rejections >= 2:
                parts = []
                if has_ob: parts.append("OB")
                if has_flip: parts.append("Flip")
                if has_sweep: parts.append("Sweep")
                strength = f"S&D Zone ({'+'.join(parts)}, {rejections}x reject) " + chr(0x1F7E2)
            elif (has_pdh or has_recent) and (has_ob or rejections >= 1):
                strength = f"Key Level ({rejections}x reject) " + chr(0x1F7E2)
            elif nm >= 3 or rejections >= 3:
                strength = f"S&R Zone — Kuat ({nm} konfirmasi, {rejections}x reject) " + chr(0x2705)
            elif (has_ob or has_flip or has_sweep) and rejections >= 1:
                strength = f"S&D Zone (SMC {nm} konfirmasi) " + chr(0x2705)
            elif nm >= 2 or rejections >= 1:
                fresh_tag = " Fresh" if is_fresh else ""
                strength = f"S&R Zone — Sedang ({nm} konfirmasi{fresh_tag}) " + chr(0x1F7E1)
            else:
                strength = f"Swing level " + chr(0x26AA)

            zones.append({
                'low': lo, 'high': hi, 'mid': mid,
                'touches': touches, 'bounces': rejections,
                'methods': list(methods), 'n_methods': nm,
                'is_fresh': is_fresh, 'htf_mtf': has_htf and has_mtf,
                'width_pct': round((hi - lo) / max(price, 1) * 100, 3),
                'score': round(final_score, 1),
                'strength': strength
            })

        # Sort by DISTANCE to price (nearest first), then by score as tiebreaker
        for z in zones:
            z['_dist'] = abs(price - z['mid'])
        zones.sort(key=lambda x: (x['_dist'], -x['score']))
        return zones

    # ------------------------------------------------------------------
    # MAIN: detect_key_levels
    # ------------------------------------------------------------------
    def detect_key_levels(self, df_higher, df_main, price, symbol='', tf=''):
        """
        Smart Money S&R + S&D Combined:
        1. Scan HTF + Main TF untuk swing, order blocks, flip zones, sweeps
        2. Combine → cluster → validate → S&D prioritized scoring
        3. Zone LOCK: zone TIDAK berubah kecuali di-break oleh candle close
        """
        sr_key = f"sr_{symbol}_{tf}"

        # Check S&R cache (30 min)
        cached = self._cache_get(self.sr_cache, sr_key, self.SR_CACHE_TTL)
        if cached is not None:
            return cached

        # ==========================================================
        # ZONE LOCK CHECK — EARLY RETURN kalau zones masih valid
        # Ini KUNCI agar zone tidak berubah-ubah.
        # Zone hanya diganti kalau BENAR-BENAR broken oleh candle close.
        # ==========================================================
        zone_key = f"{symbol}_{tf}"
        persisted = self._zone_cache.get(zone_key)

        if persisted:
            hours_old = (time.time() - persisted.get('ts', 0)) / 3600
            if hours_old < self._ZONE_PERSIST_HOURS:
                old_ks = persisted.get('ks')
                old_kr = persisted.get('kr')

                # Cek apakah zone di-BREAK (candle close, bukan cuma wick)
                ks_broken = self._is_zone_broken(old_ks, price, df_main, 'support')
                kr_broken = self._is_zone_broken(old_kr, price, df_main, 'resistance')

                if not ks_broken and not kr_broken:
                    # KEDUA zone masih valid → LANGSUNG RETURN
                    # Update dist_pct saja (biar display akurat)
                    if old_ks:
                        old_ks['dist_pct'] = round((price - old_ks['mid']) / price * 100, 2)
                    if old_kr:
                        old_kr['dist_pct'] = round((old_kr['mid'] - price) / price * 100, 2)

                    # Cache ulang dan return
                    res_tp = persisted.get('res_tp', [])
                    sup_tp = persisted.get('sup_tp', [])
                    result = (old_ks, old_kr, res_tp, sup_tp)
                    self._cache_set(self.sr_cache, sr_key, result)
                    return result

                # Kalau salah satu broken → recalculate yang broken saja
                # Yang masih valid tetap dipakai
                logger.info(f"Zone update {symbol}: S broken={ks_broken}, R broken={kr_broken}")

        atr_h = self._calc_atr_for_sr(df_higher, 14)
        atr_m = self._calc_atr_for_sr(df_main, 14)
        if atr_h is None: atr_h = price * 0.02
        if atr_m is None: atr_m = price * 0.01

        # Build raw levels dari kedua timeframe
        raw_htf = self._build_zones(df_higher, atr_h, price, is_htf=True)
        raw_mtf = self._build_zones(df_main, atr_m, price, is_htf=False)
        all_raw = raw_htf + raw_mtf

        # Cluster pakai MTF ATR (lebih kecil = presisi)
        # Validate pakai MTF ATR juga
        sup_zones = self._cluster_and_score(all_raw, atr_m, atr_m, price, df_main, 'support')
        res_zones = self._cluster_and_score(all_raw, atr_m, atr_m, price, df_main, 'resistance')

        ks = kr = None
        min_score = 15

        # ==========================================================
        # ZONE SELECTION — NEAREST FIRST, NOT HIGHEST SCORE
        #
        # SEBELUMNYA (SALAH):
        #   Sort by score → ambil [0] → zone jauh tapi score tinggi menang
        #
        # SEKARANG (BENAR):
        #   Sort by JARAK ke harga → ambil yang TERDEKAT yang lolos quality
        #   Support  = zone TERDEKAT di BAWAH harga yang score >= min
        #   Resistance = zone TERDEKAT di ATAS harga yang score >= min
        # ==========================================================

        def _make_zone_dict(b, is_support):
            dist = abs(price - b['mid']) / price * 100
            return {
                'low': b['low'], 'high': b['high'], 'mid': b['mid'],
                'touches': b['touches'], 'bounces': b['bounces'],
                'n_methods': b['n_methods'], 'is_fresh': b['is_fresh'],
                'dist_pct': round(dist, 2),
                'width_pct': b['width_pct'], 'strength': b['strength'],
                'score': b['score'], 'htf_mtf': b.get('htf_mtf', False)
            }

        # Support: sort by TERDEKAT ke harga (ascending distance)
        # Filter: hanya yang score >= min_score AND mid < price
        valid_sup = [z for z in sup_zones if z['score'] >= min_score and z['mid'] < price]
        valid_sup.sort(key=lambda x: price - x['mid'])  # nearest first

        # Resistance: sort by TERDEKAT ke harga
        valid_res = [z for z in res_zones if z['score'] >= min_score and z['mid'] > price]
        valid_res.sort(key=lambda x: x['mid'] - price)  # nearest first

        _ks_cand = _make_zone_dict(valid_sup[0], True) if valid_sup else None
        _kr_cand = _make_zone_dict(valid_res[0], False) if valid_res else None

        # Kalau nearest zone terlalu jauh (> 5%), coba cari yang lebih dekat
        # dengan lower score threshold
        if _ks_cand and _ks_cand['dist_pct'] > 5.0:
            # Cari zone dengan score >= 10 yang lebih dekat
            closer = [z for z in sup_zones if z['score'] >= 10 and z['mid'] < price]
            closer.sort(key=lambda x: price - x['mid'])
            if closer and (price - closer[0]['mid']) / price * 100 < _ks_cand['dist_pct']:
                _ks_cand = _make_zone_dict(closer[0], True)

        if _kr_cand and _kr_cand['dist_pct'] > 5.0:
            closer = [z for z in res_zones if z['score'] >= 10 and z['mid'] > price]
            closer.sort(key=lambda x: x['mid'] - price)
            if closer and (closer[0]['mid'] - price) / price * 100 < _kr_cand['dist_pct']:
                _kr_cand = _make_zone_dict(closer[0], False)

        ks = _ks_cand
        kr = _kr_cand

        # TP targeting zones — only quality zones as TP targets
        res_tp = sorted([z for z in res_zones if z['low'] > price * 1.002 and z['score'] >= 12],
                        key=lambda x: x['mid'])
        sup_tp = sorted([z for z in sup_zones if z['high'] < price * 0.998 and z['score'] >= 12],
                        key=lambda x: -x['mid'])

        # === ZONE LOCK: Simpan zone terkuat + semua zone untuk display ===
        # Kalau sebelumnya ada zone yang masih valid (tidak broken), pertahankan
        if persisted:
            ks_broken = self._is_zone_broken(persisted.get('ks'), price, df_main, 'support')
            kr_broken = self._is_zone_broken(persisted.get('kr'), price, df_main, 'resistance')
            if not ks_broken and persisted.get('ks') and ks:
                # Support lama masih valid → pertahankan kecuali baru jauh lebih kuat
                if ks['score'] < persisted['ks']['score'] + 10:
                    ks = persisted['ks']
                    ks['dist_pct'] = round((price - ks['mid']) / price * 100, 2)
            if not kr_broken and persisted.get('kr') and kr:
                if kr['score'] < persisted['kr']['score'] + 10:
                    kr = persisted['kr']
                    kr['dist_pct'] = round((kr['mid'] - price) / price * 100, 2)

        # Simpan ALL zones (top 3 per side) untuk /zones command
        all_sup = []
        for z in sup_zones[:3]:
            all_sup.append({
                'low': z['low'], 'high': z['high'], 'mid': z['mid'],
                'score': z['score'], 'strength': z['strength'],
                'is_fresh': z['is_fresh'], 'n_methods': z['n_methods'],
            })
        all_res = []
        for z in res_zones[:3]:
            all_res.append({
                'low': z['low'], 'high': z['high'], 'mid': z['mid'],
                'score': z['score'], 'strength': z['strength'],
                'is_fresh': z['is_fresh'], 'n_methods': z['n_methods'],
            })

        # Save to file-backed zone cache
        self._zone_cache[zone_key] = {
            'ks': ks, 'kr': kr,
            'all_sup': all_sup, 'all_res': all_res,
            'res_tp': [{'low': z['low'], 'high': z['high'], 'mid': z['mid'],
                         'score': z['score'], 'touches': z.get('touches', 0)}
                        for z in res_tp[:5]],
            'sup_tp': [{'low': z['low'], 'high': z['high'], 'mid': z['mid'],
                         'score': z['score'], 'touches': z.get('touches', 0)}
                        for z in sup_tp[:5]],
            'ts': time.time()
        }
        self._save_zone_locks()

        result = (ks, kr, res_tp, sup_tp)
        self._cache_set(self.sr_cache, sr_key, result)
        return ks, kr, res_tp, sup_tp

    # MARKET STRUCTURE + EMA
    # ==================================================================
    def detect_market_structure(self, df, window=3):
        if df is None or len(df) < window * 2 + 3:
            return "SIDEWAYS"
        h, l = df['high'].values, df['low'].values
        sh, sl = [], []
        for i in range(window, len(df) - window):
            if h[i] == max(h[i - window:i + window + 1]):
                sh.append(h[i])
            if l[i] == min(l[i - window:i + window + 1]):
                sl.append(l[i])
        if len(sh) < 2 or len(sl) < 2:
            return "SIDEWAYS"
        rh, rl = sh[-3:], sl[-3:]
        hh = all(rh[i] >= rh[i - 1] for i in range(1, len(rh)))
        hl = all(rl[i] >= rl[i - 1] for i in range(1, len(rl)))
        lh = all(rh[i] <= rh[i - 1] for i in range(1, len(rh)))
        ll = all(rl[i] <= rl[i - 1] for i in range(1, len(rl)))
        if hh and hl:
            return "UPTREND"
        elif lh and ll:
            return "DOWNTREND"
        elif hl and not lh:
            return "UPTREND"
        elif lh and not hl:
            return "DOWNTREND"
        return "SIDEWAYS"

    def analyze_ema_trend(self, df):
        n   = len(df)
        p   = df['close'].iloc[-1]
        cls = df['close']

        e8  = self.calc_ema(cls, min(8,  n-1)).iloc[-1]
        e21 = self.calc_ema(cls, min(21, n-1)).iloc[-1]
        e50 = self.calc_ema(cls, min(50, n-1)).iloc[-1]
        e9  = self.calc_ema(cls, min(9,  n-1)).iloc[-1]  # backward compat

        emas = {
            'ema8' : round(e8,  8),
            'ema9' : round(e9,  8),
            'ema21': round(e21, 8),
            'ema50': round(e50, 8),
        }

        if p > e8 > e21 > e50:
            return "STRONG_UP",   emas, "EMA 8>21>50 — Naik kuat ✅"
        elif p > e21 > e50:
            return "UP",          emas, "Di atas EMA 21&50 — Naik"
        elif p > e50:
            return "WEAK_UP",     emas, "Di atas EMA 50 — Naik lemah"
        elif p < e8 < e21 < e50:
            return "STRONG_DOWN", emas, "EMA 8<21<50 — Turun kuat ❌"
        elif p < e21 < e50:
            return "DOWN",        emas, "Di bawah EMA 21&50 — Turun"
        elif p < e50:
            return "WEAK_DOWN",   emas, "Di bawah EMA 50 — Turun lemah"
        else:
            return "SIDEWAYS",    emas, "EMA silang — Sideways"

    def detect_ema_cross(self, df) -> dict:
        """
        Deteksi EMA 8/21 cross dan bias arah.

        Signal:
          - EMA8 > EMA21 → BULLISH bias (peluang LONG)
          - EMA8 < EMA21 → BEARISH bias (peluang SHORT)
          - Baru cross (1-3 candle lalu) → signal lebih kuat
          - Slope searah → momentum konfirmasi

        Return:
          {
            bias      : BULLISH / BEARISH / NEUTRAL,
            cross     : GOLDEN (buy) / DEATH (sell) / None,
            bars_ago  : berapa candle lalu cross terjadi,
            e8        : nilai EMA 8,
            e21       : nilai EMA 21,
            gap_pct   : selisih EMA8 vs EMA21 dalam %,
            slope_e8  : arah slope EMA8 (naik/turun),
            signal    : True kalau ada cross baru (<=5 candle),
            desc      : deskripsi human-readable,
          }
        """
        n = len(df)
        if n < 25:
            return {'bias': 'NEUTRAL', 'cross': None, 'bars_ago': 0,
                    'signal': False, 'desc': 'Data kurang'}

        closes = df['close'].values.astype(float)

        # Hitung EMA 8 dan EMA 21 untuk semua candle
        def _ema(arr, period):
            k   = 2 / (period + 1)
            val = arr[0]
            res = []
            for v in arr:
                val = v * k + val * (1 - k)
                res.append(val)
            return res

        e8_arr  = _ema(closes, 8)
        e21_arr = _ema(closes, 21)

        e8_now  = e8_arr[-1]
        e21_now = e21_arr[-1]
        e8_prev = e8_arr[-2]
        e21_prev = e21_arr[-2]

        gap_pct = (e8_now - e21_now) / max(e21_now, 0.000001) * 100
        slope_e8 = e8_now - e8_arr[-4]  # slope 3 candle

        # Deteksi kapan cross terjadi (cari titik perubahan posisi relatif)
        cross_type = None
        bars_ago   = 0
        for i in range(1, min(10, n)):
            curr_above = e8_arr[-i]  > e21_arr[-i]
            prev_above = e8_arr[-i-1] > e21_arr[-i-1]
            if curr_above != prev_above:
                bars_ago   = i
                cross_type = 'GOLDEN' if curr_above else 'DEATH'
                break

        # Bias
        if e8_now > e21_now:
            bias = 'BULLISH'
        elif e8_now < e21_now:
            bias = 'BEARISH'
        else:
            bias = 'NEUTRAL'

        # Signal kuat kalau cross baru (<=5 candle) dan slope searah
        fresh_cross = cross_type is not None and bars_ago <= 5
        momentum_ok = (bias == 'BULLISH' and slope_e8 > 0) or                       (bias == 'BEARISH' and slope_e8 < 0)

        # Deskripsi
        cross_desc = ''
        if cross_type == 'GOLDEN' and bars_ago > 0:
            cross_desc = f" | 🌟 Golden Cross {bars_ago} candle lalu"
        elif cross_type == 'DEATH' and bars_ago > 0:
            cross_desc = f" | 💀 Death Cross {bars_ago} candle lalu"

        slope_desc = '↗' if slope_e8 > 0 else ('↘' if slope_e8 < 0 else '→')

        if bias == 'BULLISH':
            desc = f"EMA8 ({e8_now:.4f}) > EMA21 ({e21_now:.4f}) {slope_desc} → PELUANG LONG{cross_desc}"
        elif bias == 'BEARISH':
            desc = f"EMA8 ({e8_now:.4f}) < EMA21 ({e21_now:.4f}) {slope_desc} → PELUANG SHORT{cross_desc}"
        else:
            desc = f"EMA8 ≈ EMA21 ({e8_now:.4f}) → SIDEWAYS"

        return {
            'bias'      : bias,
            'cross'     : cross_type,
            'bars_ago'  : bars_ago,
            'e8'        : round(e8_now, 8),
            'e21'       : round(e21_now, 8),
            'gap_pct'   : round(gap_pct, 3),
            'slope_e8'  : slope_e8,
            'signal'    : fresh_cross,
            'momentum'  : momentum_ok,
            'desc'      : desc,
        }

    # ==================================================================
    # DERIVATIVES DATA — Funding Rate, Open Interest, Long/Short Ratio
    # ==================================================================
    def fetch_derivatives(self, symbol):
        """
        Fetch data derivatif dari Binance Futures (public, no auth).
        Return: {funding_rate, oi, oi_change, lsr, lsr_bias, sentiment}
        """
        cache_key = f"deriv_{symbol}"
        cached = self._cache_get(self.price_cache, cache_key, ttl=120)
        if cached is not None:
            return cached

        pair = f"{symbol}USDT"
        result = {
            'funding_rate': 0, 'funding_bias': 'NEUTRAL',
            'oi': 0, 'oi_change_pct': 0, 'oi_bias': 'NEUTRAL',
            'lsr': 1.0, 'lsr_bias': 'NEUTRAL',
            'sentiment': 'NEUTRAL', 'available': False
        }

        try:
            # 1. Funding Rate
            url = "https://fapi.binance.com/fapi/v1/fundingRate"
            data = self._http_get(url, params={'symbol': pair, 'limit': 3})
            if data and len(data) > 0:
                fr = float(data[-1].get('fundingRate', 0))
                result['funding_rate'] = round(fr * 100, 4)  # Convert to %
                if fr > 0.01:
                    result['funding_bias'] = 'BEARISH'   # Longs pay shorts = terlalu banyak long
                elif fr > 0.005:
                    result['funding_bias'] = 'SLIGHT_BEAR'
                elif fr < -0.005:
                    result['funding_bias'] = 'BULLISH'   # Shorts pay longs = terlalu banyak short
                elif fr < -0.01:
                    result['funding_bias'] = 'SLIGHT_BULL'

            # 2. Open Interest
            url = "https://fapi.binance.com/fapi/v1/openInterest"
            data = self._http_get(url, params={'symbol': pair})
            if data:
                result['oi'] = float(data.get('openInterest', 0))

            # OI history (24h change)
            url = "https://fapi.binance.com/futures/data/openInterestHist"
            data = self._http_get(url, params={'symbol': pair, 'period': '1h', 'limit': 24})
            if data and len(data) >= 2:
                oi_now = float(data[-1].get('sumOpenInterest', 0))
                oi_24h = float(data[0].get('sumOpenInterest', 0))
                if oi_24h > 0:
                    result['oi_change_pct'] = round((oi_now - oi_24h) / oi_24h * 100, 2)
                if result['oi_change_pct'] > 5:
                    result['oi_bias'] = 'RISING'    # New positions opening
                elif result['oi_change_pct'] < -5:
                    result['oi_bias'] = 'FALLING'   # Positions closing

            # 3. Long/Short Ratio (global)
            url = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
            data = self._http_get(url, params={'symbol': pair, 'period': '1h', 'limit': 5})
            if data and len(data) > 0:
                lsr = float(data[-1].get('longShortRatio', 1))
                result['lsr'] = round(lsr, 3)
                if lsr > 1.8:
                    result['lsr_bias'] = 'CROWD_LONG'   # Crowd long = contrarian bearish
                elif lsr > 1.3:
                    result['lsr_bias'] = 'SLIGHT_LONG'
                elif lsr < 0.55:
                    result['lsr_bias'] = 'CROWD_SHORT'  # Crowd short = contrarian bullish
                elif lsr < 0.75:
                    result['lsr_bias'] = 'SLIGHT_SHORT'

            # Composite sentiment
            bull = bear = 0
            # Funding: negatif = bullish (shorts bayar)
            if result['funding_bias'] in ('BULLISH', 'SLIGHT_BULL'): bull += 1
            if result['funding_bias'] in ('BEARISH', 'SLIGHT_BEAR'): bear += 1
            # OI rising + price up = bullish confirmation
            if result['oi_bias'] == 'RISING': bull += 1
            elif result['oi_bias'] == 'FALLING': bear += 1
            # LSR contrarian: crowd long = bearish signal
            if result['lsr_bias'] in ('CROWD_LONG', 'SLIGHT_LONG'): bear += 1
            elif result['lsr_bias'] in ('CROWD_SHORT', 'SLIGHT_SHORT'): bull += 1

            if bull > bear: result['sentiment'] = 'BULLISH'
            elif bear > bull: result['sentiment'] = 'BEARISH'
            result['available'] = True

        except Exception as e:
            logger.debug(f"Derivatives fetch error for {symbol}: {e}")

        self._cache_set(self.price_cache, cache_key, result)
        return result

    # ==================================================================
    # CANDLESTICK PATTERN LIBRARY — 15+ patterns
    # ==================================================================
    def detect_candle_patterns(self, df, atr=None):
        """
        Deteksi 15+ candlestick pattern dari 3 candle terakhir.
        Return: list of {pattern, direction, strength, desc}
        """
        if df is None or len(df) < 5:
            return []

        h = df['high'].values
        l = df['low'].values
        o = df['open'].values
        c = df['close'].values
        n = len(df)

        if atr is None:
            atr = np.mean(h[-20:] - l[-20:])
        if atr <= 0:
            atr = 1

        patterns = []

        # Helper lambdas
        body = lambda i: abs(c[i] - o[i])
        upper_wick = lambda i: h[i] - max(o[i], c[i])
        lower_wick = lambda i: min(o[i], c[i]) - l[i]
        is_bull = lambda i: c[i] > o[i]
        is_bear = lambda i: c[i] < o[i]
        candle_range = lambda i: h[i] - l[i]

        i = n - 1  # current candle
        i1 = n - 2  # previous
        i2 = n - 3  # 2 candles ago

        b0, b1, b2 = body(i), body(i1), body(i2)
        cr0 = candle_range(i)
        uw0, lw0 = upper_wick(i), lower_wick(i)
        uw1, lw1 = upper_wick(i1), lower_wick(i1)

        # === SINGLE CANDLE PATTERNS ===

        # 1. HAMMER (bullish reversal) — long lower wick, small body at top
        if lw0 >= b0 * 2 and uw0 < b0 * 0.5 and cr0 > atr * 0.5:
            patterns.append({'pattern': 'Hammer', 'direction': 'BULL', 'strength': 2,
                'desc': 'Hammer — buyer reject harga rendah'})

        # 2. INVERTED HAMMER (bullish) — long upper wick at bottom
        if uw0 >= b0 * 2 and lw0 < b0 * 0.5 and cr0 > atr * 0.5 and is_bear(i1):
            patterns.append({'pattern': 'Inverted Hammer', 'direction': 'BULL', 'strength': 1,
                'desc': 'Inverted Hammer — potensi reversal naik'})

        # 3. SHOOTING STAR (bearish reversal) — long upper wick, small body at bottom
        if uw0 >= b0 * 2 and lw0 < b0 * 0.5 and cr0 > atr * 0.5 and is_bull(i1):
            patterns.append({'pattern': 'Shooting Star', 'direction': 'BEAR', 'strength': 2,
                'desc': 'Shooting Star — seller reject harga tinggi'})

        # 4. HANGING MAN (bearish) — like hammer but at top of uptrend
        if lw0 >= b0 * 2 and uw0 < b0 * 0.5 and is_bull(i1) and is_bull(i2):
            patterns.append({'pattern': 'Hanging Man', 'direction': 'BEAR', 'strength': 1,
                'desc': 'Hanging Man — warning di puncak'})

        # 5. DOJI — tiny body, big wicks = indecision
        if b0 < atr * 0.1 and cr0 > atr * 0.5:
            d_type = 'Dragonfly Doji' if lw0 > uw0 * 2 else ('Gravestone Doji' if uw0 > lw0 * 2 else 'Doji')
            d_dir = 'BULL' if d_type == 'Dragonfly Doji' else ('BEAR' if d_type == 'Gravestone Doji' else 'NEUTRAL')
            patterns.append({'pattern': d_type, 'direction': d_dir, 'strength': 1,
                'desc': f'{d_type} — pasar ragu, potensi reversal'})

        # 6. MARUBOZU (strong momentum) — no/tiny wicks
        if b0 > atr * 0.8 and uw0 < b0 * 0.1 and lw0 < b0 * 0.1:
            d = 'BULL' if is_bull(i) else 'BEAR'
            patterns.append({'pattern': f'Marubozu {d.title()}', 'direction': d, 'strength': 2,
                'desc': f'Marubozu — momentum {d.lower()} sangat kuat'})

        # === DOUBLE CANDLE PATTERNS ===

        # 7. BULLISH ENGULFING
        if is_bear(i1) and is_bull(i) and c[i] > o[i1] and o[i] < c[i1] and b0 > b1:
            patterns.append({'pattern': 'Bullish Engulfing', 'direction': 'BULL', 'strength': 3,
                'desc': 'Bullish Engulfing — buyer dominasi, reversal kuat'})

        # 8. BEARISH ENGULFING
        if is_bull(i1) and is_bear(i) and c[i] < o[i1] and o[i] > c[i1] and b0 > b1:
            patterns.append({'pattern': 'Bearish Engulfing', 'direction': 'BEAR', 'strength': 3,
                'desc': 'Bearish Engulfing — seller dominasi, reversal kuat'})

        # 9. TWEEZER TOP (bearish)
        if abs(h[i] - h[i1]) < atr * 0.05 and is_bull(i1) and is_bear(i):
            patterns.append({'pattern': 'Tweezer Top', 'direction': 'BEAR', 'strength': 2,
                'desc': 'Tweezer Top — double rejection di resistance'})

        # 10. TWEEZER BOTTOM (bullish)
        if abs(l[i] - l[i1]) < atr * 0.05 and is_bear(i1) and is_bull(i):
            patterns.append({'pattern': 'Tweezer Bottom', 'direction': 'BULL', 'strength': 2,
                'desc': 'Tweezer Bottom — double rejection di support'})

        # 11. PIERCING LINE (bullish)
        if is_bear(i1) and is_bull(i) and o[i] < l[i1] and c[i] > (o[i1] + c[i1]) / 2:
            patterns.append({'pattern': 'Piercing Line', 'direction': 'BULL', 'strength': 2,
                'desc': 'Piercing Line — buyer comeback kuat'})

        # 12. DARK CLOUD COVER (bearish)
        if is_bull(i1) and is_bear(i) and o[i] > h[i1] and c[i] < (o[i1] + c[i1]) / 2:
            patterns.append({'pattern': 'Dark Cloud Cover', 'direction': 'BEAR', 'strength': 2,
                'desc': 'Dark Cloud Cover — seller masuk agresif'})

        # === TRIPLE CANDLE PATTERNS ===

        # 13. MORNING STAR (bullish reversal)
        if is_bear(i2) and b1 < b2 * 0.3 and is_bull(i) and c[i] > (o[i2] + c[i2]) / 2:
            patterns.append({'pattern': 'Morning Star', 'direction': 'BULL', 'strength': 3,
                'desc': 'Morning Star — reversal bullish kuat (3 candle)'})

        # 14. EVENING STAR (bearish reversal)
        if is_bull(i2) and b1 < b2 * 0.3 and is_bear(i) and c[i] < (o[i2] + c[i2]) / 2:
            patterns.append({'pattern': 'Evening Star', 'direction': 'BEAR', 'strength': 3,
                'desc': 'Evening Star — reversal bearish kuat (3 candle)'})

        # 15. THREE WHITE SOLDIERS (strong bullish)
        if all(is_bull(n-1-j) for j in range(3)) and c[i] > c[i1] > c[i2] and all(body(n-1-j) > atr * 0.3 for j in range(3)):
            patterns.append({'pattern': 'Three White Soldiers', 'direction': 'BULL', 'strength': 3,
                'desc': 'Three White Soldiers — momentum bullish sangat kuat'})

        # 16. THREE BLACK CROWS (strong bearish)
        if all(is_bear(n-1-j) for j in range(3)) and c[i] < c[i1] < c[i2] and all(body(n-1-j) > atr * 0.3 for j in range(3)):
            patterns.append({'pattern': 'Three Black Crows', 'direction': 'BEAR', 'strength': 3,
                'desc': 'Three Black Crows — momentum bearish sangat kuat'})

        # 17. SPINNING TOP — small body, long equal wicks = indecision
        if b0 < cr0 * 0.3 and uw0 > b0 and lw0 > b0:
            patterns.append({'pattern': 'Spinning Top', 'direction': 'NEUTRAL', 'strength': 1,
                'desc': 'Spinning Top — pasar ragu, tunggu konfirmasi'})

        return patterns

    # ==================================================================
    # LIQUIDATION ZONE ESTIMATION
    # ==================================================================
    def estimate_liquidation_zones(self, df, price, atr, derivatives=None):
        """
        Estimasi zona liquidasi berdasarkan:
        - Swing levels (dimana banyak SL ditempatkan)
        - ATR-based distance (leverage zones: 5x, 10x, 20x, 50x)
        - OI data (kalau tersedia)

        Return: {
          'long_liq_zones': [{price, leverage, strength}],  # dimana long bisa kena liquidasi
          'short_liq_zones': [{price, leverage, strength}],  # dimana short bisa kena liquidasi
          'nearest_long_liq': float,  # liquidasi long terdekat
          'nearest_short_liq': float,  # liquidasi short terdekat
          'liq_bias': str,  # arah mana yang lebih rentan
        }
        """
        result = {
            'long_liq_zones': [], 'short_liq_zones': [],
            'nearest_long_liq': 0, 'nearest_short_liq': 0,
            'liq_bias': 'NEUTRAL'
        }

        if df is None or len(df) < 20:
            return result

        # Leverage-based liquidation distances
        # At Nx leverage, ~(100/N)% move triggers liquidation
        leverages = [
            (50, 0.02),   # 50x = 2% move
            (25, 0.04),   # 25x = 4% move
            (20, 0.05),   # 20x = 5% move
            (10, 0.10),   # 10x = 10% move
            (5, 0.20),    # 5x = 20% move
        ]

        long_zones = []
        short_zones = []

        for lev, pct in leverages:
            # Long liquidation = price drops by pct
            liq_price = price * (1 - pct)
            long_zones.append({
                'price': round(liq_price, 8),
                'leverage': lev,
                'strength': 3 if lev >= 20 else (2 if lev >= 10 else 1)
            })

            # Short liquidation = price rises by pct
            liq_price = price * (1 + pct)
            short_zones.append({
                'price': round(liq_price, 8),
                'leverage': lev,
                'strength': 3 if lev >= 20 else (2 if lev >= 10 else 1)
            })

        # Swing-based liquidation (where many SL are)
        swing_lo, swing_hi = self._find_swings(df, 3)

        for sl in swing_lo[-5:]:  # last 5 swing lows
            # Just below swing low = long SL cluster = long liquidation zone
            liq_p = sl['price'] - atr * 0.3
            long_zones.append({
                'price': round(liq_p, 8), 'leverage': 0,
                'strength': 2, 'note': 'swing_sl'
            })

        for sh in swing_hi[-5:]:  # last 5 swing highs
            liq_p = sh['price'] + atr * 0.3
            short_zones.append({
                'price': round(liq_p, 8), 'leverage': 0,
                'strength': 2, 'note': 'swing_sl'
            })

        # OI boost: if OI is high, liquidation cascade more likely
        if derivatives and derivatives.get('available'):
            oi_change = derivatives.get('oi_change_pct', 0)
            if abs(oi_change) > 5:
                for z in long_zones:
                    z['strength'] += 1
                for z in short_zones:
                    z['strength'] += 1

        result['long_liq_zones'] = sorted(long_zones, key=lambda x: -x['price'])[:8]
        result['short_liq_zones'] = sorted(short_zones, key=lambda x: x['price'])[:8]

        # Nearest liquidation
        if long_zones:
            nearest_long = max(z['price'] for z in long_zones if z['price'] < price)
            result['nearest_long_liq'] = nearest_long
        if short_zones:
            nearest_short = min(z['price'] for z in short_zones if z['price'] > price)
            result['nearest_short_liq'] = nearest_short

        # Bias: which side is more vulnerable?
        dist_long = price - result['nearest_long_liq'] if result['nearest_long_liq'] else price
        dist_short = result['nearest_short_liq'] - price if result['nearest_short_liq'] else price

        if dist_long < dist_short * 0.7:
            result['liq_bias'] = 'LONG_VULNERABLE'  # Long positions closer to liquidation
        elif dist_short < dist_long * 0.7:
            result['liq_bias'] = 'SHORT_VULNERABLE'  # Short positions closer to liquidation

        return result

    # ==================================================================
    # BTC CORRELATION FILTER
    # ==================================================================
    def check_btc_condition(self):
        """
        Cek kondisi BTC sebelum kasih sinyal altcoin.
        BTC bearish → jangan LONG alt. BTC bullish → jangan SHORT alt.
        """
        try:
            pd_ = self.get_price('BTC')
            if not pd_:
                return {'ok_long': True, 'ok_short': True, 'btc_bias': 'NEUTRAL',
                        'btc_change': 0, 'reason': 'BTC data unavailable'}

            btc_chg = pd_['change_24h']

            df = self.get_klines('BTC', '1h', False)
            if df is None or len(df) < 20:
                return {'ok_long': True, 'ok_short': True, 'btc_bias': 'NEUTRAL',
                        'btc_change': btc_chg, 'reason': 'BTC data limited'}

            et, _, _ = self.analyze_ema_trend(df)
            rsi_s = self.calc_rsi(df, 14)
            rsi_val = rsi_s.iloc[-1] if rsi_s is not None else 50
            structure = self.detect_market_structure(df)

            btc_bull = sum([et in ("STRONG_UP", "UP"), structure == "UPTREND", btc_chg > 1])
            btc_bear = sum([et in ("STRONG_DOWN", "DOWN"), structure == "DOWNTREND", btc_chg < -1])

            if btc_bear >= 2 or btc_chg < -3:
                bias = 'BEARISH'
                ok_long = False
                ok_short = True
                reason = f'BTC bearish ({btc_chg:+.1f}%), hindari LONG alt'
            elif btc_bull >= 2 or btc_chg > 3:
                bias = 'BULLISH'
                ok_long = True
                ok_short = False
                reason = f'BTC bullish ({btc_chg:+.1f}%), hindari SHORT alt'
            else:
                bias = 'NEUTRAL'
                ok_long = True
                ok_short = True
                reason = f'BTC netral ({btc_chg:+.1f}%)'

            if btc_chg < -5:
                ok_long = False
                reason = f'BTC CRASH ({btc_chg:+.1f}%) — JANGAN LONG apapun'

            return {
                'ok_long': ok_long, 'ok_short': ok_short,
                'btc_bias': bias, 'btc_change': btc_chg,
                'btc_rsi': round(rsi_val, 1) if not pd.isna(rsi_val) else 50,
                'reason': reason
            }
        except Exception as e:
            logger.debug(f"BTC check error: {e}")
            return {'ok_long': True, 'ok_short': True, 'btc_bias': 'NEUTRAL',
                    'btc_change': 0, 'reason': 'BTC check error'}

    # ==================================================================
    # SMART MONEY CONCEPT — FULL ANALYSIS ENGINE
    # ==================================================================

    # 1. BOS (Break of Structure) & CHoCH (Change of Character)
    def detect_bos_choch(self, df, window=3):
        """
        BOS = break yang MENGKONFIRMASI trend lanjut
        CHoCH = break PERTAMA yang melawan trend → sinyal reversal

        Return: {
          'bos': 'BULLISH'|'BEARISH'|None,
          'choch': 'BULLISH'|'BEARISH'|None,
          'last_bos_idx': int, 'last_choch_idx': int,
          'swing_highs': [...], 'swing_lows': [...]
        }
        """
        if df is None or len(df) < window * 2 + 5:
            return {'bos': None, 'choch': None, 'swing_highs': [], 'swing_lows': [],
                    'last_bos_idx': 0, 'last_choch_idx': 0}

        h, l, c = df['high'].values, df['low'].values, df['close'].values
        n = len(df)

        # Collect swing points with indices
        sh, sl_pts = [], []
        for i in range(window, n - window):
            if h[i] >= max(h[i-window:i]) and h[i] >= max(h[i+1:i+window+1]):
                sh.append({'price': h[i], 'idx': i})
            if l[i] <= min(l[i-window:i]) and l[i] <= min(l[i+1:i+window+1]):
                sl_pts.append({'price': l[i], 'idx': i})

        if len(sh) < 2 or len(sl_pts) < 2:
            return {'bos': None, 'choch': None, 'swing_highs': sh,
                    'swing_lows': sl_pts, 'last_bos_idx': 0, 'last_choch_idx': 0}

        # Determine current trend from last 3 swings
        recent_sh = sh[-3:]
        recent_sl = sl_pts[-3:]

        making_hh = len(recent_sh) >= 2 and recent_sh[-1]['price'] > recent_sh[-2]['price']
        making_hl = len(recent_sl) >= 2 and recent_sl[-1]['price'] > recent_sl[-2]['price']
        making_lh = len(recent_sh) >= 2 and recent_sh[-1]['price'] < recent_sh[-2]['price']
        making_ll = len(recent_sl) >= 2 and recent_sl[-1]['price'] < recent_sl[-2]['price']

        uptrend = making_hh and making_hl
        downtrend = making_lh and making_ll

        bos = choch = None
        last_bos_idx = last_choch_idx = 0

        last_close = c[-1]

        if uptrend:
            # In uptrend: break above last swing high = BULLISH BOS
            if last_close > recent_sh[-1]['price']:
                bos = 'BULLISH'
                last_bos_idx = n - 1
            # Break below last swing low = BEARISH CHoCH (reversal warning!)
            if last_close < recent_sl[-1]['price']:
                choch = 'BEARISH'
                last_choch_idx = n - 1
        elif downtrend:
            # In downtrend: break below last swing low = BEARISH BOS
            if last_close < recent_sl[-1]['price']:
                bos = 'BEARISH'
                last_bos_idx = n - 1
            # Break above last swing high = BULLISH CHoCH (reversal!)
            if last_close > recent_sh[-1]['price']:
                choch = 'BULLISH'
                last_choch_idx = n - 1

        return {
            'bos': bos, 'choch': choch,
            'swing_highs': sh, 'swing_lows': sl_pts,
            'last_bos_idx': last_bos_idx, 'last_choch_idx': last_choch_idx,
            'making_hh': making_hh, 'making_hl': making_hl,
            'making_lh': making_lh, 'making_ll': making_ll,
        }

    # 2. Market Phase Detection (Wyckoff-inspired)
    def detect_market_phase(self, df, atr):
        """
        4 fase market:
        - ACCUMULATION: range ketat di bawah, volume naik → siap rally
        - MARKUP: trending up kuat dengan volume
        - DISTRIBUTION: range ketat di atas, volume naik → siap drop
        - MARKDOWN: trending down kuat
        """
        if df is None or len(df) < 30:
            return {'phase': 'UNKNOWN', 'confidence': 0, 'desc': ''}

        c, v, h, l = df['close'].values, df['volume'].values, df['high'].values, df['low'].values
        n = len(df)

        # Split into recent (last 1/3) and previous (first 2/3)
        split = n * 2 // 3
        prev_c, rec_c = c[:split], c[split:]
        prev_v, rec_v = v[:split], v[split:]
        prev_range = np.mean(h[:split] - l[:split])
        rec_range = np.mean(h[split:] - l[split:])

        # Trend
        price_change = (rec_c[-1] - rec_c[0]) / rec_c[0] if rec_c[0] > 0 else 0

        # Volatility contraction/expansion
        range_ratio = rec_range / max(prev_range, 0.001)

        # Volume trend
        avg_prev_v = np.mean(prev_v) if len(prev_v) > 0 else 1
        avg_rec_v = np.mean(rec_v) if len(rec_v) > 0 else 1
        vol_increase = avg_rec_v > avg_prev_v * 1.2

        conf = 0
        if price_change < -0.02 and range_ratio > 1.2:
            phase, desc = 'MARKDOWN', 'Harga turun agresif, momentum bearish kuat'
            conf = min(abs(price_change) * 20, 1.0)
        elif price_change > 0.02 and range_ratio > 1.2:
            phase, desc = 'MARKUP', 'Harga naik kuat, momentum bullish'
            conf = min(abs(price_change) * 20, 1.0)
        elif range_ratio < 0.7 and price_change <= 0:
            phase = 'ACCUMULATION'
            desc = 'Range menyempit di bawah, smart money mengumpulkan'
            conf = 0.6 + (0.2 if vol_increase else 0)
        elif range_ratio < 0.7 and price_change > 0:
            phase = 'DISTRIBUTION'
            desc = 'Range menyempit di atas, smart money mendistribusi'
            conf = 0.6 + (0.2 if vol_increase else 0)
        else:
            phase, desc, conf = 'TRANSITION', 'Fase transisi, menunggu konfirmasi', 0.3

        return {'phase': phase, 'confidence': round(conf, 2), 'desc': desc,
                'vol_increase': vol_increase, 'range_ratio': round(range_ratio, 2)}

    # 3. Liquidity Mapping — where are the stops?
    def map_liquidity(self, df, atr):
        """
        Cari liquidity pools:
        - Equal Highs (EQH) = stop loss SHORT berjejer → target sweep up
        - Equal Lows (EQL) = stop loss LONG berjejer → target sweep down
        - Relative highs/lows = liquidity yang belum diambil
        """
        if df is None or len(df) < 20 or atr <= 0:
            return {'eqh': [], 'eql': [], 'buy_liq': 0, 'sell_liq': 0}

        h, l = df['high'].values, df['low'].values
        n = len(df)
        tol = atr * 0.2  # equal = selisih < 20% ATR

        # Find Equal Highs (2+ highs at same level)
        eqh, eql = [], []
        recent = min(50, n)
        for i in range(n - recent, n):
            for j in range(i + 2, min(i + 15, n)):
                if abs(h[i] - h[j]) <= tol:
                    eqh.append({'price': (h[i] + h[j]) / 2, 'count': 2,
                                'idx_start': i, 'idx_end': j})
                if abs(l[i] - l[j]) <= tol:
                    eql.append({'price': (l[i] + l[j]) / 2, 'count': 2,
                                'idx_start': i, 'idx_end': j})

        # Cluster equal levels
        eqh = self._cluster_eq_levels(eqh, tol)
        eql = self._cluster_eq_levels(eql, tol)

        # Liquidity score: above price = buy-side, below = sell-side
        price = df['close'].iloc[-1]
        buy_liq = sum(1 for e in eqh if e['price'] > price)
        sell_liq = sum(1 for e in eql if e['price'] < price)

        return {'eqh': eqh[:5], 'eql': eql[:5],
                'buy_liq': buy_liq, 'sell_liq': sell_liq}

    def _cluster_eq_levels(self, levels, tol):
        if not levels:
            return []
        levels.sort(key=lambda x: x['price'])
        clustered = []
        current = levels[0].copy()
        for i in range(1, len(levels)):
            if abs(levels[i]['price'] - current['price']) <= tol * 2:
                current['count'] += levels[i]['count']
                current['price'] = (current['price'] + levels[i]['price']) / 2
            else:
                clustered.append(current)
                current = levels[i].copy()
        clustered.append(current)
        return sorted(clustered, key=lambda x: x['count'], reverse=True)

    # 4. Order Flow Analysis — buying vs selling pressure
    def analyze_order_flow(self, df, lookback=20):
        """
        Analisa tekanan beli/jual dari price action:
        - Bullish candles vs bearish candles
        - Close position in range (close near high = bullish)
        - Volume on up vs down candles
        - Wick analysis (long wicks = rejection)
        """
        if df is None or len(df) < lookback:
            return {'bias': 'NEUTRAL', 'score': 0, 'bull_pct': 50, 'details': ''}

        recent = df.iloc[-lookback:]
        o, c, h, l, v = (recent['open'].values, recent['close'].values,
                          recent['high'].values, recent['low'].values,
                          recent['volume'].values)

        bull_candles = sum(1 for i in range(len(c)) if c[i] > o[i])
        bear_candles = len(c) - bull_candles
        total = len(c)

        # Close position in range (0=low, 1=high)
        close_positions = []
        for i in range(len(c)):
            rng = h[i] - l[i]
            if rng > 0:
                close_positions.append((c[i] - l[i]) / rng)
            else:
                close_positions.append(0.5)
        avg_close_pos = np.mean(close_positions[-10:])  # last 10

        # Volume on up vs down candles
        bull_vol = sum(v[i] for i in range(len(c)) if c[i] > o[i])
        bear_vol = sum(v[i] for i in range(len(c)) if c[i] <= o[i])
        total_vol = bull_vol + bear_vol

        bull_vol_pct = (bull_vol / max(total_vol, 1)) * 100

        # Score: -100 (max bearish) to +100 (max bullish)
        score = 0
        score += (bull_candles - bear_candles) * 3  # candle count
        score += (avg_close_pos - 0.5) * 40         # close position
        score += (bull_vol_pct - 50) * 0.5           # volume bias

        score = max(-100, min(100, score))

        if score > 30:
            bias, details = 'BULLISH', f'Tekanan beli dominan ({bull_candles}/{total} bullish, vol {bull_vol_pct:.0f}%)'
        elif score < -30:
            bias, details = 'BEARISH', f'Tekanan jual dominan ({bear_candles}/{total} bearish, vol {100-bull_vol_pct:.0f}%)'
        else:
            bias, details = 'NEUTRAL', f'Seimbang ({bull_candles}/{total} bullish)'

        return {
            'bias': bias, 'score': round(score),
            'bull_pct': round(bull_vol_pct), 'bear_pct': round(100 - bull_vol_pct),
            'avg_close_pos': round(avg_close_pos, 2),
            'details': details
        }

    # 5. Premium/Discount Zone
    def calc_premium_discount(self, df, price):
        """
        Dealing Range: dari swing low ke swing high terbaru.
        Di atas 50% = PREMIUM (mahal, ideal untuk short)
        Di bawah 50% = DISCOUNT (murah, ideal untuk long)
        """
        if df is None or len(df) < 20:
            return {'zone': 'EQUILIBRIUM', 'pct': 50, 'range_high': 0, 'range_low': 0}

        recent = df.iloc[-50:]
        range_hi = recent['high'].max()
        range_lo = recent['low'].min()
        total_range = range_hi - range_lo
        if total_range <= 0:
            return {'zone': 'EQUILIBRIUM', 'pct': 50, 'range_high': range_hi, 'range_low': range_lo}

        position = (price - range_lo) / total_range * 100

        if position > 70:
            zone = 'PREMIUM'
        elif position < 30:
            zone = 'DISCOUNT'
        elif position > 55:
            zone = 'SLIGHT_PREMIUM'
        elif position < 45:
            zone = 'SLIGHT_DISCOUNT'
        else:
            zone = 'EQUILIBRIUM'

        return {
            'zone': zone, 'pct': round(position, 1),
            'range_high': range_hi, 'range_low': range_lo,
            'eq_level': (range_hi + range_lo) / 2
        }

    # 6. Volume Divergence
    def detect_volume_divergence(self, df):
        """
        Harga naik tapi volume turun = bearish divergence (weakness)
        Harga turun tapi volume turun = bullish divergence (seller exhaustion)
        """
        if df is None or len(df) < 20:
            return {'divergence': None, 'desc': ''}

        c = df['close'].values
        v = df['volume'].values
        n = len(c)
        mid = n // 2

        price_trend = c[-1] - c[mid]
        vol_prev = np.mean(v[mid-5:mid+5]) if mid >= 5 else np.mean(v[:mid])
        vol_recent = np.mean(v[-10:])

        if price_trend > 0 and vol_recent < vol_prev * 0.7:
            return {'divergence': 'BEARISH', 'desc': 'Harga naik tapi volume melemah — potensi reversal turun'}
        elif price_trend < 0 and vol_recent < vol_prev * 0.7:
            return {'divergence': 'BULLISH', 'desc': 'Harga turun tapi volume mengering — seller habis, potensi reversal naik'}
        elif price_trend > 0 and vol_recent > vol_prev * 1.3:
            return {'divergence': 'CONFIRM_BULL', 'desc': 'Harga naik dengan volume tinggi — trend kuat'}
        elif price_trend < 0 and vol_recent > vol_prev * 1.3:
            return {'divergence': 'CONFIRM_BEAR', 'desc': 'Harga turun dengan volume tinggi — tekanan jual kuat'}
        return {'divergence': None, 'desc': 'Volume normal'}

    # 6b. RSI Divergence Detection
    def detect_rsi_divergence(self, df, period=14):
        """
        Bullish Div: price makes lower low, RSI makes higher low → reversal UP
        Bearish Div: price makes higher high, RSI makes lower high → reversal DOWN
        Hidden Bull: price makes higher low, RSI makes lower low → trend continuation UP
        Hidden Bear: price makes lower high, RSI makes higher high → trend continuation DOWN
        """
        if df is None or len(df) < period + 20:
            return {'type': None, 'desc': ''}

        c = df['close'].values
        rsi_s = self.calc_rsi(df, period)
        if rsi_s is None:
            return {'type': None, 'desc': ''}
        rsi = rsi_s.values
        n = len(c)

        # Find last 2 swing lows and highs in price AND RSI (last 30 candles)
        lookback = min(30, n - 5)
        seg_c = c[-(lookback):]
        seg_r = rsi[-(lookback):]
        seg_l = df['low'].values[-(lookback):]
        seg_h = df['high'].values[-(lookback):]

        # Find swing lows (simplified: local min in 3-candle window)
        lows_p, lows_r = [], []
        highs_p, highs_r = [], []
        for i in range(2, len(seg_c) - 2):
            if seg_l[i] <= min(seg_l[i-2:i]) and seg_l[i] <= min(seg_l[i+1:i+3]):
                lows_p.append(seg_l[i])
                lows_r.append(seg_r[i])
            if seg_h[i] >= max(seg_h[i-2:i]) and seg_h[i] >= max(seg_h[i+1:i+3]):
                highs_p.append(seg_h[i])
                highs_r.append(seg_r[i])

        # Regular Bullish Divergence
        if len(lows_p) >= 2:
            if lows_p[-1] < lows_p[-2] and lows_r[-1] > lows_r[-2]:
                return {'type': 'BULLISH_DIV', 'desc': 'RSI Bullish Divergence — harga lower low tapi RSI higher low'}
        # Regular Bearish Divergence
        if len(highs_p) >= 2:
            if highs_p[-1] > highs_p[-2] and highs_r[-1] < highs_r[-2]:
                return {'type': 'BEARISH_DIV', 'desc': 'RSI Bearish Divergence — harga higher high tapi RSI lower high'}
        # Hidden Bullish
        if len(lows_p) >= 2:
            if lows_p[-1] > lows_p[-2] and lows_r[-1] < lows_r[-2]:
                return {'type': 'HIDDEN_BULL', 'desc': 'Hidden Bullish Div — trend continuation naik'}
        # Hidden Bearish
        if len(highs_p) >= 2:
            if highs_p[-1] < highs_p[-2] and highs_r[-1] > highs_r[-2]:
                return {'type': 'HIDDEN_BEAR', 'desc': 'Hidden Bearish Div — trend continuation turun'}

        return {'type': None, 'desc': ''}

    # 6c. Confirmation Candle at Level
    def detect_confirmation_candle(self, df, level_low, level_high, direction, atr):
        """
        Cek apakah candle terakhir (1-3 candle) menunjukkan rejection di level:
        - Pin Bar: wick panjang > 2x body, body kecil
        - Engulfing: candle besar menelan candle sebelumnya
        - Rejection wick: wick menyentuh level tapi close jauh dari level
        
        direction: 'LONG' (cek di support) atau 'SHORT' (cek di resistance)
        Return: {'confirmed': bool, 'pattern': str, 'score': int}
        """
        if df is None or len(df) < 3 or atr <= 0:
            return {'confirmed': False, 'pattern': 'none', 'score': 0}

        # Volume average — pro trader selalu konfirmasi volume
        avg_vol = 0
        if 'volume' in df.columns and len(df) >= 20:
            avg_vol = df['volume'].rolling(20).mean().iloc[-1]

        # Check last 3 candles
        best_score = 0
        best_pattern = 'none'

        for offset in range(1, 4):  # candle -1, -2, -3
            if offset >= len(df):
                break
            row = df.iloc[-offset]
            prev = df.iloc[-(offset+1)] if offset + 1 <= len(df) else None
            o, c, h, l = row['open'], row['close'], row['high'], row['low']
            body = abs(c - o)
            full_range = h - l
            if full_range == 0:
                continue

            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l
            body_ratio = body / full_range

            score = 0
            pattern = 'none'

            if direction == 'LONG':
                # Pin Bar Bullish: lower wick panjang, body kecil, close > open
                if lower_wick > body * 2 and lower_wick > atr * 0.3 and l <= level_high:
                    score = 3 if c > o else 2
                    pattern = 'Pin Bar Bullish'
                # Bullish Engulfing
                if prev is not None and c > o and c > prev['open'] and o < prev['close'] and prev['close'] < prev['open']:
                    if body > abs(prev['close'] - prev['open']) and l <= level_high * 1.005:
                        score = max(score, 3)
                        pattern = 'Bullish Engulfing'
                # Rejection wick: wick masuk zone tapi body jauh
                if l <= level_high and c > level_high and lower_wick > body:
                    score = max(score, 2)
                    if pattern == 'none': pattern = 'Rejection Wick'

            elif direction == 'SHORT':
                # Pin Bar Bearish: upper wick panjang
                if upper_wick > body * 2 and upper_wick > atr * 0.3 and h >= level_low:
                    score = 3 if c < o else 2
                    pattern = 'Pin Bar Bearish'
                # Bearish Engulfing
                if prev is not None and c < o and c < prev['open'] and o > prev['close'] and prev['close'] > prev['open']:
                    if body > abs(prev['close'] - prev['open']) and h >= level_low * 0.995:
                        score = max(score, 3)
                        pattern = 'Bearish Engulfing'
                # Rejection wick
                if h >= level_low and c < level_low and upper_wick > body:
                    score = max(score, 2)
                    if pattern == 'none': pattern = 'Rejection Wick'

            # Volume confirmation: high volume = kuat, low volume = lemah
            if score > 0 and avg_vol > 0 and 'volume' in df.columns:
                cv = row.get('volume', 0) if hasattr(row, 'get') else 0
                if cv > avg_vol * 1.5:
                    score += 1   # Strong volume = strong pattern
                elif cv < avg_vol * 0.5:
                    score = max(score - 1, 0)  # Weak volume = weak pattern

            if score > best_score:
                best_score = score
                best_pattern = pattern

        return {
            'confirmed': best_score >= 2,
            'pattern': best_pattern,
            'score': best_score
        }

    # 6d. Fair Value Gap (FVG / Imbalance)
    def detect_fvg(self, df, atr, price):
        """
        FVG = gap antara candle 1 dan candle 3 yang tidak overlap.
        Bullish FVG: candle1.high < candle3.low (gap up)
        Bearish FVG: candle1.low > candle3.high (gap down)
        FVG yang belum di-fill = magnet harga.
        """
        if df is None or len(df) < 10 or atr <= 0:
            return {'bull_fvg': [], 'bear_fvg': [], 'nearest': None}

        h, l, c = df['high'].values, df['low'].values, df['close'].values
        n = len(df)
        min_gap = atr * 0.3  # minimum gap size

        bull_fvg, bear_fvg = [], []

        for i in range(2, min(50, n)):
            # Bullish FVG: candle[i-2] high < candle[i] low
            if l[i] - h[i-2] > min_gap:
                gap_low, gap_high = h[i-2], l[i]
                # Check if filled by subsequent candles
                filled = any(l[j] <= gap_high and h[j] >= gap_low for j in range(i+1, n))
                if not filled:
                    bull_fvg.append({'low': gap_low, 'high': gap_high,
                                     'mid': (gap_low + gap_high) / 2, 'idx': n - i})

            # Bearish FVG
            if l[i-2] - h[i] > min_gap:
                gap_low, gap_high = h[i], l[i-2]
                filled = any(h[j] >= gap_low and l[j] <= gap_high for j in range(i+1, n))
                if not filled:
                    bear_fvg.append({'low': gap_low, 'high': gap_high,
                                      'mid': (gap_low + gap_high) / 2, 'idx': n - i})

        # Find nearest unfilled FVG to price
        all_fvg = [('bull', f) for f in bull_fvg] + [('bear', f) for f in bear_fvg]
        nearest = None
        min_dist = float('inf')
        for ftype, fvg in all_fvg:
            dist = abs(price - fvg['mid'])
            if dist < min_dist:
                min_dist = dist
                nearest = {'type': ftype, **fvg}

        return {'bull_fvg': bull_fvg[:3], 'bear_fvg': bear_fvg[:3], 'nearest': nearest}


    # 7. MASTER: Full SMC Analysis
    def build_smc_analysis(self, df_main, df_higher, price, atr, symbol=''):
        """Gabungkan semua SMC analysis jadi 1 objek keputusan."""
        smc = {}

        # BOS & CHoCH — main TF
        smc['bos_choch'] = self.detect_bos_choch(df_main)
        # BOS & CHoCH — HTF
        smc['htf_bos'] = self.detect_bos_choch(df_higher, window=5) if df_higher is not None and len(df_higher) >= 15 else {'bos': None, 'choch': None}

        # Market Phase
        smc['phase'] = self.detect_market_phase(df_main, atr)

        # Liquidity Map
        smc['liquidity'] = self.map_liquidity(df_main, atr)

        # Order Flow
        smc['order_flow'] = self.analyze_order_flow(df_main)

        # Premium / Discount
        smc['pd_zone'] = self.calc_premium_discount(df_main, price)

        # Volume Divergence
        smc['vol_div'] = self.detect_volume_divergence(df_main)

        # Candlestick Patterns
        smc['candle_patterns'] = self.detect_candle_patterns(df_main, atr)

        # Derivatives Data
        smc['derivatives'] = self.fetch_derivatives(symbol) if symbol else {
            'funding_rate': 0, 'funding_bias': 'NEUTRAL',
            'oi': 0, 'oi_change_pct': 0, 'oi_bias': 'NEUTRAL',
            'lsr': 1.0, 'lsr_bias': 'NEUTRAL',
            'sentiment': 'NEUTRAL', 'available': False
        }

        # Liquidation Zones
        smc['liquidation'] = self.estimate_liquidation_zones(df_main, price, atr, smc['derivatives'])

        # === SMART BIAS ===
        # Gabungkan semua sinyal jadi 1 keputusan
        bull_points = bear_points = 0

        bc = smc['bos_choch']
        if bc['bos'] == 'BULLISH': bull_points += 3
        if bc['bos'] == 'BEARISH': bear_points += 3
        if bc['choch'] == 'BULLISH': bull_points += 4  # CHoCH lebih berat
        if bc['choch'] == 'BEARISH': bear_points += 4

        hbc = smc['htf_bos']
        if hbc['bos'] == 'BULLISH': bull_points += 5  # HTF paling berat
        if hbc['bos'] == 'BEARISH': bear_points += 5
        if hbc['choch'] == 'BULLISH': bull_points += 6
        if hbc['choch'] == 'BEARISH': bear_points += 6

        ph = smc['phase']['phase']
        if ph == 'ACCUMULATION': bull_points += 3
        if ph == 'MARKUP': bull_points += 2
        if ph == 'DISTRIBUTION': bear_points += 3
        if ph == 'MARKDOWN': bear_points += 2

        of = smc['order_flow']
        if of['bias'] == 'BULLISH': bull_points += 2
        if of['bias'] == 'BEARISH': bear_points += 2

        pz = smc['pd_zone']['zone']
        if pz in ('DISCOUNT', 'SLIGHT_DISCOUNT'): bull_points += 2
        if pz in ('PREMIUM', 'SLIGHT_PREMIUM'): bear_points += 2

        vd = smc['vol_div']['divergence']
        if vd == 'BULLISH': bull_points += 2
        if vd == 'BEARISH': bear_points += 2
        if vd == 'CONFIRM_BULL': bull_points += 1
        if vd == 'CONFIRM_BEAR': bear_points += 1

        liq = smc['liquidity']
        if liq['buy_liq'] > liq['sell_liq']: bear_points += 1  # liquidity above = magnet up (then dump)
        if liq['sell_liq'] > liq['buy_liq']: bull_points += 1  # liquidity below = magnet down (then pump)

        # Derivatives sentiment
        deriv = smc['derivatives']
        if deriv.get('available'):
            if deriv['sentiment'] == 'BULLISH': bull_points += 2
            elif deriv['sentiment'] == 'BEARISH': bear_points += 2
            # Extreme funding rate = strong contrarian signal
            if deriv['funding_rate'] > 0.05:
                bear_points += 2  # Terlalu banyak long = koreksi dekat
            elif deriv['funding_rate'] < -0.03:
                bull_points += 2  # Terlalu banyak short = squeeze dekat

        # Candle pattern consensus
        cp = smc['candle_patterns']
        bull_cp = sum(p['strength'] for p in cp if p['direction'] == 'BULL')
        bear_cp = sum(p['strength'] for p in cp if p['direction'] == 'BEAR')
        if bull_cp >= 3: bull_points += 2
        elif bull_cp >= 1: bull_points += 1
        if bear_cp >= 3: bear_points += 2
        elif bear_cp >= 1: bear_points += 1

        # Liquidation bias
        liq_data = smc['liquidation']
        if liq_data['liq_bias'] == 'LONG_VULNERABLE':
            bear_points += 1  # Banyak long dekat liquidasi = tekanan turun
        elif liq_data['liq_bias'] == 'SHORT_VULNERABLE':
            bull_points += 1  # Banyak short dekat liquidasi = tekanan naik

        total = bull_points + bear_points
        if total == 0:
            smart_bias = 'NEUTRAL'
            confidence = 0
        elif bull_points > bear_points:
            smart_bias = 'BULLISH'
            confidence = round(bull_points / max(total, 1) * 100)
        elif bear_points > bull_points:
            smart_bias = 'BEARISH'
            confidence = round(bear_points / max(total, 1) * 100)
        else:
            smart_bias = 'NEUTRAL'
            confidence = 50

        # RSI Divergence
        smc['rsi_div'] = self.detect_rsi_divergence(df_main)

        # FVG
        smc['fvg'] = self.detect_fvg(df_main, atr, price)

        # RSI divergence adds to bias
        rd = smc['rsi_div']['type']
        if rd == 'BULLISH_DIV': bull_points += 3
        elif rd == 'BEARISH_DIV': bear_points += 3
        elif rd == 'HIDDEN_BULL': bull_points += 2
        elif rd == 'HIDDEN_BEAR': bear_points += 2

        smc['smart_bias'] = smart_bias
        smc['confidence'] = confidence
        smc['bull_points'] = bull_points
        smc['bear_points'] = bear_points

        # Recalc with divergence
        total = bull_points + bear_points
        if total == 0:
            smc['smart_bias'] = 'NEUTRAL'
            smc['confidence'] = 0
        elif bull_points > bear_points:
            smc['smart_bias'] = 'BULLISH'
            smc['confidence'] = round(bull_points / max(total, 1) * 100)
        elif bear_points > bull_points:
            smc['smart_bias'] = 'BEARISH'
            smc['confidence'] = round(bear_points / max(total, 1) * 100)
        else:
            smc['smart_bias'] = 'NEUTRAL'
            smc['confidence'] = 50

        return smc

    # 8. Narrative — Human-readable analysis
    def generate_narrative(self, smc, price, ks, kr, ema_trend, structure):
        """Generate analisa seperti trader profesional."""
        lines = []
        bc = smc['bos_choch']
        ph = smc['phase']
        of = smc['order_flow']
        pz = smc['pd_zone']
        vd = smc['vol_div']
        liq = smc['liquidity']
        hbc = smc['htf_bos']

        # Market phase
        lines.append(f"📊 *Fase:* {ph['phase']} ({ph['confidence']*100:.0f}%)")
        if ph['desc']:
            lines.append(f"   {ph['desc']}")

        # Structure
        if bc['bos']:
            lines.append(f"🔗 *BOS {bc['bos']}* — Struktur trend terkonfirmasi")
        if bc['choch']:
            lines.append(f"⚡ *CHoCH {bc['choch']}* — Tanda reversal!")
        if hbc['bos']:
            lines.append(f"🔗 *HTF BOS {hbc['bos']}* — Trend besar terkonfirmasi")
        if hbc['choch']:
            lines.append(f"⚡ *HTF CHoCH {hbc['choch']}* — Reversal besar!")

        # Order Flow
        icon = '🟢' if of['bias'] == 'BULLISH' else ('🔴' if of['bias'] == 'BEARISH' else '⚪')
        lines.append(f"{icon} *Order Flow:* {of['details']}")

        # Premium/Discount
        pz_icon = '💰' if 'DISCOUNT' in pz['zone'] else ('💎' if 'PREMIUM' in pz['zone'] else '⚖️')
        lines.append(f"{pz_icon} *Zone:* {pz['zone']} ({pz['pct']:.0f}%)")

        # Volume Divergence
        if vd['divergence']:
            lines.append(f"📉 *Volume:* {vd['desc']}")

        # Liquidity
        if liq['eqh'] or liq['eql']:
            parts = []
            if liq['buy_liq'] > 0:
                parts.append(f"{liq['buy_liq']} pool di atas")
            if liq['sell_liq'] > 0:
                parts.append(f"{liq['sell_liq']} pool di bawah")
            if parts:
                lines.append(f"💧 *Likuiditas:* {', '.join(parts)}")

        # RSI Divergence
        rd = smc.get('rsi_div', {})
        if rd.get('type'):
            lines.append(f"📐 *RSI:* {rd['desc']}")

        # FVG
        fvg_data = smc.get('fvg', {})
        n_bull = len(fvg_data.get('bull_fvg', []))
        n_bear = len(fvg_data.get('bear_fvg', []))
        if n_bull or n_bear:
            parts = []
            if n_bull: parts.append(f"{n_bull} bullish")
            if n_bear: parts.append(f"{n_bear} bearish")
            lines.append(f"🕳️ *FVG:* {', '.join(parts)} imbalance belum terisi")

        # Derivatives Data
        deriv = smc.get('derivatives', {})
        if deriv.get('available'):
            fr = deriv['funding_rate']
            lsr = deriv['lsr']
            oi_chg = deriv['oi_change_pct']
            fr_icon = '🔴' if fr > 0.01 else ('🟢' if fr < -0.005 else '⚪')
            lines.append(f"{fr_icon} *Funding:* {fr:+.4f}% | LSR: {lsr:.2f} | OI: {oi_chg:+.1f}%")
            if deriv['sentiment'] != 'NEUTRAL':
                lines.append(f"   Derivatif: {deriv['sentiment']}")

        # Candlestick Patterns
        cps = smc.get('candle_patterns', [])
        if cps:
            top_cp = sorted(cps, key=lambda x: x['strength'], reverse=True)[:3]
            cp_strs = [f"{p['pattern']}({'B' if p['direction']=='BULL' else 'S' if p['direction']=='BEAR' else 'N'})" for p in top_cp]
            lines.append(f"🕯️ *Candle:* {', '.join(cp_strs)}")

        # Liquidation Zones
        liq_d = smc.get('liquidation', {})
        if liq_d.get('nearest_long_liq') and liq_d.get('nearest_short_liq'):
            lines.append(f"💥 *Liq Zone:* Long ${liq_d['nearest_long_liq']:,.0f} | Short ${liq_d['nearest_short_liq']:,.0f}")
            if liq_d['liq_bias'] != 'NEUTRAL':
                tag = 'Long rentan' if liq_d['liq_bias'] == 'LONG_VULNERABLE' else 'Short rentan'
                lines.append(f"   {tag} liquidasi")

        # Conclusion
        bias = smc['smart_bias']
        conf = smc['confidence']
        if bias == 'BULLISH':
            lines.append(f"\n🧠 *SMART MONEY: BULLISH* ({conf}% konfiden)")
        elif bias == 'BEARISH':
            lines.append(f"\n🧠 *SMART MONEY: BEARISH* ({conf}% konfiden)")
        else:
            lines.append(f"\n🧠 *SMART MONEY: NETRAL* — Tunggu konfirmasi")

        return "\n".join(lines)

    # ==================================================================
    # HTF CEILING/FLOOR CHECK
    # Signal di TF kecil HARUS dicek terhadap level TF besar.
    # LONG 1H yang sudah mentok di resistance 4H = JANGAN LONG.
    # SHORT 1H yang sudah mentok di support 4H = JANGAN SHORT.
    # ==================================================================
    def check_htf_ceiling_floor(self, df_htf, price, atr_htf, direction):
        """
        Cek apakah harga sudah dekat S&R di timeframe lebih besar.
        
        Return: {
            'blocked': bool,      # True = signal harus diblok
            'near_level': bool,   # True = dekat tapi belum mentok
            'level_price': float, # harga level HTF
            'level_type': str,    # 'htf_resistance' atau 'htf_support'
            'distance_pct': float,# jarak ke level dalam %
            'reason': str,
        }
        """
        no_block = {'blocked': False, 'near_level': False, 'level_price': 0,
                     'level_type': '', 'distance_pct': 999, 'reason': ''}

        if df_htf is None or len(df_htf) < 20 or atr_htf is None or atr_htf <= 0:
            return no_block

        # Find structural S&R on HTF ONLY
        struct = self._find_structural_sr(df_htf, price, atr_htf)

        h = df_htf['high'].values
        l = df_htf['low'].values
        n = len(df_htf)

        # Also find simple recent extremes on HTF
        lookback = min(50, n - 1)
        htf_high = np.max(h[-lookback:])
        htf_low = np.min(l[-lookback:])

        if direction == 'LONG':
            # Cari RESISTANCE di HTF (ceiling)
            # 1. Structural resistance
            # 2. Recent high HTF
            # 3. Nearest strong swing high
            ceiling = None
            ceiling_type = ''

            # Structural resistance dari HTF
            if struct['resistance']:
                sr = struct['resistance']
                ceiling = sr['mid']
                ceiling_type = 'HTF Structural Resist'

            # Recent HTF high (paling visible)
            if htf_high > price:
                dist_to_recent = (htf_high - price) / price * 100
                if ceiling is None or htf_high < ceiling:
                    ceiling = htf_high
                    ceiling_type = 'HTF Recent High'

            if ceiling is None:
                return no_block

            dist_pct = (ceiling - price) / price * 100

            # Sudah MENTOK di resistance HTF (< 1% dari ceiling)
            if dist_pct < 1.0:
                return {
                    'blocked': True, 'near_level': True,
                    'level_price': ceiling, 'level_type': ceiling_type,
                    'distance_pct': round(dist_pct, 2),
                    'reason': f'Harga sudah di {ceiling_type} ({dist_pct:.1f}%) — JANGAN LONG'
                }
            # Dekat resistance HTF (< 2.5%)
            elif dist_pct < 2.5:
                return {
                    'blocked': False, 'near_level': True,
                    'level_price': ceiling, 'level_type': ceiling_type,
                    'distance_pct': round(dist_pct, 2),
                    'reason': f'Dekat {ceiling_type} ({dist_pct:.1f}%) — hati-hati LONG'
                }

        elif direction == 'SHORT':
            # Cari SUPPORT di HTF (floor)
            floor = None
            floor_type = ''

            if struct['support']:
                ss = struct['support']
                floor = ss['mid']
                floor_type = 'HTF Structural Support'

            if htf_low < price:
                if floor is None or htf_low > floor:
                    floor = htf_low
                    floor_type = 'HTF Recent Low'

            if floor is None:
                return no_block

            dist_pct = (price - floor) / price * 100

            if dist_pct < 1.0:
                return {
                    'blocked': True, 'near_level': True,
                    'level_price': floor, 'level_type': floor_type,
                    'distance_pct': round(dist_pct, 2),
                    'reason': f'Harga sudah di {floor_type} ({dist_pct:.1f}%) — JANGAN SHORT'
                }
            elif dist_pct < 2.5:
                return {
                    'blocked': False, 'near_level': True,
                    'level_price': floor, 'level_type': floor_type,
                    'distance_pct': round(dist_pct, 2),
                    'reason': f'Dekat {floor_type} ({dist_pct:.1f}%) — hati-hati SHORT'
                }

        return no_block

    # ==================================================================
    # LTF ENTRY TRIGGER — Lower Timeframe Confirmation
    # Pro trader: HTF arah → MTF level → LTF konfirmasi candle close
    #
    # TANPA LTF trigger, bot masuk terlalu cepat sebelum harga
    # benar-benar bereaksi di level. LTF membuktikan "level ini kerja".
    # ==================================================================
    def check_ltf_trigger(self, df_ltf, zone_low, zone_high, direction, atr_mtf):
        """
        Cek apakah candle LTF (15m/1h) mengkonfirmasi rejection di zone.
        
        Return: {
            'triggered': bool,    # ada konfirmasi?
            'strength': int,      # 0-5 (semakin tinggi = semakin yakin)
            'pattern': str,       # pattern yang ditemukan
            'desc': str,          # deskripsi untuk user
        }
        """
        no_trigger = {'triggered': False, 'strength': 0, 'pattern': 'none',
                      'desc': 'Belum ada konfirmasi LTF'}

        if df_ltf is None or len(df_ltf) < 10:
            return no_trigger

        h = df_ltf['high'].values
        l = df_ltf['low'].values
        o = df_ltf['open'].values
        c = df_ltf['close'].values
        v = df_ltf['volume'].values
        n = len(df_ltf)

        avg_vol = np.mean(v[-20:]) if n >= 20 else np.mean(v)
        avg_range = np.mean(h[-20:] - l[-20:]) if n >= 20 else np.mean(h - l)
        if avg_range <= 0: avg_range = atr_mtf * 0.25
        if avg_vol <= 0: avg_vol = 1

        checks = []  # list of (score, pattern_name)

        # Scan last 5 LTF candles (= ~1-2 candle di MTF)
        scan_range = min(5, n - 1)

        for offset in range(scan_range):
            idx = n - 1 - offset
            if idx < 1: break

            ci_o, ci_c, ci_h, ci_l = o[idx], c[idx], h[idx], l[idx]
            ci_body = abs(ci_c - ci_o)
            ci_range = ci_h - ci_l
            ci_bull = ci_c > ci_o
            ci_bear = ci_c < ci_o
            ci_vol = v[idx]

            prev_o, prev_c = o[idx-1], c[idx-1]
            prev_bull = prev_c > prev_o
            prev_bear = prev_c < prev_o

            if direction == 'LONG':
                # Check 1: Candle TOUCHED zone dan CLOSED ABOVE zone high
                # Ini konfirmasi paling kuat — price masuk zone, buyer reject
                touched_zone = ci_l <= zone_high
                closed_above = ci_c > zone_high
                if touched_zone and closed_above and ci_bull:
                    vol_factor = 1 if ci_vol > avg_vol else 0
                    checks.append((3 + vol_factor, 'LTF Bullish Close'))

                # Check 2: Pin Bar / Hammer di zona
                lower_wick = min(ci_o, ci_c) - ci_l
                if (ci_l <= zone_high and lower_wick > ci_body * 2
                        and lower_wick > avg_range * 0.5):
                    checks.append((2, 'LTF Pin Bar'))

                # Check 3: Bullish Engulfing di zona
                if (ci_bull and prev_bear and ci_c > prev_o
                        and ci_o <= prev_c and ci_body > abs(prev_c - prev_o)
                        and ci_l <= zone_high * 1.002):
                    checks.append((3, 'LTF Bullish Engulfing'))

                # Check 4: Mini BOS — LTF break swing high terdekat
                # Cari swing high LTF terakhir, cek apakah candle close di atas
                recent_highs = h[max(0, idx-8):idx]
                if len(recent_highs) > 2:
                    recent_swing_high = max(recent_highs)
                    if ci_c > recent_swing_high and ci_l <= zone_high * 1.01:
                        checks.append((2, 'LTF Mini-BOS'))

            elif direction == 'SHORT':
                touched_zone = ci_h >= zone_low
                closed_below = ci_c < zone_low
                if touched_zone and closed_below and ci_bear:
                    vol_factor = 1 if ci_vol > avg_vol else 0
                    checks.append((3 + vol_factor, 'LTF Bearish Close'))

                upper_wick = ci_h - max(ci_o, ci_c)
                if (ci_h >= zone_low and upper_wick > ci_body * 2
                        and upper_wick > avg_range * 0.5):
                    checks.append((2, 'LTF Pin Bar'))

                if (ci_bear and prev_bull and ci_c < prev_o
                        and ci_o >= prev_c and ci_body > abs(prev_c - prev_o)
                        and ci_h >= zone_low * 0.998):
                    checks.append((3, 'LTF Bearish Engulfing'))

                recent_lows = l[max(0, idx-8):idx]
                if len(recent_lows) > 2:
                    recent_swing_low = min(recent_lows)
                    if ci_c < recent_swing_low and ci_h >= zone_low * 0.99:
                        checks.append((2, 'LTF Mini-BOS'))

        if not checks:
            return no_trigger

        # Ambil pattern terkuat
        checks.sort(key=lambda x: -x[0])
        best_score = checks[0][0]
        best_pattern = checks[0][1]
        total_strength = min(sum(s for s, _ in checks), 5)  # cap at 5

        # Count unique patterns
        unique_patterns = list(set(p for _, p in checks))

        desc_parts = [f"{best_pattern}"]
        if len(unique_patterns) > 1:
            desc_parts.append(f"+{len(unique_patterns)-1} konfirmasi lain")
        desc = " | ".join(desc_parts)

        return {
            'triggered': True,
            'strength': total_strength,
            'pattern': best_pattern,
            'patterns': unique_patterns,
            'desc': f"LTF Trigger: {desc}",
        }

    # ==================================================================
    # ENTRY SIGNAL — WRAPPER ke signal_generator.py
    #
    # Method ini dulunya punya scoring internal sendiri (~800 baris) yang
    # tidak pakai SIGNAL_PARAMS dari config.py — jadi semua filter baru
    # (HTF alignment, score threshold, kill count) TIDAK bekerja.
    #
    # Sekarang: thin wrapper ke signal_generator.generate_entry_signal()
    # yang memang single source of truth untuk scoring.
    #
    # Post-processing yang dipertahankan:
    #   - Anti-flip enforcement (blok arah berlawanan dalam lock period)
    #   - Price rounding per tick size
    #   - Signal cache update
    # ==================================================================
    def generate_entry_signal(self, price, atr, ema_trend, structure,
                              ks, kr, res_mtf=None, sup_mtf=None, smc=None,
                              rsi=50.0, htf_ema='SIDEWAYS', df_main=None,
                              symbol='', adx=20.0):
        if _gen_entry_signal is None:
            return None
        if res_mtf is None: res_mtf = []
        if sup_mtf is None: sup_mtf = []
        if smc is None: smc = {}

        # Apply per-coin SP override kalau ada
        coin_sp = getattr(self, '_per_coin_sp', None)
        if coin_sp and _sg_set_active_sp:
            _sg_set_active_sp(coin_sp)

        try:
            sig = _gen_entry_signal(
                price=price, atr=atr,
                ema_trend=ema_trend, structure=structure,
                ks=ks, kr=kr,
                res_mtf=res_mtf, sup_mtf=sup_mtf, smc=smc,
                rsi=rsi, htf_ema=htf_ema, df_main=df_main,
                symbol=symbol, adx=adx,
                signal_cache=self._signal_cache,
            )
        finally:
            if coin_sp and _sg_set_active_sp:
                _sg_set_active_sp(None)

        if sig is None:
            return None

        # ── Post-processing: price rounding per tick size ──
        if sig:
            rp = lambda v: self._round_price(v, price)
            sig['entry'] = rp(sig['entry'])
            sig['sl']    = rp(sig['sl'])
            sig['tp1']   = rp(sig['tp1'])
            sig['tp2']   = rp(sig['tp2'])

            entry_p = sig['entry']
            risk = abs(entry_p - sig['sl'])
            if risk > 0:
                sig['rr1'] = round(abs(sig['tp1'] - entry_p) / risk, 1)
                sig['rr2'] = round(abs(sig['tp2'] - entry_p) / risk, 1)
            sig['sl_pct'] = round(risk / max(price, 1) * 100, 2)

            # Backward compat fields
            sig['entry_low']  = sig['entry']
            sig['entry_high'] = sig['entry']
            sig['tp']         = sig['tp2']
            sig['tp_max']     = sig['tp2']
            sig['rr']         = sig['rr2']
            sig['rr_max']     = sig['rr2']

        # ── Update signal cache untuk anti-flip ──
        if sig and symbol:
            self._signal_cache[symbol] = {
                'dir'    : sig['direction'],
                'ts'     : time.time(),
                'score'  : sig.get('confluence_score', 0),
                'quality': sig['quality'],
            }

        return sig

    def _fmt(self, price):
        if price >= 1000:
            return f"${price:,.2f}"
        if price >= 1:
            return f"${price:.4f}"
        if price >= 0.01:
            return f"${price:.6f}"
        return f"${price:.8f}"

    # ==================================================================
    # ANALISA LENGKAP
    # ==================================================================
    def analyze_coin(self, symbol, tf='1h', force_fresh=False):
        symbol = clean_symbol(symbol)
        tf = tf if tf in TF_CONFIG else '1h'

        # force_fresh: hapus SEMUA cache → data 100% real-time + zone baru
        if force_fresh:
            self.price_cache.pop(symbol, None)
            sr_key = f"sr_{symbol}_{tf}"
            self.sr_cache.pop(sr_key, None)
            # Hapus zone persistence → paksa deteksi ulang
            zone_key = f"{symbol}_{tf}"
            self._zone_cache.pop(zone_key, None)
            for k in list(self.ohlcv_cache.keys()):
                if symbol.lower() in k.lower():
                    self.ohlcv_cache.pop(k, None)

        # Sequential fetch: HTF → MTF → LTF (Multi-TF)
        df_m = self.get_klines(symbol, tf, False)
        df_h = self.get_klines(symbol, tf, True)
        df_l = self.get_klines(symbol, tf, False, is_lower=True)  # LTF for entry trigger

        # Harga: cek cache dulu, kalau miss ambil dari API
        pd_ = self._cache_get(self.price_cache, symbol)
        if pd_ is None:
            pd_ = self.get_price(symbol)

        if df_m is None and pd_ is None:
            return None, f"'{symbol}' tidak ditemukan. Cek nama coin"
        if df_m is None:
            return None, f"Gagal ambil data candle {symbol}"
        if len(df_m) < 10:
            return None, f"Data {symbol} tidak cukup"

        # Harga: prefer API price, fallback ke candle terakhir
        if pd_:
            price = pd_['price']
            chg24 = pd_['change_24h']
        else:
            price = df_m['close'].iloc[-1]
            chg24 = 0

        atr = self.calc_atr(df_m, min(14, len(df_m) - 1)).iloc[-1]
        if pd.isna(atr) or atr == 0:
            atr = price * 0.02

        rsi = self.calc_rsi(df_m, min(14, len(df_m) - 1)).iloc[-1]
        adx = self.calc_adx(df_m, min(14, len(df_m) - 2))
        if pd.isna(rsi):
            rsi = 50.0

        et, emas, ed = self.analyze_ema_trend(df_m)
        eth, _, edh = ("SIDEWAYS", {}, "")
        if df_h is not None and len(df_h) >= 10:
            eth, _, edh = self.analyze_ema_trend(df_h)

        structure = self.detect_market_structure(df_m)

        ks, kr, res_mtf, sup_mtf = (None, None, [], [])
        if df_h is not None and len(df_h) >= 10:
            ks, kr, res_mtf, sup_mtf = self.detect_key_levels(df_h, df_m, price, symbol, tf)

        # ── MARKET REGIME DETECTION ───────────────────────────────────────────
        regime = self.detect_market_regime(df_m, df_h, price, atr)

        # SMC Full Analysis
        smc = self.build_smc_analysis(df_m, df_h, price, atr, symbol=symbol)

        # Generate narrative
        narrative = self.generate_narrative(smc, price, ks, kr, et, structure)

        # ── CLEAN SIGNAL (Fib + 4H Struktur + Candle konfirmasi) ──
        clean_sig = None
        if _EXTRA_SIGNALS:
            try:
                clean_sig = generate_clean_signal(df_m, df_h, df_l, price, atr, symbol)
                # Log ke debug saja — clean_signal TIDAK dipakai (disabled di baris 3470),
                # sebelumnya info log bikin user kira signal CLEAN GOOD dieksekusi.
                if clean_sig:
                    logger.debug(f"clean_signal {symbol} {clean_sig['direction']} {clean_sig['quality']} (tidak dipakai)")
            except Exception as _ce:
                logger.debug(f"clean_signal error: {_ce}")

        entry = self.generate_entry_signal(price, atr, et, structure, ks, kr, res_mtf, sup_mtf, smc, rsi=rsi, htf_ema=eth, df_main=df_m, symbol=symbol, adx=adx)

        # ── CANDLE PATTERN ENRICHMENT ──────────────────────────
        # Tambahkan info pattern ke semua signal yang ada
        if _CANDLES_AVAILABLE and df_m is not None and len(df_m) >= 3:
            try:
                cp = get_candle_signal(
                    df_m['open'].values[-5:],
                    df_m['high'].values[-5:],
                    df_m['low'].values[-5:],
                    df_m['close'].values[-5:],
                    atr,
                )
                if cp.get('found') and cp['strength'] >= 1:
                    candle_info = format_candle_signal(cp)
                    # Simpan untuk dipakai signal manapun yang aktif
                    _current_candle_pattern = cp
                else:
                    _current_candle_pattern = None
            except Exception:
                _current_candle_pattern = None
        else:
            _current_candle_pattern = None

        # CLEAN SIGNAL — DINONAKTIFKAN DEFINITIF
        # Eksperimen re-enable 2026-04-12: clean_signal generate 1154 signal tapi
        # WR hanya 32% (vs main signal 62.7%). Filter superfisial tidak cukup —
        # scoring Fib 0-100 fundamentally berbeda dari SIGNAL_PARAMS scoring.
        # Butuh rewrite total clean_signal agar pakai _score_direction() dari
        # signal_generator.py sebelum bisa dipakai lagi.
        # clean_sig tidak dipakai.

        # ── Tambahkan candle pattern ke reasons ────────────────
        if entry and _current_candle_pattern and _current_candle_pattern.get('found'):
            cp = _current_candle_pattern
            direction = entry.get('direction', '')
            # Blok kalau pattern berlawanan dan kuat
            if (cp['direction'] == 'BEARISH' and direction == 'LONG' and cp['strength'] >= 3):
                logger.info(f"🕯️ {symbol} LONG diblok candle pattern bearish: {cp['pattern']}")
                entry = None
            elif (cp['direction'] == 'BULLISH' and direction == 'SHORT' and cp['strength'] >= 3):
                logger.info(f"🕯️ {symbol} SHORT diblok candle pattern bullish: {cp['pattern']}")
                entry = None
            elif entry:
                # Tambah info pattern ke reasons
                entry['reasons'].insert(0, format_candle_signal(cp))
                # Bonus score kalau pattern searah
                if cp['direction'] == direction and cp['strength'] >= 2:
                    entry['confluence_score'] = entry.get('confluence_score', 0) + 10

        # BTC condition cek sudah ada di bawah (check_btc_condition)
        # _check_btc_condition dihapus — terlalu agresif, double filter

        # ══════════════════════════════════════════════════════
        # NEWS FILTER — blok trading saat high-impact event
        # ══════════════════════════════════════════════════════
        if entry and _news_filter:
            try:
                news = _news_filter.check()
                if news['block']:
                    logger.info(f"📰 {symbol} diblok news: {news['reason']}")
                    entry = None
                elif news['warning'] and entry:
                    entry['reasons'].append(news['reason'])
            except Exception as _ne:
                logger.debug(f"news filter error: {_ne}")

        # ══════════════════════════════════════════════════════
        # SESSION FILTER — quality cap berdasarkan session
        # Asia/Dead zone = likuiditas rendah, false breakout tinggi
        # ══════════════════════════════════════════════════════
        if entry:
            entry = self._apply_session_quality_cap(entry)

        # ══════════════════════════════════════════════════════
        # PRIORITAS 2: CONFLUENCE GATE
        # Minimum 3 faktor konfirmasi sebelum signal valid
        # ══════════════════════════════════════════════════════
        if entry:
            conf_ok, conf_score, conf_missing = self._check_confluence_gate(
                entry, df_m, df_h, price, atr, rsi
            )
            if not conf_ok:
                logger.info(f"🚫 {symbol} confluence tidak cukup ({conf_score}/3): {conf_missing}")
                entry = None

        # === HTF CEILING/FLOOR CHECK ===
        # LONG di 1H tapi sudah mentok resist 4H? → BLOK
        # SHORT di 1H tapi sudah mentok support 4H? → BLOK
        htf_check = {'blocked': False, 'near_level': False, 'reason': ''}
        if entry and entry['quality'] not in ('WAIT',) and df_h is not None and len(df_h) >= 20:
            atr_h = self._calc_atr_for_sr(df_h, 14)
            if atr_h is None: atr_h = price * 0.02
            htf_check = self.check_htf_ceiling_floor(df_h, price, atr_h, entry['direction'])

            if htf_check['blocked']:
                # HARD BLOCK: signal dibatalkan
                if entry['quality'] in ('MODERATE',):
                    entry = None  # cancel entirely
                elif entry['quality'] in ('GOOD',):
                    entry['quality'] = 'WAIT'
                    entry['reasons'].insert(0, htf_check['reason'])
                elif entry['quality'] in ('IDEAL',):
                    entry['quality'] = 'MODERATE'
                    entry['reasons'].insert(0, htf_check['reason'])
            elif htf_check['near_level']:
                # WARNING: dekat level HTF, downgrade
                if entry['quality'] == 'IDEAL':
                    entry['quality'] = 'GOOD'
                elif entry['quality'] == 'GOOD':
                    entry['quality'] = 'MODERATE'
                entry['reasons'].append(htf_check['reason'])

        # === LTF ENTRY TRIGGER — Multi-TF Confirmation ===
        ltf_trigger = {'triggered': False, 'strength': 0, 'pattern': 'none', 'desc': ''}
        if entry and entry['quality'] not in ('WAIT',) and df_l is not None:
            zone_low = entry.get('sl', 0)  # fallback
            zone_high = price
            if entry['direction'] == 'LONG' and ks:
                zone_low = ks['low']
                zone_high = ks['high']
            elif entry['direction'] == 'SHORT' and kr:
                zone_low = kr['low']
                zone_high = kr['high']

            ltf_trigger = self.check_ltf_trigger(
                df_l, zone_low, zone_high, entry['direction'], atr)

            if not ltf_trigger['triggered']:
                # TIDAK ada konfirmasi LTF → downgrade
                if entry['quality'] == 'IDEAL':
                    entry['quality'] = 'GOOD'
                    entry['reasons'].append("LTF belum konfirmasi — tunggu trigger")
                elif entry['quality'] == 'GOOD':
                    entry['quality'] = 'MODERATE'
                    entry['reasons'].append("LTF belum konfirmasi — entry berisiko")
                elif entry['quality'] == 'MODERATE':
                    # MODERATE tanpa LTF trigger → ganti jadi WAIT
                    entry['quality'] = 'WAIT'
                    entry['reasons'].append("Tunggu LTF trigger sebelum entry")
            else:
                # LTF confirmed — boost confluence
                entry['confluence_score'] = entry.get('confluence_score', 0) + ltf_trigger['strength']
                entry['reasons'].insert(0, ltf_trigger['desc'])
                # Strong LTF trigger bisa UPGRADE quality
                if ltf_trigger['strength'] >= 4 and entry['quality'] == 'GOOD':
                    entry['quality'] = 'IDEAL'
                    entry['reasons'].insert(1, "LTF trigger sangat kuat — upgrade IDEAL")
                elif ltf_trigger['strength'] >= 3 and entry['quality'] == 'MODERATE':
                    entry['quality'] = 'GOOD'
                    entry['reasons'].insert(1, "LTF trigger kuat — upgrade GOOD")

        # === BTC INFO (display only — filter dilakukan di _check_btc_alt_correlation di bawah) ===
        # check_btc_condition hanya untuk info di Telegram, bukan untuk blok signal
        # Filter aktual menggunakan _check_btc_alt_correlation yang lebih akurat (data 4H)
        btc_filter = {'ok_long': True, 'ok_short': True, 'btc_bias': 'NEUTRAL',
                       'btc_change': 0, 'reason': ''}
        if symbol not in ('BTC', 'WBTC'):
            try:
                btc_filter = self.check_btc_condition()
            except Exception:
                pass

        cfg = TF_CONFIG[tf]
        # Market bias: EMA + Structure + HTF + SMC
        b = sum([et in ("STRONG_UP", "UP"), structure == "UPTREND",
                 eth in ("STRONG_UP", "UP")])
        s = sum([et in ("STRONG_DOWN", "DOWN"), structure == "DOWNTREND",
                 eth in ("STRONG_DOWN", "DOWN")])
        # SMC adds weight
        if smc.get('smart_bias') == 'BULLISH' and smc.get('confidence', 0) >= 60:
            b += 1
        if smc.get('smart_bias') == 'BEARISH' and smc.get('confidence', 0) >= 60:
            s += 1
        if b >= 2:
            mb, mi = "BULLISH", "🟢"
        elif b == 1 and s == 0:
            mb, mi = "WEAK BULLISH", "🟨"
        elif s >= 2:
            mb, mi = "BEARISH", "🔴"
        elif s == 1 and b == 0:
            mb, mi = "WEAK BEARISH", "🟧"
        else:
            mb, mi = "SIDEWAYS", "⬜"

        sm = {"UPTREND": "HH/HL ↗️", "DOWNTREND": "LH/LL ↘️", "SIDEWAYS": "Sideways ➡️"}

        # ── EMA 8/21 Cross Detection ─────────────────────────────
        ema_cross = self.detect_ema_cross(df_m)

        # Filter signal berdasarkan EMA bias
        if entry:
            ec_bias = ema_cross.get('bias', 'NEUTRAL')
            ec_dir  = entry.get('direction', '')
            gap     = abs(ema_cross.get('gap_pct', 0))

            if ec_bias == 'BEARISH' and ec_dir == 'LONG' and gap > 0.3:
                logger.info(f"📉 {symbol} LONG diblok EMA8 < EMA21 (gap {gap:.2f}%)")
                entry = None
            elif ec_bias == 'BULLISH' and ec_dir == 'SHORT' and gap > 0.3:
                logger.info(f"📈 {symbol} SHORT diblok EMA8 > EMA21 (gap {gap:.2f}%)")
                entry = None
            elif entry:
                entry.setdefault('reasons', []).insert(0, ema_cross.get('desc', ''))
                # CATATAN: bonus score +8/+15 dihapus — EMA sudah discore di
                # signal_generator._score_direction() lewat score_ema_strong.
                # Double-counting dulu bikin score melewati hard_reject dan
                # merusak analytics WR per score range.

        # ══════════════════════════════════════════════════════
        # STRUCTURE VALIDATION — filter paling kritis
        # Pelajaran dari kesalahan nyata:
        # - BTC lower low → dipaksa LONG → SL
        # - PI bearish pennant → dipaksa LONG → SL
        # - HYPE masih di bawah trendline → LONG prematur
        # - BNB lower low + bearish engulfing → harusnya SHORT
        # ══════════════════════════════════════════════════════
        if entry:
            struct_ok, struct_reason = self._validate_structure_for_entry(
                df_m, df_h, price, atr, entry.get('direction', '')
            )
            if not struct_ok:
                logger.info(f"🏗️ {symbol} diblok struktur: {struct_reason}")
                entry = None

        # ══════════════════════════════════════════════════════
        # DAILY BIAS FILTER (menggantikan Weekly)
        # Hirarki: Daily (konteks) → 4H (trend) → 1H (entry)
        # Weekly terlalu lambat untuk crypto futures — satu candle 7 hari,
        # momentum bisa selesai sebelum weekly bar tutup.
        # Daily hanya downgrade kualitas, TIDAK hard block.
        # ══════════════════════════════════════════════════════
        if entry:
            d_ok, d_reason, d_downgrade = self._check_daily_bias(symbol, entry.get('direction', ''))
            if not d_ok:
                # Daily kontra arah EKSTREM → downgrade ke MODERATE, jangan block total
                if entry.get('quality') == 'GOOD':
                    entry['quality'] = 'MODERATE'
                    entry['reasons'].append(f"Daily kontra: {d_reason}")
                    logger.info(f"📅 {symbol} downgrade daily: {d_reason}")
                elif entry.get('quality') == 'IDEAL':
                    entry['quality'] = 'GOOD'
                    entry['reasons'].append(f"Daily kontra: {d_reason}")
            elif d_downgrade and entry:
                # Daily sedikit melawan → tambahkan catatan saja
                entry['reasons'].append(f"📅 {d_reason}")

        # ══════════════════════════════════════════════════════
        # WHALE SENTIMENT FILTER
        # Kalau whale/big player sedang distribusi (jual besar-besaran),
        # jangan LONG coin itu — ikuti arus, jangan lawan.
        # Data: funding rate + top trader LSR (2 API call, cache 30 menit)
        # ══════════════════════════════════════════════════════
        if entry:
            w_ok, w_reason, w_score = self._check_whale_sentiment(
                symbol, entry.get('direction', '')
            )
            if not w_ok:
                logger.info(f"🐋 {symbol} diblok whale: {w_reason}")
                entry = None
            elif w_reason and entry:
                # Whale sedikit melawan → downgrade quality, jangan blok total
                if entry.get('quality') == 'GOOD':
                    entry['quality'] = 'MODERATE'
                entry['reasons'].append(f"🐋 {w_reason}")

        # ══════════════════════════════════════════════════════
        # BTC-ALT CORRELATION FILTER
        # Pelajaran: TAO di-LONG saat BTC call SHORT → SL
        # Alt selalu korelasi tinggi dengan BTC.
        # Jangan LONG alt kalau BTC bearish, jangan SHORT alt kalau BTC bullish.
        # ══════════════════════════════════════════════════════
        if entry:
            corr_ok, corr_reason = self._check_btc_alt_correlation(
                entry.get('direction', ''), symbol
            )
            if not corr_ok:
                logger.info(f"₿ {symbol} diblok BTC korelasi: {corr_reason}")
                entry = None

        # ══════════════════════════════════════════════════════
        # VOLATILITY REGIME FILTER
        # Saat coin sedang crash/pump ekstrem (>8% dalam 24h),
        # jangan masuk — harga tidak stabil, SL/TP tidak reliable.
        # Exception: SHORT saat crash besar OK (follow momentum)
        # ══════════════════════════════════════════════════════
        if entry and abs(chg24) > 12.0:
            direction_e = entry.get('direction', '')
            if direction_e == 'LONG' and chg24 < -12:
                logger.info(f"⚡ {symbol} LONG diblok — crash {chg24:.1f}% (volatilitas ekstrem)")
                entry = None
            elif direction_e == 'SHORT' and chg24 > 12:
                logger.info(f"⚡ {symbol} SHORT diblok — pump {chg24:.1f}% (volatilitas ekstrem)")
                entry = None

        # ══════════════════════════════════════════════════════
        # MARKET REGIME ADAPTIVE FILTER
        # Sesuaikan ambang kualitas berdasarkan kondisi market
        # ══════════════════════════════════════════════════════
        if entry and regime:
            rg = regime['regime']
            score_adj = regime.get('score_adj', 0)

            # LOW_LIQUIDITY: volume sangat kecil → butuh confluence lebih tinggi
            if rg == 'LOW_LIQUIDITY':
                if entry.get('quality') == 'MODERATE':
                    entry = None  # skip moderate di volume rendah
                    logger.info(f"📉 {symbol} MODERATE diblok — volume rendah: {regime['desc']}")
                elif entry.get('quality') == 'GOOD' and entry.get('confluence_score', 0) < 25:
                    entry['quality'] = 'MODERATE'
                    entry['reasons'].append(f"Volume rendah — downgrade ke MODERATE")

            # HIGH_VOL: SL lebih lebar, tambah catatan
            elif rg == 'HIGH_VOL' and entry:
                entry['reasons'].insert(0, f"⚡ {regime['desc']}")

            # TRENDING: signal melawan trend utama = lebih ketat
            elif rg == 'TRENDING_BULL' and entry and entry.get('direction') == 'SHORT':
                if entry.get('quality') == 'MODERATE':
                    entry = None
                    logger.info(f"📈 {symbol} SHORT MODERATE diblok — trending bull")
                elif entry.get('quality') == 'GOOD':
                    entry['quality'] = 'MODERATE'
                    entry['reasons'].insert(0, f"Counter-trend SHORT di TRENDING BULL — downgrade")

            elif rg == 'TRENDING_BEAR' and entry and entry.get('direction') == 'LONG':
                if entry.get('quality') == 'MODERATE':
                    entry = None
                    logger.info(f"📉 {symbol} LONG MODERATE diblok — trending bear")
                elif entry.get('quality') == 'GOOD':
                    entry['quality'] = 'MODERATE'
                    entry['reasons'].insert(0, f"Counter-trend LONG di TRENDING BEAR — downgrade")

        # ── Inject learning metadata ke signal ────────────────
        # Dipakai learning_engine saat log_entry → bisa analisa pola
        if entry:
            entry['_rsi']       = round(rsi, 1)
            entry['_adx']       = round(adx, 1)
            entry['_ema_trend'] = et
            entry['_structure'] = structure
            entry['_smc']       = smc      # full SMC dict
            entry['_btc_state'] = (
                'BULLISH' if btc_filter.get('ok_long') and not btc_filter.get('ok_short')
                else 'BEARISH' if btc_filter.get('ok_short') and not btc_filter.get('ok_long')
                else 'NEUTRAL'
            )

        # ── LIMIT SIGNAL — setup pending (harga belum di level, tapi menuju ke sana) ──
        # Kalau tidak ada entry signal, generate_limit_signal cari level terdekat
        # yang akan diretest. Ini yang terlihat di manual scan sebagai "entry limitnya".
        limit_entry = None
        if _LIMIT_SIGNAL_AVAILABLE:
            try:
                limit_entry = _gen_limit_signal(
                    price, atr, et, structure,
                    ks, kr, res_mtf, sup_mtf, smc,
                    df_main=df_m, symbol=symbol, htf_ema=eth,
                    signal_cache=self._signal_cache,
                )
            except Exception as _le:
                logger.debug(f"limit_signal error {symbol}: {_le}")

        # ── DIAGNOSTIC LOG: tampilkan hasil analisa final per coin ──
        # Tujuan: visibility filter mana yang paling sering block signal.
        # Format: SYM tf | q=QUALITY s=SCORE d=DIR k=KILLS sess=SESSION adx=ADX
        # Di-skip saat backtest mode (ribuan candle per coin bikin log meledak).
        try:
            import signal_generator as _sg
            if not getattr(_sg, '_IS_BACKTEST', False):
                if entry:
                    _q = entry.get('quality', '?')
                    _s = entry.get('confluence_score', 0)
                    _d = entry.get('direction', '?')
                    _k = entry.get('kill_count', 0)
                    _sess = entry.get('session', '?')
                    logger.info(
                        f"📊 {symbol} {tf} | q={_q} s={_s} d={_d} k={_k} sess={_sess} adx={round(adx,1)}"
                    )
                else:
                    logger.info(
                        f"📊 {symbol} {tf} | q=None (tidak ada signal) adx={round(adx,1)} ema={et} htf={eth}"
                    )
        except Exception:
            pass

        return {
            'symbol': symbol, 'exchange': 'Binance',
            'price': price, 'change_24h': chg24, 'atr': atr, 'rsi': rsi, 'adx': round(adx, 1),
            'btc_filter': btc_filter,
            'tf': tf, 'tf_label': cfg['label'], 'htf_label': cfg['higher_label'],
            'ema_trend': et, 'ema_desc': ed, 'ema_desc_h': edh, 'emas': emas,
            'ema_cross': ema_cross,
            'structure': structure, 'structure_label': sm.get(structure, structure),
            'market_bias': mb, 'market_bias_icon': mi,
            'key_support': ks, 'key_resistance': kr,
            'signal': entry, 'limit_signal': limit_entry,
            'timestamp': datetime.now().isoformat(),
            'smc': smc, 'narrative': narrative,
            'ltf_trigger': ltf_trigger,
            'htf_check': htf_check,
            'regime': regime,
            'direction': entry['direction'] if entry else None,
            'entry_price': round(price, 8), 'label': f"{mi} {mb}",
        }, None

    # ==================================================================
    # DYNAMIC COIN LIST — ambil top coins by market cap
    # ==================================================================
    def get_top_coins(self, limit=100):
        """
        Ambil top coins by volume dari Binance Futures.

        PERBAIKAN: Dulu pakai CryptoCompare (hanya return 5 coin).
        Sekarang pakai Binance Futures 24hr ticker — 1 call, data lengkap,
        diurutkan berdasarkan volume USDT tertinggi (paling likuid = paling
        banyak ditrading institusi).
        """
        cache_key = f"top_coins_{limit}"
        cached = self._cache_get(self.price_cache, cache_key)
        if cached is not None:
            return cached

        # Binance Futures 24hr ticker — return semua pairs sekaligus
        all_tickers = self._http_get(
            "https://fapi.binance.com/fapi/v1/ticker/24hr")

        stablecoins = {
            'USDT','USDC','BUSD','DAI','TUSD','FDUSD','USDD','USDP',
            'WBTC','WETH','STETH','WSTETH','RETH','CBETH','FRAX','LUSD',
            'GUSD','USDE','WBNB','BTCB','LBTC','USDX',
        }

        coins = []
        if all_tickers and isinstance(all_tickers, list):
            # Filter USDT pairs saja, buang stablecoin dan leveraged token
            valid = []
            for t in all_tickers:
                sym_pair = t.get('symbol', '')
                if not sym_pair.endswith('USDT'):
                    continue
                sym = sym_pair[:-4]
                if sym in stablecoins:
                    continue
                # Buang leveraged token (UP/DOWN/BULL/BEAR suffix)
                if any(sym.endswith(s) for s in ('UP','DOWN','BULL','BEAR','3L','3S')):
                    continue
                # Buang commodity/index futures (bukan crypto)
                if sym in ('XAU','XAG','CL','BZ','GC','SI','NG'):
                    continue
                # Buang simbol terlalu pendek (kemungkinan bukan crypto normal)
                if len(sym) < 2:
                    continue
                vol = float(t.get('quoteVolume', 0) or 0)
                if vol < 1_000_000:   # Min $1 juta volume 24h
                    continue
                valid.append((sym, vol))

            # Urutkan volume tertinggi dulu (paling likuid = paling relevan)
            valid.sort(key=lambda x: x[1], reverse=True)
            coins = [sym for sym, _ in valid[:limit]]

        if coins:
            self.price_cache[cache_key] = (coins, time.time())
            logger.info(f"Top {len(coins)} coins dari Binance Futures (by volume)")
        else:
            # Fallback ke SCAN_POOL kalau Binance tidak tersedia
            logger.warning("get_top_coins: Binance gagal, fallback SCAN_POOL")
            coins = list(SCAN_POOL) if isinstance(SCAN_POOL, set) else SCAN_POOL

        return coins

    # ==================================================================
    # QUICK SCREENING — fase 1: filter cepat pakai harga saja
    # ==================================================================
    def quick_screen(self, coins):
        """
        Screening cepat menggunakan Binance Futures 24hr ticker.

        PERBAIKAN: API lama (CryptoCompare) tidak punya data untuk sebagian
        besar Binance Futures coin, sehingga hanya 5 dari 100 coin yang lolos.
        Sekarang pakai Binance /fapi/v1/ticker/24hr (tanpa parameter) yang
        return semua pairs Binance Futures sekaligus dalam 1 API call.

        Filter:
          - Volume 24h >= $5 juta (coin cukup likuid untuk di-trade)
          - Price > 0
        """
        # 1 call Binance untuk semua pairs sekaligus
        all_tickers = self._http_get(
            "https://fapi.binance.com/fapi/v1/ticker/24hr")

        if not all_tickers or not isinstance(all_tickers, list):
            # Fallback: loloskan semua coin kalau API gagal
            logger.warning("quick_screen: Binance ticker gagal, loloskan semua coin")
            return coins

        # Buat lookup dict: symbol → ticker data
        ticker_map = {}
        for t in all_tickers:
            sym_pair = t.get('symbol', '')
            if sym_pair.endswith('USDT'):
                sym = sym_pair[:-4]   # Buang 'USDT'
                ticker_map[sym] = t

        interesting = []
        coin_set = set(coins)
        min_volume = 5_000_000   # Minimum $5 juta volume 24h

        for sym in coins:
            t = ticker_map.get(sym)
            if not t:
                continue
            price = float(t.get('lastPrice', 0) or 0)
            vol   = float(t.get('quoteVolume', 0) or 0)   # volume dalam USDT
            chg   = float(t.get('priceChangePercent', 0) or 0)

            if price <= 0 or vol < min_volume:
                continue

            # Cache harga untuk analyze_coin — tidak perlu fetch lagi
            self._cache_set(self.price_cache, sym, {
                'price'     : price,
                'change_24h': round(chg, 2),
                'high_24h'  : float(t.get('highPrice', 0) or 0),
                'low_24h'   : float(t.get('lowPrice', 0) or 0),
                'volume_24h': vol,
                'market_cap': 0,
            })
            interesting.append(sym)

        logger.info(f"Quick screen: {len(interesting)}/{len(coins)} coin layak analisa (Binance 24h ticker)")
        return interesting

    # ==================================================================
    # DAILY SCAN — full pipeline
    # ==================================================================
    def scan_top_signals(self, pool=None, max_signals=5, scan_delay=3,
                         progress_callback=None):
        """
        Full scan pipeline:
        1. Ambil top coins by market cap
        2. Quick screen (filter volume & pergerakan)
        3. Full analyze coin yang lolos
        4. Rank & return top signals
        """
        # Fase 1: ambil coin list
        if pool is None:
            pool = self.get_top_coins(100)

        if progress_callback:
            progress_callback(f"📡 Fase 1: {len(pool)} coin dimuat")

        # Fase 2: quick screen (batch — hemat API call)
        screened = self.quick_screen(pool)

        if progress_callback:
            progress_callback(f"📊 Fase 2: {len(screened)} coin lolos filter")

        # Fase 3: full analisa
        qs = {'IDEAL': 4, 'GOOD': 3, 'MODERATE': 2, 'WAIT': 1}
        mq = qs.get(DAILY_SIGNAL.get('min_quality', 'MODERATE'), 2)
        cands = []

        for idx, sym in enumerate(screened, 1):
            try:
                if progress_callback and idx % 10 == 0:
                    progress_callback(
                        f"🔍 Fase 3: Analisa {idx}/{len(screened)}..."
                        f" ({len(cands)} sinyal ditemukan)")

                result, err = self.analyze_coin(sym, '1h')
                if err:
                    continue
                sig = result.get('signal')

                if sig:
                    q = qs.get(sig['quality'], 0)
                    if q < mq:
                        continue
                    # Ranking by confluence score (sudah menghitung semua faktor)
                    conf_score = sig.get('confluence_score', 0)
                    kill_count = sig.get('kill_count', 0)
                    rank = q * 10 + conf_score * 2 - kill_count * 5
                    if sig.get('rr2', 0) >= 3:
                        rank += 5
                    if sig['quality'] != 'WAIT':
                        rank += 5
                    cands.append({
                        'symbol': sym, 'result': result,
                        'signal': sig, 'rank_score': rank
                    })
                else:
                    # Tidak ada entry signal — cek limit signal (setup pending)
                    # Ini yang muncul di manual scan sebagai "entry limitnya"
                    lsig = result.get('limit_signal')
                    if not lsig:
                        continue
                    # Limit signal dimasukkan dengan rank lebih rendah dari MODERATE
                    conf_score = lsig.get('confidence', lsig.get('confluence_score', 50))
                    rank = conf_score  # maks ~95, selalu di bawah MODERATE (rank ~22+)
                    cands.append({
                        'symbol': sym, 'result': result,
                        'signal': lsig, 'rank_score': rank
                    })
            except:
                pass
            if idx < len(screened):
                time.sleep(scan_delay)

        cands.sort(key=lambda x: x['rank_score'], reverse=True)
        return cands[:max_signals]

    # ==================================================================
    # BACKWARD COMPAT
    # ==================================================================
    def scan_for_signals(self, pair_str):
        sym = pair_str.replace('/USDT', '').replace('USDT', '')
        r, e = self.analyze_coin(sym, '1h')
        if e or not r.get('signal'):
            return None
        s = r['signal']
        return {
            'pair': pair_str, 'direction': s['direction'],
            'entry_price': r['entry_price'], 'atr': r['atr'],
            'rsi': r['rsi'], 'timestamp': r['timestamp'],
            'reason': "\n".join(s['reasons'][:3])
        }

    def scan_all_pairs(self):
        sigs = []
        for p in TRADING_PAIRS:
            s = self.scan_for_signals(p)
            if s:
                sigs.append(s)
            time.sleep(1)
        return sigs


    def _check_btc_condition(self, direction: str) -> tuple:
        """Cek kondisi BTC. Return (ok, reason)."""
        try:
            import numpy as np
            df4h = self.get_klines('BTC', '4h', is_higher=False)
            if df4h is None or len(df4h) < 20:
                return True, ''
            closes = df4h['close'].values.astype(float)
            highs  = df4h['high'].values.astype(float)
            lows   = df4h['low'].values.astype(float)
            opens  = df4h['open'].values.astype(float)
            p = closes[-1]; o = opens[-1]
            ema = closes[0]
            for v in closes:
                ema = v*(2/21) + ema*(19/21)
            d = np.diff(closes[-15:])
            rsi = 100 - 100/(1 + np.mean(np.where(d>0,d,0))/max(np.mean(np.where(d<0,-d,0)),0.001))
            atr_pct = np.mean(highs[-14:]-lows[-14:]) / max(p,0.001) * 100
            chg = (p - o) / max(o, 0.001) * 100
            if direction == 'LONG':
                if chg < -5: return False, f"BTC dump ekstrem {chg:.1f}% — skip LONG"  # hanya blok kalau dump >5%
                if p < ema*0.95 and rsi < 30: return False, f"BTC bearish ekstrem RSI {rsi:.0f}"  # threshold lebih ketat
                pass  # ATR check dihapus — terlalu sering blok
                if p < ema*0.98: return True, f"BTC di bawah EMA — hati-hati LONG"
                return True, ''
            else:
                if chg > 5: return False, f"BTC pump ekstrem {chg:.1f}% — skip SHORT"
                if p > ema*1.05 and rsi > 70: return False, f"BTC bullish ekstrem RSI {rsi:.0f}"
                pass  # ATR check dihapus — terlalu sering blok
                return True, ''
        except Exception:
            return True, ''

    def _check_whale_sentiment(self, symbol: str, direction: str) -> tuple:
        """
        Cek sentimen whale untuk satu coin — versi ringan (2 API call saja).
        Pakai cache 30 menit agar tidak memperlambat bot.

        Logika:
          Funding rate + Long/Short Ratio top trader → skor -100 sampai +100

          Skor >= +40  → whale akumulasi → blok SHORT
          Skor <= -40  → whale distribusi → blok LONG
          Skor 20-39   → whale sedikit bullish → downgrade SHORT ke MODERATE
          Skor -39 - -20 → whale sedikit bearish → downgrade LONG ke MODERATE

        Return: (ok: bool, reason: str, score: int)
        """
        import time as _time
        sym = symbol.upper().replace('/USDT', '').replace('USDT', '') + 'USDT'

        # Cek cache dulu
        cached = self._whale_cache.get(symbol, {})
        if cached and (_time.time() - cached.get('ts', 0)) < self._WHALE_TTL:
            score = cached['score']
            bias  = cached['bias']
        else:
            # Ambil data dari Binance — hanya 2 endpoint
            score = 0
            bias  = 'NEUTRAL'
            try:
                # 1. Funding rate
                r_fund = self._http_get(
                    "https://fapi.binance.com/fapi/v1/premiumIndex",
                    params={'symbol': sym}
                )
                if r_fund and isinstance(r_fund, dict):
                    fr = float(r_fund.get('lastFundingRate', 0)) * 100
                    if   fr >  0.05: score -= 30   # crowd long berat → bearish signal
                    elif fr >  0.01: score -= 10
                    elif fr < -0.05: score += 30   # crowd short berat → bullish signal
                    elif fr < -0.01: score += 10

                # 2. Top trader LSR
                r_lsr = self._http_get(
                    "https://fapi.binance.com/fapi/v1/topLongShortPositionRatio",
                    params={'symbol': sym, 'period': '1h', 'limit': '1'}
                )
                if r_lsr and isinstance(r_lsr, list) and r_lsr:
                    lsr = float(r_lsr[0].get('longShortRatio', 1))
                    if   lsr > 1.3: score -= 20   # top trader mayoritas long → bearish
                    elif lsr > 1.1: score -= 10
                    elif lsr < 0.77: score += 20  # top trader mayoritas short → bullish
                    elif lsr < 0.91: score += 10

                # Tentukan bias
                if   score >= 30:  bias = 'BULL'
                elif score <= -30: bias = 'BEAR'
                elif score >= 15:  bias = 'SLIGHT_BULL'
                elif score <= -15: bias = 'SLIGHT_BEAR'
                else:              bias = 'NEUTRAL'

                # Simpan cache
                self._whale_cache[symbol] = {'score': score, 'bias': bias, 'ts': _time.time()}

            except Exception as e:
                logger.debug(f"whale check {symbol}: {e}")
                return True, '', 0   # error → allow, jangan blok karena API issue

        # Evaluasi berdasarkan arah signal
        if direction == 'LONG':
            if bias == 'BEAR':
                return False, f"Whale distribusi (score {score}) — LONG berisiko", score
            if bias == 'SLIGHT_BEAR':
                return True, f"Whale sedikit bearish (score {score}) — pertimbangkan", score
        elif direction == 'SHORT':
            if bias == 'BULL':
                return False, f"Whale akumulasi (score {score}) — SHORT berisiko", score
            if bias == 'SLIGHT_BULL':
                return True, f"Whale sedikit bullish (score {score}) — pertimbangkan", score

        return True, '', score

    def _apply_session_quality_cap(self, entry: dict) -> dict:
        """
        Cap quality signal berdasarkan session trading.

        London/NY/Overlap (07:00-21:00 UTC):
          → Tidak ada perubahan. Institutional participation aktif.

        Asia session (00:00-07:00 UTC):
          → IDEAL→GOOD, GOOD→MODERATE
          → False breakout rate 40-45% vs 20-25% di London/NY

        Dead zone (21:00-00:00 UTC):
          → IDEAL→MODERATE, GOOD→MODERATE
          → Liquidity paling tipis, market maker bisa gerakkan harga dengan mudah

        Exception: kalau signal punya 0 kills DAN score >= 25 → tetap GOOD di Asia.
        Ini setup high-confluence yang jarang, cukup reliable meski di Asia session.
        """
        try:
            from session_filter import get_current_session
            from datetime import datetime, timezone
            sess = get_current_session(datetime.now(timezone.utc))

            if not sess.get('restrict', False):
                # London/NY/Overlap — tambahkan info session saja
                entry.setdefault('reasons', []).append(f"⏰ {sess['desc']}")
                return entry

            q     = entry.get('quality', '')
            score = entry.get('confluence_score', 0)
            kills = entry.get('kill_count', 99)

            # Exception: high-confluence 0-kill di Asia → tetap GOOD
            high_conf_exception = (kills == 0 and score >= 25)

            sess_label = sess['desc']

            if sess['session'] == 'DEAD':
                # Dead zone paling ketat
                if q == 'IDEAL':
                    entry['quality'] = 'MODERATE'
                    entry['reasons'].insert(0, f"⏰ Dead zone — cap MODERATE")
                elif q == 'GOOD' and not high_conf_exception:
                    entry['quality'] = 'MODERATE'
                    entry['reasons'].insert(0, f"⏰ Dead zone — cap MODERATE")
                elif q == 'GOOD' and high_conf_exception:
                    entry['reasons'].insert(0, f"⏰ Dead zone — GOOD dipertahankan (score {score}, 0 kills)")
                else:
                    entry['reasons'].append(f"⏰ {sess_label}")

            else:  # ASIA
                if q == 'IDEAL':
                    entry['quality'] = 'GOOD'
                    entry['reasons'].insert(0, f"⏰ Asia session — IDEAL→GOOD (false breakout rate tinggi)")
                elif q == 'GOOD' and not high_conf_exception:
                    entry['quality'] = 'MODERATE'
                    entry['reasons'].insert(0, f"⏰ Asia session — GOOD→MODERATE")
                elif q == 'GOOD' and high_conf_exception:
                    entry['reasons'].insert(0, f"⏰ Asia session — GOOD dipertahankan (score {score}, 0 kills)")
                else:
                    entry['reasons'].append(f"⏰ {sess_label}")

        except Exception as e:
            logger.debug(f"session filter error: {e}")

        return entry

    def _check_trading_session(self) -> tuple:
        """Trade hanya di EU/US session (WIB). Return (ok, session_name)."""
        from datetime import datetime, timezone, timedelta
        wib = timezone(timedelta(hours=7))
        now = datetime.now(wib)
        t   = now.hour + now.minute/60
        if 14.0 <= t < 18.0:   return True, "EU Session 14-18 WIB"
        if 19.5 <= t < 23.5:   return True, "US Session 19:30-23:30 WIB"
        if 9.0  <= t < 11.0:   return True, "Asia Close 09-11 WIB"
        return False, f"Di luar session ({now.hour:02d}:{now.minute:02d} WIB)"

    def _check_confluence_gate(self, entry, df_1h, df_4h, price, atr, rsi_val) -> tuple:
        """Min 2/5 faktor. Return (ok, score, missing_str)."""
        import numpy as np
        MIN = 2; score = 0; missing = []  # 2/5 faktor — Fib zone + RSI/Volume/Candle/4H
        direction = entry.get('direction',''); is_long = direction == 'LONG'
        try:
            closes = df_1h['close'].values.astype(float)
            highs  = df_1h['high'].values.astype(float)
            lows   = df_1h['low'].values.astype(float)
            opens  = df_1h['open'].values.astype(float)
            vols   = df_1h['volume'].values.astype(float) if 'volume' in df_1h.columns else None
            # 1. Fib zone
            sh=float(np.max(highs[-50:])); sl=float(np.min(lows[-50:])); rng=sh-sl
            if rng>0:
                f50=sh-0.5*rng; f786=sh-0.786*rng
                if is_long: ok_f=f786<=price<=f50*1.02
                else: ok_f=(sl+0.5*rng)*0.98<=price<=(sl+0.786*rng)
                if ok_f: score+=1
                else: missing.append("Fib")
            else: missing.append("Fib")
            # 2. Volume
            if vols is not None and len(vols)>=10:
                if vols[-1]>=np.mean(vols[-20:-1])*1.2: score+=1
                else: missing.append("Volume")
            else: score+=1
            # 3. RSI
            rv=rsi_val if rsi_val and rsi_val>0 else 50
            if (is_long and rv<=45) or (not is_long and rv>=55): score+=1
            else: missing.append(f"RSI {rv:.0f}")
            # 4. Candle
            c0,o0,h0,l0=closes[-1],opens[-1],highs[-1],lows[-1]
            c1,o1=closes[-2],opens[-2]
            body=abs(c0-o0); r0=max(h0-l0,0.000001)
            up=h0-max(c0,o0); dw=min(c0,o0)-l0
            if is_long: ok_c=(dw>=body*2 and up<=body*0.5) or (c0>o0 and c1<o1 and c0>=o1 and o0<=c1) or (dw>=r0*0.6 and body<=r0*0.3)
            else: ok_c=(up>=body*2 and dw<=body*0.5) or (c0<o0 and c1>o1 and c0<=o1 and o0>=c1) or (up>=r0*0.6 and body<=r0*0.3)
            if ok_c: score+=1
            else: missing.append("Candle")
            # 5. 4H structure
            if df_4h is not None and len(df_4h)>=3:
                c4=df_4h['close'].values.astype(float); o4=df_4h['open'].values.astype(float)
                bull4=sum(1 for i in range(1,4) if c4[-i]>o4[-i])
                bear4=sum(1 for i in range(1,4) if c4[-i]<o4[-i])
                if (is_long and bull4>=2) or (not is_long and bear4>=2): score+=1
                else: missing.append("4H")
            else: missing.append("4H")
        except Exception:
            return True, MIN, ''
        return score>=MIN, score, "|".join(missing)



    def _check_weekly_bias(self, symbol: str, direction: str) -> tuple:
        """
        Cek weekly trend sebelum entry.
        Weekly adalah bias terbesar — melawan weekly trend hampir selalu berakhir SL.

        Logic:
          Weekly DOWNTREND (struktur LH/LL) → blok LONG
          Weekly UPTREND  (struktur HH/HL) → blok SHORT
          Weekly SIDEWAYS → allow semua arah

        Pengecualian: kalau weekly EMA21 baru saja ditest dan ada konfirmasi reversal
        (weekly CHoCH), allow — tapi hanya MODERATE, bukan GOOD.
        (Konfirmasi ini sudah ditangani oleh filter lain — di sini hanya hard block.)

        Return: (ok: bool, reason: str)
        """
        try:
            df_w = self.get_klines(symbol, '1w', is_higher=False)
            if df_w is None or len(df_w) < 10:
                return True, ''  # data tidak cukup → allow

            closes = df_w['close'].values.astype(float)
            highs  = df_w['high'].values.astype(float)
            lows   = df_w['low'].values.astype(float)

            struct_w = self.detect_market_structure(df_w, window=2)

            # EMA21 weekly
            ema21 = closes[0]
            for v in closes:
                ema21 = v * (2 / 22) + ema21 * (20 / 22)

            price     = closes[-1]
            below_ema = price < ema21 * 0.98  # lebih dari 2% di bawah EMA21
            above_ema = price > ema21 * 1.02  # lebih dari 2% di atas EMA21

            # Weekly DOWNTREND: blok semua LONG
            if direction == 'LONG':
                if struct_w == 'DOWNTREND':
                    return False, f"Weekly DOWNTREND (LH/LL) — terlalu berisiko LONG"
                if struct_w == 'SIDEWAYS' and below_ema:
                    return False, f"Weekly sideways tapi harga jauh di bawah EMA21 — bias bearish"

            # Weekly UPTREND: blok semua SHORT
            elif direction == 'SHORT':
                if struct_w == 'UPTREND':
                    return False, f"Weekly UPTREND (HH/HL) — terlalu berisiko SHORT"
                if struct_w == 'SIDEWAYS' and above_ema:
                    return False, f"Weekly sideways tapi harga jauh di atas EMA21 — bias bullish"

            return True, ''

        except Exception as e:
            logger.debug(f"weekly bias check error {symbol}: {e}")
            return True, ''  # error → allow

    def _check_daily_bias(self, symbol: str, direction: str) -> tuple:
        """
        Cek daily trend sebagai konteks untuk entry.
        Hirarki: Daily (konteks) → 4H (trend aktif) → 1H (entry konfirmasi)

        Return: (ok: bool, reason: str, soft_downgrade: bool)
          ok=False        → daily kontra arah ekstrem → downgrade quality
          ok=True, soft   → daily sedikit melawan → tambah catatan
          ok=True, !soft  → daily searah atau sideways → lanjut normal
        """
        try:
            df_d = self.get_klines(symbol, '1d', is_higher=False)
            if df_d is None or len(df_d) < 10:
                return True, '', False  # data tidak cukup → allow

            closes = df_d['close'].values.astype(float)
            highs  = df_d['high'].values.astype(float)
            lows   = df_d['low'].values.astype(float)

            # EMA21 daily — trend menengah
            ema21 = float(closes[0])
            for v in closes:
                ema21 = v * (2 / 22) + ema21 * (20 / 22)

            # EMA9 daily — trend pendek
            ema9 = float(closes[0])
            for v in closes:
                ema9 = v * (2 / 10) + ema9 * (8 / 10)

            price = float(closes[-1])
            struct_d = self.detect_market_structure(df_d, window=2)

            # Ukur kekuatan: harga jauh di bawah/atas EMA21 (>5%) = bias kuat
            ema_gap_pct = (price - ema21) / ema21 * 100

            # LONG check
            if direction == 'LONG':
                # Ekstrem bearish daily: downtrend struktur + di bawah EMA21 jauh
                if struct_d == 'DOWNTREND' and ema_gap_pct < -5:
                    return False, f"Daily DOWNTREND kuat, harga {ema_gap_pct:.1f}% di bawah EMA21", False
                # Sedikit bearish: hanya di bawah EMA21
                if price < ema21 and ema9 < ema21:
                    return True, f"Daily bias bearish (harga & EMA9 di bawah EMA21 daily)", True

            # SHORT check
            elif direction == 'SHORT':
                # Ekstrem bullish daily: uptrend struktur + di atas EMA21 jauh
                if struct_d == 'UPTREND' and ema_gap_pct > 5:
                    return False, f"Daily UPTREND kuat, harga {ema_gap_pct:.1f}% di atas EMA21", False
                # Sedikit bullish: hanya di atas EMA21
                if price > ema21 and ema9 > ema21:
                    return True, f"Daily bias bullish (harga & EMA9 di atas EMA21 daily)", True

            return True, '', False

        except Exception as e:
            logger.debug(f"daily bias check error {symbol}: {e}")
            return True, '', False  # error → allow

    def _check_btc_alt_correlation(self, direction: str, symbol: str) -> tuple:
        """
        Cek korelasi BTC sebelum trade altcoin.
        Rule: jangan LONG alt kalau BTC bearish, jangan SHORT alt kalau BTC bullish.

        Ini mencegah situasi seperti:
        - BTC signal SHORT tapi TAO di-LONG → TAO ikut dump
        - BTC recovery tapi alt masih di-SHORT

        Return: (ok: bool, reason: str)
        """
        # BTC tidak perlu cek dirinya sendiri
        if symbol.upper() in ('BTC', 'BTCUSDT'):
            return True, ''

        try:
            import numpy as np

            # Ambil data BTC 4H untuk cek bias
            df_btc_4h = self.get_klines('BTC', '4h', is_higher=False)
            if df_btc_4h is None or len(df_btc_4h) < 20:
                return True, ''  # tidak bisa cek → allow

            closes = df_btc_4h['close'].values.astype(float)
            highs  = df_btc_4h['high'].values.astype(float)
            lows   = df_btc_4h['low'].values.astype(float)

            # EMA 21 BTC 4H
            ema21 = closes[0]
            for v in closes:
                ema21 = v * (2/22) + ema21 * (20/22)

            btc_price  = closes[-1]
            btc_open   = closes[-4]   # harga 4 candle lalu (shift 4H)
            btc_change = (btc_price - btc_open) / max(btc_open, 0.001) * 100

            # Struktur BTC
            btc_struct = self.detect_market_structure(df_btc_4h, window=3)

            # RSI BTC
            delta  = np.diff(closes[-15:])
            gain   = np.where(delta > 0, delta, 0)
            loss   = np.where(delta < 0, -delta, 0)
            ag     = np.mean(gain[-14:]) if len(gain) >= 14 else 50
            al     = np.mean(loss[-14:]) if len(loss) >= 14 else 50
            rsi    = 100 - 100 / (1 + ag / max(al, 0.001))

            btc_bearish = btc_struct == 'DOWNTREND' or (btc_price < ema21 * 0.99 and rsi < 45)
            btc_bullish = btc_struct == 'UPTREND'   or (btc_price > ema21 * 1.01 and rsi > 55)

            # BTC drop tajam dalam 4 candle (16 jam)
            btc_dumping = btc_change < -4.0
            btc_pumping = btc_change > 4.0

            if direction == 'LONG':
                if btc_dumping:
                    return False, f"BTC dump {btc_change:.1f}% dalam 16 jam — alt ikut turun"
                if btc_bearish:
                    reason = f"BTC 4H DOWNTREND" if btc_struct == 'DOWNTREND' else f"BTC bearish (harga < EMA21, RSI {rsi:.0f})"
                    return False, f"{reason} — jangan LONG alt"
            else:  # SHORT
                if btc_pumping:
                    return False, f"BTC pump {btc_change:.1f}% dalam 16 jam — alt ikut naik"
                if btc_bullish:
                    reason = f"BTC 4H UPTREND" if btc_struct == 'UPTREND' else f"BTC bullish (harga > EMA21, RSI {rsi:.0f})"
                    return False, f"{reason} — jangan SHORT alt"

            return True, ''

        except Exception as e:
            return True, ''

    def detect_market_regime(self, df_main, df_higher, price: float, atr: float) -> dict:
        """
        Deteksi regime market saat ini.

        Regime menentukan STRATEGI dan PARAMETER yang tepat:
          TRENDING_BULL  → ikuti trend naik, jangan SHORT kecuali sangat kuat
          TRENDING_BEAR  → ikuti trend turun, jangan LONG kecuali sangat kuat
          RANGING        → trade S/R ke S/R, jangan chase breakout
          HIGH_VOL       → volatilitas tinggi, SL lebih lebar, ukuran posisi kecil
          LOW_LIQUIDITY  → volume rendah, sering false signal, skip kecuali sangat kuat

        Return dict:
          regime        : nama regime
          atr_mult      : multiplier untuk SL (1.0 = normal, 1.5 = lebih lebar)
          score_adj     : tambahan/kurang ke score minimum (0 = normal, +5 = lebih ketat)
          prefer_long   : bool
          prefer_short  : bool
          desc          : deskripsi singkat
        """
        default = {
            'regime': 'NORMAL', 'atr_mult': 1.0, 'score_adj': 0,
            'prefer_long': True, 'prefer_short': True, 'desc': 'Kondisi normal'
        }
        try:
            if df_main is None or len(df_main) < 30:
                return default

            closes = df_main['close'].values.astype(float)
            highs  = df_main['high'].values.astype(float)
            lows   = df_main['low'].values.astype(float)
            vols   = df_main['volume'].values.astype(float) if 'volume' in df_main.columns else None

            # ── ATR relatif — bandingkan ATR sekarang vs rata-rata 30 candle ──
            recent_atr   = atr
            candle_ranges = highs[-30:] - lows[-30:]
            avg_range     = float(np.mean(candle_ranges))
            vol_regime    = recent_atr / max(avg_range, price * 0.001)

            # ── ADX manual ────────────────────────────────────────────────────
            adx_val = 20.0
            try:
                if len(highs) >= 28:
                    up_moves   = np.diff(highs[-28:])
                    down_moves = -np.diff(lows[-28:])
                    plus_dm    = np.where((up_moves > down_moves) & (up_moves > 0), up_moves, 0)
                    minus_dm   = np.where((down_moves > up_moves) & (down_moves > 0), down_moves, 0)
                    tr_vals    = np.maximum(np.diff(highs[-28:]),
                                 np.maximum(np.diff(lows[-28:]),
                                 np.abs(np.diff(closes[-28:]))))
                    atr14 = np.mean(tr_vals[-14:])
                    if atr14 > 0:
                        pdi  = np.mean(plus_dm[-14:]) / atr14 * 100
                        mdi  = np.mean(minus_dm[-14:]) / atr14 * 100
                        dx   = abs(pdi - mdi) / max(pdi + mdi, 0.001) * 100
                        adx_val = float(dx)
            except Exception:
                pass

            struct_1h = self.detect_market_structure(df_main, window=3)
            struct_4h = 'SIDEWAYS'
            if df_higher is not None and len(df_higher) >= 10:
                struct_4h = self.detect_market_structure(df_higher, window=3)

            # ── Volume regime — cek apakah volume sedang rendah ──────────────
            low_vol = False
            if vols is not None and len(vols) >= 20:
                avg_vol_20 = np.mean(vols[-20:])
                avg_vol_5  = np.mean(vols[-5:])
                low_vol    = avg_vol_5 < avg_vol_20 * 0.4  # volume 5 candle terakhir < 40% rata-rata

            # ── Klasifikasi regime ────────────────────────────────────────────
            is_high_vol  = vol_regime > 1.8   # ATR sekarang 1.8x lebih besar dari rata-rata
            is_trending  = adx_val >= 22
            is_ranging   = adx_val < 18
            both_bull    = struct_1h == 'UPTREND'   and struct_4h == 'UPTREND'
            both_bear    = struct_1h == 'DOWNTREND' and struct_4h == 'DOWNTREND'

            if low_vol:
                return {
                    'regime': 'LOW_LIQUIDITY', 'atr_mult': 1.0, 'score_adj': +5,
                    'prefer_long': True, 'prefer_short': True,
                    'desc': f'Volume rendah ({avg_vol_5/avg_vol_20*100:.0f}% dari rata-rata) — signal sering palsu'
                }
            if is_high_vol:
                return {
                    'regime': 'HIGH_VOL', 'atr_mult': 1.5, 'score_adj': +3,
                    'prefer_long': True, 'prefer_short': True,
                    'desc': f'Volatilitas tinggi (ATR {vol_regime:.1f}x normal) — SL lebih lebar'
                }
            if is_trending and both_bull:
                return {
                    'regime': 'TRENDING_BULL', 'atr_mult': 1.0, 'score_adj': -2,
                    'prefer_long': True, 'prefer_short': False,
                    'desc': f'Trending naik kuat (ADX {adx_val:.0f}) — prioritaskan LONG'
                }
            if is_trending and both_bear:
                return {
                    'regime': 'TRENDING_BEAR', 'atr_mult': 1.0, 'score_adj': -2,
                    'prefer_long': False, 'prefer_short': True,
                    'desc': f'Trending turun kuat (ADX {adx_val:.0f}) — prioritaskan SHORT'
                }
            if is_ranging:
                return {
                    'regime': 'RANGING', 'atr_mult': 0.9, 'score_adj': 0,
                    'prefer_long': True, 'prefer_short': True,
                    'desc': f'Ranging/sideways (ADX {adx_val:.0f}) — trade dari S/R ke S/R'
                }
            return {
                'regime': 'NORMAL', 'atr_mult': 1.0, 'score_adj': 0,
                'prefer_long': True, 'prefer_short': True,
                'desc': f'Kondisi normal (ADX {adx_val:.0f})'
            }
        except Exception as e:
            logger.debug(f"detect_market_regime error: {e}")
            return default

    def _validate_structure_for_entry(self, df_1h, df_4h, price: float,
                                       atr: float, direction: str) -> tuple:
        """
        Validasi struktur market sebelum entry.
        Ini filter paling penting — cegah LONG saat downtrend dan SHORT saat uptrend.

        Logic berdasarkan analisa chart nyata:
        1. LH/LL aktif di 1H + 4H → DOWNTREND → blok LONG
        2. HH/HL aktif di 1H + 4H → UPTREND → blok SHORT
        3. Breakdown lower low dalam 3 candle terakhir → jangan LONG dulu, tunggu konfirmasi
        4. Harga di bawah EMA50 4H → bias bearish kuat → blok LONG kecuali signal sangat kuat
        5. Descending trendline resistance aktif → blok LONG sebelum confirmed breakout

        Return: (ok: bool, reason: str)
        """
        import numpy as np
        is_long = direction == 'LONG'

        try:
            # ── Analisa struktur 1H ────────────────────────────────
            struct_1h = self.detect_market_structure(df_1h, window=3)
            struct_4h = 'SIDEWAYS'
            ema50_4h  = None

            if df_4h is not None and len(df_4h) >= 10:
                struct_4h = self.detect_market_structure(df_4h, window=3)
                c4 = df_4h['close'].values.astype(float)
                ema50_4h = c4[0]
                for v in c4:
                    ema50_4h = v * (2/51) + ema50_4h * (49/51)

            # ── Rule 1: LH/LL di KEDUA TF → downtrend kuat, blok LONG ──
            if is_long:
                both_down = struct_1h == 'DOWNTREND' and struct_4h == 'DOWNTREND'
                if both_down:
                    return False, f"Struktur 1H+4H DOWNTREND (LH/LL aktif) — tunggu reversal konfirmasi"

                # 4H downtrend saja → downgrade quality saja, jangan blok total
                # (filter lebih kuat ada di whale flow & HTF EMA di signal_generator)

            # ── Rule 2: HH/HL di kedua TF → uptrend kuat, blok SHORT ──
            else:
                both_up = struct_1h == 'UPTREND' and struct_4h == 'UPTREND'
                if both_up:
                    return False, f"Struktur 1H+4H UPTREND (HH/HL aktif) — tunggu reversal konfirmasi"

                # 4H uptrend saja → downgrade quality saja

            # ── Rule 3: Recent breakdown lower low → jangan LONG ──
            if is_long and df_1h is not None and len(df_1h) >= 10:
                lows_1h = df_1h['low'].values.astype(float)
                closes_1h = df_1h['close'].values.astype(float)

                # Cek apakah ada candle close yang membuat lower low baru dalam 5 candle
                recent_low = min(lows_1h[-6:-1])  # low 5 candle sebelumnya
                prev_low   = min(lows_1h[-20:-6]) if len(lows_1h) >= 20 else recent_low
                breakdown  = closes_1h[-1] < prev_low * 0.998  # close di bawah low sebelumnya

                if breakdown and struct_1h in ('DOWNTREND', 'SIDEWAYS'):
                    return False, f"Breakdown lower low terbaru — tunggu konfirmasi reversal sebelum LONG"

            # ── Rule 4: Harga jauh di bawah EMA50 4H → bias bearish kuat ──
            # Hanya blok kalau jauh sekali (>8%) — fase recovery harga selalu mulai di bawah EMA50
            if is_long and ema50_4h and price < ema50_4h * 0.92:
                if struct_1h != 'UPTREND':
                    return False, f"Harga >8% di bawah EMA50 4H — terlalu jauh untuk entry LONG aman"

            # ── Rule 5: Descending trendline aktif → blok LONG ──
            if is_long and df_1h is not None and len(df_1h) >= 15:
                highs = df_1h['high'].values.astype(float)
                closes = df_1h['close'].values.astype(float)
                n = len(highs)

                # Cari dua swing high terakhir
                recent_highs = []
                for i in range(3, n-2):
                    if highs[i] > highs[i-1] and highs[i] > highs[i-2] and                        highs[i] > highs[i+1] and highs[i] > highs[i+2]:
                        recent_highs.append((i, highs[i]))

                if len(recent_highs) >= 2:
                    h1_idx, h1_val = recent_highs[-2]
                    h2_idx, h2_val = recent_highs[-1]

                    # Descending: setiap high lebih rendah
                    if h2_val < h1_val * 0.998:
                        # Project trendline ke candle sekarang
                        slope = (h2_val - h1_val) / max(h2_idx - h1_idx, 1)
                        tl_now = h2_val + slope * (n - 1 - h2_idx)
                        tl_now = max(tl_now, price * 0.95)  # floor

                        # Harga masih di bawah atau di trendline
                        if price <= tl_now * 1.005:
                            return False, f"Descending trendline aktif ({tl_now:.4f}) — tunggu confirmed breakout dulu"

            # ── Rule 6: Bearish engulfing besar + recent lower low → jangan LONG ──
            if is_long and df_1h is not None and len(df_1h) >= 3:
                c0 = df_1h['close'].values[-1]
                o0 = df_1h['open'].values[-1]
                c1 = df_1h['close'].values[-2]
                o1 = df_1h['open'].values[-2]
                body0 = abs(c0 - o0)
                body1 = abs(c1 - o1)

                # Bearish engulfing besar (candle bearish menelan candle sebelumnya)
                big_bearish_engulf = (
                    c0 < o0 and c1 > o1 and          # bearish engulf bullish
                    c0 <= o1 and o0 >= c1 and         # engulfing
                    body0 >= body1 * 1.5 and          # body signifikan lebih besar
                    body0 >= atr * 0.6                # body cukup besar vs ATR
                )
                if big_bearish_engulf:
                    return False, f"Bearish engulfing besar — seller kuat, jangan LONG sekarang"

            return True, ''

        except Exception as e:
            return True, ''  # error → allow (jangan blok karena exception)

    def fetch_ticker(self, pair_str):
        sym = pair_str.replace('/USDT', '').replace('USDT', '')
        d = self.get_price(clean_symbol(sym))
        return {'last': d['price']} if d else None