#!/usr/bin/env python3
"""
test_live_gating.py — every live interlock must refuse INDEPENDENTLY.

    python3 test_live_gating.py

The philosophy: a live session should only be reachable when all six gates
pass, so the tests knock out one gate at a time with everything else valid
and assert refusal. Then the daily loss halt is exercised with a MockBroker
losing sequence. No network is touched anywhere.
"""

from __future__ import annotations
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tick_protocol import SIDE_BUY, SIDE_SELL
from order_manager import (arm_live_trading, AlpacaLiveBroker,
                           LIVE_ACK_PHRASE, LIVE_URL, RiskLimits,
                           OrderManager, MockBroker)

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


GOOD_ENV = {"ALPACA_LIVE_KEY": "k", "ALPACA_LIVE_SECRET": "s",
            "ALPACA_LIVE_ACK": LIVE_ACK_PHRASE}
GOOD_LIMITS = RiskLimits(order_qty=1, max_shares=1,
                         max_notional_e4=1_000 * 10_000,
                         max_orders_per_day=5, cooldown_s=60,
                         max_daily_loss=100.0)


def attempt(env=None, limits=GOOD_LIMITS, typed="LIVE SPY", tty=True):
    """Run the arming chain with injected gates; return 'armed' or 'refused'."""
    try:
        b = arm_live_trading("SPY", limits,
                             env=GOOD_ENV if env is None else env,
                             input_fn=lambda prompt: typed,
                             isatty=lambda: tty)
        return "armed" if isinstance(b, AlpacaLiveBroker) else "?"
    except SystemExit:
        return "refused"


# ---------------------------------------------------------------------------
print("\n[G1] every gate refuses independently")
os.environ["ALPACA_LIVE_ACK"] = LIVE_ACK_PHRASE   # class gate for happy path
check("all gates pass -> armed", attempt(), "armed")
check("missing live key", attempt(env={"ALPACA_LIVE_SECRET": "s",
      "ALPACA_LIVE_ACK": LIVE_ACK_PHRASE}), "refused")
check("missing live secret", attempt(env={"ALPACA_LIVE_KEY": "k",
      "ALPACA_LIVE_ACK": LIVE_ACK_PHRASE}), "refused")
check("missing ack phrase", attempt(env={"ALPACA_LIVE_KEY": "k",
      "ALPACA_LIVE_SECRET": "s"}), "refused")
check("wrong ack phrase", attempt(env={"ALPACA_LIVE_KEY": "k",
      "ALPACA_LIVE_SECRET": "s", "ALPACA_LIVE_ACK": "yes"}), "refused")
check("no daily loss limit", attempt(limits=RiskLimits(
      max_daily_loss=None)), "refused")
check("zero daily loss limit", attempt(limits=RiskLimits(
      max_daily_loss=0.0)), "refused")
check("not a tty", attempt(tty=False), "refused")
check("wrong confirmation text", attempt(typed="live spy"), "refused")
check("empty confirmation", attempt(typed=""), "refused")
check("confirmation for wrong symbol", attempt(typed="LIVE TSLA"), "refused")
check("whitespace-padded confirmation OK", attempt(typed="  LIVE SPY  "),
      "armed")

print("[G2] paper credentials can never arm live")
check("paper env vars alone refuse", attempt(env={
      "ALPACA_KEY": "k", "ALPACA_SECRET": "s",
      "ALPACA_LIVE_ACK": LIVE_ACK_PHRASE}), "refused")

print("[G3] the broker class itself re-checks the ack (defense in depth)")
del os.environ["ALPACA_LIVE_ACK"]
try:
    AlpacaLiveBroker("k", "s")
    check("class gate without ack", "constructed", "refused")
except ValueError:
    check("class gate without ack", "refused", "refused")
os.environ["ALPACA_LIVE_ACK"] = LIVE_ACK_PHRASE
b = AlpacaLiveBroker("k", "s")
check("class gate with ack, live URL pinned", b.base, LIVE_URL)
del os.environ["ALPACA_LIVE_ACK"]

# ---------------------------------------------------------------------------
print("[G4] daily loss halt")
d = tempfile.mkdtemp()
om = OrderManager(MockBroker(), "SPY",
                  RiskLimits(order_qty=10, max_shares=10,
                             max_notional_e4=10**13, max_orders_per_day=99,
                             cooldown_s=0.0, require_market_hours=False,
                             max_daily_loss=50.0),
                  audit_path=os.path.join(d, "a.jsonl"),
                  killfile=os.path.join(d, "om.kill"))


def sig(side, price_e4):
    return {"side": side, "price_e4": price_e4, "sma_fast": 0,
            "sma_slow": 0, "symbol": "SPY ", "fpga_ts": 0}


om.on_signal(sig(SIDE_BUY, 1_000_000))    # buy 10 @ $100
om.on_signal(sig(SIDE_SELL, 970_000))     # sell 10 @ $97 -> -$30 realized
check("under limit: still armed", om.halted, False)
om.on_signal(sig(SIDE_BUY, 1_000_000))    # buy 10 @ $100
om.on_signal(sig(SIDE_SELL, 970_000))     # -$30 more -> -$60 total
check("breach: halted", om.halted, True)
check("halt reason names the limit",
      "daily loss limit" in om.halt_reason, True)
check("kill marker written", os.path.exists(os.path.join(d, "om.kill")),
      True)
om.on_signal(sig(SIDE_BUY, 500_000))
check("no orders after loss halt", om.orders, 4)

# a WIN never trips it
d2 = tempfile.mkdtemp()
om2 = OrderManager(MockBroker(), "SPY",
                   RiskLimits(order_qty=10, max_shares=10,
                              max_notional_e4=10**13, max_orders_per_day=99,
                              cooldown_s=0.0, require_market_hours=False,
                              max_daily_loss=50.0),
                   audit_path=os.path.join(d2, "a.jsonl"),
                   killfile=os.path.join(d2, "om.kill"))
om2.on_signal(sig(SIDE_BUY, 1_000_000))
om2.on_signal(sig(SIDE_SELL, 1_100_000))  # +$100
check("profit never halts", om2.halted, False)

# ---------------------------------------------------------------------------
print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
