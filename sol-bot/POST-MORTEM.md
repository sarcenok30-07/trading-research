# sol-bot — project archive and post-mortem

A Solana meme coin trading bot, built and tested to a conclusion.

**Result: the strategy does not work. −4.01% expectancy per trade.**
It never traded real money. That was the point.

This file exists so the archive is useful later instead of being a
folder of mystery scripts. If you only read one thing here, read the
"What went wrong" and "What to keep" sections.

---

## The verdict

Tested on 103,336 price observations across 292 tokens, 73 trades:

| strategy | trades | win rate | breakeven needs | expectancy |
|---|---|---|---|---|
| breakout (the live rule) | 73 | 30.1% | 38.7% | **−4.01%** |
| reversion (buy the dip) | 39 | 30.8% | 44.5% | **−5.79%** |
| short_fade (diagnostic only) | 16 | 43.8% | 66.1% | **−9.69%** |

All three lose. The important detail is that **breakout and reversion
are opposite bets and produced the same ~30% win rate.** If price
movement carried directional information, one of them should have
worked. Both landing in the same place is the signature of entries with
no predictive power.

A separate structural problem: the exit geometry (15% stop, 30% target)
required a 38.7% win rate just to break even. That was decided before
anyone checked whether the data supported it — the trade structure was
unprofitable before the entry rule was even chosen.

One concrete bug in the numbers: average loss was −18.08% against a
−15% stop. Every losing trade bled ~3 extra points because 30-second
polling let price gap past the exit. Across 51 losses that was roughly
157 of the 293 percentage points lost.

## What this was

Breakout detection on Solana DEX tokens: poll DexScreener every 30s,
buy tokens up >10% over 15 minutes with sufficient volume, exit on
stop-loss, take-profit, or trailing stop.

Ran on a $6/month DigitalOcean droplet with systemd services for the
collector and a web dashboard.

## What's in here

**Core trading path**
- `main.py` — poll loop
- `price_feed.py` — DexScreener discovery and breakout detection
- `trader.py` — Jupiter swaps, position sizing, exits
- `config.py` — all settings, with validation that refuses unsafe values

**Safety layers** (the part actually worth keeping)
- `rugcheck.py` — mint/freeze authority, liquidity locks, holder concentration
- `honeypot.py` — verifies a token can be sold before buying it
- `wallet_security.py` — spend limits, kill switch, tx simulation, profit sweeping
- `key_storage.py` — PBKDF2/Fernet encrypted key at rest
- `jito.py` — MEV protection via Jito bundles
- `portfolio.py` — correlation cap across open positions

**Research tooling** (the part that produced the answer)
- `backtest.py` — three strategies, expectancy math, bootstrap-free but
  with confidence intervals on win rate
- `collect_history.py` — builds the dataset, since no free historical
  API exists for these tokens
- `stats.py` — live trade statistics

**Interface**
- `dashboard.py` — instrument-panel web UI, ranks tokens by distance to
  the breakout trigger
- `chatbot.py` — scripted assistant, no LLM, zero API cost

**Never validated, off by default**
- `snipe.py` — pump.fun bonding-curve sniping. High risk; rug/honeypot
  screening cannot meaningfully evaluate a brand-new mint.
- `copytrader.py` + `leaderboard.py` — score-weighted copy-trading.
  Scoring and allocation are built and tested, but the swap-parsing step
  that would feed it automatically was never written, so it only runs in
  paper mode.
- `whale_tracker.py` — alerts on watched wallet activity. Does not parse
  transactions, so it cannot distinguish buys from sells.

## What went wrong, in order

**1. Built the system before testing the hypothesis.** Roughly two
months of engineering preceded the first real backtest. The backtest
took an afternoon and killed the project. It should have come first.

**2. The collector silently gathered garbage for over a week.**
`fetch_solana_pairs()` searched DexScreener for the literal string
`"solana"`, which returns pools *named* something Solana-ish — dead
pairs with SOL as the base token and $0.42 daily volume. Every row said
`SOL`. The service was green, logs looked healthy, data was worthless.

Had this gone unnoticed, the backtest would have returned nothing and
the strategy would have taken the blame for a data bug. **Spot-check
the data, not just the process.**

**3. Exit geometry was assumed, not derived.** 15% stop and 30% target
were picked because they sounded reasonable. They required a 38.7% win
rate. Nothing checked whether the data could deliver that.

**4. No control group.** Two opposite strategies producing identical
results is obvious in hindsight. Testing the inverse alongside the
original from day one would have flagged "no signal" almost
immediately.

## What to keep

The safety engineering transfers to any trading project: dry-run by
default, spending caps that halt the bot, a kill switch, encrypted keys,
config validation that refuses dangerous values. That work wasn't
wasted.

The methodology lesson matters more than the code:

- Start with a hypothesis about **why** an edge exists and **who** is
  losing money to you. This project never had one — it assumed momentum
  continues, with no reason why it should on assets driven purely by
  sentiment.
- Test against a control before building anything.
- Let the data choose the stop/target geometry.
- Expectancy per trade is the number that matters. Win rate alone is
  meaningless without payoff ratio.

## If you ever restart this

Don't tune parameters against these 73 trades. With that sample you
will find settings that look profitable purely by chance, and they will
fail live precisely because they were fitted to the sample. If you test
new ideas, test them on data collected *after* September 2026 — the
current findings have already contaminated everything before that.

## Deployment notes (if resurrecting)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # DRY_RUN=true by default; leave it
python3 main.py
```

Requires Python 3.10+ (the code uses `X | None` union syntax).
`websockets` is pinned to 11.0.3 because `solana` 0.34.3 requires
`<12.0`.

The DigitalOcean droplet it ran on was decommissioned. Everything here runs
locally; nothing depends on that host.

---

*Superseded by the `fvg-study` project, which inverts the order: test
whether the signal exists first, build only if it does.*
