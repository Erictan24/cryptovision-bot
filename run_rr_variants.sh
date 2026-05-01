#!/bin/bash
# Run backtest 4 variant: baseline + A + B + C
# Sama config: 60d, 100 coin, 5m only, no fetch (pakai cache)

set -e

DAYS=60
TF=5m
LOG_DIR="."

echo "=========================================="
echo " RR VARIANT BACKTEST RUNNER"
echo "=========================================="
echo " Days   : $DAYS"
echo " TF     : $TF"
echo " Coins  : 100 (default)"
echo "=========================================="

for variant in baseline A B C; do
    log_file="$LOG_DIR/backtest_rr_${variant}.log"
    echo ""
    echo ">>> Running variant: $variant"
    echo ">>> Output: $log_file"
    start_ts=$(date +%s)

    python backtest_scalp.py \
        --tf $TF \
        --days $DAYS \
        --no-fetch \
        --rr-variant $variant \
        > "$log_file" 2>&1

    end_ts=$(date +%s)
    elapsed=$((end_ts - start_ts))
    echo ">>> Variant $variant: SELESAI dalam ${elapsed}s"

    # Quick summary
    grep -E "Total Trades|Win Rate|Avg PnL/trade|Total PnL|Monthly est" "$log_file" | head -5
done

echo ""
echo "=========================================="
echo " ALL VARIANTS DONE — comparing results"
echo "=========================================="
python compare_rr_variants.py \
    backtest_rr_baseline.log \
    backtest_rr_A.log \
    backtest_rr_B.log \
    backtest_rr_C.log
