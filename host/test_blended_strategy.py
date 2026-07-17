#!/usr/bin/env python3
"""
test_blended_strategy.py — the max-hold forced exit (compare.py) and
the two-sleeve blend (blended_strategy.py).

    python3 test_blended_strategy.py
"""

from __future__ import annotations
import sys
from datetime import datetime, timedelta, timezone

from blended_strategy import (AccountExposureCap, BlendedScorecard,
                              SleevePolicy)
from compare import ProfitGatedScorecard
from order_manager import HistoricalClock, RiskLimits, RiskPolicy
from tick_protocol import SIDE_BUY, SIDE_SELL, to_e4

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


T0 = datetime(2024, 1, 2, 14, 30, 0, tzinfo=timezone.utc)


def sig(side, price, sym="TEST"):
    return {"side": side, "price_e4": to_e4(price), "symbol": sym,
            "strategy": "sma"}


def pg_card(max_hold_days, **lim_kw):
    """A profit-gated card with its own policy + historical clock,
    wired exactly as backtest.py wires the standalone sma_pg row."""
    clk = HistoricalClock()
    limits = RiskLimits(require_market_hours=False, cooldown_s=0.0,
                        **lim_kw)
    card = ProfitGatedScorecard(
        "pg", policy=RiskPolicy(limits, now_fn=clk),
        max_hold_days=max_hold_days)
    def feed(fr, t):
        clk.set(t)
        return card.on_signal(fr, t=t)
    return card, feed


# ---- G1: max-hold force-closes a loser (win rate is no longer 100%) --------
print("[G1] max-hold forced exit realizes a loss")
card, feed = pg_card(max_hold_days=5.0)
feed(sig(SIDE_BUY, 100.00), T0)
# underwater sell inside the window: still gated, the original rule
out = feed(sig(SIDE_SELL, 99.00), T0 + timedelta(days=2))
check("inside window still gated", out.startswith("gated"), True)
check("no trips yet", card.trips, 0)
# underwater sell AFTER the window: forced exit, at a loss
out = feed(sig(SIDE_SELL, 98.00), T0 + timedelta(days=6))
check("forced exit fires", "forced exit" in out, True)
check("trip realized", card.trips, 1)
check("counted as a LOSS", card.wins, 0)
check("forced_exits counter", card.forced_exits, 1)
check("position flat after", card.positions.get("TEST", 0), 0)
check("gross reflects the loss", card.pnl_e4, (to_e4(98) - to_e4(100)))
check("trip_log marks it forced", card.trip_log[0]["forced"], True)

# ---- G2: max_hold_days=None preserves the original behavior exactly --------
print("[G2] None disables the bound (original never-sell-at-loss)")
card, feed = pg_card(max_hold_days=None)
feed(sig(SIDE_BUY, 100.00), T0)
out = feed(sig(SIDE_SELL, 98.00), T0 + timedelta(days=400))
check("still gated after a year", out.startswith("gated"), True)
check("no trips", card.trips, 0)
check("still holding", card.positions.get("TEST", 0), 1)

# ---- G3: a profitable close inside the window still works as before --------
print("[G3] profitable sells unchanged by the bound")
card, feed = pg_card(max_hold_days=5.0)
feed(sig(SIDE_BUY, 100.00), T0)
out = feed(sig(SIDE_SELL, 101.00), T0 + timedelta(days=1))
check("profitable sell fills", out, "FILLED (scored)")
check("counted as a win", card.wins, 1)
check("not a forced exit", card.forced_exits, 0)

# ---- G4: averaging down does NOT reset the hold clock ----------------------
print("[G4] hold time measured from the FIRST lot since flat")
card, feed = pg_card(max_hold_days=5.0)
feed(sig(SIDE_BUY, 100.00), T0)
feed(sig(SIDE_BUY, 96.00), T0 + timedelta(days=4))   # add to the loser
out = feed(sig(SIDE_SELL, 95.00), T0 + timedelta(days=6))
check("forced despite recent add", "forced exit" in out, True)
check("whole position closed", card.positions.get("TEST", 0), 0)

# ---- G5: the forced exit respects the policy gate (cooldown defers it) -----
print("[G5] forced exit goes through the same RiskPolicy as any sell")
clk = HistoricalClock()
limits = RiskLimits(require_market_hours=False, cooldown_s=3600.0)
card = ProfitGatedScorecard("pg", policy=RiskPolicy(limits, now_fn=clk),
                            max_hold_days=5.0)
clk.set(T0); card.on_signal(sig(SIDE_BUY, 100.00), t=T0)
# expired, but only 60s since the buy: cooldown gates the forced exit
t1 = T0 + timedelta(days=6)
clk.set(t1)
# fake "60s after an order" by re-recording: buy at t1 - 60s
card.policy.last_order_t = (t1 - timedelta(seconds=60)).timestamp()
out = card.on_signal(sig(SIDE_SELL, 98.00), t=t1)
check("cooldown defers the forced exit", out.startswith("gated"), True)
check("still holding (retry next signal)", card.positions.get("TEST", 0), 1)
t2 = t1 + timedelta(seconds=7200)
clk.set(t2)
out = card.on_signal(sig(SIDE_SELL, 98.00), t=t2)
check("fires once cooldown clears", "forced exit" in out, True)

# ---- G6: account exposure cap blocks what per-sleeve limits would allow ----
print("[G6] account-level exposure cap across sleeves")
account = AccountExposureCap(cap_e4=to_e4(250.0))
lim = RiskLimits(require_market_hours=False, cooldown_s=0.0,
                 max_shares=10, max_notional_e4=to_e4(2000.0))
pol_a = SleevePolicy(RiskPolicy(lim, now_fn=HistoricalClock()), account)
pol_b = SleevePolicy(RiskPolicy(lim, now_fn=HistoricalClock()), account)
card_a = ProfitGatedScorecard("a", policy=pol_a)
card_b = ProfitGatedScorecard("b", policy=pol_b)
account.attach(card_a); account.attach(card_b)
pol_a._now_fn.set(T0); pol_b._now_fn.set(T0)
card_a.on_signal(sig(SIDE_BUY, 100.00, "AAA"), t=T0)
card_b.on_signal(sig(SIDE_BUY, 100.00, "BBB"), t=T0)
check("open notional across sleeves", account.open_notional_e4(),
      2 * to_e4(100.0))
out = card_b.on_signal(sig(SIDE_BUY, 100.00, "BBB"),
                       t=T0 + timedelta(seconds=61))
check("third $100 buy blocked by $250 cap", out.startswith("gated"), True)
check("cap named in the reason", "account exposure cap" in out, True)
# sells are never blocked by the cap
pol_a._now_fn.set(T0 + timedelta(seconds=120))
out = card_a.on_signal(sig(SIDE_SELL, 101.00, "AAA"),
                       t=T0 + timedelta(seconds=120))
check("sell allowed under cap pressure", out, "FILLED (scored)")

# ---- G7: sleeves gate independently (one's cooldown != the other's) --------
print("[G7] per-sleeve cooldowns are independent")
account = AccountExposureCap(cap_e4=to_e4(100000.0))
lim_cd = RiskLimits(require_market_hours=False, cooldown_s=3600.0,
                    max_shares=10, max_notional_e4=to_e4(2000.0))
clk_a, clk_b = HistoricalClock(), HistoricalClock()
card_a = ProfitGatedScorecard(
    "a", policy=SleevePolicy(RiskPolicy(lim_cd, now_fn=clk_a), account))
card_b = ProfitGatedScorecard(
    "b", policy=SleevePolicy(RiskPolicy(lim_cd, now_fn=clk_b), account))
account.attach(card_a); account.attach(card_b)
clk_a.set(T0); clk_b.set(T0)
card_a.on_signal(sig(SIDE_BUY, 100.00, "AAA"), t=T0)   # starts a's cooldown
t1 = T0 + timedelta(seconds=30)                        # inside a's cooldown
clk_a.set(t1); clk_b.set(t1)
out_a = card_a.on_signal(sig(SIDE_BUY, 100.00, "AAA"), t=t1)
out_b = card_b.on_signal(sig(SIDE_BUY, 100.00, "BBB"), t=t1)
check("sleeve a gated by its own cooldown",
      out_a.startswith("gated: cooldown"), True)
check("sleeve b unaffected", out_b, "FILLED (scored)")

# ---- G8: BlendedScorecard end-to-end: build, feed, aggregate ---------------
print("[G8] blend aggregation, unrealized mark, and drawdown")
base = RiskLimits(require_market_hours=False, cooldown_s=0.0)
blend = BlendedScorecard.build(
    symbol="TEST", base_limits=base,
    vwap_shares=6, vwap_notional_e4=to_e4(1300.0),
    pg_shares=4, pg_notional_e4=to_e4(700.0),
    account_cap_e4=to_e4(2000.0), band_k=1.0, max_hold_days=5.0,
    now_fn_factory=HistoricalClock)
# drive the PG sleeve: one full profitable round trip
blend.on_sma_signal(sig(SIDE_BUY, 100.00), t=T0)
blend.on_sma_signal(sig(SIDE_SELL, 102.00), t=T0 + timedelta(hours=1))
check("blend trips aggregate", blend.trips, 1)
check("blend gross aggregates", blend.pnl_e4, to_e4(102) - to_e4(100))
check("blend win rate real", blend.wins, 1)
# open a PG position and mark it against the last tick
blend.on_sma_signal(sig(SIDE_BUY, 100.00), t=T0 + timedelta(hours=2))
blend.on_tick(T0 + timedelta(hours=3), to_e4(99.00), 100)
check("unrealized mark on open lot",
      round(blend.unrealized_usd(), 2), -1.00)
check("blend shows 1 open", sum(1 for v in blend.positions.values() if v),
      1)
# force the loser closed -> equity dips below its peak -> drawdown > 0
blend.on_sma_signal(sig(SIDE_SELL, 98.00), t=T0 + timedelta(days=6))
check("forced exit inside the blend", blend.pg.forced_exits, 1)
check("drawdown reflects the dip", blend.max_drawdown_usd() > 0, True)
check("merged trip_log in close order",
      [tr["forced"] for tr in blend.trip_log], [False, True])
row = blend.row()
check("row has aggregate + 2 sleeve lines + blend note",
      len(row.split("\n")), 4)
check("row reports unrealized/drawdown/forced",
      "unrealized" in row and "drawdown" in row and "forced" in row, True)

# ---- G9: report functions render the blend without modification ------------
print("[G9] comparison_report / monthly_breakdown_report accept the blend")
from compare import comparison_report, monthly_breakdown_report
rep = comparison_report({"blend": blend})
check("comparison_report renders", "Blend (VWAP+SMA-PG)" in rep, True)
check("sleeve-prefixed gate reasons",
      "sma-pg: would realize a loss" in rep
      or "gated-away" not in rep, True)
mon = monthly_breakdown_report({"blend": blend})
check("monthly report renders", "2024-01" in mon, True)

print("=" * 46)
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print("=" * 46)
sys.exit(1 if FAIL else 0)
