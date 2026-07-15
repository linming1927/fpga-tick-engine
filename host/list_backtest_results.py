#!/usr/bin/env python3
"""
list_backtest_results.py — browse saved backtest runs.

    python3 list_backtest_results.py
    python3 list_backtest_results.py --symbol RKLB
    python3 list_backtest_results.py --folder RKLB_2023-07-16_2026-07-13_20260715-143022

Every run saved by backtest.py (see backtest_results.py) lives under
./backtest_results/ by default — one folder per run, never overwritten,
so you can rerun the same symbol and date range with different
parameters and compare them side by side afterward.
"""

from __future__ import annotations

import argparse
import json
import os

from backtest_results import list_backtest_results, RESULTS_DIR_DEFAULT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default=RESULTS_DIR_DEFAULT)
    ap.add_argument("--symbol", default=None,
                    help="only show runs for this symbol")
    ap.add_argument("--folder", default=None,
                    help="show the full saved report.txt for one "
                         "specific run folder, instead of the table")
    args = ap.parse_args()

    if args.folder:
        path = os.path.join(args.results_dir, args.folder, "report.txt")
        if not os.path.exists(path):
            print(f"No report.txt found at {path}")
            return
        with open(path) as f:
            print(f.read())
        return

    runs = list_backtest_results(args.results_dir)
    if args.symbol:
        runs = [r for r in runs if r["symbol"].upper() == args.symbol.upper()]
    if not runs:
        print(f"No saved backtest runs found in {args.results_dir}"
             + (f" for {args.symbol}" if args.symbol else ""))
        return

    print(f"{len(runs)} saved run(s) in {args.results_dir}:\n")
    print(f"{'symbol':<8} {'date range':<24} {'params':<26} "
         f"{'live net $':>11} {'other net $':>12}  folder")
    print("-" * 110)
    for r in runs:
        p = r["strategy_params"]
        param_s = (f"f{p.get('fast')}/{p.get('slow')} "
                  f"k{p.get('ema_kf')}/{p.get('ema_ks')} "
                  f"cd{p.get('cooldown')}")
        rng = f"{r['date_range']['start']}..{r['date_range']['end']}"
        live_card = next((c for c in r["results"].values() if c["live"]),
                         None)
        other_card = next((c for c in r["results"].values()
                          if not c["live"]), None)
        live_net = f"{live_card['net_usd']:+.2f}" if live_card else "—"
        other_net = f"{other_card['net_usd']:+.2f}" if other_card else "—"
        print(f"{r['symbol']:<8} {rng:<24} {param_s:<26} "
             f"{live_net:>11} {other_net:>12}  {r['folder']}")
    print(f"\nFull report for one run: "
         f"python3 list_backtest_results.py --folder <folder>")


if __name__ == "__main__":
    main()
