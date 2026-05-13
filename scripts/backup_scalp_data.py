"""Backup semua SCALP data ke JSON sebelum dihapus.

Output: scripts/scalp_backup_YYYYMMDD_HHMM/
Contains: scalp_trades.json, scalp_paper.json, scalp_coin_params.json,
          unified_positions_scalp.json, web_scalp_data.json (if HMAC config available)
"""
import os
import sys
import json
import sqlite3
import shutil
from datetime import datetime

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def dump_sqlite(db_path: str, output: str, where: str = ''):
    """Export semua tabel SQLite ke JSON."""
    if not os.path.exists(db_path):
        print(f"  [skip] {db_path} not found")
        return 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    payload = {}
    total = 0
    for t in tables:
        if t.startswith('sqlite_'):
            continue
        try:
            q = f"SELECT * FROM {t}"
            if where:
                q += f" WHERE {where}"
            cur.execute(q)
            rows = [dict(r) for r in cur.fetchall()]
            payload[t] = rows
            total += len(rows)
            print(f"  [{t}] {len(rows)} rows")
        except Exception as e:
            print(f"  [{t}] ERROR: {e}")
    conn.close()
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, default=str)
    return total


def main():
    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
    backup_dir = f"scripts/scalp_backup_{timestamp}"
    os.makedirs(backup_dir, exist_ok=True)
    print(f"Backup dir: {backup_dir}\n")

    # 1. scalp_trades.db (all rows — semua SCALP only)
    print("== scalp_trades.db ==")
    n1 = dump_sqlite('data/scalp_trades.db', f"{backup_dir}/scalp_trades.json")
    print(f"  Total: {n1} rows\n")

    # 2. scalp_paper.db
    print("== scalp_paper.db ==")
    n2 = dump_sqlite('data/scalp_paper.db', f"{backup_dir}/scalp_paper.json")
    print(f"  Total: {n2} rows\n")

    # 3. unified_positions.db — SCALP rows only
    print("== unified_positions.db (SCALP only) ==")
    n3 = dump_sqlite('data/unified_positions.db',
                     f"{backup_dir}/unified_positions_scalp.json",
                     where="engine='SCALP'")
    print(f"  Total: {n3} rows\n")

    # 4. scalp_coin_params.json (copy as-is)
    print("== scalp_coin_params.json ==")
    for f in ('data/scalp_coin_params.json', 'data/scalp_coin_params.json.backup'):
        if os.path.exists(f):
            dst = f"{backup_dir}/{os.path.basename(f)}"
            shutil.copy(f, dst)
            sz = os.path.getsize(f)
            print(f"  Copied {f} -> {dst} ({sz} bytes)")
    print()

    # 5. Backup learning.db SCALP rows (if any — table mixed)
    print("== learning.db (SCALP rows only) ==")
    if os.path.exists('data/learning.db'):
        conn = sqlite3.connect('data/learning.db')
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        learning_payload = {}
        for t in tables:
            if t.startswith('sqlite_'):
                continue
            # Try filter by engine/strategy column if exists
            cur.execute(f"PRAGMA table_info({t})")
            cols = [r[1].lower() for r in cur.fetchall()]
            where = ''
            if 'engine' in cols:
                where = "engine='SCALP'"
            elif 'strategy' in cols:
                where = "strategy='scalp'"
            elif 'tf' in cols or 'timeframe' in cols:
                # Heuristic: 5m / 15m → SCALP
                col = 'tf' if 'tf' in cols else 'timeframe'
                where = f"{col} IN ('5m', '15m')"
            try:
                q = f"SELECT * FROM {t}"
                if where:
                    q += f" WHERE {where}"
                cur.execute(q)
                rows = [dict(r) for r in cur.fetchall()]
                if rows:
                    learning_payload[t] = rows
                    print(f"  [{t}] {len(rows)} SCALP rows (filter: {where or 'all'})")
            except Exception as e:
                print(f"  [{t}] ERROR: {e}")
        conn.close()
        if learning_payload:
            with open(f"{backup_dir}/learning_scalp.json", 'w', encoding='utf-8') as f:
                json.dump(learning_payload, f, indent=2, default=str)
    print()

    # 6. Manifest
    manifest = {
        'timestamp': timestamp,
        'created_at': datetime.now().isoformat(),
        'description': 'SCALP data backup sebelum hapus engine SCALP',
        'files': sorted(os.listdir(backup_dir)),
    }
    with open(f"{backup_dir}/manifest.json", 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)

    # Summary
    print('=' * 60)
    print(f"BACKUP SELESAI: {backup_dir}")
    print('=' * 60)
    for f in sorted(os.listdir(backup_dir)):
        size = os.path.getsize(f"{backup_dir}/{f}")
        print(f"  {f:<40} {size:>10} bytes")
    print()
    print(f"Total trade records backed up: {n1 + n2 + n3} rows")
    print("Safe to proceed with delete operation.")


if __name__ == '__main__':
    main()
