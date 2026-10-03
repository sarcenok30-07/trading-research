"""
Executes swaps via the Jupiter aggregator API (best-price routing across
Solana DEXs) and tracks open positions for stop-loss / take-profit exits.

DRY_RUN=true (the default) logs what the bot WOULD do without sending
any transaction. Keep it there until you've watched it run and agree
with its decisions.
"""
import time
import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
import base58

from config import config
from rugcheck import screen_token
from jito import send_as_jito_bundle
from honeypot import check_sellable
from key_storage import prompt_for_key
from portfolio import check_correlation_cap
from price_feed import sol_trend_allows_new_buys, get_recent_volatility, fetch_price_for_mint
from wallet_security import (
    SpendingGuard,
    SecurityLimitExceeded,
    verify_and_simulate,
    log_trade,
    sweep_profits,
    send_alert,
)

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
SOL_MINT = "So11111111111111111111111111111111111111112"

# Real RPC client even in dry-run: rug checks and price context still
# need chain reads. Only signing/sending is gated on dry_run.
client = Client(config.rpc_url)
_keypair = None
if not config.dry_run:
    resolved_key = prompt_for_key(config.use_encrypted_key, config.wallet_private_key)
    _keypair = Keypair.from_bytes(base58.b58decode(resolved_key))

guard = SpendingGuard(
    max_trades_per_hour=config.max_trades_per_hour,
    max_loss_sol_per_day=config.max_loss_sol_per_day,
    max_consecutive_losses=config.max_consecutive_losses,
)

# {token_mint: {"entry_price": float, "amount_sol": float, "opened_at": float}}
open_positions: dict[str, dict] = {}


def get_wallet_balance_sol() -> float:
    if config.dry_run:
        return 1.0  # placeholder for dry-run testing
    balance = client.get_balance(_keypair.pubkey()).value
    return balance / 1_000_000_000  # lamports -> SOL


def _check_wallet_safety_cap():
    bal = get_wallet_balance_sol()
    if bal > config.max_wallet_sol_balance:
        raise RuntimeError(
            f"Wallet balance ({bal} SOL) exceeds MAX_WALLET_SOL_BALANCE "
            f"({config.max_wallet_sol_balance}). Refusing to trade. "
            f"Raise the cap in .env only if you deliberately intend to "
            f"risk this wallet."
        )


def get_jupiter_quote(input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int = 100) -> dict | None:
    try:
        resp = requests.get(
            JUPITER_QUOTE_URL,
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount_lamports,
                "slippageBps": slippage_bps,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[trader] quote failed: {e}")
        return None


def _dynamic_slippage_bps(token_mint: str) -> int:
    """
    Widens slippage tolerance for volatile tokens (so the trade doesn't
    fail from a stale quote in a fast-moving market) and tightens it
    for calm ones (so we don't accept a worse fill than necessary).
    Falls back to a fixed 100 bps if not enough price history exists
    yet or the feature is disabled.
    """
    if not config.dynamic_slippage_enabled:
        return 100

    volatility = get_recent_volatility(token_mint)
    if volatility is None:
        return 100

    # Map coefficient-of-variation roughly onto the configured bps range.
    # This is a simple linear scale, not a calibrated model — tune the
    # bounds in .env if you find it too loose or too tight in practice.
    scaled = int(config.min_slippage_bps + (volatility * 2000))
    return max(config.min_slippage_bps, min(config.max_slippage_bps, scaled))


def _volatility_sized_amount(base_amount_sol: float, token_mint: str) -> float:
    """
    Scales position size down for more volatile tokens and up (within
    a cap) for calmer ones, instead of a flat % of wallet regardless of
    risk. A token that's already moved 30% erratically deserves a
    smaller bet than one moving steadily.
    """
    if not config.volatility_sizing_enabled:
        return base_amount_sol

    volatility = get_recent_volatility(token_mint)
    if volatility is None:
        return base_amount_sol

    # Higher volatility -> smaller multiplier. Calibrated loosely:
    # ~5% CoV maps near 1.0x, higher CoV pulls toward the floor.
    multiplier = max(config.min_size_multiplier, min(config.max_size_multiplier, 0.05 / max(volatility, 0.01)))
    return base_amount_sol * multiplier


def _manual_confirm(amount_sol: float, token_mint: str) -> bool:
    if amount_sol < config.manual_confirm_threshold_sol:
        return True
    send_alert(
        "Trade confirmation needed",
        f"{amount_sol:.4f} SOL trade on {token_mint[:8]}... exceeds "
        f"MANUAL_CONFIRM_THRESHOLD_SOL ({config.manual_confirm_threshold_sol})",
    )
    resp = input(
        f"Confirm trade of {amount_sol:.4f} SOL on {token_mint}? "
        f"Type 'yes' to proceed: "
    ).strip().lower()
    return resp == "yes"


def execute_swap(quote: dict, is_buy: bool = True, amount_sol: float = 0.0, token_mint: str = "") -> str | None:
    """
    Sends a swap transaction. Returns tx signature (or Jito bundle ID),
    or None on failure.

    Security flow before anything is sent:
      1. Kill switch + trade-rate + daily-loss + consecutive-loss checks
      2. Manual confirmation if trade size exceeds the threshold
      3. Sign the transaction
      4. Simulate it and verify the fee payer matches our wallet, to
         catch a malicious/buggy swap payload before it's irreversible
      5. Send via Jito bundle (MEV protection) if enabled, else direct
    """
    if config.dry_run:
        print(f"[DRY RUN] Would execute swap: {quote.get('outAmount')} out")
        return "DRY_RUN_NO_TX"

    try:
        guard.check_kill_switch()
        guard.check_trade_rate()
        guard.check_daily_loss()
        guard.check_consecutive_losses()
    except SecurityLimitExceeded as e:
        print(f"[trader] BLOCKED by security guard: {e}")
        log_trade({"type": "blocked", "reason": str(e)})
        return None

    if is_buy and not _manual_confirm(amount_sol, token_mint):
        print("[trader] trade declined at manual confirmation")
        log_trade({"type": "declined", "token": token_mint, "amount_sol": amount_sol})
        return None

    try:
        swap_resp = requests.post(
            JUPITER_SWAP_URL,
            json={
                "quoteResponse": quote,
                "userPublicKey": str(_keypair.pubkey()),
                "wrapAndUnwrapSol": True,
            },
            timeout=10,
        )
        swap_resp.raise_for_status()
        swap_tx = swap_resp.json()["swapTransaction"]

        raw_tx = VersionedTransaction.from_bytes(base58.b58decode(swap_tx))
        signed_tx = VersionedTransaction(raw_tx.message, [_keypair])

        ok, reason = verify_and_simulate(client, signed_tx, _keypair.pubkey())
        if not ok:
            print(f"[trader] BLOCKED by simulation/verification: {reason}")
            log_trade({"type": "blocked", "reason": reason})
            return None

        if config.use_jito_bundles and is_buy:
            blockhash = client.get_latest_blockhash().value.blockhash
            bundle_id = send_as_jito_bundle(
                signed_tx, _keypair, blockhash, tip_lamports=config.jito_tip_lamports
            )
            if bundle_id:
                guard.record_trade()
                log_trade({"type": "swap", "via": "jito", "bundle_id": bundle_id})
                return bundle_id
            print("[trader] Jito bundle failed, falling back to direct send")

        result = client.send_raw_transaction(bytes(signed_tx))
        guard.record_trade()
        log_trade({"type": "swap", "via": "direct", "tx": str(result.value)})
        return str(result.value)
    except (requests.RequestException, KeyError) as e:
        print(f"[trader] swap execution failed: {e}")
        return None


def open_position(token_mint: str, entry_price_usd: float):
    allowed, reason = sol_trend_allows_new_buys()
    if not allowed:
        print(f"[trader] SKIPPED {token_mint}: {reason}")
        log_trade({"type": "skipped", "token": token_mint, "reason": reason})
        return

    correlation_ok, reason = check_correlation_cap(token_mint, list(open_positions.keys()))
    if not correlation_ok:
        print(f"[trader] SKIPPED {token_mint}: {reason}")
        log_trade({"type": "skipped", "token": token_mint, "reason": reason})
        return

    balance = get_wallet_balance_sol()
    base_amount_sol = balance * (config.position_size_pct / 100)
    amount_sol = _volatility_sized_amount(base_amount_sol, token_mint)

    # Per-token exposure cap: volatility sizing can scale UP to
    # max_size_multiplier, which could push a single position past a
    # sane ceiling even if position_size_pct itself looks reasonable.
    # Clamp rather than skip — the trade is still worth taking, just
    # smaller.
    max_exposure_sol = balance * (config.max_token_exposure_pct / 100)
    if amount_sol > max_exposure_sol:
        print(f"[trader] clamping position size {amount_sol:.4f} -> "
              f"{max_exposure_sol:.4f} SOL (MAX_TOKEN_EXPOSURE_PCT)")
        amount_sol = max_exposure_sol

    amount_lamports = int(amount_sol * 1_000_000_000)

    if config.rug_check_enabled:
        safe, reason = screen_token(token_mint, client, position_size_lamports=amount_lamports)
        if not safe:
            print(f"[trader] SKIPPED {token_mint}: {reason}")
            log_trade({"type": "skipped", "token": token_mint, "reason": reason})
            return
        print(f"[trader] rug check passed for {token_mint}: {reason}")

    if config.honeypot_check_enabled:
        sellable, reason = check_sellable(token_mint)
        if not sellable:
            print(f"[trader] SKIPPED {token_mint}: {reason}")
            log_trade({"type": "skipped", "token": token_mint, "reason": reason})
            return
        print(f"[trader] honeypot check passed for {token_mint}: {reason}")

    if config.mode == "monitor":
        send_alert(
            "Trade candidate found",
            f"{token_mint} passed all checks — entry ${entry_price_usd}, "
            f"would size {amount_sol:.4f} SOL. MODE=monitor, not trading automatically.",
        )
        log_trade({"type": "monitor_alert", "token": token_mint, "entry_price": entry_price_usd})
        return

    _check_wallet_safety_cap()

    if len(open_positions) >= config.max_open_positions:
        print(f"[trader] max open positions ({config.max_open_positions}) reached, skipping")
        return

    if config.stale_quote_check_enabled:
        fresh_price = fetch_price_for_mint(token_mint)
        if fresh_price is not None and entry_price_usd > 0:
            divergence_pct = abs((fresh_price - entry_price_usd) / entry_price_usd) * 100
            if divergence_pct > config.stale_quote_max_divergence_pct:
                print(f"[trader] SKIPPED {token_mint}: price moved {divergence_pct:.1f}% "
                      f"since detection (${entry_price_usd} -> ${fresh_price}), stale signal")
                log_trade({"type": "skipped", "token": token_mint,
                          "reason": f"stale quote, {divergence_pct:.1f}% divergence"})
                return
        # fresh_price is None simply means the lookup failed — don't
        # block the trade on a transient lookup failure, only on a
        # confirmed divergence.

    slippage_bps = _dynamic_slippage_bps(token_mint)
    quote = get_jupiter_quote(SOL_MINT, token_mint, amount_lamports, slippage_bps=slippage_bps)
    if not quote:
        return

    tx_sig = execute_swap(quote, is_buy=True, amount_sol=amount_sol, token_mint=token_mint)
    if tx_sig:
        open_positions[token_mint] = {
            "entry_price": entry_price_usd,
            "amount_sol": amount_sol,
            "original_amount_sol": amount_sol,
            "opened_at": time.time(),
            "peak_price": entry_price_usd,
            "trailing_stop_active": False,
            "partial_exit_done": False,
        }
        print(f"[trader] opened position: {token_mint} @ ${entry_price_usd} "
              f"({amount_sol:.4f} SOL, slippage {slippage_bps}bps) tx={tx_sig}")


def check_exits(current_prices: dict[str, float]):
    """
    Checks open positions against stop-loss, take-profit, and trailing
    stop, and executes partial or full exits as appropriate.

    Flow per position:
      1. Update peak price seen since entry (needed for trailing stop)
      2. Hard stop-loss (from entry) always applies — this is the floor
      3. Once profit exceeds TRAILING_ACTIVATION_PCT, a trailing stop
         takes over: exit if price falls TRAILING_STOP_PCT off the peak,
         which locks in gains instead of riding a full round-trip back
         to break-even or into a loss
      4. At the take-profit target, if PARTIAL_EXIT_ENABLED, sell
         PARTIAL_EXIT_PCT of the position and let the remainder ride
         under the trailing stop rather than exiting all at once
    """
    for token_mint, pos in list(open_positions.items()):
        current_price = current_prices.get(token_mint)
        if current_price is None:
            continue

        if current_price > pos["peak_price"]:
            pos["peak_price"] = current_price

        pct_move_from_entry = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
        pct_off_peak = ((current_price - pos["peak_price"]) / pos["peak_price"]) * 100

        # Hard stop-loss from entry — always active, this is the floor
        if pct_move_from_entry <= -config.stop_loss_pct:
            print(f"[trader] STOP-LOSS hit on {token_mint}: {pct_move_from_entry:.1f}%")
            _close_position(token_mint, current_price, portion=1.0, reason="stop_loss")
            continue

        # Trailing stop: once we're up enough, protect gains instead of
        # waiting for a fixed take-profit target
        if config.trailing_stop_enabled and pct_move_from_entry >= config.trailing_activation_pct:
            pos["trailing_stop_active"] = True

        if pos["trailing_stop_active"] and pct_off_peak <= -config.trailing_stop_pct:
            print(f"[trader] TRAILING STOP hit on {token_mint}: "
                  f"{pct_off_peak:.1f}% off peak (peak was ${pos['peak_price']:.6f})")
            _close_position(token_mint, current_price, portion=1.0, reason="trailing_stop")
            continue

        # Fixed take-profit: partial exit if enabled, else full exit
        if pct_move_from_entry >= config.take_profit_pct and not pos["partial_exit_done"]:
            if config.partial_exit_enabled:
                portion = config.partial_exit_pct / 100
                print(f"[trader] TAKE-PROFIT hit on {token_mint}: {pct_move_from_entry:.1f}% "
                      f"— partial exit ({config.partial_exit_pct}%), remainder rides trailing stop")
                pos["partial_exit_done"] = True
                pos["trailing_stop_active"] = True  # protect the remainder from here on
                _close_position(token_mint, current_price, portion=portion, reason="take_profit_partial")
            else:
                print(f"[trader] TAKE-PROFIT hit on {token_mint}: {pct_move_from_entry:.1f}%")
                _close_position(token_mint, current_price, portion=1.0, reason="take_profit")


def _close_position(token_mint: str, exit_price: float, portion: float = 1.0, reason: str = ""):
    """
    Closes all or part of a position. `portion` is the fraction of the
    CURRENT remaining amount_sol to sell (1.0 = full close).
    """
    pos = open_positions.get(token_mint)
    if not pos:
        return

    sell_amount_sol = pos["amount_sol"] * portion
    amount_lamports = int(sell_amount_sol * 1_000_000_000)

    slippage_bps = _dynamic_slippage_bps(token_mint)
    quote = get_jupiter_quote(token_mint, SOL_MINT, amount_lamports, slippage_bps=slippage_bps)
    if not quote:
        print(f"[trader] could not get exit quote for {token_mint}, will retry next cycle")
        return

    # Exits skip Jito — no benefit to bundling a sell, and we want it
    # to land as fast as possible via normal broadcast.
    tx_sig = execute_swap(quote, is_buy=False, amount_sol=sell_amount_sol, token_mint=token_mint)
    if tx_sig:
        pct_move = ((exit_price - pos["entry_price"]) / pos["entry_price"])
        pnl_sol = sell_amount_sol * pct_move
        guard.record_pnl(pnl_sol)
        log_trade({
            "type": "swap",
            "subtype": "close",
            "reason": reason,
            "token": token_mint,
            "exit_price": exit_price,
            "portion": portion,
            "amount_sol": sell_amount_sol,
            "pnl_sol": pnl_sol,
            "tx": tx_sig,
        })
        print(f"[trader] closed {portion*100:.0f}% of position: {token_mint} @ ${exit_price} "
              f"pnl={pnl_sol:.4f} SOL tx={tx_sig}")

        pos["amount_sol"] -= sell_amount_sol
        if pos["amount_sol"] <= 1e-9 or portion >= 1.0:
            del open_positions[token_mint]
