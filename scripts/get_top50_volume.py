#!/usr/bin/env python3
"""Fetch top 50 coin by 24h volume dari Binance Futures.

Output: space-separated coin symbols (without USDT suffix).

Usage:
    python3 scripts/get_top50_volume.py            # print 50 symbols
    python3 scripts/get_top50_volume.py --json     # JSON list
"""
import sys
import json
import argparse

import requests

parser = argparse.ArgumentParser()
parser.add_argument("--limit", type=int, default=50)
parser.add_argument("--json", action="store_true",
                    help="Output as JSON list")
args = parser.parse_args()

# Binance Futures 24h ticker
url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
try:
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
except Exception as e:
    print(f"ERROR fetch: {e}", file=sys.stderr)
    sys.exit(1)

# Filter USDT pairs only, sort by quoteVolume desc
usdt_pairs = [
    x for x in data
    if x.get('symbol', '').endswith('USDT')
    and not x.get('symbol', '').startswith('USDC')  # skip USDC stablecoin pairs
]

# Filter out leverage tokens (1000XXX, BULLDOG, etc) — keep clean
# Also filter out very low volume garbage
def is_clean(sym):
    s = sym.replace('USDT', '')
    # Allow 1000PEPE etc (some are real coins)
    if len(s) > 12:  # too long = likely junk
        return False
    return True

filtered = [x for x in usdt_pairs if is_clean(x.get('symbol', ''))]

# Sort by quoteVolume descending
sorted_data = sorted(
    filtered,
    key=lambda x: float(x.get('quoteVolume', 0) or 0),
    reverse=True,
)

top_n = sorted_data[:args.limit]
symbols = [x['symbol'].replace('USDT', '') for x in top_n]

if args.json:
    print(json.dumps(symbols))
else:
    print(' '.join(symbols))
