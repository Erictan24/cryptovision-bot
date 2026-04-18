"""
sl_postmortem.py — Analisis mendalam mengapa sinyal kena SL.

Menggabungkan SEMUA data backtest historis dan mencari pola:
  1. Faktor apa yang sering muncul saat SL kena?
  2. Berapa cepat SL kena? (wick = < 3 bar, drift = 4-20 bar, trend = > 20 bar)
  3. Score range mana yang paling sering kalah?
  4. Kombinasi faktor apa yang menjadi "jebakan"?
  5. Session mana yang berkontribusi paling banyak ke SL?

Output: data/sl_patterns.json — dipakai signal_generator untuk blokir faktor jebakan
Usage:
  python backtesting/sl_postmortem.py          # analisa + tampilkan
  python backtesting/sl_postmortem.py --apply  # simpan ke data/sl_patterns.json
"""

import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

RESULTS_DIR    = Path(__file__).parent / "results"
OUTPUT_FILE    = Path(__file__).parent.parent / "data" / "sl_patterns.json"
MIN_SAMPLE     = 20   # minimum trades untuk judgment yang reliabel
MIN_SL_BIAS    = 15   # perbedaan % muncul di SL vs TP — dianggap "jebakan"

# ─── Keyword untuk ekstrak faktor dari reasons ───────────────────────────────
FACTOR_PATTERNS = {
    "ema_bullish"       : ["EMA bullish", "EMA strong"],
    "ema_bearish"       : ["EMA bearish"],
    "ema_slight"        : ["EMA slight"],
    "bos"               : ["BOS"],
    "choch"             : ["CHoCH"],
    "htf_bos"           : ["HTF BOS"],
    "htf_choch"         : ["HTF CHoCH"],
    "sr_very_strong"    : ["Sangat Kuat", "Very Strong"],
    "sr_strong"         : ["Kuat", "Strong", "Order Block"],
    "fresh_level"       : ["Fresh level", "fresh"],
    "rsi_oversold"      : ["RSI oversold", "RSI overbought"],
    "rsi_div"           : ["RSI divergence", "divergence"],
    "adx_strong"        : ["ADX kuat", "ADX strong"],
    "adx_trending"      : ["ADX trending"],
    "discount_zone"     : ["Discount zone"],
    "premium_zone"      : ["Premium zone"],
    "htf_ema"           : ["HTF EMA"],
    "fvg"               : ["FVG"],
    "liquidity"         : ["Liquidity", "Liq Pool"],
    "candle_pattern"    : ["Candle pattern", "Hammer", "Engulfing", "Pin Bar"],
    "vol_confirm"       : ["Volume confirm", "Volume spike"],
    "order_flow"        : ["Order Flow", "Whale"],
    "london_session"    : ["LONDON"],
    "ny_session"        : ["NY", "New York"],
    "overlap_session"   : ["OVERLAP"],
    "asia_session"      : ["ASIA"],
    "absorption"        : ["absorption", "menyerap"],
    "vol_delta"         : ["Delta beli", "Delta jual"],
}


def load_all_trades():
    """Muat semua file trades dari backtesting/results/."""
    files  = sorted(RESULTS_DIR.glob("trades_*.json"))
    trades = []
    for f in files:
        try:
            with open(f) as fh:
                batch = json.load(fh)
            if isinstance(batch, list):
                trades.extend(batch)
        except Exception:
            continue
    return trades


def extract_factors(reasons: list) -> set:
    """Ekstrak faktor dari list reasons."""
    factors = set()
    reasons_lower = " ".join(str(r) for r in reasons).lower()
    for factor, keywords in FACTOR_PATTERNS.items():
        for kw in keywords:
            if kw.lower() in reasons_lower:
                factors.add(factor)
                break
    return factors


def get_sl_speed(bars: int, tf: str) -> str:
    """Klasifikasi kecepatan SL."""
    if bars <= 3:
        return "wick"      # kemungkinan zona S/R salah atau SL terlalu dekat
    if bars <= 15:
        return "drift"     # market bergerak perlahan melawan kita
    return "trend"         # reversal penuh / trend baru


def analyze(trades: list) -> dict:
    """Analisis mendalam semua trades dan identifikasi pola SL."""

    sl_trades  = [t for t in trades if t.get("outcome") == "SL_HIT"]
    tp_trades  = [t for t in trades if t.get("outcome") in ("TP1_HIT", "TP2_HIT")]
    tp2_trades = [t for t in trades if t.get("outcome") == "TP2_HIT"]

    n_total = len(trades)
    n_sl    = len(sl_trades)
    n_tp    = len(tp_trades)
    n_tp2   = len(tp2_trades)

    if n_total < MIN_SAMPLE:
        return {"error": f"Terlalu sedikit data ({n_total} trades)"}

    # ── 1. Faktor jebakan — lebih sering di SL dari TP ───────────────────────
    sl_factors  = defaultdict(int)
    tp_factors  = defaultdict(int)
    for t in sl_trades:
        for f in extract_factors(t.get("reasons", [])):
            sl_factors[f] += 1
    for t in tp_trades:
        for f in extract_factors(t.get("reasons", [])):
            tp_factors[f] += 1

    trap_factors  = {}
    boost_factors = {}
    for factor in set(sl_factors) | set(tp_factors):
        sl_pct = sl_factors.get(factor, 0) / max(n_sl, 1) * 100
        tp_pct = tp_factors.get(factor, 0) / max(n_tp, 1) * 100
        bias   = sl_pct - tp_pct
        n_with = sl_factors.get(factor, 0) + tp_factors.get(factor, 0)
        if n_with < 10:
            continue
        if bias >= MIN_SL_BIAS:
            trap_factors[factor] = {
                "sl_pct": round(sl_pct, 1),
                "tp_pct": round(tp_pct, 1),
                "sl_bias": round(bias, 1),
                "n": n_with,
            }
        elif bias <= -MIN_SL_BIAS:
            boost_factors[factor] = {
                "sl_pct": round(sl_pct, 1),
                "tp_pct": round(tp_pct, 1),
                "boost": round(-bias, 1),
                "n": n_with,
            }

    # ── 2. Kecepatan SL — tipe SL ─────────────────────────────────────────────
    sl_speed_count = defaultdict(int)
    for t in sl_trades:
        speed = get_sl_speed(t.get("bars_to_outcome", 10), t.get("tf", "1h"))
        sl_speed_count[speed] += 1

    # ── 3. WR per score range ─────────────────────────────────────────────────
    by_score = defaultdict(lambda: {"sl": 0, "tp": 0})
    for t in trades:
        score  = t.get("confluence_score", 0)
        bucket = (score // 2) * 2   # bucket per 2 poin
        if t.get("outcome") == "SL_HIT":
            by_score[bucket]["sl"] += 1
        elif t.get("outcome") in ("TP1_HIT", "TP2_HIT"):
            by_score[bucket]["tp"] += 1

    score_wr = {}
    for bucket, data in sorted(by_score.items()):
        total = data["sl"] + data["tp"]
        if total < 5:
            continue
        wr  = data["tp"] / total * 100
        score_wr[bucket] = {"wr": round(wr, 1), "n": total, "sl": data["sl"], "tp": data["tp"]}

    # Identifikasi dead zones (score range dengan WR < 35%)
    dead_score_ranges = [b for b, d in score_wr.items()
                         if d["wr"] < 35 and d["n"] >= MIN_SAMPLE]

    # ── 4. WR per kill count ──────────────────────────────────────────────────
    by_kills = defaultdict(lambda: {"sl": 0, "tp": 0})
    for t in trades:
        k = t.get("kill_count", 0)
        if t.get("outcome") == "SL_HIT":
            by_kills[k]["sl"] += 1
        elif t.get("outcome") in ("TP1_HIT", "TP2_HIT"):
            by_kills[k]["tp"] += 1

    kill_wr = {}
    for k, data in sorted(by_kills.items()):
        total = data["sl"] + data["tp"]
        if total < 5:
            continue
        wr = data["tp"] / total * 100
        kill_wr[k] = {"wr": round(wr, 1), "n": total}

    # Kill count yang harus diblokir (WR < 25%)
    bad_kill_counts = [k for k, d in kill_wr.items()
                       if d["wr"] < 25 and d["n"] >= MIN_SAMPLE]

    # ── 5. WR per session ─────────────────────────────────────────────────────
    session_kw = {
        "LONDON": ["LONDON"],
        "NY"    : ["NY", "New York", "16-21"],
        "ASIA"  : ["ASIA", "21-02"],
        "DEAD"  : ["DEAD", "02-07"],
        "OVERLAP": ["OVERLAP", "13-16"],
    }

    by_session = defaultdict(lambda: {"sl": 0, "tp": 0})
    for t in trades:
        reasons_str = " ".join(str(r) for r in t.get("reasons", []))
        matched = "UNKNOWN"
        for ses, kws in session_kw.items():
            if any(kw in reasons_str for kw in kws):
                matched = ses
                break
        if t.get("outcome") == "SL_HIT":
            by_session[matched]["sl"] += 1
        elif t.get("outcome") in ("TP1_HIT", "TP2_HIT"):
            by_session[matched]["tp"] += 1

    session_wr = {}
    for ses, data in by_session.items():
        total = data["sl"] + data["tp"]
        if total < 5:
            continue
        wr = data["tp"] / total * 100
        session_wr[ses] = {"wr": round(wr, 1), "n": total}

    # ── 6. Ringkasan + Rekomendasi ────────────────────────────────────────────
    wick_pct = sl_speed_count.get("wick", 0) / max(n_sl, 1) * 100

    recommendations = []

    if wick_pct > 40:
        recommendations.append(
            f"SL tipe WICK {wick_pct:.0f}% — SL terlalu sempit. "
            f"Naikkan sl_atr_buffer dari 1.3 ke 1.5"
        )

    for factor, data in sorted(trap_factors.items(), key=lambda x: -x[1]["sl_bias"])[:5]:
        recommendations.append(
            f"Faktor jebakan '{factor}': muncul {data['sl_pct']:.0f}% di SL vs "
            f"{data['tp_pct']:.0f}% di TP (bias +{data['sl_bias']:.0f}%). "
            f"Pertimbangkan untuk kurangi bobotnya."
        )

    if dead_score_ranges:
        recommendations.append(
            f"Dead zone scores: {sorted(dead_score_ranges)} — "
            f"naikkan score_good agar range ini tidak masuk GOOD."
        )

    if bad_kill_counts:
        recommendations.append(
            f"Kill count {bad_kill_counts} WR < 25% — "
            f"turunkan max_kills_good untuk menghindari."
        )

    return {
        "generated_at"     : datetime.now().isoformat(),
        "total_trades"     : n_total,
        "sl_trades"        : n_sl,
        "tp_trades"        : n_tp,
        "tp2_trades"       : n_tp2,
        "overall_wr"       : round(n_tp / n_total * 100, 1) if n_total > 0 else 0,
        "sl_speed"         : {k: {"count": v, "pct": round(v/max(n_sl,1)*100,1)}
                              for k, v in sl_speed_count.items()},
        "trap_factors"     : trap_factors,
        "boost_factors"    : boost_factors,
        "score_wr"         : score_wr,
        "dead_score_ranges": dead_score_ranges,
        "kill_wr"          : {str(k): v for k, v in kill_wr.items()},
        "bad_kill_counts"  : bad_kill_counts,
        "session_wr"       : session_wr,
        "recommendations"  : recommendations,
    }


def print_report(result: dict):
    SEP  = "-" * 68
    SEP2 = "=" * 68

    print(f"\n{SEP2}")
    print(f"  SL POST-MORTEM ANALYSIS")
    print(f"  Generated: {result.get('generated_at','?')[:16]}")
    print(f"{SEP2}")
    print(f"\n  Total trades  : {result['total_trades']}")
    print(f"  SL hit        : {result['sl_trades']} ({result['sl_trades']/max(result['total_trades'],1)*100:.1f}%)")
    print(f"  TP hit        : {result['tp_trades']}")
    print(f"  WR overall    : {result['overall_wr']}%")

    print(f"\n{SEP}")
    print(f"  KECEPATAN SL")
    print(f"{SEP}")
    for speed, data in result.get("sl_speed", {}).items():
        label = {"wick": "<=3 bar (SL kena wick)", "drift": "4-15 bar (market drift)", "trend": ">15 bar (reversal penuh)"}.get(speed, speed)
        print(f"  {label:<35} : {data['count']:>4} ({data['pct']:.1f}%)")

    print(f"\n{SEP}")
    print(f"  FAKTOR JEBAKAN (sering muncul di SL, jarang di TP)")
    print(f"{SEP}")
    for factor, data in sorted(result.get("trap_factors", {}).items(),
                                key=lambda x: -x[1]["sl_bias"])[:10]:
        print(f"  {factor:<22} SL:{data['sl_pct']:>5.1f}%  TP:{data['tp_pct']:>5.1f}%  bias:{data['sl_bias']:>+5.1f}%  n={data['n']}")

    print(f"\n{SEP}")
    print(f"  FAKTOR PENDORONG (sering muncul di TP, jarang di SL)")
    print(f"{SEP}")
    for factor, data in sorted(result.get("boost_factors", {}).items(),
                                key=lambda x: -x[1]["boost"])[:10]:
        print(f"  {factor:<22} TP:{data['tp_pct']:>5.1f}%  SL:{data['sl_pct']:>5.1f}%  boost:{data['boost']:>+5.1f}%  n={data['n']}")

    print(f"\n{SEP}")
    print(f"  WR PER SCORE RANGE")
    print(f"{SEP}")
    for score, data in sorted(result.get("score_wr", {}).items()):
        flag = " [DEAD ZONE]" if score in result.get("dead_score_ranges", []) else \
               " [BAGUS]" if data["wr"] >= 60 else ""
        print(f"  Score {score:>3}-{score+1:<3} : WR {data['wr']:>5.1f}%  n={data['n']:>4}{flag}")

    print(f"\n{SEP}")
    print(f"  WR PER KILL COUNT")
    print(f"{SEP}")
    for k, data in sorted(result.get("kill_wr", {}).items()):
        flag = " [BLOKIR]" if int(k) in result.get("bad_kill_counts", []) else ""
        print(f"  {k} kill(s)   : WR {data['wr']:>5.1f}%  n={data['n']:>4}{flag}")

    print(f"\n{SEP}")
    print(f"  WR PER SESI")
    print(f"{SEP}")
    for ses, data in sorted(result.get("session_wr", {}).items(),
                             key=lambda x: -x[1]["wr"]):
        print(f"  {ses:<10} : WR {data['wr']:>5.1f}%  n={data['n']:>4}")

    print(f"\n{SEP2}")
    print(f"  REKOMENDASI")
    print(f"{SEP2}")
    for i, rec in enumerate(result.get("recommendations", []), 1):
        print(f"\n  {i}. {rec}")

    if not result.get("recommendations"):
        print(f"\n  Tidak ada rekomendasi — bot sudah cukup baik!")

    print(f"\n{SEP2}\n")


def run_and_save(apply: bool = False) -> dict:
    """Load data, analisa, dan optionally simpan ke data/sl_patterns.json."""
    trades = load_all_trades()
    if not trades:
        print("Tidak ada data trades ditemukan di backtesting/results/")
        return {}

    print(f"Memuat {len(trades)} trades dari {len(list(RESULTS_DIR.glob('trades_*.json')))} file...")
    result = analyze(trades)

    if "error" in result:
        print(f"Error: {result['error']}")
        return result

    print_report(result)

    if apply:
        OUTPUT_FILE.parent.mkdir(exist_ok=True)
        with open(OUTPUT_FILE, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Disimpan ke: {OUTPUT_FILE}")
        print("signal_generator.py akan otomatis membaca file ini saat berikutnya bot jalan.")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Simpan hasil ke data/sl_patterns.json")
    args = parser.parse_args()
    run_and_save(apply=args.apply)
