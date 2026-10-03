"""
Backtesting harness.

Important limitation, stated up front: DexScreener's free API only
exposes CURRENT price/volume snapshots, not historical candles. There
is no free API this bot can hit to get real historical meme coin data.
So this harness does NOT fetch anything — it replays a CSV you supply.

Two realistic ways to get that CSV:
  1. Run the bot in DRY_RUN mode for a while and use the price history
     it's been recording (see `export_price_history.py`-style export
     below, or just periodically snapshot trade_log.json).
  2. Buy access to a historical data provider (Birdeye, Bitquery, etc.
     have paid historical OHLCV APIs for Solana tokens) and export to
     the CSV format below.

Expected CSV format (one row per price observation):
    timestamp,pair_address,symbol,price_usd,volume_24h_usd

Usage (single file or a glob pattern for multiple daily files from
collect_history.py's rotation):
    python3 backtest.py price_history_2026-08-26.csv
    python3 backtest.py "price_history_*.csv"

This replays the same breakout/confirmation/volume logic used live
(mirrored here in simplified form, since the live version pulls from
a live API) against your data and reports the same stats stats.py
would show for a live run.
"""
import sys
import csv
import glob
import gzip
from collections import defaultdict

from config import config


def _open_maybe_gzip(path: str):
    """maintenance.py gzips history older than a couple of days, so a
    plain glob would silently miss most of your dataset. Handle both."""
    if path.endswith(".gz"):
        return gzip.open(path, "rt", newline="")
    return open(path, newline="")


def load_csv(path_or_pattern: str) -> list[dict]:
    files = sorted(glob.glob(path_or_pattern))
    # A bare "price_history_*.csv" pattern won't match the compressed
    # archives, so pick those up too.
    if path_or_pattern.endswith(".csv"):
        files += sorted(glob.glob(path_or_pattern + ".gz"))
    files = sorted(set(files)) or [path_or_pattern]

    rows = []
    for path in files:
        try:
            with _open_maybe_gzip(path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        rows.append({
                            "timestamp": float(row["timestamp"]),
                            "pair_address": row["pair_address"],
                            "symbol": row.get("symbol", "?"),
                            "price_usd": float(row["price_usd"]),
                            "volume_24h_usd": float(row["volume_24h_usd"]),
                        })
                    except (KeyError, ValueError) as e:
                        print(f"[backtest] skipping malformed row in {path}: {e}")
        except OSError as e:
            print(f"[backtest] could not read {path}: {e}")
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def _close(trade_log_raw, trade_log_net, pct_move):
    trade_log_raw.append(pct_move)
    trade_log_net.append(pct_move - config.backtest_roundtrip_cost_pct)


def run_backtest(rows: list[dict], strategy: str = "breakout") -> dict:
    """
    Replays rows in timestamp order against one of three hypotheses,
    using identical entry filters (volume floor, confirmation polls) and
    identical exit rules (stop-loss, take-profit, trailing stop) so the
    results are directly comparable.

        breakout   — buy after a rise of BREAKOUT_PCT. The live strategy.
        reversion  — buy after a FALL of BREAKOUT_PCT, betting the drop
                     over-shot. Implementable exactly like breakout.
        short_fade — the breakout trigger, but P&L inverted: profit when
                     the price falls after the pump.

    IMPORTANT about short_fade: it is a HYPOTHESIS TEST, not a strategy
    you can run. There is no practical way to short a small-cap Solana
    meme coin — no borrow market exists for them. If it scores well that
    tells you the breakout signal is genuinely inverted (useful
    knowledge), but you could not trade it as written. Do not read a
    good short_fade result as "profits available."

    No rug-check, honeypot, or liquidity-depth screening is simulated —
    those need live API calls. Results are therefore optimistic relative
    to live trading for every strategy here.
    """
    if strategy not in ("breakout", "reversion", "short_fade"):
        raise ValueError(f"unknown strategy: {strategy}")

    price_history = defaultdict(list)
    confirmation_counts = defaultdict(int)
    open_positions = {}
    closed_raw, closed_net = [], []
    hold_times = []

    inverted = (strategy == "short_fade")

    for row in rows:
        pair = row["pair_address"]
        ts = row["timestamp"]
        price = row["price_usd"]

        price_history[pair].append((ts, price))
        cutoff = ts - (config.breakout_lookback_minutes * 60)
        price_history[pair] = [(t, p) for t, p in price_history[pair] if t >= cutoff]

        # --- exits ---
        if pair in open_positions:
            pos = open_positions[pair]
            entry = pos["entry_price"]

            # For a short, gains come from price falling, so flip the sign
            # of the move and track the trough instead of the peak.
            raw_move = ((price - entry) / entry) * 100
            pct_move = -raw_move if inverted else raw_move

            if inverted:
                if price < pos["extreme_price"]:
                    pos["extreme_price"] = price
                pct_off_best = -(((price - pos["extreme_price"]) / pos["extreme_price"]) * 100)
            else:
                if price > pos["extreme_price"]:
                    pos["extreme_price"] = price
                pct_off_best = ((price - pos["extreme_price"]) / pos["extreme_price"]) * 100

            hit = None
            if pct_move <= -config.stop_loss_pct:
                hit = "stop"
            elif config.trailing_stop_enabled and pct_move >= config.trailing_activation_pct:
                pos["trailing_active"] = True

            if hit is None and pos.get("trailing_active") and pct_off_best <= -config.trailing_stop_pct:
                hit = "trail"
            if hit is None and pct_move >= config.take_profit_pct:
                hit = "target"

            if hit:
                _close(closed_raw, closed_net, pct_move)
                hold_times.append(ts - pos["opened_at"])
                del open_positions[pair]
                continue

        # --- entries ---
        if row["volume_24h_usd"] < config.min_volume_usd:
            confirmation_counts[pair] = 0
            continue

        history = price_history[pair]
        if len(history) < 2:
            continue
        oldest_price = history[0][1]
        if oldest_price <= 0:
            continue
        pct_change = ((price - oldest_price) / oldest_price) * 100

        if strategy == "reversion":
            triggered = pct_change <= -config.breakout_pct
        else:
            triggered = pct_change >= config.breakout_pct

        if triggered:
            confirmation_counts[pair] += 1
        else:
            confirmation_counts[pair] = 0
            continue

        if confirmation_counts[pair] >= config.confirmation_polls and pair not in open_positions:
            if len(open_positions) < config.max_open_positions:
                open_positions[pair] = {
                    "entry_price": price,
                    "extreme_price": price,
                    "trailing_active": False,
                    "opened_at": ts,
                }

    return _summarize(strategy, closed_net, closed_raw, len(open_positions), hold_times)


def _summarize(strategy, net, raw, still_open, hold_times) -> dict:
    total = len(net)
    wins = [t for t in net if t > 0]
    losses = [t for t in net if t <= 0]
    win_rate = (len(wins) / total * 100) if total else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0

    # Expectancy per trade is the number that actually decides whether a
    # strategy makes money. Win rate alone says nothing without payoff.
    expectancy = (sum(net) / total) if total else 0.0

    # The win rate this payoff ratio would need just to break even.
    breakeven_wr = None
    if avg_win > 0 and avg_loss < 0:
        breakeven_wr = (abs(avg_loss) / (avg_win + abs(avg_loss))) * 100

    # Rough 95% confidence band on the win rate, so a small sample can't
    # masquerade as a finding.
    stderr_wr = None
    if total > 0:
        p = win_rate / 100
        stderr_wr = ((p * (1 - p) / total) ** 0.5) * 100

    return {
        "strategy": strategy,
        "total_trades": total,
        "still_open_at_end": still_open,
        "win_rate_pct": win_rate,
        "win_rate_stderr": stderr_wr,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "expectancy_pct": expectancy,
        "breakeven_win_rate_pct": breakeven_wr,
        "total_pct_sum": sum(net),
        "total_pct_sum_raw": sum(raw),
        "avg_hold_minutes": (sum(hold_times) / len(hold_times) / 60) if hold_times else 0.0,
        "roundtrip_cost_pct_used": config.backtest_roundtrip_cost_pct,
    }


def print_report(stats: dict):
    s = stats
    print("=" * 58)
    print(f"BACKTEST — {s['strategy'].upper()}")
    print("=" * 58)
    print(f"Modeling ~{s['roundtrip_cost_pct_used']}% round-trip cost per trade.")
    print("No rug/honeypot/liquidity screening simulated, so live results")
    print("would likely be worse than these numbers.")
    print()
    if s["total_trades"] == 0:
        print("No trades triggered on this dataset with current config.")
        print("=" * 58)
        return
    print(f"Total trades:          {s['total_trades']}")
    print(f"Still open at end:     {s['still_open_at_end']}")
    print(f"Avg hold:              {s['avg_hold_minutes']:.0f} min")
    print()
    wr = f"{s['win_rate_pct']:.1f}%"
    if s["win_rate_stderr"]:
        lo = s["win_rate_pct"] - 1.96 * s["win_rate_stderr"]
        hi = s["win_rate_pct"] + 1.96 * s["win_rate_stderr"]
        wr += f"   (95% range {max(0,lo):.1f}–{min(100,hi):.1f}%)"
    print(f"Win rate:              {wr}")
    if s["breakeven_win_rate_pct"] is not None:
        print(f"Breakeven needs:       {s['breakeven_win_rate_pct']:.1f}%")
    print(f"Avg win:               {s['avg_win_pct']:+.2f}%")
    print(f"Avg loss:              {s['avg_loss_pct']:+.2f}%")
    print()
    print(f"EXPECTANCY PER TRADE:  {s['expectancy_pct']:+.2f}%   <-- the number that matters")
    print(f"Sum net of costs:      {s['total_pct_sum']:+.2f}%")
    print(f"Sum raw (no costs):    {s['total_pct_sum_raw']:+.2f}%  (optimistic, ignore alone)")
    print("=" * 58)


def print_comparison(results: list[dict]):
    print()
    print("=" * 78)
    print("SIDE BY SIDE — same data, same exits, only the entry rule differs")
    print("=" * 78)
    print(f"{'strategy':<12} {'trades':>7} {'win%':>7} {'breakeven%':>11} "
          f"{'expectancy':>11} {'net sum':>10}")
    print("-" * 78)
    for r in results:
        be = f"{r['breakeven_win_rate_pct']:.1f}" if r["breakeven_win_rate_pct"] else "n/a"
        print(f"{r['strategy']:<12} {r['total_trades']:>7} {r['win_rate_pct']:>6.1f}% "
              f"{be:>11} {r['expectancy_pct']:>+10.2f}% {r['total_pct_sum']:>+9.1f}%")
    print("-" * 78)

    tradeable = [r for r in results if r["strategy"] != "short_fade" and r["total_trades"] > 0]
    best = max(tradeable, key=lambda r: r["expectancy_pct"]) if tradeable else None

    print()
    if best and best["expectancy_pct"] > 0:
        print(f"Best tradeable expectancy: {best['strategy']} at "
              f"{best['expectancy_pct']:+.2f}% per trade.")
        print("Before trusting it, note this was chosen after looking at the")
        print("results. Testing several rules on one dataset means the winner")
        print("is partly luck. Confirm it on data collected AFTER today before")
        print("risking money on it.")
    else:
        print("No tradeable strategy shows positive expectancy on this data.")
        print("That is a real finding, not a failure — it says the signal is")
        print("not there, and no amount of execution tuning fixes that.")

    fade = next((r for r in results if r["strategy"] == "short_fade"), None)
    if fade and fade["expectancy_pct"] > 0 and fade["total_trades"] > 0:
        print()
        print("Note: short_fade scored positive, meaning the breakout signal is")
        print("inverted — these pumps tend to fade. That is useful to know, but")
        print("it is NOT directly tradeable: there is no practical borrow market")
        print("for small-cap Solana meme coins, so you cannot short them.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    if not args:
        print("Usage:")
        print("  python3 backtest.py <data.csv | 'pattern_*.csv'>            # compare all")
        print("  python3 backtest.py <data> --strategy=reversion             # just one")
        print()
        print("Strategies: breakout (live rule), reversion (buy the dip),")
        print("            short_fade (hypothesis test only — not tradeable)")
        sys.exit(1)

    rows = load_csv(args[0])
    if not rows:
        print("No usable rows loaded.")
        sys.exit(1)

    print(f"[backtest] loaded {len(rows)} price observations across "
          f"{len(set(r['pair_address'] for r in rows))} pairs")

    chosen = None
    for f in flags:
        if f.startswith("--strategy="):
            chosen = f.split("=", 1)[1]

    if chosen:
        print_report(run_backtest(rows, chosen))
    else:
        results = []
        for strat in ("breakout", "reversion", "short_fade"):
            r = run_backtest(rows, strat)
            print_report(r)
            print()
            results.append(r)
        print_comparison(results)
