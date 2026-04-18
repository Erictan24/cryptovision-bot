"""
main_scalp.py — Entry point Bot 2 (Scalping 15m) — PRODUCTION v4.3.

Jalankan terpisah dari main.py (Bot 1).
Bot 1 = swing trading 1H, scan 30 menit.
Bot 2 = scalping 15m, scan 5 menit.

v4.3 FEATURES:
  - Multi-timeframe trend following (1H trend, 15m entry)
  - Self-learning dari trade journal (SQLite)
  - Per-coin adaptive thresholds
  - Session filter (block DEAD + learned bad sessions)
  - 4H macro trend agreement
  - Volume pressure + SMC BOS
  - Full auto Bitunix integration
  - Daily loss limit enforcement
  - Graceful error recovery
  - Weekly learning refresh

Usage:
  python main_scalp.py
"""

import sys
import io
import logging
import time
import asyncio
import threading
import traceback
from datetime import datetime, timedelta

# Paksa stdout UTF-8 untuk Windows
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding='utf-8', errors='replace')

from database import DatabaseManager
from trading_engine import TradingEngine
from trading_engine_scalp import ScalpingEngine
from risk_manager import RiskManager
from telegram_bot import TelegramBot
from config import SCALP_CONFIG

# Learning modules (v4.3)
try:
    import scalp_trade_journal as journal
    import scalp_coin_learning as coin_learn
    import scalp_session_filter as session_filter
    _LEARNING_AVAILABLE = True
except ImportError:
    _LEARNING_AVAILABLE = False

# Paper trader (Level 1 mode)
try:
    from scalp_paper_trader import PaperTrader
    _PAPER_AVAILABLE = True
except ImportError:
    _PAPER_AVAILABLE = False

try:
    from chart_generator import generate_signal_chart, cleanup_chart
    _CHART_GEN = True
except ImportError:
    _CHART_GEN = False

# =============================================
# LOGGING — file terpisah dari bot utama
# =============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler('bot_scalp.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class ScalpingBot:
    """
    Bot 2 — Scalping 15m Production v4.3.

    Arsitektur:
      - TradingEngine: reuse untuk data fetching + BTC check
      - ScalpingEngine: wrapper yang panggil scalping_signal_engine v4.3
      - TelegramBot: reuse untuk kirim signal + auto trade
      - Scan interval: 5 menit (dari SCALP_CONFIG)
      - Self-learning: weekly refresh coin + session stats
      - Error recovery: auto-restart scan kalau crash
    """

    def __init__(self):
        logger.info("=" * 60)
        logger.info(" Bot 2 — SCALPING v4.3 PRODUCTION")
        logger.info("=" * 60)
        logger.info("Inisialisasi...")

        self.db = DatabaseManager()
        self.base_engine = TradingEngine()
        self.scalp_engine = ScalpingEngine(self.base_engine)
        self.risk = RiskManager(self.db)
        self.tg = TelegramBot(self.db)
        self.tg.engine = self.base_engine
        self.tg.risk = self.risk

        self.cfg = SCALP_CONFIG
        self._scan_running = False
        self._start_time = datetime.now()

        # Production state
        self._total_signals = 0
        self._total_trades = 0
        self._daily_pnl = 0.0
        self._daily_loss_hit = False
        self._consecutive_errors = 0
        self._last_error_time = None
        self._last_daily_reset = datetime.now().date()

        # Paper trader (Level 1 mode)
        self._paper_mode = self.cfg.get('paper_mode', False) or \
                           not self.cfg.get('auto_trade_enabled', False)
        self.paper_trader = None
        if self._paper_mode and _PAPER_AVAILABLE:
            self.paper_trader = PaperTrader(
                risk_usd=self.cfg.get('risk_per_trade_usd', 1.0),
                max_positions=self.cfg.get('max_positions', 3),
            )
            logger.info("PAPER TRADE MODE AKTIF (Level 1)")

        # Learning refresh tracker
        self._last_learning_refresh = datetime.now()

        # Initialize learning modules
        if _LEARNING_AVAILABLE:
            try:
                learning = coin_learn.get_learning()
                gate = session_filter.get_session_gate()
                total_trades_in_journal = journal.count_trades()
                logger.info(f"Learning modules loaded — "
                            f"{total_trades_in_journal} trades in journal")
            except Exception as e:
                logger.warning(f"Learning init failed: {e}")

        logger.info("Bot 2 v4.3 siap untuk production!")

    def run(self):
        logger.info("Bot 2 mulai berjalan!")
        logger.info(f"   Mode: Auto Scan {self.cfg['scan_interval_minutes']} menit")
        logger.info(f"   Timeframe: {self.cfg['main_tf']} (konfirmasi: {self.cfg['confirm_tf']})")
        logger.info(f"   Risk: ${self.cfg['risk_per_trade_usd']}/trade, "
                     f"daily limit ${self.cfg['daily_loss_limit']}")
        logger.info(f"   Max posisi: {self.cfg['max_positions']}, "
                     f"max searah: {self.cfg['max_same_direction']}")
        logger.info(f"   Learning: {'ENABLED' if _LEARNING_AVAILABLE else 'DISABLED'}")
        logger.info("   Tekan Ctrl+C untuk stop\n")

        # Start Telegram bot
        self.tg.start_bot()

        # Start scalping scan loop
        self._start_scalp_scan()

        # Start paper trade monitor loop (Level 1)
        if self._paper_mode and self.paper_trader:
            self._start_paper_monitor()

        # Start weekly learning refresh scheduler
        if _LEARNING_AVAILABLE:
            self._start_weekly_learning_refresh()

        # Start daily summary scheduler
        self._start_daily_summary()

        # Send startup notification to Telegram
        self._trigger_async(self._send_startup_notification())

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("\nBot 2 dihentikan oleh user.")
            self._trigger_async(self._broadcast(
                "Bot 2 SCALP shutdown manual"))
            time.sleep(2)
            self.tg.stop_bot()
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            self._trigger_async(self._broadcast(
                f"Bot 2 SCALP CRASHED: {str(e)[:200]}"))
            time.sleep(2)
            self.tg.stop_bot()

    async def _send_startup_notification(self):
        """Kirim notifikasi saat bot start."""
        msg = (
            "Bot 2 SCALP v4.3 STARTED\n"
            + "=" * 28 + "\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Risk: ${self.cfg['risk_per_trade_usd']}/trade\n"
            f"Daily loss limit: ${self.cfg['daily_loss_limit']}\n"
            f"Max posisi: {self.cfg['max_positions']}\n"
            f"Scan interval: {self.cfg['scan_interval_minutes']} menit\n"
            f"Learning: {'ON' if _LEARNING_AVAILABLE else 'OFF'}\n"
            f"Auto trade: {'ON' if self.cfg.get('auto_trade_enabled') else 'OFF'}"
        )
        await self._broadcast(msg)

    async def _broadcast(self, text: str):
        """Broadcast message ke semua chat."""
        for cid in list(self.tg.chat_ids):
            try:
                await self.tg._safe_send(cid, text)
            except Exception:
                pass

    # ==================================================================
    # SCALPING SCAN LOOP — setiap 5 menit dengan error recovery
    # ==================================================================
    def _start_scalp_scan(self):
        """
        Scan loop utama: setiap 5 menit, scan top 30 coin di 15m.
        Full error recovery — crash 1 scan tidak bunuh bot.
        """
        interval = self.cfg['scan_interval_minutes'] * 60
        startup_grace = 30

        def scan_loop():
            time.sleep(startup_grace)
            logger.info(f"Scalp scan dimulai (setiap "
                        f"{self.cfg['scan_interval_minutes']} menit)")

            while True:
                try:
                    # Daily reset check
                    self._check_daily_reset()

                    # Stop scanning kalau daily loss hit
                    if self._daily_loss_hit:
                        logger.warning("Daily loss limit reached, skip scan")
                        time.sleep(interval)
                        continue

                    self._run_one_scan()
                    self._consecutive_errors = 0  # reset counter on success

                except Exception as e:
                    self._consecutive_errors += 1
                    self._last_error_time = datetime.now()
                    logger.error(f"Scalp scan error #{self._consecutive_errors}: "
                                 f"{e}", exc_info=True)

                    # Kalau 5x error berturut-turut, alert Telegram
                    if self._consecutive_errors >= 5:
                        self._trigger_async(self._broadcast(
                            f"BOT 2 ERROR x{self._consecutive_errors}\n"
                            f"Latest: {str(e)[:200]}\n"
                            f"Bot masih jalan, tapi cek logs!"))
                        # Cooldown longer
                        time.sleep(interval * 2)
                        continue

                time.sleep(interval)

        t = threading.Thread(target=scan_loop, daemon=True)
        t.start()
        logger.info(f"Scalp scan scheduler aktif")

    def _check_daily_reset(self):
        """Reset daily counters setiap hari baru (UTC 00:00)."""
        today = datetime.now().date()
        if today != self._last_daily_reset:
            logger.info(f"Daily reset — new day {today}")
            self._daily_pnl = 0.0
            self._daily_loss_hit = False
            self._last_daily_reset = today

            # Auto-sync daily loss dari exchange kalau trader ada
            if self.tg.trader and self.tg.trader.is_ready:
                try:
                    self.tg.trader.sync_daily_loss_from_exchange()
                except Exception as e:
                    logger.debug(f"Daily loss sync failed: {e}")

    def _run_one_scan(self):
        """Jalankan satu siklus scan."""
        if self._scan_running:
            return
        self._scan_running = True

        try:
            start_ts = time.time()
            logger.info("Scalp scan dimulai...")

            # Cek daily loss limit dari exchange
            if self.tg.trader and self.tg.trader.is_ready:
                try:
                    self.tg.trader.sync_daily_loss_from_exchange()
                    if self.tg.trader._is_daily_loss_exceeded():
                        if not self._daily_loss_hit:
                            self._daily_loss_hit = True
                            logger.warning(
                                "DAILY LOSS LIMIT HIT — stop trading hari ini")
                            self._trigger_async(self._broadcast(
                                "Bot 2 DAILY LOSS LIMIT HIT\n"
                                f"Stopped sampai reset besok"))
                        return
                except Exception as e:
                    logger.debug(f"Daily loss check: {e}")

            # Cek BTC bias dulu
            btc = self.scalp_engine.check_btc_bias()
            btc_bias = btc.get('btc_bias', 'NEUTRAL')
            logger.info(f"BTC bias: {btc_bias}")

            # Scan semua coin
            signals = self.scalp_engine.scan_all_coins(max_signals=10)
            self._total_signals += len(signals)

            elapsed = time.time() - start_ts
            logger.info(f"Scalp scan selesai: {len(signals)} signal "
                        f"({elapsed:.1f}s)")

            if not signals:
                return

            # Kirim signal ke Telegram + auto trade
            self._trigger_async(self._process_signals(signals))

        finally:
            self._scan_running = False

    # ==================================================================
    # PAPER TRADE MONITOR (setiap 2 menit)
    # ==================================================================
    def _start_paper_monitor(self):
        """Monitor open paper trades, update SL/TP status."""
        if not self.paper_trader:
            return

        def monitor_loop():
            time.sleep(60)  # grace period
            logger.info("Paper trade monitor aktif (cek 2 menit)")

            while True:
                try:
                    # Price fetcher wrapper
                    def fetch_price(sym):
                        try:
                            return self.base_engine.get_price(sym)
                        except Exception:
                            return None

                    closed = self.paper_trader.monitor_all_open(fetch_price)

                    # Notify closed trades
                    for c in closed:
                        pnl = c.get('pnl_r', 0)
                        pnl_usd = pnl * self.cfg.get('risk_per_trade_usd', 1.0)
                        emoji = '🟢' if pnl > 0 else '🔴' if pnl < 0 else '⚪'
                        msg = (
                            f"[PAPER] CLOSED #{c['trade_id']}\n"
                            + "=" * 28 + "\n"
                            f"{emoji} {c['symbol']} {c['direction']}\n"
                            f"Outcome: {c['outcome']}\n"
                            f"Entry: {c['entry']:.6g}\n"
                            f"Close: {c['close']:.6g}\n"
                            f"PnL: {pnl:+.2f}R (${pnl_usd:+.2f})"
                        )
                        self._trigger_async(self._broadcast(msg))

                except Exception as e:
                    logger.error(f"Paper monitor error: {e}")

                time.sleep(120)  # 2 menit

        t = threading.Thread(target=monitor_loop, daemon=True)
        t.start()
        logger.info("Paper monitor scheduler aktif")

    # ==================================================================
    # WEEKLY LEARNING REFRESH (setiap Senin jam 02:00)
    # ==================================================================
    def _start_weekly_learning_refresh(self):
        """Refresh coin + session stats setiap minggu."""
        def refresh_loop():
            while True:
                try:
                    now = datetime.now()
                    # Senin dini hari (jam 02-03 UTC)
                    if (now.weekday() == 0 and 2 <= now.hour < 3 and
                            (now - self._last_learning_refresh).total_seconds() > 3600):
                        logger.info("Weekly learning refresh...")
                        try:
                            from config import SCAN_POOL
                            coins = SCAN_POOL[:30]

                            learning = coin_learn.get_learning()
                            n = learning.refresh(coins)

                            gate = session_filter.get_session_gate()
                            gate.refresh()

                            self._last_learning_refresh = now

                            # Notify Telegram
                            self._trigger_async(self._broadcast(
                                f"Weekly learning refreshed\n"
                                f"Updated {n} coins"))
                            logger.info(f"Learning refresh done ({n} coins)")
                        except Exception as e:
                            logger.error(f"Learning refresh failed: {e}")
                except Exception as e:
                    logger.error(f"Learning scheduler error: {e}")

                time.sleep(600)  # cek setiap 10 menit

        t = threading.Thread(target=refresh_loop, daemon=True)
        t.start()
        logger.info("Weekly learning refresh scheduler aktif (Senin 02:00)")

    # ==================================================================
    # DAILY SUMMARY (setiap jam 23:00 local)
    # ==================================================================
    def _start_daily_summary(self):
        """Kirim summary harian ke Telegram jam 23:00."""
        self._last_summary_sent = None

        def summary_loop():
            while True:
                try:
                    now = datetime.now()
                    if (now.hour == 23 and now.minute < 5 and
                            self._last_summary_sent != now.date()):
                        self._last_summary_sent = now.date()
                        self._trigger_async(self._send_daily_summary())
                except Exception as e:
                    logger.error(f"Daily summary error: {e}")
                time.sleep(60)

        t = threading.Thread(target=summary_loop, daemon=True)
        t.start()
        logger.info("Daily summary scheduler aktif (23:00)")

    async def _send_daily_summary(self):
        """Generate dan kirim daily summary."""
        try:
            uptime = datetime.now() - self._start_time
            uptime_hours = uptime.total_seconds() / 3600

            # Paper mode stats
            if self._paper_mode and self.paper_trader:
                paper_msg = self.paper_trader.format_stats_msg()
                mode = "[PAPER MODE]"
            else:
                paper_msg = ""
                mode = "[LIVE]"

            # Query journal untuk stats hari ini
            recent = {'n_trades': 0, 'wr': 0, 'ev_r': 0, 'total_pnl_r': 0}
            if _LEARNING_AVAILABLE:
                try:
                    recent = journal.get_recent_summary(days=1)
                except Exception:
                    pass

            msg = (
                f"DAILY SUMMARY — Bot 2 SCALP {mode}\n"
                + "=" * 28 + "\n"
                f"Date: {datetime.now().strftime('%Y-%m-%d')}\n"
                f"Uptime: {uptime_hours:.1f}h\n"
                f"Signals generated: {self._total_signals}\n"
                f"Daily loss hit: {'YES' if self._daily_loss_hit else 'NO'}\n"
                f"Errors: {self._consecutive_errors}\n"
            )
            if paper_msg:
                msg += "\n" + paper_msg
            await self._broadcast(msg)
            logger.info("Daily summary sent")
        except Exception as e:
            logger.error(f"Daily summary failed: {e}")

    async def _process_signals(self, signals: list):
        """Proses signal: kirim ke Telegram, auto trade atau paper trade."""
        import asyncio as aio
        loop = aio.get_event_loop()

        for item in signals:
            try:
                symbol = item['symbol']
                result = item['result']
                sig = item['signal']
                quality = item['quality']
                direction = item['direction']
                score = item['score']

                # Tag symbol ke signal
                sig['_symbol'] = symbol

                # Format pesan
                ico = "🟢" if direction == "LONG" else "🔴"
                text = self._format_scalp_signal(
                    symbol, sig, result, ico, quality, score)

                # Prefix [PAPER] kalau paper mode
                if self._paper_mode:
                    text = "[PAPER MODE]\n" + text

                # Kirim ke semua chat
                for cid in list(self.tg.chat_ids):
                    await self._send_with_chart(
                        cid, symbol, result, sig, text)

                # Paper mode atau Auto trade
                if quality not in ('GOOD', 'IDEAL'):
                    continue

                if self._paper_mode and self.paper_trader:
                    # PAPER TRADE — simulate only
                    trade_id = self.paper_trader.open_paper_trade(sig)
                    if trade_id:
                        await self._broadcast(
                            f"📝 PAPER TRADE OPENED #{trade_id}\n"
                            f"{symbol} {direction} [{quality}]\n"
                            f"Entry: {sig.get('entry', 0):.6g}\n"
                            f"SL: {sig.get('sl', 0):.6g}\n"
                            f"Risk: ${self.cfg.get('risk_per_trade_usd', 1)} (simulated)"
                        )
                elif (self.cfg.get('auto_trade_enabled') and
                      self.tg.trader and self.tg.trader.is_ready):
                    # LIVE AUTO TRADE
                    await self._auto_execute_scalp(
                        symbol, direction, sig, result, loop)

            except Exception as e:
                logger.error(f"Process signal {item.get('symbol', '?')}: {e}")

    def _format_scalp_signal(self, symbol: str, sig: dict,
                             result: dict, ico: str,
                             quality: str, score: int) -> str:
        """Format signal scalping untuk Telegram."""
        direction = sig['direction']
        entry = sig['entry']
        sl = sig['sl']
        tp1 = sig['tp1']
        tp2 = sig['tp2']
        tp3 = sig.get('tp3', tp2)
        rr1 = sig['rr1']
        rr2 = sig['rr2']
        sl_pct = sig['sl_pct']
        rsi_val = result.get('rsi', 0)
        htf_bias = result.get('htf_bias', '?')

        # Wedge/MACD/BB info
        wedge_info = sig.get('wedge', {})
        macd_info = sig.get('macd_state', {})
        rsi_info = sig.get('rsi_state', {})
        candle = sig.get('candle_confirm', '-')

        # v4.3 fields
        trend_state = sig.get('trend_state', '')
        pullback_q = sig.get('pullback_quality', '')
        session = sig.get('session', '')
        macro = sig.get('macro_4h_bias', '')
        vol_pressure = sig.get('volume_pressure', '')
        bos = sig.get('smc_bos', '')

        lines = [
            f"{ico} [SCALP] {symbol} {direction} [{quality}]",
            "=" * 30,
            f"Entry : {entry:.6g}",
            f"SL    : {sl:.6g} ({sl_pct:.1f}%)",
            f"TP1   : {tp1:.6g} (RR 1:{rr1})",
            f"TP2   : {tp2:.6g} (RR 1:{rr2})",
            f"TP3   : {tp3:.6g}",
            "",
            f"Strategy: Trend-Following Pullback",
            f"Trend 1H: {trend_state} | Pullback: {pullback_q}",
            f"Macro 4H: {macro} | Session: {session}",
            f"Vol pressure: {vol_pressure or '-'}",
            f"SMC BOS: {bos or '-'}",
            f"Score: {score} | RSI: {rsi_val}",
            "",
            "Reasons:",
        ]

        for r in sig.get('reasons', []):
            lines.append(f"  - {r}")

        lines.append("")
        lines.append(f"Risk: ${self.cfg['risk_per_trade_usd']} | "
                     f"Lev: {self.cfg['leverage']}x | TF: 15m")

        return "\n".join(lines)

    async def _send_with_chart(self, chat_id, symbol: str,
                               result: dict, sig: dict, text: str):
        """Kirim signal + chart ke Telegram."""
        chart_path = None
        if _CHART_GEN:
            try:
                import asyncio as aio
                loop = aio.get_event_loop()
                df_15m = await loop.run_in_executor(
                    None, self.base_engine.get_klines, symbol, '15m')

                if df_15m is not None:
                    chart_path = await loop.run_in_executor(
                        None, generate_signal_chart,
                        df_15m, symbol, '15m', sig,
                        None,  # support
                        None,  # resistance
                        None,  # ema_cross
                        result.get('structure', 'SIDEWAYS'),
                        None,  # save_path
                        None,  # smc
                    )
            except Exception as e:
                logger.debug(f"Chart gen {symbol}: {e}")
                chart_path = None

        if chart_path:
            try:
                with open(chart_path, 'rb') as f_img:
                    caption = text[:1024] if len(text) > 1024 else text
                    await self.tg.app.bot.send_photo(
                        chat_id=chat_id, photo=f_img,
                        caption=caption, parse_mode=None)
                cleanup_chart(chart_path)
                return
            except Exception as e:
                logger.debug(f"Send photo {symbol}: {e}")
                cleanup_chart(chart_path)

        # Fallback: text saja
        await self.tg._safe_send(chat_id, text)

    async def _auto_execute_scalp(self, symbol: str, direction: str,
                                  sig: dict, result: dict, loop):
        """Auto execute trade scalping."""
        try:
            trader = self.tg.trader

            # Cek daily loss
            trader.sync_daily_loss_from_exchange()
            if trader._is_daily_loss_exceeded():
                logger.info("Scalp auto trade skip: daily loss limit")
                return

            # Cek max posisi (3 untuk scalping)
            positions = await loop.run_in_executor(
                None, trader.get_positions)
            max_pos = self.cfg['max_positions']
            if len(positions) >= max_pos:
                logger.info(f"Scalp auto trade skip: max posisi "
                            f"{len(positions)}/{max_pos}")
                return

            # Cek max posisi searah (2 untuk scalping)
            dirs = [("LONG" if p.get("side", "") in ("BUY", "LONG")
                     else "SHORT") for p in positions]
            max_same = self.cfg['max_same_direction']
            if dirs.count(direction) >= max_same:
                logger.info(f"Scalp auto trade skip: max {direction} "
                            f"= {dirs.count(direction)}/{max_same}")
                return

            # Execute
            quality = sig.get('quality', 'GOOD')
            result_order = await loop.run_in_executor(
                None, trader.place_order,
                symbol, direction,
                sig.get('entry', 0), sig.get('sl', 0),
                sig.get('tp1', 0), sig.get('tp2', 0),
                None, quality, sig, 'NEUTRAL',
            )

            if result_order and result_order.get('ok'):
                ico = "🟢" if direction == "LONG" else "🔴"
                risk_usd = self.cfg['risk_per_trade_usd']
                notif = (
                    "SCALP AUTO TRADE\n" +
                    "=" * 28 + "\n" +
                    f"{ico} {symbol} {direction} [{quality}]\n"
                    f"Entry : {sig.get('entry', 0):.6g}\n"
                    f"SL    : {sig.get('sl', 0):.6g}\n"
                    f"TP1   : {sig.get('tp1', 0):.6g}\n"
                    f"TP2   : {sig.get('tp2', 0):.6g}\n"
                    f"Risk  : ${risk_usd}\n\n"
                    "TP1 monitor aktif"
                )
                for cid in list(self.tg.chat_ids):
                    await self.tg._safe_send(cid, notif)

                # Start TP1 monitor
                trader.start_tp1_monitor(
                    symbol=symbol,
                    entry=sig.get('entry', 0),
                    tp1=sig.get('tp1', 0),
                    direction=direction,
                    notify_fn=self.tg._make_notify_fn(),
                    level_price=sig.get('level_price', 0.0),
                )

        except Exception as e:
            logger.error(f"Scalp auto execute {symbol}: {e}")

    def _trigger_async(self, coro):
        """Jalankan coroutine dari thread biasa."""
        def run():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(coro)
                loop.close()
            except Exception as e:
                logger.error(f"Async trigger error: {e}")
        threading.Thread(target=run, daemon=True).start()


# =============================================
if __name__ == "__main__":
    bot = ScalpingBot()
    bot.run()
