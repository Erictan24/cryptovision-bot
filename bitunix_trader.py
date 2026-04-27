"""
bitunix_trader.py — Auto Trading Module untuk Bitunix Futures.

Fitur:
  - Open posisi LONG/SHORT otomatis
  - Set SL dan TP langsung di exchange
  - Money management: risk % per trade yang kamu tentukan
  - Monitor posisi aktif
  - Close posisi manual via Telegram
  - Daily loss limit protection
  - Max open position limit

Cara pakai:
  Tambahkan di .env:
    BITUNIX_API_KEY=your_api_key
    BITUNIX_SECRET_KEY=your_secret_key
    TRADE_RISK_PER_TRADE=1.0     # % modal per trade
    TRADE_LEVERAGE=10            # leverage default
    TRADE_MAX_POSITIONS=3        # max posisi terbuka bersamaan
    TRADE_MAX_DAILY_LOSS=3.0     # max loss harian %
    TRADE_ENABLED=false          # true untuk aktifkan auto trade
"""

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, date
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

BASE_URL = "https://fapi.bitunix.com"


# ============================================================
# SIGNATURE — Bitunix double SHA256
# ============================================================

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def _make_sign(api_key: str, secret_key: str,
               nonce: str, timestamp: str,
               query_params: str = "", body: str = "") -> str:
    """
    Bitunix signature:
      digest = sha256(nonce + timestamp + api_key + query_params + body)
      sign   = sha256(digest + secret_key)
    """
    digest = _sha256(nonce + timestamp + api_key + query_params + body)
    sign   = _sha256(digest + secret_key)
    return sign

def _headers(api_key: str, secret_key: str,
             query_params: str = "", body: str = "") -> dict:
    nonce     = uuid.uuid4().hex[:16]
    timestamp = str(int(time.time() * 1000))
    sign      = _make_sign(api_key, secret_key, nonce, timestamp, query_params, body)
    return {
        "api-key"     : api_key,
        "sign"        : sign,
        "nonce"       : nonce,
        "timestamp"   : timestamp,
        "language"    : "en-US",
        "Content-Type": "application/json",
    }


# ============================================================
# BITUNIX TRADER
# ============================================================

class BitunixTrader:
    """
    Auto trading client untuk Bitunix Futures.

    Money management sepenuhnya dikontrol user via .env:
      TRADE_RISK_PER_TRADE  — % modal yang di-risk per trade (default 1%)
      TRADE_LEVERAGE        — leverage yang dipakai (default 10x)
      TRADE_MAX_POSITIONS   — max posisi terbuka (default 3)
      TRADE_MAX_DAILY_LOSS  — batas loss harian % (default 3%)
    """

    def __init__(self):
        self.api_key    = os.getenv('BITUNIX_API_KEY', '')
        self.secret_key = os.getenv('BITUNIX_SECRET_KEY', '')
        self.enabled    = os.getenv('TRADE_ENABLED', 'false').lower() == 'true'

        # Money management — dikontrol user
        self.risk_pct        = float(os.getenv('TRADE_RISK_PER_TRADE', '2.0'))
        self.risk_usd        = float(os.getenv('TRADE_RISK_USD', '3'))  # $3 flat untuk semua signal
        self.leverage        = int(os.getenv('TRADE_LEVERAGE', '10'))
        self.max_positions   = int(os.getenv('TRADE_MAX_POSITIONS', '5'))
        self.max_daily_loss  = float(os.getenv('TRADE_MAX_DAILY_LOSS', '10.0'))

        # Track daily PnL
        self._daily_loss_pct  = 0.0
        self._daily_loss_usd  = 0.0
        self._daily_loss_date = date.today()
        self.max_daily_loss_usd = float(os.getenv('TRADE_MAX_DAILY_LOSS_USD', '10.0'))

        # Track monthly PnL
        self._monthly_pnl_usd    = 0.0
        self._monthly_loss_usd   = 0.0
        self._monthly_profit_usd = 0.0
        self._monthly_trades     = 0
        self._monthly_wins       = 0
        self._monthly_period     = self._get_monthly_period()

        # Track yearly PnL
        self._yearly_pnl_usd     = 0.0
        self._yearly_loss_usd    = 0.0
        self._yearly_profit_usd  = 0.0
        self._yearly_trades      = 0
        self._yearly_wins        = 0

        # Session
        self._session = requests.Session()

        # Cache trading pair info — minQty, precision per coin
        self._pair_info = {}   # { 'ETHUSDT': {'min_qty': 0.01, 'precision': 4} }
        self._pair_info_loaded = False

        # Cache posisi aktif saat bot restart — untuk resume BEP monitor
        self._active_monitors = set()  # symbols yang sedang dimonitor

        # File untuk simpan data posisi aktif (entry, TP1, TP2, SL)
        # Dipakai saat bot restart untuk resume monitor dengan data yang benar
        self._positions_file = 'data/active_positions.json'
        self._saved_positions = self._load_saved_positions()

        # Reset timestamp untuk PnL display — trade SEBELUM timestamp ini
        # tidak dihitung ke monthly/yearly PnL. Diset via /reset_pnl command.
        self._pnl_reset_file = 'data/pnl_reset.json'
        self._pnl_reset_ts   = self._load_pnl_reset_ts()

        # ── Circuit breaker — consecutive loss protection ────────
        # Kalau 2 SL berturut-turut → pause trading 4 jam otomatis.
        # Reset setiap kali ada TP1 hit atau trade profit.
        self._consecutive_losses = 0
        self._circuit_breaker_until: Optional[datetime] = None
        self.circuit_breaker_max   = int(os.getenv('TRADE_CIRCUIT_BREAKER', '2'))   # default 2 SL
        self.circuit_breaker_pause = int(os.getenv('TRADE_PAUSE_HOURS', '4'))        # pause 4 jam

    @property
    def is_ready(self) -> bool:
        return bool(self.api_key and self.secret_key and self.enabled)

    # ── HTTP helpers ──────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict:
        params = params or {}
        # Bitunix signature: sort by key, concatenate key+value tanpa = atau &
        # Contoh: {"marginCoin":"USDT"} → "marginCoinUSDT"
        sorted_items = sorted(params.items())
        query_sign = "".join(f"{k}{v}" for k, v in sorted_items)
        # Query string untuk URL tetap pakai format normal key=value&key=value
        query_url  = "&".join(f"{k}={v}" for k, v in sorted_items)
        headers    = _headers(self.api_key, self.secret_key, query_params=query_sign)
        url        = f"{BASE_URL}{path}"
        try:
            r = self._session.get(url, params=params, headers=headers, timeout=10)
            return r.json()
        except Exception as e:
            logger.error(f"Bitunix GET {path}: {e}")
            return {}

    def _post(self, path: str, body: dict) -> dict:
        body_str = json.dumps(body, separators=(',', ':'))
        headers  = _headers(self.api_key, self.secret_key, body=body_str)
        url      = f"{BASE_URL}{path}"
        try:
            r = self._session.post(url, data=body_str, headers=headers, timeout=10)
            return r.json()
        except Exception as e:
            logger.error(f"Bitunix POST {path}: {e}")
            return {}

    # ── ACCOUNT ───────────────────────────────────────────────

    def _load_pair_info(self) -> dict:
        """Load trading pair info dari Bitunix — minQty, precision."""
        if self._pair_info_loaded:
            return self._pair_info
        try:
            data = self._session.get(
                f"{BASE_URL}/api/v1/futures/market/trading_pairs",
                timeout=10
            ).json()
            if data.get('code') == 0:
                for pair in data.get('data', []):
                    sym = pair.get('symbol', '')
                    self._pair_info[sym] = {
                        'min_qty'  : float(pair.get('minTradeVolume', '0.001')),
                        'precision': int(pair.get('basePrecision', 4)),
                        'max_qty'  : float(pair.get('maxMarketOrderVolume', '50000')),
                        'status'   : pair.get('symbolStatus', 'OPEN'),
                    }
                self._pair_info_loaded = True
                logger.info(f"✅ Loaded {len(self._pair_info)} trading pairs")
        except Exception as e:
            logger.warning(f"Gagal load pair info: {e}")
        return self._pair_info

    def _load_saved_positions(self) -> dict:
        """Load posisi tersimpan dari file."""
        try:
            import os, json
            os.makedirs('data', exist_ok=True)
            if os.path.exists(self._positions_file):
                with open(self._positions_file, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_position(self, symbol: str, data: dict):
        """Simpan data posisi ke file untuk resume saat restart."""
        try:
            import json, os
            os.makedirs('data', exist_ok=True)
            self._saved_positions[symbol] = data
            with open(self._positions_file, 'w') as f:
                json.dump(self._saved_positions, f, indent=2)
        except Exception as e:
            logger.warning(f"Gagal simpan posisi {symbol}: {e}")

    def _save_positions_to_file(self):
        """Flush self._saved_positions ke file (tanpa tambah/ubah entry)."""
        try:
            import json, os
            os.makedirs('data', exist_ok=True)
            with open(self._positions_file, 'w') as f:
                json.dump(self._saved_positions, f, indent=2)
        except Exception as e:
            logger.warning(f"Gagal flush saved positions: {e}")

    def _remove_saved_position(self, symbol: str):
        """Hapus posisi dari file saat sudah close."""
        try:
            import json
            self._saved_positions.pop(symbol, None)
            with open(self._positions_file, 'w') as f:
                json.dump(self._saved_positions, f, indent=2)
        except Exception:
            pass

    def get_min_qty(self, symbol: str) -> tuple:
        """Return (min_qty, precision) untuk symbol."""
        self._load_pair_info()
        sym  = symbol.upper().replace('/USDT','').replace('USDT','') + 'USDT'
        info = self._pair_info.get(sym, {})
        return (
            info.get('min_qty', 0.001),
            info.get('precision', 4),
        )

    def round_qty(self, qty: float, symbol: str) -> float:
        """Bulatkan qty sesuai precision coin. Selalu round DOWN agar risk tidak melebihi target."""
        min_qty, precision = self.get_min_qty(symbol)
        import math
        # Floor bukan round — agar qty tidak pernah melebihi target risk
        factor = 10 ** precision
        qty = math.floor(qty * factor) / factor
        return qty  # Tidak pakai max(qty, min_qty) — kalau di bawah min, place_order yang handle

    def get_balance(self) -> Optional[float]:
        """Ambil balance USDT available dari Bitunix Futures."""
        data = self._get("/api/v1/futures/account", {"marginCoin": "USDT"})
        if data.get('code') == 0:
            account = data.get('data', {})
            if isinstance(account, dict):
                return float(account.get('available', 0))
            return 0.0
        logger.error(f"get_balance error: {data.get('msg', 'unknown')}")
        return None

    def get_positions(self) -> list:
        """Ambil semua posisi terbuka."""
        data = self._get("/api/v1/futures/position/get_pending_positions")
        if data.get('code') == 0:
            result = data.get('data', [])
            # API bisa return list langsung atau dict dengan positionList
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return result.get('positionList', [])
        return []

    def get_open_position(self, symbol: str) -> Optional[dict]:
        """Ambil posisi terbuka untuk coin tertentu."""
        positions = self.get_positions()
        sym = symbol.upper().replace('/USDT', '').replace('USDT', '') + 'USDT'
        for pos in positions:
            pos_sym = pos.get('symbol', '').upper()
            if pos_sym == sym:
                return pos
        return None

    def has_pending_order(self, symbol: str) -> bool:
        """
        Cek apakah ada pending LIMIT order untuk coin ini.
        Berbeda dengan get_open_position yang hanya cek posisi FILLED.
        Penting untuk mencegah dobel order saat bot restart — limit order
        yang belum trigger tidak muncul di get_positions tapi tetap aktif.
        """
        sym = symbol.upper().replace('/USDT', '').replace('USDT', '') + 'USDT'
        try:
            data = self._get("/api/v1/futures/trade/get_pending_orders", {"symbol": sym})
            if data.get('code') != 0:
                return False
            orders = data.get('data', {})
            if isinstance(orders, dict):
                order_list = orders.get('orderList', [])
            elif isinstance(orders, list):
                order_list = orders
            else:
                order_list = []
            # Filter order yang masih aktif (belum filled/canceled)
            for o in order_list:
                o_sym = o.get('symbol', '').upper()
                if o_sym == sym:
                    return True
            return False
        except Exception as e:
            logger.debug(f"has_pending_order {sym}: {e}")
            return False

    def get_all_pending_orders(self) -> list:
        """
        Return semua pending limit orders (yang belum filled).
        Dipakai untuk tampilan /posisi agar user tahu ada order pending.
        """
        try:
            data = self._get("/api/v1/futures/trade/get_pending_orders", {})
            if data.get('code') != 0:
                return []
            orders = data.get('data', {})
            if isinstance(orders, dict):
                return orders.get('orderList', [])
            if isinstance(orders, list):
                return orders
            return []
        except Exception as e:
            logger.debug(f"get_all_pending_orders: {e}")
            return []

    # ── SET LEVERAGE ──────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        sym  = symbol.upper().replace('/USDT', '').replace('USDT', '') + 'USDT'
        data = self._post("/api/v1/futures/account/change_leverage", {
            "symbol"      : sym,
            "leverage"    : str(leverage),
            "marginType"  : "CROSS",    # Cross margin
        })
        ok = data.get('code') == 0
        if not ok:
            logger.warning(f"set_leverage {sym}x{leverage}: {data.get('msg')}")
        return ok

    # ── PLACE ORDER ───────────────────────────────────────────

    def place_order(self, symbol: str, direction: str,
                    entry: float, sl: float, tp1: float, tp2: float,
                    qty: Optional[float] = None,
                    quality: str = 'GOOD',
                    signal_data: dict = None,
                    btc_state: str = 'NEUTRAL',
                    notify_fn=None) -> dict:
        """
        Open posisi futures dengan SL + TP1 + TP2.

        Strategi TP:
          - TP1 (50% qty) → limit reduce-only order di harga TP1
          - TP2 (50% qty) → attached TP di order utama
          - SL → attached SL di order utama (full qty)

        Kalau TP1 kena → 50% posisi close, 50% masih jalan
        Kalau TP2 kena → sisa 50% close, trade selesai
        Kalau SL kena   → semua posisi close (full protection)

        Position sizing berdasarkan quality:
          IDEAL → 1.5x risk (signal terkuat, konfluensi penuh)
          GOOD  → 1.0x risk (standar)
        """
        if not self.is_ready:
            return {'ok': False, 'msg': 'Auto trade tidak aktif'}

        sym     = symbol.upper().replace('/USDT','').replace('USDT','') + 'USDT'
        side    = "BUY" if direction == "LONG" else "SELL"
        is_long = direction == "LONG"

        # Cek circuit breaker — pause setelah consecutive SL
        if self.is_circuit_breaker_active():
            remaining = (self._circuit_breaker_until - datetime.now()).seconds // 60
            return {'ok': False, 'msg': f'⛔ Circuit breaker aktif — pause {remaining} menit lagi ({self._consecutive_losses} SL berturut-turut)'}

        # Cek daily loss limit — sync dari exchange dulu
        self.sync_daily_loss_from_exchange()
        if self._is_daily_loss_exceeded():
            return {'ok': False, 'msg': f'Daily loss limit ${self.max_daily_loss_usd:.0f} tercapai — trading berhenti hari ini'}

        # Cek max open positions
        open_pos = self.get_positions()
        max_pos  = self.max_positions   # pakai setting langsung (default 5)
        if len(open_pos) >= max_pos:
            return {'ok': False, 'msg': f'Max {max_pos} posisi terbuka'}

        # Cek sudah ada posisi di coin ini
        if self.get_open_position(symbol):
            return {'ok': False, 'msg': f'Sudah ada posisi {sym} terbuka — skip'}

        # Cek blocklist (dari /skip command)
        try:
            import json, os as _os
            bl_path = 'data/coin_blocklist.json'
            if _os.path.exists(bl_path):
                with open(bl_path) as f:
                    blocklist = set(json.load(f))
                clean_check = symbol.upper().replace('USDT', '')
                if clean_check in blocklist:
                    return {'ok': False, 'msg': f'{clean_check} di-block (lihat /blocklist)'}
        except Exception:
            pass

        # Cek juga pending LIMIT order — penting saat bot restart,
        # limit yang belum trigger tidak muncul di get_positions
        if self.has_pending_order(symbol):
            return {'ok': False, 'msg': f'Sudah ada limit order {sym} pending — skip'}

        # ── Hitung qty dari nominal risk (USD tetap) ─────────
        if qty is None:
            risk_per_unit = abs(entry - sl)
            if risk_per_unit <= 0:
                return {'ok': False, 'msg': 'SL sama dengan entry — invalid'}

            # risk_usd = nominal USD yang di-risk (misal $6 atau $3)
            # Kalau risk_usd tidak di-set, fallback ke % dari balance
            if self.risk_usd > 0:
                risk_amount = self.risk_usd
            else:
                balance = self.get_balance()
                if not balance or balance <= 0:
                    return {'ok': False, 'msg': 'Gagal ambil balance'}
                risk_amount = balance * (self.risk_pct / 100)

            # Opsi A (2026-04-24): Quality-based risk sizing (Kelly lite).
            # Data 105 trades: GOOD WR 57% EV +0.39R | WAIT WR 45% EV +0.11R
            # Backtest: naik 34% total PnL tanpa ubah volume.
            # Update 2026-04-24: base risk $0.50 (validasi ketat).
            #   IDEAL=1.5x → $0.75 | GOOD=1.0x → $0.50
            #   MODERATE=0.7x → $0.35 | WAIT=0.6x → $0.30
            _quality_mult = {
                'IDEAL': 1.5,
                'GOOD': 1.0,
                'MODERATE': 0.7,
                'WAIT': 0.6,
            }.get(quality.upper(), 1.0)
            risk_amount *= _quality_mult

            raw_qty  = risk_amount / risk_per_unit
            qty      = self.round_qty(raw_qty, symbol)
            min_qty, _ = self.get_min_qty(symbol)

            if qty <= 0:
                return {'ok': False, 'msg': f'Qty terlalu kecil: {qty}'}
            if qty < min_qty:
                return {'ok': False, 'msg': f'Qty {qty} di bawah minimum {min_qty} untuk {sym}'}

            logger.info(f"💰 {sym} [{quality}] risk=${risk_amount:.2f} entry={entry} sl={sl} → qty={qty}")

        # TP1 = 50% qty (reduce-only), TP2 = sisa 50%
        _, precision = self.get_min_qty(symbol)
        qty_tp1 = round(qty * 0.5, precision)
        qty_tp2 = round(qty - qty_tp1, precision)
        if qty_tp1 <= 0:
            qty_tp1 = qty
            qty_tp2 = 0

        # ── Set leverage ──────────────────────────────────────
        self.set_leverage(symbol, self.leverage)

        # ── Tentukan order type ───────────────────────────────
        # Selalu LIMIT di entry price — hindari slippage MARKET.
        # Hanya MARKET kalau entry=0 (tidak ada level spesifik).
        use_market = (entry == 0)
        order_type = "MARKET" if use_market else "LIMIT"

        # ── STEP 1: Open posisi utama dengan SL + TP2 ────────
        body = {
            "symbol"      : sym,
            "side"        : side,
            "tradeSide"   : "OPEN",
            "orderType"   : order_type,
            "qty"         : str(qty),
            "reduceOnly"  : False,
            "effect"      : "GTC",
            "clientId"    : f"bot_{int(time.time())}",
            # SL — full protection
            "slPrice"     : str(round(sl, 8)),
            "slStopType"  : "MARK_PRICE",
            "slOrderType" : "MARKET",
            # TP2 — sisa 50% posisi
            "tpPrice"     : str(round(tp2, 8)),
            "tpStopType"  : "MARK",
            "tpOrderType" : "MARKET",
        }
        if order_type == "LIMIT":
            body["price"] = str(round(entry, 8))

        logger.info(f"📤 Open {sym} {direction} {order_type} qty={qty} SL={sl} TP2={tp2}")
        result = self._post("/api/v1/futures/trade/place_order", body)

        if result.get('code') != 0:
            msg = result.get('msg', 'Unknown error')
            logger.error(f"❌ Open order gagal {sym}: {msg}")
            return {'ok': False, 'msg': f"Open order gagal: {msg}"}

        order_id = result.get('data', {}).get('orderId', '')
        logger.info(f"✅ Open order OK: {sym} orderId={order_id}")

        # ── STEP 2: Set TP1 sebagai reduce-only limit order ───
        tp1_order_id = ''
        close_side   = "SELL" if is_long else "BUY"

        if order_type == "MARKET":
            # MARKET order: posisi langsung terbuka → pasang TP1 sekarang
            if qty_tp1 > 0:
                for attempt in range(8):
                    time.sleep(2.0)
                    pos = self.get_open_position(symbol)
                    if pos:
                        pos_id   = pos.get('positionId', '')
                        tp1_body = {
                            "symbol"     : sym,
                            "side"       : close_side,
                            "tradeSide"  : "CLOSE",
                            "orderType"  : "LIMIT",
                            "price"      : str(round(tp1, 8)),
                            "qty"        : str(qty_tp1),
                            "positionId" : str(pos_id),
                            "reduceOnly" : True,
                            "effect"     : "GTC",
                            "clientId"   : f"tp1_{int(time.time())}",
                        }
                        r = self._post("/api/v1/futures/trade/place_order", tp1_body)
                        if r.get('code') == 0:
                            tp1_order_id = r.get('data', {}).get('orderId', '')
                            logger.info(f"✅ TP1 order OK: {sym} qty={qty_tp1} @ {tp1}")
                            break
                        else:
                            logger.warning(f"⚠️ TP1 attempt {attempt+1} gagal: {r.get('msg','')}")
                    else:
                        logger.info(f"⏳ TP1 retry {attempt+1}: posisi belum terbuka")

                if not tp1_order_id:
                    logger.warning(f"⚠️ TP1 tidak terpasang {sym} — hanya TP2 dan SL aktif")

        else:
            # LIMIT order: posisi belum terbuka — TP1 dipasang via monitor background
            # Monitor akan deteksi saat entry kena dan langsung pasang TP1
            logger.info(f"⏳ LIMIT order {sym} — TP1 akan dipasang otomatis saat entry kena")
            self._start_limit_entry_monitor(
                symbol=symbol, direction=direction,
                entry=entry, sl=sl, tp1=tp1, tp2=tp2,
                qty_tp1=qty_tp1, close_side=close_side,
                order_id=order_id, notify_fn=notify_fn,
            )

        # Simpan data posisi ke file untuk resume saat bot restart
        clean_sym = sym.replace('USDT', '')
        # Simpan reasons & faktor signal untuk explainer saat trade selesai
        sig_reasons    = (signal_data or {}).get('reasons', [])
        sig_score      = (signal_data or {}).get('confluence_score', 0)
        sig_quality    = (signal_data or {}).get('quality', quality)
        sig_kill_count = (signal_data or {}).get('kill_count', 0)
        sig_level_used = (signal_data or {}).get('level_used', '')

        self._save_position(clean_sym, {
            'symbol'     : clean_sym,
            'direction'  : direction,
            'entry'      : entry,
            'sl'         : sl,
            'tp1'        : tp1,
            'tp2'        : tp2,
            'qty'        : qty,
            'leverage'   : self.leverage,
            'opened_at'  : time.strftime('%Y-%m-%d %H:%M:%S'),
            'reasons'    : sig_reasons[:8],   # max 8 reason
            'score'      : sig_score,
            'quality'    : sig_quality,
            'kill_count' : sig_kill_count,
            'level_used' : sig_level_used,
        })

        # ── Push signal ke website (Neon DB) ──────────────────
        try:
            strategy = (signal_data or {}).get('_strategy', 'swing')
            self._push_signal_to_web({
                'symbol'    : clean_sym,
                'direction' : direction,
                'strategy'  : strategy,
                'quality'   : sig_quality,
                'score'     : sig_score,
                'entry'     : entry,
                'sl'        : sl,
                'tp1'       : tp1,
                'tp2'       : tp2,
                'rr'        : (signal_data or {}).get('rr', 0),
                'reasons'   : sig_reasons[:6],
                'executed'  : True,
            })

            # Broadcast signal ke semua paid subscriber via Telegram
            try:
                ico   = "🟢" if direction == "LONG" else "🔴"
                strat = (signal_data or {}).get('_strategy', 'swing').upper()
                msg   = (
                    f"📡 SIGNAL BARU — {strat}\n"
                    f"{'=' * 28}\n"
                    f"{ico} {clean_sym} {direction}\n"
                    f"Entry  : {entry}\n"
                    f"SL     : {sl}\n"
                    f"TP1    : {tp1}\n"
                    f"TP2    : {tp2}\n"
                    f"Quality: {sig_quality}  Score: {sig_score}\n"
                    f"\n👁️ Lihat detail di dashboard."
                )
                self._broadcast_to_subscribers(msg)
            except Exception as _bce:
                logger.debug(f"Broadcast subscribers error: {_bce}")
        except Exception as _pe:
            logger.debug(f"Push signal to web error: {_pe}")

        # ── Push posisi ke web HANYA kalau MARKET (langsung running) ─
        # LIMIT order: posisi belum terbuka, akan di-push dari _start_limit_entry_monitor
        if order_type == "MARKET":
            try:
                strategy = (signal_data or {}).get('_strategy', 'swing')
                self._push_position_to_web({
                    'symbol'    : clean_sym,
                    'direction' : direction,
                    'strategy'  : strategy,
                    'quality'   : sig_quality,
                    'entry'     : entry,
                    'sl'        : sl,
                    'tp1'       : tp1,
                    'tp2'       : tp2,
                    'rr'        : (signal_data or {}).get('rr', 0),
                    'qty'       : qty,
                    'leverage'  : self.leverage,
                    'reasons'   : sig_reasons[:6],
                })
                # Signal langsung filled (MARKET order fills instan)
                self._patch_signal_status(clean_sym, 'filled')
            except Exception as _ppe:
                logger.debug(f"Push position to web error: {_ppe}")

        # ── Log ke learning engine ────────────────────────────
        # Catat kondisi sinyal saat entry untuk analisa pola nanti
        try:
            if signal_data:
                from learning_engine import get_learning_engine
                from session_filter import get_current_session
                le        = get_learning_engine()
                sess_info = get_current_session()
                le.log_entry(
                    symbol    = clean_sym,
                    direction = direction,
                    signal    = signal_data,
                    btc_state = btc_state,
                    session   = sess_info.get('session', 'UNKNOWN'),
                    smc       = signal_data.get('_smc', {}),
                    adx       = signal_data.get('_adx', 0.0),
                )
        except Exception as _le_err:
            logger.debug(f"learning log_entry skip: {_le_err}")

        return {
            'ok'          : True,
            'order_id'    : order_id,
            'tp1_order_id': tp1_order_id,
            'symbol'      : sym,
            'direction'   : direction,
            'qty'         : qty,
            'qty_tp1'     : qty_tp1,
            'qty_tp2'     : qty_tp2,
            'entry'       : entry,
            'sl'          : sl,
            'tp1'         : tp1,
            'tp2'         : tp2,
            'leverage'    : self.leverage,
            'risk_pct'    : self.risk_pct,
            'msg'         : (
                f"✅ {direction} {sym} terbuka!\n"
                f"   Qty: {qty} | Leverage: {self.leverage}x | Risk: {self.risk_pct}%\n"
                f"   SL : {sl}\n"
                f"   TP1: {tp1} ({qty_tp1} lot — 50%)\n"
                f"   TP2: {tp2} ({qty_tp2} lot — 50%)\n"
                f"   🔄 Auto BEP aktif setelah TP1 kena"
            ),
        }

    # ── CLOSE POSITION ────────────────────────────────────────

    def close_position(self, symbol: str) -> dict:
        """Close posisi dengan market order."""
        sym = symbol.upper().replace('/USDT', '').replace('USDT', '') + 'USDT'

        pos = self.get_open_position(symbol)
        if not pos:
            return {'ok': False, 'msg': f'Tidak ada posisi terbuka untuk {sym}'}

        pos_id     = pos.get('positionId', '')
        qty        = pos.get('qty', '0')
        side       = pos.get('side', '')
        close_side = "SELL" if side == "BUY" else "BUY"

        body = {
            "symbol"    : sym,
            "side"      : close_side,
            "tradeSide" : "CLOSE",
            "orderType" : "MARKET",
            "qty"       : str(qty),
            "positionId": str(pos_id),
            "reduceOnly": True,
            "effect"    : "GTC",
            "clientId"  : f"bot_close_{int(time.time())}",
        }

        result = self._post("/api/v1/futures/trade/place_order", body)
        if result.get('code') == 0:
            pnl = float(pos.get('unrealizedPNL', 0))
            if pnl < 0:
                self._update_daily_loss_usd(abs(pnl))  # track USD langsung
                balance = self.get_balance()
                if balance and balance > 0:
                    self._update_daily_loss(abs(pnl) / balance * 100)
            # Hapus dari saved positions
            clean_sym = sym.replace('USDT', '')
            self._remove_saved_position(clean_sym)
            self._active_monitors.discard(clean_sym)
            return {'ok': True, 'msg': f'Posisi {sym} ditutup. PnL: {pnl:+.2f} USDT'}
        else:
            return {'ok': False, 'msg': f"Close gagal: {result.get('msg', 'error')}"}

    def move_sl_to_bep(self, symbol: str, entry_price: float) -> dict:
        """
        Geser SL ke harga entry (Break Even Point).
        Dipanggil otomatis setelah TP1 kena.
        """
        sym = symbol.upper().replace('/USDT', '').replace('USDT', '') + 'USDT'
        pos = self.get_open_position(symbol)
        if not pos:
            return {'ok': False, 'msg': f'Posisi {sym} tidak ada (mungkin sudah close)'}

        pos_id = pos.get('positionId', '')
        sl_str = str(round(entry_price, 8))
        logger.info(f"🔄 Geser SL {sym} ke BEP {entry_price} (positionId={pos_id})")

        # Step 1: Cancel semua SL aktif untuk posisi ini dulu
        try:
            tpsl_data = self._get("/api/v1/futures/tpsl/get_pending_orders",
                                  {"symbol": sym})
            tpsl_list = tpsl_data.get('data', [])
            if isinstance(tpsl_list, list):
                for order in tpsl_list:
                    if (order.get('positionId') == str(pos_id) and
                            order.get('slPrice') is not None):
                        cancel_id = order.get('id', '')
                        cancel_r  = self._post(
                            "/api/v1/futures/tpsl/cancel_order",
                            {"symbol": sym, "orderId": str(cancel_id)}
                        )
                        logger.info(f"Cancel SL {cancel_id}: {cancel_r.get('code')} {cancel_r.get('msg','')}")
                        time.sleep(0.3)
            time.sleep(0.5)
        except Exception as ce:
            logger.debug(f"Cancel SL error: {ce}")

        # Step 2: Pasang SL BEP baru
        body = {
            "symbol"      : sym,
            "positionId"  : str(pos_id),
            "slPrice"     : sl_str,
            "slStopType"  : "MARK_PRICE",
            "slOrderType" : "MARKET",
        }
        result = self._post("/api/v1/futures/tpsl/position/place_order", body)
        logger.info(f"BEP place_order: code={result.get('code')} msg={result.get('msg','')} orderId={result.get('data',{}).get('orderId','')}")

        if result.get('code') == 0:
            order_id = result.get('data', {}).get('orderId', '')
            logger.info(f"✅ SL {sym} BEP terpasang @ {entry_price} (orderId={order_id})")
            return {'ok': True, 'msg': f'SL BEP terpasang @ {entry_price}'}

        err = result.get('msg', 'error')
        logger.warning(f"⚠️ GAGAL geser SL {sym} ke BEP: {err}")
        return {'ok': False, 'msg': f'Gagal geser SL: {err}'}

    def move_sl_trailing(self, symbol: str, new_sl: float) -> dict:
        """
        Geser SL ke harga baru (trailing stop).
        Dipanggil setelah TP1 kena dan harga terus bergerak menguntungkan.
        """
        return self.move_sl_to_bep(symbol, new_sl)

    def _start_limit_entry_monitor(self, symbol, direction, entry, sl,
                                   tp1, tp2, qty_tp1, close_side, order_id,
                                   notify_fn=None):
        """
        Monitor background untuk LIMIT order.
        Tugasnya:
        1. Tunggu sampai entry kena (posisi terbuka di exchange)
        2. Pasang TP1 reduce-only setelah posisi terbuka
        3. Start TP1 monitor untuk geser SL ke BEP saat TP1 kena
        """
        import threading

        def _monitor():
            sym      = symbol.upper().replace('/USDT','').replace('USDT','') + 'USDT'
            max_wait = 48 * 3600  # tunggu max 48 jam
            interval = 30         # cek setiap 30 detik
            elapsed  = 0
            tp1_placed = False

            logger.info(f"⏳ Limit entry monitor START {sym} @ {entry}")

            while elapsed < max_wait:
                time.sleep(interval)
                elapsed += interval

                try:
                    # Cek apakah posisi sudah terbuka
                    pos = self.get_open_position(symbol)

                    if pos:
                        # Posisi terbuka — entry sudah kena!
                        if not tp1_placed and qty_tp1 > 0:
                            pos_id = pos.get('positionId', '')
                            logger.info(f"🎯 LIMIT entry kena {sym} — pasang TP1 @ {tp1}")

                            tp1_body = {
                                "symbol"     : sym,
                                "side"       : close_side,
                                "tradeSide"  : "CLOSE",
                                "orderType"  : "LIMIT",
                                "price"      : str(round(tp1, 8)),
                                "qty"        : str(qty_tp1),
                                "positionId" : str(pos_id),
                                "reduceOnly" : True,
                                "effect"     : "GTC",
                                "clientId"   : f"tp1_{int(time.time())}",
                            }
                            r = self._post("/api/v1/futures/trade/place_order", tp1_body)
                            if r.get('code') == 0:
                                tp1_order_id = r.get('data', {}).get('orderId', '')
                                logger.info(f"✅ TP1 terpasang {sym} @ {tp1} (orderId={tp1_order_id})")
                                tp1_placed = True

                                # ── Push posisi ke web (limit fill = posisi running) ──
                                try:
                                    clean_sym = sym.replace('USDT', '')
                                    saved     = self._saved_positions.get(clean_sym, {})
                                    actual_entry_px = float(pos.get('avgOpenPrice', entry))
                                    self._push_position_to_web({
                                        'symbol'    : clean_sym,
                                        'direction' : direction,
                                        'strategy'  : saved.get('_strategy', 'swing'),
                                        'quality'   : saved.get('quality'),
                                        'entry'     : actual_entry_px,
                                        'sl'        : sl,
                                        'tp1'       : tp1,
                                        'tp2'       : tp2,
                                        'qty'       : saved.get('qty'),
                                        'leverage'  : self.leverage,
                                        'reasons'   : (saved.get('reasons') or [])[:6],
                                    })
                                    # Signal pending → filled
                                    self._patch_signal_status(clean_sym, 'filled')
                                except Exception as _ppe:
                                    logger.debug(f"Push position (limit fill) error: {_ppe}")

                                # Kirim notif ke Telegram
                                if notify_fn:
                                    try:
                                        import asyncio
                                        loop = asyncio.new_event_loop()
                                        actual_entry = float(pos.get('avgOpenPrice', entry))
                                        ico = "🟢" if direction == "LONG" else "🔴"
                                        msg = (
                                            "✅ LIMIT ENTRY KENA\n" +
                                            "=" * 28 + "\n" +
                                            ico + " " + sym + " " + direction + "\n" +
                                            "Entry  : " + str(round(actual_entry, 8)) + "\n" +
                                            "TP1    : " + str(round(tp1, 8)) + "\n" +
                                            "TP2    : " + str(round(tp2, 8)) + "\n" +
                                            "SL     : " + str(round(sl, 8)) + "\n\n" +
                                            "👁️ TP1 monitor aktif — SL geser ke BEP saat TP1 kena"
                                        )
                                        loop.run_until_complete(notify_fn(msg))
                                        loop.close()
                                    except Exception:
                                        pass

                                # Start TP1 monitor untuk BEP
                                actual_entry = float(pos.get('avgOpenPrice', entry))
                                self.start_tp1_monitor(
                                    symbol=symbol,
                                    entry=actual_entry,
                                    tp1=tp1,
                                    direction=direction,
                                    notify_fn=notify_fn,
                                )
                                break  # selesai — tp1 monitor yang lanjut

                            else:
                                logger.warning(f"⚠️ Gagal pasang TP1 {sym}: {r.get('msg','')}")
                                # Retry di loop berikutnya

                    else:
                        # Cek apakah limit order masih pending
                        pending = self._get("/api/v1/futures/trade/get_pending_orders",
                                           {"symbol": sym})
                        orders  = pending.get('data', {})
                        if isinstance(orders, dict):
                            order_list = orders.get('orderList', [])
                        else:
                            order_list = []

                        still_pending = any(
                            o.get('orderId') == str(order_id)
                            for o in order_list
                        )

                        if not still_pending and not pos:
                            # Order sudah tidak ada dan posisi tidak terbuka
                            # Kemungkinan order di-cancel manual atau expired
                            logger.info(f"⚠️ Limit order {sym} tidak lagi pending — stop monitor")
                            break

                except Exception as e:
                    logger.debug(f"Limit entry monitor {sym} error: {e}")

            logger.info(f"⏳ Limit entry monitor SELESAI {sym}")

        t = threading.Thread(
            target=_monitor, daemon=True,
            name=f"limit_monitor_{symbol}"
        )
        t.start()
        logger.info(f"⏳ Limit entry monitor started untuk {symbol}")

    def resume_monitors_on_startup(self, notify_fn=None):
        """
        Resume semua monitor setelah bot restart:
        1. Posisi filled tanpa TP1 order → pasang TP1 retroactively + start TP1 monitor
        2. Pending limit order → re-start limit entry monitor (agar TP1 ke-pasang saat fill)
        """
        if not self.is_ready:
            return

        try:
            positions = self.get_positions()
            pending   = self.get_all_pending_orders()

            # ── STEP 1: Resume monitor untuk posisi FILLED ──
            resumed_filled = 0
            for pos in positions:
                sym_pair = pos.get('symbol', '')
                sym      = sym_pair.replace('USDT', '')
                side     = pos.get('side', '')
                direction = 'LONG' if side in ('BUY', 'LONG') else 'SHORT'
                entry    = float(pos.get('avgOpenPrice', 0))
                qty      = float(pos.get('qty', 0))
                pos_id   = pos.get('positionId', '')

                if not entry or sym in self._active_monitors:
                    continue

                saved = self._saved_positions.get(sym, {})
                tp1   = float(saved.get('tp1', 0))
                sl    = float(saved.get('sl', 0))

                # Fallback TP1 kalau tidak tersimpan
                if not tp1:
                    if sl:
                        risk = abs(entry - sl)
                        tp1 = entry + risk * 1.5 if direction == 'LONG' else entry - risk * 1.5
                    else:
                        tp1 = entry * 1.015 if direction == 'LONG' else entry * 0.985

                # Cek apakah TP1 reduce-only order sudah ada di exchange
                has_tp1_order = any(
                    o.get('symbol', '').upper() == sym_pair.upper()
                    and (o.get('reduceOnly') in (True, 'true', 'TRUE', 1))
                    for o in pending
                )

                # Cek apakah TP1 SUDAH PERNAH KENA — jangan re-place!
                tp1_already_hit = bool(saved.get('tp1_hit', False))

                # Fallback detection: qty sekarang < qty_original × 0.7
                # (artinya sudah partial closed, kemungkinan via TP1)
                qty_original = float(saved.get('qty', 0))
                if qty_original > 0 and qty < qty_original * 0.7:
                    tp1_already_hit = True
                    logger.info(f"ℹ️ {sym}: qty {qty} < {qty_original*0.7:.4f} "
                                f"(qty awal {qty_original}) — TP1 sudah kena")

                # Kalau TP1 belum ada DAN belum pernah kena → pasang baru
                if not has_tp1_order and not tp1_already_hit and qty > 0 and tp1 > 0:
                    try:
                        _, precision = self.get_min_qty(sym)
                        qty_tp1 = round(qty * 0.5, precision)
                        if qty_tp1 > 0:
                            close_side = "SELL" if direction == "LONG" else "BUY"
                            tp1_body = {
                                "symbol"     : sym_pair,
                                "side"       : close_side,
                                "tradeSide"  : "CLOSE",
                                "orderType"  : "LIMIT",
                                "price"      : str(round(tp1, 8)),
                                "qty"        : str(qty_tp1),
                                "positionId" : str(pos_id),
                                "reduceOnly" : True,
                                "effect"     : "GTC",
                                "clientId"   : f"tp1resume_{int(time.time())}",
                            }
                            r = self._post("/api/v1/futures/trade/place_order", tp1_body)
                            if r.get('code') == 0:
                                logger.info(f"🔧 TP1 dipasang retroactively {sym} @ {tp1} (dari restart)")
                            else:
                                logger.warning(f"⚠️ Gagal pasang TP1 retro {sym}: {r.get('msg','')}")
                    except Exception as _e:
                        logger.warning(f"TP1 retro error {sym}: {_e}")

                logger.info(f"🔄 Resume monitor {sym} {direction} entry={entry} tp1={tp1}")
                self.start_tp1_monitor(sym, entry, tp1, direction, notify_fn=notify_fn)
                resumed_filled += 1

            # ── STEP 2: Resume limit entry monitor untuk PENDING limit ──
            resumed_limit = 0
            filled_syms = {pos.get('symbol', '').upper() for pos in positions}

            for o in pending:
                o_sym = o.get('symbol', '').upper()
                if not o_sym.endswith('USDT'):
                    continue
                # Skip kalau posisi untuk symbol ini sudah filled (bukan entry order lagi)
                if o_sym in filled_syms:
                    continue
                # Skip reduce-only (itu TP/SL)
                if o.get('reduceOnly') in (True, 'true', 'TRUE', 1):
                    continue

                sym = o_sym.replace('USDT', '')
                saved = self._saved_positions.get(sym, {})
                if not saved:
                    continue  # tidak ada data saved, skip

                direction = saved.get('direction', '')
                entry_p = float(saved.get('entry', 0))
                sl_p    = float(saved.get('sl', 0))
                tp1_p   = float(saved.get('tp1', 0))
                tp2_p   = float(saved.get('tp2', 0))
                qty_p   = float(saved.get('qty', 0))

                if not (direction and entry_p and tp1_p and qty_p):
                    continue

                _, precision = self.get_min_qty(sym)
                qty_tp1 = round(qty_p * 0.5, precision)
                close_side = "SELL" if direction == "LONG" else "BUY"
                order_id = o.get('orderId', '')

                self._start_limit_entry_monitor(
                    symbol=sym, direction=direction,
                    entry=entry_p, sl=sl_p, tp1=tp1_p, tp2=tp2_p,
                    qty_tp1=qty_tp1, close_side=close_side,
                    order_id=order_id, notify_fn=notify_fn,
                )
                resumed_limit += 1
                logger.info(f"🔄 Resume limit monitor {sym} {direction} @ {entry_p}")

            if resumed_filled:
                logger.info(f"✅ {resumed_filled} TP1 monitor di-resume setelah restart")
            if resumed_limit:
                logger.info(f"✅ {resumed_limit} limit entry monitor di-resume setelah restart")

        except Exception as e:
            logger.error(f"resume_monitors_on_startup error: {e}")

    def start_tp1_monitor(self, symbol: str, entry: float, tp1: float,
                          direction: str, notify_fn=None, level_price: float = 0.0):
        """
        Monitor posisi di background — kalau TP1 kena, geser SL ke BEP.

        notify_fn: fungsi async untuk kirim notifikasi ke Telegram (opsional)
        level_price: harga level S/R yang jadi dasar sinyal (untuk level_memory)
        """
        import threading

        def _monitor():
            sym          = symbol.upper().replace('/USDT','').replace('USDT','') + 'USDT'
            is_long      = direction == 'LONG'
            max_wait     = 72 * 3600
            interval     = 30
            elapsed      = 0
            bep_done     = False
            stage2_done  = False
            stage3_done  = False
            stage4_sl    = None   # Opsi D: runner trail SL setelah +3R
            extreme_px   = entry  # high/low ekstrem sejak entry
            initial_qty  = None   # catat qty awal posisi
            bep_attempts = 0      # batasi percobaan BEP

            logger.info(f"👁️ Monitor TP1 {sym}: target={tp1}, BEP={entry}")

            while elapsed < max_wait:
                time.sleep(interval)
                elapsed += interval

                try:
                    pos = self.get_open_position(symbol)
                    if not pos:
                        logger.info(f"👁️ Monitor {sym}: posisi sudah close — stop")
                        break

                    current = self._get_current_price(sym)
                    if current <= 0:
                        continue

                    pos_qty = float(pos.get('qty', 0))

                    # Catat qty awal sekali saja
                    if initial_qty is None:
                        initial_qty = pos_qty
                        logger.info(f"👁️ {sym} initial qty={initial_qty}")
                        continue

                    # ── Deteksi TP1 kena ─────────────────────────
                    price_hit   = (is_long and current >= tp1) or (not is_long and current <= tp1)
                    qty_reduced = pos_qty > 0 and pos_qty < initial_qty * 0.7

                    tp1_hit = price_hit or qty_reduced

                    # ── TRAILING SL STAGES ────────────────────────
                    # Stage 1 (TP1 hit): SL → BEP (break even)
                    # Stage 2 (harga +1.5R dari entry): SL → +0.5R
                    # Stage 3 (harga +2.0R dari entry): SL → +1.0R (lock profit penuh)
                    risk_dist = abs(tp1 - entry)   # 1R = jarak entry ke TP1

                    if tp1_hit and not bep_done and bep_attempts == 0:
                        bep_attempts += 1
                        logger.info(f"🎯 TP1 {sym} kena @ {current} — geser SL ke BEP {entry}")
                        result = self.move_sl_to_bep(symbol, entry)
                        if result.get('ok'):
                            bep_done = True
                            logger.info(f"✅ BEP terpasang {sym} @ {entry} — monitor lanjut TP2")
                            # Simpan flag tp1_hit supaya resume_monitors tidak re-place TP1
                            try:
                                clean_sym = sym.replace('USDT', '')
                                saved = self._saved_positions.get(clean_sym, {})
                                saved['tp1_hit'] = True
                                saved['tp1_hit_at'] = datetime.now().isoformat()
                                self._saved_positions[clean_sym] = saved
                                self._save_positions_to_file()
                            except Exception as _se:
                                logger.debug(f"Save tp1_hit flag gagal: {_se}")
                            # Patch state posisi di web — TP1 hit + BEP active
                            try:
                                clean_sym = sym.replace('USDT', '')
                                self._patch_position_state(
                                    clean_sym, tp1_hit=True, bep_active=True, sl=entry
                                )
                            except Exception as _pse:
                                logger.debug(f"Patch position state error: {_pse}")
                        else:
                            bep_attempts = 0
                            logger.warning(f"⚠️ BEP gagal {sym}: {result.get('msg')} — retry berikutnya")
                            continue

                        # Notif TP1 kena — hanya sekali
                        if notify_fn and callable(notify_fn):
                            try:
                                import asyncio
                                loop = asyncio.new_event_loop()
                                loop.run_until_complete(notify_fn(
                                    "🎯 TP1 KENA — " + sym + "\n" +
                                    "   Harga : " + str(round(current, 8)) + "\n" +
                                    "   BEP   : " + str(round(entry, 8)) + "\n" +
                                    "   50% profit aman, sisanya jalan ke TP2"
                                ))
                                loop.close()
                            except Exception:
                                pass

                    # ── Stage 2: harga +1.5R dari entry → SL ke +0.5R ────
                    if bep_done and not stage2_done and risk_dist > 0:
                        trigger_2  = entry + risk_dist * 1.5 if is_long else entry - risk_dist * 1.5
                        sl_lock_2  = entry + risk_dist * 0.5 if is_long else entry - risk_dist * 0.5
                        stage2_hit = (is_long and current >= trigger_2) or (not is_long and current <= trigger_2)
                        if stage2_hit:
                            logger.info(f"📈 Stage2 {sym}: harga {current} melewati +1.5R — geser SL ke +0.5R ({sl_lock_2:.6g})")
                            r2 = self.move_sl_trailing(symbol, sl_lock_2)
                            if r2.get('ok'):
                                stage2_done = True
                                logger.info(f"✅ Stage2 SL terpasang {sym} @ {sl_lock_2:.6g}")
                                if notify_fn and callable(notify_fn):
                                    try:
                                        import asyncio
                                        loop = asyncio.new_event_loop()
                                        loop.run_until_complete(notify_fn(
                                            "📈 TRAILING SL STAGE 2 — " + sym + "\n" +
                                            "   Harga : " + str(round(current, 8)) + "\n" +
                                            "   SL baru: " + str(round(sl_lock_2, 8)) + " (+0.5R terkunci)"
                                        ))
                                        loop.close()
                                    except Exception:
                                        pass
                            else:
                                logger.warning(f"⚠️ Stage2 SL gagal {sym}: {r2.get('msg')}")

                    # ── Stage 3: harga +2.0R dari entry → SL ke +1.0R ────
                    if stage2_done and not stage3_done and risk_dist > 0:
                        trigger_3  = entry + risk_dist * 2.0 if is_long else entry - risk_dist * 2.0
                        sl_lock_3  = entry + risk_dist * 1.0 if is_long else entry - risk_dist * 1.0
                        stage3_hit = (is_long and current >= trigger_3) or (not is_long and current <= trigger_3)
                        if stage3_hit:
                            logger.info(f"🚀 Stage3 {sym}: harga {current} melewati +2.0R — SL ke +1.0R ({sl_lock_3:.6g}), profit terkunci penuh")
                            r3 = self.move_sl_trailing(symbol, sl_lock_3)
                            if r3.get('ok'):
                                stage3_done = True
                                logger.info(f"✅ Stage3 SL terpasang {sym} @ {sl_lock_3:.6g} — profit +1R aman")
                                if notify_fn and callable(notify_fn):
                                    try:
                                        import asyncio
                                        loop = asyncio.new_event_loop()
                                        loop.run_until_complete(notify_fn(
                                            "🚀 TRAILING SL STAGE 3 — " + sym + "\n" +
                                            "   Harga : " + str(round(current, 8)) + "\n" +
                                            "   SL baru: " + str(round(sl_lock_3, 8)) + " (+1R terkunci)\n" +
                                            "   Profit +1R sudah aman apapun yang terjadi!"
                                        ))
                                        loop.close()
                                    except Exception:
                                        pass
                            else:
                                logger.warning(f"⚠️ Stage3 SL gagal {sym}: {r3.get('msg')}")

                    # ── Stage 4 (Opsi D 2026-04-24): Runner trail ──────
                    # Setelah +3R dari entry, trail SL ketat = extreme ± 0.5R.
                    # Tidak ada cap profit — SL naik setiap harga bergerak searah.
                    # Exit hanya saat harga retrace 0.5R dari ekstrem.
                    if stage3_done and risk_dist > 0:
                        if is_long:
                            extreme_px = max(extreme_px, current)
                        else:
                            extreme_px = min(extreme_px, current)

                        trigger_4 = entry + risk_dist * 3.0 if is_long else entry - risk_dist * 3.0
                        stage4_active = (is_long and extreme_px >= trigger_4) or (not is_long and extreme_px <= trigger_4)

                        if stage4_active:
                            # Trail SL = ekstrem ± 0.5R
                            if is_long:
                                new_trail = extreme_px - risk_dist * 0.5
                                # Hanya update kalau lebih tinggi dari SL terakhir
                                should_move = stage4_sl is None or new_trail > stage4_sl + risk_dist * 0.2
                            else:
                                new_trail = extreme_px + risk_dist * 0.5
                                should_move = stage4_sl is None or new_trail < stage4_sl - risk_dist * 0.2

                            if should_move:
                                logger.info(f"🏃 Stage4 {sym}: ekstrem {extreme_px} → trail SL ke {new_trail:.6g}")
                                r4 = self.move_sl_trailing(symbol, new_trail)
                                if r4.get('ok'):
                                    stage4_sl = new_trail
                                    logger.info(f"✅ Stage4 trail {sym} @ {new_trail:.6g}")
                                    if notify_fn and callable(notify_fn):
                                        try:
                                            import asyncio
                                            loop = asyncio.new_event_loop()
                                            r_locked = abs(new_trail - entry) / risk_dist
                                            loop.run_until_complete(notify_fn(
                                                "🏃 RUNNER TRAIL — " + sym + "\n" +
                                                "   Ekstrem: " + str(round(extreme_px, 8)) + "\n" +
                                                "   SL baru: " + str(round(new_trail, 8)) + f" (+{r_locked:.1f}R terkunci)"
                                            ))
                                            loop.close()
                                        except Exception:
                                            pass

                except Exception as e:
                    logger.debug(f"Monitor {sym} error: {e}")
                    continue

            # ── Deteksi hasil akhir posisi untuk circuit breaker ──
            try:
                history = self._get("/api/v1/futures/position/get_history_positions", {
                    "symbol": sym, "limit": "5"
                })
                positions_h = []
                raw_h = history.get('data', [])
                if isinstance(raw_h, dict):
                    positions_h = raw_h.get('positionList', raw_h.get('list', []))
                elif isinstance(raw_h, list):
                    positions_h = raw_h

                if positions_h:
                    last_pos  = positions_h[0]
                    # Bitunix pakai field 'realizedPNL' (uppercase PNL) — bukan camelCase
                    last_pnl  = float(last_pos.get('realizedPNL',
                                last_pos.get('realizedPnl',
                                last_pos.get('pnl', 0))))
                    # BEP close = last_pnl ≈ 0 tapi tp1 sudah kena → bukan pure loss
                    is_bep_close = bep_done and abs(last_pnl) < 0.01
                    trade_won = last_pnl > 0 or is_bep_close
                    self.record_trade_result(
                        trade_won,
                        symbol=symbol,
                        direction=direction,
                        pnl_usd=last_pnl,
                        notify_fn=notify_fn,
                        level_price=level_price,
                    )
                    logger.info(f"📊 Trade {sym} selesai — {'PROFIT' if last_pnl > 0 else ('BEP' if is_bep_close else 'LOSS')} ${last_pnl:.2f}")

                    # Push closed trade ke Neon DB
                    try:
                        saved_data = self._saved_positions.get(symbol, {})
                        outcome = "PROFIT" if last_pnl > 0 else ("BEP" if is_bep_close else "LOSS")
                        risk_dist = abs(entry - saved_data.get('sl', entry)) or 1
                        pnl_r = last_pnl / (risk_dist if risk_dist > 0 else 1)
                        self._push_trade_to_web({
                            'symbol'     : symbol,
                            'direction'  : direction,
                            'strategy'   : 'swing',
                            'quality'    : saved_data.get('quality', 'GOOD'),
                            'entry'      : entry,
                            'exit_price' : current,
                            'sl'         : saved_data.get('sl'),
                            'tp1'        : saved_data.get('tp1'),
                            'tp2'        : saved_data.get('tp2'),
                            'pnl_usd'    : float(last_pnl),
                            'pnl_r'      : float(pnl_r),
                            'outcome'    : outcome,
                            'bep_done'   : bool(bep_done),
                            'opened_at'  : saved_data.get('opened_at'),
                        })
                        # Hapus posisi dari web (sudah closed) + patch signal status
                        self._delete_position_from_web(symbol)
                        self._patch_signal_status(symbol, 'closed')
                    except Exception as _pe:
                        logger.debug(f"Push trade to web error: {_pe}")

                    # Notif trade selesai
                    if notify_fn and callable(notify_fn):
                        try:
                            import asyncio
                            if last_pnl > 0:
                                stage = "TP3+" if stage3_done else "TP2"
                                msg = (
                                    "✅ TRADE SELESAI — " + sym + "\n"
                                    "========================\n"
                                    f"Hasil  : PROFIT {stage}\n"
                                    f"PnL    : +${last_pnl:.2f} USDT"
                                )
                            elif is_bep_close:
                                msg = (
                                    "⚪ TRADE SELESAI — " + sym + "\n"
                                    "========================\n"
                                    "Hasil  : BEP (TP1 kena, sisa close di entry)\n"
                                    f"PnL    : ${last_pnl:.2f} USDT"
                                )
                            else:
                                msg = (
                                    "❌ TRADE SELESAI — " + sym + "\n"
                                    "========================\n"
                                    f"Hasil  : {'SL setelah BEP' if bep_done else 'SL'}\n"
                                    f"PnL    : -${abs(last_pnl):.2f} USDT"
                                )
                            loop = asyncio.new_event_loop()
                            loop.run_until_complete(notify_fn(msg))
                            loop.close()

                            # Kirim explainer post (siap copy ke channel) jika trade WIN
                            if last_pnl > 0:
                                try:
                                    explainer = self._build_trade_explainer(
                                        symbol=symbol, direction=direction,
                                        entry=entry, last_pnl=last_pnl,
                                        stage="TP3+" if stage3_done else "TP2"
                                    )
                                    if explainer:
                                        loop2 = asyncio.new_event_loop()
                                        loop2.run_until_complete(notify_fn(explainer))
                                        loop2.close()
                                except Exception as _xe:
                                    logger.debug(f"Explainer error: {_xe}")
                        except Exception as _ne:
                            logger.warning(f"Notif trade selesai {sym} gagal: {_ne}")
            except Exception as _e:
                logger.debug(f"record_trade_result error: {_e}")

            logger.info(f"👁️ Monitor {sym} selesai (elapsed {elapsed//3600:.0f}h)")
            self._active_monitors.discard(symbol)

        # Cegah double monitor untuk coin yang sama
        if symbol in self._active_monitors:
            logger.info(f"👁️ Monitor {symbol} sudah aktif — skip")
            return

        self._active_monitors.add(symbol)
        thread = threading.Thread(target=_monitor, daemon=True, name=f"monitor_{symbol}")
        thread.start()
        logger.info(f"👁️ TP1 monitor started untuk {symbol}")

    # ── HELPERS ───────────────────────────────────────────────

    def _get_current_price(self, symbol: str) -> float:
        data = self._get("/api/v1/futures/market/tickers", {"symbols": symbol})
        if data.get('code') == 0:
            raw = data.get('data', [])
            # API bisa return list langsung atau dict dengan tickerList
            if isinstance(raw, list):
                tickers = raw
            elif isinstance(raw, dict):
                tickers = raw.get('tickerList', [])
            else:
                tickers = []
            if tickers:
                return float(tickers[0].get('lastPrice', 0))
        return 0.0

    def _get_monthly_period(self):
        """Return string YYYY-MM sebagai identifier periode bulan."""
        from datetime import datetime as dt
        return dt.now().strftime('%Y-%m')

    def _load_pnl_reset_ts(self) -> int:
        """Load reset timestamp (ms) dari file, default 0 (no reset)."""
        import json, os
        try:
            if os.path.exists(self._pnl_reset_file):
                with open(self._pnl_reset_file, 'r') as f:
                    return int(json.load(f).get('reset_ts', 0))
        except Exception:
            pass
        return 0

    def reset_pnl_tracking(self) -> dict:
        """
        Reset display PnL bulanan/tahunan — mulai hitung dari sekarang.
        Trade closed SEBELUM timestamp ini tidak akan di-count.
        """
        import json, os
        from datetime import datetime as dt
        now_ms = int(dt.now().timestamp() * 1000)
        self._pnl_reset_ts = now_ms
        try:
            os.makedirs(os.path.dirname(self._pnl_reset_file), exist_ok=True)
            with open(self._pnl_reset_file, 'w') as f:
                json.dump({
                    'reset_ts'  : now_ms,
                    'reset_date': dt.now().strftime('%Y-%m-%d %H:%M:%S'),
                }, f)
        except Exception as e:
            logger.warning(f"Save pnl_reset gagal: {e}")

        # Reset in-memory counters
        self._monthly_pnl_usd = self._monthly_profit_usd = self._monthly_loss_usd = 0.0
        self._monthly_trades = self._monthly_wins = 0
        self._yearly_pnl_usd = self._yearly_profit_usd = self._yearly_loss_usd = 0.0
        self._yearly_trades = self._yearly_wins = 0

        return {
            'reset_ts'  : now_ms,
            'reset_date': dt.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

    def sync_monthly_pnl_from_exchange(self):
        """
        Sync PnL bulanan — match persis dengan tampilan Bitunix.
        Ambil dari tanggal 1 bulan ini jam 00:00, tanpa filter tambahan.
        """
        try:
            from datetime import datetime as dt
            now = dt.now()
            # Awal bulan tanggal 1 jam 00:00 — sama dengan Bitunix
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            start_ts    = int(month_start.timestamp() * 1000)

            data = self._get("/api/v1/futures/position/get_history_positions", {
                "startTime": str(start_ts),
                "limit"    : "200",  # limit lebih besar agar semua trade masuk
            })

            if data.get('code') != 0:
                return

            raw = data.get('data', [])
            if isinstance(raw, dict):
                positions = raw.get('positionList', raw.get('list', []))
            elif isinstance(raw, list):
                positions = raw
            else:
                return

            total_profit = 0.0
            total_loss   = 0.0
            trades       = 0
            wins         = 0

            # Filter by mtime (waktu TUTUP) — skip trade sebelum awal bulan
            # ATAU sebelum reset_ts (kalau user baru /reset_pnl)
            cutoff_ts = max(start_ts, self._pnl_reset_ts)
            for pos in positions:
                mtime = int(pos.get('mtime', 0))
                if mtime > 0 and mtime < cutoff_ts:
                    continue  # ditutup sebelum cutoff — skip
                pnl = float(pos.get('realizedPNL', 0))
                trades += 1
                if pnl > 0:
                    total_profit += pnl
                    wins += 1
                elif pnl < 0:
                    total_loss += abs(pnl)

            self._monthly_profit_usd = total_profit
            self._monthly_loss_usd   = total_loss
            self._monthly_pnl_usd    = total_profit - total_loss
            self._monthly_trades     = trades
            self._monthly_wins       = wins
            self._monthly_period     = self._get_monthly_period()

            logger.info(f"📊 Monthly: {trades} trades | +${total_profit:.2f} -${total_loss:.2f} net=${self._monthly_pnl_usd:.2f}")

        except Exception as e:
            logger.debug(f"sync_monthly error: {e}")

    def sync_yearly_pnl_from_exchange(self):
        """Sync PnL tahunan — dari 1 Januari tahun ini jam 00:00."""
        try:
            from datetime import datetime as dt
            now        = dt.now()
            year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            start_ts   = int(year_start.timestamp() * 1000)

            data = self._get("/api/v1/futures/position/get_history_positions", {
                "startTime": str(start_ts),
                "limit"    : "500",
            })
            if data.get('code') != 0:
                return

            raw = data.get('data', [])
            if isinstance(raw, dict):
                positions = raw.get('positionList', raw.get('list', []))
            elif isinstance(raw, list):
                positions = raw
            else:
                return

            total_profit = total_loss = trades = wins = 0.0
            # Skip trade sebelum reset_ts (kalau user /reset_pnl)
            cutoff_ts = max(start_ts, self._pnl_reset_ts)
            for pos in positions:
                mtime = int(pos.get('mtime', 0))
                if mtime > 0 and mtime < cutoff_ts:
                    continue
                pnl = float(pos.get('realizedPNL', 0))
                trades += 1
                if pnl > 0:
                    total_profit += pnl; wins += 1
                elif pnl < 0:
                    total_loss += abs(pnl)

            self._yearly_profit_usd = total_profit
            self._yearly_loss_usd   = total_loss
            self._yearly_pnl_usd    = total_profit - total_loss
            self._yearly_trades     = int(trades)
            self._yearly_wins       = int(wins)
            logger.info(f"📊 Yearly: {int(trades)} trades | +${total_profit:.2f} -${total_loss:.2f} net=${self._yearly_pnl_usd:.2f}")

        except Exception as e:
            logger.debug(f"sync_yearly error: {e}")

    def _get_trade_date(self):
        """Trade date berdasarkan jam 8 pagi — sebelum jam 8 masih dianggap hari kemarin."""
        from datetime import datetime as dt, timedelta
        now = dt.now()
        if now.hour < 8:
            return (now - timedelta(days=1)).date()
        return now.date()

    # ── Circuit Breaker ───────────────────────────────────────

    def is_circuit_breaker_active(self) -> bool:
        """Return True kalau trading sedang di-pause karena consecutive SL."""
        if self._circuit_breaker_until is None:
            return False
        if datetime.now() >= self._circuit_breaker_until:
            self._circuit_breaker_until  = None
            self._consecutive_losses     = 0
            logger.info("✅ Circuit breaker selesai — trading resume")
            return False
        return True

    def circuit_breaker_status(self) -> str:
        """Return string status circuit breaker untuk Telegram."""
        if not self.is_circuit_breaker_active():
            return f"✅ Normal ({self._consecutive_losses}/{self.circuit_breaker_max} SL)"
        remaining = (self._circuit_breaker_until - datetime.now()).seconds // 60
        return f"⛔ PAUSE {remaining} menit lagi ({self._consecutive_losses} SL berturut-turut)"

    def record_trade_result(self, won: bool,
                            symbol: str = '', direction: str = '',
                            pnl_usd: float = 0.0,
                            notify_fn=None,
                            level_price: float = 0.0):
        """
        Catat hasil trade untuk circuit breaker + learning engine + level memory.
        Dipanggil setelah posisi close (win/loss diketahui).
        """
        outcome = 'TP1' if won else 'SL'

        if won:
            if self._consecutive_losses > 0:
                logger.info(f"✅ Trade profit — reset consecutive loss counter ({self._consecutive_losses} → 0)")
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            logger.info(f"❌ SL #{self._consecutive_losses} — circuit breaker threshold={self.circuit_breaker_max}")
            if self._consecutive_losses >= self.circuit_breaker_max:
                pause_until = datetime.now().replace(microsecond=0)
                from datetime import timedelta
                pause_until += timedelta(hours=self.circuit_breaker_pause)
                self._circuit_breaker_until = pause_until
                logger.warning(
                    f"⛔ CIRCUIT BREAKER AKTIF — {self._consecutive_losses} SL berturut-turut. "
                    f"Trading di-pause sampai {pause_until.strftime('%H:%M')}"
                )

        # ── Log outcome ke learning engine + kirim post-mortem ──
        if symbol and direction:
            try:
                from learning_engine import get_learning_engine
                le = get_learning_engine()
                le.log_outcome(symbol, direction, outcome, pnl_usd)

                # Post-mortem hanya untuk SL
                if not won:
                    msg = le.generate_postmortem(symbol, direction, 'SL', pnl_usd)
                    if msg and notify_fn:
                        try:
                            import asyncio
                            asyncio.run_coroutine_threadsafe(
                                notify_fn(msg), asyncio.get_event_loop())
                        except Exception:
                            pass
                    if msg:
                        logger.info(f"Post-mortem {symbol} {direction}:\n{msg}")

                # Setelah cukup data → auto-tune parameter
                stats = le.get_quick_stats()
                if stats['total'] >= 10 and stats['total'] % 5 == 0:
                    le.auto_tune()

            except Exception as _e:
                logger.debug(f"learning record_trade_result: {_e}")

        # ── Catat level memory (apakah level S/R ini bertahan atau tembus) ──
        if symbol and direction and level_price > 0:
            try:
                from level_memory import get_level_memory
                mem = get_level_memory()
                mem.auto_record_from_signal(symbol, level_price, 0.0, direction, pnl_usd)
                logger.debug(f"level_memory recorded {symbol} @ {level_price} — {'held' if won else 'broke'}")
            except Exception as _e:
                logger.debug(f"level_memory record error: {_e}")

    def _is_daily_loss_exceeded(self) -> bool:
        trade_date = self._get_trade_date()
        if self._daily_loss_date != trade_date:
            self._daily_loss_pct  = 0.0
            self._daily_loss_usd  = 0.0
            self._daily_loss_date = trade_date
            logger.info(f"📅 Daily loss reset untuk periode {trade_date}")
        return self._daily_loss_usd >= self.max_daily_loss_usd

    def sync_daily_loss_from_exchange(self):
        """
        Sync daily loss dari history posisi di exchange.
        Hanya hitung posisi yang ditutup HARI INI (sejak tengah malam).
        """
        try:
            from datetime import datetime as dt
            # Reset dulu kalau hari berbeda
            trade_date = self._get_trade_date()
            if self._daily_loss_date != trade_date:
                self._daily_loss_usd  = 0.0
                self._daily_loss_pct  = 0.0
                self._daily_loss_date = trade_date
                logger.info(f"📅 Daily loss reset — periode {trade_date}")

            # Reset harian jam 08:00 pagi (bukan tengah malam)
            now = dt.now()
            if now.hour < 8:
                # Sebelum jam 8 — pakai kemarin jam 8
                reset_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
                from datetime import timedelta
                reset_time = reset_time - timedelta(days=1)
            else:
                reset_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
            today_start = int(reset_time.timestamp() * 1000)

            data = self._get("/api/v1/futures/position/get_history_positions", {
                "startTime": str(today_start),
                "limit"    : "50",
            })

            if data.get('code') != 0:
                return

            raw = data.get('data', [])
            if isinstance(raw, dict):
                positions = raw.get('positionList', raw.get('list', []))
            elif isinstance(raw, list):
                positions = raw
            else:
                return

            total_loss_usd   = 0.0
            total_profit_usd = 0.0
            count = 0
            for pos in positions:
                # Filter manual pakai mtime (waktu TUTUP) — API filter pakai ctime (buka)
                mtime = int(pos.get('mtime', 0))
                if mtime > 0 and mtime < today_start:
                    continue  # ditutup sebelum periode hari ini — skip

                pnl = float(pos.get('realizedPNL', 0))
                count += 1
                if pnl < 0:
                    total_loss_usd += abs(pnl)
                elif pnl > 0:
                    total_profit_usd += pnl

            net_loss = max(0.0, total_loss_usd - total_profit_usd)

            trade_date = self._get_trade_date()
            if self._daily_loss_date != trade_date:
                self._daily_loss_usd  = 0.0
                self._daily_loss_pct  = 0.0
                self._daily_loss_date = trade_date

            self._daily_loss_usd = net_loss
            logger.info(f"📊 Daily sync ({count} trades sejak jam 8): loss=${total_loss_usd:.2f} profit=${total_profit_usd:.2f} net=${net_loss:.2f}")

        except Exception as e:
            logger.debug(f"sync_daily_loss error: {e}")

    def _push_signal_to_web(self, sig: dict) -> None:
        """Push signal ke Neon DB via website API. Best-effort, fail-silent."""
        try:
            import requests as _req
            import hmac as _hmac, hashlib as _hl
            web_url   = os.getenv('WEB_URL', 'https://cryptovision-web.vercel.app')
            bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
            symbol    = sig.get('symbol', '')
            secret    = _hmac.new(bot_token.encode(), symbol.encode(), _hl.sha256).hexdigest()
            sig['secret'] = secret
            _req.post(f"{web_url}/api/signals", json=sig, timeout=8)
        except Exception:
            pass

    def _push_trade_to_web(self, trade: dict) -> None:
        """Push closed trade ke Neon DB. Best-effort, fail-silent."""
        try:
            import requests as _req
            import hmac as _hmac, hashlib as _hl
            web_url   = os.getenv('WEB_URL', 'https://cryptovision-web.vercel.app')
            bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
            symbol    = trade.get('symbol', '')
            secret    = _hmac.new(bot_token.encode(), symbol.encode(), _hl.sha256).hexdigest()
            trade['secret'] = secret
            _req.post(f"{web_url}/api/trades", json=trade, timeout=8)
        except Exception:
            pass

    def _hmac_secret(self, symbol: str) -> str:
        """Hitung HMAC secret untuk auth ke website API."""
        import hmac as _hmac, hashlib as _hl
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        return _hmac.new(bot_token.encode(), symbol.encode(), _hl.sha256).hexdigest()

    def _web_url(self) -> str:
        return os.getenv('WEB_URL', 'https://cryptovision-web.vercel.app')

    def _push_position_to_web(self, position: dict) -> None:
        """Push posisi running ke Neon DB (saat limit fill / market open). Best-effort."""
        try:
            import requests as _req
            symbol = position.get('symbol', '')
            position['secret'] = self._hmac_secret(symbol)
            _req.post(f"{self._web_url()}/api/positions", json=position, timeout=8)
        except Exception:
            pass

    def _delete_position_from_web(self, symbol: str) -> None:
        """Hapus posisi dari Neon DB (saat trade close). Best-effort."""
        try:
            import requests as _req
            secret = self._hmac_secret(symbol)
            _req.delete(
                f"{self._web_url()}/api/positions",
                params={"symbol": symbol, "secret": secret},
                timeout=8,
            )
        except Exception:
            pass

    def _patch_position_state(self, symbol: str, **opts) -> None:
        """Update state posisi (tp1_hit, bep_active, sl). Best-effort."""
        try:
            import requests as _req
            body = {"symbol": symbol, "secret": self._hmac_secret(symbol), **opts}
            _req.patch(f"{self._web_url()}/api/positions", json=body, timeout=8)
        except Exception:
            pass

    def _patch_signal_status(self, symbol: str, status: str) -> None:
        """Update status signal (pending → filled / closed / cancelled). Best-effort."""
        try:
            import requests as _req
            body = {"symbol": symbol, "status": status, "secret": self._hmac_secret(symbol)}
            _req.patch(f"{self._web_url()}/api/signals", json=body, timeout=8)
        except Exception:
            pass

    def _fetch_active_subscriber_ids(self) -> list:
        """Ambil list telegram_id user yang punya subscription aktif. Best-effort."""
        try:
            import requests as _req
            import hmac as _hmac, hashlib as _hl
            bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
            secret = _hmac.new(bot_token.encode(), b"subscribers", _hl.sha256).hexdigest()
            r = _req.get(
                f"{self._web_url()}/api/subscribers",
                params={"secret": secret},
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("ok"):
                    return data.get("subscriber_ids", [])
        except Exception:
            pass
        return []

    def _broadcast_to_subscribers(self, text: str) -> None:
        """Kirim Telegram message ke semua paid subscriber. Best-effort, fail-silent."""
        try:
            import requests as _req
            ids = self._fetch_active_subscriber_ids()
            if not ids:
                return
            bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            for cid in ids:
                try:
                    _req.post(url, json={"chat_id": int(cid), "text": text}, timeout=8)
                except Exception:
                    pass
        except Exception:
            pass

    def _build_trade_explainer(self, symbol: str, direction: str,
                                entry: float, last_pnl: float, stage: str) -> str:
        """
        Generate post explainer ready-to-share untuk trade WIN.
        Pakai signal data yang disimpan saat trade dibuka.
        """
        clean_sym = symbol.upper().replace('USDT', '')
        saved = self._saved_positions.get(clean_sym, {})
        reasons = saved.get('reasons', [])
        score   = saved.get('score', 0)
        quality = saved.get('quality', 'GOOD')
        opened  = saved.get('opened_at', '')
        tp1     = saved.get('tp1', 0)
        tp2     = saved.get('tp2', 0)
        sl      = saved.get('sl', 0)

        try:
            rr = abs(tp2 - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        except Exception:
            rr = 0

        ico = "🟢" if direction == "LONG" else "🔴"
        lines = [
            f"📈 ANALISA TRADE {clean_sym} {direction} — HIT {stage}",
            "=" * 32,
            f"{ico} Entry : {entry:.6g}",
            f"   SL    : {sl:.6g}",
            f"   TP    : {tp2:.6g}",
            f"   RR    : 1:{rr:.1f}",
            f"   Profit: +${last_pnl:.2f} USDT",
            "",
            f"🎯 Quality: {quality} (score {score}/30+)",
            "",
            "💡 KENAPA SINYAL INI MUNCUL?",
        ]
        if reasons:
            for i, r in enumerate(reasons[:6], 1):
                # Bersihkan emoji + truncate
                r_clean = str(r).strip()
                if len(r_clean) > 90:
                    r_clean = r_clean[:87] + "..."
                lines.append(f"  {i}. {r_clean}")
        else:
            lines.append("  (data konfluensi tidak tersimpan)")

        lines.extend([
            "",
            f"⏱️ Open: {opened}",
            "",
            "Bot trading 24/7 — analisis multi-faktor swing & scalp",
            "Daftar bot trading otomatis: @CryptoVisionID",
            "",
            "#trading #crypto #bitunix #signal #cuan",
        ])
        return "\n".join(lines)

    def get_daily_summary(self) -> dict:
        """
        Return rekap trading hari ini untuk daily digest.
        Period: sejak jam 8 pagi (sesuai _get_trade_date logic).
        """
        from datetime import datetime as dt, timedelta
        try:
            now = dt.now()
            if now.hour < 8:
                reset_time = (now - timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
            else:
                reset_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
            today_start = int(reset_time.timestamp() * 1000)

            data = self._get("/api/v1/futures/position/get_history_positions", {
                "startTime": str(today_start),
                "limit"    : "100",
            })
            if data.get('code') != 0:
                return {'ok': False}

            raw = data.get('data', [])
            if isinstance(raw, dict):
                positions = raw.get('positionList', raw.get('list', []))
            elif isinstance(raw, list):
                positions = raw
            else:
                positions = []

            wins, losses, total_profit, total_loss = 0, 0, 0.0, 0.0
            best  = {'sym': '', 'pnl': 0.0, 'side': ''}
            worst = {'sym': '', 'pnl': 0.0, 'side': ''}
            count = 0
            for pos in positions:
                mtime = int(pos.get('mtime', 0))
                if mtime > 0 and mtime < today_start:
                    continue
                pnl  = float(pos.get('realizedPNL', 0))
                psym = pos.get('symbol', '').replace('USDT', '')
                pside_raw = pos.get('side', '')
                pside = 'LONG' if pside_raw in ('BUY', 'LONG') else 'SHORT'
                count += 1
                if pnl > 0:
                    wins += 1
                    total_profit += pnl
                    if pnl > best['pnl']:
                        best = {'sym': psym, 'pnl': pnl, 'side': pside}
                elif pnl < 0:
                    losses += 1
                    total_loss += abs(pnl)
                    if pnl < worst['pnl']:
                        worst = {'sym': psym, 'pnl': pnl, 'side': pside}

            net    = total_profit - total_loss
            wr     = (wins / count * 100) if count > 0 else 0.0
            open_n = len(self.get_positions())
            balance = self.get_balance() or 0.0

            return {
                'ok'        : True,
                'count'     : count,
                'wins'      : wins,
                'losses'    : losses,
                'win_rate'  : round(wr, 1),
                'profit'    : round(total_profit, 2),
                'loss'      : round(total_loss, 2),
                'net_pnl'   : round(net, 2),
                'best'      : best,
                'worst'     : worst,
                'open_pos'  : open_n,
                'balance'   : round(balance, 2),
            }
        except Exception as e:
            logger.warning(f"get_daily_summary error: {e}")
            return {'ok': False}

    def _update_daily_loss(self, loss_pct: float):
        """Update daily loss tracker. loss_pct dalam persen dari balance."""
        if self._daily_loss_date != date.today():
            self._daily_loss_pct  = 0.0
            self._daily_loss_usd  = 0.0
            self._daily_loss_date = date.today()
        self._daily_loss_pct += loss_pct

    def _update_daily_loss_usd(self, loss_usd: float):
        """Update daily loss tracker dengan nominal USD."""
        if self._daily_loss_date != date.today():
            self._daily_loss_pct  = 0.0
            self._daily_loss_usd  = 0.0
            self._daily_loss_date = date.today()
        if loss_usd > 0:
            self._daily_loss_usd += loss_usd
            logger.info(f"📉 Daily loss: ${self._daily_loss_usd:.2f} / ${self.max_daily_loss_usd:.2f}")

    def get_status(self) -> str:
        """Ringkasan status untuk Telegram."""
        if not self.is_ready:
            return (
                "🔴 AUTO TRADE: NONAKTIF\n"
                "Set TRADE_ENABLED=true di .env untuk aktifkan"
            )

        balance   = self.get_balance()
        positions = self.get_positions()
        self.sync_daily_loss_from_exchange()  # sync dari exchange dulu
        daily_ok   = not self._is_daily_loss_exceeded()
        daily_icon = "✅" if daily_ok else "🚫 LIMIT TERCAPAI"
        bal_str    = f"${balance:,.2f}" if balance is not None else "Error (cek API key)"
        # Risk display — reflect actual risk_usd setting dari .env
        if self.risk_usd > 0:
            risk_display = f"${self.risk_usd:.2f} flat per trade"
        else:
            risk_display = f"{self.risk_pct:.1f}% balance per trade"

        max_pos_dyn = self.max_positions

        # Sync monthly data
        self.sync_monthly_pnl_from_exchange()
        monthly_ico  = "✅" if self._monthly_pnl_usd >= 0 else "❌"
        monthly_wr   = (self._monthly_wins / self._monthly_trades * 100) if self._monthly_trades > 0 else 0

        # Sync yearly juga
        self.sync_yearly_pnl_from_exchange()
        yearly_ico = "✅" if self._yearly_pnl_usd >= 0 else "❌"
        yearly_wr  = (self._yearly_wins / self._yearly_trades * 100) if self._yearly_trades > 0 else 0
        yearly_losses = self._yearly_trades - self._yearly_wins

        cb_status = self.circuit_breaker_status()
        text = (
            f"🟢 AUTO TRADE: AKTIF\n"
            f"================================\n"
            f"Balance      : {bal_str} USDT\n"
            f"Risk/trade   : {risk_display}\n"
            f"Leverage     : {self.leverage}x\n"
            f"Max posisi   : {max_pos_dyn}\n"
            f"Net loss hari: ${self._daily_loss_usd:.2f} / ${self.max_daily_loss_usd:.2f} {daily_icon}\n"
            f"Posisi aktif : {len(positions)}/{max_pos_dyn}\n"
            f"Monitor aktif: {len(self._active_monitors)} coin\n"
            f"Circuit break: {cb_status}\n"
            f"\n📅 BULANAN\n"
            f"--------------------------------\n"
            f"Net PnL : {monthly_ico} ${self._monthly_pnl_usd:+.2f} USDT\n"
            f"Profit  : +${self._monthly_profit_usd:.2f} | Loss: -${self._monthly_loss_usd:.2f}\n"
            f"Trades  : {self._monthly_trades} ({self._monthly_wins}W/{self._monthly_trades - self._monthly_wins}L) WR {monthly_wr:.0f}%\n"
            f"\n📆 TAHUNAN\n"
            f"--------------------------------\n"
            f"Net PnL : {yearly_ico} ${self._yearly_pnl_usd:+.2f} USDT\n"
            f"Profit  : +${self._yearly_profit_usd:.2f} | Loss: -${self._yearly_loss_usd:.2f}\n"
            f"Trades  : {self._yearly_trades} ({self._yearly_wins}W/{yearly_losses}L) WR {yearly_wr:.0f}%\n"
        )

        if positions:
            text += "\n📊 Posisi Terbuka:\n"
            for pos in positions:
                sym       = pos.get('symbol', '')
                side      = pos.get('side', '')       # BUY atau SELL
                qty_str   = pos.get('qty', '0')
                qty       = float(qty_str)
                entry     = float(pos.get('avgOpenPrice', 0))
                pnl       = float(pos.get('unrealizedPNL', 0))
                lev       = int(pos.get('leverage', self.leverage))
                # markPrice tidak ada di API — hitung dari unrealizedPNL
                # mark = entry + pnl/qty untuk LONG, entry - pnl/qty untuk SHORT
                try:
                    if qty > 0 and entry > 0:
                        if side == 'BUY':
                            mark = entry + (pnl / qty)
                        else:
                            mark = entry - (pnl / qty)
                    else:
                        mark = entry
                except Exception:
                    mark = entry
                # Hitung % PnL dari margin
                margin = float(pos.get('margin', 0))
                pct    = (pnl / margin * 100) if margin > 0 else 0
                # Direction label
                direction = 'LONG' if side in ('BUY', 'LONG') else 'SHORT'
                ico   = "🟢" if side == "BUY" else "🔴"
                mon   = "👁️" if sym.replace('USDT','') in self._active_monitors else ""
                text += (
                    f"  {ico} {sym} {direction} x{lev} {mon}\n"
                    f"     Entry : {entry}\n"
                    f"     Mark  : {mark:.6f}\n"
                    f"     Qty   : {qty} | PnL: {pnl:+.4f} USDT ({pct:+.1f}%)\n"
                )
        elif not positions:
            text += "\nTidak ada posisi terbuka."

        return text