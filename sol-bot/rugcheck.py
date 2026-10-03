"""
Screens a token for rug-pull risk before the bot will trade it.

Two layers:
1. RugCheck's public API (api.rugcheck.xyz) — community risk score
   covering mint/freeze authority, LP lock status, holder concentration.
2. A direct on-chain fallback via RPC, in case RugCheck is down or
   hasn't indexed a brand-new token yet — checks mint authority and
   freeze authority directly, since a token where the creator can
   mint unlimited supply or freeze your tokens is close to a
   guaranteed loss regardless of what else looks fine.

This reduces exposure to obvious rugs. It does NOT eliminate rug risk —
sophisticated rugs (fake locks, delayed authority renouncement,
insider wallets disguised as separate holders) can pass these checks.
Treat a pass as "not an obvious rug," not "safe."
"""
import requests
from solana.rpc.api import Client
from solders.pubkey import Pubkey

from config import config

RUGCHECK_API = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

# RugCheck risk levels we refuse to trade regardless of other settings
BLOCKED_RISK_LEVELS = {"danger", "critical"}

# Refuse if the single largest holder (excluding known LP/burn addresses)
# owns more than this % of supply — a whale that size can dump on you
# alone, no coordinated rug required.
MAX_TOP_HOLDER_PCT = 20

# Refuse if buying our intended position size would move the pool price
# by more than this % — high impact means you can't exit at anything
# close to the price you got in at. Configurable via MAX_PRICE_IMPACT_PCT.
MAX_PRICE_IMPACT_PCT = config.max_price_impact_pct


def _rugcheck_lookup(token_mint: str) -> dict | None:
    try:
        resp = requests.get(RUGCHECK_API.format(mint=token_mint), timeout=8)
        if resp.status_code == 404:
            return None  # not indexed yet
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[rugcheck] API lookup failed for {token_mint}: {e}")
        return None


def _onchain_authority_check(rpc_client: Client, token_mint: str) -> dict:
    """
    Direct fallback: fetch mint account and check if mint/freeze
    authority is still set (i.e. NOT renounced). Either one being
    live is a major red flag on its own.
    """
    result = {"mint_authority_active": None, "freeze_authority_active": None, "error": None}
    try:
        mint_pubkey = Pubkey.from_string(token_mint)
        info = rpc_client.get_account_info_json_parsed(mint_pubkey)
        parsed = info.value.data.parsed
        info_fields = parsed["info"]
        result["mint_authority_active"] = info_fields.get("mintAuthority") is not None
        result["freeze_authority_active"] = info_fields.get("freezeAuthority") is not None
    except Exception as e:
        result["error"] = str(e)
    return result


def _check_holder_concentration(report: dict) -> tuple[bool, str]:
    """RugCheck reports return top holders with % of supply. Flag if
    any single non-LP holder exceeds MAX_TOP_HOLDER_PCT."""
    holders = report.get("topHolders", [])
    for h in holders:
        if not isinstance(h, dict) or h.get("isLp"):
            continue  # skip LP/pool addresses — those are supposed to hold a lot
        pct = h.get("pct", 0)
        if pct and float(pct) > MAX_TOP_HOLDER_PCT:
            return False, f"Top holder owns {pct:.1f}% of supply (limit {MAX_TOP_HOLDER_PCT}%)"
    return True, "Holder concentration OK"


def check_liquidity_depth(token_mint: str, position_size_lamports: int) -> tuple[bool, str]:
    """
    Requests a Jupiter quote at our actual intended position size and
    checks the reported price impact. This is the direct way to know
    whether OUR trade size specifically is safe for THIS pool's
    liquidity — a token can look fine at small size and be illiquid
    at the size we'd actually trade.
    """
    try:
        resp = requests.get(
            "https://quote-api.jup.ag/v6/quote",
            params={
                "inputMint": "So11111111111111111111111111111111111111112",
                "outputMint": token_mint,
                "amount": position_size_lamports,
                "slippageBps": 100,
            },
            timeout=8,
        )
        resp.raise_for_status()
        quote = resp.json()
        impact_pct = float(quote.get("priceImpactPct", 0)) * 100
        if impact_pct > MAX_PRICE_IMPACT_PCT:
            return False, (
                f"Price impact at our position size would be {impact_pct:.1f}% "
                f"(limit {MAX_PRICE_IMPACT_PCT}%) — pool too shallow for this trade size"
            )
        return True, f"Liquidity depth OK (price impact {impact_pct:.2f}%)"
    except (requests.RequestException, KeyError, ValueError) as e:
        return False, f"Could not verify liquidity depth: {e}"


def screen_token(token_mint: str, rpc_client: Client, position_size_lamports: int | None = None) -> tuple[bool, str]:
    """
    Returns (is_safe_enough_to_trade, reason).
    Fails closed: if we can't get any signal at all, we refuse to trade
    rather than assume it's fine.
    """
    report = _rugcheck_lookup(token_mint)

    if report is not None:
        risk_level = str(report.get("score_normalised", report.get("risk", ""))).lower()
        risks = report.get("risks", [])
        risk_names = {r.get("name", "").lower() for r in risks if isinstance(r, dict)}

        for level in BLOCKED_RISK_LEVELS:
            if level in risk_level:
                return False, f"RugCheck flagged risk level: {risk_level}"

        if "mint authority still active" in risk_names or "mintable" in risk_names:
            return False, "Mint authority not renounced (per RugCheck)"
        if "freeze authority still active" in risk_names:
            return False, "Freeze authority not renounced (per RugCheck)"
        if "low liquidity" in risk_names or "liquidity not locked" in risk_names:
            return False, "Liquidity unlocked or too low (per RugCheck)"

        holder_ok, holder_reason = _check_holder_concentration(report)
        if not holder_ok:
            return False, holder_reason

        if position_size_lamports:
            depth_ok, depth_reason = check_liquidity_depth(token_mint, position_size_lamports)
            if not depth_ok:
                return False, depth_reason

        return True, "Passed RugCheck screening (risk, holders, liquidity depth)"

    # RugCheck had nothing — fall back to direct on-chain check
    onchain = _onchain_authority_check(rpc_client, token_mint)
    if onchain["error"]:
        return False, f"Could not verify token safety (no data, RPC error: {onchain['error']})"
    if onchain["mint_authority_active"]:
        return False, "Mint authority not renounced (on-chain check)"
    if onchain["freeze_authority_active"]:
        return False, "Freeze authority not renounced (on-chain check)"

    if position_size_lamports:
        depth_ok, depth_reason = check_liquidity_depth(token_mint, position_size_lamports)
        if not depth_ok:
            return False, depth_reason

    return True, "Passed on-chain authority check (RugCheck had no data — lower confidence)"
