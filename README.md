# Trading strategy research — two systems, two negative results

Two self-directed quantitative research projects testing whether specific
trading strategies carry a real edge. **Both concluded they do not.** Neither
was ever funded with live capital.

The negative results are the point. These are written up the way a research
finding should be: with the methodology, the bugs found along the way, and an
honest account of what the evidence actually supports.

| | [`sol-bot/`](sol-bot/) | [`fvg-study/`](fvg-study/) |
|---|---|---|
| **Question** | Do price breakouts on Solana DEX tokens predict returns? | Do Fair Value Gaps predict direction on EURUSD and gold? |
| **Method** | Live data collection, multi-strategy backtest | Placebo-controlled study, bootstrap significance |
| **Data** | 103,336 observations, 292 tokens (self-collected) | ~730 days hourly, 2 instruments |
| **Result** | −4.01% expectancy per trade | 0 of 8 directional tests significant |
| **Write-up** | [POST-MORTEM.md](sol-bot/POST-MORTEM.md) | [RESULTS.md](fvg-study/RESULTS.md) |

---

## sol-bot — Solana trading system

A complete automated trading system in Python: market data collection,
execution via Jupiter, MEV protection through Jito bundles, rug-pull and
honeypot screening, encrypted key storage, position sizing with trailing
stops and partial exits, a web dashboard, and a backtesting harness.
Deployed on a DigitalOcean droplet under systemd.

**The finding:** −4.01% expectancy per trade across 73 trades. More
informative than the headline number — the breakout rule and its exact
inverse produced nearly identical ~30% win rates. Two opposite bets landing
in the same place is the signature of an entry signal carrying no
information.

A second structural finding: the 15%/30% stop-target geometry required a
38.7% win rate to break even, and that geometry was chosen before anyone
checked whether the data could support it. The trade structure was
unprofitable before the entry rule was even selected.

**The bug worth reading about:** the data collector ran green for over a week
while silently gathering garbage. A query for the literal string `"solana"`
returned dead pools with SOL as the base token and sub-dollar volume. Service
healthy, logs clean, data worthless. Had it gone unnoticed, the backtest
would have returned nothing and the strategy would have taken the blame for a
data bug.

## fvg-study — Fair Value Gap research

A placebo-controlled study testing whether a widely-taught chart pattern
predicts anything. Built after the first project, deliberately inverting the
order of operations: test the hypothesis before building the system.

**The finding:** zero of eight directional tests significant across both
markets and both time periods.

**Why the methodology matters more than the result.** Three separate times an
apparent edge turned out to be a flaw in the control — each with the same
root cause: the placebo differed from the real pattern in more than one
respect, so the comparison measured that difference instead of the pattern.

1. **Close-centered placebos** were already touching price, so they filled
   100% of the time in zero bars while real gaps sat a gap away. On *pure
   random-walk data* this manufactured a +0.31 ATR edge.
2. **Volatility-unmatched placebos** ignored that a gap always forms around a
   large candle, and large candles cluster. Real gaps were followed by bigger
   moves in *both* directions. On real gold data this read as SIGNIFICANT
   while carrying no direction.
3. **Single-seed placebos** made the control's mean a coin flip. An
   out-of-sample EURUSD flag survived only 2 of 20 seeds — roughly the test's
   own false-positive rate.

Each was caught by a synthetic control with a known answer. The study now
validates against three of them before being trusted on real data:

| control | contains | required verdict |
|---|---|---|
| pure random walk | nothing | no edge |
| volatility clustering, random direction | bigger moves both ways | no edge |
| injected directional drift | a real edge | significant |

A test that never finds anything is as useless as one that always does.

---

## What I'd take from this

**Test the hypothesis before building the system.** The first project spent
roughly two months on engineering before its first honest backtest. The
backtest took an afternoon and ended the project. The second reached a
comparable-quality answer in days, for nothing, because the order was
reversed.

**A control group is worth more than another feature.** Two opposite
strategies producing identical results answered the question immediately —
and cost one extra function to test.

**Let the data pick the parameters.** A stop/target ratio chosen because it
sounded reasonable set a breakeven win rate the strategy could never clear.

**Expectancy, not win rate.** 30% sounds survivable until you work out what
payoff ratio it would need.

**A green service is not a working service.** Check the data, not the status
page.

## Running them

Both are Python 3.9+, dependencies in each folder's `requirements.txt`.

`fvg-study` is self-contained and reproducible — it downloads its own data:

```bash
cd fvg-study && pip install -r requirements.txt
python3 study.py EURUSD=X 1h 730d
```

`sol-bot` defaults to `DRY_RUN=true` and ships no credentials. It is archived
rather than maintained; see [POST-MORTEM.md](sol-bot/POST-MORTEM.md) before
running anything.

## Disclaimer

Research code, published for the methodology. Not financial advice, not
investment guidance, and not a strategy to trade — the documented conclusion
of both projects is that these approaches lose money.
