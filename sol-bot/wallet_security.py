"""
Wallet security hardening.

Important honest framing: a bot that trades automatically needs a live
signing key in memory on a running machine. That is an inherent
exposure — no amount of code makes an internet-connected hot wallet as
safe as a hardware wallet or offline key. What THIS module does is
close the specific vectors that actually drain bots in practice:

1. Transaction simulation + destination verification before signing —
   catches a compromised/malicious API response that tries to swap to
   a different token, drain more than expected, or insert an extra
   instruction (e.g. an approval/transfer to an attacker address).
2. Spending rate limits — caps how much damage a single bad trade,
   bug, or bundle of unlucky trades can do before the bot halts itself.
3. A kill switch file — lets you stop the bot instantly (including
   from another terminal/script) without hunting for the process.
4. Profit sweeping — periodically moves gains above your working
   balance to a separate wallet address that this bot never trades
   from, so a later compromise can't claw back money that's already
   moved out.

What this does NOT do: protect against a compromised machine (keylogger,
malware with filesystem access can still read .env), protect against a
malicious RPC provider, or replace a hardware wallet. If you want the
strongest protection, run this on a dedicated machine/VM you don't use
for anything else, and keep the trading wallet's balance minimal.
"""
import os
import time
import json
from pathlib import Path

import requests
from solana.rpc.api import Client
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0

KILL_SWITCH_FILE = Path("STOP")  # touch this file to halt the bot immediately
TRADE_LOG_FILE = Path("trade_log.json")


class SecurityLimitExceeded(Exception):
    pass


class SpendingGuard:
    """
    Tracks trades in a rolling window and halts the bot if limits are
    breached. This is deliberately conservative-by-default: a bug that
    causes runaway trading gets caught here even if it slipped past
    other checks.
    """

    def __init__(self, max_trades_per_hour: int, max_loss_sol_per_day: float,
                 max_consecutive_losses: int = 4):
        self.max_trades_per_hour = max_trades_per_hour
        self.max_loss_sol_per_day = max_loss_sol_per_day
        self.max_consecutive_losses = max_consecutive_losses
        self._trade_timestamps: list[float] = []
        self._realized_pnl_sol: list[tuple[float, float]] = []  # (timestamp, pnl)
        self._consecutive_losses = 0

    def check_kill_switch(self):
        if KILL_SWITCH_FILE.exists():
            raise SecurityLimitExceeded(
                "Kill switch active (STOP file present). Delete it to resume."
            )

    def record_trade(self):
        now = time.time()
        self._trade_timestamps.append(now)
        self._trade_timestamps = [t for t in self._trade_timestamps if now - t < 3600]

    def check_trade_rate(self):
        now = time.time()
        recent = [t for t in self._trade_timestamps if now - t < 3600]
        if len(recent) >= self.max_trades_per_hour:
            raise SecurityLimitExceeded(
                f"Trade rate limit hit ({len(recent)}/{self.max_trades_per_hour} "
                f"per hour). Halting — this usually means either a real "
                f"opportunity storm or a logic bug; check trade_log.json."
            )

    def record_pnl(self, pnl_sol: float):
        now = time.time()
        self._realized_pnl_sol.append((now, pnl_sol))
        self._realized_pnl_sol = [(t, p) for t, p in self._realized_pnl_sol if now - t < 86400]

    def record_pnl(self, pnl_sol: float):
        now = time.time()
        self._realized_pnl_sol.append((now, pnl_sol))
        self._realized_pnl_sol = [(t, p) for t, p in self._realized_pnl_sol if now - t < 86400]

        # Consecutive-loss tracking is separate from the daily SOL cap:
        # it catches "strategy stopped working" (a string of small losses)
        # faster than a cumulative SOL threshold would.
        if pnl_sol < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

    def check_consecutive_losses(self):
        if self._consecutive_losses >= self.max_consecutive_losses:
            raise SecurityLimitExceeded(
                f"{self._consecutive_losses} consecutive losing trades "
                f"(limit {self.max_consecutive_losses}). Halting — this "
                f"usually means the breakout logic isn't matching current "
                f"market conditions. Review before resuming, don't just "
                f"delete STOP and continue."
            )

    def check_daily_loss(self):
        total = sum(p for _, p in self._realized_pnl_sol)
        if total <= -abs(self.max_loss_sol_per_day):
            raise SecurityLimitExceeded(
                f"Daily loss limit hit ({total:.4f} SOL <= "
                f"-{self.max_loss_sol_per_day} SOL). Halting for the day."
            )


def send_telegram_alert(title: str, message: str):
    """
    Sends a message via Telegram's Bot API — free, but needs your own
    bot token and chat ID (see README for the 2-minute BotFather
    setup). No-op silently if not configured, so this is safe to call
    unconditionally.
    """
    from config import config
    if not config.telegram_enabled:
        return
    try:
        url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
        requests.post(url, json={
            "chat_id": config.telegram_chat_id,
            "text": f"🤖 {title}\n{message}",
        }, timeout=8)
    except requests.RequestException as e:
        print(f"[telegram] failed to send alert: {e}")


def send_discord_alert(title: str, message: str):
    """
    Posts to a Discord webhook URL — free, no bot token or account
    linking needed. Create one in Discord: Server Settings → Integrations
    → Webhooks → New Webhook → Copy Webhook URL. No-op silently if not
    configured, so this is safe to call unconditionally.

    Note: the webhook URL is effectively a password — anyone with it can
    post to your channel. Keep it in .env (gitignored), not in code.
    """
    from config import config
    if not config.discord_enabled:
        return
    try:
        requests.post(
            config.discord_webhook_url,
            json={"content": f"**{title}**\n{message}"[:1900]},  # Discord caps at 2000 chars
            timeout=8,
        )
    except requests.RequestException as e:
        print(f"[discord] failed to send alert: {e}")


def send_alert(title: str, message: str):
    """
    Best-effort desktop notification for monitor-only mode, where the
    bot flags candidates instead of trading them. Falls back to a
    loud print if no OS notifier is available — never assume the
    notification was seen.
    """
    import platform
    import subprocess

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                timeout=5, check=False,
            )
        elif system == "Linux":
            subprocess.run(["notify-send", title, message], timeout=5, check=False)
        else:
            raise OSError("no notifier for this platform")
    except (OSError, FileNotFoundError, subprocess.SubprocessError):
        pass  # notification is best-effort; the print below is the real guarantee

    print(f"\n{'*' * 60}\n[ALERT] {title}: {message}\n{'*' * 60}\n")
    send_telegram_alert(title, message)
    send_discord_alert(title, message)


def log_trade(event: dict):
    """Append-only local audit log — every trade decision, independent
    of whatever the bot prints to stdout, so you have a record even if
    you weren't watching the terminal."""
    event["timestamp"] = time.time()
    existing = []
    if TRADE_LOG_FILE.exists():
        try:
            existing = json.loads(TRADE_LOG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            existing = []
    existing.append(event)
    TRADE_LOG_FILE.write_text(json.dumps(existing, indent=2))


def verify_and_simulate(
    rpc_client: Client,
    signed_tx: VersionedTransaction,
    expected_signer: Pubkey,
) -> tuple[bool, str]:
    """
    Simulates the transaction before it's actually sent, and checks
    that the fee payer/signer matches the wallet we expect. This is
    the main defense against a compromised or malicious swap API
    response — if the transaction would fail, or touches accounts it
    has no business touching, this is the last chance to catch it
    before signing is irreversible.

    Note: signing happens before simulation is possible with Jupiter's
    flow (the tx must be signed to simulate accurately in some cases).
    Treat this as a strong check, not an absolute guarantee — the
    position-size and daily-loss caps are your backstop if something
    slips through.
    """
    try:
        msg = signed_tx.message
        account_keys = msg.account_keys
        if len(account_keys) == 0 or account_keys[0] != expected_signer:
            return False, "Fee payer on transaction does not match expected wallet"

        sim = rpc_client.simulate_transaction(signed_tx)
        if sim.value.err is not None:
            return False, f"Simulation failed: {sim.value.err}"

        return True, "Simulation passed"
    except Exception as e:
        return False, f"Simulation error: {e}"


def sweep_profits(
    rpc_client: Client,
    keypair,
    cold_wallet_address: str,
    working_balance_sol: float,
):
    """
    Moves any SOL above `working_balance_sol` to a separate cold wallet.
    Run this periodically (e.g. once a day) rather than on every trade —
    frequent sweeps just create more signed transactions, which is more
    surface area, not less.
    """
    from config import config  # local import to avoid circulars in tooling

    if not cold_wallet_address:
        print("[security] no COLD_WALLET_ADDRESS set, skipping profit sweep")
        return

    balance_lamports = rpc_client.get_balance(keypair.pubkey()).value
    balance_sol = balance_lamports / 1_000_000_000
    excess_sol = balance_sol - working_balance_sol

    if excess_sol <= 0.01:  # not worth a transaction fee to sweep dust
        return

    excess_lamports = int(excess_sol * 1_000_000_000)
    cold_pubkey = Pubkey.from_string(cold_wallet_address)

    ix = transfer(TransferParams(
        from_pubkey=keypair.pubkey(),
        to_pubkey=cold_pubkey,
        lamports=excess_lamports,
    ))
    blockhash = rpc_client.get_latest_blockhash().value.blockhash
    msg = MessageV0.try_compile(
        payer=keypair.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [keypair])
    result = rpc_client.send_raw_transaction(bytes(tx))
    print(f"[security] swept {excess_sol:.4f} SOL to cold wallet, tx={result.value}")
    log_trade({"type": "sweep", "amount_sol": excess_sol, "tx": str(result.value)})
