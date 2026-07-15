#!/usr/bin/env python3
"""
test_backtest.py

    python3 test_backtest.py
"""

from __future__ import annotations
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import json as _json
import subprocess
from backtest import run_backtest, BacktestClock, iter_trades, iter_trades_multi
from backtest_results import save_backtest_result
from compare import comparison_report
from order_manager import RiskLimits, RiskPolicy
from tick_protocol import to_e4

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

# ---- G1: iter_trades parses the documented Alpaca trade schema ------------
print("[G1] streaming JSONL parse")
p1 = os.path.join(tmp, "t1.jsonl")
base = datetime(2020, 1, 2, 14, 30, tzinfo=timezone.utc)
write_jsonl(p1, [(base, 100.00), (base + timedelta(seconds=1), 100.50)])
rows = list(iter_trades(p1))
check("two rows parsed", len(rows), 2)
check("timestamp parsed correctly", rows[0][0], base)
check("price converted to e4 fixed point", rows[0][1], to_e4(100.00))
check("second row's price", rows[1][1], to_e4(100.50))

# ---- G2: crossover detection matches this project's known golden anchors --
print("[G2] SMA/EMA crossover matches the silicon-anchored sequence "
     "already used elsewhere in this project")
p2 = os.path.join(tmp, "t2.jsonl")
day1 = datetime(2020, 1, 2, 14, 30, tzinfo=timezone.utc)
prices = [2000, 1900, 1800, 1700, 1600, 1500, 1400, 1300,   # warm-up
         3000, 3100]                                       # spike, hold
rows = [(day1 + timedelta(seconds=i), p) for i, p in enumerate(prices)]
write_jsonl(p2, rows)

limits = RiskLimits(order_qty=1, max_shares=1, max_notional_e4=to_e4(10**7),
                   max_orders_per_day=99, cooldown_s=0.0,
                   require_market_hours=False)
cards, meta2 = run_backtest(p2, "SPY", fast_n=4, slow_n=8, ema_kf=1, ema_ks=3,
                          limits=limits, traded_strategy="sma")
check("meta reports the real trade count", meta2["n_trades"], 10)
check("meta's date range comes from the actual data, not a filename",
      (meta2["first_t"].date(), meta2["last_t"].date()),
      (day1.date(), day1.date()))
check("SMA fired exactly one golden-cross signal", cards["sma"].signals, 1)
check("SMA position opened at the spike price",
      cards["sma"].opens.get("SPY"), to_e4(3000))
check("EMA fired exactly one golden-cross signal", cards["ema"].signals, 1)
check("EMA position opened at the spike price",
      cards["ema"].opens.get("SPY"), to_e4(3000))

# ---- G3: the historical clock drives daily-cap rollover, NOT wall time ----
print("[G3] daily order cap rolls over against HISTORICAL dates, not "
     "real elapsed wall-clock time (the whole point of BacktestClock)")
p3 = os.path.join(tmp, "t3.jsonl")
rows = []
t = datetime(2020, 1, 2, 14, 30, tzinfo=timezone.utc)
# Day 1: descending warmup (starts "below", matching this project's
# established anchor pattern), then 2 full settled plateaus = 2 clean
# crossovers (BUY, SELL) -- confirmed empirically, not assumed.
# A tight cap of 1/day means the SECOND of these (the SELL) must block.
day1_prices = ([2000, 1900, 1800, 1700, 1600, 1500, 1400, 1300]  # warm-up
              + [3000]*4      # settle high -> BUY (order #1 today)
              + [100]*4)      # settle low  -> SELL (order #2: BLOCKED,
                             #                        cap is 1/day)
for i, p in enumerate(day1_prices):
    rows.append((t + timedelta(seconds=i), p))
# Day 2 (next calendar day): the cap must reset, so BOTH a further drop
# (closing the still-open day-1 position) and a fresh cycle should be
# allowed here even though day 1 already hit its cap.
t2 = datetime(2020, 1, 3, 14, 30, tzinfo=timezone.utc)
day2_prices = [100, 100, 100, 100,       # still low: no new crossover yet
              3000, 3000, 3000, 3000,   # -> BUY (day 2, order #1)
              100, 100, 100, 100]       # -> SELL (day 2, order #2: BLOCKED)
for i, p in enumerate(day2_prices):
    rows.append((t2 + timedelta(seconds=i), p))
write_jsonl(p3, rows)

tight = RiskLimits(order_qty=1, max_shares=1, max_notional_e4=to_e4(10**7),
                   max_orders_per_day=1, cooldown_s=0.0,
                   require_market_hours=False)
cards3, meta3 = run_backtest(p3, "SPY", fast_n=4, slow_n=8, ema_kf=1, ema_ks=3,
                            limits=tight, traded_strategy="sma")
sma = cards3["sma"]
check("day-1's 2nd order (the SELL) blocked by the 1/day cap",
      sma.blocked >= 1, True)
check("day-1's BUY still went through (1 order allowed per day)",
      sma.signals >= 2, True)
check("day-2's cap independently allowed a fresh order after rollover",
      sma.signals >= 3, True)
check("daily cap is the recorded block reason",
      any("daily order cap" in r for r in sma.block_reasons), True)

# ---- G4: cooldown uses the trade's own timestamp, not process wall-time --
print("[G4] cooldown gates against historical elapsed time between trades")
p4 = os.path.join(tmp, "t4.jsonl")
t0 = datetime(2020, 1, 2, 14, 30, tzinfo=timezone.utc)
# Confirmed empirically (see the probe above): this exact sequence
# produces 4 model-level crossovers at these historical offsets:
#   +8s   BUY   (first order ever: allowed, opens position)
#   +14s  SELL  (6s after the BUY: cooldown(60s) blocks it -- position
#                stays open in the scorecard, though the MODEL's own
#                internal state still flips regardless of the policy)
#   +102s BUY   (94s after the BUY: cooldown has expired, but max_shares=1
#                is already fully used from the still-open position, so
#                this is refused for exceeding max_shares instead)
#   +151s SELL  (143s after the original BUY: cooldown expired AND the
#                position is genuinely open -> this one succeeds,
#                closing the trip)
offsets_prices = [(0,2000),(1,1900),(2,1800),(3,1700),(4,1600),(5,1500),
                 (6,1400),(7,1300),                              # warmup
                 (8,3000),(9,3000),(10,3000),(11,3000),          # -> BUY
                 (14,100),(15,100),(16,100),(17,100),            # -> SELL (blocked)
                 (102,3000),(103,3000),(104,3000),(105,3000),    # -> BUY (blocked)
                 (151,100),(152,100),(153,100),(154,100)]        # -> SELL (succeeds)
rows = [(t0 + timedelta(seconds=s), p) for s, p in offsets_prices]
write_jsonl(p4, rows)

cooldown_limits = RiskLimits(order_qty=1, max_shares=1,
                             max_notional_e4=to_e4(10**7),
                             max_orders_per_day=99, cooldown_s=60.0,
                             require_market_hours=False)
cards4, meta4 = run_backtest(p4, "SPY", fast_n=4, slow_n=8, ema_kf=1, ema_ks=3,
                            limits=cooldown_limits, traded_strategy="sma")
sma4 = cards4["sma"]
check("all 4 model crossovers counted as signals", sma4.signals, 4)
check("cooldown blocked the too-soon SELL (6s later, 60s cooldown)",
      any("cooldown" in r for r in sma4.block_reasons), True)
check("max_shares blocked the BUY while still open (94s later, "
     "cooldown had expired by then -- proves the clock is historical, "
     "not wall-clock: this whole test runs in milliseconds of real time)",
      any("max_shares" in r for r in sma4.block_reasons), True)
check("the far-later SELL (143s after the open) succeeded, closing 1 trip",
      sma4.trips, 1)
check("exactly 2 of the 4 signals were blocked", sma4.blocked, 2)

# ---- G5: multi-file replay (combining incrementally-fetched ranges) ------
print("[G5] backtest.py replays several files as one continuous history "
     "-- the companion fix to the range-scoping bug: no need to "
     "re-download a wider range if you can just combine what you have")
pA = os.path.join(tmp, "partA.jsonl")
pB = os.path.join(tmp, "partB.jsonl")
dayA = datetime(2020, 1, 2, 14, 30, tzinfo=timezone.utc)
write_jsonl(pA, [(dayA + timedelta(seconds=i), p)
                for i, p in enumerate([2000,1900,1800,1700,1600,1500,1400,1300])])
dayB = datetime(2020, 1, 3, 14, 30, tzinfo=timezone.utc)   # strictly later
write_jsonl(pB, [(dayB + timedelta(seconds=i), p)
                for i, p in enumerate([3000,3000,3000,3000])])
rows_multi = list(iter_trades_multi([pA, pB]))
check("multi-file stream has all rows from both files", len(rows_multi), 12)
check("chronological order preserved across the file boundary",
      all(rows_multi[i][0] <= rows_multi[i+1][0]
          for i in range(len(rows_multi)-1)), True)

cards5, meta5 = run_backtest([pA, pB], "SPY", fast_n=4, slow_n=8, ema_kf=1,
                            ema_ks=3, limits=limits, traded_strategy="sma")
check("meta's date range spans BOTH files (first file's start, "
     "second file's end)",
     (meta5["first_t"].date(), meta5["last_t"].date()),
     (dayA.date(), dayB.date()))
check("crossover detected correctly ACROSS the file boundary "
     "(warmup in file A, spike in file B)", cards5["sma"].signals, 1)

# out-of-order files must raise, not silently corrupt the backtest
pC = os.path.join(tmp, "partC.jsonl")   # dated BEFORE partA -- out of order
dayC = datetime(2019, 1, 1, tzinfo=timezone.utc)
write_jsonl(pC, [(dayC, 1000)])
try:
    list(iter_trades_multi([pA, pC]))
    check("out-of-order files raise instead of silently corrupting",
          "no error", "error")
except ValueError:
    check("out-of-order files raise instead of silently corrupting",
          "error", "error")

# ---- v3.5: --profit-gate in backtest.py (wasn't wired in at all) --------
print("[G6] backtest.py can also score the profit-gated variant "
     "alongside SMA/EMA")
p6 = os.path.join(tmp, "t6.jsonl")
day6 = datetime(2020, 1, 2, 14, 30, tzinfo=timezone.utc)
prices6 = [2000, 1900, 1800, 1700, 1600, 1500, 1400, 1300,  # warmup
          3000, 3100,                                       # BUY, hold
          2900, 2800]                                       # settle low
rows6 = [(day6 + timedelta(seconds=i), p) for i, p in enumerate(prices6)]
write_jsonl(p6, rows6)

cards6, meta6 = run_backtest(p6, "SPY", fast_n=4, slow_n=8, ema_kf=1,
                            ema_ks=3, limits=limits, traded_strategy="sma",
                            profit_gate=True)
check("profit-gated card is present when requested", "sma_pg" in cards6,
      True)
check("it received the SAME sma crossover signal as the plain SMA card",
      cards6["sma_pg"].signals, cards6["sma"].signals)
check("profit-gated card is always score-only (live=False)",
      cards6["sma_pg"].live, False)

cards7, meta7 = run_backtest(p6, "SPY", fast_n=4, slow_n=8, ema_kf=1,
                            ema_ks=3, limits=limits, traded_strategy="sma")
check("profit-gated card is absent when NOT requested (default off)",
      "sma_pg" in cards7, False)

r6 = comparison_report(cards6)
check("comparison report includes the profit-gated row",
      "SMA profit-gated" in r6, True)

# ---- end-to-end: the actual CLI, not just the function -------------------
print("[G7] the real backtest.py --profit-gate CLI works end to end")
r = subprocess.run(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "backtest.py"),
     "--trades", p6, "--symbol", "SPY", "--strategy", "sma",
     "--fast", "4", "--slow", "8", "--ema-kf", "1", "--ema-ks", "3",
     "--profit-gate", "--no-save"],
    capture_output=True, text=True, timeout=30)
check("CLI run succeeded", r.returncode, 0)
check("CLI output includes the profit-gated row",
      "SMA profit-gated" in r.stdout, True)

# ---- v3.6: default risk limits must match order_manager.py exactly,
# so a bare backtest.py run (no risk-limit flags) faithfully reproduces
# what a default LIVE session would have done -- found via a real
# report: backtest.py's own defaults for max_shares ($1, not 10) and
# max_notional ($1M, effectively a no-op, not $2,000) silently diverged
# from the live tool's defaults. -----------------------------------
print("[G8] backtest.py's CLI defaults match order_manager.py's exactly")
import re

bt_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "backtest.py")).read()
om_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "order_manager.py")).read()

def cli_default(module_src, flag):
    m = re.search(re.escape(f'"{flag}"') + r'.*?default=([\d_.]+)',
                 module_src)
    return float(m.group(1).replace("_", "")) if m else None

for flag in ("--max-shares", "--max-notional", "--max-orders-per-day"):
    check(f"{flag} default matches between backtest.py and order_manager.py",
          cli_default(bt_src, flag), cli_default(om_src, flag))
check("max_orders_per_day default is specifically 1000 (per request), "
     "not the old value of 10",
     cli_default(bt_src, "--max-orders-per-day"), 1000.0)
check("max_shares default is specifically 10, not the old value of 1",
      cli_default(bt_src, "--max-shares"), 10.0)
check("max_notional default is specifically $2,000, not the old "
     "effectively-unlimited $1,000,000",
     cli_default(bt_src, "--max-notional"), 2000.0)

# ---- v3.7: --htf-ltf wired into backtest.py -------------------------------
print("[G9] backtest.py can also score the HTF/LTF trend strategy")
p9 = os.path.join(tmp, "t9.jsonl")
t9 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
rows9 = []
price = 100.0
for minute in range(0, 400, 5):     # small custom periods below need far
    price += 0.3                    # less warmup than the real 20/50/200
    rows9.append((t9 + timedelta(minutes=minute), price))
write_jsonl(p9, rows9)

cards9, meta9 = run_backtest(p9, "SPY", fast_n=4, slow_n=8, ema_kf=1,
                            ema_ks=3, limits=limits, traded_strategy="sma",
                            htf_ltf=True)
check("htf_ltf card is present when requested", "htf_ltf" in cards9, True)
check("it is always score-only (live=False)", cards9["htf_ltf"].live, False)

cards10, meta10 = run_backtest(p9, "SPY", fast_n=4, slow_n=8, ema_kf=1,
                              ema_ks=3, limits=limits, traded_strategy="sma")
check("htf_ltf card is absent when NOT requested (default off)",
      "htf_ltf" in cards10, False)

r9 = comparison_report(cards9)
check("comparison report includes the HTF/LTF row",
      "HTF/LTF trend" in r9, True)

print("[G10] the real backtest.py --htf-ltf CLI works end to end")
r = subprocess.run(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "backtest.py"),
     "--trades", p9, "--symbol", "SPY", "--strategy", "sma",
     "--fast", "4", "--slow", "8", "--ema-kf", "1", "--ema-ks", "3",
     "--htf-ltf", "--htf-interval", "3600", "--ltf-interval", "300",
     "--no-save"],
    capture_output=True, text=True, timeout=30)
check("CLI run succeeded", r.returncode, 0)
check("CLI output includes the HTF/LTF row", "HTF/LTF trend" in r.stdout,
      True)

# ---- v3.8: --vwap-bounce wired into backtest.py ---------------------------
print("[G11] backtest.py can also score the VWAP bounce strategy")
p11 = os.path.join(tmp, "t11.jsonl")
t11 = datetime(2024, 1, 2, 14, 30, 0, tzinfo=timezone.utc)
rows11 = []
price = 100.0
for i in range(200):
    price += (0.05 if i % 7 else -0.4)   # mostly drifting up, periodic dips
    rows11.append((t11 + timedelta(seconds=i * 5), price))
with open(p11, "w") as f:
    for t_, p_ in rows11:
        f.write(_json.dumps({"t": t_.isoformat().replace("+00:00", "Z"),
                            "p": round(p_, 2), "s": 100}) + "\n")

cards11, meta11 = run_backtest(p11, "SPY", fast_n=4, slow_n=8, ema_kf=1,
                               ema_ks=3, limits=limits,
                               traded_strategy="sma", vwap_bounce=True)
check("vwap_bounce card is present when requested",
      "vwap_bounce" in cards11, True)
check("it is always score-only (live=False)",
      cards11["vwap_bounce"].live, False)

cards12, meta12 = run_backtest(p11, "SPY", fast_n=4, slow_n=8, ema_kf=1,
                               ema_ks=3, limits=limits,
                               traded_strategy="sma")
check("vwap_bounce card is absent when NOT requested (default off)",
      "vwap_bounce" in cards12, False)

r11 = comparison_report(cards11)
check("comparison report includes the VWAP bounce row",
      "VWAP bounce" in r11, True)

print("[G12] the real backtest.py --vwap-bounce CLI works end to end")
r = subprocess.run(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "backtest.py"),
     "--trades", p11, "--symbol", "SPY", "--strategy", "sma",
     "--fast", "4", "--slow", "8", "--ema-kf", "1", "--ema-ks", "3",
     "--vwap-bounce", "--vwap-band-k", "1.0", "--no-save"],
    capture_output=True, text=True, timeout=30)
check("CLI run succeeded", r.returncode, 0)
check("CLI output includes the VWAP bounce row",
      "VWAP bounce" in r.stdout, True)

# ---- v3.9: Ctrl+C during a backtest must still produce a report -----------
print("[G13] an interrupted backtest returns PARTIAL results instead "
     "of crashing past the report/save steps entirely")
import backtest as backtest_module

p13 = os.path.join(tmp, "t13.jsonl")
day13 = datetime(2020, 1, 2, 14, 30, tzinfo=timezone.utc)
write_jsonl(p13, [(day13 + timedelta(seconds=i), p)
                  for i, p in enumerate([2000,1900,1800,1700,1600,1500,
                                        1400,1300,3000,3100,2900,2800])])

real_iter = backtest_module.iter_trades_multi

def interrupting_iter(paths):
    """Simulates Ctrl+C arriving partway through: raises
    KeyboardInterrupt after a fixed number of trades, deterministically
    (real signal-timing tests would be flaky; this isn't)."""
    count = 0
    for item in real_iter(paths):
        count += 1
        if count > 9:      # after the warmup, before the golden cross
            raise KeyboardInterrupt()
        yield item

backtest_module.iter_trades_multi = interrupting_iter
try:
    cards13, meta13 = run_backtest(p13, "SPY", fast_n=4, slow_n=8, ema_kf=1,
                                   ema_ks=3, limits=limits,
                                   traded_strategy="sma")
finally:
    backtest_module.iter_trades_multi = real_iter    # always restore

check("run_backtest returns normally instead of letting "
     "KeyboardInterrupt propagate and crash past the report/save steps",
     isinstance(cards13, dict), True)
check("meta correctly flags this as an interrupted, partial run",
      meta13["interrupted"], True)
check("meta still reports how far it got (9 trades, not 12)",
      meta13["n_trades"], 9)
check("cards are still usable -- partial state, not empty/broken",
      cards13["sma"].signals >= 0, True)

r13 = comparison_report(cards13)
check("comparison_report() still renders on partial/interrupted state "
     "without crashing", "strategy comparison" in r13, True)

# the save path must also mark this clearly, not silently save it as
# if it were a complete run
results_dir13 = os.path.join(tmp, "interrupted_results")
run_dir13 = save_backtest_result(cards13, "SPY", meta13,
                                 {"traded_strategy": "sma"},
                                 results_dir=results_dir13)
check("the saved folder name is marked INTERRUPTED, visible without "
     "opening the run", "INTERRUPTED" in os.path.basename(run_dir13), True)
with open(os.path.join(run_dir13, "summary.json")) as f:
    saved13 = _json.load(f)
check("summary.json itself also records interrupted=True",
      saved13["interrupted"], True)
with open(os.path.join(run_dir13, "report.txt")) as f:
    report_text13 = f.read()
check("report.txt has an unmistakable interrupted banner at the top",
      "INTERRUPTED" in report_text13.split("\n")[1], True)

# a NORMAL, complete run must NOT be mislabeled
cards14, meta14 = run_backtest(p13, "SPY", fast_n=4, slow_n=8, ema_kf=1,
                               ema_ks=3, limits=limits,
                               traded_strategy="sma")
check("a normal, uninterrupted run is NOT flagged as interrupted",
      meta14["interrupted"], False)
run_dir14 = save_backtest_result(cards14, "SPY", meta14,
                                 {"traded_strategy": "sma"},
                                 results_dir=results_dir13)
check("a normal run's folder has no INTERRUPTED suffix",
      "INTERRUPTED" in os.path.basename(run_dir14), False)

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
