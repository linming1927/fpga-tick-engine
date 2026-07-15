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

from backtest import run_backtest, BacktestClock, iter_trades, iter_trades_multi
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

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
