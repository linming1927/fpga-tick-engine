#!/usr/bin/env python3
"""
test_vwap_bounce_strategy.py

    python3 test_vwap_bounce_strategy.py
"""

from __future__ import annotations
import sys
from datetime import datetime, timedelta, timezone

from vwap_bounce_strategy import VWAPBounceScorecard
from tick_protocol import to_e4

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


def feed(card, ticks, start_t, step_s=5):
    """ticks: list of (price, qty) or bare price (qty defaults to 100)."""
    t = start_t
    for item in ticks:
        p, q = item if isinstance(item, tuple) else (item, 100)
        card.on_tick(t, to_e4(p), q)
        t += timedelta(seconds=step_s)
    return t


# ---- G1: VWAP itself, against a hand-computed example ----------------------
print("[G1] VWAP matches a hand-computed volume-weighted average")
sc = VWAPBounceScorecard("t", symbol="TEST", min_session_ticks=100)
t0 = datetime(2024, 1, 2, 14, 30, 0, tzinfo=timezone.utc)
# (100 @ qty 10) + (110 @ qty 30) -> vwap = (100*10 + 110*30)/(10+30)
sc.on_tick(t0, to_e4(100.0), 10)
sc.on_tick(t0 + timedelta(seconds=5), to_e4(110.0), 30)
expected_vwap = (100.0 * 10 + 110.0 * 30) / 40
check("vwap matches the hand-computed volume-weighted average",
      round(sc.vwap / 10_000, 6), round(expected_vwap, 6))

sc2 = VWAPBounceScorecard("t", symbol="TEST", min_session_ticks=100)
check("a zero/missing volume tick doesn't zero the denominator "
     "(clamped to at least 1)",
     sc2.on_tick(t0, to_e4(100.0), 0) or True, True)
check("vwap is still defined after a zero-volume tick", sc2.vwap, to_e4(100.0))

# ---- G2: no signals before the session warms up ----------------------------
print("[G2] no entries before min_session_ticks, even with a real dip")
sc3 = VWAPBounceScorecard("t", symbol="TEST", band_k=1.0,
                          min_session_ticks=50)   # deliberately high
t = feed(sc3, [100.0]*10 + [95.0]*3 + [100.0]*3, t0)
check("not warmed up yet", sc3.warmed_up, False)
check("no position opened despite a real dip+recovery pattern "
     "(warmup gate must hold)", sc3.positions.get("TEST", 0), 0)

# ---- G3: the validated bounce scenario ------------------------------------
print("[G3] a real dip below the lower band, then a bounce back above "
     "it, triggers a long entry")
sc4 = VWAPBounceScorecard("t", symbol="TEST", band_k=1.0,
                          min_session_ticks=10)
t4 = datetime(2024, 1, 2, 14, 30, 0, tzinfo=timezone.utc)
dip_bounce = ([100.0]*15 + [99.9, 99.8, 99.5, 99.0, 98.5]
             + [99.2, 99.8, 100.3, 100.8])
t4 = feed(sc4, dip_bounce, t4)
check("a position was opened on the bounce", sc4.trips >= 1 or
      sc4.positions.get("TEST", 0) > 0, True)
check("exactly one completed trip (entered on the bounce, exited at "
     "VWAP reversion)", sc4.trips, 1)
check("the trip was profitable (bought below VWAP, sold at/above it)",
      sc4.pnl_e4 > 0, True)

print("[G4] never re-enters while already in a position")
sc5 = VWAPBounceScorecard("t", symbol="TEST", band_k=1.0,
                          min_session_ticks=10)
t5 = datetime(2024, 1, 2, 14, 30, 0, tzinfo=timezone.utc)
t5 = feed(sc5, [100.0]*15 + [99.9, 99.8, 99.5, 99.0, 98.5, 99.2, 99.8], t5)
qty_after_entry = sc5.positions.get("TEST", 0)
check("position opened", qty_after_entry > 0, True)
# more sub-band dips and bounces while ALREADY holding must not add to
# the position (this strategy doesn't accumulate/pyramid)
t5 = feed(sc5, [97.0, 96.0, 95.0, 96.5, 98.0], t5)
check("position size unchanged while already in a trade",
      sc5.positions.get("TEST", 0), qty_after_entry)

# ---- G5: session boundary forces flat, and VWAP genuinely resets ---------
print("[G5] a new session resets VWAP AND force-closes any open position")
sc6 = VWAPBounceScorecard("t", symbol="TEST", band_k=1.0,
                          min_session_ticks=10)
day1 = datetime(2024, 1, 2, 14, 30, 0, tzinfo=timezone.utc)
day1 = feed(sc6, [100.0]*15 + [99.9, 99.8, 99.5, 99.0, 98.5, 99.2, 99.8], day1)
check("position open going into the session boundary",
      sc6.positions.get("TEST", 0) > 0, True)
trips_before = sc6.trips

day2 = datetime(2024, 1, 3, 14, 30, 0, tzinfo=timezone.utc)   # next day
sc6.on_tick(day2, to_e4(150.0), 100)     # first tick of the new session
check("position force-closed at the session boundary",
      sc6.positions.get("TEST", 0), 0)
check("the force-close counted as a real completed trip",
      sc6.trips, trips_before + 1)
check("forced_flat_count tracks this explicitly",
      sc6.forced_flat_count, 1)
check("VWAP genuinely reset for the new session (anchored near the "
     "new day's price, not dragged down by yesterday's dip)",
     sc6.vwap > to_e4(140.0), True)

# a session boundary with NO open position must be a quiet no-op
sc7 = VWAPBounceScorecard("t", symbol="TEST", min_session_ticks=100)
sc7.on_tick(day1, to_e4(100.0), 100)
sc7.on_tick(day2, to_e4(105.0), 100)
check("no forced-flat event when nothing was open", sc7.forced_flat_count, 0)
check("no phantom trip recorded", sc7.trips, 0)

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
