import requests
import threading

class WhaleAnalyzer:
    def __init__(self):
        self.binance_url = "https://fapi.binance.com"
        self.fg_url      = "https://api.alternative.me/fng/"
        self.session     = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0'})

    def _get(self, url, params=None, timeout=10):
        try:
            r = self.session.get(url, params=params, timeout=timeout)
            if r.status_code == 200: return r.json()
        except Exception as e:
            print(f"    [Whale] Request error: {e}")
        return None

    def get_funding_rate(self, symbol="BTCUSDT"):
        data = self._get(f"{self.binance_url}/fapi/v1/premiumIndex", params={'symbol': symbol})
        if not data: return None
        rate = float(data.get('lastFundingRate', 0)) * 100
        if rate > 0.05:     s, b = "🔴 LONG BERAT — Whale kemungkinan SHORT", "SHORT"
        elif rate > 0.01:   s, b = "🟡 SEDIKIT LONG — Condong bullish", "SLIGHT_LONG"
        elif rate < -0.05:  s, b = "🟢 SHORT BERAT — Whale kemungkinan LONG", "LONG"
        elif rate < -0.01:  s, b = "🟡 SEDIKIT SHORT — Condong bearish", "SLIGHT_SHORT"
        else:               s, b = "⬜ NETRAL — Seimbang", "NEUTRAL"
        return {'rate': round(rate, 4), 'sentiment': s, 'bias': b}

    def get_long_short_ratio(self, symbol="BTCUSDT"):
        dg = self._get(f"{self.binance_url}/fapi/v1/globalLongShortAccountRatio",
                       params={'symbol': symbol, 'period': '1h', 'limit': 1})
        dt = self._get(f"{self.binance_url}/fapi/v1/topLongShortPositionRatio",
                       params={'symbol': symbol, 'period': '1h', 'limit': 1})
        result = {}
        if dg and isinstance(dg, list):
            d = dg[0]; r = float(d.get('longShortRatio', 1))
            lp, sp = float(d.get('longAccount', .5))*100, float(d.get('shortAccount', .5))*100
            result['global'] = {'ratio': round(r,3), 'long_pct': round(lp,1), 'short_pct': round(sp,1),
                'label': "🔴 Mayoritas LONG" if r > 1.5 else ("🟢 Mayoritas SHORT" if r < 0.67 else "⬜ Seimbang")}
        if dt and isinstance(dt, list):
            d = dt[0]; r = float(d.get('longShortRatio', 1))
            lp, sp = float(d.get('longAccount', .5))*100, float(d.get('shortAccount', .5))*100
            result['top_trader'] = {'ratio': round(r,3), 'long_pct': round(lp,1), 'short_pct': round(sp,1),
                'label': "🐋 Top Trader LONG" if r > 1.2 else ("🐋 Top Trader SHORT" if r < 0.83 else "⬜ Top Trader Seimbang")}
        return result if result else None

    def get_open_interest(self, symbol="BTCUSDT"):
        dn = self._get(f"{self.binance_url}/fapi/v1/openInterest", params={'symbol': symbol})
        dh = self._get(f"{self.binance_url}/futures/data/openInterestHist",
                       params={'symbol': symbol, 'period': '1h', 'limit': 5})
        dp = self._get(f"{self.binance_url}/fapi/v1/ticker/price", params={'symbol': symbol})
        if not dn or not dp: return None
        oi = float(dn.get('openInterest', 0)); pr = float(dp.get('price', 0))
        oc = 0
        if dh and isinstance(dh, list) and len(dh) >= 2:
            old = float(dh[0].get('sumOpenInterest', oi))
            oc = ((oi - old) / old * 100) if old > 0 else 0
        if oc > 2:      t = "📈 OI Naik Signifikan"
        elif oc > 0.5:  t = "↗️ OI Naik Tipis"
        elif oc < -2:   t = "📉 OI Turun Signifikan"
        elif oc < -0.5: t = "↘️ OI Turun Tipis"
        else:           t = "➡️ OI Stabil"
        return {'oi_now': round(oi, 0), 'oi_change_pct': round(oc, 2), 'oi_trend': t, 'price': pr}

    def get_fear_greed(self):
        data = self._get(self.fg_url, params={'limit': 1})
        if not data: return None
        d = data.get('data', [{}])[0]; v = int(d.get('value', 50)); l = d.get('value_classification', 'Neutral')
        if v >= 80:   e, a = "🔴", "Extreme Greed — Whale kemungkinan JUAL"
        elif v >= 60: e, a = "🟡", "Greed — Hati-hati"
        elif v >= 45: e, a = "🟢", "Neutral — Sehat"
        elif v >= 25: e, a = "🟡", "Fear — Potensi akumulasi"
        else:         e, a = "🟢", "Extreme Fear — Whale sering beli"
        return {'value': v, 'label': l, 'emoji': e, 'advice': a}

    def get_liquidation_bias(self, symbol="BTCUSDT"):
        data = self._get(f"{self.binance_url}/fapi/v1/allForceOrders", params={'symbol': symbol, 'limit': 50})
        if not data or not isinstance(data, list): return None
        ll = sum(float(o.get('origQty', 0)) for o in data if o.get('side') == 'SELL')
        sl = sum(float(o.get('origQty', 0)) for o in data if o.get('side') == 'BUY')
        t = ll + sl
        if t == 0: return None
        lp, sp = (ll/t)*100, (sl/t)*100
        if lp > 60:   lb, b = "🔴 Long kena liq — tekanan TURUN", "BEARISH"
        elif sp > 60: lb, b = "🟢 Short kena liq — tekanan NAIK", "BULLISH"
        else:         lb, b = "⬜ Likuidasi seimbang", "NEUTRAL"
        return {'long_liq_pct': round(lp, 1), 'short_liq_pct': round(sp, 1), 'label': lb, 'bias': b}

    def full_whale_analysis(self, symbol="BTC"):
        sf = symbol.upper().replace('/USDT', '') + "USDT"
        R = {}
        def f(k, fn, *a): R[k] = fn(*a)
        threads = [
            threading.Thread(target=f, args=('funding', self.get_funding_rate, sf)),
            threading.Thread(target=f, args=('ls_ratio', self.get_long_short_ratio, sf)),
            threading.Thread(target=f, args=('oi', self.get_open_interest, sf)),
            threading.Thread(target=f, args=('fg', self.get_fear_greed)),
            threading.Thread(target=f, args=('liq', self.get_liquidation_bias, sf)),
        ]
        for t in threads: t.start()
        for t in threads: t.join(timeout=15)

        funding, ls_ratio = R.get('funding'), R.get('ls_ratio')
        oi, fg, liq = R.get('oi'), R.get('fg'), R.get('liq')
        score, signals = 0, []

        if funding:
            if funding['bias'] == 'LONG':         score += 30; signals.append("✅ Funding negatif → Whale LONG")
            elif funding['bias'] == 'SHORT':      score -= 30; signals.append("❌ Funding positif → Whale SHORT")
            elif funding['bias'] == 'SLIGHT_LONG':  score += 10; signals.append("↗️ Funding bullish")
            elif funding['bias'] == 'SLIGHT_SHORT': score -= 10; signals.append("↘️ Funding bearish")
            else: signals.append("➡️ Funding netral")
        if ls_ratio and 'top_trader' in ls_ratio:
            tt = ls_ratio['top_trader']
            if tt['ratio'] > 1.2:   score += 25; signals.append(f"✅ Top trader LONG ({tt['long_pct']}%)")
            elif tt['ratio'] < 0.83: score -= 25; signals.append(f"❌ Top trader SHORT ({tt['short_pct']}%)")
            else: signals.append("➡️ Top trader seimbang")
        if oi:
            if oi['oi_change_pct'] > 1:   score += 15; signals.append(f"✅ OI naik {oi['oi_change_pct']:+.1f}%")
            elif oi['oi_change_pct'] < -1: score -= 15; signals.append(f"❌ OI turun {oi['oi_change_pct']:+.1f}%")
        if fg:
            if fg['value'] <= 25:   score += 20; signals.append(f"✅ Extreme Fear ({fg['value']})")
            elif fg['value'] >= 80: score -= 20; signals.append(f"❌ Extreme Greed ({fg['value']})")
            elif fg['value'] >= 60: score -= 10; signals.append(f"⚠️ Greed ({fg['value']})")
            elif fg['value'] <= 40: score += 10; signals.append(f"📊 Fear ({fg['value']})")
        if liq:
            if liq['bias'] == 'BULLISH':  score += 10; signals.append(f"✅ Short liq ({liq['short_liq_pct']:.0f}%)")
            elif liq['bias'] == 'BEARISH': score -= 10; signals.append(f"❌ Long liq ({liq['long_liq_pct']:.0f}%)")

        if score >= 50:    v, d = "🐋🟢 WHALE AKUMULASI — Kemungkinan LONG", "LONG"
        elif score >= 20:  v, d = "🐋🟩 WHALE CENDERUNG LONG", "SLIGHT_LONG"
        elif score <= -50: v, d = "🐋🔴 WHALE DISTRIBUSI — Kemungkinan SHORT", "SHORT"
        elif score <= -20: v, d = "🐋🟥 WHALE CENDERUNG SHORT", "SLIGHT_SHORT"
        else:              v, d = "🐋⬜ WHALE NETRAL", "NEUTRAL"

        return {
            'symbol': symbol.upper(), 'score': score, 'verdict': v, 'direction': d,
            'signals': signals, 'funding': funding, 'ls_ratio': ls_ratio,
            'oi': oi, 'fg': fg, 'liq': liq
        }