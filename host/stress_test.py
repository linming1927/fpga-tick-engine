#!/usr/bin/env python3
"""
stress_test.py — find the HOST's real throughput ceiling, not the wire's.

    python3 stress_test.py

This is a BENCHMARK, not a pass/fail unit test — there's no universally
"correct" answer for how many ticks/sec your particular machine can
sustain, so most of this prints a measured report rather than asserting
one. A few low-rate sanity checks do assert (any reasonable machine
should handle those trivially); the interesting numbers are the sweep.

WHAT THIS DOES NOT MEASURE: the emulator talks to the bridge over a
pseudo-tty (pty), and a pty does not enforce real UART bit-level pacing
the way physical hardware does — it moves bytes as fast as the OS
schedules them. So this script cannot verify the WIRE's baud-derived
ceiling (that's pure arithmetic — see the header note below); what it
CAN measure, honestly, is whether the same Python code that would run
against real hardware (FrameParser, per-symbol SignalVerifiers, model
ingestion, dashboard/scorecard hooks) keeps up as tick rate increases.
That's the piece the baud math can't answer, and per the project's own
history it's the piece that actually failed first: today's real
divergence-triggered kill happened at a tick rate far below even the
OLD 115200-baud wire ceiling, which is why this test targets the host,
not the wire.

Reference ceilings (24B tick / 32B echo frames, 8N1 = 10 bits/byte):
    115200 baud -> ~360 ticks/sec sustained (downlink-bound)
    921600 baud -> ~2880 ticks/sec sustained (downlink-bound)
Compare the empirical "last healthy rate" this script finds against
whichever of those applies to you, to see which one actually binds.

Two phases:
  SWEEP  ticks at increasing aggregate rates across 8 symbols, each for
         a few seconds, checking echo/sent parity, divergences, and RTT
         at each level. Finds the highest rate that stayed healthy.
  BURST  reproduces the real incident's failure SHAPE at 8-symbol scale:
         a pile of ticks arriving with no pacing at all (simulating the
         backlog that follows a host-side stall), verifying the v2.2.1
         per-symbol grace-window fix holds up even when scaled from 2
         symbols to 8.
"""

from __future__ import annotations
import statistics
import sys
import time

import argparse
import threading
import urllib.request

from bridge import Bridge
from fpga_emulator import FPGAEmulator

# Eight liquid, real S&P 500 names — a deliberately demanding mix for a
# "what if all 8 slots are heavily-traded names at once" stress scenario.
SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "TSLA"]

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


def run_phase(br, rate_per_sec: float, duration_s: float) -> dict:
    """Round-robins ticks across all 8 symbols at the given AGGREGATE
    rate for duration_s, then reports what actually happened."""
    sent0, echo0 = br.sent, br.echoes
    div0 = sum(v.divergences for v in br.verifiers.values())
    rtt_start = len(br.rtt_us)
    price = {s: 1_000_000 + i * 300_000 for i, s in enumerate(SYMBOLS)}

    period = 1.0 / rate_per_sec
    n = int(rate_per_sec * duration_s)
    t0 = time.monotonic()
    for i in range(n):
        sym = SYMBOLS[i % len(SYMBOLS)]
        price[sym] = max(100_000, price[sym] + ((i * 7919) % 6001 - 3000))
        br.send_trade(price[sym], 1, symbol=sym)
        br.pump(timeout=period)
    br.pump(timeout=0.3)                       # drain the tail
    elapsed = time.monotonic() - t0

    sent_d = br.sent - sent0
    echo_d = br.echoes - echo0
    div_d = sum(v.divergences for v in br.verifiers.values()) - div0
    rtts = br.rtt_us[rtt_start:]

    return {
        "target_rate": rate_per_sec,
        "achieved_rate": echo_d / elapsed if elapsed > 0 else 0,
        "sent": sent_d, "echoed": echo_d,
        "lost": sent_d - echo_d,
        "divergences": div_d,
        "rtt_med_ms": (statistics.median(rtts) / 1000) if rtts else None,
        "rtt_max_ms": (max(rtts) / 1000) if rtts else None,
        "healthy": (sent_d == echo_d and div_d == 0
                   and (not rtts or max(rtts) < 2_000_000)),
    }


def start_dashboard_load(br, port: int = 8899):
    """Optional: replicate a REAL session's other CPU consumer — the
    dashboard's HTTP server plus a browser polling /api/state every
    500ms (matching dashboard.py's own frontend poll cadence exactly).
    A synthetic bridge-only test undercounts your actual load: real
    sessions run this concurrently. Returns (dash, stop_event)."""
    from dashboard import DashboardServer
    from order_manager import OrderManager, RiskLimits, MockBroker

    om = OrderManager(
        MockBroker(), SYMBOLS,
        RiskLimits(order_qty=1, max_shares=999, max_notional_e4=10**13,
                  max_orders_per_day=10**6, cooldown_s=0.0,
                  require_market_hours=False),
        audit_path="/tmp/stress_audit.jsonl", killfile="/tmp/stress.kill")
    dash = DashboardServer(br, om, port, scorecards=None)
    dash.start()

    stop = threading.Event()

    def poll_loop():
        while not stop.is_set():
            try:
                urllib.request.urlopen(
                    f"http://localhost:{port}/api/state", timeout=1)
            except Exception:
                pass
            stop.wait(0.5)          # exact cadence of the real frontend

    threading.Thread(target=poll_loop, daemon=True).start()
    print(f"[setup] dashboard load running on :{port} "
         f"(polled every 500ms, like a real open browser tab)")
    return dash, stop


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dashboard", action="store_true",
                    help="also run the dashboard HTTP server + simulated "
                         "browser polling, for a more realistic total load")
    args = ap.parse_args()

    emu = FPGAEmulator(symbol="SPY", fast_n=8, slow_n=32)
    br = Bridge(emu.start(), SYMBOLS, fast_n=8, slow_n=32)

    print(f"[setup] configuring 8 slots: {SYMBOLS}")
    ok = br.configure_symbols(SYMBOLS)
    check("all 8 slots configured + acked", ok, True)

    dash_stop = None
    if args.dashboard:
        _, dash_stop = start_dashboard_load(br)

    # ---- baseline sanity: any reasonable machine should pass these ----
    print("\n[sanity] low rates — these SHOULD pass on any machine")
    for rate in (20, 50):
        r = run_phase(br, rate, duration_s=2.0)
        check(f"{rate}/s: no frame loss", r["lost"], 0)
        check(f"{rate}/s: no divergences", r["divergences"], 0)
        check(f"{rate}/s: RTT stayed bounded (<2s)",
              r["rtt_max_ms"] is None or r["rtt_max_ms"] < 2000, True)

    # ---- the sweep: this is the actual measurement, not a fixed test ----
    print("\n[sweep] finding YOUR host's real ceiling "
         "(reference: 360/s @ 115200 baud, 2880/s @ 921600 baud)")
    print(f"  {'target/s':>9} {'achieved/s':>11} {'lost':>5} {'div':>4} "
         f"{'rtt med':>9} {'rtt max':>9}  health")
    # extended past the 921600-baud wire ceiling (2880/s) deliberately —
    # the point is to find out whether the HOST or the WIRE binds first
    levels = [50, 100, 200, 400, 800, 1200, 1800, 2400,
             3200, 4200, 5500, 7000, 9000]
    last_healthy = None
    for rate in levels:
        r = run_phase(br, rate, duration_s=2.5)
        tag = "OK" if r["healthy"] else "** DEGRADED **"
        med = f"{r['rtt_med_ms']:.0f}ms" if r["rtt_med_ms"] else "  n/a"
        mx = f"{r['rtt_max_ms']:.0f}ms" if r["rtt_max_ms"] else "  n/a"
        print(f"  {r['target_rate']:>9.0f} {r['achieved_rate']:>11.0f} "
             f"{r['lost']:>5} {r['divergences']:>4} {med:>9} {mx:>9}  {tag}")
        if r["healthy"]:
            last_healthy = rate
        elif last_healthy is not None:
            break        # stop climbing once it degrades — found the edge

    # ---- burst: the real incident's failure SHAPE, at 8-symbol scale ----
    print("\n[burst] no pacing at all across all 8 symbols "
         "(reproduces the real stall-then-backlog pattern)")
    div_before = sum(v.divergences for v in br.verifiers.values())
    price = {s: 2_000_000 for s in SYMBOLS}
    for i in range(400):
        sym = SYMBOLS[i % len(SYMBOLS)]
        price[sym] = max(100_000, price[sym] + ((i * 5003) % 4001 - 2000))
        br.send_trade(price[sym], 1, symbol=sym)     # NO pump between sends
    br.pump(timeout=3.0)                             # let it all drain
    div_after = sum(v.divergences for v in br.verifiers.values())
    check("burst: sent == echoed (nothing silently lost)",
          br.echoes, br.sent)
    check("burst: no divergence from unpaced multi-symbol arrival",
          div_after - div_before, 0)

    print(f"\n==============================================")
    print(f"  sanity checks: {PASS} PASS / {FAIL} FAIL")
    if last_healthy:
        print(f"  empirical ceiling THIS RUN stayed healthy through: "
             f"~{last_healthy}/s aggregate")
        print(f"  (a different machine, or Python build, will get a "
             f"different number — rerun there before relying on this)")
    print(f"==============================================")

    if dash_stop:
        dash_stop.set()
    br.close()
    emu.stop()
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
