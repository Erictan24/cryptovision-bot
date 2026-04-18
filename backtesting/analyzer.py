"""
analyzer.py — Analisa mendalam trades JSON untuk temukan pola SL vs TP.
"""

import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

RESULTS_DIR  = Path(__file__).parent / "results"
WEIGHTS_FILE = Path(__file__).parent / "adaptive_weights.json"

def load_trades_raw(path):
    with open(path) as f:
        return json.load(f)

def load_latest():
    files = sorted(RESULTS_DIR.glob("trades_*.json"))
    if not files:
        print("Tidak ada file trades di backtesting/results/")
        sys.exit(1)
    latest = files[-1]
    print(f"Loading: {latest.name}")
    return load_trades_raw(latest), latest

def load_all():
    files = sorted(RESULTS_DIR.glob("trades_*.json"))
    if not files:
        print("Tidak ada file trades di backtesting/results/")
        sys.exit(1)
    all_trades = []
    for f in files:
        trades = load_trades_raw(f)
        all_trades.extend(trades)
        print(f"  Loaded {len(trades):>4} trades dari {f.name}")
    print(f"\n  Total: {len(all_trades)} trades dari {len(files)} file")
    return all_trades, None

def is_win(t):  return t["outcome"] == "TP2_HIT"
def is_loss(t): return t["outcome"] == "SL_HIT"

def ev(trades):
    if not trades: return 0
    wins   = sum(1 for t in trades if is_win(t))
    total  = len(trades)
    avg_rr = sum(t["rr2"] for t in trades) / total
    w      = wins / total
    return round(w * avg_rr - (1 - w) * 1.0, 3)

def wr(trades):
    if not trades: return 0
    return round(sum(1 for t in trades if is_win(t)) / len(trades) * 100, 1)

def ci95(trades):
    n = len(trades)
    if n < 5: return 99.9
    p = wr(trades) / 100
    return round(1.96 * (p * (1 - p) / n) ** 0.5 * 100, 1)

FACTOR_KEYWORDS = {
    "EMA bullish"       : "ema_bullish",
    "EMA bearish"       : "ema_bearish",
    "EMA slight"        : "ema_slight",
    "BOS"               : "bos",
    "CHoCH"             : "choch",
    "HTF BOS"           : "htf_bos",
    "HTF CHoCH"         : "htf_choch",
    "Sangat Kuat"       : "sr_very_strong",
    "Kuat"              : "sr_strong",
    "Fresh level"       : "fresh_level",
    "HTF+MTF"           : "htf_mtf_level",
    "Fase Markup"       : "phase_markup",
    "Fase Distribusi"   : "phase_distribution",
    "Fase Akumulasi"    : "phase_accumulation",
    "Order Flow Bull"   : "orderflow_bull",
    "Order Flow Bear"   : "orderflow_bear",
    "RSI oversold"      : "rsi_oversold",
    "RSI overbought"    : "rsi_overbought",
    "RSI divergence"    : "rsi_divergence",
    "Hidden divergence" : "hidden_divergence",
    "Volume divergence" : "vol_divergence",
    "ADX kuat"          : "adx_strong",
    "ADX trending"      : "adx_trending",
    "Discount zone"     : "discount_zone",
    "Premium zone"      : "premium_zone",
    "HTF EMA bullish"   : "htf_ema_bull",
    "HTF EMA bearish"   : "htf_ema_bear",
    "FVG"               : "fvg",
    "Liquidity"         : "liquidity",
    "Candle pattern"    : "candle_pattern",
    "Order Block"       : "order_block",
    "LTF trigger"       : "ltf_trigger",
}

def extract_factors(reasons):
    factors = set()
    for r in reasons:
        for keyword, factor in FACTOR_KEYWORDS.items():
            if keyword.lower() in str(r).lower():
                factors.add(factor)
    return factors

def analyze_factors(trades):
    factor_trades = defaultdict(lambda: {"with": [], "without": []})
    for t in trades:
        factors     = extract_factors(t.get("reasons", []))
        all_factors = set(FACTOR_KEYWORDS.values())
        for factor in all_factors:
            if factor in factors:
                factor_trades[factor]["with"].append(t)
            else:
                factor_trades[factor]["without"].append(t)
    results = {}
    for factor, groups in factor_trades.items():
        w  = groups["with"]
        wo = groups["without"]
        if len(w) < 5: continue
        wr_with    = wr(w)
        wr_without = wr(wo) if wo else 0
        ev_with    = ev(w)
        impact     = wr_with - wr_without
        results[factor] = {
            "n_with"    : len(w),
            "n_without" : len(wo),
            "wr_with"   : wr_with,
            "wr_without": wr_without,
            "ev_with"   : ev_with,
            "impact"    : round(impact, 1),
        }
    return dict(sorted(results.items(), key=lambda x: x[1]["impact"], reverse=True))

def analyze_factor_combinations(trades, min_n=15):
    from itertools import combinations
    combo_trades = defaultdict(list)
    for t in trades:
        factors = extract_factors(t.get("reasons", []))
        for combo in combinations(sorted(factors), 2):
            combo_trades[combo].append(t)
    results = []
    for combo, group in combo_trades.items():
        if len(group) < min_n: continue
        results.append({
            "factors": combo,
            "n"      : len(group),
            "wr"     : wr(group),
            "ev"     : ev(group),
            "ci95"   : ci95(group),
        })
    results.sort(key=lambda x: x["ev"], reverse=True)
    return results

def analyze_score_ranges(trades):
    by_score = defaultdict(list)
    for t in trades:
        by_score[t.get("confluence_score", 0)].append(t)
    results = {}
    for score in sorted(by_score.keys()):
        group = by_score[score]
        if len(group) < 3: continue
        results[score] = {"n": len(group), "wr": wr(group), "ev": ev(group), "ci95": ci95(group)}
    return results

def analyze_sl_patterns(trades):
    sl_trades  = [t for t in trades if is_loss(t)]
    tp2_trades = [t for t in trades if is_win(t)]
    if not sl_trades or not tp2_trades: return {}
    sl_fc  = defaultdict(int)
    tp2_fc = defaultdict(int)
    for t in sl_trades:
        for f in extract_factors(t.get("reasons", [])): sl_fc[f] += 1
    for t in tp2_trades:
        for f in extract_factors(t.get("reasons", [])): tp2_fc[f] += 1
    n_sl  = len(sl_trades)
    n_tp2 = len(tp2_trades)
    results = {}
    for factor in set(sl_fc.keys()) | set(tp2_fc.keys()):
        sl_pct  = sl_fc[factor]  / n_sl  * 100 if n_sl  > 0 else 0
        tp2_pct = tp2_fc[factor] / n_tp2 * 100 if n_tp2 > 0 else 0
        results[factor] = {
            "sl_pct" : round(sl_pct, 1),
            "tp2_pct": round(tp2_pct, 1),
            "sl_bias": round(sl_pct - tp2_pct, 1),
        }
    return dict(sorted(results.items(), key=lambda x: x[1]["sl_bias"], reverse=True))

def analyze_time_patterns(trades):
    by_hour = defaultdict(list)
    for t in trades:
        ts_str = t.get("scan_timestamp", "")
        if ts_str:
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("T", " ").split(".")[0])
                by_hour[ts.hour].append(t)
            except: pass
    results = {}
    for hour in sorted(by_hour.keys()):
        group = by_hour[hour]
        if len(group) < 5: continue
        results[hour] = {"n": len(group), "wr": wr(group), "ev": ev(group)}
    return results

def analyze_sl_duration(trades):
    sl_trades = [t for t in trades if is_loss(t)]
    if not sl_trades: return {}
    by_tf = defaultdict(list)
    for t in sl_trades:
        by_tf[t.get("tf", "?")].append(t["bars_to_outcome"])
    results = {}
    for tf, bars_list in by_tf.items():
        avg = sum(bars_list) / len(bars_list)
        results[tf] = {
            "count"   : len(bars_list),
            "avg_bars": round(avg, 1),
            "fast_sl" : sum(1 for b in bars_list if b <= 3),
            "slow_sl" : sum(1 for b in bars_list if b >= 20),
        }
    return results

def generate_adaptive_weights(factor_analysis, sl_patterns, score_analysis):
    weights = {}
    for factor, data in factor_analysis.items():
        sl_bias = sl_patterns.get(factor, {}).get("sl_bias", 0)
        weight  = 1.0
        weight += (data["impact"] / 5.0) * 0.1
        weight -= (sl_bias / 5.0) * 0.1
        if data["ev_with"] > 0.05:  weight += 0.2
        elif data["ev_with"] < -0.15: weight -= 0.2
        weight = max(0.3, min(2.0, round(weight, 2)))
        if data["n_with"] >= 10:
            weights[factor] = weight
    dead_zones  = [int(s) for s, d in score_analysis.items() if d["ev"] < -0.10 and d["n"] >= 20]
    sweet_spots = [int(s) for s, d in score_analysis.items() if d["ev"] > 0.05  and d["n"] >= 10]
    return {
        "factor_weights"   : weights,
        "dead_zone_scores" : dead_zones,
        "sweet_spot_scores": sweet_spots,
        "generated_at"     : datetime.now().isoformat(),
        "notes"            : "Weight > 1.0 = faktor prediktif. Weight < 1.0 = kurangi bobot.",
    }

SEP  = "-" * 70
SEP2 = "=" * 70

def print_score_analysis(sa):
    print(f"\n{SEP2}\n  SCORE RANGE ANALYSIS\n{SEP2}")
    print(f"\n  {'Score':>6} {'N':>6} {'WR':>7} {'+-CI95':>7} {'EV':>8}  Status")
    print(f"  {SEP}")
    for score, data in sa.items():
        flag = "SWEET SPOT" if data["ev"] > 0.05 else ("NETRAL" if data["ev"] > -0.05 else "DEAD ZONE")
        print(f"  {score:>6}  {data['n']:>5}  {data['wr']:>6.1f}%  +-{data['ci95']:>4.1f}%  {data['ev']:>+.3f}R  {flag}")

def print_factor_analysis(fa, sl, n_total):
    print(f"\n{SEP2}\n  FAKTOR ANALYSIS (n={n_total})\n{SEP2}")
    print(f"\n  {'Faktor':<22} {'N_with':>6} {'WR_with':>8} {'WR_wout':>8} {'Impact':>8} {'EV_with':>8} {'SL_bias':>8}")
    print(f"  {SEP}")
    for factor, data in fa.items():
        sl_bias = sl.get(factor, {}).get("sl_bias", 0)
        flag = " BAGUS" if (data["impact"] >= 5 and data["ev_with"] > 0) else (
               " HATI-HATI" if sl_bias >= 10 else (
               " BURUK" if data["ev_with"] < -0.15 else ""))
        print(f"  {factor:<22} {data['n_with']:>6} {data['wr_with']:>7.1f}%  "
              f"{data['wr_without']:>7.1f}%  {data['impact']:>+7.1f}%  "
              f"{data['ev_with']:>+.3f}  {sl_bias:>+7.1f}%{flag}")

def print_sl_patterns(sl):
    print(f"\n{SEP2}\n  SL PATTERN — faktor sering muncul saat LOSS\n{SEP2}")
    print(f"\n  {'Faktor':<22} {'% di SL':>8} {'% di TP2':>9} {'SL bias':>9}")
    print(f"  {SEP}")
    for factor, data in list(sl.items())[:15]:
        flag = " !!!" if data["sl_bias"] > 10 else ""
        print(f"  {factor:<22} {data['sl_pct']:>7.1f}%  {data['tp2_pct']:>8.1f}%  {data['sl_bias']:>+8.1f}%{flag}")

def print_combo_analysis(combos):
    print(f"\n{SEP2}\n  KOMBINASI FAKTOR\n{SEP2}")
    golden = [c for c in combos if c["ev"] > 0.05][:8]
    death  = [c for c in combos if c["ev"] < -0.15][:8]
    if golden:
        print(f"\n  GOLDEN COMBO (konsisten menang):")
        for c in golden:
            print(f"    {'+'.join(c['factors']):<45} n={c['n']} WR={c['wr']:.1f}% EV={c['ev']:+.3f}")
    if death:
        print(f"\n  DEATH COMBO (konsisten kalah):")
        for c in death:
            print(f"    {'+'.join(c['factors']):<45} n={c['n']} WR={c['wr']:.1f}% EV={c['ev']:+.3f}")

def print_time_analysis(ta):
    print(f"\n{SEP2}\n  TIME ANALYSIS — WR per jam UTC\n{SEP2}")
    print(f"\n  {'Jam':>5} {'N':>5} {'WR':>7} {'EV':>8}")
    print(f"  {SEP}")
    for hour, data in sorted(ta.items()):
        flag = " <- TERBAIK" if data["ev"] > 0.05 else (" <- HINDARI" if data["ev"] < -0.10 else "")
        print(f"  {hour:>4}h  {data['n']:>4}  {data['wr']:>6.1f}%  {data['ev']:>+.3f}R{flag}")

def print_sl_duration(sd):
    print(f"\n{SEP2}\n  SL DURATION\n{SEP2}")
    for tf, data in sd.items():
        pf = data["fast_sl"] / data["count"] * 100 if data["count"] > 0 else 0
        ps = data["slow_sl"] / data["count"] * 100 if data["count"] > 0 else 0
        print(f"\n  TF {tf}: total={data['count']} avg={data['avg_bars']} bars")
        print(f"    Cepat (<=3 bars): {data['fast_sl']} ({pf:.0f}%) — S&R mungkin salah")
        print(f"    Lambat (>=20 bars): {data['slow_sl']} ({ps:.0f}%) — SL terlalu sempit")

def print_adaptive_suggestion(weights):
    print(f"\n{SEP2}\n  SARAN ADAPTIVE WEIGHTS\n{SEP2}")
    dead  = weights.get("dead_zone_scores", [])
    sweet = weights.get("sweet_spot_scores", [])
    if dead:  print(f"\n  Dead zone scores (hindari): {dead}")
    if sweet: print(f"  Sweet spot scores: {sweet}")
    fw = weights.get("factor_weights", {})
    if fw:
        print(f"\n  Factor weights yang berubah:")
        for factor, w in sorted(fw.items(), key=lambda x: x[1], reverse=True):
            if abs(w - 1.0) >= 0.1:
                arrow = "tingkatkan" if w > 1.0 else "kurangi"
                print(f"    {factor:<25}: {w:.2f}  ({arrow})")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",  type=str, default=None)
    parser.add_argument("--all",   action="store_true")
    parser.add_argument("--min-n", type=int, default=10)
    args = parser.parse_args()

    if args.file:
        path = Path(args.file)
        if not path.exists(): path = RESULTS_DIR / args.file
        trades = load_trades_raw(path)
    elif args.all:
        trades, _ = load_all()
    else:
        trades, _ = load_latest()

    n = len(trades)
    if n < 10:
        print(f"Terlalu sedikit trades ({n})."); return

    wins   = sum(1 for t in trades if is_win(t))
    losses = sum(1 for t in trades if is_loss(t))
    print(f"\nTotal: {n} trades  TP2:{wins}  SL:{losses}  WR:{wr(trades):.1f}%  EV:{ev(trades):+.3f}R")

    print("Menganalisa... ", end="", flush=True)
    fa     = analyze_factors(trades);                    print("faktor", end=" ", flush=True)
    sl     = analyze_sl_patterns(trades);                print("sl_pattern", end=" ", flush=True)
    sa     = analyze_score_ranges(trades);               print("scores", end=" ", flush=True)
    combos = analyze_factor_combinations(trades, args.min_n); print("combos", end=" ", flush=True)
    ta     = analyze_time_patterns(trades);              print("time", end=" ", flush=True)
    sd     = analyze_sl_duration(trades);                print("duration OK")

    print_score_analysis(sa)
    print_factor_analysis(fa, sl, n)
    print_sl_patterns(sl)
    print_combo_analysis(combos)
    print_time_analysis(ta)
    print_sl_duration(sd)

    weights = generate_adaptive_weights(fa, sl, sa)
    print_adaptive_suggestion(weights)

    with open(WEIGHTS_FILE, "w") as f:
        json.dump(weights, f, indent=2)
    print(f"\n  Disimpan: {WEIGHTS_FILE}")
    print(f"  Selanjutnya: python backtesting/adaptive_trainer.py")
    print(f"\n{SEP2}\n")

if __name__ == "__main__":
    main()