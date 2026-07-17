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

from compare import (StrategyScorecard, comparison_report,
                     monthly_breakdown_report)

RESULTS_DIR_DEFAULT = "./backtest_results"


def _card_to_dict(c: StrategyScorecard) -> dict:
    d = {
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
    # profit-gated cards with a max-hold bound: how often it fired is
    # the number that tells you whether the bound is doing real work
    if getattr(c, "max_hold_days", None) is not None:
        d["max_hold_days"] = c.max_hold_days
        d["forced_exits"] = getattr(c, "forced_exits", 0)
    # blend cards: the two numbers the per-strategy tables can't show
    # (unrealized mark on open lots; combined realized max drawdown),
    # plus each sleeve saved in full so a later comparison doesn't
    # have to re-derive them from the merged row
    if hasattr(c, "unrealized_usd"):
        d["unrealized_usd"] = round(c.unrealized_usd(), 4)
        d["max_drawdown_usd"] = round(c.max_drawdown_usd(), 4)
        d["sleeves"] = {"vwap": _card_to_dict(c.vwap),
                       "sma_pg": _card_to_dict(c.pg)}
    return d


def _monthly_dict(cards: dict) -> dict:
    """JSON-serializable version of the same grouping
    monthly_breakdown_report() prints — one {month: {...}} dict per
    strategy key, keyed the same way cards is."""
    out = {}
    for key, c in cards.items():
        if not c.trip_log:
            continue
        by_month: dict[str, list] = {}
        for trip in c.trip_log:
            ym = (trip["close_t"].strftime("%Y-%m")
                 if trip["close_t"] is not None else "unknown")
            by_month.setdefault(ym, []).append(trip)
        out[key] = {
            ym: {
                "trips": len(trips),
                "wins": sum(1 for tr in trips if tr["win"]),
                "gross_usd": round(sum(tr["pnl_e4"] for tr in trips)
                                  / 10_000.0, 4),
                "fees_usd": round(sum(tr["fees_usd"] for tr in trips), 4),
            }
            for ym, trips in sorted(by_month.items())
        }
    return out


def save_backtest_result(cards: dict, symbol: str, meta: dict,
                         params: dict,
                         results_dir: str = RESULTS_DIR_DEFAULT,
                         include_monthly: bool = False) -> str:
    """meta: the dict returned alongside `cards` by run_backtest()
    (n_trades, first_t, last_t, trades_paths, interrupted).
    params: every strategy/risk parameter used for this run — save
    everything relevant to reproducing it, not just the interesting
    ones; you won't know in six months which ones you'll wish you kept.
    Returns the path to the created run folder.

    If meta["interrupted"] is True (a Ctrl+C during the replay loop —
    see run_backtest()'s own KeyboardInterrupt handling), this is
    marked unmistakably in three places: an "-INTERRUPTED" folder-name
    suffix (visible in list_backtest_results.py's table without even
    opening the run), summary.json's own "interrupted" field, and a
    banner at the top of report.txt — a partial result should never be
    mistaken for a complete backtest later."""
    os.makedirs(results_dir, exist_ok=True)
    start = meta["first_t"].date().isoformat() if meta["first_t"] else "unknown"
    end = meta["last_t"].date().isoformat() if meta["last_t"] else "unknown"
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    interrupted = bool(meta.get("interrupted"))
    base = f"{symbol}_{start}_{end}_{run_ts}" + (
        "-INTERRUPTED" if interrupted else "")

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
        "interrupted": interrupted,
        "strategy_params": params,
        "results": {k: _card_to_dict(c) for k, c in cards.items()},
    }
    if include_monthly:
        summary["monthly"] = _monthly_dict(cards)
    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    with open(os.path.join(run_dir, "report.txt"), "w") as f:
        if interrupted:
            f.write("=" * 60 + "\n")
            f.write("*** INTERRUPTED RUN — PARTIAL RESULTS BELOW, NOT A "
                   "COMPLETE BACKTEST ***\n")
            f.write("=" * 60 + "\n\n")
        f.write(f"Backtest run: {symbol}  {start} .. {end}\n")
        f.write(f"Run at (UTC): {summary['run_timestamp_utc']}\n")
        f.write(f"Trades replayed: {meta['n_trades']:,}"
               + (" [INCOMPLETE]" if interrupted else "") + "\n")
        f.write(f"Trades file(s): "
               f"{', '.join(meta.get('trades_paths', []))}\n")
        f.write("Strategy parameters:\n")
        for k, v in params.items():
            f.write(f"  {k}: {v}\n")
        f.write("\n" + comparison_report(cards) + "\n")
        if include_monthly:
            f.write("\n" + monthly_breakdown_report(cards) + "\n")

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
