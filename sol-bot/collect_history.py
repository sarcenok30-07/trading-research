"""
Standalone data collector for backtest.py.

Since there's no free historical API for Solana meme coin prices, this
script builds one the only honest way: polling DexScreener live and
appending to a CSV, same as the bot already does in memory, but
persisted to disk so you can accumulate real history over days/weeks
and eventually backtest strategy changes against real market behavior
instead of guessing.

Run standalone with:
    python3 collect_history.py

Or, to run unattended in the background with auto-restart on crash,
use run_collector.sh (see README) instead of running this directly.

Writes to price_history.csv, rotating to a new dated file at the start
of each day so no single file grows unbounded over weeks of collection.
Also writes collector.log so you can check on it without an attached
terminal. Ctrl+C to stop when running in the foreground.
"""
import time
import csv
import logging
from pathlib import Path
from datetime import datetime, timezone

from price_feed import fetch_solana_pairs

POLL_INTERVAL_SECONDS = 30
LOG_FILE = Path("collector.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [collect_history] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def _current_output_file() -> Path:
    """Rotates to a new file per UTC day so long-running collection
    doesn't produce one unbounded CSV — easier to manage, and you can
    delete/archive old days independently."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Path(f"price_history_{date_str}.csv")


def append_snapshot(pairs: list[dict]):
    output_file = _current_output_file()
    is_new_file = not output_file.exists()
    with open(output_file, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["timestamp", "pair_address", "symbol", "price_usd", "volume_24h_usd"])
        now = time.time()
        written = 0
        for pair in pairs:
            try:
                writer.writerow([
                    now,
                    pair["pairAddress"],
                    pair.get("baseToken", {}).get("symbol", "?"),
                    float(pair["priceUsd"]),
                    float(pair.get("volume", {}).get("h24", 0)),
                ])
                written += 1
            except (KeyError, TypeError, ValueError):
                continue
    return written, output_file


def run():
    log.info(f"starting, polling every {POLL_INTERVAL_SECONDS}s, "
             f"writing to price_history_<date>.csv (rotates daily)")
    consecutive_failures = 0
    while True:
        try:
            pairs = fetch_solana_pairs()
            if not pairs:
                consecutive_failures += 1
                log.warning(f"empty result from fetch_solana_pairs "
                            f"({consecutive_failures} consecutive)")
            else:
                consecutive_failures = 0
                written, output_file = append_snapshot(pairs)
                log.info(f"snapshot written: {written} pairs -> {output_file}")
        except Exception as e:
            consecutive_failures += 1
            log.error(f"error during poll cycle: {e}")

        if consecutive_failures >= 10:
            log.error("10 consecutive failures — DexScreener may be down "
                      "or network unreachable. Still retrying, but check "
                      "your connection if this continues.")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("stopped by user (Ctrl+C)")

