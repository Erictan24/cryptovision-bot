import sys
import io
import logging
import time
import asyncio
import threading
from datetime import datetime, timedelta

# Paksa stdout UTF-8 agar emoji tidak error di terminal Windows
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from database       import DatabaseManager
from trading_engine import TradingEngine
from risk_manager   import RiskManager
from telegram_bot   import TelegramBot
from config         import DAILY_SIGNAL

# =============================================
# SETUP LOGGING
# =============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class CryptoSignalBot:
    def __init__(self):
        logger.info("Inisialisasi bot...")
        self.db         = DatabaseManager()
        self.engine     = TradingEngine()
        self.risk       = RiskManager(self.db)
        self.tg         = TelegramBot(self.db)
        self.tg.engine  = self.engine
        self.tg.risk    = self.risk
        logger.info("✅ Bot siap!")

    def run(self):
        logger.info("🚀 Bot mulai berjalan!")
        logger.info("   Mode: Auto Scan 30 menit + Daily Signal jam 08:00")
        logger.info("   Tekan Ctrl+C untuk stop\n")

        # Start Telegram bot
        self.tg.start_bot()

        # Start scheduler: daily signal jam 08:00
        self._start_daily_scheduler()

        # Start auto scan setiap 30 menit
        self._start_auto_scan()

        # Start weekly auto-tune (Senin dini hari)
        self._start_weekly_autotune()

        # Start scalp paper trade scanner (15 menit interval)
        self._start_scalp_paper()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("\n🛑 Bot dihentikan.")
            self.tg.stop_bot()
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            self.tg.stop_bot()

    # ==================================================================
    # DAILY SCHEDULER — kirim digest signal jam 08:00
    # ==================================================================
    def _start_daily_scheduler(self):
        """Kirim daily digest signal jam 08:00 setiap hari."""
        self._daily_sent_today = None

        def scheduler_loop():
            while True:
                try:
                    now = datetime.now()
                    target_h = DAILY_SIGNAL.get('hour', 8)
                    target_m = DAILY_SIGNAL.get('minute', 0)

                    if (now.hour == target_h and
                        now.minute >= target_m and
                        now.minute < target_m + 5 and
                        self._daily_sent_today != now.date()):

                        logger.info("🔔 Daily digest signal!")
                        self._daily_sent_today = now.date()
                        self._trigger_async(self.tg._auto_daily_scan())

                except Exception as e:
                    logger.error(f"Daily scheduler error: {e}")

                time.sleep(30)

        t = threading.Thread(target=scheduler_loop, daemon=True)
        t.start()
        logger.info(f"📅 Daily scheduler: jam {DAILY_SIGNAL.get('hour',8):02d}:00")

    # ==================================================================
    # AUTO SCAN — scan + execute trade setiap 30 menit
    # ==================================================================
    def _start_auto_scan(self):
        """
        Scan semua coin setiap 30 menit.
        Kalau ada signal GOOD/IDEAL → execute trade otomatis.
        """
        self._scan_start_time = datetime.now()
        STARTUP_GRACE = 120  # 2 menit setelah start baru boleh trade
        SCAN_INTERVAL = 1800  # scan setiap 30 menit

        def scan_loop():
            # Tunggu 2 menit dulu sebelum scan pertama
            time.sleep(STARTUP_GRACE)
            logger.info("⚡ Auto scan dimulai (setiap 30 menit)")

            while True:
                try:
                    elapsed = (datetime.now() - self._scan_start_time).total_seconds()
                    logger.info(f"🔍 Auto scan coin...")
                    self._trigger_async(self.tg._auto_execute_best_signal())
                except Exception as e:
                    logger.error(f"Auto scan error: {e}", exc_info=True)

                # Tunggu 30 menit
                time.sleep(SCAN_INTERVAL)

        t = threading.Thread(target=scan_loop, daemon=True)
        t.start()
        logger.info("⚡ Auto scan scheduler: setiap 30 menit")

    # ==================================================================
    # WEEKLY AUTO-TUNE — setiap Senin jam 02:00 pagi
    # ==================================================================
    def _start_weekly_autotune(self):
        """Jalankan auto-tune parameter setiap Senin jam 02:00 pagi."""
        self._autotune_last_week = None

        def autotune_loop():
            while True:
                try:
                    now = datetime.now()
                    # Senin = weekday 0, jam 02:00-02:05
                    if (now.weekday() == 0 and
                        now.hour == 2 and
                        now.minute < 5 and
                        self._autotune_last_week != now.isocalendar()[1]):

                        self._autotune_last_week = now.isocalendar()[1]
                        logger.info("🔧 Weekly auto-tune dimulai...")

                        # 1. Parameter tuning dari backtest
                        try:
                            from backtesting.auto_tune import run_auto_tune
                            from config import SCAN_POOL
                            result = run_auto_tune(coins=SCAN_POOL[:10], days=30, quiet=True)
                            wr  = result.get('wr', 0)
                            adj = result.get('adjusted', [])
                            logger.info(f"✅ Auto-tune selesai — WR={wr:.0%}, {len(adj)} parameter diubah")
                        except Exception as e:
                            logger.error(f"Auto-tune error: {e}")

                        # 2. SL post-mortem — belajar pola SL dari semua data
                        try:
                            from backtesting.sl_postmortem import run_and_save
                            run_and_save(apply=True)
                            logger.info("✅ SL post-mortem diperbarui")
                            import signal_generator as _sg
                            _sg._load_sl_patterns()
                        except Exception as e2:
                            logger.error(f"SL post-mortem error: {e2}")

                except Exception as e:
                    logger.error(f"Weekly autotune scheduler error: {e}")

                time.sleep(60)  # cek setiap 1 menit

        t = threading.Thread(target=autotune_loop, daemon=True)
        t.start()
        logger.info("🔧 Weekly auto-tune scheduler: Senin jam 02:00")

    # ==================================================================
    # SCALP PAPER TRADE — scan 15m, track TP/SL tanpa eksekusi
    # ==================================================================
    def _start_scalp_paper(self):
        """
        Start scalp live scan (15 menit interval).
        Eksekusi order real ke Bitunix kalau trader siap.
        """
        try:
            from scalp_live_runner import start_scalp_live
            import os

            def coins_fn():
                try:
                    return self.tg.engine.get_top_coins(100)
                except Exception:
                    from config import SCAN_POOL
                    return list(SCAN_POOL)[:30]

            # Delay init agar telegram bot siap dulu
            def delayed_start():
                time.sleep(30)
                risk_usd = float(os.getenv('TRADE_RISK_USD', '1.0'))
                start_scalp_live(
                    coins_fn=coins_fn,
                    notify_fn=self.tg._make_notify_fn(),
                    risk_usd=risk_usd,
                    trader=self.tg.trader,
                )

            threading.Thread(target=delayed_start, daemon=True).start()
            logger.info("📊 Scalp live scan scheduler: 15 menit scan")
        except Exception as e:
            logger.error(f"Gagal start scalp paper: {e}", exc_info=True)

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

    def _trigger_auto_daily(self):
        self._trigger_async(self.tg._auto_daily_scan())


# =============================================
if __name__ == "__main__":
    bot = CryptoSignalBot()
    bot.run()