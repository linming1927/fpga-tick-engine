#!/usr/bin/env python3
"""
backtest_results.py — save and browse backtest runs for later review.

Called automatically by backtest.py after every run (unless --no-save
is passed). Each run gets its own folder under RESULTS_DIR_DEFAULT,
named <symbol>_<start>_<end>_<run-timestamp> — the date range comes
from the ACTUAL trade data replayed (first and last timestamp seen),
not parsed from a filename, so it's correct even if files were renamed
or don't follow fetch_historical_trades.py's naming convention. The
run-timestamp suffix means rerunning the same symbol/range with
different parameters (which you'll want to do — that's the whole
point of being able to compare runs later) never overwrites an
earlier result.

Each run folder contains:
  summary.json   machine-readable: symbol, date range, every strategy
                 parameter used, and full per-strategy results
  report.txt     the exact human-readable comparison_report() text,
                 plus a small header of run metadata
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from compare import StrategyScorecard, comparison_report

RESULTS_DIR_DEFAULT = "./backtest_results"


def _card_to_dict(c: StrategyScorecard) -> dict:
    return {
        "name": c.name,
        "live": c.live,
        "signals": c.signals,
        "trips": c.trips,
        "wins": c.wins,
        "win_rate_pct": (100 * c.wins / c.trips
                        if (c.wins is not None and c.trips) else None),
        "gross_usd": round(c.pnl_e4 / 10_000.0, 4),
        "fees_usd": round(c.fees_usd, 4),
        "net_usd": round(c.net_usd, 4),
        "blocked": c.blocked,
        "block_reasons": dict(c.block_reasons),
        "open_positions": {k: v for k, v in c.positions.items() if v},
    }


def save_backtest_result(cards: dict, symbol: str, meta: dict,
                         params: dict,
                         results_dir: str = RESULTS_DIR_DEFAULT) -> str:
    """meta: the dict returned alongside `cards` by run_backtest()
    (n_trades, first_t, last_t, trades_paths).
    params: every strategy/risk parameter used for this run — save
    everything relevant to reproducing it, not just the interesting
    ones; you won't know in six months which ones you'll wish you kept.
    Returns the path to the created run folder."""
    os.makedirs(results_dir, exist_ok=True)
    start = meta["first_t"].date().isoformat() if meta["first_t"] else "unknown"
    end = meta["last_t"].date().isoformat() if meta["last_t"] else "unknown"
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"{symbol}_{start}_{end}_{run_ts}"

    # second-resolution timestamps collide if two runs happen within the
    # same second (a real, reproducible case — not hypothetical: two
    # quick successive runs hit exactly this). Guarantee uniqueness with
    # a disambiguating suffix rather than trusting the timestamp alone,
    # so a later run can never silently overwrite an earlier one.
    folder = base
    n = 2
    while os.path.exists(os.path.join(results_dir, folder)):
        folder = f"{base}-{n}"
        n += 1
    run_dir = os.path.join(results_dir, folder)
    os.makedirs(run_dir)

    summary = {
        "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "date_range": {"start": start, "end": end},
        "trades_files": meta.get("trades_paths", []),
        "total_trades_replayed": meta["n_trades"],
        "strategy_params": params,
        "results": {k: _card_to_dict(c) for k, c in cards.items()},
    }
    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    with open(os.path.join(run_dir, "report.txt"), "w") as f:
        f.write(f"Backtest run: {symbol}  {start} .. {end}\n")
        f.write(f"Run at (UTC): {summary['run_timestamp_utc']}\n")
        f.write(f"Trades replayed: {meta['n_trades']:,}\n")
        f.write(f"Trades file(s): "
               f"{', '.join(meta.get('trades_paths', []))}\n")
        f.write("Strategy parameters:\n")
        for k, v in params.items():
            f.write(f"  {k}: {v}\n")
        f.write("\n" + comparison_report(cards) + "\n")

    return run_dir


def list_backtest_results(results_dir: str = RESULTS_DIR_DEFAULT
                          ) -> list[dict]:
    """Scan results_dir and return every run's summary dict (with its
    folder name attached), most recent first. Skips anything that
    isn't a valid saved run rather than failing the whole scan."""
    if not os.path.isdir(results_dir):
        return []
    runs = []
    for name in sorted(os.listdir(results_dir)):
        p = os.path.join(results_dir, name, "summary.json")
        if not os.path.exists(p):
            continue
        try:
            with open(p) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        data["folder"] = name
        runs.append(data)
    runs.sort(key=lambda r: r.get("run_timestamp_utc", ""), reverse=True)
    return runs
