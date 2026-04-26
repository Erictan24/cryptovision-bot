"""
Telegram Bot v9 — Safe Markdown + Manual-Only Signals
=====================================================
NO auto-monitor spam. Signals only when user requests.
Safe Markdown: fallback to plain text on parse error.
"""

import logging
import asyncio
import threading
import functools
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
try:
    from bitunix_trader import BitunixTrader
except ImportError:
    BitunixTrader = None
from config import TELEGRAM_BOT_TOKEN, DAILY_SIGNAL
from whale_analyzer import WhaleAnalyzer
from trading_engine import resolve_tf, VALID_TFS

logger = logging.getLogger(__name__)

# Chart generator
try:
    from chart_generator import generate_signal_chart, cleanup_chart
    _CHART_GEN = True
except ImportError:
    _CHART_GEN = False
TF_LIST_STR = ", ".join(VALID_TFS)

QUALITY_RANK = {'IDEAL': 4, 'GOOD': 3, 'MODERATE': 2, 'WAIT': 1, 'LIMIT': 0}


import json
import os

class TradeTracker:
    """
    Track signals yang diberikan, pantau apakah TP1/TP2/SL hit.
    Simpan ke file JSON untuk persistence.
    """
    DATA_FILE = 'data/trade_history.json'

    def __init__(self):
        self.trades = []
        self._load()

    def _load(self):
        try:
            os.makedirs('data', exist_ok=True)
            if os.path.exists(self.DATA_FILE):
                with open(self.DATA_FILE, 'r') as f:
                    self.trades = json.load(f)
        except Exception:
            self.trades = []

    def _save(self):
        try:
            os.makedirs('data', exist_ok=True)
            with open(self.DATA_FILE, 'w') as f:
                json.dump(self.trades, f, indent=2)
        except Exception:
            pass

    def record_signal(self, symbol, direction, quality, entry, sl, tp1, tp2,
                      confluence_score=0, rr1=0, rr2=0):
        """Record signal yang diberikan ke user."""
        trade = {
            'id': len(self.trades) + 1,
            'symbol': symbol,
            'direction': direction,
            'quality': quality,
            'entry': entry,
            'sl': sl,
            'tp1': tp1,
            'tp2': tp2,
            'confluence': confluence_score,
            'rr1': rr1,
            'rr2': rr2,
            'timestamp': datetime.now().isoformat(),
            'status': 'OPEN',      # OPEN, TP1_HIT, TP2_HIT, SL_HIT, EXPIRED
            'result_pnl': 0,       # in R (risk units)
            'closed_at': None,
        }
        self.trades.append(trade)
        self._save()
        return trade['id']

    def check_trades(self, engine):
        """Cek semua trade OPEN — apakah TP/SL sudah hit."""
        updated = []
        for t in self.trades:
            if t['status'] != 'OPEN':
                continue
            try:
                pd_ = engine.get_price(t['symbol'])
                if not pd_:
                    continue
                price = pd_['price']
                d = t['direction']

                if d == 'LONG':
                    if price <= t['sl']:
                        t['status'] = 'SL_HIT'
                        t['result_pnl'] = -1.0
                        t['closed_at'] = datetime.now().isoformat()
                        updated.append(t)
                    elif price >= t['tp2']:
                        t['status'] = 'TP2_HIT'
                        t['result_pnl'] = t['rr2']
                        t['closed_at'] = datetime.now().isoformat()
                        updated.append(t)
                    elif price >= t['tp1'] and t['status'] == 'OPEN':
                        t['status'] = 'TP1_HIT'
                        t['result_pnl'] = t['rr1'] * 0.5  # 50% closed at TP1
                        updated.append(t)
                else:  # SHORT
                    if price >= t['sl']:
                        t['status'] = 'SL_HIT'
                        t['result_pnl'] = -1.0
                        t['closed_at'] = datetime.now().isoformat()
                        updated.append(t)
                    elif price <= t['tp2']:
                        t['status'] = 'TP2_HIT'
                        t['result_pnl'] = t['rr2']
                        t['closed_at'] = datetime.now().isoformat()
                        updated.append(t)
                    elif price <= t['tp1'] and t['status'] == 'OPEN':
                        t['status'] = 'TP1_HIT'
                        t['result_pnl'] = t['rr1'] * 0.5
                        updated.append(t)

                # Expire old trades (> 72 hours)
                if t['status'] == 'OPEN':
                    from datetime import datetime as dt
                    try:
                        age = (dt.now() - dt.fromisoformat(t['timestamp'])).total_seconds() / 3600
                        if age > 72:
                            t['status'] = 'EXPIRED'
                            t['result_pnl'] = 0
                            t['closed_at'] = datetime.now().isoformat()
                    except Exception:
                        pass
            except Exception:
                continue
        if updated:
            self._save()
        return updated

    def get_stats(self):
        """Hitung performance statistics."""
        closed = [t for t in self.trades if t['status'] in ('TP1_HIT', 'TP2_HIT', 'SL_HIT')]
        if not closed:
            return None

        total = len(closed)
        wins = len([t for t in closed if t['result_pnl'] > 0])
        losses = len([t for t in closed if t['result_pnl'] < 0])
        wr = (wins / total * 100) if total > 0 else 0

        total_pnl = sum(t['result_pnl'] for t in closed)
        avg_win = 0
        avg_loss = 0
        if wins > 0:
            avg_win = sum(t['result_pnl'] for t in closed if t['result_pnl'] > 0) / wins
        if losses > 0:
            avg_loss = sum(t['result_pnl'] for t in closed if t['result_pnl'] < 0) / losses

        # By quality
        by_quality = {}
        for q in ('IDEAL', 'GOOD', 'MODERATE'):
            q_trades = [t for t in closed if t['quality'] == q]
            if q_trades:
                q_wins = len([t for t in q_trades if t['result_pnl'] > 0])
                by_quality[q] = {
                    'total': len(q_trades),
                    'wins': q_wins,
                    'wr': round(q_wins / len(q_trades) * 100, 1)
                }

        # Best/worst coins
        coin_pnl = {}
        for t in closed:
            s = t['symbol']
            if s not in coin_pnl:
                coin_pnl[s] = 0
            coin_pnl[s] += t['result_pnl']

        best_coin = max(coin_pnl, key=coin_pnl.get) if coin_pnl else '-'
        worst_coin = min(coin_pnl, key=coin_pnl.get) if coin_pnl else '-'

        open_trades = len([t for t in self.trades if t['status'] == 'OPEN'])

        return {
            'total_trades': total,
            'open_trades': open_trades,
            'wins': wins,
            'losses': losses,
            'win_rate': round(wr, 1),
            'total_pnl_r': round(total_pnl, 2),
            'avg_win_r': round(avg_win, 2),
            'avg_loss_r': round(avg_loss, 2),
            'by_quality': by_quality,
            'best_coin': best_coin,
            'worst_coin': worst_coin,
            'best_pnl': round(coin_pnl.get(best_coin, 0), 2),
            'worst_pnl': round(coin_pnl.get(worst_coin, 0), 2),
        }

    def get_recent(self, n=10):
        """Last N trades."""
        return list(reversed(self.trades[-n:]))


class TelegramBot:
    def __init__(self, database_manager):
        self.db       = database_manager
        self.engine   = None
        self.risk     = None
        self.whale    = WhaleAnalyzer()
        # Auto trader (Bitunix)
        self.trader = None
        if BitunixTrader:
            try:
                self.trader = BitunixTrader()
                if self.trader.is_ready:
                    logger.info("✅ BitunixTrader siap")
                    # Resume TP1 monitor untuk posisi yang ada sebelum restart
                    import threading
                    def _resume():
                        import time; time.sleep(5)  # tunggu 5 detik biar koneksi stabil
                        self.trader.resume_monitors_on_startup(notify_fn=self._make_notify_fn())
                    threading.Thread(target=_resume, daemon=True).start()
            except Exception as te:
                logger.warning(f"BitunixTrader init error: {te}")
                self.trader = None
        self.chat_ids       = set()
        # Admin selalu masuk chat_ids agar dapat notif walau belum kirim /start
        _admin_id = os.getenv('ADMIN_TELEGRAM_ID', '')
        if _admin_id:
            try:
                self.chat_ids.add(int(_admin_id))
            except ValueError:
                pass
        self.app            = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self._daily_running = False
        self.tracker        = TradeTracker()
        self._sent_signals  = {}   # {trade_key: datetime} anti-duplicate 4 jam
        self._watch_alerts  = {}   # {trade_key: datetime} cooldown 2 jam untuk WAIT alert
        self._start_time    = datetime.now()
        self._startup_grace = 120  # 2 menit grace setelah restart

        cmds = [
            ("start",     self.cmd_start),
            ("help",      self.cmd_help),
            ("test",      self.cmd_test),
            ("analyze",   self.cmd_analyze),
            ("signal",    self.cmd_signal),
            ("chart",     self.cmd_chart),
            ("market",    self.cmd_market),
            ("sr",        self.cmd_sr),
            ("zones",     self.cmd_zones),
            ("whale",     self.cmd_whale),
            ("daily",     self.cmd_daily),
            ("stats",     self.cmd_stats),
            ("trades",    self.cmd_trades),
            ("trade",     self.cmd_trade),
            ("positions", self.cmd_positions),
            ("monitor",   self.cmd_monitor),
            ("close",     self.cmd_close),
            ("monthly",   self.cmd_monthly),
            ("news",      self.cmd_news),
            ("learn",     self.cmd_learn),
            ("train",     self.cmd_train),
            ("verify",    self.cmd_verify),
            ("grant",     self.cmd_grant),
            ("reset_pnl", self.cmd_reset_pnl),
            ("scalp_stats", self.cmd_scalp_stats),
        ]
        for name, handler in cmds:
            self.app.add_handler(CommandHandler(name, handler))

    # ==================================================================
    # SAFE MARKDOWN HELPERS
    # ==================================================================
    def _esc(self, text):
        """Remove Markdown special chars from dynamic text to prevent parse errors."""
        if not isinstance(text, str):
            text = str(text)
        for ch in ['*', '_', '`', '[', ']', '~']:
            text = text.replace(ch, '')
        return text

    async def _safe_edit(self, msg, text):
        """Edit message — try Markdown first, fallback to plain text."""
        try:
            await msg.edit_text(text, parse_mode="Markdown")
        except Exception as e1:
            try:
                clean = text.replace('*', '').replace('_', '').replace('`', '')
                await msg.edit_text(clean)
            except Exception as e2:
                logger.error(f"safe_edit failed: {e1} / {e2}")
                try:
                    await msg.edit_text("Terjadi error formatting. Coba lagi.")
                except Exception:
                    pass

    def _make_notify_fn(self):
        """
        Buat fungsi notify untuk kirim pesan ke semua chat_ids.
        Pakai direct HTTP requests agar reliable dari background thread manapun.
        """
        token     = TELEGRAM_BOT_TOKEN
        chat_ids  = self.chat_ids

        async def _notify(text: str):
            import requests as _req
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            for cid in list(chat_ids):
                try:
                    _req.post(url, json={"chat_id": cid, "text": text}, timeout=10)
                except Exception as e:
                    logger.debug(f"notify HTTP error {cid}: {e}")
        return _notify

    async def _safe_send(self, chat_id, text):
        """Send to chat — try Markdown first, fallback to plain text."""
        try:
            await self.app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception:
            try:
                clean = text.replace('*', '').replace('_', '').replace('`', '')
                await self.app.bot.send_message(chat_id=chat_id, text=clean)
            except Exception as e:
                if "blocked" in str(e).lower() or "not found" in str(e).lower():
                    self.chat_ids.discard(chat_id)

    # ==================================================================
    # PRICE + PARSE HELPERS
    # ==================================================================
    def _p(self, price):
        if price >= 1000:  return f"${price:,.2f}"
        if price >= 1:     return f"${price:.4f}"
        if price >= 0.01:  return f"${price:.6f}"
        return f"${price:.8f}"

    def _parse_args(self, args):
        symbol, tf = None, '1h'
        for a in (args or []):
            resolved = resolve_tf(a)
            if resolved:
                tf = resolved
            elif symbol is None:
                symbol = a.upper()
        return symbol, tf

    def _fmt_sr(self, result):
        p = self._p
        rd, sd = result.get('key_resistance'), result.get('key_support')
        regime = result.get('regime', {})
        lines = []

        # Tampilkan market regime kalau bukan NORMAL
        rg_name = regime.get('regime', 'NORMAL')
        if rg_name not in ('NORMAL', ''):
            lines.append(f"  Regime: {self._esc(regime.get('desc', rg_name))}")

        def _fmt_level(z, label):
            strength  = self._esc(z.get('strength', ''))
            fib_from  = z.get('fib_from', '')
            exhaustion = z.get('exhaustion', '')
            vol_label = z.get('vol_label', '')
            lines.append(f"  {label}: {p(z['low'])} - {p(z['high'])}")
            detail = strength
            if vol_label:
                detail += f" | {vol_label}"
            if fib_from:
                detail += f" | dari {self._esc(fib_from)}"
            if exhaustion in ('exhausted', 'weakening'):
                detail += f" ({exhaustion})"
            lines.append(f"    {detail}")

        if rd:
            _fmt_level(rd, 'RES')
        if sd:
            _fmt_level(sd, 'SUP')
        if not rd and not sd:
            lines.append("  Level belum terdeteksi")
        return "\n".join(lines)

    # ==================================================================
    # /start
    # ==================================================================
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.chat_ids.add(update.effective_chat.id)
        name = update.effective_user.first_name or "Trader"

        # --- Web login flow ---
        if context.args and context.args[0] == 'weblogin':
            await self._handle_web_login(update)
            return

        # --- Payment confirmation flow ---
        if context.args and context.args[0].startswith('paid_'):
            order_id = context.args[0][5:]  # strip 'paid_'
            await self._handle_paid(update, order_id)
            return
        trader_status = "🟢 AUTO TRADE AKTIF" if (self.trader and self.trader.is_ready) else "🔴 Auto Trade Nonaktif"
        chart_status  = "✅ Aktif" if _CHART_GEN else "❌ Nonaktif"
        text = (
            "🤖 CRYPTOVISION — Smart Money Bot\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👋 Halo, {self._esc(name)}!\n"
            f"📡 Auto Trade : {trader_status}\n"
            f"🖼️ Chart Pro   : {chart_status}\n\n"

            "─── 📊 CHART & ANALISA ─────────────\n"
            "  /chart [coin] [tf]   — Chart pro (OB/FVG/BOS)\n"
            "  /signal [coin] [tf]  — Signal + chart\n"
            "  /analyze [coin] [tf] — Analisa SMC lengkap\n"
            "  /sr [coin] [tf]      — Support & Resistance\n"
            "  /zones [coin] [tf]   — Zona S&R detail\n"
            "  /daily               — Scan semua coin sekali\n"
            "  /market              — Overview pasar global\n"
            "  /whale [coin]        — Aktivitas whale on-chain\n\n"

            "─── 💼 POSISI & TRADING ────────────\n"
            "  /trade               — Status auto trade + balance\n"
            "  /positions           — Posisi aktif + limit order\n"
            "  /monitor             — Aktifkan TP1 BEP monitor\n"
            "  /close [coin]        — Close posisi manual\n\n"

            "─── 📈 STATISTIK & RIWAYAT ─────────\n"
            "  /stats               — Win rate & total PnL\n"
            "  /trades              — 10 trade terakhir\n"
            "  /monthly             — Laporan bulanan\n"
            "  /reset_pnl           — Reset display PnL bulanan\n\n"

            "─── 📝 PAPER TRADE SCALP ───────────\n"
            "  /scalp_stats         — Rekap signal scalp paper\n"
            "                         (WR, EV, open/closed)\n"
            "                         Signal auto masuk 15mnt sekali\n\n"

            "─── ⚙️ UTILITAS ─────────────────────\n"
            "  /learn               — Laporan bot belajar\n"
            "  /train [hari] [coin] — Latih dari data historis\n"
            "  /news [jam]          — Economic calendar\n"
            "  /test                — Cek koneksi API\n"
            "  /help                — Panduan lengkap\n\n"

            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 Tip baru:\n"
            "  • /scalp_stats — cek paper trade scalp\n"
            "  • /reset_pnl — reset PnL bulanan dari 0\n"
            "  • /chart BTC 4h — chart profesional"
        )
        await update.message.reply_text(text)

    # ==================================================================
    # Web Login — generate login link for website
    # ==================================================================
    async def _handle_web_login(self, update: Update):
        """Generate one-time login token and send link to user."""
        import hashlib, hmac, requests as _req
        user = update.effective_user
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        web_url = os.getenv('WEB_URL', 'https://cryptovision-web.vercel.app')

        # Generate secret = HMAC-SHA256(user_id, bot_token)
        secret = hmac.new(
            bot_token.encode(), str(user.id).encode(), hashlib.sha256
        ).hexdigest()

        # Request token from website API
        try:
            resp = _req.post(f"{web_url}/api/auth/token", json={
                'user_id': user.id,
                'name': f"{user.first_name or ''} {user.last_name or ''}".strip(),
                'username': user.username,
                'photo': None,
                'secret': secret,
            }, timeout=10)
            data = resp.json()

            if resp.ok and data.get('url'):
                login_url = data['url']
                await update.message.reply_text(
                    "🔐 *Login CryptoVision Website*\n\n"
                    "Klik link di bawah untuk masuk ke dashboard:\n\n"
                    f"👉 [Klik untuk Login]({login_url})\n\n"
                    "⏰ Link berlaku 5 menit\\.\n"
                    "🔒 Link hanya bisa dipakai 1x\\.",
                    parse_mode='MarkdownV2',
                    disable_web_page_preview=True,
                )
            else:
                await update.message.reply_text(
                    "❌ Gagal generate login link. Coba lagi nanti."
                )
        except Exception as e:
            logger.error(f"Web login failed: {e}")
            await update.message.reply_text(
                "❌ Server website tidak bisa dihubungi. Coba lagi nanti."
            )

    # ==================================================================
    # Payment confirmation — user confirms they paid
    # ==================================================================
    async def _handle_paid(self, update: Update, order_id: str):
        """User confirms payment. Notify admin for verification."""
        user = update.effective_user
        admin_id = os.getenv('ADMIN_TELEGRAM_ID', '')

        await update.message.reply_text(
            f"Konfirmasi Pembayaran\n\n"
            f"Order ID: {order_id}\n"
            f"User: {user.first_name} (@{user.username or 'N/A'})\n\n"
            f"Pembayaran kamu sedang diverifikasi.\n"
            f"Biasanya selesai dalam 1-24 jam.\n\n"
            f"Kalau ada pertanyaan, hubungi admin.",
        )

        # Notify admin
        if admin_id:
            try:
                await update.get_bot().send_message(
                    chat_id=int(admin_id),
                    text=(
                        f"💰 PEMBAYARAN BARU!\n\n"
                        f"Order: {order_id}\n"
                        f"User: {user.first_name} (@{user.username or 'N/A'})\n"
                        f"Telegram ID: {user.id}\n\n"
                        f"Cek rekening, lalu verify:\n"
                        f"/verify {order_id}"
                    ),
                )
            except Exception as e:
                logger.error(f"Failed to notify admin: {e}")

    # ==================================================================
    # /verify — Admin approves payment
    # ==================================================================
    async def cmd_scalp_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Tampilkan rekap paper trade scalp."""
        try:
            from scalp_live_runner import get_paper_trader
            pt = get_paper_trader()
            text = pt.format_stats_msg()
            # Tambahkan open trades
            opens = pt.get_open_trades()
            if opens:
                text += "\n\n📂 OPEN PAPER TRADES:\n"
                for o in opens[:10]:
                    ico = "🟢" if o['direction'] == 'LONG' else "🔴"
                    text += (f"{ico} #{o['id']} {o['symbol']} "
                             f"{o['direction']} entry {o['entry_price']}\n")
            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_reset_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reset SEMUA history — /trade (bulanan/tahunan), /stats, /trades.
        Yang disimpan: posisi yang masih OPEN saja."""
        # 1. Reset PnL tracking di bitunix_trader (bulanan/tahunan)
        reset_info = None
        if self.trader:
            reset_info = self.trader.reset_pnl_tracking()

        # 2. Clear closed trades di TradeTracker (stats & trades)
        tracker_cleared = 0
        tracker_kept = 0
        if self.tracker:
            before = len(self.tracker.trades)
            self.tracker.trades = [t for t in self.tracker.trades
                                   if t.get('status') == 'OPEN']
            tracker_kept = len(self.tracker.trades)
            tracker_cleared = before - tracker_kept
            self.tracker._save()

        ts_str = reset_info['reset_date'] if reset_info else 'now'
        text = (
            "🔄 RESET SEMUA HISTORY\n" +
            "=" * 28 + "\n" +
            f"Timestamp : {ts_str}\n\n" +
            "✅ /trade bulanan/tahunan → $0 (0 trades)\n" +
            f"✅ /stats /trades → hapus {tracker_cleared} closed trade\n" +
            f"✅ Open trade tetap aktif: {tracker_kept}\n\n" +
            "Posisi yang masih OPEN tetap jalan.\n" +
            "Trade baru ke depan akan dihitung dari 0."
        )
        await update.message.reply_text(text)

    async def cmd_verify(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin verifies payment and activates subscription."""
        admin_id = os.getenv('ADMIN_TELEGRAM_ID', '')
        if str(update.effective_user.id) != admin_id:
            await update.message.reply_text("❌ Hanya admin yang bisa verify.")
            return

        if not context.args:
            await update.message.reply_text("Usage: /verify ORDER_ID")
            return

        order_id = context.args[0]
        import hashlib, hmac, requests as _req
        web_url = os.getenv('WEB_URL', 'https://cryptovision-web.vercel.app')
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        secret = hmac.new(
            bot_token.encode(), order_id.encode(), hashlib.sha256
        ).hexdigest()

        try:
            # Get order info
            resp = _req.get(f"{web_url}/api/orders?id={order_id}&secret={secret}", timeout=10)
            if not resp.ok:
                await update.message.reply_text(f"❌ Order {order_id} tidak ditemukan.")
                return

            order = resp.json().get('order', {})

            # DB returns snake_case, handle both formats
            user_id = order.get('user_id') or order.get('userId')
            user_name = order.get('user_name') or order.get('userName') or 'User'
            plan = order.get('plan', '')
            plan_name = order.get('plan_name') or order.get('planName') or plan
            amount = order.get('amount', 'N/A')

            # Update order status to active
            resp2 = _req.patch(f"{web_url}/api/orders", json={
                'orderId': order_id,
                'status': 'active',
                'secret': secret,
            }, timeout=10)

            # Activate subscription
            user_secret = hmac.new(
                bot_token.encode(), str(user_id).encode(), hashlib.sha256
            ).hexdigest()
            _req.post(f"{web_url}/api/subscription", json={
                'userId': user_id,
                'plan': plan,
                'planName': plan_name,
                'userName': user_name,
                'secret': user_secret,
            }, timeout=10)

            if resp2.ok:
                await update.message.reply_text(
                    f"Order {order_id} VERIFIED!\n"
                    f"User: {user_name}\n"
                    f"Plan: {plan_name}\n"
                    f"Amount: {amount}\n\n"
                    f"Subscription diaktifkan."
                )
                # Notify user
                try:
                    await update.get_bot().send_message(
                        chat_id=user_id,
                        text=(
                            f"🎉 Pembayaran CONFIRMED!\n\n"
                            f"Order: {order_id}\n"
                            f"Plan: {plan_name}\n\n"
                            f"Subscription kamu sudah aktif! 🚀\n"
                            f"Buka dashboard: {web_url}/dashboard"
                        ),
                    )
                except Exception:
                    pass
            else:
                await update.message.reply_text(f"❌ Gagal update order: {resp2.text}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    # ==================================================================
    # /grant — admin manual grant subscription
    # ==================================================================
    async def cmd_grant(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: kasih subscription manual ke user.
        Usage: /grant <telegram_id> <plan>
        Plan: m1 (1 Bulan), m3 (3 Bulan), y1 (1 Tahun), lt (Lifetime)
        Contoh: /grant 1885958291 lt
        """
        admin_id = os.getenv('ADMIN_TELEGRAM_ID', '')
        if str(update.effective_user.id) != admin_id:
            await update.message.reply_text("❌ Hanya admin yang bisa grant.")
            return

        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /grant TELEGRAM_ID PLAN\n\n"
                "PLAN options:\n"
                "  m1 = 1 Bulan\n"
                "  m3 = 3 Bulan\n"
                "  y1 = 1 Tahun\n"
                "  lt = Lifetime\n\n"
                "Contoh: /grant 1885958291 lt"
            )
            return

        target_id = context.args[0].strip()
        plan      = context.args[1].strip().lower()
        plan_map  = {
            'm1': '1 Bulan',
            'm3': '3 Bulan',
            'y1': '1 Tahun',
            'lt': 'Lifetime',
        }
        if plan not in plan_map:
            await update.message.reply_text(
                f"❌ Plan '{plan}' tidak valid. Pilih: m1 / m3 / y1 / lt"
            )
            return

        try:
            int(target_id)
        except ValueError:
            await update.message.reply_text(f"❌ Telegram ID '{target_id}' bukan angka valid")
            return

        plan_name = plan_map[plan]
        admin_key = os.getenv('ADMIN_API_KEY', '')
        web_url   = os.getenv('WEB_URL', 'https://cryptovision-web.vercel.app')

        if not admin_key:
            await update.message.reply_text(
                "❌ ADMIN_API_KEY belum diset di .env / Vercel env vars.\n"
                "Set dulu lalu restart bot."
            )
            return

        import requests as _req
        try:
            resp = _req.post(
                f"{web_url}/api/admin/grant",
                json={
                    'adminKey' : admin_key,
                    'userId'   : int(target_id),
                    'plan'     : plan,
                    'planName' : plan_name,
                    'userName' : f"User {target_id}",
                },
                timeout=15,
            )
            if resp.ok:
                await update.message.reply_text(
                    f"✅ GRANT BERHASIL\n"
                    f"========================\n"
                    f"User ID : {target_id}\n"
                    f"Plan    : {plan_name} ({plan})\n\n"
                    f"Subscription aktif di dashboard."
                )
                # Notify recipient (kalau bukan admin sendiri)
                if str(target_id) != admin_id:
                    try:
                        await update.get_bot().send_message(
                            chat_id=int(target_id),
                            text=(
                                f"🎉 SUBSCRIPTION DIAKTIFKAN!\n\n"
                                f"Plan: {plan_name}\n"
                                f"Status: Active\n\n"
                                f"Buka dashboard: {web_url}/dashboard"
                            ),
                        )
                    except Exception:
                        pass
            else:
                await update.message.reply_text(
                    f"❌ Grant gagal ({resp.status_code}): {resp.text[:200]}"
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    # ==================================================================
    # /help
    # ==================================================================
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "📖 PANDUAN CRYPTOVISION\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "─── 📊 CHART & ANALISA ─────────────\n\n"
            "/chart [coin] [tf]\n"
            "  Chart profesional bergaya TradingView\n"
            "  Tampil: Order Block, FVG, Trendline,\n"
            "  BOS/CHoCH, RSI, Liquidity, Entry/SL/TP\n"
            "  Contoh: /chart ETH 1h\n\n"
            "/signal [coin] [tf]\n"
            "  Signal entry + chart otomatis\n"
            "  Entry, SL, TP1, TP2, R:R\n"
            "  Contoh: /signal ETH 1h\n\n"
            "/analyze [coin] [tf]\n"
            "  Analisa penuh: EMA, RSI, S&R, SMC, BOS\n"
            "  Contoh: /analyze XRP 4h\n\n"
            "/sr [coin] [tf]\n"
            "  Daftar level Support & Resistance\n"
            "  Contoh: /sr ATOM 1h\n\n"
            "/zones [coin] [tf]\n"
            "  Zona demand/supply detail + score\n"
            "  Contoh: /zones ETH 1h\n\n"
            "/daily\n"
            "  Scan semua coin, tampilkan top signal\n\n"
            "/market\n"
            "  Overview pasar: BTC, ETH, XRP, dll\n\n"
            "/whale [coin]\n"
            "  Funding rate, LSR ratio, OI, Fear & Greed\n"
            "  Contoh: /whale BTC\n\n"
            "─── 💼 AUTO TRADE ───────────────────\n\n"
            "/trade\n"
            "  Status auto trade, balance, daily loss\n\n"
            "/positions\n"
            "  Posisi aktif + PnL real-time\n\n"
            "/monitor\n"
            "  Aktifkan TP1 monitor secara manual\n\n"
            "/close [coin]\n"
            "  Close posisi — contoh: /close ETH\n"
            "  Close semua : /close ALL\n\n"
            "─── 📊 LAPORAN ──────────────────────\n\n"
            "/stats\n"
            "  Win rate, total PnL, rata-rata RR\n\n"
            "/trades\n"
            "  10 trade terakhir dari semua exchange\n\n"
            "/monthly\n"
            "  Laporan lengkap bulan ini dari Bitunix\n\n"
            "─── ⚙️ UTILITAS ─────────────────────\n\n"
            "/learn\n"
            "  Laporan pola yang sudah dipelajari bot\n"
            "  Win rate per kondisi, threshold yang disesuaikan\n\n"
            "/train [hari] [coin ...]\n"
            "  Latih bot dari data historis 1-2 tahun\n"
            "  Contoh: /train 365\n"
            "  Contoh: /train 180 ETH XRP ATOM\n"
            "  Bot akan analisa ribuan candle lama,\n"
            "  pelajari pola entry yang profit vs rugi\n\n"
            "/news [jam]\n"
            "  Economic calendar high-impact\n"
            "  Contoh: /news 12 (lihat 12 jam ke depan)\n\n"
            "/test\n"
            "  Cek koneksi Binance & CryptoCompare\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ RISK MANAGEMENT\n"
            "  • GOOD/IDEAL  → Risk $3/trade\n"
            "  • Daily limit → $15 (auto stop)\n"
            "  • BEP otomatis setelah TP1 hit\n"
            "  • Max 3 posisi terbuka sekaligus\n\n"
            f"📊 Timeframes: {TF_LIST_STR}\n"
            "🔍 Metode: Fibonacci + SMC + Price Action"
        )
        await update.message.reply_text(text)

    # ==================================================================
    # /test
    # ==================================================================
    async def cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = await update.message.reply_text("Testing koneksi...")
        try:
            loop = asyncio.get_event_loop()
            conn = await loop.run_in_executor(None, self.engine.test_connections)
            lines = ["CONNECTION TEST\n"]
            for c in conn:
                status = "OK" if c['ok'] else "FAIL"
                lines.append(f"  [{status}] {c['name']} ({c.get('ms',0)}ms)")
                if c.get('price'):
                    lines.append(f"       BTC = {self._p(c['price'])}")
            text = "\n".join(lines)
            await msg.edit_text(text)
        except Exception as e:
            logger.error(f"cmd_test: {e}", exc_info=True)
            await msg.edit_text(f"Error: {str(e)[:200]}")

    # ==================================================================
    # /analyze — Full analysis
    # ==================================================================
    async def cmd_analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol, tf = self._parse_args(context.args)
        if not symbol:
            await update.message.reply_text("Contoh: /analyze BTC 4h")
            return
        msg = await update.message.reply_text(f"Analisa {symbol} ({tf.upper()})...")
        try:
            loop = asyncio.get_event_loop()
            result, err = await loop.run_in_executor(
                None, lambda s=symbol, t=tf: self.engine.analyze_coin(s, t, force_fresh=True))
            if err:
                await msg.edit_text(f"Error: {err}")
                return

            p = self._p
            price, chg = result['price'], result['change_24h']
            rsi = result['rsi']
            emas = result['emas']
            ci = "+" if chg >= 0 else ""
            rsi_l = "Oversold" if rsi < 30 else ("Overbought" if rsi > 70 else "Normal")

            text = (
                f"ANALISA - {symbol}/USDT ({result['tf_label']})\n"
                f"================================\n\n"
                f"Harga: {p(price)} ({ci}{chg:.2f}%)\n\n"
                f"Bias: {self._esc(result['market_bias'])}\n"
                f"  {self._esc(result['ema_desc'])}\n"
                f"  Struktur: {self._esc(result['structure_label'])}\n"
            )
            if result.get('ema_desc_h'):
                text += f"  HTF ({result['htf_label']}): {self._esc(result['ema_desc_h'])}\n"

            # EMA 8/21 cross
            ec = result.get('ema_cross', {})
            ema_line = ""
            if ec:
                e8_v  = ec.get('e8', emas.get('ema8', 0))
                e21_v = ec.get('e21', emas.get('ema21', 0))
                bias  = ec.get('bias', 'NEUTRAL')
                cross = ec.get('cross', None)
                gap   = ec.get('gap_pct', 0)
                bias_ico = "🟢" if bias == "BULLISH" else ("🔴" if bias == "BEARISH" else "⬜")
                cross_str = ""
                if cross == "GOLDEN":
                    cross_str = f" | 🌟 Golden Cross ({ec.get('bars_ago',0)} candle lalu)"
                elif cross == "DEATH":
                    cross_str = f" | 💀 Death Cross ({ec.get('bars_ago',0)} candle lalu)"
                ema_line = f"EMA 8/21: {bias_ico} {p(e8_v)} / {p(e21_v)} (gap {gap:+.2f}%){cross_str}\n"

            text += f"\n{ema_line}EMA Stack: {p(emas.get('ema8', emas.get('ema9',0)))} | {p(emas['ema21'])} | {p(emas['ema50'])}\n\n"
            text += self._fmt_sr(result) + "\n\n"
            adx_v = result.get('adx', 0)
            adx_l = "Trending" if adx_v >= 25 else ("Transition" if adx_v >= 20 else "Ranging")
            text += f"RSI: {rsi:.1f} ({rsi_l}) | ADX: {adx_v:.0f} ({adx_l})\n\n"

            # SMC Narrative
            narrative = result.get('narrative', '')
            if narrative:
                text += self._esc(narrative) + "\n\n"

            # Derivatives
            deriv = result.get('smc', {}).get('derivatives', {})
            if deriv.get('available'):
                fr = deriv['funding_rate']
                lsr = deriv['lsr']
                oi_chg = deriv['oi_change_pct']
                text += f"Derivatives:\n"
                text += f"  Funding: {fr:+.4f}% | LSR: {lsr:.2f} | OI: {oi_chg:+.1f}%\n"
                if deriv['sentiment'] != 'NEUTRAL':
                    text += f"  Sentiment: {deriv['sentiment']}\n"
                text += "\n"

            # Candle Patterns
            cps = result.get('smc', {}).get('candle_patterns', [])
            if cps:
                top = sorted(cps, key=lambda x: x['strength'], reverse=True)[:3]
                names = [f"{p['pattern']}" for p in top]
                text += f"Candle: {', '.join(names)}\n\n"

            # Liquidation
            liq_d = result.get('smc', {}).get('liquidation', {})
            if liq_d.get('nearest_long_liq') and liq_d.get('nearest_short_liq'):
                p_func = self._p
                text += f"Liq Zone: L={p_func(liq_d['nearest_long_liq'])} | S={p_func(liq_d['nearest_short_liq'])}\n\n"

            # LTF Trigger
            ltf = result.get('ltf_trigger', {})
            if ltf.get('triggered'):
                text += f"LTF: {self._esc(ltf.get('desc', ''))}\n\n"
            else:
                text += f"LTF: Belum ada trigger\n\n"

            sig = result.get('signal')
            if sig:
                text += f"Signal: {sig['direction']} {sig['quality']}\n"
                text += f"  /signal {symbol} {tf} untuk detail\n"
            else:
                text += "Signal: TUNGGU - Harga jauh dari area\n"

            # label exchange dihapus
            await self._safe_edit(msg, text)
        except Exception as e:
            logger.error(f"cmd_analyze: {e}", exc_info=True)
            await msg.edit_text(f"Error: {str(e)[:200]}")

    # ==================================================================
    # /signal — Entry signal with full detail
    # ==================================================================
    async def cmd_signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol, tf = self._parse_args(context.args)
        if not symbol:
            await update.message.reply_text("Contoh: /signal ETH 1h")
            return
        msg = await update.message.reply_text(f"Analisa {symbol} ({tf.upper()})...")
        try:
            loop = asyncio.get_event_loop()
            result, err = await loop.run_in_executor(
                None, lambda s=symbol, t=tf: self.engine.analyze_coin(s, t, force_fresh=True))
            if err:
                await msg.edit_text(f"Error: {err}")
                return
            text = self._format_signal_message(result)
            sig  = result.get('signal')

            if _CHART_GEN and sig:
                # Hapus "loading" message, ganti dengan foto chart + caption
                try:
                    await msg.delete()
                except Exception:
                    pass
                await self._send_signal_with_chart(
                    update.effective_chat.id, symbol, tf, result, sig, text)
            else:
                await self._safe_edit(msg, text)

            # Track signal
            if sig and sig['quality'] not in ('WAIT',):
                self.tracker.record_signal(
                    symbol=result['symbol'],
                    direction=sig['direction'],
                    quality=sig['quality'],
                    entry=sig['entry'], sl=sig['sl'],
                    tp1=sig['tp1'], tp2=sig['tp2'],
                    confluence_score=sig.get('confluence_score', 0),
                    rr1=sig.get('rr1', 0), rr2=sig.get('rr2', 0))
        except Exception as e:
            logger.error(f"cmd_signal: {e}", exc_info=True)
            await msg.edit_text(f"Error: {str(e)[:200]}")

    # ==================================================================
    # /chart — Kirim chart analisa profesional tanpa perlu ada signal
    # ==================================================================
    async def cmd_chart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol, tf = self._parse_args(context.args)
        if not symbol:
            await update.message.reply_text("Contoh: /chart ETH 1h")
            return
        msg = await update.message.reply_text(f"Generate chart {symbol} ({tf.upper()})...")
        try:
            if not _CHART_GEN:
                await msg.edit_text("Chart generator tidak tersedia.")
                return

            loop = asyncio.get_event_loop()
            result, err = await loop.run_in_executor(
                None, lambda s=symbol, t=tf: self.engine.analyze_coin(s, t, force_fresh=True))
            if err:
                await msg.edit_text(f"Error: {err}")
                return

            sig  = result.get('signal')
            df_m = await loop.run_in_executor(
                None, self.engine.get_klines, symbol, tf, False, False)

            if df_m is None:
                await msg.edit_text("Tidak bisa ambil data candle.")
                return

            chart_path = await loop.run_in_executor(
                None,
                generate_signal_chart,
                df_m, symbol, tf, sig,
                result.get('key_support'),
                result.get('key_resistance'),
                result.get('ema_cross'),
                result.get('structure', 'SIDEWAYS'),
                None,
                result.get('smc'),
            )

            if chart_path:
                text = self._format_signal_message(result)
                caption = text[:1024] if len(text) > 1024 else text
                with open(chart_path, 'rb') as f_img:
                    await self.app.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=f_img,
                        caption=caption,
                    )
                cleanup_chart(chart_path)
                await msg.delete()
            else:
                text = self._format_signal_message(result)
                await self._safe_edit(msg, text)

        except Exception as e:
            logger.error(f"cmd_chart: {e}", exc_info=True)
            await msg.edit_text(f"Error: {str(e)[:200]}")

    # ==================================================================
    # FORMAT SIGNAL — all dynamic text escaped
    # ==================================================================
    def _format_signal_message(self, result, compact=False):
        p = self._p
        price, chg = result['price'], result['change_24h']
        ci = "+" if chg >= 0 else ""
        symbol = result['symbol']
        sig = result.get('signal')

        text = f"{symbol}/USDT ({result['tf_label']}) | {p(price)} | {ci}{chg:.2f}%\n"

        if not compact:
            text += (
                f"================================\n\n"
                f"Bias: {self._esc(result['market_bias'])}"
                f" | {self._esc(result['structure_label'])}\n"
                f"  {self._esc(result['ema_desc'])}\n\n"
                f"{self._fmt_sr(result)}\n\n"
            )
            narrative = result.get('narrative', '')
            if narrative:
                text += self._esc(narrative) + "\n\n"

            # BTC filter info (altcoin only)
            btc_f = result.get('btc_filter', {})
            if btc_f.get('reason') and result.get('symbol') not in ('BTC', 'WBTC'):
                text += f"BTC: {self._esc(btc_f['reason'])}\n\n"
        else:
            text += f"Bias: {self._esc(result['market_bias'])} | {self._esc(result['structure_label'])}\n"

        if sig is None:
            # Cek limit signal (setup pending — harga belum di level tapi menuju ke sana)
            lsig = result.get('limit_signal')
            if lsig:
                sig = lsig  # tampilkan limit signal sebagai pengganti
            else:
                if compact:
                    text += "  TIDAK ADA SINYAL\n"
                    return text
                text += "TIDAK ADA SINYAL\n"
                text += "Harga tidak berada di area level.\n"
                ks, kr = result.get('key_support'), result.get('key_resistance')
                if ks:
                    text += f"  Tunggu ke Support {p(ks['low'])} - {p(ks['high'])}\n"
                if kr:
                    text += f"  Tunggu ke Resistance {p(kr['low'])} - {p(kr['high'])}\n"
                return text

        di = sig['direction']
        q = sig['quality']

        if compact:
            q_label = "LIMIT ORDER" if q == 'LIMIT' else q
            text += (
                f"\n{di} {q_label}\n"
                f"  Entry: {p(sig['entry'])}\n"
                f"  SL: {p(sig['sl'])} ({sig.get('sl_pct',0)}%)\n"
                f"  TP1: {p(sig['tp1'])} ({sig.get('rr1',0)}:1)"
                f" | TP2: {p(sig['tp2'])} ({sig.get('rr2',0)}:1)\n"
            )
            return text

        # ── Limit signal: format khusus ─────────────────────────────
        if q == 'LIMIT':
            dist_pct  = sig.get('dist_pct', 0)
            at_zone   = sig.get('at_zone', False)
            order_t   = sig.get('order_type', 'LIMIT')
            status_ln = "Harga SUDAH di area — bisa entry sekarang" if at_zone else \
                        f"Pasang limit order sekarang, tunggu harga bergerak ({dist_pct:.1f}% dari entry)"
            conf_val  = sig.get('confidence', sig.get('confluence_score', 0))
            text += (
                f"SETUP LIMIT ORDER - {di}\n"
                f"================================\n\n"
                f"  {status_ln}\n\n"
                f"  Entry limit: {p(sig['entry'])}\n"
                f"  SL         : {p(sig['sl'])} ({sig.get('sl_pct',0)}%)\n\n"
                f"  TP1        : {p(sig['tp1'])} (R:R {sig.get('rr1',0)}:1)\n"
                f"  TP2        : {p(sig['tp2'])} (R:R {sig.get('rr2',0)}:1)\n\n"
                f"  Keyakinan  : {conf_val}%\n\n"
                f"ALASAN:\n"
            )
            for r in sig.get('reasons', [])[:5]:
                text += f"  - {self._esc(r)}\n"
            text += (
                f"\nPasang limit order di {p(sig['entry'])}\n"
                f"SL {p(sig['sl'])} — otomatis cancel kalau tidak tersentuh\n\n"
                f"Risk max 1-2% per trade. Selalu pasang SL!"
            )
            return text

        # Extract TP labels from reasons
        tp1_why = ""
        tp2_why = ""
        for r in sig.get('reasons', []):
            if r.startswith('TP1:'):
                tp1_why = f" [{self._esc(r[5:])}]"
            elif r.startswith('TP2:'):
                tp2_why = f" [{self._esc(r[5:])}]"

        text += (
            f"SINYAL {di} - {q}\n"
            f"================================\n\n"
            f"  Entry : {p(sig['entry'])}\n"
            f"  SL    : {p(sig['sl'])} ({sig.get('sl_pct',0)}%)\n\n"
            f"  TP1   : {p(sig['tp1'])} (R:R {sig.get('rr1',0)}:1){tp1_why}\n"
            f"  TP2   : {p(sig['tp2'])} (R:R {sig.get('rr2',0)}:1){tp2_why}\n\n"
            f"  Confluence: {sig.get('confluence_score', 0)} pts"
        )
        if sig.get('kill_count', 0) > 0:
            text += f" | {sig['kill_count']} risk factor"
        text += (
            f"\n\nSTRATEGY:\n"
            f"  TP1 hit -> tutup 50%, geser SL ke entry\n"
            f"  TP1 break -> konfirmasi, hold ke TP2\n"
            f"  Sisa 50% trailing ke TP2\n\n"
        )

        rsi = result['rsi']
        rsi_ok = (rsi < 70 if sig['direction'] == 'LONG' else rsi > 30)
        # Derivatives summary
        deriv = result.get('smc', {}).get('derivatives', {})
        if deriv.get('available'):
            fr = deriv['funding_rate']
            lsr = deriv['lsr']
            oi_chg = deriv['oi_change_pct']
            text += f"DERIVATIF: FR {fr:+.4f}% | LSR {lsr:.2f} | OI {oi_chg:+.1f}%\n\n"

        text += "KONFIRMASI:\n"
        for r in sig.get('reasons', [])[:7]:
            safe_r = self._esc(r)
            icon = "[!]" if "melawan" in safe_r.lower() or "terlalu" in safe_r.lower() else "[v]"
            text += f"  {icon} {safe_r}\n"
        adx_v = result.get('adx', 0)
        adx_ok = adx_v >= 22
        text += f"  {'[v]' if rsi_ok else '[!]'} RSI: {rsi:.1f}\n"
        text += f"  {'[v]' if adx_ok else '[!]'} ADX: {adx_v:.0f} {'trending' if adx_ok else 'ranging'}\n"

        # LTF Trigger status
        ltf = result.get('ltf_trigger', {})
        ltf_ok = ltf.get('triggered', False)
        ltf_desc = self._esc(ltf.get('pattern', 'none')) if ltf_ok else 'belum trigger'
        text += f"  {'[v]' if ltf_ok else '[!]'} LTF: {ltf_desc}\n\n"

        if q == 'WAIT':
            text += f"Pasang limit order di {p(sig['entry'])}\n"
        else:
            text += f"Entry di {p(sig['entry'])}\n"
        text += f"SL {p(sig['sl'])} - geser ke entry setelah TP1\n\n"
        text += f"Zone LOCKED — tidak berubah sampai di-break\n"
        text += f"Gunakan /zones {result['symbol']} {result.get('tf', '1h')} untuk detail\n\n"
        text += f"Risk max 1-2% per trade. Selalu pasang SL!"
        return text

    # ==================================================================
    # /sr — Support & Resistance detail
    # ==================================================================
    async def cmd_sr(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol, tf = self._parse_args(context.args)
        if not symbol:
            await update.message.reply_text("Contoh: /sr BTC 4h")
            return
        msg = await update.message.reply_text(f"S&R {symbol} ({tf.upper()})...")
        try:
            loop = asyncio.get_event_loop()
            result, err = await loop.run_in_executor(
                None, lambda s=symbol, t=tf: self.engine.analyze_coin(s, t, force_fresh=True))
            if err:
                await msg.edit_text(f"Error: {err}")
                return

            p = self._p
            price = result['price']

            text = (
                f"S&R - {symbol}/USDT ({result['tf_label']})\n"
                f"================================\n\n"
                f"Harga: {p(price)}\n\n"
                f"{self._fmt_sr(result)}\n\n"
            )
            ks, kr = result.get('key_support'), result.get('key_resistance')
            if ks:
                text += (
                    f"Support detail:\n"
                    f"  Zone: {p(ks['low'])} - {p(ks['high'])}\n"
                    f"  Touch: {ks['touches']}x | Reject: {ks['bounces']}x\n"
                    f"  Methods: {ks['n_methods']} | Fresh: {'Ya' if ks.get('is_fresh') else 'Tidak'}\n"
                    f"  {self._esc(ks.get('strength', ''))}\n\n"
                )
            if kr:
                text += (
                    f"Resistance detail:\n"
                    f"  Zone: {p(kr['low'])} - {p(kr['high'])}\n"
                    f"  Touch: {kr['touches']}x | Reject: {kr['bounces']}x\n"
                    f"  Methods: {kr['n_methods']} | Fresh: {'Ya' if kr.get('is_fresh') else 'Tidak'}\n"
                    f"  {self._esc(kr.get('strength', ''))}\n"
                )

            # label exchange dihapus
            await self._safe_edit(msg, text)
        except Exception as e:
            logger.error(f"cmd_sr: {e}", exc_info=True)
            await msg.edit_text(f"Error: {str(e)[:200]}")

    # ==================================================================
    # ==================================================================
    # /zones — Show locked zones (STABIL, tidak berubah)
    # ==================================================================
    async def cmd_zones(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text("Format: /zones BTC 1h")
            return
        symbol = args[0].upper()
        tf = args[1] if len(args) > 1 else '1h'

        msg = await update.message.reply_text(f"Loading zones {symbol}...")

        try:
            # Trigger analyze to populate zones (non-fresh = use cached)
            result = self.engine.analyze_coin(symbol, tf, force_fresh=False)
            if result is None or (isinstance(result, tuple) and result[0] is None):
                err = result[1] if isinstance(result, tuple) and len(result) > 1 else 'Error'
                await self._safe_edit(msg, str(err))
                return
            if isinstance(result, tuple):
                result = result[0] if result[0] else result
            if not isinstance(result, dict):
                await self._safe_edit(msg, "Data tidak tersedia")
                return

            p = self._p
            price = result['price']

            zones_data = self.engine.get_locked_zones(symbol, tf)
            ks = zones_data.get('ks')
            kr = zones_data.get('kr')
            all_sup = zones_data.get('all_sup', [])
            all_res = zones_data.get('all_res', [])
            locked_ts = zones_data.get('locked_since', 0)

            import time as t_mod
            age_min = int((t_mod.time() - locked_ts) / 60) if locked_ts else 0

            text = (
                f"ZONE MAP - {symbol}/USDT ({tf})\n"
                f"================================\n"
                f"Harga: {p(price)}\n\n"
            )

            # Resistance zones
            text += "RESISTANCE (di atas):\n"
            if kr:
                text += f"  [#1] {p(kr['low'])} - {p(kr['high'])}\n"
                text += f"       {self._esc(kr.get('strength', ''))}\n"
                text += f"       Score: {kr['score']} | Jarak: {kr.get('dist_pct', 0):.1f}%\n"
                if kr.get('is_fresh'):
                    text += f"       FRESH - belum di-retest\n"
                text += "\n"
            else:
                text += "  (tidak ada resistance kuat)\n\n"

            if len(all_res) > 1:
                for i, z in enumerate(all_res[1:3], 2):
                    text += f"  [#{i}] {p(z['low'])} - {p(z['high'])} (score {z['score']:.0f})\n"
                text += "\n"

            text += f"  --- {p(price)} (harga sekarang) ---\n\n"

            text += "SUPPORT (di bawah):\n"
            if ks:
                text += f"  [#1] {p(ks['low'])} - {p(ks['high'])}\n"
                text += f"       {self._esc(ks.get('strength', ''))}\n"
                text += f"       Score: {ks['score']} | Jarak: {ks.get('dist_pct', 0):.1f}%\n"
                if ks.get('is_fresh'):
                    text += f"       FRESH - belum di-retest\n"
                text += "\n"
            else:
                text += "  (tidak ada support kuat)\n\n"

            if len(all_sup) > 1:
                for i, z in enumerate(all_sup[1:3], 2):
                    text += f"  [#{i}] {p(z['low'])} - {p(z['high'])} (score {z['score']:.0f})\n"
                text += "\n"

            text += f"================================\n"
            if age_min > 0:
                jam = age_min // 60
                mnt = age_min % 60
                if jam > 0:
                    text += f"Zone terkunci sejak {jam}j {mnt}m lalu\n"
                else:
                    text += f"Zone terkunci sejak {mnt} menit lalu\n"
            text += f"Zone TIDAK berubah kecuali di-break candle close"

            await self._safe_edit(msg, text)

        except Exception as e:
            logger.error(f"cmd_zones: {e}", exc_info=True)
            await self._safe_edit(msg, f"Error: {e}")

    # /daily — Best signals dari 100 coins
    # ==================================================================
    async def cmd_daily(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.chat_ids.add(update.effective_chat.id)
        if self._daily_running:
            await update.message.reply_text("Scan sedang berjalan, tunggu...")
            return
        msg = await update.message.reply_text(
            "FULL MARKET SCAN\n\n"
            "Loading top 100 coins...\n"
            "Filter volume & pergerakan...\n"
            "Analisa SMC mendalam...\n\n"
            "Estimasi 3-5 menit")
        try:
            self._daily_running = True
            loop = asyncio.get_event_loop()

            progress = {'text': ''}
            def on_progress(txt):
                progress['text'] = txt

            top = await loop.run_in_executor(
                None, self.engine.scan_top_signals,
                None,
                DAILY_SIGNAL['max_signals'],
                DAILY_SIGNAL['scan_delay'],
                on_progress)

            if not top:
                await msg.edit_text(
                    "Tidak ada signal yang memenuhi kriteria.\n"
                    "Gunakan /signal [coin] untuk analisa manual.")
                return
            text = self._format_daily_digest(top)
            await self._safe_edit(msg, text)
        except Exception as e:
            logger.error(f"cmd_daily: {e}", exc_info=True)
            await msg.edit_text(f"Error: {str(e)[:200]}")
        finally:
            self._daily_running = False

    def _format_daily_digest(self, top_signals):
        now = datetime.now()
        p = self._p

        text = (
            f"TOP SIGNALS - Market Scan\n"
            f"================================\n"
            f"{now.strftime('%d %b %Y %H:%M')}\n"
            f"Scanned: Top 100 coins\n\n"
        )

        for idx, item in enumerate(top_signals, 1):
            result, sig = item['result'], item['signal']
            di = sig['direction']
            q = sig['quality']
            price, chg = result['price'], result['change_24h']
            ci = "+" if chg >= 0 else ""
            conf = sig.get('confluence_score', sig.get('confidence', 0))

            is_limit = (q == 'LIMIT')
            order_type = sig.get('order_type', 'MARKET')
            at_zone    = sig.get('at_zone', False)

            if is_limit:
                # Label khusus untuk setup pending
                if at_zone:
                    limit_label = "LIMIT (Harga sudah di area!)"
                else:
                    dist_pct = sig.get('dist_pct', 0)
                    limit_label = f"LIMIT ORDER ({dist_pct:.1f}% dari entry)"
                q_display = limit_label
            else:
                q_display = q

            text += (
                f"---\n"
                f"#{idx} {item['symbol']}/USDT - {di} {q_display}\n\n"
                f"  Harga: {p(price)} ({ci}{chg:.1f}%)\n"
                f"  Bias: {self._esc(result['market_bias'])}"
                f" | {self._esc(result['structure_label'])}\n"
                f"{self._fmt_sr(result)}\n\n"
                f"  Entry limit: {p(sig['entry'])}\n"
                f"  SL: {p(sig['sl'])} ({sig.get('sl_pct',0)}%)\n"
                f"  TP1: {p(sig['tp1'])} ({sig.get('rr1',0)}:1)"
                f" | TP2: {p(sig['tp2'])} ({sig.get('rr2',0)}:1)\n"
            ) if is_limit else (
                f"---\n"
                f"#{idx} {item['symbol']}/USDT - {di} {q_display}\n\n"
                f"  Harga: {p(price)} ({ci}{chg:.1f}%)\n"
                f"  Bias: {self._esc(result['market_bias'])}"
                f" | {self._esc(result['structure_label'])}\n"
                f"{self._fmt_sr(result)}\n\n"
                f"  Entry: {p(sig['entry'])}\n"
                f"  SL: {p(sig['sl'])} ({sig.get('sl_pct',0)}%)\n"
                f"  TP1: {p(sig['tp1'])} ({sig.get('rr1',0)}:1)"
                f" | TP2: {p(sig['tp2'])} ({sig.get('rr2',0)}:1)\n"
                f"  Confluence: {conf} pts\n\n"
            )

            if not is_limit:
                text += "\n"

            for r in sig.get('reasons', [])[:3]:
                text += f"    {self._esc(r)}\n"

            # Quick derivatives
            deriv_d = result.get('smc', {}).get('derivatives', {})
            if deriv_d.get('available'):
                text += f"    FR: {deriv_d['funding_rate']:+.4f}% LSR: {deriv_d['lsr']:.2f}\n"
            text += "\n"

        text += (
            f"---\n"
            f"/signal [coin] [tf] untuk detail\n"
            f"Risk max 1-2%. DYOR!"
        )
        return text

    # ==================================================================
    # AUTO DAILY (called by scheduler, not spammy)
    # ==================================================================
    async def _auto_daily_scan(self):
        if not self.chat_ids or self._daily_running:
            return
        self._daily_running = True
        try:
            loop = asyncio.get_event_loop()
            # Scan semua top-100 coin — filter kualitas dilakukan oleh signal generator
            # Bot cukup pintar membedakan setup bagus dari yang buruk lewat whale flow filter
            scan_pool = await loop.run_in_executor(None, self.engine.get_top_coins, 100)
            logger.info(f"Daily scan: {len(scan_pool)} coin (top-100 Binance Futures)")
            top = await loop.run_in_executor(
                None, self.engine.scan_top_signals,
                scan_pool, DAILY_SIGNAL['max_signals'], DAILY_SIGNAL['scan_delay'])
            if not top:
                return

            text = self._format_daily_digest(top)
            for cid in list(self.chat_ids):
                await self._safe_send(cid, text)

            # NOTE: auto-trade dari daily digest DIHAPUS (2026-04-20).
            # Dulu bikin dobel order karena auto_execute_best_signal (30 menit)
            # juga execute signal yang sama. Sekarang daily digest cuma
            # broadcast info — semua trading via _auto_execute_best_signal.

        except Exception as e:
            logger.error(f"Auto daily error: {e}", exc_info=True)
        finally:
            self._daily_running = False

    async def _send_signal_with_chart(self, chat_id, symbol: str, tf: str,
                                        result: dict, sig: dict, text: str):
        """
        Kirim signal + chart ke Telegram.
        Kalau chart gagal generate → kirim text saja.
        """
        chart_path = None
        try:
            if _CHART_GEN and result:
                import asyncio as aio
                loop = aio.get_event_loop()
                df_m = await loop.run_in_executor(
                    None, self.engine.get_klines, symbol, tf, False, False)

                if df_m is not None:
                    chart_path = await loop.run_in_executor(
                        None,
                        generate_signal_chart,
                        df_m, symbol, tf, sig,
                        result.get('key_support'),
                        result.get('key_resistance'),
                        result.get('ema_cross'),
                        result.get('structure', 'SIDEWAYS'),
                        None,        # save_path
                        result.get('smc'),  # order blocks, FVG, BOS, liquidity
                    )
        except Exception as e:
            logger.debug(f"chart gen {symbol}: {e}")
            chart_path = None

        if chart_path:
            try:
                with open(chart_path, 'rb') as f_img:
                    # Kirim chart dengan caption signal
                    caption = text[:1024] if len(text) > 1024 else text
                    await self.app.bot.send_photo(
                        chat_id=chat_id,
                        photo=f_img,
                        caption=caption,
                        parse_mode=None,
                    )
                cleanup_chart(chart_path)
                return  # sukses kirim dengan chart
            except Exception as e:
                logger.debug(f"send photo {symbol}: {e}")
                cleanup_chart(chart_path)

        # Fallback: kirim text saja
        await self._safe_send(chat_id, text)

    async def _auto_execute_signals(self, signals: list):
        """Auto trade dari hasil scan — dengan semua filter prioritas."""
        import asyncio as aio
        loop = aio.get_event_loop()
        executed = 0

        for item in signals:
            try:
                sig       = item.get("signal", item)   # support both wrapper and raw signal
                quality   = sig.get("quality", "")
                direction = sig.get("direction", "")
                symbol    = item.get("symbol", sig.get("symbol", ""))

                if quality not in ("GOOD", "IDEAL"):
                    continue

                # Cek daily loss
                self.trader.sync_daily_loss_from_exchange()
                if self.trader._is_daily_loss_exceeded():
                    logger.info("Daily loss limit tercapai")
                    break

                # Cek max posisi
                positions = await loop.run_in_executor(None, self.trader.get_positions)
                if len(positions) >= self.trader.max_positions:
                    break

                # PRIORITAS 4: Korelasi check
                dirs = [("LONG" if p.get("side","") in ("BUY","LONG") else "SHORT") for p in positions]
                if dirs.count(direction) >= 3:
                    logger.info(f"Korelasi tinggi — {dirs.count(direction)} posisi {direction}")
                    continue

                # Eksekusi — pass signal_data untuk learning engine
                btc_state = sig.get('_btc_state', 'NEUTRAL')
                result = await loop.run_in_executor(
                    None,
                    functools.partial(
                        self.trader.place_order,
                        symbol, direction,
                        sig.get("entry", 0), sig.get("sl", 0),
                        sig.get("tp1", 0), sig.get("tp2", 0),
                        None, quality, sig, btc_state,
                        self._make_notify_fn(),
                    ),
                )

                if result and result.get("ok"):
                    executed += 1
                    ico        = "🟢" if direction == "LONG" else "🔴"
                    q_mult     = 1.5 if quality == "IDEAL" else 1.0
                    risk_shown = round(self.trader.risk_usd * q_mult, 2)
                    notif = (
                        "✅ SWING AUTO TRADE EXECUTED\n" +
                        "=" * 28 + "\n" +
                        ico + " " + symbol + " " + direction + " [" + quality + "]\n" +
                        "Entry: " + str(round(sig.get("entry",0), 6)) + "\n" +
                        "SL   : " + str(round(sig.get("sl",0), 6)) + "\n" +
                        "TP1  : " + str(round(sig.get("tp1",0), 6)) + "\n" +
                        "TP2  : " + str(round(sig.get("tp2",0), 6)) + "\n" +
                        "Risk : $" + str(risk_shown) +
                        (" (1.5x IDEAL)" if quality == "IDEAL" else "") + "\n\n" +
                        "👁️ TP1 monitor aktif"
                    )
                    for cid in list(self.chat_ids):
                        await self._safe_send(cid, notif)
                    self.trader.start_tp1_monitor(
                        symbol=symbol, entry=sig.get("entry",0),
                        tp1=sig.get("tp1",0), direction=direction,
                        notify_fn=self._make_notify_fn(),
                        level_price=sig.get("level_price", 0.0),
                    )
                    if executed >= 1:
                        break

            except Exception as e:
                logger.error(f"Auto execute {symbol}: {e}")


    async def _auto_execute_best_signal(self):
        """
        Scan semua coin dan execute trade terbaik.
        Dipanggil setiap 30 menit dari main.py.
        """
        if not self.trader or not self.trader.is_ready:
            return

        import asyncio as aio
        loop = aio.get_event_loop()

        try:
            # Cek daily loss dulu
            await loop.run_in_executor(None, self.trader.sync_daily_loss_from_exchange)
            if self.trader._is_daily_loss_exceeded():
                logger.info("🛑 Auto scan skip — daily loss limit tercapai")
                return

            # Cek max posisi
            positions = await loop.run_in_executor(None, self.trader.get_positions)
            if len(positions) >= self.trader.max_positions:
                logger.info(f"⏸️ Auto scan skip — max posisi {len(positions)}/{self.trader.max_positions}")
                return

            # Scan semua top-100 coin dari Binance Futures
            logger.info("🔍 Auto scan mencari signal terbaik...")
            coins = await loop.run_in_executor(None, self.engine.get_top_coins, 100)

            traded = 0
            MAX_PER_CYCLE = 3  # max 3 posisi per 30 menit — risk $1 × 3 = $3/cycle

            for symbol in coins:
                if traded >= MAX_PER_CYCLE:
                    break

                try:
                    result, err = await loop.run_in_executor(
                        None, self.engine.analyze_coin, symbol, "1h")
                    if err or not result:
                        continue

                    sig = result.get("signal")
                    if not sig:
                        continue

                    quality   = sig.get("quality", "")
                    direction = sig.get("direction", "")

                    if quality not in ("GOOD", "IDEAL"):
                        continue

                    # Anti-duplicate: cek apakah sudah trade coin ini
                    trade_key = f"{symbol}_{direction}"
                    last = self._sent_signals.get(trade_key)
                    if last and (datetime.now() - last).total_seconds() < 14400:  # 4 jam
                        continue

                    # Korelasi check — cluster-aware
                    # Max 3 posisi arah sama (general)
                    # Max 1 posisi per cluster coin (coin yang terkorelasi)
                    dirs = [("LONG" if p.get("side","") in ("BUY","LONG") else "SHORT")
                            for p in positions]
                    if dirs.count(direction) >= 3:
                        continue
                    # Cluster check: coin L1/L2 dan DeFi sering gerak bareng
                    CLUSTERS = {
                        'L1': {'ETH','SOL','AVAX','APT','SUI','SEI','TON','DOT','ATOM'},
                        'L2': {'ARB','OP','POL'},
                        'DEFI': {'INJ','PENDLE','FET','UNI'},
                        'MEME': {'DOGE','WLD','BLUR'},
                    }
                    sym_clean = symbol.upper().replace('/USDT','').replace('USDT','')
                    my_clusters = [c for c, coins in CLUSTERS.items() if sym_clean in coins]
                    if my_clusters:
                        open_syms = [(p.get('symbol','').replace('USDT',''),
                                      "LONG" if p.get("side","") in ("BUY","LONG") else "SHORT")
                                     for p in positions]
                        _cluster_blocked = False
                        for cluster_name in my_clusters:
                            cluster_coins = CLUSTERS[cluster_name]
                            same_dir_cluster = sum(
                                1 for s, d in open_syms
                                if s in cluster_coins and d == direction
                            )
                            if same_dir_cluster >= 1:
                                logger.info(f"🔗 {sym_clean} skip — sudah ada {same_dir_cluster} {direction} di cluster {cluster_name}")
                                _cluster_blocked = True
                                break
                        if _cluster_blocked:
                            continue

                    # Cek max posisi lagi (mungkin sudah berubah)
                    if len(positions) >= self.trader.max_positions:
                        break

                    # Execute trade — pass signal_data untuk learning engine
                    btc_state  = sig.get('_btc_state', 'NEUTRAL')
                    result_order = await loop.run_in_executor(
                        None,
                        functools.partial(
                            self.trader.place_order,
                            symbol, direction,
                            sig.get("entry", 0), sig.get("sl", 0),
                            sig.get("tp1", 0), sig.get("tp2", 0),
                            None, quality, sig, btc_state,
                            self._make_notify_fn(),
                        ),
                    )

                    if result_order and result_order.get("ok"):
                        traded += 1
                        self._sent_signals[trade_key] = datetime.now()

                        ico        = "🟢" if direction == "LONG" else "🔴"
                        rr         = sig.get("rr2", sig.get("rr", 0))
                        q_mult     = 1.5 if quality == "IDEAL" else 1.0
                        risk_shown = round(self.trader.risk_usd * q_mult, 2)
                        notif = (
                            "✅ SWING AUTO TRADE\n" +
                            "=" * 28 + "\n" +
                            ico + " " + symbol + " " + direction + " [" + quality + "]\n" +
                            "Entry : " + str(round(sig.get("entry",0), 6)) + "\n" +
                            "SL    : " + str(round(sig.get("sl",0), 6)) + "\n" +
                            "TP1   : " + str(round(sig.get("tp1",0), 6)) + "\n" +
                            "TP2   : " + str(round(sig.get("tp2",0), 6)) + "\n" +
                            "RR    : 1:" + str(round(rr, 1)) + "\n" +
                            "Risk  : $" + str(risk_shown) +
                            (" (1.5x IDEAL)" if quality == "IDEAL" else "") + "\n\n" +
                            "👁️ TP1 monitor aktif"
                        )
                        for cid in list(self.chat_ids):
                            await self._send_signal_with_chart(
                                cid, symbol, "1h", result, sig, notif)

                        # Refresh posisi
                        positions = await loop.run_in_executor(None, self.trader.get_positions)

                        # Start TP1 monitor — dengan notify_fn agar post-mortem dikirim ke Telegram
                        self.trader.start_tp1_monitor(
                            symbol=symbol, entry=sig.get("entry",0),
                            tp1=sig.get("tp1",0), direction=direction,
                            notify_fn=self._make_notify_fn(),
                            level_price=sig.get("level_price", 0.0),
                        )

                except Exception as e:
                    logger.error(f"Auto scan {symbol}: {e}")
                    continue

            if traded == 0:
                logger.info("🔍 Auto scan selesai — tidak ada signal yang memenuhi syarat")
            else:
                logger.info(f"✅ Auto scan selesai — {traded} trade dieksekusi")

            # ── WAIT SIGNAL ALERT — scan sekunder ────────────────
            # Setelah loop utama, scan lagi untuk WAIT/MODERATE di level kuat.
            # Kirim notifikasi "pantau ini" tanpa auto-execute.
            await self._send_watch_alerts(coins, loop)

        except Exception as e:
            logger.error(f"_auto_execute_best_signal error: {e}", exc_info=True)

    async def _send_watch_alerts(self, coins: list, loop):
        """
        Kirim alert 'pantau ini' untuk WAIT/MODERATE signal di level kuat.

        Tidak auto-execute — hanya info agar kamu bisa manual pantau.
        Berguna untuk tahu setup yang hampir layak, tinggal tunggu konfirmasi.

        Kriteria alert:
          - Quality MODERATE atau WAIT
          - Confluence score >= 15 (ada fondasi signal)
          - Di dekat key support/resistance (at_zone atau close)
          - Belum dikirim dalam 2 jam terakhir (anti-spam)
        """
        WATCH_COOLDOWN = 7200   # 2 jam per coin per arah
        MIN_SCORE      = 15     # score minimum untuk dianggap layak dipantau
        alerts_sent    = 0
        MAX_ALERTS     = 2      # max 2 alert per cycle agar tidak spam

        for symbol in coins[:10]:   # cek 10 coin teratas saja
            if alerts_sent >= MAX_ALERTS:
                break
            try:
                result, err = await loop.run_in_executor(
                    None, self.engine.analyze_coin, symbol, "1h")
                if err or not result:
                    continue

                sig = result.get("signal")
                if not sig:
                    continue

                quality   = sig.get("quality", "")
                direction = sig.get("direction", "")
                score     = sig.get("confluence_score", 0)

                # Hanya untuk MODERATE dan WAIT dengan score cukup
                if quality not in ("MODERATE", "WAIT"):
                    continue
                if score < MIN_SCORE:
                    continue

                # Cek cooldown — jangan kirim yang sama dalam 2 jam
                watch_key = f"watch_{symbol}_{direction}"
                last_sent = self._watch_alerts.get(watch_key)
                if last_sent and (datetime.now() - last_sent).total_seconds() < WATCH_COOLDOWN:
                    continue

                # Cek apakah sudah ada posisi terbuka untuk coin ini
                if self.trader and self.trader.is_ready:
                    open_pos = self.trader.get_open_position(symbol)
                    if open_pos:
                        continue   # sudah ada posisi → skip alert

                # Bangun pesan alert
                ico    = "🟢" if direction == "LONG" else "🔴"
                price  = result.get("price", 0)
                rr     = sig.get("rr2", sig.get("rr", 0))
                ks     = result.get("key_support")
                kr     = result.get("key_resistance")
                level  = ks if direction == "LONG" else kr
                level_str = ""
                if level:
                    level_str = f"\nLevel  : {self._p(level.get('low',0))} - {self._p(level.get('high',0))}"

                # Ambil 2 alasan utama (bersihkan dari emoji session)
                reasons = [r for r in sig.get("reasons", [])[:3]
                           if not r.startswith("⏰") and not r.startswith("Menunggu")]
                reasons_str = "\n  ".join(reasons[:2]) if reasons else "-"

                # Kenapa belum entry (apa yang kurang)
                block_reasons = []
                if quality == "WAIT":
                    block_reasons.append("Menunggu rejection candle lebih kuat")
                if quality == "MODERATE":
                    block_reasons.append("Konfluensi belum cukup untuk auto-trade")

                text = (
                    f"👁️ WATCH — {symbol} {direction}\n"
                    f"{'─' * 30}\n"
                    f"{ico} Tier    : {quality} (score {score})\n"
                    f"   Harga  : {self._p(price)}{level_str}\n"
                    f"   RR     : 1:{round(rr, 1)}\n"
                    f"   Entry  : {self._p(sig.get('entry', 0))}\n"
                    f"   SL     : {self._p(sig.get('sl', 0))}\n"
                    f"   TP1    : {self._p(sig.get('tp1', 0))}\n\n"
                    f"📋 Alasan:\n  {reasons_str}\n\n"
                    f"⏳ Belum entry karena:\n  {chr(10).join('  ' + r for r in block_reasons)}\n\n"
                    f"💡 Pantau candle berikutnya — kalau muncul Pin Bar\n"
                    f"   atau Engulfing di level ini, bisa manual entry."
                )

                for cid in list(self.chat_ids):
                    await self._safe_send(cid, text)

                self._watch_alerts[watch_key] = datetime.now()
                alerts_sent += 1
                logger.info(f"👁️ Watch alert: {symbol} {direction} [{quality}] score={score}")

            except Exception as e:
                logger.debug(f"watch alert {symbol}: {e}")

    async def cmd_whale(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = (context.args[0].upper() if context.args else "BTC")
        msg = await update.message.reply_text(f"Whale {symbol}...")
        try:
            loop = asyncio.get_event_loop()
            w = await loop.run_in_executor(None, self.whale.full_whale_analysis, symbol)
            if not w:
                await msg.edit_text("Data whale tidak tersedia.")
                return
            text = f"WHALE ACTIVITY - {symbol}\n================================\n\n"
            for k, v in w.items():
                text += f"  {self._esc(str(k))}: {self._esc(str(v))}\n"
            await self._safe_edit(msg, text)
        except Exception as e:
            logger.error(f"cmd_whale: {e}", exc_info=True)
            await msg.edit_text(f"Error: {str(e)[:200]}")

    # ==================================================================
    # /market
    # ==================================================================
    async def cmd_market(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = await update.message.reply_text("Loading market...")
        try:
            loop = asyncio.get_event_loop()
            coins = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP']
            text = "MARKET OVERVIEW\n================================\n\n"

            for coin in coins:
                try:
                    result, err = await loop.run_in_executor(
                        None, self.engine.analyze_coin, coin, '1h')
                    if err or not result:
                        text += f"  {coin}: data unavailable\n"
                        continue
                    pr = result['price']
                    ch = result['change_24h']
                    ci = "+" if ch >= 0 else ""
                    bias = self._esc(result['market_bias'])
                    text += f"  {coin}: {self._p(pr)} ({ci}{ch:.1f}%) {bias}\n"
                except Exception:
                    text += f"  {coin}: error\n"

            text += f"\n/analyze [coin] untuk detail"
            await self._safe_edit(msg, text)
        except Exception as e:
            logger.error(f"cmd_market: {e}", exc_info=True)
            await msg.edit_text(f"Error: {str(e)[:200]}")

    # ==================================================================
    # /stats + /trades
    # ==================================================================
    async def cmd_trade(self, update, context):
        if not self.trader:
            await update.message.reply_text("Auto trade tidak aktif."); return
        msg = await update.message.reply_text("Mengambil status...")
        try:
            import asyncio as aio
            loop = aio.get_event_loop()
            text = await loop.run_in_executor(None, self.trader.get_status)
            await self._safe_edit(msg, text)
        except Exception as e:
            await msg.edit_text(str(e)[:300])

    async def cmd_positions(self, update, context):
        if not self.trader:
            await update.message.reply_text("Auto trade tidak aktif."); return
        msg = await update.message.reply_text("Mengambil posisi...")
        try:
            import asyncio as aio
            loop = aio.get_event_loop()
            positions = await loop.run_in_executor(None, self.trader.get_positions)
            pending = await loop.run_in_executor(None, self.trader.get_all_pending_orders)

            if not positions and not pending:
                await msg.edit_text("Tidak ada posisi terbuka & tidak ada pending order."); return

            def _p(v):
                v = float(v)
                if v >= 100: return f"{v:.2f}"
                if v >= 1:   return f"{v:.4f}"
                return f"{v:.6f}"

            text = ""
            if positions:
                text += "POSISI AKTIF (" + str(len(positions)) + ")\n\n"
                for pos in positions:
                    side = pos.get("side", ""); entry = float(pos.get("avgOpenPrice", 0))
                    pnl  = float(pos.get("unrealizedPNL", 0)); qty = float(pos.get("qty", 0))
                    realized = float(pos.get("realizedPNL", 0)); margin = float(pos.get("margin", 0))
                    lev = pos.get("leverage", "?"); sym = pos.get("symbol", "")
                    d   = "LONG" if side == "BUY" else "SHORT"
                    ico = "🟢" if side == "BUY" else "🔴"
                    pct = (pnl / margin * 100) if margin > 0 else 0
                    try: mark = entry + (pnl/qty) if side == "BUY" else entry - (pnl/qty)
                    except: mark = entry
                    text += ico + " " + sym + " " + d + " x" + str(lev) + "\n"
                    text += "  Entry: " + _p(entry) + " | Mark: " + _p(mark) + "\n"
                    text += "  Qty: " + str(qty) + "\n"
                    text += "  PnL: " + f"{pnl:+.4f}" + " (" + f"{pct:+.1f}" + "%)\n"
                    text += "  Real: " + f"{realized:+.4f}" + "\n\n"

            if pending:
                text += "PENDING LIMIT ORDER (" + str(len(pending)) + ")\n\n"
                for o in pending:
                    sym = o.get("symbol", "")
                    side = o.get("side", "") or o.get("tradeSide", "")
                    price = float(o.get("price", 0) or 0)
                    qty = float(o.get("qty", 0) or o.get("size", 0) or 0)
                    d = "LONG" if side.upper() in ("BUY", "LONG") else "SHORT"
                    ico = "🟡"
                    text += ico + " " + sym + " " + d + " LIMIT\n"
                    text += "  Entry: " + _p(price) + "\n"
                    text += "  Qty: " + str(qty) + "\n"
                    text += "  Status: menunggu harga sampai entry\n\n"

            text += "/close [coin] untuk close posisi"
            await self._safe_edit(msg, text)
        except Exception as e:
            await msg.edit_text(str(e)[:300])

    async def cmd_monitor(self, update, context):
        if not self.trader:
            await update.message.reply_text("Auto trade tidak aktif."); return
        msg = await update.message.reply_text("Cek posisi...")
        try:
            import asyncio as aio
            loop = aio.get_event_loop()
            positions = await loop.run_in_executor(None, self.trader.get_positions)
            if not positions:
                await msg.edit_text("Tidak ada posisi aktif."); return
            started = []
            for pos in positions:
                sf = pos.get("symbol", ""); sc = sf.replace("USDT", "")
                side = pos.get("side", ""); d = "LONG" if side == "BUY" else "SHORT"
                entry = float(pos.get("avgOpenPrice", 0))
                if not entry: continue
                saved = self.trader._saved_positions.get(sc, {})
                tp1 = float(saved.get("tp1", 0)); sl = float(saved.get("sl", 0))
                if not tp1:
                    risk = abs(entry - sl) if sl else entry * 0.015
                    tp1  = entry + risk if d == "LONG" else entry - risk
                if sc in self.trader._active_monitors:
                    started.append("👁️ " + sf + " sudah aktif"); continue
                self.trader.start_tp1_monitor(sc, entry, tp1, d, None)
                started.append("✅ " + sf + " " + d)
            text = "TP1 MONITOR\n\n" + "\n".join(started) if started else "Tidak ada"
            text += "\n\nTotal: " + str(len(self.trader._active_monitors)) + " coin"
            await self._safe_edit(msg, text)
        except Exception as e:
            await msg.edit_text(str(e)[:300])

    async def cmd_close(self, update, context):
        if not self.trader:
            await update.message.reply_text("Auto trade tidak aktif."); return
        args = context.args
        if not args:
            await update.message.reply_text("Contoh: /close ETH atau /close ALL"); return
        coin = args[0].upper()
        msg  = await update.message.reply_text("Menutup " + coin + "...")
        try:
            import asyncio as aio
            loop = aio.get_event_loop()
            if coin == "ALL":
                positions = await loop.run_in_executor(None, self.trader.get_positions)
                res = []
                for pos in positions:
                    s = pos.get("symbol", "").replace("USDT", "")
                    r = await loop.run_in_executor(None, self.trader.close_position, s)
                    res.append(("✅" if r.get("ok") else "❌") + " " + s + ": " + r.get("msg", ""))
                await msg.edit_text("CLOSE ALL\n" + "\n".join(res))
            else:
                r = await loop.run_in_executor(None, self.trader.close_position, coin)
                await msg.edit_text(("✅" if r.get("ok") else "❌") + " " + r.get("msg", "Selesai"))
        except Exception as e:
            await msg.edit_text(str(e)[:300])

    async def cmd_monthly(self, update, context):
        """Laporan bulanan lengkap semua trade dari Bitunix."""
        if not self.trader:
            await update.message.reply_text("Auto trade tidak aktif.")
            return
        msg = await update.message.reply_text("Mengambil data bulanan...")
        try:
            import asyncio as aio
            from datetime import datetime
            loop = aio.get_event_loop()

            def fetch():
                now = datetime.now()
                month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                start_ts    = int(month_start.timestamp() * 1000)
                data = self.trader._get("/api/v1/futures/position/get_history_positions", {
                    "startTime": str(start_ts), "limit": "200",
                })
                if data.get("code") != 0:
                    return None, data.get("msg", "Error")
                raw = data.get("data", [])
                if isinstance(raw, dict):
                    positions = raw.get("positionList", raw.get("list", []))
                elif isinstance(raw, list):
                    positions = raw
                else:
                    return None, "No data"
                # Filter by mtime — hanya trade yang DITUTUP bulan ini
                trades_this_month = [
                    p for p in positions
                    if int(p.get("mtime", 0)) >= start_ts
                ]
                return trades_this_month, start_ts

            result = await loop.run_in_executor(None, fetch)
            if result[0] is None:
                await msg.edit_text("Error: " + str(result[1]))
                return

            trades, start_ts = result
            if not trades:
                await msg.edit_text("Belum ada trade yang ditutup bulan ini.")
                return

            total_profit = total_loss = wins = losses = 0.0
            win_count = loss_count = 0
            for t in trades:
                pnl = float(t.get("realizedPNL", 0))
                if pnl > 0:
                    total_profit += pnl; win_count += 1
                elif pnl < 0:
                    total_loss += abs(pnl); loss_count += 1

            net   = total_profit - total_loss
            total = win_count + loss_count
            wr    = (win_count / total * 100) if total > 0 else 0
            now_str = datetime.now().strftime("%B %Y")
            net_ico = "+" if net >= 0 else ""

            header = (
                "LAPORAN BULANAN — " + now_str + "\n" +
                "=" * 30 + "\n\n" +
                "RINGKASAN\n" +
                "  Total trade : " + str(total) + "\n" +
                "  Win / Loss  : " + str(win_count) + "W / " + str(loss_count) + "L\n" +
                "  Win Rate    : " + f"{wr:.1f}%" + "\n" +
                "  Profit      : +$" + f"{total_profit:.2f}" + "\n" +
                "  Loss        : -$" + f"{total_loss:.2f}" + "\n" +
                "  Net PnL     : $" + net_ico + f"{net:.2f}" + " USDT\n\n" +
                "=" * 30 + "\n" +
                "DETAIL TRADE (terbaru)\n\n"
            )

            trades_sorted = sorted(trades, key=lambda x: int(x.get("mtime", 0)), reverse=True)

            detail = ""
            for i, t in enumerate(trades_sorted, 1):
                pnl   = float(t.get("realizedPNL", 0))
                sym   = t.get("symbol", "").replace("USDT", "")
                side  = t.get("side", "")
                qty   = t.get("qty", "0")
                entry = float(t.get("avgOpenPrice", 0))
                lev   = t.get("leverage", "?")
                ctime = int(t.get("ctime", 0))
                mtime = int(t.get("mtime", 0))
                open_s  = datetime.fromtimestamp(ctime/1000).strftime("%d/%m %H:%M") if ctime else "?"
                close_s = datetime.fromtimestamp(mtime/1000).strftime("%d/%m %H:%M") if mtime else "?"
                d = "LONG" if side in ("BUY", "LONG") else "SHORT"
                result_icon = "P" if pnl > 0 else ("L" if pnl < 0 else "-")

                detail += (
                    str(i) + ". " + sym + " " + d + " x" + str(lev) + "  [" + result_icon + "]\n" +
                    "   Entry: " + f"{entry:.4f}" + "  Qty: " + str(qty) + "\n" +
                    "   " + open_s + " -> " + close_s + "\n" +
                    "   PnL: " + f"{pnl:+.4f}" + " USDT\n\n"
                )

            # Kirim header dulu, lalu detail (mungkin panjang)
            full = header + detail
            chat_id = update.effective_chat.id

            # Split kalau lebih dari 4000 karakter
            if len(full) <= 4000:
                await self._safe_edit(msg, full)
            else:
                await self._safe_edit(msg, header)
                chunk = ""
                for line in detail.split("\n\n"):
                    if not line.strip():
                        continue
                    chunk += line + "\n\n"
                    if len(chunk) > 3000:
                        await self._safe_send(chat_id, chunk)
                        chunk = ""
                if chunk:
                    await self._safe_send(chat_id, chunk)

        except Exception as e:
            import traceback
            await msg.edit_text("Error: " + str(e)[:300])

    async def cmd_news(self, update, context):
        """High-impact economic calendar."""
        msg = await update.message.reply_text("Mengambil news calendar...")
        try:
            from news_filter import get_news_filter
            nf        = get_news_filter()
            h         = int(context.args[0]) if context.args else 24
            h         = min(max(h, 1), 48)

            now_check = nf.check()
            upcoming  = nf.format_upcoming(h)

            lines_out = ["NEWS FILTER STATUS", "=" * 28, ""]

            if now_check.get("block"):
                lines_out += ["STATUS: TRADING DIBLOK",
                              now_check.get("reason",""), ""]
            elif now_check.get("warning"):
                lines_out += ["STATUS: HATI-HATI",
                              now_check.get("reason",""), ""]
            else:
                lines_out += ["STATUS: AMAN TRADING", ""]

            lines_out.append(upcoming)
            lines_out.append("")
            lines_out.append("Contoh: /news 12 (lihat 12 jam ke depan)")
            await self._safe_edit(msg, "\n".join(lines_out))

        except ImportError:
            await msg.edit_text("news_filter.py belum ada di folder bot.")
        except Exception as e:
            logger.error(f"cmd_news: {e}", exc_info=True)
            await msg.edit_text("Error: " + str(e)[:300])

    # ==================================================================
    # /learn — Laporan introspeksi bot + pola yang dipelajari
    # ==================================================================
    async def cmd_learn(self, update, context):
        msg = await update.message.reply_text("Menganalisa hasil training...")
        try:
            from smart_coin_selector import get_training_summary
            s = get_training_summary()

            if not s:
                await self._safe_edit(msg,
                    "Belum ada data training.\nJalankan /train dulu.")
                return

            sur_sign = "+" if s['surplus_wr'] >= 0 else ""
            lines = [
                f"HASIL TRAINING TERAKHIR",
                f"{'─'*30}",
                f"Dilatih pada : {s['trained_at']}",
                f"Total signal : {s['total_signals']:,}",
                f"",
                f"Win Rate     : {s['overall_wr']:.1f}%",
                f"Breakeven WR : {s['breakeven_wr']:.1f}%  (minimum agar tidak rugi)",
                f"Surplus      : {sur_sign}{s['surplus_wr']:.1f}%  {'✓ PROFITABLE' if s['surplus_wr']>0 else '✗ MERUGI'}",
                f"EV/trade     : ${s['ev_per_trade']:+.3f}",
                f"Total PnL    : ${s['overall_pnl']:+.2f}  (risk $3/trade)",
                f"",
                f"Coin aktif ({s['n_profitable']}): {', '.join(s['profitable'][:15])}{'...' if s['n_profitable']>15 else ''}",
                f"",
                f"Coin dihindari ({s['n_avoid']}): {', '.join(s['avoid']) if s['avoid'] else 'tidak ada'}",
                f"",
                f"Bot hanya auto-trade coin di daftar aktif.",
            ]
            await self._safe_edit(msg, "\n".join(lines))
        except Exception as e:
            logger.error(f"cmd_learn: {e}", exc_info=True)
            await msg.edit_text(f"Error: {str(e)[:300]}")

    # ==================================================================
    # /train [hari] [coin ...] — Latih bot dari data historis
    # ==================================================================
    async def cmd_train(self, update, context):
        args = context.args or []
        # Parse argumen: /train [days] [COIN1 COIN2 ...]
        days = 365
        coins = []
        for a in args:
            if a.isdigit():
                days = int(a)
            else:
                coins.append(a.upper())

        if not coins:
            coins = self.engine.get_top_coins(100)

        coin_str = ", ".join(coins)
        msg = await update.message.reply_text(
            f"Memulai pelatihan historis...\n"
            f"Coins  : {coin_str}\n"
            f"Periode: {days} hari ke belakang\n\n"
            f"Proses ini membutuhkan waktu beberapa menit.\n"
            f"Bot akan kirim notifikasi saat selesai."
        )

        loop = asyncio.get_event_loop()
        chat_id = update.effective_chat.id
        bot = self.app.bot

        async def _run_training():
            results = {}
            try:
                from historical_trainer import HistoricalTrainer
                trainer = HistoricalTrainer()

                # Progress callback — kirim update tiap coin selesai
                def on_progress(coin, n_signals, n_full_tp, n_tp1_only, n_sl, pnl):
                    n_tp = n_full_tp + n_tp1_only
                    wr   = round(n_tp / n_signals * 100, 1) if n_signals else 0
                    line = (
                        f"  [{coin}] {n_signals} signal | "
                        f"FullTP:{n_full_tp} TP1:{n_tp1_only} SL:{n_sl} "
                        f"WR:{wr}% PnL:${pnl:+.2f}"
                    )
                    results[coin] = line

                # Jalankan training di thread terpisah agar tidak block event loop
                def _blocking():
                    return trainer.train_all(coins=coins, days=days,
                                            progress_cb=on_progress)

                summary = await loop.run_in_executor(None, _blocking)

                lines = [
                    f"Pelatihan selesai! ({days} hari)",
                    f"{'─'*32}",
                ]
                for coin in coins:
                    if coin in results:
                        lines.append(results[coin])

                if summary:
                    n      = summary.get('total_signals', 0)
                    ftp    = summary.get('total_full_tp', 0)
                    tp1    = summary.get('total_tp1only', 0)
                    sl     = summary.get('total_sl', 0)
                    pnl    = summary.get('total_pnl', 0)
                    ev     = summary.get('ev_per_trade', 0)
                    wr_all = summary.get('win_rate', 0)
                    bkeven = summary.get('breakeven_wr', 0)
                    surp   = summary.get('surplus_wr', 0)
                    lines.append(f"{'─'*32}")
                    lines.append(f"Total signal  : {n}")
                    lines.append(f"Full TP       : {ftp} ({ftp/n*100:.1f}%)" if n else f"Full TP: {ftp}")
                    lines.append(f"TP1 Only      : {tp1} ({tp1/n*100:.1f}%)" if n else f"TP1 Only: {tp1}")
                    lines.append(f"SL            : {sl} ({sl/n*100:.1f}%)"   if n else f"SL: {sl}")
                    lines.append(f"Win Rate      : {wr_all:.1f}%")
                    lines.append(f"Breakeven WR  : {bkeven:.1f}%")
                    sur_sign = "+" if surp >= 0 else ""
                    lines.append(f"Surplus       : {sur_sign}{surp:.1f}%  {'PROFITABLE' if surp>0 else 'MERUGI'}")
                    lines.append(f"EV/trade      : ${ev:+.3f}")
                    lines.append(f"Total PnL     : ${pnl:+.2f}  (risk $3/trade)")
                    lines.append("")
                    lines.append("Coin profitable disimpan.")
                    lines.append("Ketik /learn untuk detail.")

                text_out = "\n".join(lines)
                await bot.send_message(chat_id=chat_id, text=text_out)

            except ImportError as ie:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"historical_trainer.py belum tersedia: {ie}"
                )
            except Exception as e:
                logger.error(f"cmd_train background: {e}", exc_info=True)
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"Training error: {str(e)[:400]}"
                )

        # Jalankan sebagai asyncio task agar tidak block handler
        asyncio.create_task(_run_training())

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Check open trades first
        if self.engine:
            self.tracker.check_trades(self.engine)

        stats = self.tracker.get_stats()
        if not stats:
            await update.message.reply_text("Belum ada trade yang tercatat. Gunakan /signal untuk mulai.")
            return

        text = (
            f"PERFORMANCE DASHBOARD\n"
            f"================================\n\n"
            f"Total Trades: {stats['total_trades']}\n"
            f"Open: {stats['open_trades']}\n\n"
            f"Win: {stats['wins']} | Loss: {stats['losses']}\n"
            f"WIN RATE: {stats['win_rate']}%\n\n"
            f"Total PnL: {stats['total_pnl_r']:+.2f}R\n"
            f"Avg Win: {stats['avg_win_r']:+.2f}R\n"
            f"Avg Loss: {stats['avg_loss_r']:.2f}R\n\n"
        )

        if stats['by_quality']:
            text += "Per Quality:\n"
            for q, data in stats['by_quality'].items():
                text += f"  {q}: {data['wr']}% WR ({data['wins']}/{data['total']})\n"
            text += "\n"

        text += (
            f"Best: {stats['best_coin']} ({stats['best_pnl']:+.2f}R)\n"
            f"Worst: {stats['worst_coin']} ({stats['worst_pnl']:+.2f}R)"
        )
        await update.message.reply_text(text)

    async def cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Check trades first
        if self.engine:
            self.tracker.check_trades(self.engine)

        recent = self.tracker.get_recent(10)
        if not recent:
            await update.message.reply_text("Belum ada trade. Gunakan /signal untuk mulai.")
            return

        p = self._p
        text = "RECENT TRADES\n================================\n\n"
        for t in recent:
            status_icon = {
                'OPEN': 'O', 'TP1_HIT': 'TP1', 'TP2_HIT': 'TP2',
                'SL_HIT': 'SL', 'EXPIRED': 'EXP'
            }.get(t['status'], '?')

            pnl = t['result_pnl']
            pnl_str = f"{pnl:+.1f}R" if pnl != 0 else "0R"
            ts = t['timestamp'][:16].replace('T', ' ')

            text += (
                f"  [{status_icon}] {t['symbol']} {t['direction']} {t['quality']}\n"
                f"    Entry: {p(t['entry'])} | {pnl_str}\n"
                f"    {ts}\n\n"
            )
        await update.message.reply_text(text)

    # ==================================================================
    # START / STOP — NO AUTO MONITOR
    # ==================================================================
    def start_bot(self):
        self._thread = threading.Thread(target=self._run_polling, daemon=True)
        self._thread.start()
        logger.info("Telegram bot started!")

    def _run_polling(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        async def _start():
            await self.app.initialize()
            await self.app.start()
            await self.app.updater.start_polling(drop_pending_updates=True)
            # Notif startup ke admin
            try:
                from datetime import datetime
                import os as _os
                risk_usd  = _os.getenv('TRADE_RISK_USD', '?')
                max_pos   = _os.getenv('TRADE_MAX_POSITIONS', '?')
                trade_on  = _os.getenv('TRADE_ENABLED', 'false').lower() == 'true'
                trade_str = "ON" if trade_on else "OFF"
                now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                msg = (
                    "Bot ONLINE\n"
                    "========================\n"
                    f"Waktu   : {now_str}\n"
                    f"Risk    : ${risk_usd} per trade\n"
                    f"MaxPos  : {max_pos}\n"
                    f"Trading : {trade_str}\n"
                    "========================\n"
                    "Auto scan aktif setiap 30 menit."
                )
                for cid in list(self.chat_ids):
                    try:
                        await self.app.bot.send_message(chat_id=cid, text=msg)
                    except Exception:
                        pass
            except Exception:
                pass
            # Keep running
            while True:
                await asyncio.sleep(3600)
        try:
            loop.run_until_complete(_start())
        except Exception as e:
            logger.error(f"Polling error: {e}")

    def stop_bot(self):
        logger.info("Telegram bot stopped!")