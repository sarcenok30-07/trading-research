# Solana Breakout Bot (scaffold)

Watches Solana DEX pairs for price/volume breakouts, opens a position sized
as a % of your wallet, and exits on stop-loss or take-profit. Runs entirely
on your own machine, with your own key.

## Before you touch real money

- **This is a starting scaffold, not audited financial software.** Read
  every file before running it live. I (the AI that wrote this) cannot
  verify it's bug-free, and a bug in trading code costs real money.
- **Meme coin markets are adversarial.** You are competing against bots
  with faster infrastructure. Breakout chasing in particular is prone to
  buying tops right before a dump — a token up 10% in 15 minutes can
  easily be a pump about to reverse, not the start of a trend.
- **DexScreener and Jupiter are third-party services.** Their APIs can
  change, rate-limit you, or go down. Don't assume this bot's view of
  the market is complete or current.
- **Start in DRY_RUN.** Watch its decisions for at least a few days
  before enabling real trades. If its logic doesn't match your
  judgment, that's information — fix the logic, don't override it by
  hand while it's live.
- **Only fund the wallet you connect with money you can lose entirely.**
  `MAX_WALLET_SOL_BALANCE` is a rail to stop you from accidentally
  pointing this at your main wallet, not a guarantee of safety.

## Setup

```bash
cd sol-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
- Leave `DRY_RUN=true` for now.
- Fill in `RPC_URL` (a free public one works for dry-run testing; get a
  paid RPC from Helius/QuickNode/Triton before going live — public RPCs
  are too slow/rate-limited for real trading).
- Set your risk parameters. Defaults are conservative starting points,
  not recommendations.
- Leave `WALLET_PRIVATE_KEY` empty until you're ready to go live.

## Run

```bash
python3 main.py
```

It polls every 30 seconds, logs breakout candidates and (in dry run)
what it *would* trade.

## Going live

1. Create a **fresh wallet** — don't use your main one.
2. Fund it with a small, deliberately-limited amount of SOL.
3. Get your key into the bot, one of two ways:
   - **Plaintext (simpler, weaker):** export from Phantom (Settings →
     Export Private Key) and paste into `.env` as `WALLET_PRIVATE_KEY`.
   - **Encrypted (recommended):** run `python3 key_storage.py encrypt`,
     paste your key when prompted, set a passphrase. Then set
     `USE_ENCRYPTED_KEY=true` in `.env` and leave `WALLET_PRIVATE_KEY`
     blank.
4. Set `DRY_RUN=false`. Consider starting with `MODE=monitor` first so
   the bot alerts you to candidates without trading, before switching
   to `MODE=trade`.
5. Watch the first several trades closely. Keep `Ctrl+C` (or the `STOP`
   file kill switch) handy.

## Rug-pull screening (rugcheck.py)

Before opening any position, the bot checks:
1. RugCheck's public API — community risk score, mint/freeze authority
   status, liquidity lock status.
2. If RugCheck has no data (common for brand-new tokens), falls back
   to a direct on-chain check of mint/freeze authority via RPC.

It **fails closed**: if neither source gives a clear signal, it refuses
to trade rather than assuming the token is fine. Toggle with
`RUG_CHECK_ENABLED` — leave it on.

**Limits:** this catches obvious rugs (unrenounced mint authority, no
liquidity lock). It does not catch insider wallets disguised as
separate holders, fake/temporary locks, or a team that rugs through
means the checks don't cover. Treat a pass as "not an obvious rug,"
not "safe."

## MEV protection (jito.py)

Buy transactions are submitted as Jito bundles instead of the public
mempool, with a small tip to the validator (`JITO_TIP_LAMPORTS`). This
closes the main vector where other bots watch the mempool, see your
trade about to land, and buy ahead of you / sell into you (sandwich
attacks) — the biggest reason retail fills are worse than the quoted
price. Sells skip Jito since there's no upside to bundling an exit;
speed matters more there.

**Limits:** this isn't a guarantee of zero MEV, and bundle landing
isn't guaranteed — if a bundle fails, the code falls back to a direct
send. Raise the tip if you're regularly losing bundle races on
contested trades.

## New safety features

**Honeypot detection (honeypot.py)** — before buying, checks that the
token can actually be sold: requests a reverse (sell) quote and flags
tokens with no sell route, or where a round-trip buy+sell would lose
more than ~15% beyond normal fees. This is the check that catches
"you can buy but can't sell" traps, which price/volume/rug-authority
checks don't see.

**Liquidity depth check (rugcheck.py)** — requests a quote at your
actual position size and refuses to trade if it would move the pool
price by more than `MAX_PRICE_IMPACT_PCT`. A token can look fine at
tiny size and be a trap at the size you'd actually trade.

**Holder concentration check (rugcheck.py)** — pulls RugCheck's top
holder data and refuses if any single non-LP wallet holds more than
`MAX_TOP_HOLDER_PCT` (20% by default) of supply — that's a wallet that
can crash the price alone.

**Consecutive-loss pause (wallet_security.py)** — separate from the
daily SOL loss cap, this halts the bot after `MAX_CONSECUTIVE_LOSSES`
losing trades in a row. A string of small losses can mean "the
breakout strategy stopped matching current market conditions" well
before the cumulative SOL cap trips.

**Manual confirmation for large trades** — any trade sized above
`MANUAL_CONFIRM_THRESHOLD_SOL` pauses and requires you to type `yes`
in the terminal (plus a desktop notification) before it executes. Set
to `0` to require confirmation on every trade while you're building
trust in the bot.

**Monitor mode (`MODE=monitor`)** — the bot runs its full pipeline
(breakout detection, rug check, honeypot check, liquidity check) but
never trades. It sends a desktop notification and logs the candidate
instead, so you can review and trade manually. Good middle ground
before `MODE=trade`.

**Encrypted key storage (key_storage.py)** — instead of a plaintext
key in `.env`, encrypt it with a passphrase:
```bash
python3 key_storage.py encrypt
```
This writes `wallet.enc` + `wallet.salt` (both gitignored). Set
`USE_ENCRYPTED_KEY=true` and remove `WALLET_PRIVATE_KEY` from `.env`.
The bot will prompt for your passphrase at startup (hidden input) and
hold the decrypted key in memory only for the process lifetime.
**Caveat:** this protects the key at rest, not a compromised running
process — see the security limits section below.

**Devnet mode (`NETWORK=devnet`)** — switches the default RPC to
Solana devnet, useful for testing wallet/transaction mechanics with
fake SOL. **It does not test your trading logic** — Jupiter and
DexScreener don't have meaningful devnet liquidity. For testing the
actual pipeline (breakout detection → rug check → honeypot check →
sizing) against real market data without risking money, use
`DRY_RUN=true` on mainnet instead — that's the more useful "testnet."

## Strategy, execution, and portfolio features

**Multi-timeframe confirmation** (`CONFIRMATION_POLLS`) — a breakout
must hold across N consecutive polls (default 2) before the bot acts
on it, filtering out single-poll wicks that reverse immediately.

**SOL correlation filter** (`SOL_CORRELATION_FILTER_ENABLED`) — pauses
new buys when SOL itself is down more than `SOL_DUMP_THRESHOLD_PCT`
over the lookback window, since meme coins are highly correlated to
SOL and a "breakout" during a SOL dump is usually a dead cat bounce.

**Dynamic slippage** (`DYNAMIC_SLIPPAGE_ENABLED`) — scales slippage
tolerance with a token's recent price volatility instead of a fixed
1%, between `MIN_SLIPPAGE_BPS` and `MAX_SLIPPAGE_BPS`.

**Trailing stop-loss** (`TRAILING_STOP_ENABLED`) — once a position is
up `TRAILING_ACTIVATION_PCT`, the exit switches from the fixed
stop-loss to trailing `TRAILING_STOP_PCT` below the peak price seen
since entry. This is the single change most likely to improve real
P&L versus the original fixed stop/target — it locks in gains instead
of letting a winning trade round-trip back to breakeven or a loss.

**Partial exits** (`PARTIAL_EXIT_ENABLED`) — at the take-profit
target, sells `PARTIAL_EXIT_PCT` (default 50%) of the position and
lets the remainder ride under the trailing stop, instead of exiting
the whole position at once.

**Volatility-based sizing** (`VOLATILITY_SIZING_ENABLED`) — scales
position size down for more volatile tokens and up (within
`MIN_SIZE_MULTIPLIER`/`MAX_SIZE_MULTIPLIER` bounds) for calmer ones,
instead of a flat % of wallet regardless of risk.

**Correlation cap across positions** (`portfolio.py`,
`CORRELATION_CAP_ENABLED`) — computes real Pearson correlation between
a candidate's price history and each currently-open position's
history; refuses the trade if correlation exceeds
`MAX_POSITION_CORRELATION`. Degrades gracefully (allows the trade)
when there isn't enough price history yet to compute correlation —
this is a risk-reduction layer, not a guarantee.

## Observability

**Stats dashboard** — run `python3 stats.py` any time to see win rate,
average win/loss, profit factor, max drawdown, and counts of
blocked/skipped/declined trades, pulled from `trade_log.json`. This is
how you judge whether the strategy is actually working, rather than
eyeballing scrolling terminal output.

**Backtesting** (`backtest.py`) — replays the breakout/confirmation/
stop-loss/trailing-stop/take-profit logic against historical price
data you supply as a CSV (`timestamp,pair_address,symbol,price_usd,
volume_24h_usd`). **Important:** DexScreener's free API has no
historical endpoint, so this doesn't fetch anything on its own. Two
ways to get real data:
  1. Run `python3 collect_history.py` alongside the bot to build up a
     real price history CSV over time from live polling.
  2. Use a paid historical data provider (Birdeye, Bitquery, etc.) and
     export to the same CSV format.

Backtest results don't simulate rug-check/honeypot/liquidity-depth
screening (those need live API calls), so live performance will
generally be worse than backtest numbers — treat backtests as
directional signal on the entry/exit logic, not a P&L guarantee.

### Running the collector unattended

`collect_history.py` polls every 30 seconds and writes to
`price_history_<date>.csv`, rotating to a new file each UTC day so no
single file grows unbounded over weeks of collection. It logs to both
the terminal and `collector.log`.

Running it in the foreground (`python3 collect_history.py`) works but
dies the moment you close the terminal or your machine sleeps. Two
options for actually unattended collection:

**Option A — background wrapper with auto-restart** (works on
macOS/Linux, no extra setup):
```bash
./run_collector.sh start    # runs detached, auto-restarts on crash
./run_collector.sh status   # check it's alive, see recent log lines
./run_collector.sh stop     # stop it
```

**Option B — systemd service** (Linux, survives reboots): see
`sol-bot-collector.service` for setup steps. Use this if you're
running on a server/VM you want the collector on permanently.

Once you've got a few days or weeks of `price_history_*.csv` files:
```bash
python3 backtest.py "price_history_*.csv"
```

## pump.fun integration (pumpfun.py)

Uses PumpPortal (pumpportal.fun) — a free, unofficial, third-party
WebSocket API, since pump.fun has no official public API of its own.
No API key needed for the data streams this uses.

**Scope, read carefully:** two different events are tracked, and only
one feeds actual trading:

- **Migrations/graduations** (a token's bonding curve fills and it
  moves to real AMM liquidity on PumpSwap/Raydium) — **this feeds the
  trading pipeline**, going through every existing safety check (rug
  check, honeypot check, liquidity depth, correlation cap) exactly
  like a breakout candidate. The idea: graduation is the first moment
  a token has real, Jupiter-routable liquidity, so catching it right
  then is genuinely earlier than waiting for it to surface in a
  DexScreener search poll — without taking on more risk, since the
  same screening applies either way.
- **New token creation** (bonding-curve phase) — **logged only, never
  auto-traded.** Most pump.fun launches are abandoned or rugged within
  minutes, and trading during the bonding-curve phase needs a
  completely different execution path (PumpPortal's own trade API,
  since Jupiter doesn't route bonding-curve trades) that isn't
  implemented here. If you want that built, it's a substantial,
  separate, higher-risk addition — say so explicitly rather than
  assuming "pump.fun integration" already covers it.

Enable with `PUMPFUN_ENABLED=true`. Runs in a background thread
alongside the main polling loop; reconnects automatically with
backoff if the WebSocket drops (PumpPortal is third-party
infrastructure, not something this bot controls the uptime of).

## Dashboard chat assistant (chatbot.py)

The dashboard (`dashboard.py`) has a chat bubble in the bottom-right
that answers questions about the collector — status, top movers, data
volume, trading stats if any exist, and definitions of terms like
"breakout" or "honeypot".

**This is deliberately NOT an LLM.** It's plain keyword matching
against a fixed set of recognized questions, answered with real
numbers pulled from the same data the dashboard shows — zero API
calls, zero ongoing cost, nothing that can run out of credits. The
tradeoff: it only understands questions close to what it's been
taught to recognize, not open-ended novel phrasing. If you want a real
LLM-backed version later, that's a separate small addition (an API key
+ a proxy endpoint) — ask for it explicitly, since it does carry a
small per-query cost unlike everything else in this project.

## Known issue: pre-fix price history is not usable

If you collected data before this fix, check it:

```bash
cut -d, -f3 price_history_*.csv | sort -u | head
```

If the only symbol is `SOL`, that data is worthless and should be
deleted. The original `fetch_solana_pairs()` called DexScreener's
search endpoint with the literal string `"solana"`, which returns
pairs matching that *text* — mostly dead pools with SOL as the base
token and sub-dollar volume. The breakout strategy could never have
fired on it, and a backtest against it would return nothing while
looking like the strategy was at fault.

The feed now discovers tokens via DexScreener's `token-boosts` and
`token-profiles` endpoints (where meme coin activity actually is),
fetches their pair data in batches, keeps only the deepest-liquidity
pool per token, and filters out major/quote currencies and dust pools.

Delete the old files and restart collection:

```bash
rm price_history_*.csv
systemctl restart sol-bot-collector
```

## Alerts (Discord / Telegram)

Both free, both optional, use either or both. Alerts fire on trade
candidates, whale activity, security-guard trips, and snipe
confirmations.

**Discord** (simplest): Server Settings → Integrations → Webhooks →
New Webhook → Copy Webhook URL → paste into `DISCORD_WEBHOOK_URL`.
Treat that URL like a password — anyone holding it can post to your
channel.

**Telegram**: message `@BotFather`, send `/newbot`, copy the token into
`TELEGRAM_BOT_TOKEN`. Then message your new bot once and visit
`https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` to find your
chat ID for `TELEGRAM_CHAT_ID`.

WhatsApp was considered and deliberately skipped: the official
Business API costs money per message and needs business verification,
and the free unofficial libraries violate WhatsApp's ToS in a way that
risks getting your personal number banned.

## Copy-trading (leaderboard.py + copytrader.py)

Discovers top traders from leaderboard APIs, evaluates them on paper,
then allocates real capital proportional to how well they're actually
doing.

### Getting wallets (leaderboard.py)

Three providers, all optional — set whichever keys you have:

| Provider | Scope | Notes |
|---|---|---|
| Vybe | Global wallet leaderboard | Most directly useful; not tied to one token |
| Birdeye | Top traders *per token* | Queried against currently-trending tokens |
| Solana Tracker | Global PnL leaderboard | Least-documented API of the three |

**GMGN, pump.fun, and FOMO were deliberately skipped.** Their
leaderboards are web UIs behind Cloudflare with no public API.
Scraping them violates their terms, breaks on markup changes, and gets
IP-blocked — meaning the bot would silently follow a stale wallet list.

**Verify before trusting it:**
```bash
python3 leaderboard.py test
```
This prints what each configured provider actually returns. I could
not test these endpoints live — my sandbox can't reach those hosts, so
paths, headers, and response shapes come from provider docs, not a
verified call. Parsing is defensive (tries multiple field names), but
the test command is how you find out whether it worked. Base URLs are
overridable in `.env` if a provider has moved.

### Why not simple rotation

The obvious design — follow the top wallet, drop after N losses, move
to the next — is brittle. At a ~45% win rate, three losses in a row
happens constantly by chance, so you'd churn through wallets on noise,
and one bad week permanently discards a wallet that might recover.

Instead, **allocation is proportional to score**. A wallet that starts
underperforming gets smaller automatically; if its score hits the
floor, allocation reaches zero. That's rotation, but continuous and
self-correcting, with no "how many failures" threshold to tune.

### Three tiers

1. **Paper** — newly discovered. The bot records what copying it
   *would* have made, using real prices, spending nothing. Free to
   evaluate.
2. **Active** — real allocation, sized by score relative to other
   active wallets.
3. **Benched** — score fell below the floor, or the drawdown backstop
   tripped. Still tracked on paper, so it can earn its way back.

### The survivorship-bias problem

A "top trader today" leaderboard ranks who got *lucky* today. In meme
coins, the top daily PnL wallet is very often someone who made one
concentrated bet that hit — and that trade is already over by the time
you see it.

The scorer is built specifically to reject those. It weights win rate
(70%) and *median* return (30%) — median, not mean, so one outlier win
can't carry an otherwise-losing wallet. Tested against a synthetic
wallet with one 400% win and eleven losses: it has the highest total
PnL of any test wallet (+326%) and would top a PnL-ranked leaderboard,
and it scores **0.0**.

### Backstops

- `COPYTRADE_ALLOCATION_PCT` caps total copy-trade exposure; config
  refuses to start above 50% (this follows strangers' decisions — it
  shouldn't control most of your wallet)
- `COPYTRADE_MAX_DRAWDOWN_SOL` hard-cuts a wallet after that much
  cumulative real loss, independent of score, because the rolling
  score reacts slowly by design
- Config rejects `BENCH_SCORE >= PROMOTE_SCORE`, which would make
  wallets flap between tiers every cycle

Check status any time:
```bash
python3 copytrader.py
```

### Remaining limitation

Detecting *what* a followed wallet bought still depends on parsing
Solana swap transactions, which `whale_tracker.py` does not do — it
alerts on any new transaction and hands you a Solscan link. Until that
parsing exists, copy-trading runs in paper/manual mode: the scoring,
tiering, and allocation machinery is complete and tested, but the
trade-detection step that would feed it automatically is not. That's
the next piece of work if you want this fully automatic.

## Whale wallet tracking (whale_tracker.py)

Watches wallet addresses you list in `WHALE_WALLETS` and alerts on new
transactions, using free Solana RPC calls.

Two honest limits:
- **There's no built-in list of profitable wallets.** You supply the
  addresses (e.g. from DexScreener's top-traders view on tokens that
  did well). The tool is only as good as the addresses you feed it.
- **It alerts on any new transaction, not specifically buys.** Parsing
  Solana swap transactions reliably across every DEX program is
  non-trivial and isn't implemented. You get a Solscan link and decide
  what it was.

It deliberately does **not** auto-copy-trade. Copy-trading assumes the
watched wallet's judgment beats your own screening, which may not hold
— and a whale can be exiting into you.

## Per-token exposure cap and stale-quote check

**`MAX_TOKEN_EXPOSURE_PCT`** — hard ceiling on how much of the wallet
any single token can take, regardless of what volatility sizing
calculates. Oversized positions get clamped down rather than skipped,
since the trade may still be worth taking at a smaller size.

**`STALE_QUOTE_CHECK_ENABLED`** — re-checks price immediately before
executing. If it moved more than `STALE_QUOTE_MAX_DIVERGENCE_PCT`
since the signal fired, the trade is skipped as stale. A failed price
lookup does *not* block the trade — only a confirmed divergence does.

## Maintenance (maintenance.py)

Without this, price history CSVs, logs, and `trade_log.json` grow
without limit on a 10GB droplet. Run it daily:

```bash
crontab -e
# add:
0 4 * * * cd /root/sol-bot && /root/sol-bot/venv/bin/python3 maintenance.py >> maintenance.log 2>&1
```

It gzips history older than 2 days (~80% smaller), deletes archives
past `BACKUP_RETENTION_DAYS`, truncates oversized logs keeping recent
lines, and trims `trade_log.json` (with a `.bak` copy first).
`backtest.py` reads the gzipped archives transparently, so compression
doesn't shrink your backtest dataset.

**This is local housekeeping, not offsite backup.** If the droplet
dies, the data dies with it. For real backups, either enable
DigitalOcean's snapshots (small monthly cost) or periodically pull the
files down:

```bash
scp root@YOUR_DROPLET_IP:~/sol-bot/price_history_* ~/Downloads/sol-bot-backups/
```

For uptime monitoring, UptimeRobot's free tier can ping
`http://YOUR_DROPLET_IP:8080` every 5 minutes and email you if the
dashboard goes down — that's a signup on their site, not something in
this codebase.

## Bonding-curve sniping (snipe.py) — HIGH RISK, off by default

Buys brand-new pump.fun launches before they graduate to real
liquidity. **This is the riskiest thing in this project** and is
disabled by default. Read `snipe.py`'s docstring in full before
enabling.

The core problem: `rugcheck.py` and `honeypot.py` **cannot
meaningfully screen a brand-new mint** — there's no liquidity pool, no
holder history, nothing to evaluate. A new token passes "no red flags"
trivially because there are no flags yet. Most pump.fun launches are
abandoned or rugged within minutes; that's closer to the median
outcome than a tail risk. And you're racing infrastructure-grade
snipers that will win most speed contests against a $6/month droplet.

Guardrails, all deliberate:
- `SNIPE_ENABLED=false` by default
- `config.py` refuses to start if `SNIPE_MAX_SOL` exceeds 0.5
- `SNIPE_ALWAYS_CONFIRM=true` requires typing `yes` per snipe (which
  will usually mean missing the launch — that delay is the point)
- Requires a separate `PUMPPORTAL_API_KEY`, since execution goes
  through PumpPortal's API rather than Jupiter

## Wallet security (wallet_security.py)

- **Transaction simulation + verification before every send** — checks
  the fee payer matches your wallet and simulates the transaction
  before it's signed and broadcast. This is the main defense against
  a compromised or buggy swap API response trying to redirect funds.
- **Spending rate limits** — `MAX_TRADES_PER_HOUR` and
  `MAX_LOSS_SOL_PER_DAY` halt the bot automatically if breached. This
  catches bugs and runaway logic, not just bad luck.
- **Kill switch** — create a file named `STOP` in the bot's directory
  (`touch STOP`) and the bot halts on its next cycle, before any
  further trade. Delete the file to resume.
- **Audit log** — every trade decision (executed, blocked, skipped) is
  appended to `trade_log.json`, independent of what scrolls past in
  the terminal.
- **Profit sweeping** — once a day, SOL above `WORKING_BALANCE_SOL` is
  moved to `COLD_WALLET_ADDRESS`, a separate wallet this bot never
  trades from. So even if the trading wallet is later compromised,
  swept profits are already out of reach.

### What this does NOT protect against

Be honest with yourself about this list — it's the actual risk, not
the code:

- **A compromised machine.** If malware or a keylogger has access to
  the computer running this bot, it can read `.env` directly. No
  application-level code fixes that. Run this on a dedicated
  machine/VM you don't browse the web or install random software on.
- **A malicious or compromised RPC/API provider.** The simulation
  check catches a bad transaction payload, but if your RPC provider
  itself is malicious, you have bigger problems. Use a reputable
  provider (Helius, QuickNode, Triton).
- **Social engineering.** Nothing here stops you from being tricked
  into exporting your key elsewhere.
- **A hardware wallet's level of protection.** A bot needs a live,
  in-memory signing key to operate. That's structurally less safe
  than a hardware wallet that never exposes its key. The mitigation
  is exposure limiting: keep the trading wallet's balance small
  (`MAX_WALLET_SOL_BALANCE`, `WORKING_BALANCE_SOL`) and sweep the rest
  out regularly, so the maximum possible loss is bounded and small.

## Other known gaps

- No persistent state across restarts (open positions are in-memory
  only). If the bot crashes, check your wallet directly — don't trust
  the bot's memory of what it holds.
- `trade_log.json` grows unbounded — rotate or archive it periodically
  if you run this for a long time.
