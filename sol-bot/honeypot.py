"""
Honeypot detection.

The classic meme coin trap: a token is buyable (a normal-looking buy
transaction succeeds) but selling is blocked or taxed at ~100% by
malicious logic in the token contract itself. No amount of price/volume
analysis catches this — it has to be checked directly.

Approach: before buying, request a Jupiter quote for the REVERSE trade
(token -> SOL) at a small representative size. If Jupiter can't route
a sell at all, or the effective sell price implies a tax/loss far
beyond normal slippage, treat it as a likely honeypot and refuse.

This isn't perfect — some honeypots only trigger the block after a
delay, or only for wallets that aren't on an allowlist. But it catches
the common case cheaply, before any money is at risk.
"""
import requests

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
SOL_MINT = "So11111111111111111111111111111111111111112"

# If a round-trip buy+sell at quoted prices would lose more than this
# (beyond normal fees/slippage), treat it as a honeypot red flag.
MAX_ACCEPTABLE_ROUNDTRIP_LOSS_PCT = 15


def check_sellable(token_mint: str, test_amount_lamports: int = 10_000_000) -> tuple[bool, str]:
    """
    Returns (is_probably_sellable, reason).
    test_amount_lamports defaults to 0.01 SOL worth — small enough to
    be a cheap, representative check.
    """
    # Step 1: can we even get a buy quote?
    buy_quote = _get_quote(SOL_MINT, token_mint, test_amount_lamports)
    if not buy_quote:
        return False, "No buy route available"

    token_amount_out = int(buy_quote.get("outAmount", 0))
    if token_amount_out <= 0:
        return False, "Buy quote returned zero output"

    # Step 2: can we get a sell quote for what we'd receive?
    sell_quote = _get_quote(token_mint, SOL_MINT, token_amount_out)
    if not sell_quote:
        return False, "No sell route available — likely honeypot (can buy, can't sell)"

    sol_amount_back = int(sell_quote.get("outAmount", 0))
    if sol_amount_back <= 0:
        return False, "Sell quote returned zero output — likely honeypot"

    roundtrip_loss_pct = (1 - (sol_amount_back / test_amount_lamports)) * 100
    if roundtrip_loss_pct > MAX_ACCEPTABLE_ROUNDTRIP_LOSS_PCT:
        return False, (
            f"Round-trip buy+sell would lose {roundtrip_loss_pct:.1f}% "
            f"(beyond normal slippage/fees) — likely high sell tax or honeypot"
        )

    return True, f"Sell route confirmed, round-trip cost ~{roundtrip_loss_pct:.1f}%"


def _get_quote(input_mint: str, output_mint: str, amount: int) -> dict | None:
    try:
        resp = requests.get(
            JUPITER_QUOTE_URL,
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "slippageBps": 100,
            },
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except requests.RequestException:
        return None
