#!/usr/bin/env python3
"""
analyze_sigma_distribution.py

Computes the REAL, historical distribution of VWAPMirror's own sigma
(expressed as a percentage of that moment's session VWAP) across a
full year of tick data — for the specific purpose of picking a
sensible sigma FLOOR for position_risk.py's risk-sizing formula,
grounded in what this symbol's sigma actually looks like on ordinary
days, rather than a guessed number.

Reuses the EXACT same VWAPMirror class (and the same once-per-day
reset convention order_manager.py/backtest.py use since v3.38/v3.44)
the real live/backtest system runs — so the sigma values here are
genuinely representative of what the real risk overlay would have
seen, tick for tick.

Usage:
    python3 analyze_sigma_distribution.py \\
        --trades historical_trades/SPY_....trades.jsonl --symbol SPY

    # multiple files for one symbol, comma-separated, same as backtest.py:
    python3 analyze_sigma_distribution.py \\
        --trades file1.jsonl,file2.jsonl --symbol RIVN

Given a full year of SPY-scale data can be 200M+ ticks, sigma is
SAMPLED (recorded only every --sample-every'th warmed-up tick, default
200) rather than stored for every single tick -- keeps memory bounded
while still giving a representative picture of the distribution.
"""
from __future__ import annotations
import argparse
import os
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tick_protocol import VWAPMirror, iter_trades_multi

ET = ZoneInfo("America/New_York")


def percentile(sorted_vals: list[float], p: float) -> float | None:
    """Linear-interpolated percentile, p in [0.0, 1.0]. sorted_vals
    must already be sorted ascending."""
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trades", required=True,
                    help="comma-separated trade JSONL path(s), same "
                        "format as order_manager.py/backtest.py's own "
                        "--trades")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--warmup", type=int, default=20,
                    help="ticks before VWAP is considered warmed up -- "
                        "matches --vwap-warmup elsewhere (default 20)")
    ap.add_argument("--sample-every", type=int, default=200,
                    help="record sigma only every Nth warmed-up tick "
                        "(default 200) -- keeps memory bounded across "
                        "a full year of tick-level data while still "
                        "giving a representative distribution")
    ap.add_argument("--progress-every", type=int, default=5_000_000)
    args = ap.parse_args()

    paths = [p.strip() for p in args.trades.split(",") if p.strip()]
    for p in paths:
        if not os.path.exists(p):
            sys.exit(f"file not found: {p}")

    model = VWAPMirror(warmup_n=args.warmup)
    session_date = None
    sigma_pcts: list[float] = []
    n = 0
    n_warmed = 0

    print(f"[analyze] reading {', '.join(paths)} ...", file=sys.stderr)
    for t, price_e4, qty in iter_trades_multi(paths):
        n += 1
        if n % args.progress_every == 0:
            print(f"  ...{n:,} ticks processed ({t.date()})",
                 file=sys.stderr)

        # once-per-day reset, matching the same convention
        # order_manager.py (v3.38) and backtest.py (v3.44) use for the
        # real session VWAP -- NOT resetting here would let sigma
        # accumulate across the whole year as one giant, meaningless
        # running average instead of a real trading day's own value
        day = t.astimezone(ET).date()
        if session_date is None:
            session_date = day
        elif day != session_date:
            model.sess_reset()
            session_date = day

        model.ingest(price_e4, qty)

        if model.warmed_up and model.sum_v > 0 and model.vwap > 0:
            n_warmed += 1
            if n_warmed % args.sample_every == 0:
                mean_sq = model.sum_ppv / model.sum_v
                variance = max(0.0, mean_sq - model.vwap ** 2)
                sigma = variance ** 0.5
                sigma_pcts.append(sigma / model.vwap)

    if not sigma_pcts:
        sys.exit(f"[analyze] no warmed-up ticks found for {args.symbol} "
                 f"-- check --warmup and the trades file(s)")

    sigma_pcts.sort()
    print(f"\n{args.symbol}: {n:,} total ticks, {len(sigma_pcts):,} "
         f"sigma samples (every {args.sample_every}th warmed-up tick)")
    print(f"{'percentile':>12s}  {'sigma as % of price':>20s}")
    for p in (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        val = percentile(sigma_pcts, p)
        print(f"{p*100:>11.0f}%  {val*100:>19.4f}%")
    print(f"{'min':>12s}  {sigma_pcts[0]*100:>19.4f}%")
    print(f"{'max':>12s}  {sigma_pcts[-1]*100:>19.4f}%")
    print(f"\n[analyze] the LOWER percentiles (1%/5%/10%) are the ones "
         f"that matter for picking a floor -- they show how tight "
         f"sigma gets on the quietest {args.symbol} moments in this "
         f"data, without being skewed by the rare, wide-sigma spikes "
         f"at the top end")


if __name__ == "__main__":
    main()
