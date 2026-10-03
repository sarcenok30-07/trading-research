"""
Top-trader leaderboard adapters.

Pulls candidate wallets from Vybe, Birdeye, and/or Solana Tracker.
Each provider is optional — whichever API keys you supply in .env get
used, the rest are skipped. Results are normalized to a common shape
and merged, so downstream code doesn't care where a wallet came from.

WHY NOT GMGN / pump.fun / FOMO: those leaderboards are web UIs behind
Cloudflare with no public API. Scraping them violates their terms,
breaks whenever they change markup, and gets IP-blocked — which would
mean the bot silently following a stale wallet list. The three
providers here have documented APIs with keys.

IMPORTANT — I could not test these live. My sandbox can't reach these
hosts, so the endpoint paths, auth headers, and response shapes below
come from each provider's public docs and reference repos, not from a
verified call. Response parsing is written defensively (multiple
candidate field names, graceful skips), but treat the first live run
as the real test:

    python3 leaderboard.py test

That prints exactly what each configured provider returns, so you can
see whether parsing worked before any of it feeds the trading logic.
Base URLs are overridable in .env if a provider has moved.

SURVIVORSHIP BIAS WARNING: a "top trader today" leaderboard ranks who
got lucky today. In meme coins, the top daily PnL wallet is very often
someone who made one concentrated bet that hit — not someone with a
repeatable edge, and the trade that put them there is already over.
That's why copytrader.py scores wallets on consistency over many
trades and starts everyone in paper mode, rather than trusting the
leaderboard rank directly.
"""
import os
import sys
import json

import requests

from config import config

# Overridable in .env in case a provider changes hosts.
VYBE_BASE_URL = os.getenv("VYBE_BASE_URL", "https://api.vybenetwork.xyz").rstrip("/")
BIRDEYE_BASE_URL = os.getenv("BIRDEYE_BASE_URL", "https://public-api.birdeye.so").rstrip("/")
SOLANATRACKER_BASE_URL = os.getenv("SOLANATRACKER_BASE_URL", "https://data.solanatracker.io").rstrip("/")

REQUEST_TIMEOUT = 15


def _first_present(d: dict, *keys, default=None):
    """
    Providers rename fields between versions (realizedPnlUsd vs
    realized_pnl vs pnl). Rather than hard-coding one guess, try
    several and take the first that exists.
    """
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


def _as_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize(address: str, source: str, raw: dict) -> dict | None:
    """Common shape for a leaderboard entry, whatever provider it came from."""
    if not address or not isinstance(address, str) or len(address) < 32:
        return None
    win_rate = _as_float(_first_present(raw, "winRate", "win_rate", "winrate"), default=0.0)
    # Some providers express win rate as 0-1, others as 0-100. Normalize to 0-100.
    if 0 < win_rate <= 1:
        win_rate *= 100
    return {
        "address": address,
        "source": source,
        "realized_pnl_usd": _as_float(_first_present(
            raw, "realizedPnlUsd", "realized_pnl", "realizedPnl", "pnl", "total_pnl", "totalPnl")),
        "win_rate_pct": win_rate,
        "trades_count": int(_as_float(_first_present(
            raw, "tradesCount", "trades_count", "trades", "trade_count"))),
        "volume_usd": _as_float(_first_present(
            raw, "tradesVolumeUsd", "volume_usd", "volumeUsd", "volume", "totalVolumeUsd")),
        "raw": raw,
    }


def fetch_vybe_top_traders(limit: int = 50, resolution: str = "7d") -> list[dict]:
    """
    Vybe: GET /v4/wallets/top-traders — a genuine global wallet
    leaderboard (not scoped to one token), which makes it the most
    directly useful of the three.

    Defaults to 7d rather than 1d deliberately: a one-day leaderboard
    is mostly luck, a seven-day one at least requires repeat activity.
    """
    if not config.vybe_api_key:
        return []
    try:
        resp = requests.get(
            f"{VYBE_BASE_URL}/v4/wallets/top-traders",
            headers={"X-API-KEY": config.vybe_api_key, "accept": "application/json"},
            params={"resolution": resolution, "limit": limit, "sortByDesc": "realizedPnlUsd"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[leaderboard] Vybe fetch failed: {e}")
        return []

    entries = payload if isinstance(payload, list) else (
        payload.get("data") or payload.get("traders") or payload.get("results") or [])
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        addr = _first_present(entry, "accountAddress", "traderAddress", "address", "wallet", "owner")
        normalized = _normalize(addr, "vybe", entry)
        if normalized:
            out.append(normalized)
    return out


def fetch_birdeye_top_traders(token_addresses: list[str], limit: int = 10,
                              time_frame: str = "7d") -> list[dict]:
    """
    Birdeye: GET /defi/v2/tokens/top_traders — token-scoped, NOT a
    global leaderboard. It answers "who traded THIS token well", so it
    needs token addresses to query against.

    We feed it currently-trending tokens from price_feed, which gives
    a useful signal: wallets that did well on tokens that are hot right
    now. Costs one request per token, so keep the token list short.
    """
    if not config.birdeye_api_key or not token_addresses:
        return []

    out = []
    for token in token_addresses[:config.leaderboard_birdeye_max_tokens]:
        try:
            resp = requests.get(
                f"{BIRDEYE_BASE_URL}/defi/v2/tokens/top_traders",
                headers={
                    "X-API-KEY": config.birdeye_api_key,
                    "x-chain": "solana",
                    "accept": "application/json",
                },
                params={
                    "address": token,
                    "time_frame": time_frame,
                    "sort_by": "realized_pnl",
                    "sort_type": "desc",
                    "limit": limit,
                    "offset": 0,
                },
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 404:
                continue  # token not indexed, not an error worth logging
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[leaderboard] Birdeye fetch failed for {token[:8]}...: {e}")
            continue

        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        entries = data if isinstance(data, list) else (
            data.get("items") or data.get("traders") or [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            addr = _first_present(entry, "owner", "address", "wallet", "traderAddress")
            normalized = _normalize(addr, "birdeye", entry)
            if normalized:
                out.append(normalized)
    return out


def fetch_solanatracker_top_traders(limit: int = 50) -> list[dict]:
    """
    Solana Tracker: GET /top-traders/all — global PnL leaderboard.

    Their endpoint layout is the least documented of the three, so this
    is the most likely of the adapters to need adjustment. Run the
    test command to see what actually comes back.
    """
    if not config.solanatracker_api_key:
        return []
    try:
        resp = requests.get(
            f"{SOLANATRACKER_BASE_URL}/top-traders/all",
            headers={"x-api-key": config.solanatracker_api_key, "accept": "application/json"},
            params={"page": 1, "limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[leaderboard] Solana Tracker fetch failed: {e}")
        return []

    entries = payload if isinstance(payload, list) else (
        payload.get("wallets") or payload.get("data") or payload.get("traders") or [])
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        addr = _first_present(entry, "wallet", "address", "owner", "traderAddress")
        summary = entry.get("summary") if isinstance(entry.get("summary"), dict) else {}
        merged = {**summary, **entry}  # summary holds pnl/winrate on this provider
        normalized = _normalize(addr, "solanatracker", merged)
        if normalized:
            out.append(normalized)
    return out


def fetch_candidate_wallets(trending_token_addresses: list[str] | None = None) -> list[dict]:
    """
    Queries every configured provider and merges results, deduped by
    wallet address. When the same wallet appears on multiple
    leaderboards that's a meaningful signal in itself, so we keep a
    `sources` list rather than discarding duplicates silently.

    Applies minimum-quality filters (trade count, win rate) so
    one-lucky-bet wallets don't reach the scorer at all.
    """
    all_entries: list[dict] = []
    all_entries += fetch_vybe_top_traders()
    all_entries += fetch_solanatracker_top_traders()
    if trending_token_addresses:
        all_entries += fetch_birdeye_top_traders(trending_token_addresses)

    merged: dict[str, dict] = {}
    for entry in all_entries:
        addr = entry["address"]
        if addr in merged:
            merged[addr]["sources"].append(entry["source"])
            # Keep the higher trade count — more data is better evidence.
            if entry["trades_count"] > merged[addr]["trades_count"]:
                merged[addr].update({
                    "trades_count": entry["trades_count"],
                    "win_rate_pct": entry["win_rate_pct"],
                    "realized_pnl_usd": entry["realized_pnl_usd"],
                })
        else:
            merged[addr] = {**entry, "sources": [entry["source"]]}

    qualified = []
    for entry in merged.values():
        # A wallet with 3 trades and a huge PnL is one lucky bet, not
        # an edge. Require enough trades to mean something.
        if entry["trades_count"] < config.leaderboard_min_trades:
            continue
        # win_rate of 0 usually means the provider didn't return it,
        # so only filter when we actually got a value.
        if 0 < entry["win_rate_pct"] < config.leaderboard_min_win_rate_pct:
            continue
        if entry["realized_pnl_usd"] <= 0:
            continue
        qualified.append(entry)

    qualified.sort(key=lambda e: (len(e["sources"]), e["realized_pnl_usd"]), reverse=True)
    return qualified


def _test():
    """Prints raw results per provider so you can verify parsing before trusting it."""
    print("=" * 60)
    print("LEADERBOARD PROVIDER TEST")
    print("=" * 60)

    configured = []
    if config.vybe_api_key:
        configured.append("vybe")
    if config.birdeye_api_key:
        configured.append("birdeye")
    if config.solanatracker_api_key:
        configured.append("solanatracker")

    if not configured:
        print("No API keys configured. Set at least one of VYBE_API_KEY, "
              "BIRDEYE_API_KEY, SOLANATRACKER_API_KEY in .env.")
        return

    print(f"Configured providers: {', '.join(configured)}\n")

    if config.vybe_api_key:
        print("--- Vybe (global leaderboard) ---")
        results = fetch_vybe_top_traders(limit=5)
        print(f"parsed {len(results)} entries")
        for r in results[:3]:
            print(f"  {r['address'][:12]}... pnl=${r['realized_pnl_usd']:,.0f} "
                  f"win={r['win_rate_pct']:.0f}% trades={r['trades_count']}")
        if not results:
            print("  (nothing parsed — either the key is invalid, the endpoint "
                  "moved, or the response shape differs from what this adapter expects)")
        print()

    if config.solanatracker_api_key:
        print("--- Solana Tracker (global leaderboard) ---")
        results = fetch_solanatracker_top_traders(limit=5)
        print(f"parsed {len(results)} entries")
        for r in results[:3]:
            print(f"  {r['address'][:12]}... pnl=${r['realized_pnl_usd']:,.0f} "
                  f"win={r['win_rate_pct']:.0f}% trades={r['trades_count']}")
        if not results:
            print("  (nothing parsed — see note above)")
        print()

    if config.birdeye_api_key:
        print("--- Birdeye (token-scoped) ---")
        try:
            from price_feed import fetch_solana_pairs
            pairs = fetch_solana_pairs()
            tokens = [p["baseToken"]["address"] for p in pairs[:3] if p.get("baseToken")]
        except Exception as e:
            print(f"  could not get trending tokens to query: {e}")
            tokens = []
        if tokens:
            results = fetch_birdeye_top_traders(tokens, limit=5)
            print(f"parsed {len(results)} entries across {len(tokens)} tokens")
            for r in results[:3]:
                print(f"  {r['address'][:12]}... pnl=${r['realized_pnl_usd']:,.0f} "
                      f"trades={r['trades_count']}")
        print()

    print("--- Merged + quality-filtered ---")
    try:
        from price_feed import fetch_solana_pairs
        pairs = fetch_solana_pairs()
        tokens = [p["baseToken"]["address"] for p in pairs[:3] if p.get("baseToken")]
    except Exception:
        tokens = []
    final = fetch_candidate_wallets(tokens)
    print(f"{len(final)} wallets passed filters "
          f"(min {config.leaderboard_min_trades} trades, "
          f"min {config.leaderboard_min_win_rate_pct}% win rate, positive PnL)")
    for w in final[:10]:
        print(f"  {w['address'][:12]}... sources={w['sources']} "
              f"pnl=${w['realized_pnl_usd']:,.0f} win={w['win_rate_pct']:.0f}% "
              f"trades={w['trades_count']}")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        _test()
    else:
        print("Usage: python3 leaderboard.py test")
