"""
Polls DexScreener's public API for Solana pairs and flags breakout
candidates: tokens whose price rose more than BREAKOUT_PCT over the
lookback window, with enough volume to not be a one-wallet pump.

Includes multi-timeframe confirmation (a breakout must hold across
consecutive polls, not just appear once — filters out single-candle
wicks) and a SOL market-trend check (skip new buys when SOL itself is
dumping, since most meme coins are highly correlated to it).

DexScreener's API is free and requires no key, but it's a third-party
service — rate limits and schema can change. This is not a guarantee
of data freshness or accuracy; always sanity-check before trusting it
with real money.
"""
import time
import requests
from config import config

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"
DEXSCREENER_DISCOVERY_URLS = [
    "https://api.dexscreener.com/token-boosts/top/v1",
    "https://api.dexscreener.com/token-boosts/latest/v1",
    "https://api.dexscreener.com/token-profiles/latest/v1",
]
SOL_USDC_PAIR_QUERY = "SOL USDC"

# Base tokens that are quote/major currencies, not tradeable meme coin
# candidates. Without this filter the feed fills up with dead pairs
# where SOL itself is the base token — which is exactly the bug that
# made the original "search for solana" approach useless.
EXCLUDED_BASE_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT", "USDH", "WBTC", "WETH", "MSOL", "JITOSOL", "BSOL"}


def _discover_solana_token_addresses() -> list[str]:
    """
    Pulls candidate token addresses from DexScreener's boost and profile
    endpoints — tokens people are actively promoting or that were just
    listed. These are where meme coin activity actually is, unlike the
    generic search endpoint.

    Rate limits are 60/min on these endpoints; polling every 30s uses
    6/min across all three, well within budget.
    """
    addresses: list[str] = []
    seen = set()
    for url in DEXSCREENER_DISCOVERY_URLS:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            # These endpoints return a bare list; the websocket variants
            # wrap it in {"data": [...]}. Handle both.
            entries = data if isinstance(data, list) else data.get("data", [])
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("chainId") != "solana":
                    continue
                addr = entry.get("tokenAddress")
                if addr and addr not in seen:
                    seen.add(addr)
                    addresses.append(addr)
        except (requests.RequestException, ValueError) as e:
            print(f"[price_feed] discovery fetch failed for {url}: {e}")
    return addresses


def _fetch_pairs_for_addresses(addresses: list[str]) -> list[dict]:
    """
    Batch-fetches pair data. DexScreener's tokens endpoint accepts up
    to 30 comma-separated addresses per call.
    """
    pairs = []
    for i in range(0, len(addresses), 30):
        batch = addresses[i:i + 30]
        try:
            resp = requests.get(DEXSCREENER_TOKENS_URL + ",".join(batch), timeout=10)
            resp.raise_for_status()
            data = resp.json()
            for pair in (data.get("pairs") or []):
                if pair.get("chainId") == "solana":
                    pairs.append(pair)
        except (requests.RequestException, ValueError) as e:
            print(f"[price_feed] pair fetch failed for batch {i//30}: {e}")
    return pairs


def _dedupe_best_pair_per_token(pairs: list[dict]) -> list[dict]:
    """
    A token can have several pools. Keep only the deepest-liquidity one
    per token, so the same token doesn't occupy multiple slots with
    different prices.
    """
    best: dict[str, dict] = {}
    for pair in pairs:
        try:
            token_addr = pair["baseToken"]["address"]
            liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        existing = best.get(token_addr)
        if existing is None or liquidity > existing[0]:
            best[token_addr] = (liquidity, pair)
    return [pair for _, pair in best.values()]


def fetch_solana_pairs(query: str | None = None) -> list[dict]:
    """
    Returns Solana pairs worth evaluating as breakout candidates.

    If `query` is given, falls back to the text-search endpoint (used
    by update_sol_trend to look up SOL itself). Otherwise it discovers
    actively-promoted and newly-listed Solana tokens and fetches their
    pair data.

    Note on the previous implementation: this used to call the search
    endpoint with the literal string "solana", which returned pairs
    matching that *text* — mostly dead pools with SOL as the base
    token. That produced data the breakout strategy could never fire
    on. If you have price_history CSVs where every symbol is "SOL",
    they were collected under the old behavior and aren't usable.
    """
    if query is not None:
        try:
            resp = requests.get(DEXSCREENER_SEARCH_URL, params={"q": query}, timeout=10)
            resp.raise_for_status()
            pairs = resp.json().get("pairs") or []
            return [p for p in pairs if p.get("chainId") == "solana"]
        except (requests.RequestException, ValueError) as e:
            print(f"[price_feed] search fetch failed: {e}")
            return []

    addresses = _discover_solana_token_addresses()
    if not addresses:
        print("[price_feed] discovery returned no Solana tokens this cycle")
        return []

    pairs = _fetch_pairs_for_addresses(addresses)
    pairs = _dedupe_best_pair_per_token(pairs)

    filtered = []
    for pair in pairs:
        try:
            symbol = pair["baseToken"].get("symbol", "").upper()
            liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            volume_24h = float(pair.get("volume", {}).get("h24", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if symbol in EXCLUDED_BASE_SYMBOLS:
            continue
        # Floor out dust pools — the old feed was full of pairs with
        # sub-dollar volume, which can't produce a tradeable move.
        if liquidity < 1000 or volume_24h < 1000:
            continue
        filtered.append(pair)

    return filtered

# In-memory price history: {pair_address: [(timestamp, price), ...]}
_price_history: dict[str, list[tuple[float, float]]] = {}

# Consecutive poll cycles a pair has satisfied the breakout condition
_confirmation_counts: dict[str, int] = {}

# SOL's own recent price history, for the correlation/trend filter
_sol_price_history: list[tuple[float, float]] = []


def _record_price(pair_address: str, price: float):
    now = time.time()
    history = _price_history.setdefault(pair_address, [])
    history.append((now, price))
    cutoff = now - (config.breakout_lookback_minutes * 60)
    _price_history[pair_address] = [(t, p) for t, p in history if t >= cutoff]


def get_price_history(pair_address: str) -> list[tuple[float, float]]:
    """Exposed for portfolio.py's correlation check."""
    return _price_history.get(pair_address, [])


def update_sol_trend() -> float | None:
    """
    Fetches SOL/USDC and returns SOL's % price change over the same
    lookback window used for breakout detection. Returns None if data
    isn't available yet (first poll) or the fetch fails — callers
    should treat None as "unknown, don't block on it" rather than
    "SOL is crashing."
    """
    pairs = fetch_solana_pairs(SOL_USDC_PAIR_QUERY)
    sol_pair = next(
        (p for p in pairs if p.get("baseToken", {}).get("symbol", "").upper() == "SOL"),
        None,
    )
    if not sol_pair:
        return None
    try:
        price = float(sol_pair["priceUsd"])
    except (KeyError, TypeError, ValueError):
        return None

    now = time.time()
    _sol_price_history.append((now, price))
    cutoff = now - (config.breakout_lookback_minutes * 60)
    while _sol_price_history and _sol_price_history[0][0] < cutoff:
        _sol_price_history.pop(0)

    if len(_sol_price_history) < 2:
        return None

    oldest_price = _sol_price_history[0][1]
    if oldest_price <= 0:
        return None
    return ((price - oldest_price) / oldest_price) * 100


def sol_trend_allows_new_buys() -> tuple[bool, str]:
    """
    Refuses new buys if SOL itself is down more than
    SOL_DUMP_THRESHOLD_PCT over the lookback window — meme coins are
    highly correlated to SOL, and buying breakouts during a SOL dump
    is usually buying a dead cat bounce.
    """
    if not config.sol_correlation_filter_enabled:
        return True, "SOL correlation filter disabled"

    trend_pct = update_sol_trend()
    if trend_pct is None:
        return True, "SOL trend not yet available, allowing (fail-open on this filter only)"

    if trend_pct <= -config.sol_dump_threshold_pct:
        return False, f"SOL down {trend_pct:.1f}% over lookback window — pausing new buys"

    return True, f"SOL trend OK ({trend_pct:+.1f}%)"


def find_breakout_candidates(pairs: list[dict]) -> list[dict]:
    """
    Returns pairs that meet the breakout + volume thresholds AND have
    held that condition for CONFIRMATION_POLLS consecutive poll cycles
    (multi-timeframe confirmation) — filters out single-poll noise
    and wicks that reverse immediately.
    """
    candidates = []
    seen_this_cycle = set()

    for pair in pairs:
        try:
            addr = pair["pairAddress"]
            price = float(pair["priceUsd"])
            volume_24h = float(pair.get("volume", {}).get("h24", 0))
        except (KeyError, TypeError, ValueError):
            continue

        seen_this_cycle.add(addr)

        if volume_24h < config.min_volume_usd:
            _confirmation_counts[addr] = 0
            continue

        _record_price(addr, price)
        history = _price_history[addr]
        if len(history) < 2:
            continue  # need at least two samples to measure a move

        oldest_price = history[0][1]
        if oldest_price <= 0:
            continue

        pct_change = ((price - oldest_price) / oldest_price) * 100

        if pct_change >= config.breakout_pct:
            _confirmation_counts[addr] = _confirmation_counts.get(addr, 0) + 1
        else:
            _confirmation_counts[addr] = 0
            continue

        if _confirmation_counts[addr] >= config.confirmation_polls:
            candidates.append({**pair, "_measured_pct_change": pct_change})

    # Reset confirmation count for anything that dropped out of results
    # entirely (delisted, no longer matches search) to avoid stale state
    for addr in list(_confirmation_counts.keys()):
        if addr not in seen_this_cycle:
            _confirmation_counts.pop(addr, None)

    return candidates


def fetch_price_for_mint(mint: str) -> float | None:
    """
    Looks up current price for a specific token by mint address, via
    DexScreener's per-token endpoint. Used for pump.fun migration
    events, which arrive with a mint address but no price — the token
    only just got real liquidity, so this is the first point a price
    is even meaningful.
    """
    try:
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=8
        )
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not solana_pairs:
            return None
        # Prefer the pair with the highest liquidity if there are multiple
        solana_pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
        return float(solana_pairs[0]["priceUsd"])
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None


def get_recent_volatility(pair_address: str) -> float | None:
    """
    Returns the coefficient of variation (stdev/mean) of recorded
    prices for a pair over the lookback window — a simple, dependency-
    free volatility measure used for volatility-based position sizing.
    Returns None if there's not enough history yet.
    """
    history = _price_history.get(pair_address, [])
    if len(history) < 3:
        return None
    prices = [p for _, p in history]
    mean = sum(prices) / len(prices)
    if mean <= 0:
        return None
    variance = sum((p - mean) ** 2 for p in prices) / len(prices)
    stdev = variance ** 0.5
    return stdev / mean
