"""
coin_profiler.py — Profiling karakter setiap coin dari data historis + backtest.
"""

import sys
import json
import pickle
import argparse
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
from backtesting.data_fetcher import CACHE_FILE

PROFILES_FILE = Path(__file__).parent / "coin_profiles.json"
RESULTS_DIR   = Path(__file__).parent / "results"


def detect_regime(df, lookback=100):
    if df is None or len(df) < lookback + 10:
        return {"regime": "UNKNOWN", "adx_avg": 20, "trend_pct": 50}
    recent = df.tail(lookback).copy()
    h, l, c = recent["high"].values, recent["low"].values, recent["close"].values
    n = len(c)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    dmp, dmn = np.zeros(n), np.zeros(n)
    for i in range(1, n):
        up, dn = h[i]-h[i-1], l[i-1]-l[i]
        dmp[i] = up if (up > dn and up > 0) else 0
        dmn[i] = dn if (dn > up and dn > 0) else 0
    period = 14
    if n < period * 2:
        return {"regime": "UNKNOWN", "adx_avg": 20, "trend_pct": 50}
    atr_s, di_p, di_n = np.zeros(n), np.zeros(n), np.zeros(n)
    atr_s[period] = np.mean(tr[1:period+1])
    di_p[period]  = np.mean(dmp[1:period+1])
    di_n[period]  = np.mean(dmn[1:period+1])
    for i in range(period+1, n):
        atr_s[i] = (atr_s[i-1]*(period-1) + tr[i]) / period
        di_p[i]  = (di_p[i-1]*(period-1) + dmp[i]) / period
        di_n[i]  = (di_n[i-1]*(period-1) + dmn[i]) / period
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = np.where(atr_s > 0, di_p/atr_s*100, 0)
        ndi = np.where(atr_s > 0, di_n/atr_s*100, 0)
        dx  = np.where((pdi+ndi) > 0, np.abs(pdi-ndi)/(pdi+ndi)*100, 0)
    adx_arr = np.zeros(n)
    adx_arr[period*2] = np.mean(dx[period:period*2+1])
    for i in range(period*2+1, n):
        adx_arr[i] = (adx_arr[i-1]*(period-1) + dx[i]) / period
    valid = adx_arr[period*2:]
    adx_avg   = float(np.mean(valid)) if len(valid) > 0 else 20.0
    trend_pct = float(np.sum(valid >= 20)/len(valid)*100) if len(valid) > 0 else 50.0
    if adx_avg >= 25 and trend_pct >= 60:   regime = "STRONG_TRENDING"
    elif adx_avg >= 18 and trend_pct >= 45: regime = "TRENDING"
    elif adx_avg < 15 or trend_pct < 30:   regime = "RANGING"
    else:                                   regime = "MIXED"
    return {"regime": regime, "adx_avg": round(adx_avg,1),
            "trend_pct": round(trend_pct,1), "atr_val": round(float(np.mean(tr[1:])),6)}


def compute_volatility(df, lookback=100):
    if df is None or len(df) < lookback:
        return {"vol_pct": 2.0, "category": "MEDIUM"}
    recent = df.tail(lookback).copy()
    price  = float(recent["close"].iloc[-1])
    h, l, c = recent["high"].values, recent["low"].values, recent["close"].values
    tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
    atr     = float(np.mean(tr))
    vol_pct = (atr / max(price, 1)) * 100
    if vol_pct > 4.0:   category = "HIGH"
    elif vol_pct > 2.0: category = "MEDIUM"
    elif vol_pct > 1.0: category = "LOW"
    else:               category = "VERY_LOW"
    return {"vol_pct": round(vol_pct,3), "atr": round(atr,6),
            "price": round(price,4), "category": category}


def compute_btc_correlation(df_coin, df_btc, lookback=180):
    if df_coin is None or df_btc is None:
        return {"correlation": 0.5, "category": "MEDIUM"}
    if len(df_coin) < lookback or len(df_btc) < lookback:
        return {"correlation": 0.5, "category": "MEDIUM"}
    c_coin = df_coin["close"].tail(lookback).values
    c_btc  = df_btc["close"].tail(lookback).values
    min_len = min(len(c_coin), len(c_btc))
    c_coin, c_btc = c_coin[-min_len:], c_btc[-min_len:]
    if min_len < 10: return {"correlation": 0.5, "category": "MEDIUM"}
    ret_coin = np.diff(c_coin) / c_coin[:-1]
    ret_btc  = np.diff(c_btc)  / c_btc[:-1]
    try:
        corr = float(np.corrcoef(ret_coin, ret_btc)[0, 1])
        if np.isnan(corr): corr = 0.5
    except: corr = 0.5
    if corr >= 0.75:   category = "HIGH"
    elif corr >= 0.5:  category = "MEDIUM"
    elif corr >= 0.25: category = "LOW"
    else:              category = "INDEPENDENT"
    return {"correlation": round(corr,3), "category": category}


def compute_sl_optimal(trades_coin):
    sl_trades = [t for t in trades_coin if t.get("outcome") == "SL_HIT"]
    if len(sl_trades) < 5:
        return {"sl_buffer_suggested": 1.0, "fast_sl_pct": 0, "n_sl": len(sl_trades)}
    bars     = [t.get("bars_to_outcome", 10) for t in sl_trades]
    fast_sl  = sum(1 for b in bars if b <= 3)
    fast_pct = fast_sl / len(bars) * 100
    if fast_pct > 40:   suggested = 1.8
    elif fast_pct > 25: suggested = 1.4
    else:               suggested = 1.0
    return {"sl_buffer_suggested": suggested, "fast_sl_pct": round(fast_pct,1),
            "avg_bars_to_sl": round(float(np.mean(bars)),1), "n_sl": len(sl_trades)}


def compute_score_sweetspot(trades_coin):
    if len(trades_coin) < 10:
        return {"sweetspot_min": 9, "sweetspot_max": 18, "n_trades": len(trades_coin)}
    by_score = defaultdict(list)
    for t in trades_coin:
        bucket = (t.get("confluence_score", 0) // 3) * 3
        by_score[bucket].append(t)
    best_ev, best_range = -99, (9, 18)
    for bucket, group in by_score.items():
        if len(group) < 3: continue
        wins   = sum(1 for t in group if t.get("outcome") == "TP2_HIT")
        n      = len(group)
        avg_rr = sum(t.get("rr2", 2.0) for t in group) / n
        ev_val = (wins/n) * avg_rr - (1-wins/n) * 1.0
        if ev_val > best_ev:
            best_ev, best_range = ev_val, (bucket, bucket+5)
    return {"sweetspot_min": best_range[0], "sweetspot_max": best_range[1],
            "best_ev": round(best_ev,3), "n_trades": len(trades_coin)}


def compute_best_direction(trades_coin):
    if len(trades_coin) < 10: return "BOTH"
    def ev_calc(ts):
        if not ts: return -99
        wins = sum(1 for t in ts if t.get("outcome") == "TP2_HIT")
        n    = len(ts)
        avg_rr = sum(t.get("rr2", 2.0) for t in ts) / n
        return (wins/n)*avg_rr - (1-wins/n)*1.0
    ev_l = ev_calc([t for t in trades_coin if t.get("direction") == "LONG"])
    ev_s = ev_calc([t for t in trades_coin if t.get("direction") == "SHORT"])
    if ev_l > 0.05 and ev_s < -0.1: return "LONG_PREFERRED"
    if ev_s > 0.05 and ev_l < -0.1: return "SHORT_PREFERRED"
    return "BOTH"


def compute_best_tf(trades_coin):
    """
    TF mana yang lebih baik untuk coin ini.
    PENTING: Hanya lock ke satu TF jika perbedaan EV > 0.20R DAN TF terbaik > +0.05R.
    Default BOTH agar semua TF tetap di-scan dan tidak ada yang diblok.
    """
    if len(trades_coin) < 20: return "BOTH"
    by_tf = defaultdict(list)
    for t in trades_coin: by_tf[t.get("tf","1h")].append(t)
    def ev_calc(ts):
        if not ts: return -99
        wins   = sum(1 for t in ts if t.get("outcome") == "TP2_HIT")
        n      = len(ts)
        avg_rr = sum(t.get("rr2", 2.0) for t in ts) / n
        return (wins/n)*avg_rr - (1-wins/n)*1.0
    tf_evs = {tf: ev_calc(trades) for tf, trades in by_tf.items() if len(trades) >= 10}
    if not tf_evs: return "BOTH"
    sorted_tfs = sorted(tf_evs.items(), key=lambda x: x[1], reverse=True)
    best_tf, best_ev = sorted_tfs[0]
    # Hanya lock ke satu TF jika perbedaan sangat signifikan
    if len(sorted_tfs) >= 2:
        second_ev = sorted_tfs[1][1]
        if best_ev > 0.05 and (best_ev - second_ev) > 0.20:
            return best_tf
    return "BOTH"


def profile_coin(symbol, data, all_trades):
    coin_data   = data.get(symbol, {})
    df_1h       = coin_data.get("1h")
    df_btc      = data.get("BTC", {}).get("1h")
    coin_trades = [t for t in all_trades if t.get("symbol") == symbol]

    regime     = detect_regime(df_1h)
    volatility = compute_volatility(df_1h)
    btc_corr   = compute_btc_correlation(df_1h, df_btc)
    sl_opt     = compute_sl_optimal(coin_trades)
    score_ss   = compute_score_sweetspot(coin_trades)
    best_dir   = compute_best_direction(coin_trades)
    best_tf    = compute_best_tf(coin_trades)

    n = len(coin_trades)
    if n >= 5:
        wins      = sum(1 for t in coin_trades if t.get("outcome") == "TP2_HIT")
        avg_rr    = sum(t.get("rr2", 2.0) for t in coin_trades) / n
        wr_val    = wins / n
        overall_ev = round(wr_val*avg_rr - (1-wr_val)*1.0, 3)
        overall_wr = round(wr_val*100, 1)
    else:
        overall_ev, overall_wr = 0.0, 0.0

    if n < 20:             tradeable = "INSUFFICIENT_DATA"
    elif overall_ev >= 0:  tradeable = "YES"
    elif overall_ev >= -0.10: tradeable = "BORDERLINE"
    else:                  tradeable = "NO"

    # Cap score thresholds agar tidak terlalu tinggi dan memblok semua signal
    raw_ideal    = score_ss["sweetspot_min"] + 3
    raw_good     = score_ss["sweetspot_min"]
    raw_moderate = max(3, score_ss["sweetspot_min"] - 3)

    config_overrides = {
        "sl_atr_buffer"      : sl_opt["sl_buffer_suggested"],
        "score_ideal"        : min(15, max(9,  raw_ideal)),
        "score_good"         : min(12, max(6,  raw_good)),
        "score_moderate"     : min(6,  max(3,  raw_moderate)),
        "preferred_direction": best_dir,
        "preferred_tf"       : best_tf,
    }

    if regime["regime"] == "RANGING":
        config_overrides["adx_ranging_block"] = 8
    elif regime["regime"] == "STRONG_TRENDING":
        config_overrides["adx_ranging_block"] = 18

    if volatility["category"] == "HIGH":
        config_overrides["sl_atr_buffer"] = max(config_overrides["sl_atr_buffer"], 2.0)
    elif volatility["category"] == "VERY_LOW":
        config_overrides["sl_atr_buffer"] = min(config_overrides["sl_atr_buffer"], 0.8)

    return {
        "symbol": symbol, "updated_at": datetime.now().isoformat(),
        "n_trades": n, "overall_wr": overall_wr, "overall_ev": overall_ev,
        "tradeable": tradeable, "regime": regime, "volatility": volatility,
        "btc_correlation": btc_corr, "sl_analysis": sl_opt,
        "score_sweetspot": score_ss, "best_direction": best_dir,
        "best_tf": best_tf, "config_overrides": config_overrides,
    }


def load_all_trades():
    trades = []
    for f in sorted(RESULTS_DIR.glob("trades_*.json")):
        try:
            with open(f) as fp: trades.extend(json.load(fp))
        except: pass
    return trades


def print_profiles(profiles):
    SEP = "=" * 85
    print(f"\n{SEP}\n  COIN PROFILES — {len(profiles)} coins\n{SEP}")
    print(f"\n  {'Coin':<8} {'N':>6} {'WR':>6} {'EV':>8} {'Regime':<18} {'Vol':<8} "
          f"{'BTC':>6} {'Tradeable':<16} {'Dir'}")
    print(f"  {'-'*83}")
    for sym, p in sorted(profiles.items(), key=lambda x: x[1].get("overall_ev",0), reverse=True):
        ev_v = p.get("overall_ev", 0)
        trd  = p.get("tradeable", "?")
        flag = "V" if trd=="YES" else ("~" if trd=="BORDERLINE" else "X")
        print(f"  {sym:<8} {p.get('n_trades',0):>6} {p.get('overall_wr',0):>5.1f}% "
              f"{ev_v:>+7.3f}R {p.get('regime',{}).get('regime','?'):<18} "
              f"{p.get('volatility',{}).get('category','?'):<8} "
              f"{p.get('btc_correlation',{}).get('correlation',0):>5.2f}  "
              f"{trd:<16} {flag}  {p.get('best_direction','BOTH')}")
    print(f"\n{SEP}\n  CONFIG OVERRIDES per coin (YES & BORDERLINE):\n  {'-'*83}")
    for sym, p in sorted(profiles.items(), key=lambda x: x[1].get("overall_ev",0), reverse=True):
        if p.get("tradeable") not in ("YES","BORDERLINE"): continue
        ov = p.get("config_overrides", {})
        print(f"\n  {sym}: sl_buf={ov.get('sl_atr_buffer',1.0):.1f}  "
              f"score={ov.get('score_moderate',3)}/{ov.get('score_good',9)}/{ov.get('score_ideal',12)}  "
              f"dir={ov.get('preferred_direction','BOTH')}  tf={ov.get('preferred_tf','BOTH')}")
    print(f"\n{SEP}\n")


def main():
    parser = argparse.ArgumentParser(description="Coin Profiler")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--coin", type=str, default=None)
    args = parser.parse_args()

    if args.show and PROFILES_FILE.exists():
        with open(PROFILES_FILE) as f: profiles = json.load(f)
        print_profiles(profiles)
        return

    if not CACHE_FILE.exists():
        print("ERROR: Cache tidak ditemukan. Jalankan --fetch-only dulu.")
        sys.exit(1)

    print("Loading data cache...")
    with open(CACHE_FILE, "rb") as f: data = pickle.load(f)
    print(f"  {len(data)} coins")

    print("Loading backtest trades...")
    all_trades = load_all_trades()
    print(f"  {len(all_trades)} trades dari semua run\n")

    coins = [args.coin.upper()] if args.coin else sorted(data.keys())
    profiles = {}
    if PROFILES_FILE.exists():
        with open(PROFILES_FILE) as f: profiles = json.load(f)

    for i, sym in enumerate(coins):
        print(f"  [{i+1}/{len(coins)}] {sym}...", end=" ", flush=True)
        try:
            p = profile_coin(sym, data, all_trades)
            profiles[sym] = p
            print(f"EV={p['overall_ev']:+.3f}  {p['tradeable']}")
        except Exception as e:
            print(f"ERROR: {e}")

    with open(PROFILES_FILE, "w") as f: json.dump(profiles, f, indent=2)
    print(f"\nDisimpan: {PROFILES_FILE}")
    print_profiles(profiles)


if __name__ == "__main__":
    main()