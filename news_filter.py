"""
news_filter.py — Filter trading saat ada high-impact news.

Sumber: Forex Factory calendar (free, no API key)
Update: setiap jam

Event yang diblok:
  - FOMC (Federal Reserve rate decision)
  - CPI (Consumer Price Index)
  - NFP (Non-Farm Payroll)
  - GDP
  - PPI
  - JOLTS
  - PCE
  - Dan semua event HIGH impact dari USD/BTC/Crypto

Logic:
  - 2 jam sebelum event HIGH → warning, kurangi risk
  - 30 menit sebelum event → blok semua trade
  - 30 menit setelah event → blok semua trade (volatilitas tinggi)
"""

import requests
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class NewsFilter:
    CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    # Keyword event high impact yang relevan untuk crypto/BTC
    HIGH_IMPACT_KEYWORDS = [
        'FOMC', 'Federal Funds', 'Fed Rate', 'Interest Rate',
        'CPI', 'Consumer Price', 'Inflation',
        'NFP', 'Non-Farm', 'Nonfarm', 'Employment',
        'GDP', 'Gross Domestic',
        'PPI', 'Producer Price',
        'PCE', 'Personal Consumption',
        'JOLTS', 'Job Opening',
        'Retail Sales',
        'Powell', 'Fed Chair', 'Fed Speech',
        'Treasury', 'Debt',
        'Bitcoin ETF', 'Crypto', 'SEC',
    ]

    def __init__(self):
        self._events      = []
        self._last_update = None
        self._lock        = threading.Lock()
        self._update_interval = 3600  # update setiap 1 jam
        self._warned_unavailable = False  # Cegah spam warning di backtest mode

        # Fetch langsung saat init
        self._fetch_calendar()

    def _fetch_calendar(self):
        """Fetch economic calendar dari Forex Factory."""
        # Coba beberapa URL
        urls = [
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
        ]
        for url in urls:
            try:
                resp = requests.get(url, timeout=8,
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        with self._lock:
                            # Merge events dari thisweek + nextweek
                            existing = {e.get('date','') for e in self._events}
                            new_events = [e for e in data if e.get('date','') not in existing]
                            self._events.extend(new_events)
                            self._last_update = datetime.now()
                        logger.info(f"📰 News calendar: {len(self._events)} events loaded")
            except Exception as e:
                logger.debug(f"News calendar {url}: {e}")

        if not self._events and not self._warned_unavailable:
            logger.warning("📰 News calendar tidak tersedia — news filter dinonaktifkan")
            self._warned_unavailable = True

    def _maybe_refresh(self):
        """Refresh kalau sudah lebih dari 1 jam."""
        if (self._last_update is None or
                (datetime.now() - self._last_update).total_seconds() > self._update_interval):
            threading.Thread(target=self._fetch_calendar, daemon=True).start()

    def _parse_event_time(self, event) -> Optional[datetime]:
        """Parse waktu event dari berbagai format Forex Factory."""
        try:
            if not isinstance(event, dict):
                return None
            date_str = event.get('date', '') or event.get('datetime', '') or ''
            if not date_str:
                return None

            # Coba parse dengan timezone
            for fmt in ['%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S',
                        '%m-%d-%YT%H:%M:%S', '%Y-%m-%d']:
                try:
                    dt = datetime.strptime(date_str[:19], fmt[:19])
                    # Asumsikan ET (UTC-5) kalau tidak ada timezone
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone(timedelta(hours=-5)))
                    # Convert ke UTC
                    return dt.astimezone(timezone.utc).replace(tzinfo=None)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _is_relevant_event(self, event) -> bool:
        """Cek apakah event relevan untuk crypto trading."""
        # Guard: event harus dict
        if not isinstance(event, dict):
            return False
        impact   = str(event.get('impact', '')).upper()
        title    = str(event.get('title', '')).upper()
        currency = str(event.get('country', '')).upper()

        # Hanya HIGH impact
        if impact not in ('HIGH', 'RED', '3', '!!!'):
            return False

        # Hanya USD (paling berpengaruh ke crypto) atau label crypto langsung
        if currency not in ('USD', 'US', 'BTC', 'CRYPTO', ''):
            return False

        # Cek keyword
        for kw in self.HIGH_IMPACT_KEYWORDS:
            if kw.upper() in title:
                return True

        # Kalau high impact USD apapun — tetap relevan
        if currency in ('USD', 'US') and impact in ('HIGH', 'RED', '3', '!!!'):
            return True

        return False

    def check(self) -> dict:
        """Cek kondisi news sekarang."""
        result = {
            'block'      : False,
            'warning'    : False,
            'reason'     : '',
            'next_event' : None,
            'minutes_to' : 999,
        }

        self._maybe_refresh()

        # Kalau tidak ada data → aman trading
        with self._lock:
            if not self._events:
                return result

        now = datetime.utcnow()
        upcoming = []

        with self._lock:
            for event in self._events:
                if not isinstance(event, dict):
                    continue
                if not self._is_relevant_event(event):
                    continue

                event_time = self._parse_event_time(event)
                if not event_time:
                    continue

                diff_minutes = (event_time - now).total_seconds() / 60

                # Cek event dalam range -60 menit (sudah lewat) sampai +180 menit (akan datang)
                if -60 <= diff_minutes <= 180:
                    upcoming.append({
                        'title'     : event.get('title', 'Unknown Event'),
                        'time'      : event_time,
                        'diff_min'  : diff_minutes,
                        'currency'  : event.get('country', 'USD'),
                        'forecast'  : event.get('forecast', ''),
                        'previous'  : event.get('previous', ''),
                    })

        if not upcoming:
            return result

        # Sort by waktu
        upcoming.sort(key=lambda x: abs(x['diff_min']))
        nearest = upcoming[0]
        diff    = nearest['diff_min']

        result['next_event'] = nearest
        result['minutes_to'] = round(diff, 1)

        title = nearest['title']
        time_str = nearest['time'].strftime('%H:%M UTC')

        if -30 <= diff <= 30:
            # 30 menit sebelum/sesudah event → BLOK total
            if diff >= 0:
                result['block']  = True
                result['reason'] = f"🚫 HIGH IMPACT NEWS dalam {diff:.0f} menit: {title} ({time_str}) — trading diblok"
            else:
                result['block']  = True
                result['reason'] = f"🚫 HIGH IMPACT NEWS baru saja: {title} ({abs(diff):.0f} menit lalu) — market volatile"

        elif 30 < diff <= 120:
            # 30–120 menit sebelum → WARNING
            result['warning'] = True
            result['reason']  = f"⚠️ HIGH IMPACT NEWS dalam {diff:.0f} menit: {title} ({time_str}) — hati-hati"

        return result

    def get_upcoming_events(self, hours_ahead: int = 24) -> list:
        """Return semua event relevan dalam X jam ke depan."""
        self._maybe_refresh()
        now     = datetime.utcnow()
        cutoff  = now + timedelta(hours=hours_ahead)
        events  = []

        with self._lock:
            for event in self._events:
                if not isinstance(event, dict):
                    continue
                if not self._is_relevant_event(event):
                    continue
                event_time = self._parse_event_time(event)
                if event_time and now <= event_time <= cutoff:
                    diff = (event_time - now).total_seconds() / 60
                    events.append({
                        'title'   : event.get('title', ''),
                        'time'    : event_time,
                        'diff_min': round(diff, 0),
                        'currency': event.get('country', ''),
                    })

        events.sort(key=lambda x: x['diff_min'])
        return events

    def format_upcoming(self, hours_ahead: int = 24) -> str:
        """Format event untuk ditampilkan di Telegram."""
        events = self.get_upcoming_events(hours_ahead)
        if not events:
            return f"✅ Tidak ada high-impact news dalam {hours_ahead} jam ke depan"

        # Convert UTC ke WIB (UTC+7)
        lines = [f"📰 HIGH IMPACT NEWS ({hours_ahead}H ke depan):\n"]
        for e in events:
            wib_time = (e['time'] + timedelta(hours=7)).strftime('%d/%m %H:%M WIB')
            h = int(e['diff_min'] // 60)
            m = int(e['diff_min'] % 60)
            eta = f"{h}j {m}m" if h > 0 else f"{m}m"
            lines.append(f"  ⏰ {wib_time} ({eta} lagi)\n  📌 {e['title']}\n")

        return "\n".join(lines)


# Singleton global
_news_filter: Optional[NewsFilter] = None

def get_news_filter() -> NewsFilter:
    global _news_filter
    if _news_filter is None:
        _news_filter = NewsFilter()
    return _news_filter