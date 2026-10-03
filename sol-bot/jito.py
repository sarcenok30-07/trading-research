"""
MEV protection via Jito bundles.

Normally a signed transaction goes into the public mempool, where
bots can see it before it lands and front-run or sandwich it — buy
just ahead of you, sell just after, pocketing the price movement your
own trade caused. This is the single biggest reason retail trades get
worse fills than the quote suggested.

Jito bundles route the transaction directly to Jito-connected
validators instead of the public mempool, with a small tip paid to
the validator. It doesn't guarantee no MEV, but it closes the "anyone
watching the mempool can see and react to your trade before it lands"
vector, which is the main one that matters for a bot like this.
"""
import requests
import base58
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0
from solders.hash import Hash

JITO_BLOCK_ENGINE_URL = "https://mainnet.block-engine.jito.wtx/api/v1/bundles"

# Jito tip accounts (any one of these works; round-robin is fine)
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
]

DEFAULT_TIP_LAMPORTS = 100_000  # ~0.0001 SOL, adjust based on urgency/competition


def _build_tip_transaction(keypair: Keypair, tip_lamports: int, recent_blockhash: Hash) -> VersionedTransaction:
    tip_account = Pubkey.from_string(JITO_TIP_ACCOUNTS[0])
    ix = transfer(TransferParams(
        from_pubkey=keypair.pubkey(),
        to_pubkey=tip_account,
        lamports=tip_lamports,
    ))
    msg = MessageV0.try_compile(
        payer=keypair.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=recent_blockhash,
    )
    tx = VersionedTransaction(msg, [keypair])
    return tx


def send_as_jito_bundle(
    signed_swap_tx: VersionedTransaction,
    keypair: Keypair,
    recent_blockhash: Hash,
    tip_lamports: int = DEFAULT_TIP_LAMPORTS,
) -> str | None:
    """
    Submits the signed swap transaction alongside a tip transaction as
    a Jito bundle. Returns the bundle ID, or None on failure (caller
    should fall back to a normal sendTransaction if this fails, rather
    than silently not trading).
    """
    tip_tx = _build_tip_transaction(keypair, tip_lamports, recent_blockhash)

    swap_tx_b58 = base58.b58encode(bytes(signed_swap_tx)).decode("utf-8")
    tip_tx_b58 = base58.b58encode(bytes(tip_tx)).decode("utf-8")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendBundle",
        "params": [[swap_tx_b58, tip_tx_b58]],
    }

    try:
        resp = requests.post(JITO_BLOCK_ENGINE_URL, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            print(f"[jito] bundle rejected: {result['error']}")
            return None
        return result.get("result")  # bundle ID
    except requests.RequestException as e:
        print(f"[jito] bundle submission failed: {e}")
        return None
