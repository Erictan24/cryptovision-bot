"""
main_unified.py — Unified Bot Entry Point

Single command untuk jalankan kedua bot bersamaan:
  - Bot 1 SWING (1H) — main.py engine
  - Bot 2 SCALP (15m) — main_scalp.py engine

Dengan fitur anti-tabrakan:
  - Shared position manager
  - Signal arbitrator (conflict resolver)
  - SWING priority mode (default)
  - Global risk limits (max 4 positions, $10 daily loss)

Usage:
  python main_unified.py

Config di config.py → UNIFIED_CONFIG.
"""

import sys
import io
import logging
import time
import asyncio
import threading
from datetime import datetime

# Windows UTF-8 safe
if hasattr(sys.stdout, 'buffer') and not sys.stdout.closed:
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

# Core imports
from database import DatabaseManager
from trading_engine import TradingEngine
from trading_engine_scalp import ScalpingEngine
from risk_manager import RiskManager
from telegram_bot import TelegramBot
from config import SCALP_CONFIG, UNIFIED_CONFIG, DAILY_SIGNAL

# Unified modules
from position_manager import get_position_manager
from signal_arbitrator import get_arbitrator

# Learning modules (optional)
try:
    import scalp_trade_journal as journal
    import scalp_coin_learning as coin_learn
    import scalp_session_filter as session_filter
    _LEARNING_AVAILABLE = True
except ImportError:
    _LEARNING_AVAILABLE = False

# Paper trader (Level 1)
try:
    from scalp_paper_trader import PaperTrader
    _PAPER_AVAILABLE = True
except ImportError:
    _PAPER_AVAILABLE = False

# Chart
try:
    from chart_generator import generate_signal_chart, cleanup_chart
    _CHART_GEN = True
except ImportError:
    _CHART_GEN = False


# Logging — file terpisah untuk unified
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler('bot_unified.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class UnifiedBot:
    """
    Unified bot yang handle Bot 1 SWING + Bot 2 SCALP.

    Arsitektur:
      - Shared: TradingEngine, TelegramBot, BitunixTrader
      - Swing engine: trading_engine.analyze_coin (via TelegramBot.auto_execute)
      - Scalp engine: ScalpingEngine
      - Arbitrator: mencegah signal tabrakan
      - Position manager: single source of truth
    """

    def __init__(self):
        logger.info("=" * 60)
        logger.info(" UNIFIED BOT — Bot 1 (SWING) + Bot 2 (SCALP)")
        logger.info("=" * 60)
        logger.info("Inisialisasi...")

        # Core services (shared)
        self.db = DatabaseManager()
        self.engine = TradingEngine()
        self.scalp_engine = ScalpingEngine(self.engine)
        self.risk = RiskManager(self.db)
        self.tg = TelegramBot(self.db)
        self.tg.engine = self.engine
        self.tg.risk = self.risk

        # Unified config
        self.unified_cfg = UNIFIED_CONFIG
        self.scalp_cfg = SCALP_CONFIG

        # Position manager + arbitrator (shared state)
        self.pm = get_position_manager()
        self.arbitrator = get_arbitrator(UNIFIED_CONFIG)

        # Paper trader (untuk Level 1 paper mode)
        self._paper_mode = self.scalp_cfg.get('paper_mode', False)
        self.paper_trader = None
        if self._paper_mode and _PAPER_AVAILABLE:
            self.paper_trader = PaperTrader(
                risk_usd=self.scalp_cfg.get('risk_per_trade_usd', 1.0),
                max_positions=self.scalp_cfg.get('max_positions', 3),
            )
            logger.info("PAPER TRADE MODE AKTIF (Level 1)")

        # State
        self._scalp_scan_running = False
        self._start_time = datetime.now()
        self._last_daily_reset = datetime.now().date()
        self._daily_sent_today = None
        self._scan_start_time = datetime.now()
        self._total_signals = 0
        self._consecutive_errors = 0

        # Learning init
        if _LEARNING_AVAILABLE:
            try:
                n_trades = journal.count_trades()
                logger.info(f"Learning modules loaded — "
                            f"{n_trades} trades in journal")
            except Exception as e:
                logger.warning(f"Learning init: {e}")

        logger.info("UnifiedBot siap!")

    def run(self):
        """Main run loop."""
        logger.info("=" * 60)
        logger.info(" RUNNING MODE")
        logger.info("=" * 60)
        logger.info(f"SWING enabled: {self.unified_cfg['swing_enabled']}")
        logger.info(f"SCALP enabled: {self.unified_cfg['scalp_enabled']}")
        logger.info(f"Paper mode: {self._paper_mode}")
        logger.info(f"Max positions: {self.unified_cfg['global_max_positions']}")
        logger.info(f"Daily loss limit: ${self.unified_cfg['global_max_daily_loss_usd']}")
        logger.info(f"Conflict mode: {self.unified_cfg['conflict_mode']}")

        # Start Telegram bot
        self.tg.start_bot()

        # Start SWING scheduler (Bot 1)
        if self.unified_cfg.get('swing_enabled'):
            self._start_swing_scheduler()

        # Start SCALP scan (Bot 2)
        if self.unified_cfg.get('scalp_enabled'):
            self._start_scalp_scan()

        # Shared schedulers
        if self._paper_mode and self.paper_trader:
            self._start_paper_monitor()

        if _LEARNING_AVAILABLE:
            self._start_weekly_learning_refresh()

        self._start_daily_summary()
        self._start_daily_reset()

        # Startup notification
        self._trigger_async(self._send_startup_notification())

        logger.info("Unified bot running. Ctrl+C to stop.\n")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("\nShutdown by user")
            self._trigger_async(self._broadcast("Unified bot shutdown manual"))
            time.sleep(2)
            self.tg.stop_bot()
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            self._trigger_async(self._broadcast(
                f"Unified bot CRASHED: {str(e)[:200]}"))
            time.sleep(2)
            self.tg.stop_bot()

    async def _send_startup_notification(self):
        msg = (
            "UNIFIED BOT STARTED\n"
            + "=" * 28 + "\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"SWING: {'ON' if self.unified_cfg['swing_enabled'] else 'OFF'}\n"
            f"SCALP: {'ON' if self.unified_cfg['scalp_enabled'] else 'OFF'}\n"
            f"Mode: {'PAPER' if self._paper_mode else 'LIVE'}\n"
            f"Max positions: {self.unified_cfg['global_max_positions']}\n"
            f"Daily limit: ${self.unified_cfg['global_max_daily_loss_usd']}\n"
            f"Whitelist SCALP: {len(self.scalp_cfg['scalp_whitelist'])} coins\n"
            f"Conflict mode: {self.unified_cfg['conflict_mode']}"
        )
        await self._broadcast(msg)

    async def _broadcast(self, text: str):
        for cid in list(self.tg.chat_ids):
            try:
                await self.tg._safe_send(cid, text)
            except Exception:
                pass

    # ══════════════════════════════════════════════════
    #  SWING SCHEDULER (Bot 1)
    # ══════════════════════════════════════════════════
    def _start_swing_scheduler(self):
        """
        Bot 1 SWING scan — 30 menit interval.
        Reuse telegram_bot._auto_execute_best_signal yang sudah ada.
        """
        self._scan_start_time = datetime.now()
        STARTUP_GRACE = 120  # 2 menit grace
        SCAN_INTERVAL = 1800  # 30 menit

        def scan_loop():
            time.sleep(STARTUP_GRACE)
            logger.info("SWING auto scan dimulai (setiap 30 menit)")

            while True:
                try:
                    logger.info("[SWING] Auto scan...")
                    # Note: _auto_execute_best_signal tidak tahu arbitrator.
                    # Untuk sekarang, kita trust default checks di bot 1.
                    # Future: intercept signals sebelum execute.
                    self._trigger_async(
                        self.tg._auto_execute_best_signal())
                except Exception as e:
                    logger.error(f"SWING scan error: {e}", exc_info=True)
                time.sleep(SCAN_INTERVAL)

        t = threading.Thread(target=scan_loop, daemon=True)
        t.start()
        logger.info("SWING scheduler aktif (30 menit)")

        # Daily signal scheduler
        def daily_loop():
            while True:
                try:
                    now = datetime.now()
                    target_h = DAILY_SIGNAL.get('hour', 8)
                    target_m = DAILY_SIGNAL.get('minute', 0)
                    if (now.hour == target_h and
                            now.minute >= target_m and
                            now.minute < target_m + 5 and
                            self._daily_sent_today != now.date()):
                        logger.info("SWING daily digest...")
                        self._daily_sent_today = now.date()
                        self._trigger_async(self.tg._auto_daily_scan())
                except Exception as e:
                    logger.error(f"Daily scheduler error: {e}")
                time.sleep(30)

        t2 = threading.Thread(target=daily_loop, daemon=True)
        t2.start()

    # ══════════════════════════════════════════════════
    #  SCALP SCHEDULER (Bot 2)
    # ══════════════════════════════════════════════════
    def _start_scalp_scan(self):
        interval = self.scalp_cfg['scan_interval_minutes'] * 60
        startup_grace = 30

        def scan_loop():
            time.sleep(startup_grace)
            logger.info(f"SCALP scan dimulai (setiap "
                        f"{self.scalp_cfg['scan_interval_minutes']} menit)")

            while True:
                try:
                    self._check_daily_reset()

                    if self.arbitrator._daily_loss_hit:
                        logger.warning("Daily loss hit, SCALP skip")
                        time.sleep(interval)
                        continue

                    self._run_one_scalp_scan()
                    self._consecutive_errors = 0

                except Exception as e:
                    self._consecutive_errors += 1
                    logger.error(f"SCALP scan error #{self._consecutive_errors}: "
                                 f"{e}", exc_info=True)
                    if self._consecutive_errors >= 5:
                        self._trigger_async(self._broadcast(
                            f"SCALP ERROR x{self._consecutive_errors}\n"
                            f"{str(e)[:200]}"))
                        time.sleep(interval * 2)
                        continue

                time.sleep(interval)

        t = threading.Thread(target=scan_loop, daemon=True)
        t.start()
        logger.info(f"SCALP scheduler aktif "
                    f"({self.scalp_cfg['scan_interval_minutes']} menit)")

    def _check_daily_reset(self):
        today = datetime.now().date()
        if today != self._last_daily_reset:
            logger.info(f"Daily reset — new day {today}")
            self.arbitrator.reset_daily()
            self._last_daily_reset = today

    def _run_one_scalp_scan(self):
        if self._scalp_scan_running:
            return
        self._scalp_scan_running = True

        try:
            start_ts = time.time()
            logger.info("[SCALP] Scan dimulai...")

            btc = self.scalp_engine.check_btc_bias()
            btc_bias = btc.get('btc_bias', 'NEUTRAL')
            logger.info(f"[SCALP] BTC bias: {btc_bias}")

            signals = self.scalp_engine.scan_all_coins(max_signals=10)
            self._total_signals += len(signals)

            elapsed = time.time() - start_ts
            logger.info(f"[SCALP] {len(signals)} signal ({elapsed:.1f}s)")

            if not signals:
                return

            self._trigger_async(self._process_scalp_signals(signals))

        finally:
            self._scalp_scan_running = False

    async def _process_scalp_signals(self, signals: list):
        """Process SCALP signals dengan arbitrator check."""
        for item in signals:
            try:
                symbol = item['symbol']
                result = item['result']
                sig = item['signal']
                sig['_symbol'] = symbol

                # ═══════════════════════════════════════
                # ARBITRATOR CHECK — mencegah tabrakan
                # ═══════════════════════════════════════
                verdict = self.arbitrator.check_signal(sig, 'SCALP')

                if not verdict.allow:
                    logger.info(f"[SCALP] {symbol} REJECTED: {verdict.reason}")
                    continue

                # Format + kirim ke Telegram
                ico = "🟢" if sig['direction'] == "LONG" else "🔴"
                text = self._format_scalp_signal(symbol, sig, result, ico)
                if self._paper_mode:
                    text = "[PAPER MODE]\n" + text

                for cid in list(self.tg.chat_ids):
                    await self._send_with_chart(
                        cid, symbol, result, sig, text, tf='15m')

                # Execute — paper atau live
                if self._paper_mode and self.paper_trader:
                    trade_id = self.paper_trader.open_paper_trade(sig)
                    if trade_id:
                        # Record ke position manager
                        self.pm.open_position(
                            engine='SCALP',
                            mode='PAPER',
                            symbol=symbol,
                            direction=sig['direction'],
                            entry_price=sig.get('entry', 0),
                            sl=sig.get('sl', 0),
                            tp1=sig.get('tp1', 0),
                            tp2=sig.get('tp2', 0),
                            tp3=sig.get('tp3', 0),
                            risk_usd=self.scalp_cfg.get('risk_per_trade_usd', 1.0),
                            quality=sig.get('quality', 'GOOD'),
                            external_id=f'paper_{trade_id}',
                        )
                        await self._broadcast(
                            f"📝 [SCALP PAPER] OPENED #{trade_id}\n"
                            f"{symbol} {sig['direction']} [{sig.get('quality', 'GOOD')}]\n"
                            f"Entry: {sig.get('entry', 0):.6g}\n"
                            f"SL: {sig.get('sl', 0):.6g}\n"
                            f"Risk: ${self.scalp_cfg.get('risk_per_trade_usd', 1)} (simulated)"
                        )
                elif (self.scalp_cfg.get('auto_trade_enabled') and
                      self.tg.trader and self.tg.trader.is_ready):
                    # LIVE — TODO: integrate dengan position manager
                    logger.info(f"[SCALP LIVE] {symbol} execute")
                    # Reuse _auto_execute_scalp dari main_scalp if needed
                    pass

            except Exception as e:
                logger.error(f"Process scalp {item.get('symbol', '?')}: {e}")

    def _format_scalp_signal(self, symbol: str, sig: dict,
                             result: dict, ico: str) -> str:
        direction = sig['direction']
        lines = [
            f"{ico} [SCALP] {symbol} {direction} [{sig.get('quality', 'GOOD')}]",
            "=" * 30,
            f"Entry : {sig.get('entry', 0):.6g}",
            f"SL    : {sig.get('sl', 0):.6g} ({sig.get('sl_pct', 0):.1f}%)",
            f"TP1   : {sig.get('tp1', 0):.6g} (RR 1:{sig.get('rr1', 0)})",
            f"TP2   : {sig.get('tp2', 0):.6g} (RR 1:{sig.get('rr2', 0)})",
            f"TP3   : {sig.get('tp3', 0):.6g}",
            "",
            f"Trend 1H: {sig.get('trend_state', '-')}",
            f"Pullback: {sig.get('pullback_quality', '-')}",
            f"Macro 4H: {sig.get('macro_4h_bias', '-')}",
            f"Session : {sig.get('session', '-')}",
            f"Score: {sig.get('confluence_score', 0)}",
            "",
        ]
        reasons = sig.get('reasons', [])[:5]
        if reasons:
            lines.append("Reasons:")
            for r in reasons:
                lines.append(f"  - {r}")
        return "\n".join(lines)

    async def _send_with_chart(self, chat_id, symbol: str,
                               result: dict, sig: dict, text: str,
                               tf: str = '15m'):
        """Kirim signal + chart."""
        chart_path = None
        if _CHART_GEN:
            try:
                loop = asyncio.get_event_loop()
                df = await loop.run_in_executor(
                    None, self.engine.get_klines, symbol, tf, False, False)
                if df is not None:
                    chart_path = await loop.run_in_executor(
                        None, generate_signal_chart,
                        df, symbol, tf, sig,
                        None, None, None,
                        result.get('structure', 'SIDEWAYS'),
                        None, None,
                    )
            except Exception as e:
                logger.debug(f"Chart gen {symbol}: {e}")

        if chart_path:
            try:
                with open(chart_path, 'rb') as f_img:
                    caption = text[:1024]
                    await self.tg.app.bot.send_photo(
                        chat_id=chat_id, photo=f_img,
                        caption=caption, parse_mode=None)
                cleanup_chart(chart_path)
                return
            except Exception as e:
                logger.debug(f"Send photo {symbol}: {e}")
                cleanup_chart(chart_path)

        await self.tg._safe_send(chat_id, text)

    # ══════════════════════════════════════════════════
    #  PAPER MONITOR
    # ══════════════════════════════════════════════════
    def _start_paper_monitor(self):
        """Monitor open paper trades, check TP/SL hit."""
        def monitor_loop():
            time.sleep(60)
            logger.info("Paper monitor aktif (2 menit interval)")
            while True:
                try:
                    def fetch_price(sym):
                        try:
                            return self.engine.get_price(sym)
                        except Exception:
                            return None

                    closed = self.paper_trader.monitor_all_open(fetch_price)

                    for c in closed:
                        pnl_r = c.get('pnl_r', 0)
                        pnl_usd = pnl_r * self.scalp_cfg.get('risk_per_trade_usd', 1.0)

                        # Update position manager
                        # Find position by external_id
                        # (paper trader uses paper_X as external_id)
                        # TODO: proper sync

                        # Arbitrator track loss
                        self.arbitrator.record_loss(pnl_usd)

                        emoji = '🟢' if pnl_r > 0 else '🔴' if pnl_r < 0 else '⚪'
                        msg = (
                            f"[SCALP PAPER] CLOSED #{c['trade_id']}\n"
                            + "=" * 28 + "\n"
                            f"{emoji} {c['symbol']} {c['direction']}\n"
                            f"Outcome: {c['outcome']}\n"
                            f"PnL: {pnl_r:+.2f}R (${pnl_usd:+.2f})"
                        )
                        self._trigger_async(self._broadcast(msg))

                except Exception as e:
                    logger.error(f"Paper monitor error: {e}")

                time.sleep(120)

        t = threading.Thread(target=monitor_loop, daemon=True)
        t.start()

    # ══════════════════════════════════════════════════
    #  SCHEDULERS
    # ══════════════════════════════════════════════════
    def _start_weekly_learning_refresh(self):
        """Refresh learning setiap Senin 02:00."""
        self._last_refresh = datetime.now()

        def loop():
            while True:
                try:
                    now = datetime.now()
                    if (now.weekday() == 0 and 2 <= now.hour < 3 and
                            (now - self._last_refresh).total_seconds() > 3600):
                        logger.info("Weekly learning refresh...")
                        from config import SCAN_POOL
                        learning = coin_learn.get_learning()
                        n = learning.refresh(SCAN_POOL[:30])
                        gate = session_filter.get_session_gate()
                        gate.refresh()
                        self._last_refresh = now
                        self._trigger_async(self._broadcast(
                            f"Weekly learning refresh\nUpdated {n} coins"))
                except Exception as e:
                    logger.error(f"Learning scheduler: {e}")
                time.sleep(600)

        t = threading.Thread(target=loop, daemon=True)
        t.start()

    def _start_daily_summary(self):
        """Daily summary jam 23:00."""
        self._last_summary = None

        def loop():
            while True:
                try:
                    now = datetime.now()
                    if (now.hour == 23 and now.minute < 5 and
                            self._last_summary != now.date()):
                        self._last_summary = now.date()
                        self._trigger_async(self._send_daily_summary())
                except Exception as e:
                    logger.error(f"Daily summary: {e}")
                time.sleep(60)

        t = threading.Thread(target=loop, daemon=True)
        t.start()

    def _start_daily_reset(self):
        """Reset counter jam 00:00."""
        def loop():
            while True:
                try:
                    self._check_daily_reset()
                except Exception:
                    pass
                time.sleep(60)

        t = threading.Thread(target=loop, daemon=True)
        t.start()

    async def _send_daily_summary(self):
        try:
            uptime = datetime.now() - self._start_time
            uptime_hours = uptime.total_seconds() / 3600

            arb_status = self.arbitrator.get_status_summary()

            paper_stats = ""
            if self._paper_mode and self.paper_trader:
                paper_stats = "\n" + self.paper_trader.format_stats_msg()

            recent = journal.get_recent_summary(days=1) if _LEARNING_AVAILABLE else {
                'n_trades': 0, 'wr': 0, 'ev_r': 0, 'total_pnl_r': 0
            }

            mode = "[PAPER]" if self._paper_mode else "[LIVE]"
            msg = (
                f"DAILY SUMMARY {mode}\n"
                + "=" * 28 + "\n"
                f"Date: {datetime.now().strftime('%Y-%m-%d')}\n"
                f"Uptime: {uptime_hours:.1f}h\n"
                f"Signals total: {self._total_signals}\n"
                f"\n{arb_status}"
                f"{paper_stats}"
            )
            await self._broadcast(msg)
            logger.info("Daily summary sent")
        except Exception as e:
            logger.error(f"Daily summary failed: {e}")

    def _trigger_async(self, coro):
        """Run coroutine from thread."""
        def run():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(coro)
                loop.close()
            except Exception as e:
                logger.error(f"Async trigger: {e}")
        threading.Thread(target=run, daemon=True).start()


# ══════════════════════════════════════════════════
if __name__ == "__main__":
    bot = UnifiedBot()
    bot.run()
