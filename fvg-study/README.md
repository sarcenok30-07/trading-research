# FVG study — does the pattern actually carry information?

A placebo-controlled test of whether Fair Value Gaps predict anything,
for EURUSD and gold. **This is a research tool, not a trading bot.** It
answers one question before any strategy gets built: is there a signal
here at all?

## Why it is built this way

The previous project (a Solana meme coin bot) failed for reasons worth
not repeating:

- The strategy was built first and tested last. Two months in, the
  backtest showed −4% expectancy per trade.
- Two opposite entry rules produced the same ~30% win rate, which is
  what "no signal" looks like. That could have been discovered in week
  one with a control group.
- The stop/target geometry was chosen before checking whether the data
  supported it. It needed a 38.7% win rate to break even and was
  doomed regardless of entry rule.

So this study inverts the order and builds in three safeguards.

### 1. Placebo control

Every statistic is computed twice — once for real FVGs, once for fake
zones matched on size, direction, distance from price, and the size of
the candle they form around. Only the difference is evidence.

That matching went wrong twice, the same way both times:

1. An early version centered placebo zones on the bar's close, so price
   was already inside them: they filled 100% of the time in zero bars
   while real FVGs sat a gap away and took time to reach. **On pure
   random-walk data it manufactured an apparent edge of +0.31 ATR.**

2. The next version still ignored that an FVG always forms around a
   large displacement candle. Volatility clusters, so real FVGs were
   followed by bigger moves in both directions. **On real gold data
   that read as SIGNIFICANT while carrying no direction.**

The lesson both times: the control must differ from the real thing in
exactly **one** respect — whether the pattern is present. Any other
mismatch means you are measuring that mismatch instead.

### 2. No pre-chosen stop or target

The study records MFE and MAE (best and worst excursion after entry)
rather than imposing a geometry. From that distribution you can work
out whether any stop/target combination is profitable. The data picks
the geometry instead of you picking it and hoping.

### 3. Directional measures, not magnitude

MFE and MAE on their own are *magnitude* measures. If a pattern is
simply followed by bigger moves in both directions, both grow together
and neither indicates a tradeable direction.

That is not hypothetical — it happened. An earlier version flagged gold
as SIGNIFICANT because MFE was larger after real FVGs. But MAE was
worse by a similar amount: price moved further in your favour *and*
further against you. No edge, just bigger swings.

Volatility clustering explains it. Big candles follow big candles, and
an FVG by definition forms around a displacement candle. So the study
now leads with two directional statistics:

- **Terminal P/L** — where the trade stands at the end of the hold. The
  honest bottom line, immune to volatility inflation.
- **Asymmetry (MFE − |MAE|)** — how much further price went for you
  than against you. A symmetric increase in range cancels out.

MFE and MAE are still reported, but labelled diagnostic. If both move
significantly in opposite directions with no directional difference,
the study now says so explicitly instead of calling it an edge.

### 4. Bootstrap significance

A difference between two noisy averages is not a finding. Every
comparison gets a bootstrap 95% confidence interval; if it straddles
zero, the verdict is "not significant" regardless of how the raw
numbers look.

### 5. Pooled placebo draws plus a stability check

A single placebo draw makes the control's mean a coin flip. On real
EURUSD data this produced an out-of-sample "SIGNIFICANT" verdict that
held in only **2 of 20 seeds** — and across all directional tests,
roughly 6% of seed/test combinations came back significant, which is
simply the test's own false-positive rate.

So the control is now pooled from 12 independent draws (`--draws=N`),
which estimates the null distribution instead of sampling it once. On
top of that, any significance flag is re-checked against each
individual draw. If it survives fewer than half, the verdict is
overruled:

```
Stability across individual placebo draws: significant in 4/12 (33%)
VERDICT: no reliable directional edge. A significance flag
appeared but did not survive redrawing the control.
```

That mechanism is verified: the volatility-clustering control throws a
spurious flag out-of-sample and the stability check correctly overrules
it, while a genuine injected edge scores 12/12 in both periods.

## Validation

The study is checked against three synthetic datasets before being
trusted on real data:

| control | contains | required verdict | result |
|---|---|---|---|
| pure random walk | nothing | no edge | passes |
| volatility clustering, random direction | bigger moves both ways | no edge | passes (spurious flag overruled by stability check) |
| injected directional drift after gaps | a real edge | SIGNIFICANT | passes, 12/12 draws |

The middle one exists specifically because of the gold false positive.
An earlier version would have called it significant.

A test that never finds anything is as useless as one that always
does. All three cases matter.

## Running it

```bash
pip install yfinance pandas numpy
python3 study.py EURUSD=X 1h 730d      # euro/dollar
python3 study.py GC=F 1h 730d          # gold futures
python3 study.py --csv mydata.csv      # your own OHLCV
```

Options:
- `--wait=48` bars to wait for price to touch the zone
- `--hold=48` bars to hold after entry
- `--minatr=0.25` ignore gaps smaller than this fraction of ATR

Yahoo limits intraday history (roughly 60 days at 5m, 730 days at 1h),
so 1h is the practical choice for a usable sample size.

## Reading the output

**Fill rate alone means nothing.** Price revisits levels eventually, so
almost every FVG fills if you wait long enough. Compare against the
placebo.

**Read the DIRECTIONAL tests, nothing else.** Terminal P/L and
Asymmetry are the only two numbers that indicate a tradeable edge. MFE,
MAE, and the MFE/MAE ratio are printed as diagnostics — a pattern
followed by bigger moves in both directions inflates MFE without
carrying any direction, which is exactly the gold false positive that
prompted this section.

**Significance is necessary but not sufficient.** If the bootstrap
interval straddles zero there is no evidence, whatever the point
estimate says. And even a significant flag is overruled if it fails the
stability check below.

**Stability across placebo draws is the final filter.** A real effect
shows up in most draws. If it appears in under half, it is an artifact
of which random control zones happened to be drawn. On real EURUSD data
an apparent out-of-sample edge appeared in only 2 of 20 seeds — about
the test's own false-positive rate.

## What this does not model

Spread, commission, and slippage. On EURUSD spread is small; on gold it
is not. Any edge found here has to be large enough to survive those
costs before it means anything.

It also assumes a resting limit order fills at the zone edge, which is
optimistic during fast moves.

## Expected outcome

Most likely there is no significant difference. FVGs appear in roughly
11% of bars on a pure random walk with a near-perfect bullish/bearish
split — they are a normal feature of noisy price data, not a rare
signal. If that is the result, it cost an afternoon rather than months.
