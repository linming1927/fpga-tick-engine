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
print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
