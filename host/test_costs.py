#!/usr/bin/env python3
"""
test_costs.py — fee and tax estimation checks.

    python3 test_costs.py

  G1  Fee schedule: SEC $20.60/M and TAF $0.000195/sh with the $9.79 cap,
      round-up-to-cent behavior, buys free
  G2  Federal bracket engine against two independently published anchors
      (single $200k -> ~$40,600; MFJ $200k -> ~$33,400)
  G3  Incremental gains tax: bracket spanning, NIIT threshold crossing,
      marginal vs effective, losses -> $0
  G4  CostTracker P&L: avg-entry accounting, fee accumulation, net P&L
"""

from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from costs import (FeeSchedule, CostTracker, federal_tax, estimate_gains_tax,
                   marginal_rate)

PASS = FAIL = 0


def check(name, got, exp, tol=0.0):
    global PASS, FAIL
    ok = (abs(got - exp) <= tol) if tol else (got == exp)
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r} (tol {tol})")


# ---------------------------------------------------------------------------
print("\n[G1] fee schedule")
fs = FeeSchedule()
f = fs.sell_fees(qty=100, notional_usd=50_000.0)      # 100 sh @ $500
check("SEC on $50k sale", f["sec"], 1.03)             # 50000/1e6*20.60=1.03
check("TAF 100 shares rounds up", f["taf"], 0.02)     # 0.0195 -> $0.02
check("sell total", f["total"], 1.05)
f = fs.sell_fees(qty=100_000, notional_usd=1_000_000.0)
check("TAF caps at $9.79", f["taf"], 9.79)            # 19.50 uncapped
check("SEC exactly $20.60 per $1M", f["sec"], 20.60)
f = fs.sell_fees(qty=1, notional_usd=100.0)
check("tiny sale still costs 2 cents min", f["total"], 0.02)  # SEC .01 + TAF .01

# ---------------------------------------------------------------------------
print("[G2] federal bracket engine vs published anchors")
check("single $200k taxable", federal_tax(200_000, "single"), 40_600, tol=100)
check("MFJ $200k taxable",    federal_tax(200_000, "mfj"),    33_400, tol=100)
check("zero income", federal_tax(0, "mfj"), 0.0)
check("top bracket marginal", marginal_rate(1_000_000, "mfj"), 0.37)
check("10%% bracket marginal", marginal_rate(10_000, "single"), 0.10)

# ---------------------------------------------------------------------------
print("[G3] incremental gains tax")
# gain entirely inside the 22% bracket (MFJ base 150k, +10k stays < 211,400)
t = estimate_gains_tax(150_000, 10_000, "mfj", state_rate_pct=4.40)
check("fed = 22%% of gain", t["federal"], 2_200.0, tol=0.01)
check("state = 4.4%%", t["state"], 440.0, tol=0.01)
check("no NIIT under threshold", t["niit"], 0.0)

# gain spanning the 22->24 boundary at 211,400: base 210k, gain 10k
t = estimate_gains_tax(210_000, 10_000, "mfj")
exp = 0.22 * 1_400 + 0.24 * 8_600
check("bracket-spanning federal", t["federal"], exp, tol=0.01)
check("marginal reported as 24%%", t["marginal_pct"], 24.0)

# NIIT: MFJ threshold 250k. base 245k, gain 20k -> 15k over threshold
t = estimate_gains_tax(245_000, 20_000, "mfj")
check("NIIT on portion over 250k", t["niit"], 0.038 * 15_000, tol=0.01)

# losses
t = estimate_gains_tax(150_000, -500.0, "mfj")
check("loss -> zero tax", t["total"], 0.0)
check("loss note mentions $3,000", "$3,000" in t["note"], True)

# gross-income mode subtracts the standard deduction
t_tax = estimate_gains_tax(100_000, 10_000, "mfj", income_is_gross=True)
check("gross mode taxable base", t_tax["taxable_base"], 100_000 - 32_200)

# ---------------------------------------------------------------------------
print("[G4] CostTracker P&L accounting")
ct = CostTracker()
check("buy returns no fees", ct.on_fill("buy", 2, 1_000_000, "SPY"), None)
fees = ct.on_fill("sell", 2, 1_100_000, "SPY")
check("sell returns fee dict", fees is not None, True)
check("realized P&L $20", ct.realized_pnl_usd, 20.0)                  # 2 x $10
check("fees accumulated", ct.total_fees > 0, True)
check("net = gross - fees", ct.net_pnl_usd, 20.0 - ct.total_fees)
check("position closed resets entry", ct._entries["SPY"][0], 0)
# averaging: two buys at different prices
ct2 = CostTracker()
ct2.on_fill("buy", 1, 1_000_000, "SPY")
ct2.on_fill("buy", 1, 2_000_000, "SPY")
ct2.on_fill("sell", 2, 1_500_000, "SPY")         # sell at exactly the average
check("avg-entry P&L is zero", ct2.realized_pnl_usd, 0.0)
# v2: entries are independent per symbol
ct3 = CostTracker()
ct3.on_fill("buy", 1, 1_000_000, "SPY")
ct3.on_fill("buy", 1, 5_000_000, "QQQ")
ct3.on_fill("sell", 1, 1_100_000, "SPY")     # +$10 vs SPY entry, not QQQ's
check("per-symbol entries isolated", ct3.realized_pnl_e4, 100_000)

# report renders with and without income
r = ct.report(None)
check("report hints at --household-income", "--household-income" in r, True)
r = ct.report(215_000, "mfj", 4.40)
check("report includes federal line", "federal" in r, True)
check("report includes disclaimer", "not tax advice" in r, True)

# ---------------------------------------------------------------------------
# ---- v2.8: per-trip win/loss tracking (this used to always show as a
# dash in the dashboard, since CostTracker never remembered it) ------------
print("[G] per-trip win tracking")
ct5 = CostTracker()
ct5.on_fill("buy", 1, 1_000_000, "SPY")
ct5.on_fill("sell", 1, 1_100_000, "SPY")     # win: +$10
check("first win counted", ct5.wins, 1)
check("win rate 100% after one win", ct5.win_rate_pct, 100.0)
ct5.on_fill("buy", 1, 1_000_000, "SPY")
ct5.on_fill("sell", 1, 900_000, "SPY")       # loss: -$10
check("wins NOT incremented on a losing trip", ct5.wins, 1)
check("win rate now 50% (1 win, 1 loss)", ct5.win_rate_pct, 50.0)
check("sells count matches trip count", ct5.sells, 2)
ct6 = CostTracker()
check("win_rate_pct is None before any trip closes (nothing to divide by)",
      ct6.win_rate_pct, None)
# exact tie (sell at cost basis) must NOT count as a win
ct7 = CostTracker()
ct7.on_fill("buy", 1, 1_000_000, "SPY")
ct7.on_fill("sell", 1, 1_000_000, "SPY")     # exactly flat
check("a flat (zero P&L) trip does not count as a win", ct7.wins, 0)

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
