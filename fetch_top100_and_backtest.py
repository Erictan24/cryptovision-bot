"""
fetch_top100_and_backtest.py — Full pipeline untuk unified backtest top 100 coin.

Steps:
  1. Fetch top 100 coins from Binance Futures (by volume)
  2. Fetch 15m + 1h + 4h data untuk semua 100 coin (90 hari)
  3. Save ke unified cache
  4. Run unified backtest (SCALP + SWING)

Expected runtime: 5-6 jam
"""

import os
import sys
import time
import pickle
import requests
import logging

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(message)s')
logger = logging.getLogger(__name__)

CACHE_FILE = 'backtesting/cache/unified_data.pkl'


def fetch_top_100_coins() -> list:
    """
    Get top 100 LIQUID coins dari Binance Futures sorted by 24h volume.
    Filter: stablecoins, leveraged tokens, non-ASCII names, meme coins tanpa history.
    """
    logger.info("Fetching top coins from Binance Futures...")
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    tickers = resp.json()

    stablecoins = {
        'USDT', 'USDC', 'BUSD', 'DAI', 'TUSD', 'FDUSD', 'USDD', 'USDP',
        'WBTC', 'WETH', 'STETH', 'WSTETH', 'RETH', 'CBETH', 'FRAX', 'LUSD',
        'GUSD', 'USDE', 'WBNB', 'BTCB', 'LBTC', 'USDX', 'USD1', 'AEUR',
    }

    # Meme coin baru yang volatile dan sering tidak cukup historical data
    meme_skip = {
        'RAVE', 'BLESS', 'ALPACA', 'AURA', 'PUMP', 'DUMP',
        'MOON', 'ELON', 'SHIB2', 'DOGE2', 'SAFEMOON',
    }

    valid = []
    for t in tickers:
        sym_pair = t.get('symbol', '')
        if not sym_pair.endswith('USDT'):
            continue
        sym = sym_pair[:-4]

        # Skip non-ASCII names (mandarin, cyrillic, etc)
        if not sym.isascii() or not sym.replace('_', '').isalnum():
            continue

        # Skip 1000-prefix tokens (biasanya meme derivative)
        if sym.startswith('1000'):
            continue

        if sym in stablecoins or sym in meme_skip:
            continue

        # Skip leveraged tokens
        if any(sym.endswith(s) for s in
               ('UP', 'DOWN', 'BULL', 'BEAR', '3L', '3S', '5L', '5S')):
            continue

        # Skip commodity/index
        if sym in ('XAU', 'XAG', 'CL', 'BZ', 'GC', 'SI', 'NG'):
            continue

        vol = float(t.get('quoteVolume', 0))
        if vol < 10_000_000:  # minimal $10M daily volume (lebih ketat)
            continue

        # Skip kalau price < $0.0001 (coin yang sangat kecil)
        price = float(t.get('lastPrice', 0))
        if price < 0.0001:
            continue

        valid.append((sym, vol))

    # Sort by volume descending
    valid.sort(key=lambda x: x[1], reverse=True)
    top_100 = [s for s, _ in valid[:100]]

    logger.info(f"Filtered {len(valid)} valid coins, taking top 100")
    logger.info(f"Top 10: {top_100[:10]}")
    logger.info(f"#91-100: {top_100[90:100]}")
    return top_100


def fetch_coin_data(symbol: str, interval: str, days: int) -> object:
    """Fetch OHLCV dari Binance dengan pagination."""
    from backtest_scalp import fetch_klines_paginated
    return fetch_klines_paginated(symbol, interval, days)


def fetch_all_data(coins: list, days: int = 90) -> dict:
    """
    Fetch 15m + 1h + 4h untuk semua coin.
    Resume capable — kalau cache ada, skip yang sudah ada.
    """
    data = {}

    # Load existing cache kalau ada
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'rb') as f:
                data = pickle.load(f)
            logger.info(f"Loaded existing cache: {len(data)} entries")
        except Exception as e:
            logger.warning(f"Cache load failed: {e}")
            data = {}

    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)

    total_to_fetch = len(coins) * 3
    fetched = 0

    for i, coin in enumerate(coins):
        if coin not in data:
            data[coin] = {}

        existing_tfs = set(data[coin].keys()) if coin in data else set()

        for tf in ['15m', '1h', '4h']:
            if tf in existing_tfs:
                logger.debug(f"  [{i+1}/{len(coins)}] {coin} {tf}: skip (cached)")
                fetched += 1
                continue

            logger.info(f"  [{i+1}/{len(coins)}] {coin} {tf}... "
                        f"(progress: {fetched}/{total_to_fetch})")
            try:
                df = fetch_coin_data(coin, tf, days)
                if df is not None and len(df) > 20:
                    data[coin][tf] = df
                    logger.info(f"    OK: {len(df)} candles")
                else:
                    logger.warning(f"    FAILED: no data")
            except Exception as e:
                logger.error(f"    ERROR: {e}")

            fetched += 1
            time.sleep(0.3)  # rate limit courtesy

            # Save cache setiap 10 coin (in case crash)
            if fetched % 30 == 0:
                _save_cache(data, coins, days)
                logger.info(f"  Checkpoint saved ({fetched}/{total_to_fetch})")

    # Final save
    _save_cache(data, coins, days)
    logger.info(f"All data fetched and cached")
    return data


def _save_cache(data: dict, coins: list, days: int):
    """Save data to cache with meta."""
    data['_meta'] = {
        'days': days,
        'coins': coins,
        'ts': time.time(),
        'unified': True,
        'version': 'top100',
    }
    try:
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(data, f, protocol=4)
    except Exception as e:
        logger.error(f"Save failed: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--skip-fetch', action='store_true',
                        help='Skip fetching, use existing cache')
    parser.add_argument('--skip-backtest', action='store_true',
                        help='Only fetch, don\'t run backtest')
    parser.add_argument('--coins', type=int, default=100)
    args = parser.parse_args()

    days = args.days
    n_coins = args.coins

    print("=" * 70)
    print(f" TOP {n_coins} UNIFIED BACKTEST PIPELINE")
    print(f" Period: {days} hari")
    print("=" * 70)

    # Step 1: Get top 100 coins
    if not args.skip_fetch:
        top_coins = fetch_top_100_coins()[:n_coins]
        logger.info(f"Using {len(top_coins)} coins")

        # Step 2: Fetch data
        data = fetch_all_data(top_coins, days=days)

        # Count successful
        success = sum(1 for c in top_coins if c in data and
                      len(data[c]) == 3)
        logger.info(f"Fetch complete: {success}/{len(top_coins)} "
                    f"coins have all 3 TFs")
    else:
        if not os.path.exists(CACHE_FILE):
            logger.error("No cache found, can't skip fetch")
            return 1
        with open(CACHE_FILE, 'rb') as f:
            data = pickle.load(f)
        top_coins = data.get('_meta', {}).get('coins', [])[:n_coins]
        logger.info(f"Using cached data: {len(top_coins)} coins")

    if args.skip_backtest:
        logger.info("Skipping backtest (--skip-backtest)")
        return 0

    # Step 3: Run unified backtest
    logger.info("=" * 70)
    logger.info(" STARTING UNIFIED BACKTEST")
    logger.info("=" * 70)

    from backtest_unified import (
        run_scalp_backtest, run_swing_backtest, print_unified_report
    )

    all_trades = []
    start_ts = time.time()

    # SCALP
    logger.info("\n>>> Running SCALP engine...")
    try:
        scalp_trades = run_scalp_backtest(top_coins, data, verbose=False)
        all_trades.extend(scalp_trades)
        logger.info(f"SCALP total: {len(scalp_trades)} trades")
    except Exception as e:
        logger.error(f"SCALP failed: {e}", exc_info=True)

    # SWING
    logger.info("\n>>> Running SWING engine...")
    try:
        swing_trades = run_swing_backtest(top_coins, data, verbose=False)
        all_trades.extend(swing_trades)
        logger.info(f"SWING total: {len(swing_trades)} trades")
    except Exception as e:
        logger.error(f"SWING failed: {e}", exc_info=True)

    elapsed = time.time() - start_ts
    logger.info(f"\nTotal backtest: {len(all_trades)} trades "
                f"({elapsed/60:.1f} min)")

    # Report
    print_unified_report(all_trades, days)

    # Save results
    from dataclasses import asdict
    from datetime import datetime
    os.makedirs('backtesting/results', exist_ok=True)
    ts_label = datetime.now().strftime('%Y%m%d_%H%M')
    result_file = f'backtesting/results/unified_top{n_coins}_{ts_label}.pkl'
    try:
        with open(result_file, 'wb') as f:
            pickle.dump({
                'trades': [asdict(t) for t in all_trades],
                'coins': top_coins,
                'days': days,
                'n_coins': n_coins,
                'timestamp': ts_label,
            }, f)
        logger.info(f"Results saved: {result_file}")
    except Exception as e:
        logger.warning(f"Save failed: {e}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
