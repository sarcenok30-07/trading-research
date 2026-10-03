"""
Fair Value Gap detection and outcome measurement.

An FVG is a three-candle pattern leaving an unfilled price range:

    Bullish:  high[i-1] < low[i+1]     gap = (high[i-1], low[i+1])
    Bearish:  low[i-1]  > high[i+1]    gap = (high[i+1], low[i-1])

The pattern is only KNOWN once candle i+1 closes, so all forward
measurement starts at i+2. Getting this wrong is the single easiest way
to manufacture a fake edge, so it is enforced in one place here rather
than trusted to callers.

WHAT THIS MODULE DELIBERATELY DOES NOT DO: decide whether FVGs are
tradeable. It measures what happens after them, and — critically —
measures the same thing after matched PLACEBO zones that are not FVGs.
If real and placebo outcomes look alike, the pattern carries no
information, no matter how good the raw numbers look on their own.
"""
import random
from typing import Optional

import numpy as np
import pandas as pd

BULLISH = "bullish"
BEARISH = "bearish"


def average_true_range(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    ATR, used to normalize gap sizes and outcomes. Gold moves in far
    bigger absolute numbers than EURUSD; without normalizing, results
    are not comparable across instruments.
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def detect_fvgs(df: pd.DataFrame, min_atr_multiple: float = 0.0) -> list[dict]:
    """
    Returns every FVG in the series.

    min_atr_multiple filters out gaps smaller than a given fraction of
    ATR. A one-tick gap is technically an FVG but is noise; the filter
    exists so that can be tested rather than assumed.
    """
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"dataframe missing columns: {sorted(missing)}")

    atr = average_true_range(df)
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()

    out = []
    for i in range(1, len(df) - 1):
        atr_here = atr.iloc[i]
        if not np.isfinite(atr_here) or atr_here <= 0:
            continue

        if highs[i - 1] < lows[i + 1]:
            kind, bottom, top = BULLISH, highs[i - 1], lows[i + 1]
        elif lows[i - 1] > highs[i + 1]:
            kind, bottom, top = BEARISH, highs[i + 1], lows[i - 1]
        else:
            continue

        size = top - bottom
        if size <= 0:
            continue
        size_atr = size / atr_here
        if size_atr < min_atr_multiple:
            continue

        out.append({
            "kind": kind,
            "formed_index": i + 1,      # last candle of the pattern
            "known_from_index": i + 2,  # first index usable without lookahead
            "gap_bottom": float(bottom),
            "gap_top": float(top),
            "size": float(size),
            "size_atr": float(size_atr),
            "atr_at_formation": float(atr_here),
            "timestamp": df.index[i + 1],
        })
    return out


def _first_touch(df: pd.DataFrame, start_idx: int, level_low: float,
                 level_high: float, max_bars: int) -> Optional[int]:
    """Index of the first bar within the window whose range overlaps the zone."""
    end = min(len(df), start_idx + max_bars)
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    for j in range(start_idx, end):
        if lows[j] <= level_high and highs[j] >= level_low:
            return j
    return None


def measure_outcome(df: pd.DataFrame, zone: dict, wait_bars: int = 48,
                    hold_bars: int = 48) -> Optional[dict]:
    """
    Simulates the standard FVG trade and records what actually happened.

    Entry: a resting limit order at the near edge of the gap. Long for a
    bullish FVG (treating it as support), short for bearish. Fills when
    price first trades into the zone within `wait_bars`.

    Rather than imposing a stop and target — which is what doomed the
    last project, since we picked the geometry before knowing what the
    data supported — this records MFE and MAE: the best and worst the
    trade ever got over `hold_bars`. From that distribution you can
    derive whether any stop/target combination is profitable, instead of
    guessing one.

    All figures are normalized by ATR at entry so EURUSD and gold are
    directly comparable.
    """
    start = zone["known_from_index"]
    if start >= len(df):
        return None

    touch = _first_touch(df, start, zone["gap_bottom"], zone["gap_top"], wait_bars)
    if touch is None:
        return {"filled": False, "bars_to_touch": None, "mfe_atr": None,
                "mae_atr": None, "kind": zone["kind"], "size_atr": zone["size_atr"]}

    if zone["kind"] == BULLISH:
        entry, direction = zone["gap_top"], 1
    else:
        entry, direction = zone["gap_bottom"], -1

    atr = zone["atr_at_formation"]
    if atr <= 0:
        return None

    end = min(len(df), touch + 1 + hold_bars)
    if touch + 1 >= end:
        return None

    highs = df["high"].to_numpy()[touch + 1:end]
    lows = df["low"].to_numpy()[touch + 1:end]

    if direction == 1:
        mfe = (highs.max() - entry) / atr
        mae = (lows.min() - entry) / atr
    else:
        mfe = (entry - lows.min()) / atr
        mae = (entry - highs.max()) / atr

    # DIRECTIONAL measures. MFE and MAE on their own are magnitude
    # measures — if a pattern is simply followed by bigger moves in both
    # directions (which is what volatility clustering produces, and an
    # FVG always forms around a displacement candle), both grow together
    # and neither indicates a tradeable direction.
    #
    # terminal_atr: where the trade actually stands at the end of the
    #   hold. The honest bottom line, immune to volatility inflation.
    # asymmetry_atr: how much further price went for you than against
    #   you. Cancels out a symmetric increase in range.
    closes = df["close"].to_numpy()
    terminal = ((closes[end - 1] - entry) / atr) * direction
    asymmetry = mfe - abs(mae)

    return {
        "filled": True,
        "bars_to_touch": touch - start,
        "mfe_atr": float(mfe),
        "mae_atr": float(mae),
        "terminal_atr": float(terminal),
        "asymmetry_atr": float(asymmetry),
        "kind": zone["kind"],
        "size_atr": zone["size_atr"],
    }


def make_placebo_zones(df: pd.DataFrame, real_zones: list[dict],
                       seed: int = 0, match_volatility: bool = True) -> list[dict]:
    """
    Builds a control group: for each real FVG, a fake zone matched on
    size, direction, position relative to price, AND — when
    match_volatility is on — the size of the candle it forms around.

    Two bugs were found here in sequence, both the same mistake:

    1. The first version centered placebos on the bar's close, so price
       was already inside them. They filled 100% of the time in zero
       bars while real FVGs sit a gap away. On pure random-walk data
       that manufactured an apparent +0.31 ATR edge.

    2. The second version still ignored that an FVG always forms around
       a large displacement candle. Volatility clusters — big candles
       follow big candles — so real FVGs were followed by bigger moves
       in BOTH directions than placebos. That reads as "significant"
       while carrying no directional information at all. This was caught
       on real gold data, where MFE was significantly larger after real
       FVGs and MAE was significantly worse by a similar amount.

    The lesson both times: the control must differ from the real thing
    in exactly ONE respect — whether the pattern is present. Every other
    property has to match, or the comparison measures the wrong thing.

    So each placebo is now placed at a bar whose own range (relative to
    ATR) is close to that of the real FVG's displacement candle.
    """
    rng = random.Random(seed)
    fvg_indices = {z["formed_index"] for z in real_zones}
    atr = average_true_range(df)
    closes = df["close"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)

    # Range of each candle in ATR units, used for volatility matching.
    atr_arr = atr.to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        candle_range_atr = (highs - lows) / atr_arr

    placebos = []
    unmatched = 0
    for z in real_zones:
        fi = z["formed_index"]
        close_at_formation = closes[fi]
        a0 = z["atr_at_formation"]
        if a0 <= 0:
            continue
        bottom_off = (z["gap_bottom"] - close_at_formation) / a0
        top_off = (z["gap_top"] - close_at_formation) / a0
        # The displacement candle is the MIDDLE of the three-candle
        # pattern. formed_index points at the third candle, so step back
        # one. Matching on the wrong candle defeats the purpose of the
        # volatility control — the third candle is not what causes the
        # clustering effect.
        displacement_idx = max(0, fi - 1)
        target_range = candle_range_atr[displacement_idx]

        # Widen the tolerance progressively rather than failing outright,
        # so a strict band doesn't silently shrink the control group.
        chosen = None
        for tolerance in (0.15, 0.30, 0.60, None):
            for _ in range(120):
                i = rng.randint(2, n - 3)
                if i in fvg_indices:
                    continue
                a = atr_arr[i]
                if not np.isfinite(a) or a <= 0:
                    continue
                if match_volatility and tolerance is not None:
                    cr = candle_range_atr[i]
                    if not np.isfinite(cr) or not np.isfinite(target_range):
                        continue
                    if abs(cr - target_range) > tolerance:
                        continue
                chosen = (i, a)
                break
            if chosen:
                break

        if not chosen:
            unmatched += 1
            continue

        i, a = chosen
        c = closes[i]
        placebos.append({
            "kind": z["kind"],
            "formed_index": i,
            "known_from_index": i + 1,
            "gap_bottom": float(c + bottom_off * a),
            "gap_top": float(c + top_off * a),
            "size": float((top_off - bottom_off) * a),
            "size_atr": float(z["size_atr"]),
            "atr_at_formation": float(a),
            "candle_range_atr": float(candle_range_atr[i]),
            "timestamp": df.index[i],
        })

    if unmatched:
        print(f"[fvg] warning: {unmatched} FVGs had no volatility-matched "
              f"control available and were dropped from the comparison")
    return placebos


def make_placebo_pool(df: pd.DataFrame, real_zones: list[dict],
                      draws: int = 12, base_seed: int = 42,
                      match_volatility: bool = True) -> list[list[dict]]:
    """
    Generates several independent placebo sets instead of one.

    Why this matters: with a single draw, the control's mean is itself a
    coin flip. Tested on real EURUSD data, the out-of-sample "SIGNIFICANT"
    verdict appeared in only 2 of 20 seeds, and across all directional
    tests roughly 6% of seed/test combinations came back significant —
    which is just the false-positive rate of the test. The finding was an
    artifact of which random zones happened to be drawn.

    Pooling many draws estimates the null distribution properly rather
    than sampling it once. This is legitimate rather than a way of
    narrowing the interval artificially: unlimited placebo zones can be
    generated, so the uncertainty that genuinely constrains the result is
    the real sample's fixed size, not the control's.
    """
    return [make_placebo_zones(df, real_zones, seed=base_seed + k,
                               match_volatility=match_volatility)
            for k in range(draws)]


def bootstrap_difference(real_vals: list, placebo_vals: list,
                         iterations: int = 4000, seed: int = 0) -> Optional[dict]:
    """
    Bootstrap confidence interval on the difference in means.

    Without this, any gap between two noisy averages looks like a
    finding. On random-walk data an earlier version of this study
    reported a "+0.31 edge" that was pure sampling noise. If the 95%
    interval straddles zero, there is no evidence of a difference.
    """
    if len(real_vals) < 30 or len(placebo_vals) < 30:
        return None
    rng = np.random.default_rng(seed)
    a = np.asarray(real_vals, dtype=float)
    b = np.asarray(placebo_vals, dtype=float)
    diffs = np.empty(iterations)
    for k in range(iterations):
        diffs[k] = (rng.choice(a, len(a), replace=True).mean()
                    - rng.choice(b, len(b), replace=True).mean())
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    observed = a.mean() - b.mean()
    return {
        "observed": float(observed),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "significant": bool(lo > 0 or hi < 0),
    }


def summarize(results: list[dict], label: str) -> dict:
    """Aggregates outcomes. Unfilled zones are excluded from MFE/MAE
    statistics but counted in the fill rate."""
    total = len(results)
    filled = [r for r in results if r and r["filled"]]
    if total == 0:
        return {"label": label, "n": 0}

    mfe = np.array([r["mfe_atr"] for r in filled if r["mfe_atr"] is not None])
    mae = np.array([r["mae_atr"] for r in filled if r["mae_atr"] is not None])
    term = np.array([r.get("terminal_atr") for r in filled
                     if r.get("terminal_atr") is not None])
    asym = np.array([r.get("asymmetry_atr") for r in filled
                     if r.get("asymmetry_atr") is not None])
    touch = np.array([r["bars_to_touch"] for r in filled if r["bars_to_touch"] is not None])

    return {
        "label": label,
        "n": total,
        "n_filled": len(filled),
        "fill_rate": len(filled) / total * 100,
        "median_bars_to_touch": float(np.median(touch)) if len(touch) else None,
        "mean_mfe_atr": float(mfe.mean()) if len(mfe) else None,
        "median_mfe_atr": float(np.median(mfe)) if len(mfe) else None,
        "mean_mae_atr": float(mae.mean()) if len(mae) else None,
        "median_mae_atr": float(np.median(mae)) if len(mae) else None,
        # The ratio that decides whether any stop/target geometry can work.
        "mfe_mae_ratio": float(mfe.mean() / abs(mae.mean()))
                         if len(mfe) and len(mae) and mae.mean() != 0 else None,
        "pct_mfe_over_1atr": float((mfe >= 1.0).mean() * 100) if len(mfe) else None,
        "pct_mae_worse_than_1atr": float((mae <= -1.0).mean() * 100) if len(mae) else None,
        # Directional. These are the ones that indicate a tradeable edge;
        # MFE and MAE above are magnitude measures that volatility alone
        # can inflate.
        "mean_terminal_atr": float(term.mean()) if len(term) else None,
        "mean_asymmetry_atr": float(asym.mean()) if len(asym) else None,
        "pct_terminal_positive": float((term > 0).mean() * 100) if len(term) else None,
        # Raw series kept so the caller can run significance tests rather
        # than eyeballing a difference between two noisy averages.
        "_mfe_values": mfe.tolist(),
        "_mae_values": mae.tolist(),
        "_terminal_values": term.tolist(),
        "_asymmetry_values": asym.tolist(),
    }
