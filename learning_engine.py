"""
learning_engine.py — Bot belajar dari setiap trade yang terjadi.

Cara kerja:
  1. Saat trade dieksekusi -> catat 15+ kondisi sinyal (RSI, BTC state, session, score, dll)
  2. Saat trade tutup (SL/TP) -> catat hasil dan hitung WR per kondisi
  3. Setelah cukup data -> auto-adjust threshold di SIGNAL_PARAMS
  4. /learn command -> tampilkan laporan introspeksi lengkap

Kondisi yang dilacak:
  direction, quality, score, kills, rsi, adx, ema_trend, structure,
  btc_state, session, smc_phase, pd_zone, rejection_strength,
  htf_aligned, fvg_near, ob_near, rr2

Output:
  data/learning.db     — riwayat semua trade + kondisi
  data/learned_params.json — threshold baru hasil belajar
"""

import json
import os
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

DB_PATH            = 'data/learning.db'
LEARNED_PARAMS_PATH = 'data/learned_params.json'

# Minimum sample sebelum auto-adjust diaktifkan per kondisi
MIN_SAMPLE_ADJUST  = 8
# WR minimum untuk kondisi agar TIDAK diblok
WR_BLOCK_THRESHOLD = 0.38   # kalau WR < 38% -> blok kondisi itu
# WR sangat bagus -> jangan diubah
WR_KEEP_THRESHOLD  = 0.60


class LearningEngine:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    # ──────────────────────────────────────────────────────────
    # Database setup
    # ──────────────────────────────────────────────────────────
    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS trade_log (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_key        TEXT,       -- symbol_direction_timestamp
                    symbol           TEXT,
                    direction        TEXT,       -- LONG / SHORT
                    quality          TEXT,       -- GOOD / IDEAL / MODERATE
                    entry_price      REAL,
                    sl_price         REAL,
                    tp1_price        REAL,
                    tp2_price        REAL,

                    -- Kondisi sinyal saat entry
                    confluence_score INTEGER,
                    kill_count       INTEGER,
                    rsi              REAL,
                    adx              REAL,
                    ema_trend        TEXT,
                    structure        TEXT,
                    btc_state        TEXT,       -- BULLISH / BEARISH / NEUTRAL
                    session          TEXT,       -- LONDON / NY / ASIA / DEAD / OVERLAP
                    smc_phase        TEXT,       -- ACCUMULATION / MARKUP / etc.
                    pd_zone          TEXT,       -- DISCOUNT / PREMIUM / etc.
                    rejection_strength INTEGER,
                    htf_aligned      INTEGER,    -- 0/1
                    fvg_near         INTEGER,    -- 0/1
                    ob_near          INTEGER,    -- 0/1
                    rr2              REAL,

                    -- Hasil trade
                    outcome          TEXT,       -- SL / TP1 / TP2 / EXPIRED / OPEN
                    pnl_usd          REAL,
                    entry_time       TEXT,
                    close_time       TEXT,
                    hours_open       REAL,

                    created_at       TEXT DEFAULT (datetime('now'))
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS postmortem (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_key   TEXT,
                    symbol      TEXT,
                    direction   TEXT,
                    outcome     TEXT,
                    analysis    TEXT,   -- JSON teks analisa
                    created_at  TEXT DEFAULT (datetime('now'))
                )
            """)

    def _conn(self):
        return sqlite3.connect(self.db_path)

    # ──────────────────────────────────────────────────────────
    # 1. LOG KONDISI SAAT ENTRY
    # ──────────────────────────────────────────────────────────
    def log_entry(self, symbol: str, direction: str, signal: dict,
                  btc_state: str = 'NEUTRAL', session: str = 'UNKNOWN',
                  smc: dict = None, adx: float = 0.0):
        """
        Dipanggil saat trade dieksekusi (di place_order).
        Catat semua kondisi sinyal ke database.
        """
        try:
            ts = datetime.now().isoformat()
            trade_key = f"{symbol}_{direction}_{ts[:16]}"

            smc = smc or {}
            phase   = smc.get('phase', {}).get('phase', 'UNKNOWN')
            pd_zone = smc.get('pd_zone', {}).get('zone', 'UNKNOWN')
            fvg_near = int(bool(
                smc.get('fvg', {}).get('bull_fvg' if direction == 'LONG' else 'bear_fvg')))
            ob_data  = smc.get('order_blocks', {})
            ob_near  = int(ob_data.get('at_bull_ob' if direction == 'LONG' else 'at_bear_ob', False))

            htf_ema   = smc.get('_htf_ema_trend', 'SIDEWAYS')
            htf_align = int(htf_ema in ('STRONG_UP','UP') if direction == 'LONG'
                            else htf_ema in ('STRONG_DOWN','DOWN'))

            # Rejection strength — baca dari reasons (workaround)
            rej_str = 0
            for r in signal.get('reasons', []):
                if 'Pin Bar' in r or 'Engulfing' in r:
                    rej_str = 4; break
                elif 'Hammer' in r or 'Shooting' in r:
                    rej_str = 3; break

            with self._conn() as c:
                c.execute("""
                    INSERT INTO trade_log
                    (trade_key, symbol, direction, quality,
                     entry_price, sl_price, tp1_price, tp2_price,
                     confluence_score, kill_count, rsi, adx,
                     ema_trend, structure, btc_state, session,
                     smc_phase, pd_zone, rejection_strength,
                     htf_aligned, fvg_near, ob_near, rr2,
                     outcome, pnl_usd, entry_time)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',0,?)
                """, (
                    trade_key, symbol, direction,
                    signal.get('quality',''),
                    signal.get('entry', 0), signal.get('sl', 0),
                    signal.get('tp1', 0),   signal.get('tp2', 0),
                    signal.get('confluence_score', 0),
                    signal.get('kill_count', 0),
                    signal.get('_rsi', 50.0), adx,
                    signal.get('_ema_trend', 'SIDEWAYS'),
                    signal.get('_structure', 'SIDEWAYS'),
                    btc_state, session,
                    phase, pd_zone, rej_str,
                    htf_align, fvg_near, ob_near,
                    signal.get('rr2', signal.get('rr', 0)),
                    ts,
                ))
            return trade_key
        except Exception as e:
            logger.error(f"LearningEngine.log_entry: {e}")
            return None

    # ──────────────────────────────────────────────────────────
    # 2. UPDATE HASIL TRADE
    # ──────────────────────────────────────────────────────────
    def log_outcome(self, symbol: str, direction: str,
                    outcome: str, pnl_usd: float):
        """
        Dipanggil saat trade tutup (SL/TP1/TP2).
        Update baris paling baru untuk symbol+direction.
        """
        try:
            close_time = datetime.now().isoformat()
            with self._conn() as c:
                row = c.execute("""
                    SELECT id, entry_time FROM trade_log
                    WHERE symbol=? AND direction=? AND outcome='OPEN'
                    ORDER BY id DESC LIMIT 1
                """, (symbol, direction)).fetchone()

                if row:
                    trade_id, entry_time = row
                    hours = 0.0
                    try:
                        dt_entry = datetime.fromisoformat(entry_time)
                        hours = (datetime.now() - dt_entry).total_seconds() / 3600
                    except Exception:
                        pass
                    c.execute("""
                        UPDATE trade_log
                        SET outcome=?, pnl_usd=?, close_time=?, hours_open=?
                        WHERE id=?
                    """, (outcome, pnl_usd, close_time, round(hours, 2), trade_id))
                    return trade_id
        except Exception as e:
            logger.error(f"LearningEngine.log_outcome: {e}")
        return None

    # ──────────────────────────────────────────────────────────
    # 3. POST-MORTEM — analisa kenapa SL terjadi
    # ──────────────────────────────────────────────────────────
    def generate_postmortem(self, symbol: str, direction: str,
                            outcome: str, pnl_usd: float) -> str:
        """
        Setelah trade tutup, analisa kondisi dan beri penjelasan.
        Return string untuk dikirim ke Telegram.
        """
        try:
            with self._conn() as c:
                row = c.execute("""
                    SELECT * FROM trade_log
                    WHERE symbol=? AND direction=? AND outcome=?
                    ORDER BY id DESC LIMIT 1
                """, (symbol, direction, outcome)).fetchone()

            if not row:
                return ""

            cols = [d[0] for d in c.description] if hasattr(c, 'description') else []
            # Ambil dengan query terpisah untuk dapat kolom names
            with self._conn() as c2:
                c2.row_factory = sqlite3.Row
                row2 = c2.execute("""
                    SELECT * FROM trade_log
                    WHERE symbol=? AND direction=?
                    ORDER BY id DESC LIMIT 1
                """, (symbol, direction)).fetchone()

            if not row2:
                return ""

            r = dict(row2)
            problems = []
            lessons  = []

            # Analisa tiap kondisi
            if r['direction'] == 'LONG':
                if r.get('rsi', 50) >= 65:
                    problems.append(f"RSI {r['rsi']:.0f} (terlalu tinggi untuk LONG)")
                    lessons.append(f"Hindari LONG saat RSI > 62")
                if r.get('btc_state') == 'BEARISH':
                    problems.append("BTC sedang bearish saat entry LONG")
                    lessons.append("Jangan LONG saat BTC bearish")
                if r.get('session') in ('ASIA', 'DEAD'):
                    problems.append(f"Session {r['session']} — volume rendah")
                    lessons.append("Hindari trade di session Asia/Dead Zone")
                if r.get('pd_zone') in ('PREMIUM', 'SLIGHT_PREMIUM'):
                    problems.append("Harga di Premium Zone — LONG di atas equilibrium")
                    lessons.append("LONG idealnya di Discount Zone")
            else:  # SHORT
                if r.get('rsi', 50) <= 35:
                    problems.append(f"RSI {r['rsi']:.0f} (terlalu rendah untuk SHORT)")
                    lessons.append("Hindari SHORT saat RSI < 38")
                if r.get('btc_state') == 'BULLISH':
                    problems.append("BTC sedang bullish saat entry SHORT")
                    lessons.append("Hati-hati SHORT saat BTC kuat")
                if r.get('pd_zone') in ('DISCOUNT', 'SLIGHT_DISCOUNT'):
                    problems.append("Harga di Discount Zone — SHORT di area murah")
                    lessons.append("SHORT idealnya di Premium Zone")

            if r.get('kill_count', 0) >= 2:
                problems.append(f"Kill count {r['kill_count']} — banyak faktor melawan")
                lessons.append("Kill count 2+ = hindari trade")

            if r.get('rejection_strength', 0) <= 2:
                problems.append("Rejection lemah (< kuat) saat entry")
                lessons.append("Tunggu Pin Bar atau Engulfing yang jelas")

            if r.get('confluence_score', 0) < 22:
                problems.append(f"Score {r['confluence_score']} — terlalu dekat threshold minimum")
                lessons.append("Score minimal 22+ lebih aman daripada 21")

            if r.get('structure') == ('DOWNTREND' if r['direction'] == 'LONG' else 'UPTREND'):
                problems.append(f"Struktur melawan direction (counter-trend trade)")
                lessons.append("Hindari counter-trend trade tanpa CHoCH konfirmasi")

            # Simpan ke DB
            analysis_json = json.dumps({
                'problems': problems,
                'lessons': lessons,
                'conditions': {k: r.get(k) for k in
                               ['rsi','adx','btc_state','session','structure',
                                'smc_phase','pd_zone','confluence_score',
                                'kill_count','rejection_strength','htf_aligned']},
            }, ensure_ascii=False)

            with self._conn() as c3:
                c3.execute("""
                    INSERT INTO postmortem (trade_key, symbol, direction, outcome, analysis)
                    VALUES (?,?,?,?,?)
                """, (r.get('trade_key',''), symbol, direction, outcome, analysis_json))

            # Format pesan Telegram
            if outcome == 'SL':
                header = f"📚 POST-MORTEM — {symbol} {direction} -> SL HIT"
            elif outcome in ('TP1', 'TP2'):
                header = f"✅ POST-MORTEM — {symbol} {direction} -> {outcome} HIT"
                return ""  # Tidak perlu introspeksi untuk TP
            else:
                return ""

            lines = [header, "─" * 32]
            lines.append(f"Score: {r.get('confluence_score',0)} | Kills: {r.get('kill_count',0)} | RSI: {r.get('rsi',50):.0f}")
            lines.append(f"Session: {r.get('session','?')} | BTC: {r.get('btc_state','?')} | Zone: {r.get('pd_zone','?')}")
            lines.append("")

            if problems:
                lines.append("⚠️ Yang mungkin menyebabkan SL:")
                for p in problems[:4]:
                    lines.append(f"  • {p}")
            else:
                lines.append("⚠️ Tidak ada faktor jelas — bisa market noise")

            if lessons:
                lines.append("")
                lines.append("💡 Pelajaran:")
                for l in lessons[:3]:
                    lines.append(f"  -> {l}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"generate_postmortem: {e}")
            return ""

    # ──────────────────────────────────────────────────────────
    # 4. ANALISA POLA — cari kondisi yang konsisten SL
    # ──────────────────────────────────────────────────────────
    def analyze_patterns(self) -> dict:
        """
        Cari kondisi mana yang punya WR rendah dari data historis.
        Return dict kondisi -> statistik.
        """
        patterns = {}
        try:
            with self._conn() as c:
                c.row_factory = sqlite3.Row
                rows = c.execute("""
                    SELECT * FROM trade_log
                    WHERE outcome IN ('SL','TP1','TP2')
                    ORDER BY created_at
                """).fetchall()

            if len(rows) < 3:
                return {}

            def _wr_for(key, val):
                subset = [r for r in rows if r[key] == val]
                if len(subset) < MIN_SAMPLE_ADJUST:
                    return None
                wins = sum(1 for r in subset if r['outcome'] in ('TP1','TP2'))
                return {
                    'wr': round(wins / len(subset), 3),
                    'wins': wins,
                    'total': len(subset),
                }

            # Session WR
            for sess in ('LONDON','NY','ASIA','DEAD','OVERLAP'):
                s = _wr_for('session', sess)
                if s:
                    patterns[f'session_{sess}'] = s

            # BTC state WR
            for btc in ('BULLISH','BEARISH','NEUTRAL'):
                s = _wr_for('btc_state', btc)
                if s:
                    patterns[f'btc_{btc}'] = s

            # Direction WR
            for d in ('LONG','SHORT'):
                s = _wr_for('direction', d)
                if s:
                    patterns[f'dir_{d}'] = s

            # Structure WR
            for st in ('UPTREND','DOWNTREND','SIDEWAYS'):
                s = _wr_for('structure', st)
                if s:
                    patterns[f'struct_{st}'] = s

            # Kill count WR
            for k in (0, 1, 2):
                subset = [r for r in rows if r['kill_count'] == k]
                if len(subset) >= MIN_SAMPLE_ADJUST:
                    wins = sum(1 for r in subset if r['outcome'] in ('TP1','TP2'))
                    patterns[f'kills_{k}'] = {
                        'wr': round(wins / len(subset), 3),
                        'wins': wins,
                        'total': len(subset),
                    }

            # Score bucket WR
            buckets = [(21, 22), (22, 23), (23, 24), (24, 30)]
            for lo, hi in buckets:
                subset = [r for r in rows if lo <= (r['confluence_score'] or 0) < hi]
                if len(subset) >= MIN_SAMPLE_ADJUST:
                    wins = sum(1 for r in subset if r['outcome'] in ('TP1','TP2'))
                    patterns[f'score_{lo}_{hi}'] = {
                        'wr': round(wins / len(subset), 3),
                        'wins': wins,
                        'total': len(subset),
                    }

            # RSI bucket WR (LONG only)
            long_rows = [r for r in rows if r['direction'] == 'LONG']
            rsi_buckets = [(25, 40), (40, 50), (50, 60), (60, 65), (65, 75)]
            for lo, hi in rsi_buckets:
                subset = [r for r in long_rows if lo <= (r['rsi'] or 50) < hi]
                if len(subset) >= MIN_SAMPLE_ADJUST:
                    wins = sum(1 for r in subset if r['outcome'] in ('TP1','TP2'))
                    patterns[f'rsi_long_{lo}_{hi}'] = {
                        'wr': round(wins / len(subset), 3),
                        'wins': wins,
                        'total': len(subset),
                    }

        except Exception as e:
            logger.error(f"analyze_patterns: {e}")

        return patterns

    # ──────────────────────────────────────────────────────────
    # 5. AUTO-TUNE — adjust threshold dari pola yang ditemukan
    # ──────────────────────────────────────────────────────────
    def auto_tune(self) -> dict:
        """
        Hitung parameter baru berdasarkan pola historis.
        Return dict adjustments yang akan di-merge ke SIGNAL_PARAMS.
        """
        patterns  = self.analyze_patterns()
        adjustments = {}
        notes     = []

        if not patterns:
            return {}

        # Minimum sample per pola sebelum auto-tune berani membuat keputusan.
        # Sample kecil (< 30) tidak bisa diandalkan — bisa kebetulan semua loss
        # karena kondisi pasar sementara, bukan karena faktornya memang buruk.
        MIN_PATTERN_SAMPLE = 30

        # RSI threshold untuk LONG
        rsi_60_65 = patterns.get('rsi_long_60_65')
        if (rsi_60_65 and rsi_60_65['wr'] < WR_BLOCK_THRESHOLD
                and rsi_60_65.get('total', 0) >= MIN_PATTERN_SAMPLE):
            adjustments['rsi_near_overbought'] = 60
            notes.append(f"RSI 60-65 LONG WR={rsi_60_65['wr']:.0%} (n={rsi_60_65['total']}) -> turun threshold ke 60")

        # Score threshold
        s21 = patterns.get('score_21_22')
        if (s21 and s21['wr'] < WR_BLOCK_THRESHOLD
                and s21.get('total', 0) >= MIN_PATTERN_SAMPLE):
            adjustments['score_good'] = 23
            adjustments['score_moderate'] = 23
            notes.append(f"Score 21-22 WR={s21['wr']:.0%} (n={s21['total']}) -> naikkan min score ke 23")

        # Kill factor — HATI-HATI: jangan sampai block semua signal
        # Hanya naikkan max_kills_good ke 0 kalau sample cukup DAN WR benar-benar buruk
        k1 = patterns.get('kills_1')
        if (k1 and k1['wr'] < WR_BLOCK_THRESHOLD
                and k1.get('total', 0) >= MIN_PATTERN_SAMPLE):
            adjustments['max_kills_good'] = 0
            notes.append(f"Kill=1 WR={k1['wr']:.0%} (n={k1['total']}) -> GOOD hanya boleh 0 kills")

        # Session adjustment — hanya block kalau sample cukup banyak
        asia = patterns.get('session_ASIA')
        if (asia and asia['wr'] < WR_BLOCK_THRESHOLD
                and asia.get('total', 0) >= MIN_PATTERN_SAMPLE):
            adjustments['block_asia_session'] = True
            notes.append(f"ASIA session WR={asia['wr']:.0%} (n={asia['total']}) -> block Asia session")

        dead = patterns.get('session_DEAD')
        if (dead and dead['wr'] < WR_BLOCK_THRESHOLD
                and dead.get('total', 0) >= MIN_PATTERN_SAMPLE):
            adjustments['block_dead_session'] = True
            notes.append(f"DEAD session WR={dead['wr']:.0%} (n={dead['total']}) -> block Dead zone")

        # Simpan ke file
        if adjustments:
            adjustments['_notes']      = notes
            adjustments['_updated_at'] = datetime.now().isoformat()
            adjustments['_sample_size'] = sum(p.get('total', 0)
                                              for p in patterns.values())

            existing = self.load_learned_params()
            existing.update(adjustments)
            with open(LEARNED_PARAMS_PATH, 'w', encoding='utf-8') as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)

            logger.info(f"Auto-tune: {len(adjustments)-3} adjustments saved. Notes: {notes}")

        return adjustments

    # ──────────────────────────────────────────────────────────
    # 6. LOAD LEARNED PARAMS
    # ──────────────────────────────────────────────────────────
    @staticmethod
    def load_learned_params() -> dict:
        """Load parameter hasil belajar dari file JSON."""
        try:
            if os.path.exists(LEARNED_PARAMS_PATH):
                with open(LEARNED_PARAMS_PATH, encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    # ──────────────────────────────────────────────────────────
    # 7. LAPORAN INTROSPEKSI (/learn command)
    # ──────────────────────────────────────────────────────────
    def generate_report(self) -> str:
        """Laporan lengkap untuk Telegram /learn command."""
        try:
            with self._conn() as c:
                c.row_factory = sqlite3.Row
                all_rows = c.execute("""
                    SELECT * FROM trade_log ORDER BY created_at DESC
                """).fetchall()
                pm_rows = c.execute("""
                    SELECT * FROM postmortem ORDER BY created_at DESC LIMIT 5
                """).fetchall()

            closed = [r for r in all_rows if r['outcome'] in ('SL','TP1','TP2')]
            open_  = [r for r in all_rows if r['outcome'] == 'OPEN']

            n_total = len(all_rows)
            n_sl    = sum(1 for r in closed if r['outcome'] == 'SL')
            n_tp    = sum(1 for r in closed if r['outcome'] in ('TP1','TP2'))
            wr      = n_tp / len(closed) if closed else 0

            lines = [
                "🧠 LAPORAN INTROSPEKSI BOT",
                "━" * 32,
                f"Total trade dicatat : {n_total}",
                f"Selesai (SL+TP)     : {len(closed)}",
                f"Win rate            : {wr:.0%} ({n_tp}W / {n_sl}L)",
                f"Masih terbuka       : {len(open_)}",
                "",
            ]

            patterns = self.analyze_patterns()

            # Tampilkan WR per kondisi yang sudah cukup sample
            if patterns:
                lines.append("📊 WR PER KONDISI:")
                lines.append("─" * 32)

                # Sort dari WR terendah
                sorted_p = sorted(patterns.items(), key=lambda x: x[1]['wr'])
                for key, stat in sorted_p[:12]:
                    emoji = "🔴" if stat['wr'] < 0.40 else (
                            "🟡" if stat['wr'] < 0.55 else "🟢")
                    label = key.replace('_', ' ').replace('session ', 'Session ') \
                               .replace('dir ', 'Direction ') \
                               .replace('btc ', 'BTC ') \
                               .replace('struct ', 'Struktur ') \
                               .replace('score ', 'Score ') \
                               .replace('kills ', 'Kill count ') \
                               .replace('rsi long ', 'RSI LONG ')
                    lines.append(f"  {emoji} {label}: {stat['wr']:.0%} ({stat['wins']}W/{stat['total']})")
                lines.append("")

            # Auto-adjustments yang aktif
            learned = self.load_learned_params()
            active_adj = {k: v for k, v in learned.items()
                          if not k.startswith('_')}
            if active_adj:
                lines.append("⚙️ AUTO-ADJUSTMENTS AKTIF:")
                lines.append("─" * 32)
                for k, v in active_adj.items():
                    lines.append(f"  • {k}: {v}")
                if learned.get('_updated_at'):
                    lines.append(f"  (diperbarui: {learned['_updated_at'][:10]})")
                lines.append("")
            else:
                lines.append("⚙️ Belum ada auto-adjustment (perlu min "
                              f"{MIN_SAMPLE_ADJUST}+ trade per kondisi)")
                lines.append("")

            # Post-mortem terbaru
            if pm_rows:
                lines.append("📚 POST-MORTEM TERBARU:")
                lines.append("─" * 32)
                for pm in pm_rows[:3]:
                    try:
                        data = json.loads(pm['analysis'])
                        probs = data.get('problems', [])
                        if probs:
                            lines.append(f"• {pm['symbol']} {pm['direction']} -> {pm['outcome']}")
                            lines.append(f"  {probs[0]}")
                    except Exception:
                        pass
                lines.append("")

            # Rekomendasi
            lines.append("💡 REKOMENDASI SAAT INI:")
            lines.append("─" * 32)
            recs = self._get_recommendations(patterns, learned)
            for r in recs:
                lines.append(f"  {r}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"generate_report: {e}")
            return f"Error generate report: {e}"

    def _get_recommendations(self, patterns: dict, learned: dict) -> list:
        recs = []

        # Cek pola dengan WR rendah
        for key, stat in patterns.items():
            if stat['wr'] < WR_BLOCK_THRESHOLD and stat['total'] >= MIN_SAMPLE_ADJUST:
                if 'ASIA' in key:
                    recs.append("⚠️ Session Asia WR rendah — sudah/perlu diblok")
                elif 'BEARISH' in key and 'btc' in key:
                    recs.append("⚠️ BTC Bearish WR rendah — LONG saat BTC bearish harus lebih ketat")
                elif 'score_21_22' in key:
                    recs.append("⚠️ Score 21-22 tidak reliable — naikkan threshold ke 23+")
                elif 'kills_1' in key:
                    recs.append("⚠️ Trade dengan 1 kill faktor WR rendah — pertimbangkan 0 kill minimum")
                elif 'rsi_long_60' in key:
                    recs.append("⚠️ LONG di RSI 60+ WR rendah — turunkan RSI threshold")

        if not recs:
            recs.append("✅ Belum cukup data untuk rekomendasi spesifik")
            recs.append(f"   Butuh {MIN_SAMPLE_ADJUST}+ trade per kondisi")

        return recs[:5]

    # ──────────────────────────────────────────────────────────
    # 8. STATS RINGKAS
    # ──────────────────────────────────────────────────────────
    def get_quick_stats(self) -> dict:
        """Statistik ringkas untuk tampilkan di /trade status."""
        try:
            with self._conn() as c:
                row = c.execute("""
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN outcome IN ('TP1','TP2') THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN outcome = 'SL' THEN 1 ELSE 0 END) as losses
                    FROM trade_log
                    WHERE outcome IN ('SL','TP1','TP2')
                """).fetchone()
            if row and row[0] > 0:
                return {'total': row[0], 'wins': row[1], 'losses': row[2],
                        'wr': round(row[1] / row[0], 3)}
        except Exception:
            pass
        return {'total': 0, 'wins': 0, 'losses': 0, 'wr': 0}


# ── Singleton ──────────────────────────────────────────────────
_engine: Optional[LearningEngine] = None

def get_learning_engine() -> LearningEngine:
    global _engine
    if _engine is None:
        _engine = LearningEngine()
    return _engine
