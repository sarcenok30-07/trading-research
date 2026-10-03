"""
Whale wallet tracking — ALERT ONLY, does not auto-trade.

Watches a list of wallet addresses you supply (WHALE_WALLETS in .env)
for recent buy activity, using free Solana RPC calls
(getSignaturesForAddress + getTransaction) — no paid API needed.

Important honest limitation: there is no list of "known profitable
wallets" built into this. You have to supply real addresses yourself
— e.g. wallets you've identified from DexScreener's "top traders" view
on tokens that did well, or addresses you already track elsewhere.
Watching a wallet that isn't actually good at this tells you nothing
useful; the tool is only as good as the addresses you feed it.

This deliberately does NOT feed into open_position() / auto-trading.
Copy-trading a wallet automatically is a meaningfully different (and
riskier) strategy than breakout detection — it assumes the watched
wallet's judgment is better than the bot's own screening, which may
or may not be true, and a whale wallet can be wrong or itself exiting
into you. If you want auto-copy-trading built later, that's a
separate, explicit ask — for now this only alerts you so you can
decide manually.
"""
import time
from solana.rpc.api import Client

from config import config
from wallet_security import send_alert

# Tracks the most recent signature seen per wallet, so we only alert
# on genuinely new activity, not the same historical transactions
# every poll cycle.
_last_seen_signature: dict[str, str] = {}


def check_whale_activity(rpc_client: Client):
    """
    Call this once per poll cycle from main.py. Checks each configured
    wallet for new transactions since last check and alerts on any.

    Honest limitation: this does NOT parse transaction contents to
    distinguish buys from sells or unrelated activity — Solana swap
    transactions are non-trivial to parse reliably across every DEX
    program. It alerts on ANY new transaction from a watched wallet;
    you decide from the Solscan link what it actually was. Filtering
    to buys specifically would be a further addition if useful.
    """
    if not config.whale_tracking_enabled:
        return

    for wallet_address in config.whale_wallets:
        try:
            _check_one_wallet(rpc_client, wallet_address)
        except Exception as e:
            print(f"[whale_tracker] error checking {wallet_address[:8]}...: {e}")


def _check_one_wallet(rpc_client: Client, wallet_address: str):
    from solders.pubkey import Pubkey

    pubkey = Pubkey.from_string(wallet_address)
    sigs_resp = rpc_client.get_signatures_for_address(pubkey, limit=5)
    signatures = sigs_resp.value
    if not signatures:
        return

    last_seen = _last_seen_signature.get(wallet_address)
    new_sigs = []
    for sig_info in signatures:
        sig_str = str(sig_info.signature)
        if sig_str == last_seen:
            break
        new_sigs.append(sig_info)

    if not signatures:
        return
    _last_seen_signature[wallet_address] = str(signatures[0].signature)

    if last_seen is None:
        # First time seeing this wallet — just record where we are,
        # don't alert on its entire history at once.
        return

    for sig_info in reversed(new_sigs):
        if sig_info.err is not None:
            continue  # failed transaction, not worth alerting on
        send_alert(
            "Whale wallet activity",
            f"{wallet_address[:8]}...{wallet_address[-4:]} made a new "
            f"transaction: https://solscan.io/tx/{sig_info.signature}"
        )
        print(f"[whale_tracker] activity from {wallet_address[:8]}...: {sig_info.signature}")
