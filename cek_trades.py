import json

with open('backtesting/results/trades_default_20260318_1541.json') as f:
    trades = json.load(f)

print("Total trades: " + str(len(trades)))
print("")
for t in trades[:10]:
    print(t['symbol'] + " " + t['tf'] + " " + t['direction'] + " " + t['quality'])
    print("  Entry:" + str(round(t['entry'],6)) + "  SL:" + str(round(t['sl'],6)) + "  TP2:" + str(round(t['tp2'],6)))
    print("  RR2:" + str(t['rr2']) + "  Outcome:" + t['outcome'])
    print("")