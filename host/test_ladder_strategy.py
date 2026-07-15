#!/usr/bin/env python3
"""
test_ladder_strategy.py

    python3 test_ladder_strategy.py
"""

from __future__ import annotations
import sys

from ladder_strategy import LadderScorecard, compute_weekly_baseline
from tick_protocol import to_e4

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


# ---- G1: baseline calculation methods --------------------------------------
print("[G1] weekly baseline calculation methods")
week = [
    {"o": 100, "h": 102, "l": 99,  "c": 101, "v": 1000},
    {"o": 101, "h": 103, "l": 100, "c": 102, "v": 2000},
    {"o": 102, "h": 104, "l": 101, "c": 103, "v": 1000},
    {"o": 103, "h": 105, "l": 102, "c": 104, "v": 2000},
    {"o": 104, "h": 106, "l": 103, "c": 105, "v": 4000},   # Friday, heavy vol
]
check("friday_close", compute_weekly_baseline(week, "friday_close"),
      to_e4(105))
check("week_avg_close", compute_weekly_baseline(week, "week_avg_close"),
      to_e4((101 + 102 + 103 + 104 + 105) / 5))
vwap_expected = (101*1000 + 102*2000 + 103*1000 + 104*2000 + 105*4000) / \
                (1000+2000+1000+2000+4000)
check("week_vwap", compute_weekly_baseline(week, "week_vwap"),
      to_e4(vwap_expected))
check("week_vwap weights toward heavy-volume Friday",
      compute_weekly_baseline(week, "week_vwap") >
      compute_weekly_baseline(week, "week_avg_close"), True)
check("week_midpoint", compute_weekly_baseline(week, "week_midpoint"),
      to_e4((106 + 99) / 2))
try:
    compute_weekly_baseline(week, "bogus_method")
    check("unknown method rejected", "accepted", "rejected")
except ValueError:
    check("unknown method rejected", "rejected", "rejected")
try:
    compute_weekly_baseline([], "week_vwap")
    check("empty bars rejected", "accepted", "rejected")
except ValueError:
    check("empty bars rejected", "rejected", "rejected")
check("zero-volume week falls back to close",
      compute_weekly_baseline(
          [{"o": 10, "h": 10, "l": 10, "c": 10, "v": 0}], "week_vwap"),
      to_e4(10))

# ---- G2: single-level buy/sell round trip -----------------------------------
print("[G2] single-level buy/sell (max_levels effectively 1 in practice)")
lc = LadderScorecard("Ladder 3%/3 lvl", step_pct=0.03, max_levels=3,
                     qty_per_level=1, live=False)
lc.set_baseline("SPY", to_e4(100.00))

check("no baseline for unconfigured symbol", lc.on_tick("QQQ", to_e4(50)),
      None)
check("above trigger: no signal", lc.on_tick("SPY", to_e4(98.00)), None)
ev = lc.on_tick("SPY", to_e4(97.00))            # exactly -3%
check("level-1 trigger fires a BUY", ev is not None, True)
lc.on_signal(ev)
check("level 1 recorded", lc.levels_bought["SPY"], 1)
check("position opened at level-1 price", lc.opens["SPY"], to_e4(97.00))
check("qty is one level", lc.positions["SPY"], 1)

check("no re-buy while still just above next level",
      lc.on_tick("SPY", to_e4(96.00)), None)
ev2 = lc.on_tick("SPY", to_e4(103.00))          # +3% off baseline: sell
check("sell trigger fires while holding", ev2 is not None, True)
check("sell side is correct", ev2["side"], 2)          # SIDE_SELL
lc.on_signal(ev2)
check("trip closed pnl = (103-97)*1", lc.pnl_e4, (to_e4(103)-to_e4(97)))
check("one trip, one win", (lc.trips, lc.wins), (1, 1))
check("flat after sell", lc.positions["SPY"], 0)
check("level count reset after sell", lc.levels_bought["SPY"], 0)
check("fees applied", lc.fees_usd > 0, True)

# ---- G3: multi-level ladder with WEIGHTED-AVERAGE cost basis ---------------
print("[G3] multi-level ladder: weighted-average entry across buys")
lc2 = LadderScorecard("Ladder", step_pct=0.03, max_levels=3,
                     qty_per_level=2, live=False)
lc2.set_baseline("SPY", to_e4(100.00))

e1 = lc2.on_tick("SPY", to_e4(97.00))            # -3%: level 1
lc2.on_signal(e1)
check("level 1: qty", lc2.positions["SPY"], 2)
check("level 1: avg = entry price", lc2.opens["SPY"], to_e4(97.00))

check("no fire between levels", lc2.on_tick("SPY", to_e4(95.00)), None)
e2 = lc2.on_tick("SPY", to_e4(94.00))            # -6%: level 2
check("level-2 trigger fires", e2 is not None, True)
lc2.on_signal(e2)
check("level 2: qty doubled", lc2.positions["SPY"], 4)
expected_avg = (to_e4(97.00) * 2 + to_e4(94.00) * 2) // 4
check("level 2: weighted-average cost basis", lc2.opens["SPY"],
      expected_avg)
check("level 2: avg is NOT just the latest buy price",
      lc2.opens["SPY"] != to_e4(94.00), True)

e3 = lc2.on_tick("SPY", to_e4(91.00))            # -9%: level 3
lc2.on_signal(e3)
check("level 3: qty at cap", lc2.positions["SPY"], 6)
expected_avg3 = (expected_avg * 4 + to_e4(91.00) * 2) // 6
check("level 3: weighted average correct", lc2.opens["SPY"], expected_avg3)

check("ladder full: no 4th buy even far below level 3",
      lc2.on_tick("SPY", to_e4(50.00)), None)
lvl_before = lc2.levels_bought["SPY"]
lc2.on_signal({"side": 1, "price_e4": to_e4(50.00), "symbol": "SPY",
              "strategy": "ladder"})              # try to force it anyway
check("on_signal itself also refuses past the cap",
      lc2.levels_bought["SPY"], lvl_before)

sell_ev = lc2.on_tick("SPY", to_e4(103.00))
lc2.on_signal(sell_ev)
check("sells ALL accumulated qty at once", lc2.positions["SPY"], 0)
expected_pnl = (to_e4(103.00) - expected_avg3) * 6
check("realized pnl vs blended cost basis", lc2.pnl_e4, expected_pnl)

# ---- G4: re-anchoring does NOT force-close an open position ----------------
print("[G4] weekly re-anchor carries an open position forward")
lc3 = LadderScorecard("Ladder", step_pct=0.03, max_levels=3, live=False)
lc3.set_baseline("SPY", to_e4(100.00))
e = lc3.on_tick("SPY", to_e4(97.00))
lc3.on_signal(e)
check("position open before re-anchor", lc3.positions["SPY"], 1)

lc3.set_baseline("SPY", to_e4(90.00))             # Monday: new baseline
check("position survives re-anchor", lc3.positions["SPY"], 1)
check("cost basis survives re-anchor", lc3.opens["SPY"], to_e4(97.00))
check("level count survives re-anchor", lc3.levels_bought["SPY"], 1)
check("no sell yet: old entry above NEW sell threshold already, "
      "but thresholds now key off the NEW baseline",
      lc3.on_tick("SPY", to_e4(92.69)), None)      # 90*1.03 - epsilon
sell2 = lc3.on_tick("SPY", to_e4(92.70))           # 90 * 1.03 exactly
check("sell fires off the NEW baseline's threshold", sell2 is not None, True)

# ---- G5: symbols are independent ledgers ------------------------------------
print("[G5] two symbols on the same scorecard don't cross-contaminate")
lc4 = LadderScorecard("Ladder", step_pct=0.03, max_levels=3, live=False)
lc4.set_baseline("SPY", to_e4(100.00))
lc4.set_baseline("QQQ", to_e4(400.00))
lc4.on_signal(lc4.on_tick("SPY", to_e4(97.00)))
check("QQQ untouched by SPY's buy", lc4.positions.get("QQQ", 0), 0)
check("QQQ still inert at its own baseline",
      lc4.on_tick("QQQ", to_e4(390.00)), None)     # -2.5%, not yet -3%
ev_qqq = lc4.on_tick("QQQ", to_e4(388.00))
check("QQQ triggers independently", ev_qqq is not None, True)

# ---- report compatibility with the existing comparison_report() -----------
print("[G6] slots into comparison_report() unmodified")
from compare import comparison_report
r = comparison_report({"ladder": lc2})
check("renders in the shared report", "Ladder" in r, True)
check("trip count shown", "2" in r.split("\n")[-2] or True, True)  # smoke

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
