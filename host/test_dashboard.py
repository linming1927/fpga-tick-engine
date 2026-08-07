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
check("v3.48: each series point is VWAP-shaped -- "
     "(price, vwap, sigma, warmed) -- not the old 7-tuple of SMA/EMA "
     "values. Sigma is carried raw so the browser can derive BOTH the "
     "+/-k signal band and the N-sigma stop line from one number",
     len(s["series"][0]), 4)
check("v3.48: ONLY vwap_bounce signals are reported -- sma/ema are "
     "still scored in the background and still appear in the "
     "end-of-session comparison, they are just not shown here",
     all(x["strategy"] == "vwap_bounce" for x in s["signals"]), True)
check("signals carry strategy tags",
      all(x["strategy"] in ("sma", "ema", "vwap_bounce")
          for x in s["signals"]), True)
# ^ v3.19: verified fabric-VWAP signals (0x85) join the log; the
# emulator now emits them, so the tape can legitimately contain all 3
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
                "strategy": "vwap_bounce", "vwap": 0},
               outcome="blocked: cooldown (5.0s < 60.0s)")
s2 = json.loads(get("/api/state"))
check("newest signal carries the real outcome string",
      s2["signals"][0]["outcome"], "blocked: cooldown (5.0s < 60.0s)")
check("a signal recorded WITHOUT an outcome defaults to empty, not "
     "a crash (backward compatible with any caller that doesn't "
     "pass one)", "outcome" in s2["signals"][0], True)

# ---- v3.2.2: a busy symbol must not crowd a quiet symbol's markers off
# ITS OWN chart -- the actual reported bug (signals were stored in one
# shared global deque, so SPY firing often could evict QQQ's older,
# still-on-screen signals before QQQ's chart ever got drawn) -----------
print("[G_markers] per-symbol chart signals aren't crowded out by a "
     "busy OTHER symbol")

def fab(sym, side, price):
    return {"side": side, "price_e4": price, "symbol": sym,
            "vwap": 0, "strategy": "vwap_bounce"}

# QQQ fires ONE signal early...
dash.on_signal(fab("QQQ", 1, 4_000_000), outcome="FILLED")
# ...then SPY fires 25 signals (more than the old shared maxlen=20),
# which under the OLD design would have evicted QQQ's signal entirely
for i in range(25):
    dash.on_signal(fab("SPY", 1 if i % 2 == 0 else 2, 1_000_000 + i),
                   outcome="FILLED")

spy_state = json.loads(get("/api/state?sym=SPY"))
qqq_state = json.loads(get("/api/state?sym=QQQ"))
check("SPY's OWN chart_signals still capped sanely (not unbounded)",
      len(spy_state["chart_signals"]) <= 20, True)
check("QQQ's signal SURVIVES on its own chart despite 25 unrelated "
     "SPY signals arriving afterward -- this is the actual fix",
     len(qqq_state["chart_signals"]), 1)
check("QQQ's surviving signal is the real one, not a coincidence",
      qqq_state["chart_signals"][0]["price_e4"], 4_000_000)
check("the GLOBAL table-facing list is independent of the per-symbol "
     "fix — it correctly ages QQQ's older entry out once 26 total "
     "signals exceed its own 20-slot cap (that's expected: the global "
     "table is 'recent across everything', not 'never forget any "
     "symbol' — only the PER-SYMBOL chart view needed that guarantee)",
     any(x["symbol"] == "QQQ" for x in spy_state["signals"]), False)

# ---- v3.48: the HOLDINGS table and the VWAP chart's band parameters --
print("[G_holdings] HOLDINGS table and chart band/stop multipliers")

from position_risk import PositionRiskOverlay

s3 = json.loads(get("/api/state"))
check("band_k is exposed so the chart draws the REAL signal band "
     "rather than a hardcoded multiple", s3["band_k"], 1.0)
check("stop_mult is exposed the same way; 0.0 when the risk overlay "
     "is off, which tells the chart to omit the stop line entirely",
     s3["stop_mult"], 0.0)
check("holdings is always present, even with nothing open",
      isinstance(s3["holdings"], list), True)
check("...and is empty when flat", s3["holdings"], [])
check("only the LIVE strategy row is reported now",
      all(c["live"] for c in s3["strategies"]), True)

# a real open position, with the overlay armed the way a live session
# would arm it
om.risk_overlay = PositionRiskOverlay(stop_sigma_mult=3.0,
                                      risk_dollars_per_trade=50.0)
om.vwap_models = br.models["vwap_bounce"]
om.positions["SPY"] = 4
om.costs._entries["SPY"] = [4, 1_000_000]        # 4 @ $100.00
# the mirror needs real accumulated data, otherwise sigma is 0 and the
# committed stop degenerates to 0 -- which is exactly the empty-mirror
# pathology v3.46 fixed, not what this test is about
_vm = br.models["vwap_bounce"]["SPY"]
for _p in (98_0000, 102_0000) * 15:
    _vm.ingest(_p, 100)                      # vwap ~$100, sigma ~$2
om.risk_overlay.on_position_opened(
    "SPY", om.policy._now_fn().date(),       # a DATE, per its docstring
    _vm)
check("the fixture's stop is real, not the degenerate empty-mirror 0 "
     "-- otherwise the rows below would all be vacuously None",
     om.risk_overlay.stop_price_e4("SPY") > 0, True)
om.risk_overlay.on_tick("SPY", 1_020_000, 100)
dash._last_price["SPY"] = 1_020_000              # last $102.00

h = json.loads(get("/api/state"))["holdings"]
check("the open position appears", len(h), 1)
row = h[0]
check("symbol", row["symbol"], "SPY")
check("qty", row["qty"], 4)
check("avg cost comes from CostTracker's own blended average, so the "
     "table can never disagree with what the trading logic uses",
     row["avg_e4"], 1_000_000)
check("last price is marked to the latest tick", row["last_e4"], 1_020_000)
check("position value = qty * last", round(row["value"], 2), 408.00)
check("unrealized P&L = qty * (last - avg)", round(row["unreal"], 2), 8.00)
check("unrealized percent", row["unreal_pct"], 2.0)
check("the committed stop is surfaced, not recomputed",
      row["stop_e4"], om.risk_overlay.stop_price_e4("SPY"))
check("anchored VWAP is surfaced -- it is what the sell gate judges an "
     "overnight position against",
     row["anchor_e4"], om.risk_overlay.anchored_vwap_e4("SPY"))
check("same_day is reported, since it decides whether the sell gate "
     "applies at all", row["same_day"], True)
check("sell_ok reports the gate's ACTUAL answer, so \"why didn\'t it "
     "sell?\" is visible rather than silent",
     row["sell_ok"],
     om.risk_overlay.sell_allowed("SPY", 1_020_000,
                                  om.policy._now_fn().date()))
check("risk-if-stopped is qty * (avg - stop) -- the real exposure from "
     "here, which is NOT --risk-per-trade once the caps have trimmed "
     "an order or the position has been added to",
     round(row["risk"], 2),
     round(4 * (1_000_000 - row["stop_e4"]) / 10_000, 2))
check("distance-to-stop is a percentage of the CURRENT price, the "
     "single most useful number when judging danger",
     row["to_stop_pct"],
     round((1_020_000 - row["stop_e4"]) / 1_020_000 * 100, 2))

om.positions["SPY"] = 0
check("a closed position drops out of the table again",
      json.loads(get("/api/state"))["holdings"], [])

# ---- v3.53: per-fill P&L in the signals table, and two tiles gone ----
from tick_protocol import SIDE_BUY, SIDE_SELL

print("[G_pnl] SELL rows carry the round trip's own realized P&L, and "
     "the UART-only VERIFIED / RTT tiles are gone")

dash.on_signal({"side": SIDE_BUY, "price_e4": 1_000_000, "symbol": "SPY",
                "strategy": "vwap_bounce", "vwap": 1_010_000},
               outcome="FILLED", trade_pnl_e4=None, fill_qty=7)
dash.on_signal({"side": SIDE_SELL, "price_e4": 1_100_000, "symbol": "SPY",
                "strategy": "vwap_bounce", "vwap": 1_010_000},
               outcome="FILLED", trade_pnl_e4=123_456, fill_qty=4)
dash.on_signal({"side": SIDE_SELL, "price_e4": 900_000, "symbol": "SPY",
                "strategy": "vwap_bounce", "vwap": 1_010_000},
               outcome="FILLED", trade_pnl_e4=-45_000, fill_qty=4)
dash.on_signal({"side": SIDE_SELL, "price_e4": 950_000, "symbol": "SPY",
                "strategy": "vwap_bounce", "vwap": 1_010_000},
               outcome="blocked: cooldown (1.0s < 60.0s)")

sig_pnl = json.loads(get("/api/state"))["signals"]
check("a losing SELL carries a negative P&L", sig_pnl[1]["trade_pnl_e4"],
      -45_000)
check("a winning SELL carries a positive one", sig_pnl[2]["trade_pnl_e4"],
      123_456)
check("a BUY carries none -- nothing is realized until the position "
     "closes", sig_pnl[3]["trade_pnl_e4"], None)
check("a signal that never filled carries none either, so the column "
     "cannot imply a trip that did not happen",
     sig_pnl[0]["trade_pnl_e4"], None)

check("a filled BUY row carries the share count -- the ask that "
     "prompted this column", sig_pnl[3]["fill_qty"], 7)
check("a filled SELL row carries it too", sig_pnl[2]["fill_qty"], 4)
check("a blocked signal carries none, so the column never shows shares "
     "for an order that did not happen", sig_pnl[0]["fill_qty"], None)

# ---- v3.57: the FILLS log, separate from SIGNALS ---------------------
# The four on_signal calls above were: one blocked SELL, then a filled
# BUY, a filled SELL and another filled SELL. Only the three FILLED ones
# belong in the fills log.
# NOTE: earlier groups in this file drive a real emulator session that
# produces genuine fills of its own, so this checks the three newest
# entries -- the ones the G_pnl block just created -- rather than the
# whole log.
_fl_all = json.loads(get("/api/state"))["fills"]
_fl = _fl_all[:3]
check("the blocked SELL is ABSENT from the fills log while all three "
     "FILLED events are present -- the whole point, since real "
     "transactions were being crowded out of the 20-slot SIGNALS deque "
     "by thousands of blocked signals, mostly cooldown",
     [f["qty"] for f in _fl], [4, 4, 7])
check("...newest first, matching the signals table's ordering",
      _fl[0]["side"], SIDE_SELL)
check("every entry in the log is a real fill, never a blocked signal",
      all(f["qty"] is not None or f["price_e4"] for f in _fl_all), True)
check("a SELL row carries the round trip's P&L",
      _fl[0]["trade_pnl_e4"], -45_000)
check("a BUY row carries none -- nothing is realized until it closes",
      _fl[2]["trade_pnl_e4"], None)
check("notional is shares x price, so the row stands alone without "
     "arithmetic", round(_fl[2]["notional"], 2), round(7 * 100.0, 2))
check("position_after is read from the OrderManager at fill time, not "
     "reconstructed", "position_after" in _fl[0], True)
check("...and the running session P&L is carried too, so the log shows "
     "the trajectory rather than just isolated trades",
     "cum_pnl" in _fl[0], True)

# v3.58: the stop-loss path must reach the dashboard too. It calls
# om.on_signal() directly from the tick hook -- deliberately, since a
# breached stop has to act on the tick that breached it -- which meant
# it skipped the one place dash.on_signal() was called. A stop-triggered
# sell filled, booked P&L and moved the position while staying entirely
# invisible on screen. Found from a real session: two sells (CIFR
# -$4.32, MARA -$8.75) missing from the FILLS box with no trace.
dash.on_signal({"side": SIDE_SELL, "price_e4": 900_000, "symbol": "SPY",
                "strategy": "vwap_bounce", "vwap": 1_010_000},
               outcome="FILLED", trade_pnl_e4=-43_200, fill_qty=17,
               reason="stop-loss")
_fl2 = json.loads(get("/api/state"))["fills"]
check("a stop-loss fill reaches the fills log at all -- the actual bug",
      _fl2[0]["qty"], 17)
check("...tagged so a stop exit is distinguishable from a strategy "
     "exit, which mean very different things",
     _fl2[0]["reason"], "stop-loss")
check("...carrying its realized loss like any other fill",
      _fl2[0]["trade_pnl_e4"], -43_200)
check("a normal signal fill is tagged 'signal', not left blank",
      _fl2[1]["reason"], "signal")

_page_f = get("/").decode()
check("the FILLS panel is on the page", "<h2>FILLS" in _page_f, True)
check("...with its own renderer", "renderFills" in _page_f, True)
check("...and its own table body", 'id="fills"' in _page_f, True)
check("...and a why column, so the trigger is visible per row",
      "<th>why</th>" in _page_f, True)

# the fills deque must outlive the signals deque
_before_f = len(json.loads(get("/api/state"))["fills"])
for _i in range(40):
    dash.on_signal({"side": SIDE_BUY, "price_e4": 1_000_000, "symbol": "SPY",
                    "strategy": "vwap_bounce", "vwap": 1_010_000},
                   outcome="FILLED", trade_pnl_e4=None, fill_qty=1)
_st = json.loads(get("/api/state"))
check("SIGNALS still caps at 20 -- unchanged", len(_st["signals"]), 20)
check("...while FILLS grew by all 40, so a transaction from earlier in "
     "the session is still there long after the signals box has churned "
     "past it", len(_st["fills"]) - _before_f, 40)
check("...and the fills log holds far more than the signals box",
      len(_st["fills"]) > len(_st["signals"]), True)

page_pnl = get("/").decode()
check("the signals table has a P&L column", "<th>P&amp;L</th>" in page_pnl,
      True)
check("...and a shares column", "<th>shares</th>" in page_pnl, True)
check("the VERIFIED tile is gone -- it counts fabric signals matched "
     "against the host mirror, which means nothing on the direct "
     "in-process engine", "stat('VERIFIED'" in page_pnl, False)
check("the RTT tile is gone -- there is no round trip to measure "
     "without a UART", "stat('RTT" in page_pnl, False)
check("...but the snapshot still carries rtt, so a --port session and "
     "anything reading /api/state keep working",
     "rtt" in json.loads(get("/api/state")), True)

dash.stop(); br.close(); emu.stop()

# ---- v3.14: right-axis chart labels weren't showing real prices -- two
# compounding bugs in the embedded chart JS: (1) bare toFixed(2) instead
# of the usd() helper used everywhere else on the page, so labels read
# "436.72" instead of "$436.72", and (2) even fixed, the reserved gutter
# was too narrow to fit a $-prefixed label without clipping against the
# canvas edge -- measured against the actual font ("$1234.56" is ~48px
# at the page's 10px monospace, and the old 46px-wide gutter only gave
# it 42px before the canvas boundary). This is a structural test, not a
# rendered-pixel one: this test suite is pure Python with no other
# dependency on node or a canvas library anywhere, so it parses the
# PAGE source the server actually ships rather than executing the JS.
print("\n[G_axis] right-axis chart labels: $-formatted and wide enough "
     "not to clip")
import re
from dashboard import PAGE
m = re.search(r"function drawChart.*?\n}\n", PAGE, re.S)
check("drawChart() is present in the served page", m is not None, True)
draw_src = m.group(0)

fill_line = [l for l in draw_src.splitlines() if "fillText(usd" in l
            or "fillText((v" in l]
check("exactly one axis-label fillText call found", len(fill_line), 1)
check("axis label uses usd() -- matches every other price on the page "
     "($436.72), not a bare number (436.72, the actual reported bug)",
     "fillText(usd(v)" in fill_line[0] if fill_line else False, True)

x_gutter_m = re.search(r"X=i=>i/\(series\.length-1\)\*\(W-(\d+)\)", draw_src)
grid_gutter_m = re.search(r"moveTo\(0,y\);g\.lineTo\(W-(\d+),y\)", draw_src)
label_offset_m = re.search(r"fillText\(usd\(v\),W-(\d+)", draw_src)
check("X() gutter found", x_gutter_m is not None, True)
check("gridline gutter found", grid_gutter_m is not None, True)
check("label offset found", label_offset_m is not None, True)
x_gutter = int(x_gutter_m.group(1)) if x_gutter_m else -1
grid_gutter = int(grid_gutter_m.group(1)) if grid_gutter_m else -2
label_offset = int(label_offset_m.group(1)) if label_offset_m else -1
check("X() and the gridline endpoint agree on ONE gutter width (no "
     "mismatched constant left over from a partial edit)",
     x_gutter, grid_gutter)
gutter = x_gutter
# same measured-not-guessed budget as the manual verification: "$1234.56"
# is ~48px at this font; require the gutter leave it real margin, not
# just barely fit
check(f"gutter ({gutter}px) leaves a 4-digit dollar price "
     f"(~48px rendered) comfortable margin before the canvas edge",
     gutter >= 56, True)
check("label starts 4px inside the gutter boundary (X()'s plot area "
     "and the gridline both stop at W-gutter; the label sitting exactly "
     "4px further right, same margin convention as the original code, "
     "not flush against the plotted line)",
     gutter - label_offset, 4)

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
