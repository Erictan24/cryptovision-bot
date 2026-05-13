"""Delete SEMUA SCALP data — local SQLite + dokumentasi SQL untuk Neon Postgres.

DEFAULT: --dry-run mode (preview only, no action).
Untuk execute beneran: --execute flag.

WAJIB jalanin backup_scalp_data.py dulu sebelum delete!
"""
import os
import sys
import argparse
import sqlite3
import shutil
import glob
from datetime import datetime


def humansize(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def confirm_backup_exists():
    backups = glob.glob('scripts/scalp_backup_*')
    if not backups:
        print("ERROR: Backup tidak ditemukan! Run scripts/backup_scalp_data.py dulu.")
        return False
    latest = max(backups, key=os.path.getmtime)
    age_h = (datetime.now().timestamp() - os.path.getmtime(latest)) / 3600
    print(f"[OK] Backup found: {latest} (age {age_h:.1f} hours)")
    return True


def delete_files(file_list, dry_run=True):
    total_size = 0
    deleted = 0
    for pattern in file_list:
        for f in glob.glob(pattern):
            if not os.path.isfile(f):
                continue
            size = os.path.getsize(f)
            total_size += size
            if dry_run:
                print(f"  [DRY] would delete {f} ({humansize(size)})")
            else:
                os.remove(f)
                print(f"  [DEL] {f} ({humansize(size)})")
            deleted += 1
    return deleted, total_size


def delete_pycache(dry_run=True):
    """Hapus SCALP-related .pyc files."""
    deleted = 0
    pycache_dirs = glob.glob('**/__pycache__', recursive=True)
    for pd in pycache_dirs:
        for f in os.listdir(pd):
            if 'scalp' in f.lower():
                path = os.path.join(pd, f)
                if dry_run:
                    print(f"  [DRY] would delete {path}")
                else:
                    os.remove(path)
                deleted += 1
    return deleted


def delete_db_rows(db_path, where, dry_run=True):
    """Delete rows from SQLite by WHERE clause (no full file delete)."""
    if not os.path.exists(db_path):
        print(f"  [skip] {db_path} not found")
        return 0
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall() if not r[0].startswith('sqlite_')]
    total = 0
    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {t} WHERE {where}")
            n = cur.fetchone()[0]
            if n == 0:
                continue
            if dry_run:
                print(f"  [DRY] would delete {n} rows from {db_path}:{t} WHERE {where}")
            else:
                cur.execute(f"DELETE FROM {t} WHERE {where}")
                print(f"  [DEL] deleted {n} rows from {db_path}:{t}")
            total += n
        except Exception as e:
            print(f"  [{t}] skip ({e})")
    if not dry_run:
        conn.commit()
    conn.close()
    return total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true',
                        help='Actually delete (default: dry-run)')
    parser.add_argument('--skip-backup-check', action='store_true',
                        help='Skip check apakah backup exists (BERBAHAYA)')
    args = parser.parse_args()

    dry_run = not args.execute
    mode = 'DRY RUN' if dry_run else 'EXECUTE'

    print('=' * 70)
    print(f' SCALP DATA DELETE — Mode: {mode}')
    print('=' * 70)

    if not args.skip_backup_check:
        if not confirm_backup_exists():
            sys.exit(1)
    print()

    # ============================================================
    # 1. SCALP-specific database files
    # ============================================================
    print('== 1. Local SCALP DB files ==')
    scalp_db_files = [
        'data/scalp_trades.db',
        'data/scalp_paper.db',
    ]
    n1, s1 = delete_files(scalp_db_files, dry_run)
    print(f"  Total: {n1} files, {humansize(s1)}\n")

    # ============================================================
    # 2. SCALP config files
    # ============================================================
    print('== 2. SCALP config files ==')
    scalp_config_files = [
        'data/scalp_coin_params.json',
        'data/scalp_coin_params.json.backup',
    ]
    n2, s2 = delete_files(scalp_config_files, dry_run)
    print(f"  Total: {n2} files, {humansize(s2)}\n")

    # ============================================================
    # 3. Shared DB — SCALP rows only (engine='SCALP')
    # ============================================================
    print('== 3. unified_positions.db SCALP rows ==')
    n3 = delete_db_rows('data/unified_positions.db',
                        "engine='SCALP'", dry_run)
    print(f"  Total: {n3} rows\n")

    # ============================================================
    # 4. Backtest artifacts (caches + results)
    # ============================================================
    print('== 4. SCALP backtest artifacts ==')
    backtest_files = [
        'backtesting/results/scalp_backtest_*.pkl',
        'backtesting/cache/scalp_data*.pkl',
    ]
    n4, s4 = delete_files(backtest_files, dry_run)
    print(f"  Total: {n4} files, {humansize(s4)}\n")

    # ============================================================
    # 5. Pycache
    # ============================================================
    print('== 5. SCALP pycache ==')
    n5 = delete_pycache(dry_run)
    print(f"  Total: {n5} files\n")

    # ============================================================
    # 6. SCALP code files (will be deleted via git rm in commit)
    # ============================================================
    print('== 6. SCALP code files (DELETE via git rm) ==')
    scalp_code = [
        'scalping_signal_engine.py',
        'scalping_signal_engine.py.backup_pre_range_mode',
        'scalp_live_runner.py',
        'scalp_paper_trader.py',
        'scalp_session_filter.py',
        'scalp_trade_journal.py',
        'scalp_coin_learning.py',
        'main_scalp.py',
        'main_unified.py',
        'trading_engine_scalp.py',
        'backtest_scalp.py',
        'diag_scalp.py',
        'dashboard_api.py',
    ]
    for f in scalp_code:
        if os.path.exists(f):
            size = os.path.getsize(f)
            print(f"  [TODO git rm] {f} ({humansize(size)})")

    # ============================================================
    # 7. SCALP backup folder (jangan delete!)
    # ============================================================
    print()
    print('== 7. Backup folder (KEEP) ==')
    for b in glob.glob('scripts/scalp_backup_*'):
        print(f"  [KEEP] {b}")

    # ============================================================
    # 8. Web Postgres — Manual SQL
    # ============================================================
    print()
    print('== 8. Web Postgres (Neon) — Manual SQL ==')
    print('Login ke Neon console di https://console.neon.tech')
    print('atau pakai psql, lalu run query berikut:')
    print()
    print('  -- COUNT dulu (safety check)')
    print('  SELECT COUNT(*) FROM trades   WHERE strategy=\'scalp\';')
    print('  SELECT COUNT(*) FROM signals  WHERE strategy=\'scalp\';')
    print('  SELECT COUNT(*) FROM positions WHERE strategy=\'scalp\';')
    print()
    print('  -- DELETE (kalau count sesuai expectation)')
    print('  DELETE FROM trades   WHERE strategy=\'scalp\';')
    print('  DELETE FROM signals  WHERE strategy=\'scalp\';')
    print('  DELETE FROM positions WHERE strategy=\'scalp\';')
    print()
    print('Verify setelah delete:')
    print('  curl -s https://cryptovision-web.vercel.app/api/stats | python -m json.tool')

    # ============================================================
    # SUMMARY
    # ============================================================
    total_local_files = n1 + n2 + n4 + n5
    total_local_size = s1 + s2 + s4
    total_db_rows = n3

    print()
    print('=' * 70)
    print(f' SUMMARY — Mode: {mode}')
    print('=' * 70)
    print(f' Local files {"akan" if dry_run else "sudah"} dihapus: {total_local_files}')
    print(f' Local size  {"akan" if dry_run else "sudah"} dibebaskan: {humansize(total_local_size)}')
    print(f' DB rows     {"akan" if dry_run else "sudah"} dihapus: {total_db_rows}')
    print(f' Code files  TODO via git rm di commit: {sum(1 for f in scalp_code if os.path.exists(f))}')
    print(f' Web DELETE  TODO manual SQL di Neon console')
    print()
    if dry_run:
        print(' Untuk execute beneran: python scripts/delete_scalp_data.py --execute')
    else:
        print(' DELETE COMPLETE.')


if __name__ == '__main__':
    main()
