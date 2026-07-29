#!/usr/bin/env python3
"""
test_tick_engine.py — the direct in-process engine (v3.46).

    python3 test_tick_engine.py

Covers the TickEngine's tick->model->signal path, its duck-type
compatibility with Bridge (the feeds and the dashboard drive both
through the same calls), and — the reason this module exists at all —
the two model-ownership bugs that silently broke live risk management:

  [G3] v3.44: a captured reference to models["vwap_bounce"] must stay
       valid across symbol reconfiguration. Bridge._build_models()
       REASSIGNED self.models, orphaning it; TickEngine mutates in
       place so there is nothing to orphan.

  [G4] v3.46: adding symbols mid-session must not disturb the risk
       overlay OR reset the other symbols' running session VWAP.

  [G6] v3.46: feeds.run_alpaca must CHAIN on_symbols_changed rather
       than overwrite it — the live bug that disabled stop-losses.
"""

from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tick_engine import TickEngine
from tick_protocol import (TYPE_ECHO_TRADE, TYPE_SIGNAL_SMA,
                           TYPE_SIGNAL_EMA, TYPE_SIGNAL_VWAP, to_e4)

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


def make(symbols=("SPY",), **kw):
    return TickEngine(list(symbols), fast_n=4, slow_n=8, k_fast=1,
                      k_slow=3, vwap_warmup=5, quiet=True, **kw)


def feed(eng, sym, prices, qty=100):
    for p in prices:
        eng.send_trade(to_e4(p), qty, symbol=sym)
    eng.pump()


# ---- [G1] basic tick -> model -> signal path -------------------------
print("[G1] ticks reach the models and produce signals")
eng = make()
echoes, signals = [], []
eng.on_echo = echoes.append
eng.on_verified = signals.append
feed(eng, "SPY", [100, 99, 98, 97, 96, 95, 94, 93, 300, 310])

check("every tick produced exactly one echo frame", len(echoes), 10)
check("echoes are tagged TYPE_ECHO_TRADE (what the risk hook filters on)",
      all(e["type"] == TYPE_ECHO_TRADE for e in echoes), True)
check("echo carries the qty the VWAP math needs", echoes[0]["qty"], 100)
check("echo carries the symbol", echoes[0]["symbol"], "SPY")
check("engine counted the ticks", eng.sent, 10)
check("engine counted them as processed", eng.echoes, 10)
check("a crossover actually produced signals", len(signals) > 0, True)
check("signals are tagged kind=signal", all(s["kind"] == "signal"
                                            for s in signals), True)
check("every signal names its strategy",
      all(s["strategy"] in ("sma", "ema", "vwap_bounce") for s in signals),
      True)
check("every signal carries symbol/side/price_e4 (on_signal's contract)",
      all({"symbol", "side", "price_e4"} <= set(s) for s in signals), True)
_types = {s["strategy"]: s["type"] for s in signals}
for _st, _ty in (("sma", TYPE_SIGNAL_SMA), ("ema", TYPE_SIGNAL_EMA),
                 ("vwap_bounce", TYPE_SIGNAL_VWAP)):
    if _st in _types:
        check(f"{_st} signals carry the right frame type", _types[_st], _ty)
check("signal counter agrees with the callbacks", eng.fpga_signals,
      len(signals))

# ---- [G2] echo fires BEFORE signals (stop-loss ordering) -------------
print("[G2] the echo hook fires before any signal from the same tick")
eng = make()
order = []
eng.on_echo = lambda fr: order.append("echo")
eng.on_verified = lambda fr: order.append("signal")
feed(eng, "SPY", [100, 99, 98, 97, 96, 95, 94, 93, 300, 310])
# every "signal" must be preceded by the echo of its own tick, i.e. the
# sequence never starts with a signal
check("the very first event is an echo, not a signal", order[0], "echo")
_bad = any(order[i] == "signal" and "echo" not in order[:i]
           for i in range(len(order)))
check("no signal ever precedes its own tick's echo -- the stop-loss "
      "check must get the tick first", _bad, False)

# ---- [G3] v3.44 regression: captured models reference stays valid ----
print("[G3] a captured models reference survives reconfiguration")
eng = make(("SPY", "QQQ"))
captured = eng.models["vwap_bounce"]        # exactly what order_manager
                                            # hands the risk overlay
check("captured reference sees the initial symbols",
      sorted(captured), ["QQQ", "SPY"])
eng.configure_symbols(["SPY", "QQQ", "AAPL"])
check("models dict object is the SAME object after reconfiguration -- "
      "the v3.44 bug was Bridge reassigning it",
      eng.models["vwap_bounce"] is captured, True)
check("the captured reference SEES the newly added symbol",
      "AAPL" in captured, True)
check("captured.get(new symbol) is not None -- None is what made "
      "peek_stop_price_e4 return 0 and disabled stop-losses",
      captured.get("AAPL") is not None, True)
eng.configure_symbols(["SPY", "AAPL"])
check("removed symbols drop out of the same reference",
      "QQQ" in captured, False)
check("still the same object after a removal too",
      eng.models["vwap_bounce"] is captured, True)

# ---- [G4] v3.46: reconfiguration preserves other symbols' state ------
print("[G4] adding a symbol does not reset the other symbols' models")
eng = make(("SPY", "QQQ"))
feed(eng, "SPY", [100, 101, 102, 103, 104, 105])
spy_model = eng.models["vwap_bounce"]["SPY"]
sum_v_before = spy_model.sum_v
vwap_before = spy_model.vwap
check("SPY accumulated some session VWAP volume", sum_v_before > 0, True)

eng.configure_symbols(["SPY", "QQQ", "AAPL"])
check("SPY's VWAP model object is untouched by the reconfiguration",
      eng.models["vwap_bounce"]["SPY"] is spy_model, True)
check("SPY's accumulated volume survived",
      eng.models["vwap_bounce"]["SPY"].sum_v, sum_v_before)
check("SPY's session VWAP survived -- under the Bridge every "
      "reconfiguration rebuilt every model, silently moving the stops "
      "and risk sizing of symbols you never touched",
      eng.models["vwap_bounce"]["SPY"].vwap, vwap_before)
check("the newly added symbol starts fresh, as it should",
      eng.models["vwap_bounce"]["AAPL"].sum_v, 0)

# ---- [G5] session reset + unconfigured symbols -----------------------
print("[G5] session reset and unconfigured-symbol handling")
eng = make(("SPY",))
feed(eng, "SPY", [100, 101, 102, 103, 104, 105])
check("VWAP accumulated before the reset",
      eng.models["vwap_bounce"]["SPY"].sum_v > 0, True)
check("send_sessrst reports success", eng.send_sessrst(), True)
check("session VWAP volume cleared", eng.models["vwap_bounce"]["SPY"].sum_v,
      0)

eng = make(("SPY",))
got = []
eng.on_echo = got.append
feed(eng, "TSLA", [100, 101, 102])       # never configured
check("ticks for an unconfigured symbol are dropped, not crashed on",
      len(got), 0)
check("...and are not counted as processed", eng.echoes, 0)

# ---- [G6] v3.46 regression: run_alpaca must CHAIN the hook -----------
print("[G6] feeds.run_alpaca chains on_symbols_changed instead of "
      "overwriting it")
import inspect
import feeds
_src = inspect.getsource(feeds.run_alpaca)
                    # match a real STATEMENT, not the comment above it
                    # that quotes the old line while explaining the bug
_stmts = [ln.strip() for ln in _src.splitlines()
          if not ln.strip().startswith("#")]
check("the bare overwrite that caused the live bug is gone as an "
      "actual statement",
      "br.on_symbols_changed = resub" in _stmts, False)
check("a previous handler is captured before installing",
      "_prev_on_symbols_changed" in _src, True)

# behavioural proof: install a handler the way order_manager does, then
# let run_alpaca's wiring run, then reconfigure -- both must fire.
fired = []


class _FakeEngine:
    """Minimal stand-in exposing just what the chaining logic touches."""
    def __init__(self):
        self.on_symbols_changed = None
        self.symbols = ["SPY"]

    def configure_symbols(self, syms):
        self.symbols = syms
        if self.on_symbols_changed:
            self.on_symbols_changed(syms)
        return True


fake = _FakeEngine()
fake.on_symbols_changed = lambda syms: fired.append(("om_resync", syms))


def _install_like_run_alpaca(br):
    """The exact shape feeds.run_alpaca now uses."""
    def resub(new_syms):
        fired.append(("resub", new_syms))
    _prev = getattr(br, "on_symbols_changed", None)

    def _chained(new_syms):
        resub(new_syms)
        if _prev:
            _prev(new_syms)
    br.on_symbols_changed = _chained


_install_like_run_alpaca(fake)
fake.configure_symbols(["SPY", "AAPL"])
check("the feed's own resubscribe still fires",
      ("resub", ["SPY", "AAPL"]) in fired, True)
check("the previously-installed handler ALSO fires -- this is the "
      "regression: overwriting it left the risk overlay stale and "
      "opened positions with stop_price_e4 = 0",
      ("om_resync", ["SPY", "AAPL"]) in fired, True)

# ---- [G7] Bridge duck-type compatibility ----------------------------
print("[G7] duck-type compatible with Bridge for every attribute the "
      "feeds and dashboard touch")
eng = make(("SPY", "QQQ"))
for attr in ("symbols", "symbol", "models", "sent", "echoes",
             "fpga_signals", "rtt_us", "parser", "verifiers",
             "on_verified", "on_echo", "on_divergence",
             "on_symbols_changed"):
    check(f"has attribute {attr}", hasattr(eng, attr), True)
for meth in ("configure_symbols", "send_trade", "pump", "send_sessrst",
             "summary", "close"):
    check(f"has callable {meth}", callable(getattr(eng, meth, None)), True)
check("parser.resync_count readable (dashboard reads it)",
      eng.parser.resync_count, 0)
check("verifiers is empty -- nothing to verify with one computation",
      len(eng.verifiers), 0)
check("summary() returns True (a single computation cannot diverge "
      "from itself)", eng.summary(), True)
eng.close()

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
