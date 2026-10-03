# RESULTS — FVG study, September 2026

**Verdict: hourly Fair Value Gaps carry no tradeable directional
information on EURUSD or gold.**

Zero of 8 directional tests came back significant. At a 5% threshold
you would expect roughly 0.4 false positives from pure noise, so this
is what "no effect" looks like rather than a near miss.

---

## The numbers

Terminal P/L is the average result 48 bars after entry, in ATR units.
Entry is a resting limit order at the near edge of the gap, long for
bullish FVGs and short for bearish.

| market / period | real FVG | placebo | difference | verdict |
|---|---|---|---|---|
| EURUSD in-sample | +0.075 ATR, 49.8% positive | −0.075 | +0.149 | not significant |
| EURUSD out-of-sample | +0.258 ATR, 50.5% positive | −0.079 | +0.336 | not significant |
| Gold in-sample | +0.240 ATR, 51.7% positive | +0.120 | +0.120 | not significant |
| Gold out-of-sample | −0.293 ATR, 48.5% positive | −0.117 | −0.176 | not significant |

Asymmetry (MFE − |MAE|) was not significant anywhere either.

Data: Yahoo Finance hourly bars, ~730 days, `EURUSD=X` and `GC=F`.
Split two-thirds in-sample / one-third out-of-sample, with the reserve
untouched during development.

## Why this is a firm result, not an inconclusive one

**The one known bias works against the conclusion.** The 12 pooled
placebo draws come from the same price bars, so they overlap. The
bootstrap treats the pooled values as independent, which makes the
confidence intervals slightly too narrow — biasing the test *toward*
false positives. A test tilted toward finding things found nothing.

**Four independent periods, two markets, consistent answer.** The only
recurring pattern was that real EURUSD FVG trades drew down less than
placebos. That difference was significant, but drawdown is a diagnostic
magnitude measure, not a directional one, and the directional measures
it accompanies were not significant.

**Even taken at face value it is not tradeable.** The best figure,
EURUSD out-of-sample, is +0.26 ATR per trade at 50.5% positive, before
spread and commission. On gold, spread alone would consume it.

## The false positives this study caught

Three separate times an apparent edge turned out to be a flaw in the
control. All three had the same root cause: **the placebo differed from
the real thing in more than one respect, so the comparison measured
that difference instead of the pattern.**

**1. Close-centered placebo.** Placebo zones were centered on the bar's
close, so price was already inside them — they filled 100% of the time
in zero bars, while real FVGs sit a gap away and take time to reach.
On *pure random-walk data* this produced an apparent +0.31 ATR edge.
Caught by the synthetic control.

**2. Volatility-unmatched placebo.** An FVG always forms around a large
displacement candle, and volatility clusters — big candles follow big
candles. Real FVGs were therefore followed by bigger moves in *both*
directions. On real gold data this read as SIGNIFICANT while carrying
no direction at all. Caught by a directional cross-check.

**3. Single placebo draw.** With one random draw the control's mean is
itself a coin flip. An out-of-sample EURUSD flag held in only 2 of 20
seeds; across all directional tests roughly 6% of seed/test
combinations were significant, which is just the test's own
false-positive rate. Caught by a seed sweep.

Each fix is now built in: geometry-matched placebos, volatility
matching on the displacement candle, 12 pooled draws, and a stability
check that overrules any flag surviving fewer than half the draws.

## Validation state

Three synthetic controls, all passing:

| control | contains | required verdict | result |
|---|---|---|---|
| pure random walk | nothing | no edge | passes |
| volatility clustering, random direction | bigger moves both ways | no edge | passes — throws a spurious flag, stability check overrules it |
| injected directional drift after gaps | a real edge | SIGNIFICANT | passes, 12/12 draws |

The middle control exists because of failure #2 above. An earlier
version would have called it significant.

## If you revisit this

**The in-sample data is now contaminated.** Every result above has been
seen, so anything developed from here is shaped by that knowledge.
Test new ideas on data after September 2026, or accept that in-sample
results prove nothing.

**Count your tests.** Each variation multiplies the false-positive
count. Five variations × 8 directional tests = 40 tests, and at a 5%
threshold you would expect 2 significant results from noise alone. You
would almost certainly find one, it would feel like a discovery, and it
would be the same phenomenon as the seed-42 flag but harder to detect.
If you test variations, choose them in advance, cap the list at two or
three, and require out-of-sample plus stability confirmation.

**Plausible variations, if you must:** `--minatr=0.5` (larger gaps
only), a shorter `--hold`, or session-filtered entries. None have a
mechanism behind them, which is the real problem.

**The structural argument against continuing.** FVGs are taught
publicly to a very large audience. A pattern that visible, on a market
as liquid as EURUSD, is not a plausible home for undiscovered edge. Any
future attempt should start by answering: why would this edge exist,
and who is losing money to me? Neither this study nor the preceding
meme coin project ever had an answer to that, which in hindsight was
the strongest predictor of both outcomes.

## Cost comparison, for calibration

| | sol-bot (meme coins) | this study |
|---|---|---|
| build time before first honest test | ~2 months | 2 days |
| hosting cost | ~$12 | $0 |
| hypotheses tested | 3 | 2 markets × 2 measures × 2 periods |
| synthetic controls | 0 | 3 |
| result | −4.01% expectancy per trade | no directional signal |

Same quality of answer, a fraction of the effort — because the order
was hypothesis first, code second. That inversion is the transferable
lesson: find the cheapest test that could kill the idea, and run it
before building anything.

---

*Companion document: `sol-bot/POST-MORTEM.md`, covering the preceding
Solana meme coin bot.*
