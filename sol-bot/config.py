"""
Loads and validates configuration from .env.
Fails loudly rather than silently defaulting anything risk-related —
a bot that quietly falls back to "trade everything" on a typo is a
bad bot.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _get_float(name: str) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        raise ValueError(f"Missing required env var: {name}")
    return float(val)


def _get_int(name: str) -> int:
    return int(_get_float(name))


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes")


class Config:
    def __init__(self):
        self.wallet_private_key = os.getenv("WALLET_PRIVATE_KEY", "").strip()
        default_rpc = "https://api.mainnet-beta.solana.com"
        if os.getenv("NETWORK", "mainnet").strip().lower() == "devnet":
            default_rpc = "https://api.devnet.solana.com"
        self.rpc_url = os.getenv("RPC_URL", default_rpc).strip()

        self.position_size_pct = _get_float("POSITION_SIZE_PCT")
        self.stop_loss_pct = _get_float("STOP_LOSS_PCT")
        self.take_profit_pct = _get_float("TAKE_PROFIT_PCT")
        self.min_volume_usd = _get_float("MIN_VOLUME_USD")
        self.breakout_pct = _get_float("BREAKOUT_PCT")
        self.breakout_lookback_minutes = _get_int("BREAKOUT_LOOKBACK_MINUTES")
        self.max_open_positions = _get_int("MAX_OPEN_POSITIONS")
        self.max_wallet_sol_balance = _get_float("MAX_WALLET_SOL_BALANCE")
        self.dry_run = _get_bool("DRY_RUN", default=True)

        # --- Rug-pull screening ---
        self.rug_check_enabled = _get_bool("RUG_CHECK_ENABLED", default=True)

        # --- MEV protection ---
        self.use_jito_bundles = _get_bool("USE_JITO_BUNDLES", default=True)
        self.jito_tip_lamports = _get_int("JITO_TIP_LAMPORTS") if os.getenv("JITO_TIP_LAMPORTS") else 100_000

        # --- Wallet security ---
        self.max_trades_per_hour = _get_int("MAX_TRADES_PER_HOUR") if os.getenv("MAX_TRADES_PER_HOUR") else 10
        self.max_loss_sol_per_day = _get_float("MAX_LOSS_SOL_PER_DAY") if os.getenv("MAX_LOSS_SOL_PER_DAY") else 0.5
        self.cold_wallet_address = os.getenv("COLD_WALLET_ADDRESS", "").strip()
        self.working_balance_sol = _get_float("WORKING_BALANCE_SOL") if os.getenv("WORKING_BALANCE_SOL") else self.max_wallet_sol_balance

        # --- New safety features ---
        self.mode = os.getenv("MODE", "trade").strip().lower()  # "trade" or "monitor"
        self.honeypot_check_enabled = _get_bool("HONEYPOT_CHECK_ENABLED", default=True)
        self.max_consecutive_losses = _get_int("MAX_CONSECUTIVE_LOSSES") if os.getenv("MAX_CONSECUTIVE_LOSSES") else 4
        self.manual_confirm_threshold_sol = _get_float("MANUAL_CONFIRM_THRESHOLD_SOL") if os.getenv("MANUAL_CONFIRM_THRESHOLD_SOL") else 0.5
        self.max_price_impact_pct = _get_float("MAX_PRICE_IMPACT_PCT") if os.getenv("MAX_PRICE_IMPACT_PCT") else 5.0
        self.use_encrypted_key = _get_bool("USE_ENCRYPTED_KEY", default=False)
        self.network = os.getenv("NETWORK", "mainnet").strip().lower()  # "mainnet" or "devnet"

        # --- Strategy signal quality ---
        self.confirmation_polls = _get_int("CONFIRMATION_POLLS") if os.getenv("CONFIRMATION_POLLS") else 2
        self.sol_correlation_filter_enabled = _get_bool("SOL_CORRELATION_FILTER_ENABLED", default=True)
        self.sol_dump_threshold_pct = _get_float("SOL_DUMP_THRESHOLD_PCT") if os.getenv("SOL_DUMP_THRESHOLD_PCT") else 5.0

        # --- Execution quality ---
        self.dynamic_slippage_enabled = _get_bool("DYNAMIC_SLIPPAGE_ENABLED", default=True)
        self.min_slippage_bps = _get_int("MIN_SLIPPAGE_BPS") if os.getenv("MIN_SLIPPAGE_BPS") else 50
        self.max_slippage_bps = _get_int("MAX_SLIPPAGE_BPS") if os.getenv("MAX_SLIPPAGE_BPS") else 300

        self.trailing_stop_enabled = _get_bool("TRAILING_STOP_ENABLED", default=True)
        self.trailing_activation_pct = _get_float("TRAILING_ACTIVATION_PCT") if os.getenv("TRAILING_ACTIVATION_PCT") else 10.0
        self.trailing_stop_pct = _get_float("TRAILING_STOP_PCT") if os.getenv("TRAILING_STOP_PCT") else 10.0

        self.partial_exit_enabled = _get_bool("PARTIAL_EXIT_ENABLED", default=True)
        self.partial_exit_pct = _get_float("PARTIAL_EXIT_PCT") if os.getenv("PARTIAL_EXIT_PCT") else 50.0

        # --- Portfolio-level risk ---
        self.correlation_cap_enabled = _get_bool("CORRELATION_CAP_ENABLED", default=True)
        self.max_position_correlation = _get_float("MAX_POSITION_CORRELATION") if os.getenv("MAX_POSITION_CORRELATION") else 0.7

        self.volatility_sizing_enabled = _get_bool("VOLATILITY_SIZING_ENABLED", default=True)
        self.min_size_multiplier = _get_float("MIN_SIZE_MULTIPLIER") if os.getenv("MIN_SIZE_MULTIPLIER") else 0.4
        self.max_size_multiplier = _get_float("MAX_SIZE_MULTIPLIER") if os.getenv("MAX_SIZE_MULTIPLIER") else 1.5

        # --- pump.fun integration (via PumpPortal, free) ---
        # Migration/graduation events feed the trading pipeline (still
        # gated by every existing safety check). New-token/bonding-curve
        # events are logged only, never auto-traded — see pumpfun.py
        # for why.
        self.pumpfun_enabled = _get_bool("PUMPFUN_ENABLED", default=False)

        # --- Per-token exposure cap ---
        self.max_token_exposure_pct = _get_float("MAX_TOKEN_EXPOSURE_PCT") if os.getenv("MAX_TOKEN_EXPOSURE_PCT") else 10.0

        # --- Stale-quote / multi-source price check ---
        self.stale_quote_check_enabled = _get_bool("STALE_QUOTE_CHECK_ENABLED", default=True)
        self.stale_quote_max_divergence_pct = _get_float("STALE_QUOTE_MAX_DIVERGENCE_PCT") if os.getenv("STALE_QUOTE_MAX_DIVERGENCE_PCT") else 3.0

        # --- Whale wallet tracking (alert-only, does not auto-trade) ---
        self.whale_wallets = [w.strip() for w in os.getenv("WHALE_WALLETS", "").split(",") if w.strip()]
        self.whale_tracking_enabled = _get_bool("WHALE_TRACKING_ENABLED", default=False) and bool(self.whale_wallets)

        # --- Telegram alerts (free, needs your own bot token) ---
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.telegram_enabled = bool(self.telegram_bot_token and self.telegram_chat_id)

        # --- Discord alerts (free, just a webhook URL) ---
        self.discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        self.discord_enabled = bool(self.discord_webhook_url)

        # --- pump.fun bonding-curve sniping (HIGH RISK, off by default) ---
        # Distinct from PUMPFUN_ENABLED (migrations only). This trades
        # brand-new launches before they have real liquidity. Most
        # pump.fun launches are abandoned or rugged within minutes.
        # See snipe.py before ever enabling this.
        self.snipe_enabled = _get_bool("SNIPE_ENABLED", default=False)
        self.snipe_max_sol = _get_float("SNIPE_MAX_SOL") if os.getenv("SNIPE_MAX_SOL") else 0.05
        self.snipe_always_confirm = _get_bool("SNIPE_ALWAYS_CONFIRM", default=True)
        self.pumpportal_api_key = os.getenv("PUMPPORTAL_API_KEY", "").strip()

        # --- Backups ---
        self.backup_retention_days = _get_int("BACKUP_RETENTION_DAYS") if os.getenv("BACKUP_RETENTION_DAYS") else 30

        # --- Leaderboard providers (all optional, use whichever you have) ---
        self.vybe_api_key = os.getenv("VYBE_API_KEY", "").strip()
        self.birdeye_api_key = os.getenv("BIRDEYE_API_KEY", "").strip()
        self.solanatracker_api_key = os.getenv("SOLANATRACKER_API_KEY", "").strip()
        self.leaderboard_min_trades = _get_int("LEADERBOARD_MIN_TRADES") if os.getenv("LEADERBOARD_MIN_TRADES") else 20
        self.leaderboard_min_win_rate_pct = _get_float("LEADERBOARD_MIN_WIN_RATE_PCT") if os.getenv("LEADERBOARD_MIN_WIN_RATE_PCT") else 45.0
        self.leaderboard_birdeye_max_tokens = _get_int("LEADERBOARD_BIRDEYE_MAX_TOKENS") if os.getenv("LEADERBOARD_BIRDEYE_MAX_TOKENS") else 5

        # --- Copy-trading (score-weighted allocation) ---
        self.copytrade_enabled = _get_bool("COPYTRADE_ENABLED", default=False)
        self.copytrade_allocation_pct = _get_float("COPYTRADE_ALLOCATION_PCT") if os.getenv("COPYTRADE_ALLOCATION_PCT") else 20.0
        self.copytrade_max_tracked_wallets = _get_int("COPYTRADE_MAX_TRACKED_WALLETS") if os.getenv("COPYTRADE_MAX_TRACKED_WALLETS") else 50
        self.copytrade_max_active_wallets = _get_int("COPYTRADE_MAX_ACTIVE_WALLETS") if os.getenv("COPYTRADE_MAX_ACTIVE_WALLETS") else 5
        self.copytrade_score_window = _get_int("COPYTRADE_SCORE_WINDOW") if os.getenv("COPYTRADE_SCORE_WINDOW") else 20
        self.copytrade_min_trades_to_score = _get_int("COPYTRADE_MIN_TRADES_TO_SCORE") if os.getenv("COPYTRADE_MIN_TRADES_TO_SCORE") else 5
        self.copytrade_probation_min_trades = _get_int("COPYTRADE_PROBATION_MIN_TRADES") if os.getenv("COPYTRADE_PROBATION_MIN_TRADES") else 10
        self.copytrade_promote_score = _get_float("COPYTRADE_PROMOTE_SCORE") if os.getenv("COPYTRADE_PROMOTE_SCORE") else 55.0
        self.copytrade_bench_score = _get_float("COPYTRADE_BENCH_SCORE") if os.getenv("COPYTRADE_BENCH_SCORE") else 40.0
        self.copytrade_max_drawdown_sol = _get_float("COPYTRADE_MAX_DRAWDOWN_SOL") if os.getenv("COPYTRADE_MAX_DRAWDOWN_SOL") else 0.3

        # --- Backtest realism ---
        # Approximate round-trip cost (slippage both ways + Jupiter/
        # network fees) as a flat % deducted from each simulated
        # trade's return, since a perfect-fill backtest is always
        # more optimistic than live trading will be.
        self.backtest_roundtrip_cost_pct = _get_float("BACKTEST_ROUNDTRIP_COST_PCT") if os.getenv("BACKTEST_ROUNDTRIP_COST_PCT") else 1.5

        self._validate()

    def _validate(self):
        if not self.dry_run and self.mode == "trade" and not self.wallet_private_key and not self.use_encrypted_key:
            raise ValueError(
                "DRY_RUN is false and MODE is 'trade' but no key source is "
                "configured (WALLET_PRIVATE_KEY empty and USE_ENCRYPTED_KEY "
                "is false). Refusing to start a live bot with no key."
            )
        if self.mode not in ("trade", "monitor"):
            raise ValueError("MODE must be 'trade' or 'monitor'")
        if self.network not in ("mainnet", "devnet"):
            raise ValueError("NETWORK must be 'mainnet' or 'devnet'")
        if self.position_size_pct <= 0 or self.position_size_pct > 25:
            raise ValueError(
                "POSITION_SIZE_PCT looks wrong (must be 0-25). "
                "Refusing to start — this is a safety rail, not a bug."
            )
        if self.stop_loss_pct <= 0:
            raise ValueError("STOP_LOSS_PCT must be positive.")
        if self.max_open_positions < 1:
            raise ValueError("MAX_OPEN_POSITIONS must be at least 1.")
        if self.copytrade_bench_score >= self.copytrade_promote_score:
            raise ValueError(
                "COPYTRADE_BENCH_SCORE must be below COPYTRADE_PROMOTE_SCORE. "
                "Otherwise wallets would be promoted and benched on the same "
                "score, flapping between tiers every cycle."
            )
        if self.copytrade_enabled and self.copytrade_allocation_pct > 50:
            raise ValueError(
                "COPYTRADE_ALLOCATION_PCT above 50 — refusing to start. Copy-trading "
                "follows strangers' decisions; it shouldn't control most of your wallet."
            )
        if self.snipe_enabled and self.snipe_max_sol > 0.5:
            raise ValueError(
                "SNIPE_MAX_SOL is above 0.5 — refusing to start. Bonding-curve "
                "sniping is high-risk; this cap exists so a config typo can't "
                "expose more than a small test amount. Lower it to proceed."
            )


config = Config()
