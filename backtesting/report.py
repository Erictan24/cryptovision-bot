"""
report.py -- Stats computation dan formatted output.

Input : list[TradeResult]
Output: tabel win rate per quality, per TF, per coin, per direction.
        Juga simpan ke JSON untuk analisa lanjutan.
"""

import json
import math
from pathlib import Path
from collections import defaultdict
from dataclasses import asdict

from simulator import TradeResult, OUTCOME_TP2, OUTCOME_TP1, OUTCOME_SL, OUTCOME_EXPIRED

RESULTS_DIR = Path(__file__).parent / "results"


# ============================================================
# STATS CALCULATION
# ============================================================

def _stats(trades: list[TradeResult]) -> dict:
    """Hitung statistik untuk satu group trade."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr_tp2": 0, "wr_tp1": 0, "avg_pnl_r": 0,
                "exp_val": 0, "avg_bars": 0, "avg_rr2": 0,
                "tp2": 0, "tp1": 0, "sl": 0, "exp": 0}

    tp2  = sum(1 for t in trades if t.outcome == OUTCOME_TP2)
    tp1  = sum(1 for t in trades if t.outcome == OUTCOME_TP1)
    sl   = sum(1 for t in trades if t.outcome == OUTCOME_SL)
    exp  = sum(1 for t in trades if t.outcome == OUTCOME_EXPIRED)

    wr_tp2 = tp2 / n * 100
    wr_tp1 = (tp2 + tp1) / n * 100
    avg_pnl = sum(t.pnl_r for t in trades) / n
    avg_bars = sum(t.bars_to_outcome for t in trades) / n
    avg_rr2  = sum(t.rr2 for t in trades) / n

    # Expected value: E = WR_TP2 * avg_RR2 - (1-WR_TP2) * 1
    wr2_frac = wr_tp2 / 100
    ev = wr2_frac * avg_rr2 - (1 - wr2_frac) * 1.0

    return {
        "n"       : n,
        "wr_tp2"  : round(wr_tp2, 1),
        "wr_tp1"  : round(wr_tp1, 1),
        "avg_pnl_r": round(avg_pnl, 2),
        "exp_val" : round(ev, 2),
        "avg_bars": round(avg_bars, 1),
        "avg_rr2" : round(avg_rr2, 2),
        "tp2"     : tp2,
        "tp1"     : tp1,
        "sl"      : sl,
        "exp"     : exp,
    }


def _ci95(wr_pct: float, n: int) -> float:
    """Wilson 95% confidence interval half-width."""
    if n == 0: return 0
    p = wr_pct / 100
    z = 1.96
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2*n)) / denom
    hw = z * math.sqrt(p*(1-p)/n + z**2/(4*n**2)) / denom
    return round(hw * 100, 1)


# ============================================================
# MAIN REPORT
# ============================================================

def compute_report(trades: list[TradeResult]) -> dict:
    """
    Hitung semua breakdown statistics.
    """
    report = {}

    # --- Overall ---
    report["overall"] = _stats(trades)

    # --- Per quality ---
    by_quality = defaultdict(list)
    for t in trades:
        by_quality[t.quality].append(t)

    report["by_quality"] = {}
    for q in ["IDEAL", "GOOD", "MODERATE", "WAIT"]:
        if q in by_quality:
            s = _stats(by_quality[q])
            s["ci95"] = _ci95(s["wr_tp2"], s["n"])
            report["by_quality"][q] = s

    # --- Per TF ---
    by_tf = defaultdict(list)
    for t in trades:
        by_tf[t.tf].append(t)

    report["by_tf"] = {tf: _stats(ts) for tf, ts in sorted(by_tf.items())}

    # --- Per direction ---
    by_dir = defaultdict(list)
    for t in trades:
        by_dir[t.direction].append(t)

    report["by_direction"] = {d: _stats(ts) for d, ts in by_dir.items()}

    # --- Per coin (top 10 by count) ---
    by_coin = defaultdict(list)
    for t in trades:
        by_coin[t.symbol].append(t)

    report["by_coin"] = {
        sym: _stats(ts)
        for sym, ts in sorted(by_coin.items(), key=lambda x: len(x[1]), reverse=True)
    }

    # --- Quality x TF matrix ---
    matrix = {}
    for q in ["IDEAL", "GOOD", "MODERATE"]:
        matrix[q] = {}
        for tf in ["15m", "1h", "4h"]:
            group = [t for t in trades if t.quality == q and t.tf == tf]
            matrix[q][tf] = _stats(group)
    report["quality_tf_matrix"] = matrix

    # --- Filter effectiveness ---
    # WR por confluence score bucket
    by_score = defaultdict(list)
    for t in trades:
        bucket = (t.confluence_score // 3) * 3  # buckets of 3
        by_score[bucket].append(t)

    report["by_conf_score"] = {
        str(b): _stats(ts)
        for b, ts in sorted(by_score.items())
    }

    # WR dengan / tanpa kills
    no_kills  = [t for t in trades if t.kill_count == 0]
    has_kills = [t for t in trades if t.kill_count > 0]
    report["kills_impact"] = {
        "no_kills" : _stats(no_kills),
        "has_kills": _stats(has_kills),
    }

    return report


# ============================================================
# PRINT REPORT
# ============================================================

def print_report(report: dict, trades: list[TradeResult]):
    SEP  = "-" * 70
    SEP2 = "=" * 70

    print(f"\n{SEP2}")
    print(f"  BACKTEST RESULTS -- {len(trades)} signals total")
    print(SEP2)

    overall = report["overall"]
    print(f"\n  Overall WR (TP2): {overall['wr_tp2']:.1f}%  |  WR (TP1+): {overall['wr_tp1']:.1f}%")
    print(f"  Avg P&L (R):      {overall['avg_pnl_r']:+.2f}  |  Expected value: {overall['exp_val']:+.2f}R/trade")
    print(f"  TP2:{overall['tp2']}  TP1:{overall['tp1']}  SL:{overall['sl']}  Expired:{overall['exp']}")

    # --- Quality breakdown ---
    print(f"\n{SEP}")
    print(f"  {'QUALITY':<10} {'N':>5} {'WR_TP2':>8} {'+-CI95':>7} {'WR_TP1+':>8} {'EV':>7} {'Avg RR2':>8}")
    print(SEP)
    for q in ["IDEAL", "GOOD", "MODERATE", "WAIT"]:
        if q not in report["by_quality"]:
            continue
        s = report["by_quality"][q]
        ci = s.get("ci95", 0)
        verdict = ""
        if q != "WAIT":
            if s["wr_tp2"] >= 60:  verdict = "TARGET HIT"
            elif s["wr_tp2"] >= 50: verdict = "~ OK"
            else:                    verdict = "BELOW TARGET"
        print(f"  {q:<10} {s['n']:>5} {s['wr_tp2']:>7.1f}% {f'+-{ci}%':>7} "
              f"{s['wr_tp1']:>7.1f}% {s['exp_val']:>+7.2f}  {s['avg_rr2']:>7.2f}  {verdict}")

    # --- TF breakdown ---
    print(f"\n{SEP}")
    print(f"  {'TF':<8} {'N':>5} {'WR_TP2':>8} {'WR_TP1+':>8} {'EV':>7} {'Avg bars':>10}")
    print(SEP)
    for tf, s in report["by_tf"].items():
        print(f"  {tf:<8} {s['n']:>5} {s['wr_tp2']:>7.1f}% {s['wr_tp1']:>7.1f}% "
              f"{s['exp_val']:>+7.2f}  {s['avg_bars']:>9.1f}")

    # --- Direction breakdown ---
    print(f"\n{SEP}")
    print(f"  {'DIRECTION':<10} {'N':>5} {'WR_TP2':>8} {'WR_TP1+':>8} {'EV':>7}")
    print(SEP)
    for d, s in report["by_direction"].items():
        print(f"  {d:<10} {s['n']:>5} {s['wr_tp2']:>7.1f}% {s['wr_tp1']:>7.1f}% {s['exp_val']:>+7.2f}")

    # --- Quality x TF matrix ---
    print(f"\n{SEP}")
    print(f"  WIN RATE (TP2) MATRIX -- Quality x Timeframe")
    print(SEP)
    header = f"  {'':>10}"
    for tf in ["15m", "1h", "4h"]:
        header += f"  {tf:>8}"
    print(header)
    for q in ["IDEAL", "GOOD", "MODERATE"]:
        row = f"  {q:<10}"
        for tf in ["15m", "1h", "4h"]:
            s = report["quality_tf_matrix"][q][tf]
            if s["n"] == 0:
                row += f"  {'  n/a':>8}"
            else:
                row += f"  {s['wr_tp2']:>6.1f}% ({s['n']})"
        print(row)

    # --- Confluence score breakdown ---
    print(f"\n{SEP}")
    print(f"  CONFLUENCE SCORE vs WIN RATE")
    print(SEP)
    print(f"  {'Score':>8} {'N':>5} {'WR_TP2':>8} {'EV':>7}")
    for bucket, s in sorted(report["by_conf_score"].items(), key=lambda x: int(x[0])):
        if s["n"] >= 3:
            print(f"  {bucket:>8} {s['n']:>5} {s['wr_tp2']:>7.1f}% {s['exp_val']:>+7.2f}")

    # --- Kill factors impact ---
    print(f"\n{SEP}")
    nk = report["kills_impact"]["no_kills"]
    hk = report["kills_impact"]["has_kills"]
    print(f"  Kill factors impact:")
    print(f"  No kills  (n={nk['n']}):  WR {nk['wr_tp2']:.1f}%  EV {nk['exp_val']:+.2f}")
    print(f"  Has kills (n={hk['n']}):  WR {hk['wr_tp2']:.1f}%  EV {hk['exp_val']:+.2f}")

    # --- Top/Bottom coins ---
    by_coin = report["by_coin"]
    coins_with_data = [(sym, s) for sym, s in by_coin.items() if s["n"] >= 5]
    if coins_with_data:
        top    = sorted(coins_with_data, key=lambda x: x[1]["wr_tp2"], reverse=True)[:5]
        bottom = sorted(coins_with_data, key=lambda x: x[1]["wr_tp2"])[:5]

        print(f"\n{SEP}")
        print(f"  TOP 5 COINS BY WIN RATE:")
        for sym, s in top:
            print(f"    {sym:<8} WR:{s['wr_tp2']:>5.1f}%  n={s['n']}")
        print(f"\n  BOTTOM 5 COINS BY WIN RATE:")
        for sym, s in bottom:
            print(f"    {sym:<8} WR:{s['wr_tp2']:>5.1f}%  n={s['n']}")

    print(f"\n{SEP2}\n")


# ============================================================
# SAVE / LOAD
# ============================================================

def save_results(trades: list[TradeResult], report: dict,
                 label: str = "default"):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M")

    # Trades as JSON
    trades_path = RESULTS_DIR / f"trades_{label}_{ts}.json"
    with open(trades_path, "w") as f:
        json.dump(
            [asdict(t) | {"scan_timestamp": str(t.scan_timestamp)}
             for t in trades],
            f, indent=2, default=str
        )

    # Report as JSON
    report_path = RESULTS_DIR / f"report_{label}_{ts}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"Saved: {trades_path}")
    print(f"Saved: {report_path}")
    return trades_path, report_path


def load_trades(path) -> list[TradeResult]:
    with open(path) as f:
        raw = json.load(f)
    trades = []
    for r in raw:
        r["scan_timestamp"] = __import__("pandas").Timestamp(r["scan_timestamp"])
        r.pop("is_win_tp2", None); r.pop("is_win_tp1", None)
        r.pop("is_loss", None);    r.pop("is_expired", None)
        trades.append(TradeResult(**r))
    return trades
