"""
Standalone stats dashboard. Run with:

    python3 stats.py

Reads trade_log.json (written by wallet_security.log_trade) and prints
win rate, average win/loss, profit factor, and other metrics needed to
actually judge whether the strategy works — rather than eyeballing
scrolling terminal output.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

TRADE_LOG_FILE = Path("trade_log.json")


def load_events() -> list[dict]:
    if not TRADE_LOG_FILE.exists():
        print("No trade_log.json found yet — nothing to report on.")
        return []
    try:
        return json.loads(TRADE_LOG_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Could not read trade_log.json: {e}")
        return []


def compute_stats(events: list[dict]) -> dict:
    closes = [e for e in events if e.get("type") == "swap_close" or (e.get("type") == "swap" and "pnl_sol" in e)]
    # trader.py logs closes via the "closed position" print but not yet
    # as a dedicated log_trade event with pnl — pull pnl from the
    # dedicated pnl-bearing entries if present, else fall back gracefully.
    pnl_events = [e for e in events if "pnl_sol" in e]

    wins = [e["pnl_sol"] for e in pnl_events if e["pnl_sol"] > 0]
    losses = [e["pnl_sol"] for e in pnl_events if e["pnl_sol"] <= 0]

    total_trades = len(pnl_events)
    win_rate = (len(wins) / total_trades * 100) if total_trades else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    total_pnl = sum(wins) + sum(losses)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    blocked = len([e for e in events if e.get("type") == "blocked"])
    skipped = len([e for e in events if e.get("type") == "skipped"])
    declined = len([e for e in events if e.get("type") == "declined"])

    # Simple running-balance drawdown from realized pnl sequence
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for e in sorted(pnl_events, key=lambda x: x.get("timestamp", 0)):
        running += e["pnl_sol"]
        peak = max(peak, running)
        max_drawdown = min(max_drawdown, running - peak)

    return {
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate,
        "avg_win_sol": avg_win,
        "avg_loss_sol": avg_loss,
        "total_pnl_sol": total_pnl,
        "profit_factor": profit_factor,
        "max_drawdown_sol": max_drawdown,
        "blocked_count": blocked,
        "skipped_count": skipped,
        "declined_count": declined,
    }


def print_report(stats: dict, events: list[dict]):
    print("=" * 50)
    print("SOL BOT TRADE STATS")
    print("=" * 50)

    if stats["total_trades"] == 0:
        print("No closed trades with recorded PnL yet.")
    else:
        print(f"Total closed trades:  {stats['total_trades']}")
        print(f"Win rate:             {stats['win_rate_pct']:.1f}%  "
              f"({stats['wins']}W / {stats['losses']}L)")
        print(f"Avg win:              {stats['avg_win_sol']:+.4f} SOL")
        print(f"Avg loss:             {stats['avg_loss_sol']:+.4f} SOL")
        print(f"Total realized PnL:   {stats['total_pnl_sol']:+.4f} SOL")
        pf = stats["profit_factor"]
        print(f"Profit factor:        {'inf' if pf == float('inf') else f'{pf:.2f}'}")
        print(f"Max drawdown:         {stats['max_drawdown_sol']:.4f} SOL")

    print()
    print(f"Trades blocked by safety guards: {stats['blocked_count']}")
    print(f"Candidates skipped (rug/honeypot/liquidity): {stats['skipped_count']}")
    print(f"Trades declined at manual confirmation: {stats['declined_count']}")
    print("=" * 50)

    if events:
        last = sorted(events, key=lambda x: x.get("timestamp", 0))[-1]
        ts = datetime.fromtimestamp(last.get("timestamp", 0), tz=timezone.utc)
        print(f"Last event: {last.get('type')} at {ts.isoformat()}")


if __name__ == "__main__":
    events = load_events()
    if events:
        stats = compute_stats(events)
        print_report(stats, events)
