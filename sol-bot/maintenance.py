"""
Housekeeping: compress old data, rotate logs, prune by retention.

Run daily (see the cron line in README). Without this, three things
grow without limit on a 10GB droplet:
  - price_history_<date>.csv, one per day, forever
  - collector.log / collector_wrapper.log
  - trade_log.json

What it does:
  1. Gzips price_history CSVs older than 2 days (they compress ~80%,
     and backtest.py can still read them after you gunzip).
  2. Deletes compressed history older than BACKUP_RETENTION_DAYS.
  3. Truncates log files that have grown past a size cap, keeping the
     most recent lines rather than deleting outright.
  4. Trims trade_log.json to the most recent entries if it gets large.

Honest scope note: this is local housekeeping on one droplet, NOT
offsite backup. If the droplet dies, the data dies with it. For real
backups, either enable DigitalOcean's snapshot feature (small monthly
cost) or periodically scp the files to your Mac:

    scp root@YOUR_DROPLET_IP:~/sol-bot/price_history_*.csv* ~/Downloads/sol-bot-backups/

Run with:
    python3 maintenance.py
"""
import gzip
import json
import shutil
import time
from pathlib import Path

from config import config

COMPRESS_AFTER_DAYS = 2
MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB
KEEP_LOG_LINES = 5000
MAX_TRADE_LOG_ENTRIES = 10000

LOG_FILES = ["collector.log", "collector_wrapper.log"]


def compress_old_history() -> int:
    """Gzips price_history CSVs older than COMPRESS_AFTER_DAYS."""
    cutoff = time.time() - (COMPRESS_AFTER_DAYS * 86400)
    compressed = 0
    for path in Path(".").glob("price_history_*.csv"):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            gz_path = path.with_suffix(".csv.gz")
            with open(path, "rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()
            compressed += 1
            print(f"[maintenance] compressed {path.name} -> {gz_path.name}")
        except OSError as e:
            print(f"[maintenance] could not compress {path.name}: {e}")
    return compressed


def prune_old_archives() -> int:
    """Deletes compressed history past the retention window."""
    cutoff = time.time() - (config.backup_retention_days * 86400)
    removed = 0
    for path in Path(".").glob("price_history_*.csv.gz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
                print(f"[maintenance] pruned {path.name} "
                      f"(older than {config.backup_retention_days} days)")
        except OSError as e:
            print(f"[maintenance] could not prune {path.name}: {e}")
    return removed


def rotate_logs() -> int:
    """Truncates oversized logs, keeping the most recent lines."""
    rotated = 0
    for name in LOG_FILES:
        path = Path(name)
        try:
            if not path.exists() or path.stat().st_size <= MAX_LOG_BYTES:
                continue
            with open(path, "r", errors="replace") as f:
                lines = f.readlines()
            with open(path, "w") as f:
                f.writelines(lines[-KEEP_LOG_LINES:])
            rotated += 1
            print(f"[maintenance] rotated {name}, kept last {KEEP_LOG_LINES} lines")
        except OSError as e:
            print(f"[maintenance] could not rotate {name}: {e}")
    return rotated


def trim_trade_log() -> bool:
    """
    Trims trade_log.json to the most recent entries. Deliberately
    conservative: keeps 10k entries, which is far more than stats.py
    needs but preserves a long audit trail.
    """
    path = Path("trade_log.json")
    if not path.exists():
        return False
    try:
        events = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"[maintenance] could not read trade_log.json: {e}")
        return False

    if len(events) <= MAX_TRADE_LOG_ENTRIES:
        return False

    events.sort(key=lambda e: e.get("timestamp", 0))
    trimmed = events[-MAX_TRADE_LOG_ENTRIES:]
    # Keep the pre-trim copy once, so a bad trim isn't silently lossy.
    shutil.copy(path, path.with_suffix(".json.bak"))
    path.write_text(json.dumps(trimmed, indent=2))
    print(f"[maintenance] trimmed trade_log.json from {len(events)} "
          f"to {len(trimmed)} entries (backup at trade_log.json.bak)")
    return True


def disk_usage_summary():
    total = sum(p.stat().st_size for p in Path(".").glob("price_history_*") if p.is_file())
    print(f"[maintenance] price history now using {total / (1024*1024):.1f} MB")


if __name__ == "__main__":
    print("[maintenance] starting housekeeping run")
    compress_old_history()
    prune_old_archives()
    rotate_logs()
    trim_trade_log()
    disk_usage_summary()
    print("[maintenance] done")
