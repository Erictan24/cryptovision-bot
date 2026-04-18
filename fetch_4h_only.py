"""
fetch_4h_only.py — Quick script untuk download 4H data saja
dan merge ke existing scalp_data cache untuk unified backtest.
"""

import os
import sys
import time
import pickle
import logging

from backtest_scalp import fetch_klines_paginated, CACHE_FILE

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
logger = logging.getLogger(__name__)

UNIFIED_CACHE = 'backtesting/cache/unified_data.pkl'


def main():
    if not os.path.exists(CACHE_FILE):
        print(f"ERROR: Scalp cache tidak ada di {CACHE_FILE}")
        return 1

    print(f"Loading existing scalp cache...")
    with open(CACHE_FILE, 'rb') as f:
        data = pickle.load(f)

    meta = data.get('_meta', {})
    coins = [c for c in meta.get('coins', []) if c in data]
    days = meta.get('days', 90)

    print(f"Found {len(coins)} coins, {days} days period")
    print(f"Current TFs: {list(data[coins[0]].keys()) if coins else 'none'}")

    # Check which coins need 4h
    missing_4h = [c for c in coins if '4h' not in data.get(c, {})]
    print(f"Need to fetch 4h for {len(missing_4h)} coins")

    if not missing_4h:
        print("All coins already have 4h data!")
        # Still save to unified cache
        os.makedirs(os.path.dirname(UNIFIED_CACHE), exist_ok=True)
        data['_meta']['unified'] = True
        with open(UNIFIED_CACHE, 'wb') as f:
            pickle.dump(data, f, protocol=4)
        print(f"Saved unified cache to {UNIFIED_CACHE}")
        return 0

    # Fetch 4h for missing coins
    for i, coin in enumerate(missing_4h):
        print(f"[{i+1}/{len(missing_4h)}] Fetching {coin} 4h...")
        try:
            df_4h = fetch_klines_paginated(coin, '4h', days)
            if df_4h is not None and len(df_4h) > 20:
                data[coin]['4h'] = df_4h
                print(f"  {coin} 4h: {len(df_4h)} candles")
            else:
                print(f"  {coin} 4h: FAILED")
        except Exception as e:
            print(f"  {coin} 4h ERROR: {e}")

        # Rate limit courtesy
        time.sleep(0.5)

    # Save to unified cache
    os.makedirs(os.path.dirname(UNIFIED_CACHE), exist_ok=True)
    data['_meta']['unified'] = True
    data['_meta']['ts'] = time.time()
    with open(UNIFIED_CACHE, 'wb') as f:
        pickle.dump(data, f, protocol=4)
    print(f"\nSaved unified cache to {UNIFIED_CACHE}")
    print(f"Cache size: {os.path.getsize(UNIFIED_CACHE) / 1024 / 1024:.1f} MB")

    return 0


if __name__ == '__main__':
    sys.exit(main())
