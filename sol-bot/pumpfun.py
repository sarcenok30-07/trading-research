"""
pump.fun integration, via PumpPortal (pumpportal.fun) — a free,
unofficial, third-party WebSocket API. pump.fun itself has no official
public API; PumpPortal is the de facto standard the community built to
fill that gap. Its `subscribeNewToken` and `subscribeMigration`
streams are free with no API key.

IMPORTANT SCOPE LIMIT — read before assuming this auto-trades launches:

This module tracks two different events, and only ONE of them feeds
the bot's actual trading pipeline:

  1. NEW TOKEN CREATION (bonding-curve phase) — logged/alerted only,
     NEVER auto-traded. The overwhelming majority of pump.fun launches
     are abandoned or outright rugs within minutes. Trading during the
     bonding-curve phase also requires a completely different
     execution path (PumpPortal's own trade API, not Jupiter — Jupiter
     doesn't route bonding-curve trades), which this bot does not
     implement. If you want that built later, it's a substantial,
     separate, higher-risk addition — ask explicitly, don't assume
     it's covered by "pump.fun integration."

  2. MIGRATION / GRADUATION (bonding curve fills, token moves to a
     real AMM pool on PumpSwap/Raydium) — THIS feeds the trading
     pipeline, as a candidate passed to trader.open_position(), same
     as a breakout candidate from price_feed.py. It still goes through
     every existing safety check (rug check, honeypot check, liquidity
     depth, correlation cap) before anything is bought. The rationale:
     graduation is the point a token first has real, Jupiter-routable
     liquidity — catching it right at graduation is genuinely earlier
     than waiting for it to show up in a DexScreener search poll, and
     it's not meaningfully riskier than any other breakout candidate
     since the same screening applies.

Runs in a background thread with its own asyncio event loop so it can
coexist with main.py's synchronous polling loop without a rewrite.
"""
import json
import threading
import time
import queue
from collections import deque

try:
    import websockets
except ImportError:
    websockets = None  # handled at startup in main.py with a clear error

from config import config

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"

# Bounded so a burst of activity (pump.fun creates thousands of tokens
# a day) can't grow memory unbounded — recent-only view is what
# matters for both alerting and the trading queue.
_recent_new_tokens: deque = deque(maxlen=200)
_migration_queue: "queue.Queue[dict]" = queue.Queue(maxsize=500)

_listener_thread: threading.Thread | None = None
_stop_event = threading.Event()


def get_recent_new_tokens(limit: int = 20) -> list[dict]:
    """Informational only — recent bonding-curve launches seen. Not
    used for trading."""
    return list(_recent_new_tokens)[-limit:]


def get_pending_migrations(max_items: int = 10) -> list[dict]:
    """Drains up to max_items migration events for main.py to feed
    into the trading pipeline this poll cycle."""
    items = []
    for _ in range(max_items):
        try:
            items.append(_migration_queue.get_nowait())
        except queue.Empty:
            break
    return items


async def _listen():
    """Connects to PumpPortal, subscribes to the two free streams, and
    routes events into the module-level buffers. Reconnects with
    backoff on disconnect rather than dying silently."""
    backoff = 5
    while not _stop_event.is_set():
        try:
            async with websockets.connect(PUMPPORTAL_WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                print("[pumpfun] connected to PumpPortal, subscribed to new tokens + migrations")
                backoff = 5  # reset after a successful connect

                while not _stop_event.is_set():
                    try:
                        raw = await asyncio_wait_for_compat(ws, timeout=30)
                    except TimeoutError:
                        continue  # just a quiet period, keep looping
                    if raw is None:
                        break  # connection closed

                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    _handle_event(event)

        except Exception as e:
            print(f"[pumpfun] connection error: {e}, reconnecting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def asyncio_wait_for_compat(ws, timeout: float):
    import asyncio
    try:
        return await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError()


def _handle_event(event: dict):
    """PumpPortal's event shape varies by type; this handles the
    common fields defensively rather than assuming a fixed schema,
    since it's a third-party API that can change without notice."""
    mint = event.get("mint") or event.get("mintAddress") or event.get("ca")
    if not mint:
        return

    event_type = event.get("txType") or event.get("type") or ""

    if "migrat" in str(event_type).lower() or event.get("pool") in ("raydium", "pump-amm"):
        migration_event = {
            "mint": mint,
            "symbol": event.get("symbol", "?"),
            "name": event.get("name", "?"),
            "timestamp": time.time(),
            "raw": event,
        }
        try:
            _migration_queue.put_nowait(migration_event)
            print(f"[pumpfun] MIGRATION detected: {migration_event['symbol']} ({mint[:8]}...) — queued for screening")
        except queue.Full:
            print("[pumpfun] migration queue full, dropping event (bot may be behind)")
    else:
        _recent_new_tokens.append({
            "mint": mint,
            "symbol": event.get("symbol", "?"),
            "name": event.get("name", "?"),
            "timestamp": time.time(),
        })


def start_listener():
    """Starts the background listener thread. Safe to call once at
    bot startup; no-op if already running or if pump.fun integration
    is disabled in config."""
    global _listener_thread

    if not config.pumpfun_enabled:
        return

    if websockets is None:
        print("[pumpfun] 'websockets' package not installed — pump.fun "
              "integration disabled. Run: pip install websockets")
        return

    if _listener_thread and _listener_thread.is_alive():
        return

    import asyncio

    def _run():
        asyncio.run(_listen())

    _listener_thread = threading.Thread(target=_run, daemon=True)
    _listener_thread.start()
    print("[pumpfun] listener thread started")


def stop_listener():
    _stop_event.set()
