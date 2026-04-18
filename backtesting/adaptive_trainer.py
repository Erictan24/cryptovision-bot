"""
adaptive_trainer.py — Terapkan adaptive weights ke config.
Usage:
  python backtesting/adaptive_trainer.py           -- lihat saran
  python backtesting/adaptive_trainer.py --apply   -- apply ke config.py
"""

import sys
import json
import re
import shutil
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

WEIGHTS_FILE = Path(__file__).parent / "adaptive_weights.json"
RESULTS_DIR  = Path(__file__).parent / "results"
CONFIG_FILE  = Path(__file__).parent.parent / "config.py"

FACTOR_TO_SCORE_PARAM = {
    "ema_bullish"        : "score_ema_strong",
    "ema_bearish"        : "score_ema_strong",
    "ema_slight"         : "score_ema_slight",
    "bos"                : "score_bos",
    "choch"              : "score_choch",
    "htf_bos"            : "score_htf_bos",
    "htf_choch"          : "score_htf_choch",
    "sr_very_strong"     : "score_sr_very_strong",
    "sr_strong"          : "score_sr_strong",
    "fresh_level"        : "score_fresh_level",
    "htf_mtf_level"      : "score_htf_mtf_level",
    "phase_markup"       : "score_market_phase",
    "phase_distribution" : "score_market_phase",
    "phase_accumulation" : "score_market_phase",
    "orderflow_bull"     : "score_order_flow",
    "orderflow_bear"     : "score_order_flow",
    "rsi_oversold"       : "score_rsi_oversold",
    "rsi_overbought"     : "score_rsi_oversold",
    "rsi_divergence"     : "score_rsi_div",
    "hidden_divergence"  : "hidden_div",
    "vol_divergence"     : "score_vol_div",
    "adx_strong"         : "score_adx_strong",
    "adx_trending"       : "score_adx_trending",
    "discount_zone"      : "score_pd_zone",
    "premium_zone"       : "score_pd_zone",
    "htf_ema_bull"       : "score_htf_ema",
    "htf_ema_bear"       : "score_htf_ema",
    "fvg"                : "score_fvg",
    "liquidity"          : "score_liq_zones",
    "candle_pattern"     : "score_candle_pattern",
    "order_block"        : "score_sr_strong",
}

DEFAULT_SCORES = {
    "score_ema_strong"       : 2,
    "score_ema_slight"       : 1,
    "score_structure"        : 2,
    "score_sr_very_strong"   : 4,
    "score_sr_strong"        : 3,
    "score_sr_weak"          : 1,
    "score_fresh_level"      : 2,
    "score_htf_mtf_level"    : 2,
    "score_bos"              : 2,
    "score_choch"            : 3,
    "score_htf_bos"          : 3,
    "score_htf_choch"        : 4,
    "score_market_phase"     : 2,
    "score_market_phase_sub" : 1,
    "score_order_flow"       : 2,
    "score_pd_zone"          : 2,
    "score_pd_zone_slight"   : 1,
    "score_vol_div"          : 2,
    "score_vol_confirm"      : 1,
    "score_rsi_very_oversold": 3,
    "score_rsi_oversold"     : 1,
    "score_htf_ema"          : 2,
    "score_rsi_div"          : 3,
    "score_hidden_div"       : 2,
    "score_adx_strong"       : 2,
    "score_adx_trending"     : 1,
    "score_fvg"              : 1,
    "score_liq_zones"        : 1,
    "score_candle_pattern"   : 3,
}

def load_weights():
    if not WEIGHTS_FILE.exists():
        print("ERROR: adaptive_weights.json tidak ditemukan.")
        print("Jalankan dulu: python backtesting/analyzer.py")
        sys.exit(1)
    with open(WEIGHTS_FILE) as f:
        return json.load(f)

def compute_adjusted_scores(weights):
    fw = weights.get("factor_weights", {})
    param_weights = {}
    for factor, weight in fw.items():
        param = FACTOR_TO_SCORE_PARAM.get(factor)
        if param:
            param_weights.setdefault(param, []).append(weight)
    new_scores = {}
    for param, default in DEFAULT_SCORES.items():
        if param in param_weights:
            avg_w = sum(param_weights[param]) / len(param_weights[param])
            new_scores[param] = max(1, round(default * avg_w))
        else:
            new_scores[param] = default
    return new_scores

def print_comparison(old, new):
    print(f"\n{'='*60}\n  PERBANDINGAN SCORE: Default vs Adaptive\n{'='*60}")
    print(f"\n  {'Parameter':<28} {'Default':>8} {'Adaptive':>9} {'Ubah':>8}")
    print(f"  {'-'*58}")
    changed = []
    for param in sorted(old.keys()):
        old_v = old[param]
        new_v = new.get(param, old_v)
        diff  = new_v - old_v
        flag  = f"  +{diff}" if diff > 0 else (f"  {diff}" if diff < 0 else "")
        if diff != 0: changed.append((param, old_v, new_v))
        print(f"  {param:<28} {old_v:>8} {new_v:>9}{flag}")
    if changed:
        print(f"\n  {len(changed)} parameter berubah:")
        for p, o, n in changed:
            print(f"    {p}: {o} -> {n}")
    else:
        print(f"\n  Tidak ada perubahan.")

def apply_to_config(new_scores, weights):
    if not CONFIG_FILE.exists():
        print("ERROR: config.py tidak ditemukan."); return
    backup = CONFIG_FILE.parent / f"config_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.py"
    shutil.copy(CONFIG_FILE, backup)
    print(f"Backup: {backup.name}")
    with open(CONFIG_FILE) as f:
        content = f.read()
    changed = 0
    for param, new_val in new_scores.items():
        pattern = rf"('{param}'\s*:\s*)\d+"
        new_content, n = re.subn(pattern, f"\\g<1>{new_val}", content)
        if n > 0:
            content = new_content
            changed += 1
    with open(CONFIG_FILE, "w") as f:
        f.write(content)
    print(f"\nconfig.py diupdate: {changed} parameter berubah.")
    dead = weights.get("dead_zone_scores", [])
    if dead:
        print(f"Dead zone scores yang diidentifikasi: {dead}")
        print("Pertimbangkan untuk turunkan score_ideal agar dead zones masuk kategori WAIT.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    weights = load_weights()
    print(f"\nWeights dari: {weights.get('generated_at','?')[:16]}")
    print(f"Dead zones  : {weights.get('dead_zone_scores', [])}")
    print(f"Sweet spots : {weights.get('sweet_spot_scores', [])}")
    print(f"Factors     : {len(weights.get('factor_weights', {}))} custom weights")

    new_scores = compute_adjusted_scores(weights)
    print_comparison(DEFAULT_SCORES, new_scores)

    if args.apply:
        confirm = input("\nApply ke config.py? (y/n): ").strip().lower()
        if confirm == "y":
            apply_to_config(new_scores, weights)
            print("\nSelesai. Backtest ulang:")
            print("  python backtesting/run_backtest.py --no-fetch --tf 15m 1h")
        else:
            print("Dibatalkan.")
    else:
        print("\nUntuk apply: python backtesting/adaptive_trainer.py --apply")

if __name__ == "__main__":
    main()