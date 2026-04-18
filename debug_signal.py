"""
debug_signal.py — Cek kenapa signal tidak muncul.
Jalankan: python debug_signal.py
"""
import sys, pickle
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from backtesting.data_fetcher import CACHE_FILE
from backtesting.replay_engine import BacktestEngine

# Load cache
print("Loading cached data...")
with open(CACHE_FILE, "rb") as f:
    data = pickle.load(f)
print(f"Loaded {len(data)} coins\n")

engine = BacktestEngine(data)

# Test beberapa coin × TF × scan point
test_cases = [
    ("BTC",  "1h",  200),
    ("BTC",  "1h",  500),
    ("BTC",  "1h", 1000),
    ("ETH",  "1h",  300),
    ("BTC",  "4h",  100),
    ("BTC",  "4h",  200),
    ("NEAR", "1h",  300),
    ("SEI",  "1h",  300),
    ("TIA",  "1h",  300),
]

print("="*70)
print(f"{'SYMBOL':<8} {'TF':<5} {'IDX':<6} {'RESULT':<12} {'QUALITY':<10} {'SCORE':<7} {'KILLS':<7} {'REASON'}")
print("="*70)

for symbol, tf, idx in test_cases:
    if symbol not in data or tf not in data[symbol]:
        print(f"{symbol:<8} {tf:<5} {idx:<6} NO_DATA")
        continue

    df = data[symbol][tf]
    if len(df) < idx + 10:
        print(f"{symbol:<8} {tf:<5} {idx:<6} IDX_TOO_HIGH (len={len(df)})")
        continue

    try:
        engine.set_context(symbol, tf, idx)
        result, err = engine.analyze_coin(symbol, tf)

        if err:
            print(f"{symbol:<8} {tf:<5} {idx:<6} ERROR        {'':10} {'':7} {'':7} {str(err)[:60]}")
            continue

        if result is None:
            print(f"{symbol:<8} {tf:<5} {idx:<6} NONE")
            continue

        signal = result.get("signal")
        if signal is None:
            print(f"{symbol:<8} {tf:<5} {idx:<6} NO_SIGNAL")
            continue

        q     = signal.get("quality", "?")
        score = signal.get("confluence_score", "?")
        kills = signal.get("kill_count", "?")
        dirs  = signal.get("direction", "?")
        reasons = signal.get("reasons", [])

        # Ambil alasan utama (reject reasons)
        reject = [r for r in reasons if "block" in r.lower() or "kill" in r.lower()
                  or "wait" in r.lower() or "reject" in r.lower() or "fail" in r.lower()]
        reason_str = reject[0] if reject else (reasons[0] if reasons else "")

        print(f"{symbol:<8} {tf:<5} {idx:<6} {dirs+'/'+q:<12} {q:<10} {str(score):<7} {str(kills):<7} {reason_str[:50]}")

    except Exception as e:
        print(f"{symbol:<8} {tf:<5} {idx:<6} EXCEPTION    {'':10} {'':7} {'':7} {str(e)[:60]}")

print()
print("="*70)
print("Scanning BTC 1h untuk temukan scan point yang ada signal...")
print("="*70)

df_btc = data["BTC"]["1h"]
found = 0
for i in range(150, min(len(df_btc)-10, 4000), 4):
    try:
        engine.set_context("BTC", "1h", i)
        result, err = engine.analyze_coin("BTC", "1h")
        if result and result.get("signal"):
            sig = result["signal"]
            q = sig.get("quality","?")
            if q != "WAIT":
                ts = df_btc.iloc[i]["timestamp"]
                score = sig.get("confluence_score","?")
                kills = sig.get("kill_count","?")
                dirs  = sig.get("direction","?")
                print(f"  [{i}] {ts.strftime('%Y-%m-%d %H:%M')} → {dirs} {q} score={score} kills={kills}")
                found += 1
                if found >= 10:
                    break
    except:
        pass

if found == 0:
    print("  Tidak ada signal ditemukan di BTC 1h (scan 150-4000)")
    print()
    print("  Cek raw result untuk BTC 1h idx=200:")
    engine.set_context("BTC", "1h", 200)
    result, err = engine.analyze_coin("BTC", "1h")
    if result:
        sig = result.get("signal", {})
        print(f"  quality: {sig.get('quality')}")
        print(f"  score  : {sig.get('confluence_score')}")
        print(f"  kills  : {sig.get('kill_count')}")
        reasons = sig.get("reasons", [])
        print(f"  reasons ({len(reasons)} total):")
        for r in reasons[:20]:
            print(f"    - {r}")
    elif err:
        print(f"  Error: {err}")
