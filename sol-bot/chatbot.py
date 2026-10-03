"""
Scripted chat assistant for the dashboard. Deliberately NOT an LLM —
no API calls, no per-query cost, nothing to run out of credits. It
answers by keyword-matching the question against a fixed set of
intents and pulling real numbers from the dashboard's own data
(price_history_*.csv, trade_log.json if it exists yet).

This means it can only answer questions it's been taught to recognize
— it won't handle open-ended novel phrasing the way a real LLM would.
That's the deliberate tradeoff for zero cost. If you ever want a real
LLM-backed version instead, that's a separate, small addition (an API
key + a few lines proxying to it) — ask for it explicitly.
"""
import json
import time
from pathlib import Path

from config import config

GLOSSARY = {
    "1h change": "The % price move for a token over the last hour, based on what the collector has actually observed — not DexScreener's own 24h figure.",
    "volume": "24-hour trading volume in USD for that token, as reported by DexScreener.",
    "pairs tracked": "The number of distinct token/liquidity-pool pairs the collector has seen data for recently.",
    "snapshot": "One poll of DexScreener's data, taken every 30 seconds. Each snapshot records price and volume for every pair seen that cycle.",
    "breakout": "The bot's trading trigger: a token whose price rose more than a configured % over a lookback window, with enough volume to filter out one-wallet pumps.",
    "rug check": "A screening step (rugcheck.py) that checks mint/freeze authority and liquidity locks before the bot will trade a token — refuses tokens that look like an obvious rug.",
    "honeypot": "A token that can be bought but not sold. honeypot.py checks for a working sell route before the bot buys anything.",
    "slippage": "The tolerance for price movement between quoting a trade and it executing. The bot scales this with a token's volatility.",
    "trailing stop": "An exit that rises with the price and locks in gains, instead of a fixed take-profit target.",
    "dry run": "A mode where the bot logs what it would trade without sending any real transaction. This is the default and safest way to test the strategy.",
}


def _load_summary_data():
    """Imports lazily to avoid a circular import with dashboard.py."""
    from dashboard import load_recent_rows, compute_summary
    rows = load_recent_rows()
    return compute_summary(rows)


def _load_trade_stats():
    """Reads trade_log.json if the bot has actually run live/monitor
    mode and logged anything. Returns None if it doesn't exist yet —
    that's the normal state before the bot has ever traded."""
    path = Path("trade_log.json")
    if not path.exists():
        return None
    try:
        events = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    pnl_events = [e for e in events if "pnl_sol" in e]
    if not pnl_events:
        return {"total_trades": 0, "events_logged": len(events)}

    wins = [e["pnl_sol"] for e in pnl_events if e["pnl_sol"] > 0]
    losses = [e["pnl_sol"] for e in pnl_events if e["pnl_sol"] <= 0]
    return {
        "total_trades": len(pnl_events),
        "win_rate_pct": (len(wins) / len(pnl_events) * 100) if pnl_events else 0,
        "total_pnl_sol": sum(wins) + sum(losses),
        "events_logged": len(events),
    }


def _contains_any(text: str, phrases: list[str]) -> bool:
    return any(p in text for p in phrases)


def answer_question(question: str) -> str:
    q = question.lower().strip()

    if not q:
        return "Ask me something about the collector — try 'is it running?' or 'what's the top mover?'"

    # Greeting
    if q in ("hi", "hello", "hey", "yo", "sup") or q.startswith(("hi ", "hello ", "hey ")):
        return ("Hey! I can tell you about the collector's status, top movers, "
                "how much data it's gathered, and whether the bot has traded "
                "anything yet. What do you want to know?")

    # Help
    if _contains_any(q, ["help", "what can you", "what can i ask", "commands", "what do you do"]):
        return ("I can answer questions like: 'is it running?', 'what's the top "
                "mover?', 'how many pairs are tracked?', 'when was the last "
                "update?', 'has it traded anything?', and 'what does [term] "
                "mean?' for terms like breakout, rug check, honeypot, slippage, "
                "trailing stop, or dry run.")

    # Glossary lookups — check before other intents since "what is X" could
    # otherwise get caught by a broader pattern
    for term, definition in GLOSSARY.items():
        if term in q:
            return f"{term.capitalize()}: {definition}"

    summary = _load_summary_data()

    # Status / running
    if _contains_any(q, ["is it running", "is the collector running", "still running",
                          "alive", "status", "working"]):
        if summary["last_update"] is None:
            return "No data yet — the collector may not have started, or hasn't completed its first poll."
        stale = summary["seconds_since_update"] > 120
        if stale:
            return (f"It looks stale — last update was {int(summary['seconds_since_update'])} "
                     f"seconds ago. Worth checking `systemctl status sol-bot-collector` on the droplet.")
        return f"Yes, running normally. Last update was {int(summary['seconds_since_update'])} seconds ago."

    # Last update
    if _contains_any(q, ["last update", "how fresh", "how recent", "when did it last"]):
        if summary["last_update"] is None:
            return "No data collected yet."
        return f"Last snapshot was {int(summary['seconds_since_update'])} seconds ago."

    # Pairs tracked
    if _contains_any(q, ["how many pairs", "how many tokens", "what are you tracking",
                          "pairs tracked", "tokens tracked"]):
        return f"Currently tracking {summary['pairs_tracked']} pairs with recent data."

    # Snapshot / data volume
    if _contains_any(q, ["how many snapshots", "how much data", "data collected", "data gathered"]):
        return f"{summary['total_snapshots']} recent snapshots in the loaded window (last few days of files)."

    # Top gainer / mover
    if _contains_any(q, ["top gainer", "biggest gain", "pumping", "best performer",
                          "top mover", "top movers", "what's up", "whats up"]):
        # Movers arrive sorted by proximity to the breakout trigger, not
        # by price change, so sort explicitly here.
        movers = [m for m in summary["movers"] if m.get("change_1h_pct") is not None]
        if not movers:
            return "No movers yet — needs more data before it can compute 1h changes."
        top = max(movers, key=lambda m: m["change_1h_pct"])
        return (f"Top mover right now: {top['symbol']}, "
                f"{'+' if top['change_1h_pct'] >= 0 else ''}{top['change_1h_pct']:.1f}% "
                f"over the last hour, price ${top['price_usd']:.6f}.")

    # Top loser
    if _contains_any(q, ["top loser", "biggest drop", "dumping", "worst performer"]):
        movers = [m for m in summary["movers"] if m.get("change_1h_pct") is not None]
        if not movers:
            return "No movers yet — needs more data before it can compute 1h changes."
        bottom = min(movers, key=lambda m: m["change_1h_pct"])
        return (f"Biggest drop right now: {bottom['symbol']}, "
                f"{bottom['change_1h_pct']:.1f}% over the last hour, "
                f"price ${bottom['price_usd']:.6f}.")

    # Proximity to the breakout trigger
    if _contains_any(q, ["close to", "about to", "trigger", "firing", "fire", "buy soon",
                          "near trigger", "armed"]):
        movers = [m for m in summary["movers"] if m.get("gap_to_trigger_pct") is not None]
        if not movers:
            return ("Not enough samples yet to measure any token against the "
                    "breakout rule.")
        firing = [m for m in movers if m.get("armed")]
        if firing:
            names = ", ".join(m["symbol"] for m in firing[:5])
            return (f"{len(firing)} token(s) currently meet the breakout rule: {names}. "
                    f"In dry run the bot logs these instead of buying.")
        closest = min(movers, key=lambda m: m["gap_to_trigger_pct"])
        return (f"Nothing meets the rule right now. Closest is {closest['symbol']}, "
                f"{closest['gap_to_trigger_pct']:.1f} percentage points short of the "
                f"{config.breakout_pct}% threshold.")

    # Trading status
    if _contains_any(q, ["is it trading", "has it traded", "any trades", "made money",
                          "profit", "pnl", "win rate", "made any trades"]):
        stats = _load_trade_stats()
        if stats is None:
            return ("No trades yet — the bot hasn't been run live or in monitor "
                     "mode. Right now this is just the price collector gathering "
                     "history for backtesting.")
        if stats["total_trades"] == 0:
            return f"No closed trades with recorded PnL yet ({stats['events_logged']} other events logged, e.g. skips/blocks)."
        return (f"{stats['total_trades']} closed trades, "
                f"{stats['win_rate_pct']:.1f}% win rate, "
                f"{stats['total_pnl_sol']:+.4f} SOL total realized PnL.")

    # Fallback
    return ("I didn't recognize that one — I can answer questions about "
            "collector status, top movers, data volume, trading stats, or "
            "explain terms like breakout/rug check/honeypot/slippage/trailing "
            "stop/dry run. Try rephrasing, or ask 'help'.")
