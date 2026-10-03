"""
Main loop: poll for breakout candidates, open positions on new ones,
check exits on open positions. Ctrl+C to stop.
"""
import time
from config import config
from price_feed import fetch_solana_pairs, find_breakout_candidates, fetch_price_for_mint
from trader import open_position, check_exits, open_positions, client, _keypair
from wallet_security import sweep_profits, KILL_SWITCH_FILE
from whale_tracker import check_whale_activity
from snipe import snipe_new_token
import pumpfun

POLL_INTERVAL_SECONDS = 30
SWEEP_INTERVAL_SECONDS = 86400  # once a day — see wallet_security.sweep_profits


def main():
    print("=" * 60)
    print("Solana breakout bot starting")
    print(f"  DRY_RUN: {config.dry_run}")
    print(f"  Position size: {config.position_size_pct}% of wallet")
    print(f"  Stop-loss: -{config.stop_loss_pct}%  Take-profit: +{config.take_profit_pct}%")
    print(f"  Breakout: +{config.breakout_pct}% over {config.breakout_lookback_minutes}min, "
          f"min volume ${config.min_volume_usd}")
    print(f"  Max open positions: {config.max_open_positions}")
    print(f"  Mode: {config.mode.upper()}  Network: {config.network}")
    print(f"  Rug check: {config.rug_check_enabled}  Honeypot check: {config.honeypot_check_enabled}  Jito MEV protection: {config.use_jito_bundles}")
    print(f"  Trade rate cap: {config.max_trades_per_hour}/hr  Daily loss cap: {config.max_loss_sol_per_day} SOL  Max consecutive losses: {config.max_consecutive_losses}")
    print(f"  Manual confirm above: {config.manual_confirm_threshold_sol} SOL  Max price impact: {config.max_price_impact_pct}%")
    print(f"  Confirmation polls: {config.confirmation_polls}  SOL correlation filter: {config.sol_correlation_filter_enabled}")
    print(f"  Trailing stop: {config.trailing_stop_enabled} (activate +{config.trailing_activation_pct}%, trail {config.trailing_stop_pct}%)")
    print(f"  Partial exit: {config.partial_exit_enabled} ({config.partial_exit_pct}% at TP)")
    print(f"  Dynamic slippage: {config.dynamic_slippage_enabled}  Volatility sizing: {config.volatility_sizing_enabled}")
    print(f"  Correlation cap: {config.correlation_cap_enabled} (max {config.max_position_correlation})")
    print(f"  pump.fun integration: {config.pumpfun_enabled} (migrations feed trading, new launches logged only)")
    print(f"  Per-token exposure cap: {config.max_token_exposure_pct}%  Stale-quote check: {config.stale_quote_check_enabled}")
    print(f"  Whale tracking: {config.whale_tracking_enabled} ({len(config.whale_wallets)} wallets, alert-only)")
    print(f"  Alerts: Telegram={config.telegram_enabled} Discord={config.discord_enabled}")
    if config.snipe_enabled:
        print(f"  >>> BONDING-CURVE SNIPING ENABLED: max {config.snipe_max_sol} SOL/snipe, "
              f"confirm={config.snipe_always_confirm} — HIGH RISK, see snipe.py <<<")
    print(f"  Kill switch: touch a file named 'STOP' in this directory to halt immediately")
    if config.dry_run:
        print("  >>> DRY RUN MODE: no real trades will be executed <<<")
    if config.mode == "monitor":
        print("  >>> MONITOR MODE: bot will alert on candidates, never trade automatically <<<")
    print("=" * 60)

    pumpfun.start_listener()

    last_sweep = 0.0

    while True:
        if KILL_SWITCH_FILE.exists():
            print("[main] STOP file detected — halting. Delete it to resume.")
            break

        try:
            pairs = fetch_solana_pairs()
            current_prices = {
                p["baseToken"]["address"]: float(p["priceUsd"])
                for p in pairs
                if p.get("priceUsd")
            }

            if open_positions:
                check_exits(current_prices)

            candidates = find_breakout_candidates(pairs)
            for c in candidates:
                token_mint = c["baseToken"]["address"]
                symbol = c["baseToken"].get("symbol", "?")
                price = float(c["priceUsd"])
                pct = c["_measured_pct_change"]

                if token_mint in open_positions:
                    continue

                print(f"[main] breakout candidate: {symbol} ({token_mint}) "
                      f"+{pct:.1f}% @ ${price}")
                open_position(token_mint, price)

            # pump.fun migrations: freshly-graduated tokens, fed into
            # the same screened pipeline as breakout candidates
            for migration in pumpfun.get_pending_migrations():
                token_mint = migration["mint"]
                if token_mint in open_positions:
                    continue
                price = fetch_price_for_mint(token_mint)
                if price is None:
                    print(f"[main] pump.fun migration {migration['symbol']} ({token_mint}) "
                          f"— no price data yet, skipping this cycle")
                    continue
                print(f"[main] pump.fun migration candidate: {migration['symbol']} "
                      f"({token_mint}) @ ${price}")
                open_position(token_mint, price)

            if config.whale_tracking_enabled:
                check_whale_activity(client)

            # Bonding-curve snipes: only when explicitly enabled. Note
            # these bypass rug/honeypot screening by necessity — a
            # brand-new mint has no history to screen. See snipe.py.
            if config.snipe_enabled:
                for token in pumpfun.get_recent_new_tokens(limit=5):
                    if token.get("_snipe_attempted"):
                        continue
                    token["_snipe_attempted"] = True
                    snipe_new_token(token["mint"], token.get("symbol", "?"))

            if not config.dry_run and time.time() - last_sweep > SWEEP_INTERVAL_SECONDS:
                sweep_profits(client, _keypair, config.cold_wallet_address, config.working_balance_sol)
                last_sweep = time.time()

        except Exception as e:
            # Never let one bad cycle kill the bot silently, but never
            # swallow errors invisibly either.
            print(f"[main] error in poll cycle: {e}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
