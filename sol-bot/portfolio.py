"""
Correlation cap across open positions.

Holding 3 "different" meme coins that all pump and dump together
isn't diversification — MAX_OPEN_POSITIONS exists to spread risk, but
that only works if the positions are actually somewhat independent.

This computes Pearson correlation between a candidate token's recorded
price history and each currently-open position's price history (both
sourced from price_feed's in-memory history, which the bot has been
building up during its own polling). If correlation with any open
position exceeds MAX_POSITION_CORRELATION, the candidate is skipped.

Honest limitation: correlation can only be computed from price history
the bot has actually observed while running. A token seen for the
first time this cycle has no history yet, so this check degrades
gracefully (allows the trade) rather than blocking on missing data —
it's a risk-reduction layer, not a hard guarantee.
"""
from config import config
from price_feed import get_price_history


def _pearson_correlation(series_a: list[tuple[float, float]], series_b: list[tuple[float, float]]) -> float | None:
    """
    Correlates two (timestamp, price) series on overlapping time
    windows using nearest-timestamp pairing. Returns None if there's
    not enough overlapping data to compute a meaningful correlation.
    """
    if len(series_a) < 3 or len(series_b) < 3:
        return None

    # Convert to price-only sequences of returns (pct change between
    # consecutive samples) rather than raw prices — comparing return
    # series is what actually captures "do these move together."
    def to_returns(series):
        prices = [p for _, p in series]
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                returns.append((prices[i] - prices[i - 1]) / prices[i - 1])
        return returns

    returns_a = to_returns(series_a)
    returns_b = to_returns(series_b)

    n = min(len(returns_a), len(returns_b))
    if n < 3:
        return None
    returns_a = returns_a[-n:]
    returns_b = returns_b[-n:]

    mean_a = sum(returns_a) / n
    mean_b = sum(returns_b) / n

    cov = sum((returns_a[i] - mean_a) * (returns_b[i] - mean_b) for i in range(n))
    var_a = sum((r - mean_a) ** 2 for r in returns_a)
    var_b = sum((r - mean_b) ** 2 for r in returns_b)

    denom = (var_a * var_b) ** 0.5
    if denom == 0:
        return None
    return cov / denom


def check_correlation_cap(candidate_pair_address: str, open_position_mints: list[str]) -> tuple[bool, str]:
    """
    Returns (allowed, reason). Only blocks when correlation is
    confidently computed and exceeds the threshold — missing data
    never blocks a trade on its own.
    """
    if not config.correlation_cap_enabled or not open_position_mints:
        return True, "Correlation cap disabled or no open positions to compare against"

    candidate_history = get_price_history(candidate_pair_address)
    if len(candidate_history) < 3:
        return True, "Not enough history on candidate yet to compute correlation (allowing)"

    for open_mint in open_position_mints:
        open_history = get_price_history(open_mint)
        corr = _pearson_correlation(candidate_history, open_history)
        if corr is None:
            continue
        if corr > config.max_position_correlation:
            return False, (
                f"Correlation with open position {open_mint[:8]}... is "
                f"{corr:.2f} (limit {config.max_position_correlation}) — "
                f"too similar to existing exposure"
            )

    return True, "Correlation check passed"
