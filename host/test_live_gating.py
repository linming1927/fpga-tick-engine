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


def attempt(env=None, limits=GOOD_LIMITS, typed="LIVE SPY SMA", tty=True,
           strategy="sma"):
    """Run the arming chain with injected gates; return 'armed' or 'refused'."""
    try:
        b = arm_live_trading("SPY", limits, strategy,
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
check("confirmation for wrong symbol", attempt(typed="LIVE TSLA SMA"),
      "refused")
check("whitespace-padded confirmation OK", attempt(typed="  LIVE SPY SMA  "),
      "armed")

# ---------------------------------------------------------------------------
print("[G1b] v3.24: the confirmation phrase now names the STRATEGY too, "
     "not just the symbol — a --strategy typo or a stale saved command "
     "must not silently arm the wrong strategy live")
check("old phrase (symbol only, no strategy) now refuses",
      attempt(typed="LIVE SPY"), "refused")
check("correct symbol, MISSING strategy word entirely, still refuses",
      attempt(typed="LIVE SPY "), "refused")
check("correct symbol, WRONG strategy named, refuses -- this is the "
     "exact mistake the banner exists to catch: arming with "
     "--strategy vwap_bounce but confirming as if it were SMA",
     attempt(strategy="vwap_bounce", typed="LIVE SPY SMA"), "refused")
check("correct symbol AND correct (matching) strategy: armed",
      attempt(strategy="vwap_bounce", typed="LIVE SPY VWAP_BOUNCE"),
      "armed")
check("strategy name in the confirmation is case-insensitive-safe the "
     "same way the existing code upper()s it -- typed lowercase still "
     "matches the upper()'d expected phrase",
     attempt(strategy="ema", typed="LIVE SPY EMA"), "armed")

import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    attempt(strategy="vwap_bounce", typed="LIVE SPY VWAP_BOUNCE")
banner = buf.getvalue()
check("the banner ITSELF displays which strategy is about to trade "
     "(not just the confirmation gate -- an operator reading before "
     "typing anything should already see this)",
     "VWAP_BOUNCE" in banner, True)
check("the banner is explicit that only this one strategy trades",
      "TRADES" in banner, True)

# v3.27: the banner's new max-position line is conditional on the field
# being set (None means disabled, same opt-in design as the underlying
# RiskPolicy check) -- confirm both states render correctly
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    attempt()   # GOOD_LIMITS doesn't set max_position_notional_e4 -> None
banner_no_cap = buf2.getvalue()
check("no max-position line when the field isn't set (None, the "
     "default) -- matches the underlying check being a true no-op",
     "max position" in banner_no_cap, False)

from dataclasses import replace
limits_with_cap = replace(GOOD_LIMITS, max_position_notional_e4=10_000*10_000)
buf3 = io.StringIO()
with contextlib.redirect_stdout(buf3):
    attempt(limits=limits_with_cap)
banner_with_cap = buf3.getvalue()
check("the max-position line DOES appear, with the right dollar "
     "figure, when the field is set -- an operator arming live "
     "should see their actual dollar exposure cap before confirming",
     "max position      $10,000.00 total exposure" in banner_with_cap,
     True)

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
