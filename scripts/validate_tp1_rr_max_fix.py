"""Validate fix tp1_rr_max 2.0 -> 1.5.

Run SWING backtest only (SCALP engine gak pakai config ini),
pakai cache existing dari top 200 coin.
Compare hasil ke baseline.
"""
import os
import sys
import time
import pickle
import logging
from dataclasses import asdict
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(message)s')
logger = logging.getLogger(__name__)

CACHE_FILE = 'backtesting/cache/unified_data.pkl'
BASELINE_RESULT = 'backtesting/results/unified_top200_20260513_2036.pkl'


def main():
    if not os.path.exists(CACHE_FILE):
        logger.error(f"Cache not found: {CACHE_FILE}")
        return 1

    with open(CACHE_FILE, 'rb') as f:
        data = pickle.load(f)
    logger.info(f"Loaded cache: {len(data)} entries")

    coins = data.get('_meta', {}).get('coins', [])
    coins = coins[:200]
    logger.info(f"Using top {len(coins)} coins")

    # Confirm config aktual
    from config import SIGNAL_PARAMS
    logger.info(f"SIGNAL_PARAMS tp1_rr_min={SIGNAL_PARAMS['tp1_rr_min']}, "
                f"tp1_rr_max={SIGNAL_PARAMS['tp1_rr_max']}")

    # Run SWING only
    from backtest_unified import run_swing_backtest, print_unified_report

    logger.info("=" * 70)
    logger.info(" RUNNING SWING ENGINE (with tp1_rr_max fix)")
    logger.info("=" * 70)
    start = time.time()
    swing_trades = run_swing_backtest(coins, data, verbose=False)
    elapsed = time.time() - start
    logger.info(f"SWING: {len(swing_trades)} trades ({elapsed/60:.1f} min)")

    # Save
    os.makedirs('backtesting/results', exist_ok=True)
    ts_label = datetime.now().strftime('%Y%m%d_%H%M')
    result_file = f'backtesting/results/swing_tp1max_1p5_{ts_label}.pkl'
    with open(result_file, 'wb') as f:
        pickle.dump({
            'trades': [asdict(t) for t in swing_trades],
            'coins': coins,
            'days': 90,
            'timestamp': ts_label,
            'config': dict(SIGNAL_PARAMS),
        }, f)
    logger.info(f"Saved: {result_file}")

    # Print SWING-only report
    print_unified_report(swing_trades, days=90)

    # Compare vs baseline
    print()
    print("=" * 75)
    print(" COMPARISON vs BASELINE (tp1_rr_max=2.0)")
    print("=" * 75)
    with open(BASELINE_RESULT, 'rb') as f:
        baseline = pickle.load(f)
    baseline_swing = [t for t in baseline['trades'] if t.get('engine', '').upper() == 'SWING']

    def stats(t_list):
        n = len(t_list)
        if isinstance(t_list[0], dict):
            wins = sum(1 for t in t_list if t.get('pnl_r', 0) > 0)
            total = sum(t.get('pnl_r', 0) for t in t_list)
            sl = sum(1 for t in t_list if t.get('outcome', '').upper() in ('SL', 'SL_HIT'))
        else:
            wins = sum(1 for t in t_list if getattr(t, 'pnl_r', 0) > 0)
            total = sum(getattr(t, 'pnl_r', 0) for t in t_list)
            sl = sum(1 for t in t_list if getattr(t, 'outcome', '').upper() in ('SL', 'SL_HIT'))
        return n, wins/n*100, total/n, total, sl/n*100

    bn, bwr, bev, btot, bsl = stats(baseline_swing)
    nn, nwr, nev, ntot, nsl = stats(swing_trades)
    print(f"{'Metric':<15} {'BASELINE':<15} {'NEW (tp1=1.5)':<18} {'Delta':<15}")
    print('-' * 70)
    print(f"{'Trades':<15} {bn:<15} {nn:<18} {nn-bn:+d}")
    print(f"{'WR%':<15} {bwr:>6.1f}%        {nwr:>6.1f}%             {nwr-bwr:+.1f}pp")
    print(f"{'EV/trade':<15} {bev:>+6.3f}R        {nev:>+6.3f}R             {nev-bev:+.3f}R")
    print(f"{'Total PnL':<15} {btot:>+6.1f}R        {ntot:>+6.1f}R             {ntot-btot:+.1f}R")
    print(f"{'SL rate':<15} {bsl:>6.1f}%        {nsl:>6.1f}%             {nsl-bsl:+.1f}pp")

    return 0


if __name__ == '__main__':
    sys.exit(main())
