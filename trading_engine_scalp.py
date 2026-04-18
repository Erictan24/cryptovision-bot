"""
trading_engine_scalp.py — Wrapper analisa untuk Bot 2 (Scalping 15m).

Reuse TradingEngine existing untuk:
  - Data fetching (Binance/CryptoCompare)
  - BTC correlation check
  - News filter
  - Top coins listing

Signal generation: scalping_signal_engine.py (BB+RSI+MACD+Wedge)
Tidak mengubah trading_engine.py sama sekali.
"""

import logging
import time
from datetime import datetime

from trading_engine import TradingEngine
from scalping_signal_engine import (
    generate_scalping_signal,
    get_htf_bias,
)
from indicators import (
    calc_atr, calc_rsi, calc_adx, analyze_ema_trend,
    detect_market_structure,
)
from config import SCALP_CONFIG

try:
    from news_filter import get_news_filter
    _news_filter = get_news_filter()
except ImportError:
    _news_filter = None

logger = logging.getLogger(__name__)


class ScalpingEngine:
    """
    Thin wrapper di atas TradingEngine untuk scalping 15m.

    Tanggung jawab:
      1. Fetch data 15m (main) + 1H (konfirmasi)
      2. Hitung indikator dasar (ATR, RSI, ADX, EMA)
      3. Panggil scalping_signal_engine
      4. Apply filter: BTC correlation, news, position limit
      5. Return signal dict kompatibel dengan telegram_bot
    """

    def __init__(self, engine: TradingEngine = None):
        self.engine = engine or TradingEngine()
        self._signal_cache = {}  # anti-flip cache
        self._daily_loss_usd = 0.0
        self._daily_loss_date = None
        self.cfg = SCALP_CONFIG

    def analyze_coin_scalp(self, symbol: str) -> tuple:
        """
        Analisa satu coin untuk scalping 15m.

        Returns:
            (result_dict, error_string) — sama format dengan analyze_coin()
            result_dict punya key 'signal' yang kompatibel 100%.
        """
        try:
            # --- 1. Fetch data ---
            # 15m = main timeframe, langsung fetch via Binance
            df_15m = self.engine.get_klines(symbol, '15m')
            if df_15m is None or len(df_15m) < 80:
                return None, f"{symbol}: data 15m tidak cukup"

            # 1H = konfirmasi bias arah
            df_1h = self.engine.get_klines(symbol, '1h')

            # Harga real-time
            price_data = self.engine.get_price(symbol)
            if not price_data:
                return None, f"{symbol}: harga tidak tersedia"
            price = price_data['price']
            chg24 = price_data.get('change_24h', 0)

            # --- 2. Indikator dasar dari 15m ---
            atr_series = calc_atr(df_15m, 14)
            atr = float(atr_series.iloc[-1]) if atr_series is not None else 0
            if atr <= 0:
                return None, f"{symbol}: ATR invalid"

            rsi = float(calc_rsi(df_15m, 14).iloc[-1])
            adx = calc_adx(df_15m, 14)
            ema_trend, emas, ema_desc = analyze_ema_trend(df_15m)
            structure = detect_market_structure(df_15m)

            # --- 3. HTF bias dari 1H ---
            htf_bias = get_htf_bias(df_1h) if df_1h is not None else 'SIDEWAYS'

            # Map bias ke format htf_ema yang diharapkan signal engine
            htf_ema_map = {
                'BULLISH': 'UP',
                'BEARISH': 'DOWN',
                'SIDEWAYS': 'SIDEWAYS',
            }
            htf_ema = htf_ema_map.get(htf_bias, 'SIDEWAYS')

            # --- 4. Generate scalping signal ---
            signal = generate_scalping_signal(
                price=price,
                atr=atr,
                ema_trend=ema_trend,
                structure=structure,
                ks=None,   # Scalping tidak pakai S&R zone
                kr=None,
                res_mtf=[],
                sup_mtf=[],
                smc={},    # Scalping tidak pakai SMC
                rsi=rsi,
                htf_ema=htf_ema,
                df_main=df_15m,
                symbol=symbol,
                adx=adx,
                signal_cache=self._signal_cache,
            )

            if signal is None:
                return None, None  # Tidak ada error, hanya tidak ada signal

            # Update signal cache untuk anti-flip
            self._signal_cache[symbol] = {
                'dir': signal['direction'],
                'ts': time.time(),
                'score': signal['confluence_score'],
                'quality': signal['quality'],
            }

            # --- 5. Apply filters ---

            # Filter A: News filter
            if _news_filter:
                news = _news_filter.check()
                if news.get('block'):
                    logger.info(f"[{symbol}] Scalp BLOCKED by news: "
                                f"{news.get('reason', '')}")
                    return None, None

            # Filter B: BTC correlation (reuse existing engine method)
            if symbol != 'BTC':
                try:
                    btc_ok, btc_reason = self.engine._check_btc_alt_correlation(
                        signal['direction'], symbol)
                    if not btc_ok:
                        logger.info(f"[{symbol}] Scalp BLOCKED by BTC: "
                                    f"{btc_reason}")
                        return None, None
                except Exception:
                    pass  # Jika method tidak ada, skip

            # --- 6. Build result dict (kompatibel dengan telegram_bot) ---
            result = {
                'symbol': symbol,
                'exchange': 'Binance',
                'price': price,
                'change_24h': chg24,
                'atr': atr,
                'rsi': round(rsi, 1),
                'adx': round(adx, 1),
                'tf': '15m',
                'tf_label': '15M',
                'htf_label': '1H',
                'ema_trend': ema_trend,
                'ema_desc': ema_desc,
                'emas': emas,
                'structure': structure,
                'signal': signal,
                'direction': signal['direction'],
                'timestamp': datetime.now().isoformat(),
                'htf_bias': htf_bias,
                'engine_type': 'scalping',
                # Keys yang dibutuhkan chart_generator
                'key_support': None,
                'key_resistance': None,
                'ema_cross': None,
                'smc': None,
            }

            return result, None

        except Exception as e:
            logger.error(f"analyze_coin_scalp {symbol}: {e}", exc_info=True)
            return None, str(e)

    def scan_all_coins(self, max_signals: int = 10,
                       max_coins: int = 30) -> list:
        """
        Scan top coin dan return list signal terbaik.

        v4.3: Gunakan whitelist kalau aktif — hanya scan proven coins.

        max_coins: batasi jumlah coin yang di-scan (default 30).
        """
        # v4.3: whitelist check (dari config)
        if self.cfg.get('use_whitelist') and self.cfg.get('scalp_whitelist'):
            coins = list(self.cfg['scalp_whitelist'])
            logger.info(f"Using SCALP whitelist: {len(coins)} coins")
        else:
            coins = self.engine.get_top_coins(100)
            if not coins:
                logger.warning("Scalp scan: gagal ambil coin list")
                return []
            # Ambil top N coin saja (berdasarkan volume)
            coins = coins[:max_coins]

        signals = []
        for symbol in coins:
            try:
                result, err = self.analyze_coin_scalp(symbol)
                if result and result.get('signal'):
                    sig = result['signal']
                    signals.append({
                        'symbol': symbol,
                        'result': result,
                        'signal': sig,
                        'quality': sig['quality'],
                        'score': sig['confluence_score'],
                        'direction': sig['direction'],
                    })
            except Exception as e:
                logger.debug(f"Scalp scan {symbol}: {e}")
                continue

        # Sort: GOOD > MODERATE > WAIT, lalu by score descending
        quality_rank = {'IDEAL': 0, 'GOOD': 1, 'MODERATE': 2, 'WAIT': 3}
        signals.sort(key=lambda x: (
            quality_rank.get(x['quality'], 99),
            -x['score']
        ))

        return signals[:max_signals]

    def check_btc_bias(self) -> dict:
        """
        Cek kondisi BTC untuk display.
        Reuse engine.check_btc_condition().
        """
        try:
            return self.engine.check_btc_condition()
        except Exception:
            return {
                'ok_long': True, 'ok_short': True,
                'btc_bias': 'NEUTRAL', 'reason': 'N/A'
            }
