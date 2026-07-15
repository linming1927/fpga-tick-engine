#!/usr/bin/env python3
"""
test_htf_ltf_strategy.py

    python3 test_htf_ltf_strategy.py
"""

from __future__ import annotations
import sys
from datetime import datetime, timedelta, timezone

from htf_ltf_strategy import BarAggregator, SingleEMA, HTFLTFScorecard
from tick_protocol import to_e4

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


# ---- G1: BarAggregator ------------------------------------------------------
print("[G1] bar aggregation: correct OHLC, correct boundaries")
agg = BarAggregator(interval_s=60)          # 1-minute bars for the test
t0 = datetime(2024, 1, 1, 9, 30, 0, tzinfo=timezone.utc)

# 3 ticks inside the FIRST minute
check("1st tick: no bar completes yet", agg.on_tick(t0, to_e4(100.0)), None)
check("2nd tick same bucket: still no bar",
      agg.on_tick(t0 + timedelta(seconds=10), to_e4(105.0)), None)
check("3rd tick same bucket: still no bar",
      agg.on_tick(t0 + timedelta(seconds=50), to_e4(95.0)), None)

# 4th tick, now in the NEXT minute -- the first bar must complete here
bar1 = agg.on_tick(t0 + timedelta(seconds=61), to_e4(102.0))
check("bar completes exactly when a new bucket starts", bar1 is not None, True)
check("open = first tick's price", bar1.open_e4, to_e4(100.0))
check("high = the max seen in the bucket", bar1.high_e4, to_e4(105.0))
check("low = the min seen in the bucket", bar1.low_e4, to_e4(95.0))
check("close = the LAST tick strictly inside the bucket "
     "(not the one that rolled it over)", bar1.close_e4, to_e4(95.0))
check("bar start is bucket-aligned", bar1.start, t0)

check("no bar yet for the second bucket (only one tick so far)",
      agg.on_tick(t0 + timedelta(seconds=90), to_e4(103.0)), None)
flushed = agg.flush()
check("flush() returns the final, still-open bar", flushed is not None, True)
check("flushed bar's close is the last tick seen", flushed.close_e4,
      to_e4(103.0))

# ---- G2: SingleEMA ------------------------------------------------------
print("[G2] textbook EMA math (alpha = 2/(N+1)), not the power-of-two kind")
ema = SingleEMA(period=3)                    # alpha = 2/4 = 0.5, easy by hand
check("alpha matches the textbook formula", ema.alpha, 0.5)
v1 = ema.update(to_e4(100.0))
check("first value seeds the EMA exactly", v1, to_e4(100.0))
v2 = ema.update(to_e4(110.0))
check("second value: 0.5*110 + 0.5*100 = 105 (in e4 units)",
      round(v2), to_e4(105.0))
check("not warmed up yet (period=3, only 2 updates)", ema.warmed_up, False)
ema.update(to_e4(120.0))
check("warmed up after `period` updates", ema.warmed_up, True)

# ---- G3: HTFLTFScorecard -- small periods so warmup is fast to test ------
print("[G3] HTF bias correctly gates entries; no trade when NOT bullish")
sc = HTFLTFScorecard("HTF/LTF test", symbol="TEST",
                    htf_interval_s=60, ltf_interval_s=10,
                    htf_periods=(2, 3, 4), ltf_periods=(2, 3))
t = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)

def feed(card, prices, start_t, step_s=10):
    t = start_t
    for p in prices:
        card.on_tick(t, to_e4(p))
        t += timedelta(seconds=step_s)
    return t

# Drive a clearly DESCENDING HTF trend (bearish stack) with plenty of
# LTF ticks so LTF EMAs warm up too -- entries must NOT fire here
descending = [100 - i * 0.5 for i in range(80)]
t = feed(sc, descending, t)
check("HTF warmed up", sc.htf_warmed_up, True)
check("bias correctly reads bearish for a descending trend",
      sc.bias, "bearish")
check("no position opened -- bias isn't bullish, entries must not fire",
      sc.positions.get("TEST", 0), 0)
check("no trips at all yet", sc.trips, 0)

print("[G4] a genuine bullish setup: HTF bullish stack + a FRESH LTF "
     "cross-up after a pullback (realistic -- HTF is slower-reacting "
     "than LTF, so bias typically flips bullish first, and the actual "
     "entry times off a subsequent LTF pullback/re-cross, not the very "
     "first uptick)")
sc2 = HTFLTFScorecard("HTF/LTF test", symbol="TEST",
                     htf_interval_s=60, ltf_interval_s=10,
                     htf_periods=(2, 3, 4), ltf_periods=(2, 3))
t2 = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
t2 = feed(sc2, [100.0] * 60, t2)                          # warm up flat
t2 = feed(sc2, [100 + i * 1.0 for i in range(1, 15)], t2)  # flips HTF bullish
check("bias is bullish after the initial ascent", sc2.bias, "bullish")
check("no entry yet -- the LTF cross that got us here happened BEFORE "
     "bias turned bullish, so it isn't fresh", sc2.trips, 0)
t2 = feed(sc2, [114 - i * 1.5 for i in range(1, 6)], t2)   # brief pullback
check("bias survives the brief pullback (HTF reacts slower than LTF)",
      sc2.bias, "bullish")
t2 = feed(sc2, [107 + i * 1.0 for i in range(1, 10)], t2)  # resumed ascent
check("a position WAS opened on the fresh LTF cross-up during the "
     "resumed ascent, with bias still bullish",
     sc2.positions.get("TEST", 0) > 0, True)
check("exactly one buy fill so far (holding, not yet exited)",
      (sc2.trips, sc2.opens.get("TEST") is not None), (0, True))

print("[G5] trailing exit: a close back below the LTF fast EMA closes it")
falling = [sc2.opens["TEST"] / 10_000 - i * 2.0 for i in range(1, 15)]
t2 = feed(sc2, falling, t2)
check("the position closed on the trailing-exit rule",
      sc2.positions.get("TEST", 0), 0)
check("exactly one completed trip", sc2.trips, 1)

print("[G6] never re-enters while already in a position "
     "(no pyramiding into this strategy, by construction)")
sc3 = HTFLTFScorecard("HTF/LTF test", symbol="TEST",
                     htf_interval_s=60, ltf_interval_s=10,
                     htf_periods=(2, 3, 4), ltf_periods=(2, 3))
t3 = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
t3 = feed(sc3, [100.0] * 60, t3)
t3 = feed(sc3, [100 + i * 1.0 for i in range(1, 15)], t3)
t3 = feed(sc3, [114 - i * 1.5 for i in range(1, 6)], t3)
t3 = feed(sc3, [107 + i * 1.0 for i in range(1, 10)], t3)
qty_after_first_entry = sc3.positions.get("TEST", 0)
check("position opened", qty_after_first_entry > 0, True)
# keep ascending -- more fast-above-slow ticks, but NOT a fresh cross
# (already above), so no additional buy should fire
t3 = feed(sc3, [140 + i * 0.5 for i in range(20)], t3)
check("position size UNCHANGED -- no pyramiding while already in",
      sc3.positions.get("TEST", 0), qty_after_first_entry)

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
