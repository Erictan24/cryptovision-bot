"""diag_scalp.py — Diagnostic kenapa signal scalp tidak muncul."""
import sys
import logging

sys.path.insert(0, '/home/eric/cryptovision-bot')
logging.basicConfig(level=logging.DEBUG, format='%(message)s')
for name in ['urllib3', 'requests', 'httpx', 'telegram', 'httpcore']:
    logging.getLogger(name).setLevel(logging.WARNING)

from scalp_live_runner import _scan_coin

coins = ['BTC', 'ETH', 'DOGE', 'BNB', 'ADA', 'DOT', 'TRUMP', 'WLD', 'TAO', 'ORDI', 'ARB', 'ZEC']
for coin in coins:
    print(f"\n{'='*50}\n>>> {coin}")
    sig = _scan_coin(coin)
    print(f">>> RESULT: {'*** SIGNAL FOUND ***' if sig else 'None'}")
