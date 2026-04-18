"""
dashboard_api.py — FastAPI backend untuk public dashboard.

Expose stats dari scalp_trades.db via REST API.
Untuk public dashboard HTML + nanti website.

Usage:
  python dashboard_api.py
  curl http://localhost:8080/stats
  curl http://localhost:8080/recent-trades?limit=20

Deploy:
  - Jalankan di VPS bersama main_scalp.py
  - Expose port 8080 (atau reverse proxy via nginx)
  - Frontend fetch dari endpoint ini
"""

import os
import sys
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional

try:
    from fastapi import FastAPI, Query, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
except ImportError:
    print("ERROR: FastAPI belum terinstall")
    print("Run: pip install fastapi uvicorn")
    sys.exit(1)

logger = logging.getLogger(__name__)

DB_PATH = 'data/scalp_trades.db'

app = FastAPI(
    title="CryptoVision Bot — Public Stats API",
    description="Real-time performance statistics dari trading bot",
    version="1.0.0",
)

# CORS — allow all origins (public API)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _get_db():
    """Connect ke SQLite database."""
    if not os.path.exists(DB_PATH):
        raise HTTPException(500, f"Database not found: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/")
def root():
    """Root endpoint — HTML dashboard."""
    return HTMLResponse(content=_get_dashboard_html(), status_code=200)


@app.get("/api/stats")
def get_stats(engine: Optional[str] = None, days: int = 90):
    """
    Get overall performance stats.

    Query params:
      engine: 'SCALP' | 'SWING' | None (all)
      days: period dalam hari (default 90)
    """
    conn = _get_db()
    cur = conn.cursor()

    since = (datetime.now() - timedelta(days=days)).isoformat()

    # Build query
    where_clauses = ["timestamp >= ?"]
    params = [since]
    if engine:
        # Filter via engine_version
        where_clauses.append(
            "(engine_version LIKE ? OR engine_version = ?)")
        params.extend([f'%{engine.lower()}%', engine])

    where = " AND ".join(where_clauses)

    cur.execute(f"""
        SELECT
            COUNT(*) as n,
            SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_r < 0 THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN pnl_r = 0 THEN 1 ELSE 0 END) as bep,
            AVG(pnl_r) as avg_pnl,
            SUM(pnl_r) as total_pnl,
            MAX(pnl_r) as best_trade,
            MIN(pnl_r) as worst_trade
        FROM trades
        WHERE {where}
    """, params)
    row = cur.fetchone()

    if not row or row['n'] == 0:
        conn.close()
        return {
            'n_trades': 0,
            'wr': 0,
            'ev_r': 0,
            'total_pnl_r': 0,
            'engine': engine or 'all',
            'days': days,
        }

    # Compute drawdown
    cur.execute(f"""
        SELECT pnl_r FROM trades
        WHERE {where}
        ORDER BY id ASC
    """, params)
    pnls = [r['pnl_r'] for r in cur.fetchall()]

    equity = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    conn.close()

    n = row['n']
    wins = row['wins'] or 0

    return {
        'engine': engine or 'all',
        'days': days,
        'n_trades': n,
        'wr': round(wins / n * 100, 1) if n else 0,
        'ev_r': round(row['avg_pnl'] or 0, 2),
        'total_pnl_r': round(row['total_pnl'] or 0, 2),
        'best_trade_r': round(row['best_trade'] or 0, 2),
        'worst_trade_r': round(row['worst_trade'] or 0, 2),
        'wins': wins,
        'losses': row['losses'] or 0,
        'bep': row['bep'] or 0,
        'max_drawdown_r': round(max_dd, 2),
    }


@app.get("/api/recent-trades")
def get_recent_trades(limit: int = 20, engine: Optional[str] = None):
    """Get recent trades dengan filter."""
    conn = _get_db()
    cur = conn.cursor()

    where = "1=1"
    params = []
    if engine:
        where = "engine_version LIKE ?"
        params = [f'%{engine.lower()}%']

    cur.execute(f"""
        SELECT timestamp, symbol, direction, quality, entry_price,
               sl, tp1, tp2, outcome, pnl_r, engine_version,
               session, trend_state
        FROM trades
        WHERE {where}
        ORDER BY id DESC
        LIMIT ?
    """, params + [limit])

    rows = cur.fetchall()
    conn.close()

    return [dict(r) for r in rows]


@app.get("/api/per-coin")
def get_per_coin_stats():
    """Stats per coin."""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol,
               COUNT(*) as n,
               SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) as wins,
               AVG(pnl_r) as ev,
               SUM(pnl_r) as total_pnl
        FROM trades
        GROUP BY symbol
        HAVING n >= 3
        ORDER BY total_pnl DESC
    """)
    rows = cur.fetchall()
    conn.close()

    return [
        {
            'symbol': r['symbol'],
            'n_trades': r['n'],
            'wr': round(r['wins'] / r['n'] * 100, 1) if r['n'] else 0,
            'ev_r': round(r['ev'] or 0, 2),
            'total_pnl_r': round(r['total_pnl'] or 0, 2),
        }
        for r in rows
    ]


@app.get("/api/equity-curve")
def get_equity_curve(engine: Optional[str] = None, limit: int = 200):
    """Equity curve untuk chart."""
    conn = _get_db()
    cur = conn.cursor()

    where = "1=1"
    params = []
    if engine:
        where = "engine_version LIKE ?"
        params = [f'%{engine.lower()}%']

    cur.execute(f"""
        SELECT timestamp, pnl_r, symbol
        FROM trades
        WHERE {where}
        ORDER BY id ASC
        LIMIT ?
    """, params + [limit])

    rows = cur.fetchall()
    conn.close()

    equity = 0
    curve = []
    for r in rows:
        equity += r['pnl_r'] or 0
        curve.append({
            'timestamp': r['timestamp'],
            'equity_r': round(equity, 2),
            'symbol': r['symbol'],
        })
    return curve


@app.get("/api/health")
def health():
    return {
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'db_exists': os.path.exists(DB_PATH),
    }


def _get_dashboard_html() -> str:
    """Return inline HTML dashboard."""
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoVision Bot — Live Stats</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif;
    background: #0b0e16;
    color: #e4e7ed;
    padding: 20px;
  }
  .container { max-width: 1200px; margin: 0 auto; }
  header {
    background: linear-gradient(135deg, #1a1f2e 0%, #131722 100%);
    padding: 30px;
    border-radius: 12px;
    margin-bottom: 24px;
    border: 1px solid #2a2e3e;
  }
  header h1 { font-size: 28px; margin-bottom: 8px; }
  header p { color: #8b92a8; font-size: 14px; }
  .tabs { display: flex; gap: 8px; margin-bottom: 20px; }
  .tab {
    padding: 10px 20px;
    background: #1a1f2e;
    border: 1px solid #2a2e3e;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.2s;
    font-size: 14px;
  }
  .tab:hover { background: #252b3d; }
  .tab.active {
    background: #2962ff;
    border-color: #2962ff;
    color: white;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }
  .card {
    background: #1a1f2e;
    border: 1px solid #2a2e3e;
    border-radius: 10px;
    padding: 20px;
  }
  .card h3 {
    font-size: 12px;
    text-transform: uppercase;
    color: #8b92a8;
    margin-bottom: 8px;
    letter-spacing: 0.5px;
  }
  .card .value {
    font-size: 28px;
    font-weight: 700;
  }
  .card .sub {
    font-size: 12px;
    color: #8b92a8;
    margin-top: 4px;
  }
  .green { color: #26a69a; }
  .red { color: #ef5350; }
  .dim { color: #8b92a8; }
  .chart-container {
    background: #1a1f2e;
    border: 1px solid #2a2e3e;
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 24px;
    height: 400px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    background: #1a1f2e;
    border-radius: 10px;
    overflow: hidden;
  }
  th, td {
    padding: 12px;
    text-align: left;
    border-bottom: 1px solid #2a2e3e;
    font-size: 13px;
  }
  th {
    background: #131722;
    color: #8b92a8;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-size: 11px;
  }
  .tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
  }
  .tag-scalp { background: #2962ff; color: white; }
  .tag-swing { background: #9c27b0; color: white; }
  .tag-long { background: #26a69a; color: white; }
  .tag-short { background: #ef5350; color: white; }
  .footer {
    margin-top: 40px;
    padding: 20px;
    text-align: center;
    color: #8b92a8;
    font-size: 12px;
  }
  .disclaimer {
    background: #1a1f2e;
    border: 1px solid #2a2e3e;
    border-left: 3px solid #ff9800;
    border-radius: 6px;
    padding: 16px;
    margin: 24px 0;
    font-size: 12px;
    color: #b8bec8;
  }
</style>
</head>
<body>
<div class="container">

<header>
  <h1>🤖 CryptoVision Bot — Live Performance</h1>
  <p>Transparent trading bot statistics. All data from real backtest + live trades.</p>
</header>

<div class="tabs">
  <div class="tab active" data-engine="">All</div>
  <div class="tab" data-engine="scalp">SCALP (15m)</div>
  <div class="tab" data-engine="swing">SWING (1h/4h)</div>
</div>

<div class="grid" id="stats-grid">
  <div class="card">
    <h3>Total Trades</h3>
    <div class="value" id="stat-n">-</div>
    <div class="sub" id="stat-n-sub">Loading...</div>
  </div>
  <div class="card">
    <h3>Win Rate</h3>
    <div class="value" id="stat-wr">-</div>
    <div class="sub" id="stat-wr-sub">-</div>
  </div>
  <div class="card">
    <h3>Avg PnL</h3>
    <div class="value" id="stat-ev">-</div>
    <div class="sub">per trade (R)</div>
  </div>
  <div class="card">
    <h3>Total Profit</h3>
    <div class="value" id="stat-total">-</div>
    <div class="sub" id="stat-total-sub">total R units</div>
  </div>
  <div class="card">
    <h3>Max Drawdown</h3>
    <div class="value red" id="stat-dd">-</div>
    <div class="sub">worst case</div>
  </div>
</div>

<div class="chart-container">
  <canvas id="equity-chart"></canvas>
</div>

<h2 style="margin: 30px 0 16px; font-size: 18px;">Recent Trades</h2>
<table>
  <thead>
    <tr>
      <th>Time</th>
      <th>Engine</th>
      <th>Symbol</th>
      <th>Direction</th>
      <th>Entry</th>
      <th>Outcome</th>
      <th>PnL (R)</th>
    </tr>
  </thead>
  <tbody id="trades-body">
    <tr><td colspan="7" class="dim" style="text-align:center">Loading...</td></tr>
  </tbody>
</table>

<div class="disclaimer">
  <strong>⚠️ Disclaimer:</strong> This dashboard shows educational/research data only.
  Past performance does not guarantee future results. Cryptocurrency trading involves
  substantial risk. This is NOT financial advice. Trade at your own risk.
</div>

<div class="footer">
  CryptoVision Bot • Auto-updated every 30s • v4.3 Learning Edition
</div>

</div>

<script>
const API_BASE = '';  // empty = same origin
let currentEngine = '';
let equityChart = null;

async function fetchStats() {
  const url = `${API_BASE}/api/stats${currentEngine ? '?engine=' + currentEngine : ''}`;
  const r = await fetch(url);
  return r.json();
}

async function fetchTrades() {
  const url = `${API_BASE}/api/recent-trades?limit=30${currentEngine ? '&engine=' + currentEngine : ''}`;
  const r = await fetch(url);
  return r.json();
}

async function fetchEquity() {
  const url = `${API_BASE}/api/equity-curve?limit=500${currentEngine ? '&engine=' + currentEngine : ''}`;
  const r = await fetch(url);
  return r.json();
}

function fmt(n, decimals = 2) {
  if (n === null || n === undefined) return '-';
  return Number(n).toFixed(decimals);
}

function updateStats(stats) {
  document.getElementById('stat-n').textContent = stats.n_trades || 0;
  document.getElementById('stat-n-sub').textContent =
    `${stats.wins || 0}W / ${stats.losses || 0}L / ${stats.bep || 0} BEP`;

  const wr = fmt(stats.wr, 1);
  const wrEl = document.getElementById('stat-wr');
  wrEl.textContent = wr + '%';
  wrEl.className = 'value ' + (stats.wr >= 50 ? 'green' : stats.wr >= 40 ? '' : 'red');

  const ev = stats.ev_r || 0;
  const evEl = document.getElementById('stat-ev');
  evEl.textContent = (ev >= 0 ? '+' : '') + fmt(ev) + 'R';
  evEl.className = 'value ' + (ev > 0 ? 'green' : ev < 0 ? 'red' : '');

  const total = stats.total_pnl_r || 0;
  const totalEl = document.getElementById('stat-total');
  totalEl.textContent = (total >= 0 ? '+' : '') + fmt(total, 1) + 'R';
  totalEl.className = 'value ' + (total > 0 ? 'green' : 'red');

  document.getElementById('stat-dd').textContent =
    '-' + fmt(stats.max_drawdown_r, 1) + 'R';
}

function updateTrades(trades) {
  const tbody = document.getElementById('trades-body');
  if (!trades || trades.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="dim" style="text-align:center">No trades yet</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const engine = (t.engine_version || '').toLowerCase().includes('scalp') ||
                   (t.engine_version || '').includes('v4') ? 'SCALP' : 'SWING';
    const pnl = t.pnl_r || 0;
    const pnlClass = pnl > 0 ? 'green' : pnl < 0 ? 'red' : 'dim';
    const ts = (t.timestamp || '').substring(0, 16);
    return `
      <tr>
        <td class="dim">${ts}</td>
        <td><span class="tag tag-${engine.toLowerCase()}">${engine}</span></td>
        <td><strong>${t.symbol}</strong></td>
        <td><span class="tag tag-${(t.direction || '').toLowerCase()}">${t.direction || '-'}</span></td>
        <td>${fmt(t.entry_price, 4)}</td>
        <td class="dim">${t.outcome || '-'}</td>
        <td class="${pnlClass}"><strong>${pnl >= 0 ? '+' : ''}${fmt(pnl)}R</strong></td>
      </tr>
    `;
  }).join('');
}

function updateChart(curve) {
  const ctx = document.getElementById('equity-chart').getContext('2d');
  if (equityChart) equityChart.destroy();

  const labels = curve.map((_, i) => i + 1);
  const data = curve.map(p => p.equity_r);
  const lastVal = data[data.length - 1] || 0;

  equityChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Equity (R units)',
        data,
        borderColor: lastVal >= 0 ? '#26a69a' : '#ef5350',
        backgroundColor: (lastVal >= 0 ? '#26a69a' : '#ef5350') + '20',
        borderWidth: 2,
        fill: true,
        tension: 0.2,
        pointRadius: 0,
        pointHoverRadius: 4,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        title: {
          display: true,
          text: 'Cumulative Equity Curve',
          color: '#e4e7ed',
          font: { size: 14, weight: 'bold' }
        }
      },
      scales: {
        x: { display: false },
        y: {
          ticks: { color: '#8b92a8' },
          grid: { color: '#2a2e3e' }
        }
      }
    }
  });
}

async function refresh() {
  try {
    const [stats, trades, equity] = await Promise.all([
      fetchStats(), fetchTrades(), fetchEquity()
    ]);
    updateStats(stats);
    updateTrades(trades);
    updateChart(equity);
  } catch (e) {
    console.error('Refresh error:', e);
  }
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    currentEngine = tab.dataset.engine;
    refresh();
  });
});

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


def main():
    """Start API server."""
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s | %(levelname)s | %(message)s')

    port = int(os.getenv('DASHBOARD_PORT', '8080'))
    host = os.getenv('DASHBOARD_HOST', '0.0.0.0')

    print("=" * 60)
    print(" CryptoVision Bot — Dashboard API")
    print("=" * 60)
    print(f" Starting on http://{host}:{port}")
    print(f" Database: {DB_PATH}")
    print(f" Dashboard: http://localhost:{port}/")
    print(f" API docs:  http://localhost:{port}/docs")
    print("=" * 60)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == '__main__':
    main()
