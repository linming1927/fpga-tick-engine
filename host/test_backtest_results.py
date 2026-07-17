#!/usr/bin/env python3
"""
test_backtest_results.py

    python3 test_backtest_results.py
"""

from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from backtest_results import (save_backtest_result, list_backtest_results)
from backtest import run_backtest
from order_manager import RiskLimits

# Resolve backtest.py's path relative to THIS FILE, not the caller's
# current working directory — already fixed once (v3.0.1) but lost when
# a sandbox reset caused this file to be rebuilt from a copy that
# predated the fix. Re-applying it.
BACKTEST_PY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "backtest.py")

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


def write_jsonl(path, rows):
    with open(path, "w") as f:
        for t, p in rows:
            f.write(json.dumps({"t": t.isoformat().replace("+00:00", "Z"),
                                "p": p}) + "\n")


tmp = tempfile.mkdtemp()
results_dir = os.path.join(tmp, "backtest_results")

# ---- G1: a real run through run_backtest, saved and read back --------------
print("[G1] a saved run round-trips correctly through save + list")
p1 = os.path.join(tmp, "t1.jsonl")
day1 = datetime(2023, 7, 16, 14, 30, tzinfo=timezone.utc)
prices = [2000, 1900, 1800, 1700, 1600, 1500, 1400, 1300, 3000, 3100]
rows = [(day1 + timedelta(seconds=i), p) for i, p in enumerate(prices)]
write_jsonl(p1, rows)

limits = RiskLimits(order_qty=1, max_shares=1, max_notional_e4=10**13,
                   max_orders_per_day=10, cooldown_s=0.0,
                   require_market_hours=False)
cards, meta = run_backtest(p1, "RKLB", fast_n=4, slow_n=8, ema_kf=1, ema_ks=3,
                          limits=limits, traded_strategy="sma")
params = {"traded_strategy": "sma", "fast": 4, "slow": 8, "ema_kf": 1,
         "ema_ks": 3, "order_qty": 1, "max_shares": 1,
         "max_notional": 1_000_000.0, "max_orders_per_day": 10,
         "cooldown": 0.0}
run_dir = save_backtest_result(cards, "RKLB", meta, params,
                               results_dir=results_dir)

check("run folder was created", os.path.isdir(run_dir), True)
check("folder name includes the symbol", "RKLB" in os.path.basename(run_dir),
      True)
check("folder name includes the REAL data date (2023-07-16), not "
     "today's date or a filename guess",
     "2023-07-16" in os.path.basename(run_dir), True)
check("summary.json exists", os.path.exists(os.path.join(run_dir,
                                                        "summary.json")),
      True)
check("report.txt exists", os.path.exists(os.path.join(run_dir,
                                                       "report.txt")), True)

with open(os.path.join(run_dir, "summary.json")) as f:
    summary = json.load(f)
check("summary records the symbol", summary["symbol"], "RKLB")
check("summary records the real date range", summary["date_range"],
      {"start": "2023-07-16", "end": "2023-07-16"})
check("summary records total trades replayed", summary["total_trades_replayed"],
      10)
check("summary records ALL strategy params, not just some",
      summary["strategy_params"], params)
check("summary has both strategies' results",
      set(summary["results"].keys()), {"sma", "ema"})
check("SMA result carries real numbers, not placeholders",
      summary["results"]["sma"]["signals"], cards["sma"].signals)
check("win_rate_pct is present when trips > 0 (may be None if 0 trips)",
      "win_rate_pct" in summary["results"]["sma"], True)

with open(os.path.join(run_dir, "report.txt")) as f:
    report_text = f.read()
check("report.txt contains the human-readable comparison table",
      "strategy comparison" in report_text, True)
check("report.txt lists the strategy parameters",
      "fast: 4" in report_text, True)

# ---- G2: listing finds it, with correct fields ------------------------------
print("[G2] list_backtest_results finds and correctly summarizes the run")
runs = list_backtest_results(results_dir)
check("exactly one run listed", len(runs), 1)
check("listed run has the folder name attached", "folder" in runs[0], True)
check("listed run's symbol matches", runs[0]["symbol"], "RKLB")

# ---- G3: multiple runs never collide, even same symbol + same range -------
print("[G3] rerunning the SAME symbol/range with DIFFERENT parameters "
     "creates a separate run, never overwriting the earlier one")
cards2, meta2 = run_backtest(p1, "RKLB", fast_n=4, slow_n=8, ema_kf=2,
                            ema_ks=6, limits=limits, traded_strategy="ema")
params2 = dict(params, traded_strategy="ema", ema_kf=2, ema_ks=6)
run_dir2 = save_backtest_result(cards2, "RKLB", meta2, params2,
                                results_dir=results_dir)
check("second run got its OWN folder", run_dir2 != run_dir, True)
check("first run's folder is untouched", os.path.isdir(run_dir), True)
runs_after = list_backtest_results(results_dir)
check("both runs are now listed", len(runs_after), 2)
check("most-recent-first ordering: the second run appears first",
      runs_after[0]["strategy_params"]["ema_kf"], 2)

# ---- G4: --symbol filtering in the CLI tool ---------------------------------
print("[G4] a second symbol is filterable separately")
p4 = os.path.join(tmp, "t4.jsonl")
write_jsonl(p4, rows)   # reuse the same synthetic prices, different symbol
cards4, meta4 = run_backtest(p4, "SPY", fast_n=4, slow_n=8, ema_kf=1, ema_ks=3,
                            limits=limits, traded_strategy="sma")
save_backtest_result(cards4, "SPY", meta4, params, results_dir=results_dir)
all_runs = list_backtest_results(results_dir)
check("three total runs now", len(all_runs), 3)
rklb_only = [r for r in all_runs if r["symbol"] == "RKLB"]
check("filtering by symbol works (2 RKLB runs, not 3)", len(rklb_only), 2)

# ---- G5: backtest.py's actual CLI saves by default, --no-save skips it ----
print("[G5] backtest.py's real CLI: saves by default, --no-save skips it")
env_results = os.path.join(tmp, "cli_results")
r = subprocess.run(
    [sys.executable, BACKTEST_PY, "--trades", p1, "--symbol", "RKLB",
     "--strategy", "sma", "--fast", "4", "--slow", "8", "--ema-kf", "1",
     "--ema-ks", "3", "--results-dir", env_results],
    capture_output=True, text=True, timeout=30)
check("CLI run succeeded", r.returncode, 0)
check("CLI announces where it saved", "saved to" in r.stdout, True)
check("CLI actually created the results dir", os.path.isdir(env_results),
      True)

env_results2 = os.path.join(tmp, "cli_results_nosave")
r2 = subprocess.run(
    [sys.executable, BACKTEST_PY, "--trades", p1, "--symbol", "RKLB",
     "--strategy", "sma", "--fast", "4", "--slow", "8", "--ema-kf", "1",
     "--ema-ks", "3", "--results-dir", env_results2, "--no-save"],
    capture_output=True, text=True, timeout=30)
check("--no-save succeeded", r2.returncode, 0)
check("--no-save did not print a save confirmation", "saved to" in r2.stdout,
      False)
check("--no-save did not create the results dir at all",
      os.path.isdir(env_results2), False)

shutil.rmtree(tmp, ignore_errors=True)

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
