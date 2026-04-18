"""
debug4.py — Cari kenapa signal=None (bukan WAIT) di mayoritas scan points.
"""
import sys, pickle, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from backtesting.data_fetcher import CACHE_FILE
from backtesting.replay_engine import BacktestEngine
from sr_detector import detect_key_levels
from smc_analyzer import build_smc_analysis
from indicators import analyze_ema_trend, detect_market_structure, calc_rsi, calc_atr, calc_adx
from signal_generator import generate_entry_signal
from config import SIGNAL_PARAMS as SP

print("Loading cached data...")
with open(CACHE_FILE, "rb") as f:
    data = pickle.load(f)

engine = BacktestEngine(data)

print("\n" + "="*70)
print("DEEP DEBUG: Trace setiap langkah di BTC 1h untuk 4 scan points")
print("="*70)

for idx in [800, 1200, 2000, 3000]:
    engine.set_context("BTC", "1h", idx)
    df_m = engine.get_klines("BTC", "1h")
    df_h = engine.get_klines("BTC", "1h", is_higher=True)
    df_l = engine.get_klines("BTC", "1h", is_lower=True)
    pd_  = engine.get_price("BTC")
    drv  = engine.fetch_derivatives("BTC")
    btc  = engine.check_btc_condition()
    ts   = data["BTC"]["1h"].iloc[idx]["timestamp"]

    print(f"\n--- idx={idx} ({ts.strftime('%Y-%m-%d %H:%M')}) ---")
    print(f"  df_m={len(df_m) if df_m is not None else None}  df_h={len(df_h) if df_h is not None else None}  df_l={len(df_l) if df_l is not None else None}")
    print(f"  btc: ok_long={btc['ok_long']} ok_short={btc['ok_short']} bias={btc['btc_bias']}")

    if pd_ is None:
        print("  price=None — SKIP")
        continue
    price = pd_['price']
    print(f"  price={price:.2f}")

    # Indicators
    try:
        atr_s = calc_atr(df_m, 14)
        atr   = float(atr_s.iloc[-1]) if atr_s is not None else price * 0.01
        adx_s = calc_adx(df_m, 14)
        adx   = float(adx_s.iloc[-1]) if adx_s is not None else 20
        rsi_s = calc_rsi(df_m, 14)
        rsi   = float(rsi_s.iloc[-1]) if rsi_s is not None else 50
        et, _, eth = analyze_ema_trend(df_m), None, None
        try:
            et, _, eth = analyze_ema_trend(df_m)
        except:
            et = "SIDEWAYS"; eth = "SIDEWAYS"
        structure = detect_market_structure(df_m)
        print(f"  atr={atr:.2f}  adx={adx:.1f}  rsi={rsi:.1f}  ema={et}  struct={structure}")
    except Exception as e:
        print(f"  indicator error: {e}")
        continue

    # S&R
    try:
        ks, kr, res_mtf, sup_mtf = detect_key_levels(df_h, df_m, price, "BTC", "1h")
        print(f"  ks={'YES' if ks else 'None'}  kr={'YES' if kr else 'None'}  res_mtf={len(res_mtf)}  sup_mtf={len(sup_mtf)}")
        if ks: print(f"    ks: price={ks['price']:.2f} score={ks.get('score',0)}")
        if kr: print(f"    kr: price={kr['price']:.2f} score={kr.get('score',0)}")
    except Exception as e:
        print(f"  detect_key_levels error: {e}")
        traceback.print_exc()
        continue

    # SMC
    try:
        smc = build_smc_analysis(df_m, df_h, price, atr, symbol="BTC",
                                  fetch_derivatives_fn=engine.fetch_derivatives)
        print(f"  smc: bias={smc.get('bias','?')} confidence={smc.get('confidence','?')}")
    except Exception as e:
        print(f"  smc error: {e}")
        smc = {}

    # Signal generator
    try:
        eth_val = eth if eth else "SIDEWAYS"
        signal = generate_entry_signal(
            price, atr, et, structure, ks, kr, res_mtf, sup_mtf, smc,
            rsi=rsi, htf_ema=eth_val, df_main=df_m, symbol="BTC", adx=adx,
            signal_cache={}
        )
        if signal is None:
            print(f"  signal=None ← generate_entry_signal returned None")
        else:
            q     = signal.get('quality')
            score = signal.get('confluence_score')
            kills = signal.get('kill_count')
            print(f"  signal: {signal.get('direction')} {q} score={score} kills={kills}")
            reasons = signal.get('reasons', [])
            print(f"  reasons ({len(reasons)}):")
            for r in reasons[:15]:
                print(f"    {r}")
    except Exception as e:
        print(f"  generate_entry_signal error: {e}")
        traceback.print_exc()

print("\n" + "="*70)
print("SCAN: Cari 10 scan point pertama yang hasilkan signal NON-WAIT di BTC 1h")
print("="*70)

found = 0
for idx in range(200, len(data["BTC"]["1h"]) - 10, 4):
    engine.set_context("BTC", "1h", idx)
    try:
        result, err = engine.analyze_coin("BTC", "1h")
        if err or result is None:
            continue
        sig = result.get("signal")
        if sig and sig.get("quality") not in ("WAIT", None):
            ts = data["BTC"]["1h"].iloc[idx]["timestamp"]
            print(f"  [{idx}] {ts.strftime('%Y-%m-%d %H:%M')} → {sig['direction']} {sig['quality']} score={sig.get('confluence_score')} kills={sig.get('kill_count')}")
            found += 1
            if found >= 10:
                break
    except:
        pass

if found == 0:
    print("  Tidak ada non-WAIT signal ditemukan di BTC 1h!")
    print("  Cek apakah LTF trigger selalu gagal...")

    # Scan WAIT signals saja
    print("\n  WAIT signals di BTC 1h (10 pertama):")
    found_wait = 0
    for idx in range(200, min(len(data["BTC"]["1h"])-10, 1000), 4):
        engine.set_context("BTC", "1h", idx)
        try:
            result, err = engine.analyze_coin("BTC", "1h")
            if err or result is None: continue
            sig = result.get("signal")
            if sig and sig.get("quality") == "WAIT":
                ts = data["BTC"]["1h"].iloc[idx]["timestamp"]
                reasons = sig.get("reasons", [])
                last_reason = reasons[-1] if reasons else ""
                print(f"    [{idx}] {ts.strftime('%Y-%m-%d')} score={sig.get('confluence_score')} → {last_reason}")
                found_wait += 1
                if found_wait >= 10:
                    break
        except:
            pass
