"""
FVG study runner — does the pattern carry information?

Usage:
    python3 study.py EURUSD=X 1h 730d
    python3 study.py GC=F 1h 730d
    python3 study.py --csv mydata.csv

Design decisions, and why:

1. PLACEBO CONTROL. Every statistic is computed twice — once for real
   FVGs, once for size-matched fake zones at non-FVG locations. A fill
   rate of 80% sounds great until the placebo group also fills 80% of
   the time. Only the DIFFERENCE is evidence.

2. NO PRE-CHOSEN STOP OR TARGET. The study records MFE and MAE (best
   and worst excursion) rather than imposing a geometry. In the previous
   project a 15%/30% stop/target needed a 38.7% win rate to break even,
   and that was decided before anyone checked whether the data supported
   it. Here the data decides.

3. OUT-OF-SAMPLE HELD BACK. The most recent third is reserved. Develop
   on the older two-thirds; check the reserve once, at the end. If a
   result only appears in-sample, it is curve-fitting.

Data: yfinance is free and needs no key. Yahoo limits intraday history
(roughly 60 days at 5m, 730 days at 1h), so 1h is the practical choice
for a sample large enough to mean anything.
    EURUSD=X   euro/dollar spot
    GC=F       gold futures
    XAUUSD=X   gold spot (patchier coverage)
"""
import sys

import numpy as np
import pandas as pd

from fvg import (detect_fvgs, measure_outcome, make_placebo_zones,
                 make_placebo_pool, summarize, bootstrap_difference)

OOS_FRACTION = 1 / 3


def load_yahoo(symbol: str, interval: str, period: str) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance is not installed. Run:  pip install yfinance")
        sys.exit(1)

    raw = yf.download(symbol, interval=interval, period=period,
                      progress=False, auto_adjust=False)
    if raw is None or len(raw) == 0:
        print(f"No data returned for {symbol}. Check the symbol and that the "
              f"interval/period combination is within Yahoo's limits.")
        sys.exit(1)

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
    return df


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    for candidate in ("datetime", "date", "timestamp", "time"):
        if candidate in df.columns:
            df[candidate] = pd.to_datetime(df[candidate])
            df = df.set_index(candidate)
            break
    return df[["open", "high", "low", "close"]].dropna()


def run_group(df: pd.DataFrame, zones: list[dict], label: str,
              wait_bars: int, hold_bars: int) -> dict:
    results = [measure_outcome(df, z, wait_bars, hold_bars) for z in zones]
    return summarize([r for r in results if r], label)


def _pool_summaries(summaries: list[dict]) -> dict:
    """
    Merges several placebo draws into one control group by concatenating
    their raw outcome series, then recomputing the aggregates from the
    combined sample.
    """
    usable = [s for s in summaries if s.get("n", 0) > 0]
    if not usable:
        return {"label": "placebo", "n": 0}

    pooled = {"label": "placebo", "n": sum(s["n"] for s in usable),
              "n_filled": sum(s.get("n_filled", 0) for s in usable)}
    pooled["fill_rate"] = (pooled["n_filled"] / pooled["n"] * 100) if pooled["n"] else 0.0

    touches = [s["median_bars_to_touch"] for s in usable
               if s.get("median_bars_to_touch") is not None]
    pooled["median_bars_to_touch"] = float(np.median(touches)) if touches else None

    for series_key in ("_mfe_values", "_mae_values", "_terminal_values",
                       "_asymmetry_values"):
        merged = []
        for s in usable:
            merged.extend(s.get(series_key, []))
        pooled[series_key] = merged

    def mean_or_none(key):
        vals = pooled.get(key, [])
        return float(np.mean(vals)) if vals else None

    pooled["mean_mfe_atr"] = mean_or_none("_mfe_values")
    pooled["median_mfe_atr"] = (float(np.median(pooled["_mfe_values"]))
                                if pooled["_mfe_values"] else None)
    pooled["mean_mae_atr"] = mean_or_none("_mae_values")
    pooled["median_mae_atr"] = (float(np.median(pooled["_mae_values"]))
                                if pooled["_mae_values"] else None)
    pooled["mean_terminal_atr"] = mean_or_none("_terminal_values")
    pooled["mean_asymmetry_atr"] = mean_or_none("_asymmetry_values")

    mfe, mae = pooled["_mfe_values"], pooled["_mae_values"]
    pooled["mfe_mae_ratio"] = (float(np.mean(mfe) / abs(np.mean(mae)))
                               if mfe and mae and np.mean(mae) != 0 else None)
    pooled["pct_mfe_over_1atr"] = (float(np.mean(np.array(mfe) >= 1.0) * 100)
                                   if mfe else None)
    pooled["pct_mae_worse_than_1atr"] = (float(np.mean(np.array(mae) <= -1.0) * 100)
                                         if mae else None)
    term = pooled["_terminal_values"]
    pooled["pct_terminal_positive"] = (float(np.mean(np.array(term) > 0) * 100)
                                       if term else None)
    return pooled


def print_group(s: dict):
    if s.get("n", 0) == 0:
        print(f"  {s['label']}: no zones")
        return
    print(f"  {s['label']:<10} n={s['n']:<5} fill={s['fill_rate']:.1f}%  "
          f"median bars to touch={s['median_bars_to_touch']}")
    if s.get("mean_mfe_atr") is not None:
        print(f"  {'':<10} MFE mean={s['mean_mfe_atr']:+.3f} ATR   "
              f"median={s['median_mfe_atr']:+.3f}")
        print(f"  {'':<10} MAE mean={s['mean_mae_atr']:+.3f} ATR   "
              f"median={s['median_mae_atr']:+.3f}")
        ratio = s["mfe_mae_ratio"]
        print(f"  {'':<10} MFE/MAE ratio={ratio:.3f}" if ratio else "")
        print(f"  {'':<10} reached +1 ATR: {s['pct_mfe_over_1atr']:.1f}%   "
              f"drew down past -1 ATR: {s['pct_mae_worse_than_1atr']:.1f}%")
    if s.get("mean_terminal_atr") is not None:
        print(f"  {'':<10} terminal P/L mean={s['mean_terminal_atr']:+.3f} ATR   "
              f"positive {s['pct_terminal_positive']:.1f}% of the time")
        print(f"  {'':<10} asymmetry mean={s['mean_asymmetry_atr']:+.3f} ATR")


def compare(real: dict, placebo: dict, per_draw: list = None,
            is_final: bool = False):
    print()
    print("-" * 66)
    print("REAL vs PLACEBO — the comparison that decides it")
    print("-" * 66)
    if real.get("n", 0) == 0 or placebo.get("n", 0) == 0:
        print("Not enough zones to compare.")
        return

    print(f"{'metric':<26} {'real':>10} {'placebo':>10} {'difference':>12}")
    print(f"{'fill rate %':<26} {real['fill_rate']:>10.1f} "
          f"{placebo['fill_rate']:>10.1f} "
          f"{real['fill_rate'] - placebo['fill_rate']:>+12.1f}")
    for key, name in (("mean_mfe_atr", "MFE mean (ATR)"),
                      ("mean_mae_atr", "MAE mean (ATR)"),
                      ("mfe_mae_ratio", "MFE/MAE ratio"),
                      ("mean_terminal_atr", "terminal P/L (ATR)"),
                      ("mean_asymmetry_atr", "asymmetry (ATR)")):
        a, b = real.get(key), placebo.get(key)
        if a is None or b is None:
            continue
        print(f"{name:<26} {a:>10.3f} {b:>10.3f} {a - b:>+12.3f}")

    # A difference between two noisy means is not a finding until it
    # survives a significance test. And it must be a DIRECTIONAL
    # difference: MFE and MAE both growing together is volatility
    # clustering, not an edge.
    print()
    print("DIRECTIONAL tests — these decide whether there is an edge:")
    directional = {}
    for key, name in (("_terminal_values", "Terminal P/L"),
                      ("_asymmetry_values", "Asymmetry (MFE-|MAE|)")):
        bs = bootstrap_difference(real.get(key, []), placebo.get(key, []))
        if bs is None:
            print(f"  {name:<22} too few samples to test")
            continue
        mark = "SIGNIFICANT" if bs["significant"] else "not significant"
        print(f"  {name:<22} {bs['observed']:+.3f} ATR  "
              f"[{bs['ci_low']:+.3f}, {bs['ci_high']:+.3f}]  {mark}")
        directional[name] = bs

    print()
    print("MAGNITUDE tests — diagnostic only, do NOT read as edge:")
    magnitude = {}
    for key, name in (("_mfe_values", "MFE"), ("_mae_values", "MAE")):
        bs = bootstrap_difference(real.get(key, []), placebo.get(key, []))
        if bs is None:
            print(f"  {name:<22} too few samples to test")
            continue
        mark = "significant" if bs["significant"] else "not significant"
        print(f"  {name:<22} {bs['observed']:+.3f} ATR  "
              f"[{bs['ci_low']:+.3f}, {bs['ci_high']:+.3f}]  {mark}")
        magnitude[name] = bs

    print()
    any_directional = any(b["significant"] for b in directional.values())
    both_magnitude_moved = (
        magnitude.get("MFE", {}).get("significant")
        and magnitude.get("MAE", {}).get("significant")
        and magnitude["MFE"]["observed"] > 0
        and magnitude["MAE"]["observed"] < 0
    )

    # If a verdict only survives certain placebo draws it is an artifact
    # of sampling, not a finding. Report the rate explicitly.
    stability = None
    if any_directional and per_draw:
        hits = 0
        tested = 0
        for draw in per_draw:
            if draw.get("n", 0) == 0:
                continue
            tested += 1
            draw_sig = False
            for key in ("_terminal_values", "_asymmetry_values"):
                bs = bootstrap_difference(real.get(key, []), draw.get(key, []),
                                          iterations=1200)
                if bs and bs["significant"]:
                    draw_sig = True
            if draw_sig:
                hits += 1
        if tested:
            stability = hits / tested * 100
            print(f"Stability across individual placebo draws: significant in "
                  f"{hits}/{tested} ({stability:.0f}%)")
            if stability < 50:
                print("  A real effect should show up in most draws. This rate is")
                print("  close to the test's own false-positive rate, so the flag")
                print("  above is most likely an artifact of which random zones")
                print("  were drawn rather than a property of the pattern.")
            print()

    if any_directional and stability is not None and stability < 50:
        print("VERDICT: no reliable directional edge. A significance flag")
        print("appeared but did not survive redrawing the control.")
    elif any_directional:
        print("A DIRECTIONAL measure differs significantly from the control")
        print("and holds across placebo draws.")
        if is_final:
            print("This is the out-of-sample period, so it was not used to")
            print("develop the idea. Still, spread and commission are not")
            print("modeled — check the effect is large enough to survive them.")
        else:
            print("Check whether it also holds in the out-of-sample section")
            print("below before treating it as real.")
    elif both_magnitude_moved:
        print("MFE is larger AND MAE is worse after real FVGs, with no")
        print("directional difference. That is volatility clustering: an FVG")
        print("forms around a big candle, and big candles cluster, so bigger")
        print("moves follow in BOTH directions. Bigger swings are not an edge.")
    elif not directional:
        print("Not enough filled zones to test directionally.")
    else:
        print("Real FVGs are directionally indistinguishable from matched")
        print("random zones. On this evidence the pattern carries no")
        print("tradeable information. That is a genuine result, not a failure")
        print("of the test.")


def study(df: pd.DataFrame, name: str, wait_bars: int, hold_bars: int,
          min_atr: float, placebo_draws: int = 12):
    split = int(len(df) * (1 - OOS_FRACTION))
    in_sample = df.iloc[:split]
    out_sample = df.iloc[split:]

    print("=" * 66)
    print(f"FVG STUDY — {name}")
    print("=" * 66)
    print(f"bars total {len(df)}   in-sample {len(in_sample)}   "
          f"out-of-sample {len(out_sample)}")
    print(f"range {df.index[0]}  to  {df.index[-1]}")
    print(f"wait up to {wait_bars} bars for a touch, then hold {hold_bars} bars")
    print(f"minimum gap size: {min_atr} ATR")
    print(f"control: {placebo_draws} pooled placebo draws")
    print()

    for label, data in (("IN-SAMPLE", in_sample), ("OUT-OF-SAMPLE", out_sample)):
        zones = detect_fvgs(data, min_atr_multiple=min_atr)
        print(f"### {label}  ({len(zones)} FVGs found, "
              f"{len(zones)/max(1,len(data))*100:.1f}% of bars)")
        real = run_group(data, zones, "real", wait_bars, hold_bars)

        # Several independent placebo draws, pooled. A single draw makes
        # the control mean a coin flip — on real EURUSD data that alone
        # produced a spurious SIGNIFICANT verdict in 2 of 20 seeds.
        pool = make_placebo_pool(data, zones, draws=placebo_draws)
        per_draw = [run_group(data, p, "placebo", wait_bars, hold_bars)
                    for p in pool]
        fake = _pool_summaries(per_draw)

        print_group(real)
        print()
        print(f"  (control pooled from {placebo_draws} independent draws)")
        print_group(fake)
        compare(real, fake, per_draw, is_final=(label == "OUT-OF-SAMPLE"))
        print()

    print("=" * 66)
    print("Reminder: spread, commission and slippage are NOT modeled. Any")
    print("edge here must be large enough to survive those before it is real.")
    print("=" * 66)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a.split("=")[0]: (a.split("=", 1)[1] if "=" in a else True)
             for a in sys.argv[1:] if a.startswith("--")}

    wait_bars = int(flags.get("--wait", 48))
    hold_bars = int(flags.get("--hold", 48))
    min_atr = float(flags.get("--minatr", 0.25))
    placebo_draws = int(flags.get("--draws", 12))

    if "--csv" in flags:
        df = load_csv(flags["--csv"])
        name = flags["--csv"]
    elif args:
        symbol = args[0]
        interval = args[1] if len(args) > 1 else "1h"
        period = args[2] if len(args) > 2 else "730d"
        df = load_yahoo(symbol, interval, period)
        name = f"{symbol} {interval} {period}"
    else:
        print(__doc__)
        sys.exit(1)

    study(df, name, wait_bars, hold_bars, min_atr, placebo_draws)
