#!/usr/bin/env python3
"""
test_position_risk.py — the host-side risk overlay, in isolation.

    python3 test_position_risk.py

Covers: stop-loss placement and triggering, the position-anchored
VWAP (separate from and unaffected by the session VWAP), the
same-day-vs-older sell gate, and risk-based position sizing. No
Bridge, no emulator, no serial — pure logic, tested directly against
a minimal fake VWAPMirror standing in for the real (already
separately verified) one.
"""

from __future__ import annotations
import os, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from position_risk import PositionRiskOverlay

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


class _FakeVWAPMirror:
    """Stands in for tick_protocol.VWAPMirror -- only the public sums
    the overlay actually reads (sum_v, sum_ppv, vwap) matter here; the
    real VWAPMirror's own fabric-verified math is tested elsewhere."""
    def __init__(self, vwap_e4, sigma_e4, sum_v=1000):
        self.vwap = vwap_e4
        self.sum_v = sum_v
        variance = sigma_e4 ** 2
        mean_sq = variance + vwap_e4 ** 2
        self.sum_ppv = int(mean_sq * sum_v)


TODAY = date(2026, 7, 23)
YESTERDAY = date(2026, 7, 22)

# ---------------------------------------------------------------------------
print("[G1] stop-loss: placed at N sigma below VWAP at position-open time, "
     "fixed afterward, triggers exactly at/below that level")

ov = PositionRiskOverlay(stop_sigma_mult=3.0, risk_dollars_per_trade=500.0)
mirror = _FakeVWAPMirror(vwap_e4=400_0000, sigma_e4=1_0000)   # $400 vwap, $1 sigma
ov.on_position_opened("SPY", TODAY, mirror)

check("stop is set at vwap - 3*sigma = $400 - $3 = $397",
      ov.stop_price_e4("SPY"), 397_0000)
check("not triggered above the stop", ov.stop_triggered("SPY", 398_0000),
      False)
check("triggered exactly AT the stop", ov.stop_triggered("SPY", 397_0000),
      True)
check("triggered below the stop", ov.stop_triggered("SPY", 390_0000), True)

# the stop must NOT move even if VWAP moves a lot afterward -- it's
# fixed at position-open time, not continuously recalculated
mirror.vwap = 350_0000   # VWAP crashed down hard after entry
check("stop stays FIXED at its original level despite VWAP moving "
     "afterward -- a stop that moved with VWAP would be far less "
     "protective exactly when it matters most",
     ov.stop_price_e4("SPY"), 397_0000)

check("flat symbol (never opened) has no stop",
      ov.stop_price_e4("QQQ"), None)
check("flat symbol never triggers", ov.stop_triggered("QQQ", 1), False)

ov.on_position_closed("SPY")
check("closing clears the stop", ov.stop_price_e4("SPY"), None)
check("closed position no longer triggers regardless of price",
      ov.stop_triggered("SPY", 1), False)

# ---------------------------------------------------------------------------
print("[G2] anchored VWAP: separate running Σ(p·v)/Σv per symbol, only "
     "while a position is open, unaffected by the session VWAP")

ov2 = PositionRiskOverlay()
mirror2 = _FakeVWAPMirror(vwap_e4=100_0000, sigma_e4=5000)
check("no anchored VWAP before any position ever opened",
      ov2.anchored_vwap_e4("TSLA"), None)

ov2.on_tick("TSLA", 100_0000, 10)   # flat -- must be ignored
check("ticks while flat are ignored (nothing anchored yet)",
      ov2.anchored_vwap_e4("TSLA"), None)

ov2.on_position_opened("TSLA", TODAY, mirror2)
check("anchored VWAP starts undefined right at the instant of opening "
     "(no ticks accumulated since the anchor yet)",
     ov2.anchored_vwap_e4("TSLA"), None)

ov2.on_tick("TSLA", 380_0000, 10)
ov2.on_tick("TSLA", 382_0000, 20)
check("anchored VWAP is the volume-weighted average of ticks SINCE "
     "the anchor -- (380*10 + 382*20) / 30 = 381.333...",
     ov2.anchored_vwap_e4("TSLA"), (380_0000 * 10 + 382_0000 * 20) // 30)

# a same-day scalp trade layered on top must NOT reset the anchor --
# only a full close does
ov2.on_tick("TSLA", 390_0000, 5)
check("more ticks continue accumulating into the SAME anchor, not a "
     "fresh one, as long as the position never went flat in between",
     ov2.anchored_vwap_e4("TSLA"),
     (380_0000 * 10 + 382_0000 * 20 + 390_0000 * 5) // 35)

ov2.on_position_closed("TSLA")
check("closing resets the anchored VWAP", ov2.anchored_vwap_e4("TSLA"), None)
ov2.on_tick("TSLA", 999_0000, 1)
check("ticks after closing (before a new position opens) are ignored",
      ov2.anchored_vwap_e4("TSLA"), None)

# reopening starts a genuinely FRESH anchor, unrelated to the closed one
ov2.on_position_opened("TSLA", TODAY, mirror2)
ov2.on_tick("TSLA", 200_0000, 1)
check("reopening starts a fresh anchor -- no memory of the previous, "
     "now-closed position's accumulated sums",
     ov2.anchored_vwap_e4("TSLA"), 200_0000)

# ---------------------------------------------------------------------------
print("[G3] the sell gate: same-day sells unconditionally, an older "
     "position needs price near/above its own anchored VWAP")

ov3 = PositionRiskOverlay(anchor_gate_tolerance=0.0)
mirror3 = _FakeVWAPMirror(vwap_e4=380_0000, sigma_e4=2000)

check("nothing tracked for this symbol at all -- sell is allowed "
     "(this overlay has no opinion on a symbol it's never seen)",
     ov3.sell_allowed("NFLX", 500_0000, TODAY), True)

ov3.on_position_opened("TSLA", TODAY, mirror3)
ov3.on_tick("TSLA", 320_0000, 10)
check("same-day position: sell allowed unconditionally, EVEN WELL "
     "BELOW its own anchored VWAP -- this is exactly the scalp "
     "behavior the strategy is supposed to keep doing for a fresh "
     "same-day entry",
     ov3.sell_allowed("TSLA", 300_0000, TODAY), True)

# now simulate this same position surviving into a NEW day (the
# anchor was set yesterday, still open)
ov4 = PositionRiskOverlay(anchor_gate_tolerance=0.0)
mirror4 = _FakeVWAPMirror(vwap_e4=380_0000, sigma_e4=2000)
ov4.on_position_opened("TSLA", YESTERDAY, mirror4)
ov4.on_tick("TSLA", 380_0000, 10)   # anchored vwap = $380 exactly
check("older position, price BELOW its own anchored VWAP: sell BLOCKED "
     "-- this is the exact fix for the incident (an unrelated same-day "
     "trade's exit signal must not sweep this position out while it's "
     "still well below a reasonable reference)",
     ov4.sell_allowed("TSLA", 325_0000, TODAY), False)
check("older position, price AT its own anchored VWAP: sell allowed",
      ov4.sell_allowed("TSLA", 380_0000, TODAY), True)
check("older position, price ABOVE its own anchored VWAP: sell allowed",
      ov4.sell_allowed("TSLA", 400_0000, TODAY), True)

# tolerance: allow exiting slightly below the anchored VWAP
ov5 = PositionRiskOverlay(anchor_gate_tolerance=0.01)   # 1%
mirror5 = _FakeVWAPMirror(vwap_e4=380_0000, sigma_e4=2000)
ov5.on_position_opened("TSLA", YESTERDAY, mirror5)
ov5.on_tick("TSLA", 380_0000, 10)   # anchored vwap = $380
check("with 1% tolerance, 0.5% below anchored VWAP is still allowed",
      ov5.sell_allowed("TSLA", 378_1000, TODAY), True)   # ~0.5% below
check("with 1% tolerance, 2% below anchored VWAP is still blocked",
      ov5.sell_allowed("TSLA", 372_4000, TODAY), False)   # ~2% below

ov6 = PositionRiskOverlay()
mirror6 = _FakeVWAPMirror(vwap_e4=380_0000, sigma_e4=2000)
ov6.on_position_opened("TSLA", YESTERDAY, mirror6)
check("older position, zero ticks since anchor (edge case): sell "
     "allowed, not blocked on an undefined anchored VWAP",
     ov6.sell_allowed("TSLA", 1, TODAY), True)

# ---------------------------------------------------------------------------
print("[G4] risk-based position sizing: $500 risk / distance to stop, "
     "floored to a whole share, minimum 1")

ov7 = PositionRiskOverlay(risk_dollars_per_trade=500.0)
check("$500 risk, $5/share to stop -> 100 shares",
      ov7.risk_sized_qty(entry_price_e4=400_0000, stop_price_e4=395_0000),
      100)
check("$500 risk, $1/share to stop -> 500 shares",
      ov7.risk_sized_qty(entry_price_e4=100_0000, stop_price_e4=99_0000),
      500)
check("floors rather than rounds -- $500 / $3.33/share = 150.15 -> 150",
      ov7.risk_sized_qty(entry_price_e4=400_0000, stop_price_e4=396_6700),
      150)
check("very wide stop (large risk-per-share) still sizes at least 1 "
     "share, never 0 -- a signal that fires but sizes to nothing "
     "would look exactly like a silent bug",
     ov7.risk_sized_qty(entry_price_e4=400_0000, stop_price_e4=1_0000)
     >= 1, True)
check("degenerate case -- entry AT or THROUGH the stop already "
     "(risk-per-share <= 0) -- sizes minimally rather than raising "
     "or going negative",
     ov7.risk_sized_qty(entry_price_e4=400_0000, stop_price_e4=400_0000),
     1)
check("entry below the stop (even more degenerate) -- same minimal "
     "handling, no crash",
     ov7.risk_sized_qty(entry_price_e4=390_0000, stop_price_e4=400_0000),
     1)

# ---------------------------------------------------------------------------
print("[G5] peek_stop_price_e4: matches on_position_opened's own "
     "computed stop exactly, without committing any state")

ov8 = PositionRiskOverlay(stop_sigma_mult=3.0)
mirror8 = _FakeVWAPMirror(vwap_e4=250_0000, sigma_e4=1500)
peeked = ov8.peek_stop_price_e4(mirror8)
check("peek matches what on_position_opened would actually commit",
      peeked, 250_0000 - int(3.0 * 1500))
check("peeking does NOT commit any state -- position still shows flat",
      ov8.stop_price_e4("SPY"), None)
ov8.on_position_opened("SPY", TODAY, mirror8)
check("the value actually committed matches what was peeked "
     "beforehand, for the same mirror state",
     ov8.stop_price_e4("SPY"), peeked)

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
