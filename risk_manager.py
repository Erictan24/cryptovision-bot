from datetime import datetime
from config import RISK_CONFIG

class RiskManager:
    def __init__(self, database_manager):
        self.db  = database_manager
        self.cfg = RISK_CONFIG

    # ------------------------------------------------------------------
    # FORMAT HARGA — otomatis sesuai harga coin
    # ------------------------------------------------------------------
    def fmt_price(self, price):
        """Format harga dengan aman tanpa karakter spesial"""
        if price >= 1000:  return f"${price:,.2f}"
        if price >= 1:     return f"${price:.4f}"
        if price >= 0.01:  return f"${price:.6f}"
        return f"${price:.8f}"

    # ------------------------------------------------------------------
    # HITUNG SL DAN TP BERDASARKAN ATR
    # ------------------------------------------------------------------
    def calc_sl_tp(self, signal):
        entry   = signal['entry_price']
        atr     = signal.get('atr', entry * 0.02)   # default 2% kalau ATR tidak ada
        atr     = max(atr, entry * 0.005)            # minimal ATR 0.5% dari harga

        sl_mult = self.cfg['stop_loss_atr_multiplier']
        tp_mult = self.cfg['take_profit_atr_multiplier']

        if signal['direction'] == 'LONG':
            sl = entry - (atr * sl_mult)
            tp = entry + (atr * tp_mult)
        else:  # SHORT
            sl = entry + (atr * sl_mult)
            tp = entry - (atr * tp_mult)

        sl = max(sl, 0)   # SL tidak boleh negatif
        risk   = abs(entry - sl)
        reward = abs(tp - entry)
        rr     = round(reward / risk, 2) if risk > 0 else 0

        return {
            'stop_loss'  : round(sl, 8),
            'take_profit': round(tp, 8),
            'risk_reward': rr,
            'risk_pct'   : round((risk / entry) * 100, 2),   # SL distance dalam %
            'reward_pct' : round((reward / entry) * 100, 2)  # TP distance dalam %
        }

    # ------------------------------------------------------------------
    # HITUNG UKURAN POSISI
    # ------------------------------------------------------------------
    def calc_position_size(self, entry, stop_loss, balance):
        risk_amount = balance * self.cfg['risk_per_trade']
        price_risk  = abs(entry - stop_loss)
        size        = risk_amount / price_risk if price_risk > 0 else 0
        return round(size, 4), round(risk_amount, 2)

    # ------------------------------------------------------------------
    # CEK BATAS RISIKO HARIAN
    # ------------------------------------------------------------------
    def check_risk_limits(self):
        open_trades = self.db.get_open_trades()
        if len(open_trades) >= self.cfg['max_open_trades']:
            return False, f"Maks {self.cfg['max_open_trades']} trade terbuka sudah tercapai"
        return True, "OK"

    def risk_level(self, rr):
        if rr >= 3.0: return "LOW"
        if rr >= 2.0: return "MEDIUM"
        return "HIGH"

    # ------------------------------------------------------------------
    # SIAPKAN TRADE LENGKAP
    # ------------------------------------------------------------------
    def prepare_trade(self, signal, balance=10000):
        ok, msg = self.check_risk_limits()
        if not ok:
            return None, msg

        sl_tp = self.calc_sl_tp(signal)

        if sl_tp['risk_reward'] < self.cfg['risk_reward_ratio']:
            return None, f"R:R terlalu rendah ({sl_tp['risk_reward']} < {self.cfg['risk_reward_ratio']})"

        size, risk_amt = self.calc_position_size(
            signal['entry_price'], sl_tp['stop_loss'], balance
        )

        trade = {
            'timestamp'    : signal['timestamp'],
            'pair'         : signal['pair'],
            'direction'    : signal['direction'],
            'entry_price'  : signal['entry_price'],
            'stop_loss'    : sl_tp['stop_loss'],
            'take_profit'  : sl_tp['take_profit'],
            'position_size': size,
            'risk_amount'  : risk_amt,
            'risk_reward'  : sl_tp['risk_reward'],
            'risk_level'   : self.risk_level(sl_tp['risk_reward']),
            'reason'       : signal['reason']
        }
        return trade, "Trade siap"

    # ------------------------------------------------------------------
    # FORMAT PESAN SINYAL LENGKAP UNTUK TELEGRAM
    # ------------------------------------------------------------------
    def format_signal_message(self, symbol, direction, entry, sl_tp,
                               score, label, rsi, reasons, balance=10000,
                               support=None, resistance=None,
                               demand=None, supply=None):
        p       = self.fmt_price
        d_icon  = "📈" if direction == "LONG" else "📉"
        rr      = sl_tp['risk_reward']
        rl      = self.risk_level(rr)
        rl_icon = {"LOW":"🟢","MEDIUM":"🟡","HIGH":"🔴"}.get(rl,"⚪")

        reasons_text = "\n".join([f"  • {r}" for r in reasons[:5]])

        # Format zona S&R
        sr_text = ""
        if resistance:
            r_str    = "  |  ".join([p(r) for r in resistance[:2]])
            sr_text += f"🔴 *Resistance* : `{r_str}`\n"
        if support:
            s_str    = "  |  ".join([p(s) for s in support[:2]])
            sr_text += f"🟢 *Support*    : `{s_str}`\n"

        # Format zona Supply & Demand
        sd_text = ""
        if supply:
            for z in supply[:1]:
                sd_text += (f"🔴 *Supply Zone*: `{p(z['low'])} – {p(z['high'])}`"
                            f"  _(strength: {z['strength']}x)_\n")
        if demand:
            for z in demand[:1]:
                sd_text += (f"🟢 *Demand Zone*: `{p(z['low'])} – {p(z['high'])}`"
                            f"  _(strength: {z['strength']}x)_\n")

        msg = (
            f"{d_icon} *SINYAL {direction}: {symbol}/USDT*\n"
            f"{'─'*32}\n\n"
            f"💵 *Entry*      : `{p(entry)}`\n"
            f"🔴 *Stop Loss*  : `{p(sl_tp['stop_loss'])}`  _(-{sl_tp['risk_pct']}%)_\n"
            f"🎯 *Take Profit*: `{p(sl_tp['take_profit'])}`  _(+{sl_tp['reward_pct']}%)_\n\n"
            f"⚖️  *R:R Ratio* : *{rr}:1*\n"
            f"{rl_icon} *Risk Level* : *{rl}*\n"
            f"📊 *RSI*        : {rsi:.1f}\n"
            f"🧭 *Kondisi*    : {label}\n"
            f"🔢 *Score*      : {score:+.0f}/100\n\n"
        )

        if sr_text or sd_text:
            msg += f"📍 *Level Penting:*\n{sr_text}{sd_text}\n"

        msg += (
            f"📋 *Alasan Sinyal:*\n{reasons_text}\n\n"
            f"💡 *Cara Pasang:*\n"
            f"  1️⃣  Entry di `{p(entry)}`\n"
            f"  2️⃣  Stop Loss di `{p(sl_tp['stop_loss'])}`\n"
            f"  3️⃣  Take Profit di `{p(sl_tp['take_profit'])}`\n\n"
            f"⚠️ _Risk 2% per trade. Selalu pasang SL!_"
        )
        return msg

    # ------------------------------------------------------------------
    # CEK EXIT TRADE
    # ------------------------------------------------------------------
    def check_exit(self, trade, current_price):
        direction = trade[3]
        entry     = trade[4]
        sl        = trade[5]
        tp        = trade[6]
        size      = trade[7]

        def make_exit(status, exit_price):
            mult = 1 if direction == 'LONG' else -1
            pnl  = round((exit_price - entry) * size * mult, 2)
            pct  = round((exit_price - entry) / entry * 100 * mult, 2)
            return {
                'status'        : status,
                'exit_price'    : exit_price,
                'exit_timestamp': datetime.now().isoformat(),
                'pnl'           : pnl,
                'pnl_percent'   : pct
            }

        if direction == 'LONG':
            if current_price <= sl: return make_exit('loss', sl)
            if current_price >= tp: return make_exit('win',  tp)
        else:
            if current_price >= sl: return make_exit('loss', sl)
            if current_price <= tp: return make_exit('win',  tp)
        return None