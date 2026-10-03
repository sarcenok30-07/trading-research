"""
pump.fun bonding-curve sniping — buying brand-new token launches
before they've graduated to real AMM liquidity.

READ THIS BEFORE ENABLING SNIPE_ENABLED:

This is meaningfully higher-risk than everything else in this bot:

- The overwhelming majority of pump.fun launches are abandoned or
  outright rugged within minutes of creation. This is not a tail risk
  — it is closer to the median outcome.
- rugcheck.py and honeypot.py CANNOT meaningfully screen a
  bonding-curve token — there's no established liquidity pool, no
  holder history, nothing resembling what those checks were built to
  evaluate. A brand-new mint passes "no red flags" trivially because
  there's no history to flag.
- Execution uses PumpPortal's Lightning Transaction API, a DIFFERENT
  path than the rest of this bot (Jupiter). PumpPortal signs and
  broadcasts server-side via an API key tied to a wallet YOU create
  through their portal — read their docs before trusting this with
  real funds: https://github.com/thetateman/Trading-API
- You are competing against infrastructure-grade sniper bots with
  sub-millisecond latency. This code, running on a $6/month droplet
  polling a WebSocket, will lose most speed races. Any launch you
  catch fast enough to matter was likely already caught faster by
  someone else, for reasons discussed earlier in this project.

Given all that, this module is:
  - Disabled by default (SNIPE_ENABLED=false)
  - Hard-capped at a tiny SOL amount regardless of other settings
    (config.py refuses to start above 0.5 SOL even if you set it higher)
  - Defaults to requiring manual confirmation on every single snipe
    (SNIPE_ALWAYS_CONFIRM=true), same mechanism as trader.py's
    large-trade confirmation
  - Only acts on pumpfun.py's new-token stream, filtered to nothing
    more sophisticated than "it exists" — no scoring, no ranking

If you want a more sophisticated version (dev wallet %, initial buyer
concentration, social presence, etc.) that's a further, substantial
addition — ask explicitly rather than assuming this covers it.
"""
import requests

from config import config
from wallet_security import log_trade, send_alert

PUMPPORTAL_TRADE_URL = "https://pumpportal.fun/api/trade"


def snipe_new_token(mint: str, symbol: str) -> bool:
    """
    Attempts to buy a brand-new pump.fun token via PumpPortal's
    Lightning API. Returns True if a trade was sent, False otherwise.
    Requires PUMPPORTAL_API_KEY in .env (from pumpportal.fun's own
    portal — a completely separate credential from your Solana wallet
    key, tied to a wallet PumpPortal itself manages for this purpose).
    """
    if not config.snipe_enabled:
        return False

    api_key = config.pumpportal_api_key
    if not api_key:
        print("[snipe] SNIPE_ENABLED is true but PUMPPORTAL_API_KEY is not "
              "set — refusing to snipe. See snipe.py docstring.")
        return False

    if config.dry_run:
        print(f"[DRY RUN][snipe] would attempt to buy {symbol} ({mint[:8]}...) "
              f"for {config.snipe_max_sol} SOL")
        log_trade({"type": "snipe_dry_run", "mint": mint, "symbol": symbol,
                  "amount_sol": config.snipe_max_sol})
        return True

    if config.snipe_always_confirm:
        send_alert(
            "Snipe confirmation needed",
            f"New pump.fun launch: {symbol} ({mint}). Would buy "
            f"{config.snipe_max_sol} SOL. This is HIGH RISK bonding-curve "
            f"sniping — most launches fail. Confirm in terminal to proceed."
        )
        resp = input(
            f"Snipe {symbol} ({mint}) for {config.snipe_max_sol} SOL? "
            f"Type 'yes' to proceed (note: the launch will likely already "
            f"be gone by the time you finish deciding — that delay is the "
            f"point of this confirmation, not a bug): "
        ).strip().lower()
        if resp != "yes":
            print(f"[snipe] declined snipe on {symbol}")
            log_trade({"type": "snipe_declined", "mint": mint, "symbol": symbol})
            return False

    try:
        resp = requests.post(
            PUMPPORTAL_TRADE_URL,
            params={"api-key": api_key},
            json={
                "action": "buy",
                "mint": mint,
                "amount": config.snipe_max_sol,
                "denominatedInSol": "true",
                "slippage": 20,  # bonding-curve prices move fast; wide tolerance is normal here
                "priorityFee": 0.0005,
                "pool": "pump",
            },
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        tx_sig = result.get("signature")
        if tx_sig:
            print(f"[snipe] bought {symbol} ({mint[:8]}...) tx={tx_sig}")
            log_trade({"type": "snipe_buy", "mint": mint, "symbol": symbol,
                      "amount_sol": config.snipe_max_sol, "tx": tx_sig})
            return True
        print(f"[snipe] PumpPortal response had no signature: {result}")
        return False
    except requests.RequestException as e:
        print(f"[snipe] trade request failed: {e}")
        log_trade({"type": "snipe_failed", "mint": mint, "symbol": symbol, "error": str(e)})
        return False
