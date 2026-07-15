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
check("no pyramiding", pol.evaluate(SIDE_BUY, 2, 1_000_000)[:2],
      (False, "already long (no pyramiding)"))
check("sell when flat blocked", pol.evaluate(SIDE_SELL, 0, 1_000_000)[0], False)
check("sell closes full position", pol.evaluate(SIDE_SELL, 2, 1_000_000)[2], 2)
check("notional cap blocks",
      pol.evaluate(SIDE_BUY, 0, 3_000_000)[0], False)   # 2 x $300 > $500
pol9 = RiskPolicy(RiskLimits(**{**LIM, "order_qty": 9}))
check("position ceiling blocks", pol9.evaluate(SIDE_BUY, 0, 1)[0], False)

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
from order_manager import _replay_todays_fills
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
replayed = _replay_todays_fills(audit8)
check("yesterday's fills are NOT replayed into today", len(replayed), 0)
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

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
