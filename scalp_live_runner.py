"""
scalp_live_runner.py — Live scalp scan + paper trade integration.

Fungsi:
1. Scan semua coin tiap 15 menit → generate scalp signal.
2. Kalau ada signal valid → add ke scalp_paper_trader.
3. Monitor thread cek outcome tiap 5 menit.

TIDAK eksekusi order ke exchange — pure paper tracking.
"""
import time
import logging
import threading
import asyncio
import requests
from datetime import datetime
from typing import Optional, Callable

from scalp_paper_trader import PaperTrader

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────
SCALP_SCAN_INTERVAL = 900      # 15 menit
MONITOR_INTERVAL    = 300      # 5 menit
DEDUP_HOURS         = 4        # anti dobel signal per coin+direction


def _fetch_binance_klines(symbol: str, interval: str, limit: int = 200):
    """Fetch OHLCV dari Binance Futures. Return DataFrame atau None."""
    import pandas as pd
    try:
        url = "https://fapi.binance.com/fapi/v1/klines"
        r = requests.get(url, params={
            'symbol': f"{symbol}USDT",
            'interval': interval,
            'limit': limit,
        }, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        df = pd.DataFrame(data, columns=[
            'timestamp','open','high','low','close','volume',
            'close_time','quote_vol','trades','taker_buy_base',
            'taker_buy_quote','ignore'
        ])
        for col in ['open','high','low','close','volume']:
            df[col] = df[col].astype(float)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df[['timestamp','open','high','low','close','volume']]
    except Exception as e:
        logger.debug(f"Fetch {symbol} {interval} error: {e}")
        return None


def _get_current_price(symbol: str) -> Optional[float]:
    """Get harga terkini dari Binance."""
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params={'symbol': f"{symbol}USDT"}, timeout=10)
        if r.status_code == 200:
            return float(r.json().get('price', 0))
    except Exception:
        pass
    return None


def _scan_coin(symbol: str) -> Optional[dict]:
    """
    Scan 1 coin, return scalp signal dict atau None.
    """
    from scalping_signal_engine import generate_scalping_signal
    from indicators import calc_atr, calc_rsi, calc_adx, analyze_ema_trend

    try:
        df_15m = _fetch_binance_klines(symbol, '15m', 200)
        df_1h  = _fetch_binance_klines(symbol, '1h', 100)
        if df_15m is None or len(df_15m) < 80:
            return None
        if df_1h is None or len(df_1h) < 55:
            return None

        # Indicators dari 15m
        atr_s = calc_atr(df_15m, 14)
        atr   = float(atr_s.iloc[-1]) if atr_s is not None else 0
        if atr <= 0:
            return None
        rsi_val = float(calc_rsi(df_15m, 14).iloc[-1])
        adx_val = calc_adx(df_15m, 14)
        ema_trend, _, _ = analyze_ema_trend(df_15m)
        price = float(df_15m['close'].iloc[-1])

        # HTF bias dari 1h
        from scalping_signal_engine import get_htf_bias
        htf_bias = get_htf_bias(df_1h)
        htf_map  = {'BULLISH': 'UP', 'BEARISH': 'DOWN', 'SIDEWAYS': 'SIDEWAYS'}
        htf_ema  = htf_map.get(htf_bias, 'SIDEWAYS')

        signal = generate_scalping_signal(
            price=price, atr=atr, ema_trend=ema_trend,
            structure='SIDEWAYS',
            ks=None, kr=None, res_mtf=[], sup_mtf=[],
            smc={'df_1h': df_1h},
            rsi=rsi_val, htf_ema=htf_ema,
            df_main=df_15m, symbol=symbol,
            adx=adx_val, signal_cache=None,
        )
        if signal:
            signal['_symbol'] = symbol
            signal['symbol']  = symbol
        return signal

    except Exception as e:
        logger.debug(f"scan {symbol} error: {e}")
        return None


def _send_signal_notif(notify_fn, trade_id, signal):
    """Kirim notif ke Telegram saat scalp signal baru masuk."""
    if not notify_fn:
        return
    direction = signal.get('direction', '?')
    ico = "🟢" if direction == 'LONG' else "🔴"
    sym = signal.get('symbol', '?')
    entry = signal.get('entry', 0)
    sl    = signal.get('sl', 0)
    tp1   = signal.get('tp1', 0)
    tp2   = signal.get('tp2', 0)
    score = signal.get('confluence_score', signal.get('score', 0))
    qty   = signal.get('quality', 'GOOD')

    try:
        rr = abs(tp2 - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
    except Exception:
        rr = 0

    msg = (
        "📊 SCALP PAPER SIGNAL\n" +
        "=" * 28 + "\n" +
        f"{ico} {sym} {direction} [{qty}]\n" +
        f"Entry : {entry:.6g}\n" +
        f"SL    : {sl:.6g}\n" +
        f"TP1   : {tp1:.6g}\n" +
        f"TP2   : {tp2:.6g}\n" +
        f"RR    : 1:{rr:.1f}\n" +
        f"Score : {score}\n\n" +
        "[Paper trade — tidak dieksekusi]\n" +
        f"Trade ID: {trade_id}"
    )
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(notify_fn(msg))
        loop.close()
    except Exception:
        pass


def _send_close_notif(notify_fn, close_result, stats):
    """Kirim notif saat paper trade closed."""
    if not notify_fn:
        return
    outcome = close_result.get('outcome', '?')
    sym = close_result.get('symbol', '?')
    direction = close_result.get('direction', '?')
    pnl_r = close_result.get('pnl_r', 0)

    emoji = "✅" if pnl_r > 0 else ("⚪" if pnl_r == 0 else "❌")
    msg = (
        f"{emoji} PAPER {outcome} — {sym} {direction}\n" +
        f"PnL: {pnl_r:+.2f}R\n\n" +
        f"📊 Paper WR: {stats['wr']:.1f}% " +
        f"({stats['wins']}W/{stats['losses']}L of {stats['n_closed']})\n" +
        f"Total PnL: {stats['total_pnl_r']:+.2f}R"
    )
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(notify_fn(msg))
        loop.close()
    except Exception:
        pass


# ── Singleton ────────────────────────────────────────────────
_paper_trader = None
_scan_started = False


def get_paper_trader(notify_fn=None, risk_usd=1.0, max_positions=5):
    """Get or create paper trader instance."""
    global _paper_trader
    if _paper_trader is None:
        _paper_trader = PaperTrader(
            risk_usd=risk_usd,
            max_positions=max_positions,
            notify_fn=notify_fn,
        )
    elif notify_fn is not None:
        _paper_trader.notify_fn = notify_fn
    return _paper_trader


def start_scalp_live(coins_fn: Callable, notify_fn: Callable,
                     risk_usd: float = 1.0):
    """
    Start scalp live paper trading di background thread.

    coins_fn  : callable yang return list of coin names (dipanggil tiap scan)
    notify_fn : async function untuk kirim notif ke Telegram
    risk_usd  : $ per trade untuk display PnL
    """
    global _scan_started
    if _scan_started:
        logger.warning("Scalp live sudah jalan, skip")
        return
    _scan_started = True

    pt = get_paper_trader(notify_fn=notify_fn, risk_usd=risk_usd,
                          max_positions=10)

    # ── Scan loop (generate signal) ──────────────────────
    def scan_loop():
        time.sleep(120)  # grace period setelah bot start
        logger.info("⚡ Scalp paper scan dimulai (15 menit interval)")
        while True:
            try:
                coins = coins_fn()
                signals_found = 0
                for coin in coins:
                    sig = _scan_coin(coin)
                    if sig is None:
                        continue

                    trade_id = pt.open_paper_trade(sig)
                    if trade_id:
                        signals_found += 1
                        logger.info(f"📊 Paper signal #{trade_id}: "
                                    f"{coin} {sig.get('direction')}")
                        _send_signal_notif(notify_fn, trade_id, sig)

                    time.sleep(0.3)  # rate limit buffer

                if signals_found:
                    logger.info(f"✅ Scalp scan selesai: {signals_found} signal baru")
            except Exception as e:
                logger.error(f"Scalp scan error: {e}", exc_info=True)
            time.sleep(SCALP_SCAN_INTERVAL)

    t1 = threading.Thread(target=scan_loop, daemon=True,
                          name="scalp_scan_loop")
    t1.start()
    logger.info("📊 Scalp scan scheduler: 15 menit interval")

    # ── Monitor loop (cek outcome) ───────────────────────
    def monitor_loop():
        time.sleep(180)  # grace lebih lama
        logger.info("👁️ Scalp paper monitor dimulai (5 menit interval)")
        while True:
            try:
                closed = pt.monitor_all_open(_get_current_price)
                if closed:
                    stats = pt.get_stats()
                    for c in closed:
                        _send_close_notif(notify_fn, c, stats)
            except Exception as e:
                logger.error(f"Monitor error: {e}", exc_info=True)
            time.sleep(MONITOR_INTERVAL)

    t2 = threading.Thread(target=monitor_loop, daemon=True,
                          name="scalp_monitor_loop")
    t2.start()
    logger.info("👁️ Scalp monitor scheduler: 5 menit interval")
