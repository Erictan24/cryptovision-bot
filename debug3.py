"""
debug3.py — Print semua reasons dari analyze_coin untuk cari tahu
kenapa hampir semua scan point menghasilkan WAIT/NO_SIGNAL.
"""
import sys, pickle, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from backtesting.data_fetcher import CACHE_FILE
from backtesting.replay_engine import BacktestEngine

print("Loading cached data...")
with open(CACHE_FILE, "rb") as f:
    data = pickle.load(f)

engine = BacktestEngine(data)

# -------------------------------------------------------
# Test 1: Print full reasons untuk beberapa scan point
# -------------------------------------------------------
print("\n" + "="*70)
print("TEST 1: Full reasons untuk BTC 1h di berbagai scan points")
print("="*70)

for idx in [200, 400, 800, 1200, 2000, 3000]:
    engine.set_context("BTC", "1h", idx)
    try:
        result, err = engine.analyze_coin("BTC", "1h")
    except Exception as e:
        print(f"\n[idx={idx}] EXCEPTION: {e}")
        traceback.print_exc()
        continue

    if err:
        print(f"\n[idx={idx}] ERROR: {err}")
        continue
    if result is None:
        print(f"\n[idx={idx}] result=None")
        continue

    sig = result.get("signal")
    if sig is None:
        print(f"\n[idx={idx}] signal=None (result keys: {list(result.keys())})")
        continue

    ts = data["BTC"]["1h"].iloc[idx]["timestamp"]
    q  = sig.get("quality","?")
    sc = sig.get("confluence_score","?")
    kl = sig.get("kill_count","?")
    dr = sig.get("direction","?")
    reasons = sig.get("reasons", [])

    print(f"\n[idx={idx}] {ts.strftime('%Y-%m-%d %H:%M')} → {dr} {q} score={sc} kills={kl}")
    print(f"  Reasons ({len(reasons)}):")
    for r in reasons[:30]:
        print(f"    {r}")

# -------------------------------------------------------
# Test 2: Cek check_btc_condition di beberapa titik
# -------------------------------------------------------
print("\n" + "="*70)
print("TEST 2: BTC condition di berbagai titik")
print("="*70)

for idx in [200, 800, 2000, 3500]:
    engine.set_context("BTC", "1h", idx)
    btc = engine.check_btc_condition()
    ts  = data["BTC"]["1h"].iloc[idx]["timestamp"]
    print(f"  [idx={idx}] {ts.strftime('%Y-%m-%d')} → {btc}")

# -------------------------------------------------------
# Test 3: Cek signal_generator langsung (bypass engine)
# -------------------------------------------------------
print("\n" + "="*70)
print("TEST 3: Cek imports dan module yang dipakai")
print("="*70)

try:
    from signal_generator import generate_entry_signal
    print("  signal_generator: OK")
except Exception as e:
    print(f"  signal_generator: ERROR — {e}")

try:
    from config import SIGNAL_PARAMS as SP
    print(f"  config SIGNAL_PARAMS: OK ({len(SP)} params)")
    print(f"    score_ideal    = {SP.get('score_ideal')}")
    print(f"    score_good     = {SP.get('score_good')}")
    print(f"    score_moderate = {SP.get('score_moderate')}")
    print(f"    score_wait     = {SP.get('score_wait')}")
    print(f"    adx_ranging_block = {SP.get('adx_ranging_block')}")
    print(f"    max_kills_hard_reject = {SP.get('max_kills_hard_reject')}")
    print(f"    btc_bear_change_soft = {SP.get('btc_bear_change_soft')}")
except Exception as e:
    print(f"  config: ERROR — {e}")

# -------------------------------------------------------
# Test 4: Manual signal_generator dengan data real
# -------------------------------------------------------
print("\n" + "="*70)
print("TEST 4: Signal generator langsung dengan data BTC 1h idx=2000")
print("="*70)

engine.set_context("BTC", "1h", 2000)
df_m = engine.get_klines("BTC", "1h")
df_h = engine.get_klines("BTC", "1h", is_higher=True)
df_l = engine.get_klines("BTC", "1h", is_lower=True)
pd_  = engine.get_price("BTC")
drv  = engine.fetch_derivatives("BTC")
btc  = engine.check_btc_condition()

print(f"  df_m: {len(df_m) if df_m is not None else None} candles")
print(f"  df_h: {len(df_h) if df_h is not None else None} candles")
print(f"  df_l: {len(df_l) if df_l is not None else None} candles")
print(f"  price: {pd_}")
print(f"  btc  : {btc}")

try:
    from signal_generator import generate_entry_signal
    signal = generate_entry_signal(df_m, df_h, df_l, pd_, drv, btc, "BTC", "1h")
    if signal:
        print(f"\n  SIGNAL: {signal.get('direction')} {signal.get('quality')} score={signal.get('confluence_score')}")
        print(f"  kills={signal.get('kill_count')}")
        print(f"  reasons:")
        for r in signal.get("reasons", [])[:20]:
            print(f"    {r}")
    else:
        print("  signal = None")
except Exception as e:
    print(f"  ERROR: {e}")
    traceback.print_exc()

