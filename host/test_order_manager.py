#!/usr/bin/env python3
"""
test_order_manager.py — the guardrails are the product; test the refusals.

    python3 test_order_manager.py

  G1  RiskPolicy pure-function checks: pyramiding, position ceiling,
      notional cap, cooldown, daily cap, long-only sell-when-flat
  G2  Kill switch: latches on divergence, blocks everything after,
      writes the marker file, and a new session REFUSES to start
      while the marker exists
  G3  Broker rejection escalation: 3 consecutive rejections trip the kill
  G4  Full chain: FPGAEmulator -> Bridge -> verified signals ->
      OrderManager -> MockBroker; fills alternate BUY/SELL, position
      stays within limits, audit log accounts for every signal
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from order_manager import ET
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tick_protocol import SIDE_BUY, SIDE_SELL
from order_manager import (RiskLimits, RiskPolicy, OrderManager,
                           MockBroker)

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


def tmp(name):
    return os.path.join(tempfile.mkdtemp(), name)


def sig(side, price_e4=1_000_000, symbol="SPY"):
    return {"side": side, "price_e4": price_e4, "sma_fast": 0, "sma_slow": 0,
            "symbol": symbol, "fpga_ts": 0, "strategy": "sma"}


LIM = dict(order_qty=2, max_shares=4, max_notional_e4=5_000_000,
           max_orders_per_day=3, cooldown_s=0.2, require_market_hours=False)

# ---------------------------------------------------------------------------
print("\n[G1] RiskPolicy refusals")
pol = RiskPolicy(RiskLimits(**LIM))
check("buy when flat allowed", pol.evaluate(SIDE_BUY, 0, 1_000_000)[0], True)
check("buy while already holding, still under max_shares, is now "
     "ALLOWED (pyramiding is intentional: buys accumulate up to "
     "max_shares, they're no longer refused just for holding a "
     "position at all)", pol.evaluate(SIDE_BUY, 2, 1_000_000)[:2],
     (True, "ok"))
check("buy correctly blocked once it would exceed max_shares",
      pol.evaluate(SIDE_BUY, 4, 1_000_000)[:2],
      (False, f"would exceed max_shares ({LIM['max_shares']})"))
check("sell when flat blocked", pol.evaluate(SIDE_SELL, 0, 1_000_000)[0], False)
check("sell closes the FULL accumulated position, not just one lot",
      pol.evaluate(SIDE_SELL, 4, 1_000_000)[2], 4)
check("notional cap blocks",
      pol.evaluate(SIDE_BUY, 0, 3_000_000)[0], False)   # 2 x $300 > $500
pol9 = RiskPolicy(RiskLimits(**{**LIM, "order_qty": 9}))
check("max_shares still blocks a single oversized order from flat",
      pol9.evaluate(SIDE_BUY, 0, 1)[0], False)

pol.record_order()
check("cooldown blocks immediately",
      pol.evaluate(SIDE_BUY, 0, 1_000_000)[0], False)
time.sleep(0.25)
check("cooldown expires", pol.evaluate(SIDE_BUY, 0, 1_000_000)[0], True)

pol2 = RiskPolicy(RiskLimits(**{**LIM, "cooldown_s": 0.0}))
for _ in range(3):
    pol2.record_order()
check("daily cap blocks 4th order",
      pol2.evaluate(SIDE_BUY, 0, 1_000_000)[:2][1],
      "daily order cap (3) reached")

# ---------------------------------------------------------------------------
print("[G2] kill switch latches and requires human re-arm")
kf = tmp("om.kill")
om = OrderManager(MockBroker(), "SPY", RiskLimits(**LIM),
                  audit_path=tmp("a.jsonl"), killfile=kf)
om.on_signal(sig(SIDE_BUY))
check("order filled before kill", om.orders, 1)
om.on_divergence({"reason": "SMA mismatch"})
check("halted", om.halted, True)
check("marker file written", os.path.exists(kf), True)
time.sleep(0.25)
om.on_signal(sig(SIDE_SELL))
check("signals blocked after kill", om.orders, 1)
try:
    OrderManager(MockBroker(), "SPY", RiskLimits(**LIM),
                 audit_path=tmp("b.jsonl"), killfile=kf)
    check("restart refused while marker exists", "started", "refused")
except SystemExit:
    check("restart refused while marker exists", "refused", "refused")
os.remove(kf)
om2 = OrderManager(MockBroker(), "SPY", RiskLimits(**LIM),
                   audit_path=tmp("c.jsonl"), killfile=kf)
check("re-arms after human deletes marker", om2.halted, False)

# ---------------------------------------------------------------------------
print("[G3] repeated broker rejections trip the kill")
kf3 = tmp("om.kill")
om3 = OrderManager(MockBroker(reject_next=3), "SPY",
                   RiskLimits(**{**LIM, "cooldown_s": 0.0,
                                 "max_orders_per_day": 99}),
                   audit_path=tmp("d.jsonl"), killfile=kf3)
for _ in range(3):
    om3.on_signal(sig(SIDE_BUY))
check("halted after 3 rejections", om3.halted, True)
check("no fills happened", om3.orders, 0)

# ---------------------------------------------------------------------------
print("[G4] full chain: emulator -> bridge -> verified signal -> mock broker")
from fpga_emulator import FPGAEmulator
from bridge import Bridge

emu = FPGAEmulator(symbol="SPY ", fast_n=4, slow_n=8)
path = emu.start()
br = Bridge(path, "SPY", fast_n=4, slow_n=8)
kf4 = tmp("om.kill")
audit4 = tmp("audit.jsonl")
broker = MockBroker()
om4 = OrderManager(broker, "SPY",
                   RiskLimits(order_qty=1, max_shares=1,
                              max_notional_e4=10**12,
                              max_orders_per_day=99, cooldown_s=0.0,
                              require_market_hours=False),
                   audit_path=audit4, killfile=kf4)
br.on_verified = om4.on_signal          # bridge-level hooks: late-bound,
br.on_divergence = om4.on_divergence    # survive slot reconfiguration

rng = random.Random(7)                      # same walk as test_host G4
price = 1_500_000
for _ in range(120):
    price = max(100_000, price + rng.randint(-60_000, 60_000))
    br.send_trade(price, 1)
    br.pump(timeout=0.004)
br.pump(timeout=0.5)
time.sleep(0.2)
br.pump(timeout=0.2)

check("no divergence, kill still armed", om4.halted, False)
check("signals reached the OM",
      om4.orders + om4.blocked,
      sum(v.verified for v in br.verifiers.values()))
check("fills alternate: position is 0 or 1", om4.position_qty in (0, 1), True)
# v2: per-symbol position isolation
d5 = tempfile.mkdtemp()
om5 = OrderManager(MockBroker(), ["SPY", "QQQ"],
                   RiskLimits(order_qty=1, max_shares=1,
                              max_notional_e4=10**12, max_orders_per_day=99,
                              cooldown_s=0.0, require_market_hours=False),
                   audit_path=os.path.join(d5, "a.jsonl"),
                   killfile=os.path.join(d5, "om.kill"))
om5.on_signal(sig(SIDE_BUY, 1_000_000, "SPY"))
om5.on_signal(sig(SIDE_BUY, 2_000_000, "QQQ"))   # long-only PER SYMBOL
check("both symbols opened", om5.positions, {"SPY": 1, "QQQ": 1})
om5.on_signal(sig(SIDE_SELL, 1_100_000, "QQQ"))
check("QQQ closed, SPY untouched", om5.positions, {"SPY": 1, "QQQ": 0})
sides = [f["side"] for f in broker.fills]
check("first fill is a buy (long-only)", sides[0] if sides else None, "buy")
check("no two consecutive same-side fills",
      any(a == b for a, b in zip(sides, sides[1:])), False)

with open(audit4) as f:
    events = [json.loads(line)["event"] for line in f]
check("audit has startup", "startup" in events, True)
check("audit fills match broker", events.count("order_filled"),
      len(broker.fills))
check("audit records the refusals too", events.count("blocked"), om4.blocked)
print(f"       {sum(v.verified for v in br.verifiers.values())} verified signals -> "
      f"{om4.orders} fills, {om4.blocked} blocked "
      f"(SELL-when-flat before first cross, etc.)")

om4.summary()
br.close()
emu.stop()

# ---------------------------------------------------------------------------
# ---- v2.6: sync_live_card keeps the dashboard's view CONTINUOUSLY
# fresh, not just at session shutdown -- the actual reported bug: real
# fills were happening, but the strategy comparison panel showed
# 0 trips/0 wins/net $0 for the whole session because the live card
# was only ever synced from om.costs ONCE, at the very end. --------
print("[G6] sync_live_card reflects reality immediately after EACH "
     "fill, not only when the session ends")
from order_manager import sync_live_card
from compare import StrategyScorecard, comparison_report

d6 = tempfile.mkdtemp()
om6 = OrderManager(MockBroker(), ["SPY", "QQQ"],
                   RiskLimits(order_qty=1, max_shares=5,
                              max_notional_e4=10**13, max_orders_per_day=99,
                              cooldown_s=0.0, require_market_hours=False),
                   audit_path=os.path.join(d6, "a.jsonl"),
                   killfile=os.path.join(d6, "om.kill"))
cards6 = {"sma": StrategyScorecard("SMA", live=True)}

# BEFORE any fills: the card should still show the pre-fill truth (zero)
sync_live_card(cards6, "sma", om6)
check("before any fills: trips is 0", cards6["sma"].trips, 0)
check("before any fills: positions show all configured symbols at 0",
      cards6["sma"].positions, {"SPY": 0, "QQQ": 0})

# fill #1: open SPY -- sync must reflect this IMMEDIATELY, mid-session,
# not just at the end
om6.on_signal(sig(SIDE_BUY, 1_000_000, "SPY"))
sync_live_card(cards6, "sma", om6)
check("after fill #1: position reflects the real OM state",
      cards6["sma"].positions.get("SPY"), 1)
check("after fill #1: still 0 trips (position is OPEN, not closed yet)",
      cards6["sma"].trips, 0)

# fill #2: open QQQ too -- exactly the user's reported scenario (two
# open positions, no closes yet)
om6.on_signal(sig(SIDE_BUY, 2_000_000, "QQQ"))
sync_live_card(cards6, "sma", om6)
check("two open positions reflected correctly",
      cards6["sma"].positions, {"SPY": 1, "QQQ": 1})
check("0 trips is CORRECT here (nothing has closed) -- the bug wasn't "
     "that 0 trips could ever be shown, it's that stale zeros were "
     "shown even AFTER real closes happened", cards6["sma"].trips, 0)

# fill #3: CLOSE spy -- a real trip completes; sync must show it
om6.on_signal(sig(SIDE_SELL, 1_100_000, "SPY"))
sync_live_card(cards6, "sma", om6)
check("after a real close: trips is now 1, not stuck at 0",
      cards6["sma"].trips, 1)
check("after a real close: net P&L reflects the REAL realized gain",
      cards6["sma"].pnl_e4, om6.costs.realized_pnl_e4)
check("after a real close: net P&L is nonzero (matches the reported "
     "symptom: dashboard showed $0 despite real realized P&L)",
      cards6["sma"].pnl_e4 != 0, True)
check("QQQ position untouched by SPY's close",
      cards6["sma"].positions.get("QQQ"), 1)

# ---- v2.7: today's REAL trading history survives a restart --------------
# (NET P&L and the daily order cap both used to silently reset to zero
# on every restart, even mid-day, even though positions correctly
# reconcile from the broker -- a real reported issue)
print("[G7] a restart resumes today's cumulative P&L and order count "
     "instead of resetting to zero")
from order_manager import _load_fills_split_by_today
import json as _json

d7 = tempfile.mkdtemp()
audit7 = os.path.join(d7, "a.jsonl")
tight7 = RiskLimits(order_qty=1, max_shares=5, max_notional_e4=10**13,
                   max_orders_per_day=10, cooldown_s=0.0,
                   require_market_hours=False)

# "session 1": open SPY, close it for a real realized gain, then the
# process would normally exit here (Ctrl+C) -- we just stop using this
# instance, matching what actually happens at shutdown
om7a = OrderManager(MockBroker(), ["SPY"], tight7, audit_path=audit7,
                    killfile=os.path.join(d7, "om.kill"))
om7a.on_signal(sig(SIDE_BUY, 1_000_000, "SPY"))
om7a.on_signal(sig(SIDE_SELL, 1_100_000, "SPY"))
session1_pnl = om7a.costs.net_pnl_usd
check("session 1 realized a real gain", session1_pnl > 0, True)
check("session 1 recorded 2 orders", om7a.policy.orders_today, 2)

# "session 2": a fresh OrderManager, SAME audit path -- simulating a
# restart (e.g. to deploy a fix) later the same day
om7b = OrderManager(MockBroker(), ["SPY"], tight7, audit_path=audit7,
                    killfile=os.path.join(d7, "om2.kill"))
check("restart: net P&L resumed, not reset to $0",
      abs(om7b.costs.net_pnl_usd - session1_pnl) < 0.01, True)
check("restart: daily order count resumed (2 from session 1)",
      om7b.policy.orders_today, 2)
check("restart: cooldown timer restored (not 0 -- a fresh 0 would let "
     "an immediate order through even inside the real cooldown window)",
     om7b.policy.last_order_t > 0, True)

# a 3rd order right after "restart" must still respect the ORIGINAL
# day's order count toward the cap, not start over from 0
cooldown_test = RiskLimits(order_qty=1, max_shares=5,
                           max_notional_e4=10**13, max_orders_per_day=2,
                           cooldown_s=0.0, require_market_hours=False)
om7c = OrderManager(MockBroker(), ["SPY"], cooldown_test, audit_path=audit7,
                    killfile=os.path.join(d7, "om3.kill"))
check("restart correctly inherits an ALREADY-exhausted daily cap "
     "(session 1 used 2/2 -- a new session must not get a fresh 2)",
     om7c.policy.orders_today, 2)
om7c.on_signal(sig(SIDE_BUY, 1_000_000, "SPY"))
check("further order today correctly blocked by the cap carried over "
     "from before the restart", om7c.blocked, 1)

# ---- new calendar day: must NOT inherit yesterday's history ---------------
print("[G8] a new day starts fresh, even with a non-empty audit log "
     "from a PRIOR day")
d8 = tempfile.mkdtemp()
audit8 = os.path.join(d8, "a.jsonl")
yesterday_us = int((datetime.now(ET) - timedelta(days=1)).timestamp()
                   * 1_000_000)
with open(audit8, "w") as f:
    f.write(_json.dumps({"t": yesterday_us, "event": "order_filled",
                        "symbol": "SPY", "side": "buy", "qty": 1,
                        "fill_price_e4": 1_000_000}) + "\n")
    f.write(_json.dumps({"t": yesterday_us + 1_000_000,
                        "event": "order_filled", "symbol": "SPY",
                        "side": "sell", "qty": 1,
                        "fill_price_e4": 2_000_000}) + "\n")
prior8, today_only8 = _load_fills_split_by_today(audit8)
check("yesterday's fills are NOT counted as today's", len(today_only8), 0)
check("but they ARE tracked as prior history for cost-basis "
     "reconstruction (a fully-closed prior position just nets to "
     "flat, which is exactly what should happen here)", len(prior8), 2)
om8 = OrderManager(MockBroker(), ["SPY"], tight7, audit_path=audit8,
                   killfile=os.path.join(d8, "om.kill"))
check("new day: P&L starts at $0 despite a non-empty audit log",
      om8.costs.net_pnl_usd, 0.0)
check("new day: order count starts at 0", om8.policy.orders_today, 0)

# ---- a corrupted line doesn't crash startup -------------------------------
print("[G9] a corrupted audit line is skipped, not fatal")
d9 = tempfile.mkdtemp()
audit9 = os.path.join(d9, "a.jsonl")
now_valid = int(datetime.now(ET).timestamp() * 1_000_000)
with open(audit9, "w") as f:
    f.write(_json.dumps({"t": now_valid, "event": "order_filled",
                        "symbol": "SPY", "side": "buy", "qty": 1,
                        "fill_price_e4": 1_000_000}) + "\n")
    f.write("{ this is not valid json at all\n")           # corrupted line
    f.write(_json.dumps({"t": now_valid + 1, "event": "order_filled",
                        "symbol": "SPY", "side": "sell", "qty": 1,
                        "fill_price_e4": 1_200_000}) + "\n")
om9 = OrderManager(MockBroker(), ["SPY"], tight7, audit_path=audit9,
                   killfile=os.path.join(d9, "om.kill"))
check("both valid fills survive around a corrupted line",
      om9.policy.orders_today, 2)
check("startup did not crash on the corrupted line", om9.costs.net_pnl_usd > 0,
      True)

# ---- v2.8: sync_live_card now reports a REAL win count, not None -----
print("[G10] the dashboard's SMA win column is a real number, not a dash")
d10 = tempfile.mkdtemp()
om10 = OrderManager(MockBroker(), ["SPY"],
                    RiskLimits(order_qty=1, max_shares=5,
                              max_notional_e4=10**13, max_orders_per_day=99,
                              cooldown_s=0.0, require_market_hours=False),
                    audit_path=os.path.join(d10, "a.jsonl"),
                    killfile=os.path.join(d10, "om.kill"))
cards10 = {"sma": StrategyScorecard("SMA", live=True)}
sync_live_card(cards10, "sma", om10)
check("before any trips: wins is 0, not None", cards10["sma"].wins, 0)
om10.on_signal(sig(SIDE_BUY, 1_000_000, "SPY"))
om10.on_signal(sig(SIDE_SELL, 1_100_000, "SPY"))    # a real win
sync_live_card(cards10, "sma", om10)
check("after a real win: wins is 1, a real number", cards10["sma"].wins, 1)
r10 = comparison_report(cards10)
check("report shows a percentage, not a dash, for the live row",
      "100%" in r10, True)

# ---- v2.9: THE REPORTED BUG -- a position bought YESTERDAY and sold
# TODAY must be priced against its real prior-day entry, not $0 -------
print("[G11] a position carried overnight is priced correctly on "
     "today's close (the exact reported scenario: QQQ bought "
     "yesterday, sold at today's open)")
from order_manager import _load_fills_split_by_today
import json as _json2

d11 = tempfile.mkdtemp()
audit11 = os.path.join(d11, "a.jsonl")
yesterday_us = int((datetime.now(ET) - timedelta(days=1)).timestamp()
                   * 1_000_000)
# yesterday: bought 1 share of QQQ around $700
with open(audit11, "w") as f:
    f.write(_json2.dumps({"t": yesterday_us, "event": "order_filled",
                         "symbol": "QQQ", "side": "buy", "qty": 1,
                         "fill_price_e4": 7_000_000}) + "\n")

prior, today = _load_fills_split_by_today(audit11)
check("yesterday's buy is classified as prior, not today", len(prior), 1)
check("nothing from today yet", len(today), 0)

limits11 = RiskLimits(order_qty=1, max_shares=5, max_notional_e4=10**13,
                      max_orders_per_day=99, cooldown_s=0.0,
                      require_market_hours=False)
broker11 = MockBroker()
broker11.positions["QQQ"] = 1   # the REAL broker already shows this share
                                # (audit log reconstructs the PRICE it was
                                # bought at; the broker is what confirms
                                # the position itself actually exists)
om11 = OrderManager(broker11, ["QQQ"], limits11, audit_path=audit11,
                    killfile=os.path.join(d11, "om.kill"))
check("cost basis carried forward from yesterday's buy",
      om11.costs._entries.get("QQQ"), [1, 7_000_000])
check("but NOTHING counted toward today's totals yet",
      (om11.costs.buys, om11.costs.sells), (0, 0))

# THIS MORNING: sell that same share at $723.18 (the exact price from
# the report) -- must realize the REAL ~$23.18 gain, not $723.18
om11.on_signal(sig(SIDE_SELL, 7_231_800, "QQQ"))
check("realized gain is the REAL difference vs yesterday's buy "
     "(not the full sale price treated as pure profit)",
     om11.costs.realized_pnl_e4, 7_231_800 - 7_000_000)
check("that's about $23.18, not $723.18",
      round(om11.costs.realized_pnl_usd, 2), 23.18)
check("today's sell count is 1 (only today's own activity)",
      om11.costs.sells, 1)

# ---- v3.1: buys accumulate up to max_shares (the actual reported
# scenario: RKLB refused a 2nd share even with max_shares=10) ---------------
print("[G12] real accumulation: 3 separate buys build a position, "
     "ONE sell closes it all at the correct weighted-average cost basis")
d12 = tempfile.mkdtemp()
om12 = OrderManager(MockBroker(), ["RKLB"],
                    RiskLimits(order_qty=1, max_shares=10,
                              max_notional_e4=10**13, max_orders_per_day=99,
                              cooldown_s=0.0, require_market_hours=False),
                    audit_path=os.path.join(d12, "a.jsonl"),
                    killfile=os.path.join(d12, "om.kill"))

om12.on_signal(sig(SIDE_BUY, 100_000, "RKLB"))   # buy #1 @ $10.00
check("1st buy allowed, position opens", om12.positions["RKLB"], 1)
om12.on_signal(sig(SIDE_BUY, 120_000, "RKLB"))   # buy #2 @ $12.00
check("2nd buy ALSO allowed (this is the exact reported bug: it used "
     "to refuse this unconditionally)", om12.positions["RKLB"], 2)
om12.on_signal(sig(SIDE_BUY, 140_000, "RKLB"))   # buy #3 @ $14.00
check("3rd buy allowed, still under max_shares=10",
      om12.positions["RKLB"], 3)
check("nothing blocked so far", om12.blocked, 0)

expected_avg = (100_000 + 120_000 + 140_000) // 3   # weighted average entry
check("cost basis correctly weighted across all 3 accumulated buys",
      om12.costs._entries["RKLB"], [3, expected_avg])

# one sell must close the ENTIRE accumulated position, not just 1 share
om12.on_signal(sig(SIDE_SELL, 150_000, "RKLB"))
check("sell closes the full accumulated position", om12.positions["RKLB"], 0)
check("realized P&L correctly uses the weighted-average cost basis "
     "across ALL 3 buys, not just the most recent one",
     om12.costs.realized_pnl_e4, (150_000 - expected_avg) * 3)
check("exactly one trip recorded (one sell = one closing trip, "
     "regardless of how many buys built the position)",
     om12.costs.sells, 1)

# the max_shares ceiling must still genuinely block once actually reached
om13 = OrderManager(MockBroker(), ["RKLB"],
                    RiskLimits(order_qty=1, max_shares=3,
                              max_notional_e4=10**13, max_orders_per_day=99,
                              cooldown_s=0.0, require_market_hours=False),
                    audit_path=os.path.join(d12, "b.jsonl"),
                    killfile=os.path.join(d12, "om2.kill"))
for _ in range(3):
    om13.on_signal(sig(SIDE_BUY, 100_000, "RKLB"))
check("filled up to max_shares (3)", om13.positions["RKLB"], 3)
om13.on_signal(sig(SIDE_BUY, 100_000, "RKLB"))     # 4th buy: must be refused
check("a 4th buy is correctly refused once max_shares is truly reached",
      om13.positions["RKLB"], 3)
check("blocked with the right reason", om13.blocked, 1)

# ---- v3.1: on_signal() reports what actually happened, for the GUI's
# new signals-table outcome column -----------------------------------------
print("[G13] on_signal returns a real outcome string, not just a "
     "side effect — this is what feeds the GUI's new outcome column")
d13 = tempfile.mkdtemp()
om13b = OrderManager(MockBroker(), ["SPY"],
                     RiskLimits(order_qty=1, max_shares=1,
                               max_notional_e4=10**13, max_orders_per_day=99,
                               cooldown_s=0.0, require_market_hours=False),
                     audit_path=os.path.join(d13, "a.jsonl"),
                     killfile=os.path.join(d13, "om.kill"))
check("a filled buy returns FILLED",
      om13b.on_signal(sig(SIDE_BUY, 1_000_000, "SPY")), "FILLED")
check("a blocked buy (max_shares reached) reports why",
      om13b.on_signal(sig(SIDE_BUY, 1_000_000, "SPY")).startswith("blocked:"),
      True)
check("a filled sell also returns FILLED",
      om13b.on_signal(sig(SIDE_SELL, 1_100_000, "SPY")), "FILLED")
check("a blocked sell (now flat) reports why",
      om13b.on_signal(sig(SIDE_SELL, 1_100_000, "SPY")).startswith("blocked:"),
      True)

from compare import StrategyScorecard
sc13 = StrategyScorecard("T", live=False)
check("ungated scorecard: a normal fill reports FILLED (scored)",
      sc13.on_signal({"side": SIDE_BUY, "price_e4": 1_000_000,
                      "symbol": "SPY", "strategy": "sma"}),
      "FILLED (scored)")
check("ungated scorecard: buy-while-open is reported as ignored, "
     "not silently swallowed",
     sc13.on_signal({"side": SIDE_BUY, "price_e4": 1_200_000,
                     "symbol": "SPY", "strategy": "sma"}),
     "ignored: already open")

# ---- v3.4: THE REPORTED BUG -- EMA and SMA-profit-gated must resume
# their trips/wins/net$ across a restart, not reset to zero, exactly
# like the LIVE row already does (v2.7) ------------------------------------
print("[G14] a real restart (two subprocess sessions, same audit file) "
     "resumes EMA and SMA-profit-gated's numbers instead of resetting them")
import subprocess, re
d14 = tempfile.mkdtemp()
audit14 = os.path.join(d14, "a.jsonl")

def run_session(n_ticks):
    emu = FPGAEmulator(symbol="SPY", fast_n=4, slow_n=8, ema_kf=1, ema_ks=3)
    port = emu.start()
    r = subprocess.run(
        [sys.executable, "order_manager.py", "--port", port,
         "--source", "sim", "--broker", "mock", "--cooldown", "0",
         "--n", str(n_ticks), "--rate", "50", "--fast", "4", "--slow", "8",
         "--ema-kf", "1", "--ema-ks", "3", "--profit-gate",
         "--audit", audit14],
        capture_output=True, text=True, timeout=90)
    emu.stop()
    return r.stdout

def parse_row(stdout, prefix):
    # "  EMA 1/2:1/8                   21      5   20%    ..."
    for line in stdout.splitlines():
        if line.strip().startswith(prefix):
            parts = line.split()
            # strategy name may contain no spaces after the leading label
            # in this test's fixed config (fast=4/slow=8, ema-kf=1/ks=3)
            trips = int(parts[-8]) if prefix == "SMA profit-gated" else None
            return line
    return None

out1 = run_session(300)
sma_1 = [l for l in out1.splitlines() if l.strip().startswith("SMA 4/8")][0]
ema_1 = [l for l in out1.splitlines() if l.strip().startswith("EMA")][0]
pg_1 = [l for l in out1.splitlines()
       if l.strip().startswith("SMA profit-gated")][0]

out2 = run_session(1)   # near-zero new activity: isolates the RESTORE
check("session 2 announces the restore for both shadow strategies",
      "restored" in out2 and "EMA" in out2 and "profit-gated" in out2,
      True)
sma_2 = [l for l in out2.splitlines() if l.strip().startswith("SMA 4/8")][0]
ema_2 = [l for l in out2.splitlines() if l.strip().startswith("EMA")][0]
pg_2 = [l for l in out2.splitlines()
       if l.strip().startswith("SMA profit-gated")][0]

def trips_win_net(line):
    # compare.py's row() left-justifies name+tag in a 24-char field
    # after 2 leading spaces (see StrategyScorecard.row()) — slicing
    # at that fixed boundary is robust regardless of the name's own
    # length ("SMA 4/8 [LIVE]" vs "SMA profit-gated" vs "EMA 1/2:1/8"),
    # unlike token-counting from the end, which shifts with variable
    # trailing text like "(N blocked)"/"(N gated)".
    fields = line[26:].split()
    return fields[1], fields[2], fields[5]   # trips, win, net$

check("SMA (live) trips/win/net survive the restart (already fixed "
     "in v2.7 -- confirms this test's own methodology is sound)",
     trips_win_net(sma_1), trips_win_net(sma_2))
check("EMA's trips/win/net RESUME after restart instead of resetting "
     "to zero -- the actual reported bug",
     trips_win_net(ema_1), trips_win_net(ema_2))
check("SMA profit-gated's trips/win/net ALSO resume correctly",
      trips_win_net(pg_1), trips_win_net(pg_2))

# ---- v3.11: halt() persists rich divergence detail, not just the "reason"
# one-liner -- previously that detail existed in memory for one moment and
# was then gone, making a real incident hard to reconstruct after the fact
print("[G15] halt() persists extra diagnostic fields to the audit log, "
     "not just the short reason string")
d15 = tempfile.mkdtemp()
om15 = OrderManager(MockBroker(), ["SPY"],
                    RiskLimits(order_qty=1, max_shares=5,
                              max_notional_e4=10**13, max_orders_per_day=99,
                              cooldown_s=0.0, require_market_hours=False),
                    audit_path=os.path.join(d15, "a.jsonl"),
                    killfile=os.path.join(d15, "om.kill"))

# on_divergence() is the actual real-world call path (from the bridge's
# SignalVerifier) -- exercise that directly, not just halt() in isolation
om15.on_divergence({
    "reason": "orphan FPGA signal", "symbol": "RKLB", "strategy": "sma",
    "waited_s": 2.13, "echoes_elapsed": 47,
    "side": 1, "price_e4": 1_665_000, "sma_fast": 10, "sma_slow": 20,
})
check("halted", om15.halted, True)
check("the concise reason still reads as before (backward compatible "
     "with anything that reads the killfile's plain text)",
     om15.halt_reason, "model/hardware divergence: orphan FPGA signal")
with open(om15.killfile) as f:
    killfile_text = f.read()
check("killfile's own content is unchanged -- still just the reason, "
     "one line", "orphan FPGA signal" in killfile_text, True)

# now check the AUDIT LOG actually has the rich detail persisted
with open(os.path.join(d15, "a.jsonl")) as f:
    audit_lines = [json.loads(l) for l in f if l.strip()]
kill_events = [e for e in audit_lines if e.get("event") == "KILL"]
check("exactly one KILL event in the audit log", len(kill_events), 1)
kill_ev = kill_events[0]
check("symbol persisted", kill_ev.get("symbol"), "RKLB")
check("strategy persisted", kill_ev.get("strategy"), "sma")
check("waited_s persisted -- this is the whole point: a real incident "
     "can now be reconstructed from the audit log alone, instead of "
     "having to guess after the fact", kill_ev.get("waited_s"), 2.13)
check("echoes_elapsed persisted", kill_ev.get("echoes_elapsed"), 47)
check("the actual signal contents (side/price/sma_fast/sma_slow) "
     "persisted, not just that something diverged",
     (kill_ev.get("side"), kill_ev.get("price_e4"), kill_ev.get("sma_fast"),
      kill_ev.get("sma_slow")), (1, 1_665_000, 10, 20))

# backward compatibility: a plain halt(reason) with NO extra fields
# (e.g. the existing "N consecutive broker rejections" call site) must
# still work exactly as before
d15b = tempfile.mkdtemp()
om15b = OrderManager(MockBroker(), ["SPY"],
                     RiskLimits(order_qty=1, max_shares=5,
                               max_notional_e4=10**13, max_orders_per_day=99,
                               cooldown_s=0.0, require_market_hours=False),
                     audit_path=os.path.join(d15b, "a.jsonl"),
                     killfile=os.path.join(d15b, "om.kill"))
om15b.halt("3 consecutive broker rejections")
check("plain halt() with no extra fields still works unchanged",
      om15b.halt_reason, "3 consecutive broker rejections")
with open(os.path.join(d15b, "a.jsonl")) as f:
    audit_lines_b = [json.loads(l) for l in f if l.strip()]
kill_b = [e for e in audit_lines_b if e.get("event") == "KILL"][0]
check("no spurious extra fields appear when none were given",
      set(kill_b.keys()), {"t", "event", "reason"})

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
