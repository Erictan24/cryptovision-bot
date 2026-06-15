"""
Test lokal PaperTrader — simulasi siklus trade tanpa network.
Mock _get_current_price (feed harga manual) + _tg_send (no Telegram).
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Pastikan ada keys palsu biar is_ready True
os.environ.setdefault('BITUNIX_API_KEY', 'testkey')
os.environ.setdefault('BITUNIX_SECRET_KEY', 'testsecret')
os.environ['TRADE_RISK_USD'] = '1'
os.environ['TRADE_MAX_POSITIONS'] = '99999'
os.environ['TRADE_MAX_DAILY_LOSS_USD'] = '99999'

from paper_trader import PaperTrader

# DB sementara
tmp_db = os.path.join(tempfile.gettempdir(), 'test_paper_trades.db')
if os.path.exists(tmp_db):
    os.remove(tmp_db)

pt = PaperTrader()
pt._db_path = tmp_db
pt._init_db()

# Mock network
_sent = []
pt._tg_send = lambda msg: _sent.append(msg) or True
_price = {'val': 0.0}
pt._get_current_price = lambda sym: _price['val']
pt.round_qty = lambda q, s: round(q, 3)
pt.get_min_qty = lambda s: (0.001, 3)

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name)

def last_trade():
    return pt._query("SELECT * FROM paper_trades ORDER BY id DESC LIMIT 1")[0]

# ================================================================
# SKENARIO 1: LONG full win — PENDING -> fill -> TP1 -> Stage2/3 -> TP2
# entry=100 sl=95 (risk_dist=5) tp1=106 (1.2R) tp2=110 (2.0R)
# ================================================================
print("\n[S1] LONG full win TP2")
r = pt.place_order('AAA', 'LONG', 100, 95, 106, 110, quality='GOOD',
                   signal_data={'confluence_score': 20})
check("place_order ok", r['ok'] and r['order_type'] == 'LIMIT')
check("status PENDING", last_trade()['status'] == 'PENDING')

# Harga belum nyentuh entry (turun ke 100) -> tetap pending
_price['val'] = 101
pt._tick()
check("masih PENDING di 101", last_trade()['status'] == 'PENDING')

# Harga turun ke 100 -> fill
_price['val'] = 100
pt._tick()
check("FILLED jadi OPEN", last_trade()['status'] == 'OPEN')

# Naik ke 106 -> TP1 hit, SL ke BEP(100)
_price['val'] = 106
pt._tick()
t = last_trade()
check("TP1 hit", t['tp1_hit'] == 1 and t['bep_done'] == 1)
check("cur_sl = BEP 100", abs(t['cur_sl'] - 100) < 1e-9)

# Naik ke 107.5 (=entry+1.5R) -> Stage2 SL ke 102.5
_price['val'] = 107.5
pt._tick()
t = last_trade()
check("Stage2 done", t['stage2_done'] == 1 and abs(t['cur_sl'] - 102.5) < 1e-9)

# Naik ke 110 -> TP2, close
_price['val'] = 110
pt._tick()
t = last_trade()
check("CLOSED TP2", t['status'] == 'CLOSED' and t['outcome'] == 'TP2')
# PnL: 0.5*(106-100)/5 + 0.5*(110-100)/5 = 0.6 + 1.0 = 1.6R
check("PnL TP2 = +1.6R", abs(t['pnl_r'] - 1.6) < 1e-6)
check("PnL USD = +1.6", abs(t['pnl_usd'] - 1.6) < 1e-6)

# ================================================================
# SKENARIO 2: SHORT SL sebelum TP1 (full loss -1R)
# entry=200 sl=210 (risk_dist=10) tp1=188 tp2=180
# ================================================================
print("\n[S2] SHORT full loss SL")
pt.place_order('BBB', 'SHORT', 200, 210, 188, 180, quality='MODERATE',
               signal_data={'confluence_score': 18})
_price['val'] = 200   # SHORT fill saat harga >= entry
pt._tick()
check("SHORT FILLED", last_trade()['status'] == 'OPEN')
_price['val'] = 210   # SL kena
pt._tick()
t = last_trade()
check("CLOSED SL", t['status'] == 'CLOSED' and t['outcome'] == 'SL')
check("PnL SL = -1.0R", abs(t['pnl_r'] + 1.0) < 1e-6)

# ================================================================
# SKENARIO 3: LONG TP1 lalu balik ke BEP (outcome BEP)
# entry=50 sl=48 (risk_dist=2) tp1=52 (1.0R, di bawah trig Stage2=53) tp2=58
# ================================================================
print("\n[S3] LONG TP1 -> balik BEP")
pt.place_order('CCC', 'LONG', 50, 48, 52, 58, quality='GOOD',
               signal_data={'confluence_score': 19})
_price['val'] = 50
pt._tick()           # fill
_price['val'] = 52
pt._tick()           # TP1, SL->BEP 50 (Stage2 belum krn trig=53)
t = last_trade()
check("TP1 hit S3", t['tp1_hit'] == 1 and t['stage2_done'] == 0)
_price['val'] = 50   # balik ke BEP -> SL(=50) kena
pt._tick()
t = last_trade()
check("CLOSED BEP", t['status'] == 'CLOSED' and t['outcome'] == 'BEP')
# PnL: 0.5*(52-50)/2 + 0.5*(50-50)/2 = 0.5R
check("PnL BEP = +0.5R", abs(t['pnl_r'] - 0.5) < 1e-6)

# ================================================================
# SKENARIO 4: Expiry — limit gak kena dalam window
# ================================================================
print("\n[S4] Expiry limit")
pt.place_order('DDD', 'LONG', 10, 9, 12, 14)
# Paksa expire_at ke masa lalu
tid = last_trade()['id']
pt._update(tid, expire_at='2000-01-01T00:00:00')
_price['val'] = 11   # belum nyentuh entry 10
pt._tick()
t = last_trade()
check("EXPIRED", t['status'] == 'EXPIRED')

# ================================================================
# Stats
# ================================================================
print("\n[STATS]")
s = pt.paper_stats()
print("  ", s)
check("closed = 3", s['closed'] == 3)
check("wins = 2", s['wins'] == 2)       # S1 TP2, S3 BEP(+0.75) win; S2 SL loss
check("expired = 1", s['expired'] == 1)

print(f"\n=== {len(PASS)} PASS / {len(FAIL)} FAIL ===")
if FAIL:
    print("GAGAL:", FAIL)
    sys.exit(1)
print("SEMUA TEST LULUS - OK")
os.remove(tmp_db)
