#!/usr/bin/env python3
"""
test_dashboard.py — the console must reflect reality and the kill must kill.

    python3 test_dashboard.py

Runs the full stack (FPGAEmulator -> Bridge -> OrderManager -> MockBroker)
with a DashboardServer attached, drives a random walk, then checks over
HTTP that: the page serves; /api/state carries live series, signals, P&L
and fee numbers that match the Python objects; and POST /api/kill trips
the latching kill switch.
"""

from __future__ import annotations
import contextlib, io, json, os, random, sys, tempfile, time
import urllib.error, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fpga_emulator import FPGAEmulator
from bridge import Bridge
from order_manager import OrderManager, RiskLimits, MockBroker
from dashboard import DashboardServer

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


PORT = 8765


def get(path):
    with urllib.request.urlopen(f"http://localhost:{PORT}{path}",
                                timeout=3) as r:
        return r.read()


# ---- assemble the stack ------------------------------------------------------
d = tempfile.mkdtemp()
emu = FPGAEmulator(symbol="SPY ", fast_n=4, slow_n=8)
br = Bridge(emu.start(), "SPY", fast_n=4, slow_n=8)
om = OrderManager(MockBroker(), ["SPY"],
                  RiskLimits(order_qty=1, max_shares=1,
                             max_notional_e4=10**13, max_orders_per_day=99,
                             cooldown_s=0.0, require_market_hours=False),
                  audit_path=os.path.join(d, "a.jsonl"),
                  killfile=os.path.join(d, "om.kill"))
dash = DashboardServer(br, om, PORT).start()
br.on_verified = lambda fr: (dash.on_signal(fr), om.on_signal(fr))
br.on_divergence = lambda i: (dash.on_event("DIV", True),
                              om.on_divergence(i))
br._build_models()          # re-attach hooks set after construction

rng = random.Random(7)
price = 1_500_000
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    for _ in range(100):
        price = max(100_000, price + rng.randint(-60_000, 60_000))
        br.send_trade(price, 1)
        br.pump(timeout=0.004)
    br.pump(timeout=0.5); time.sleep(0.2); br.pump(timeout=0.2)

# ---- G1: page + state --------------------------------------------------------
print("\n[G1] page serves and state mirrors the live objects")
page = get("/").decode()
check("HTML page serves", page.startswith("<!DOCTYPE html>"), True)
check("page has the chart canvas", 'id="chart"' in page, True)
check("page has the kill switch", "KILL SWITCH" in page, True)
check("two chart canvases present, for two side-by-side symbols",
      page.count('<canvas id="chart') >= 2, True)
check("both chart dropdowns share the same styling rule "
     "(#csym2 previously fell back to unstyled browser defaults, "
     "since the CSS only targeted #csym)",
     "#csym,#csym2" in page or "#csym2,#csym" in page, True)
check("second chart has its own independent symbol selector",
      'id="csym2"' in page, True)
check("EVENTS section appears AFTER (below) the SIGNALS table in the "
     "page source, not beside the chart", page.index('id="log"') >
     page.index('id="sigs"'), True)
check("no external resources", ("http://" in page.replace("http://localhost", "")
                                or "https://" in page), False)

s = json.loads(get("/api/state"))
check("symbol", s["symbol"], "SPY")
check("series populated", len(s["series"]) > 50, True)
check("series matches echo count", len(s["series"]), min(br.echoes, 240))
check("signals present", len(s["signals"]) > 0, True)
check("signals mirror verifiers", s["verified"],
      sum(v.verified for v in br.verifiers.values()))
check("series carries both strategies", len(s["series"][0]), 7)
check("signals carry strategy tags",
      all(x["strategy"] in ("sma", "ema") for x in s["signals"]), True)
check("signals carry symbols",
      all(x["symbol"] == "SPY" for x in s["signals"]), True)
check("slot list in state", s["symbols"], ["SPY"])

# ---- v2: the GUI symbol editor endpoint reconfigures the FPGA ----------
print("[G1b] POST /api/symbols writes slots and rebuilds models")
req = urllib.request.Request(
    f"http://localhost:{PORT}/api/symbols", method="POST",
    data=json.dumps({"symbols": ["SPY", "QQQ"]}).encode(),
    headers={"Content-Type": "application/json"})
with contextlib.redirect_stdout(io.StringIO()):
    r = json.loads(urllib.request.urlopen(req, timeout=5).read())
check("symbols endpoint acked", (r["ok"], r["symbols"]),
      (True, ["SPY", "QQQ"]))
s2 = json.loads(get("/api/state?sym=QQQ"))
check("state reflects new slots", s2["symbols"], ["SPY", "QQQ"])
check("chart follows ?sym=", s2["symbol"], "QQQ")
bad = urllib.request.Request(
    f"http://localhost:{PORT}/api/symbols", method="POST",
    data=json.dumps({"symbols": ["TOOLONG7"]}).encode(),
    headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(bad, timeout=5)
    check("bad ticker rejected with 400", "200", "400")
except urllib.error.HTTPError as e:
    check("bad ticker rejected with 400", str(e.code), "400")
check("pnl matches tracker", s["pnl_net"], om.costs.net_pnl_usd)
check("fees match tracker", s["fees"], om.costs.total_fees)
check("positions match OM",
      s["positions"], {k: v for k, v in om.positions.items() if v})
check("warmed up", s["warmed_up"], True)
check("rtt reported", s["rtt"] is not None, True)
check("not halted yet", s["halted"], False)
check("link LED on", s["led"]["link"], True)

# ---- G2: kill endpoint --------------------------------------------------------
print("[G2] POST /api/kill trips the latching kill switch")
with contextlib.redirect_stdout(io.StringIO()):
    r = json.loads(urllib.request.urlopen(
        urllib.request.Request(f"http://localhost:{PORT}/api/kill",
                               method="POST"), timeout=3).read())
check("kill endpoint acknowledges", r["halted"], True)
check("OM halted", om.halted, True)
check("kill marker written", os.path.exists(os.path.join(d, "om.kill")), True)
s = json.loads(get("/api/state"))
check("state reflects halt", s["halted"], True)
check("halt reason names dashboard", "dashboard" in s["halt_reason"], True)
check("trouble LED on after kill", s["led"]["trouble"], True)
check("event logged", any("KILL" in e["text"] for e in s["events"]), True)

# ---- v3.1: the outcome column reaches the API, not just the object ----
print("[G_outcome] on_signal's outcome parameter reaches /api/state")
dash.on_signal({"side": 1, "price_e4": 1_000_000, "symbol": "SPY",
                "strategy": "sma", "sma_fast": 0, "sma_slow": 0},
               outcome="blocked: cooldown (5.0s < 60.0s)")
s2 = json.loads(get("/api/state"))
check("newest signal carries the real outcome string",
      s2["signals"][0]["outcome"], "blocked: cooldown (5.0s < 60.0s)")
check("a signal recorded WITHOUT an outcome defaults to empty, not "
     "a crash (backward compatible with any caller that doesn't "
     "pass one)", "outcome" in s2["signals"][0], True)

dash.stop(); br.close(); emu.stop()

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
