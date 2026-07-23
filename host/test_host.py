#!/usr/bin/env python3
"""
test_host.py — host-side test suite. Runs standalone (no pytest needed):

    python3 test_host.py

Four groups:
  G1  Golden vectors: the SMAMirror must reproduce the exact values the
      HARDWARE produced in tb_indicator_e2e.sv (warm-up 2000..1300, spike
      3000 -> BUY with fast=1800, slow=1775). This anchors the Python model
      to silicon-verified behavior — the defense against common-mode error
      where model and RTL could share the same misunderstanding.
  G2  Codec roundtrips: pack -> parse identity for both frame directions,
      including boundary values (max price, clamped qty).
  G3  FrameParser resync torture: frames delivered byte-by-byte, garbage
      injection, a fake SOF inside garbage, truncated frames.
  G4  Closed loop: a real Bridge talking to a real FPGAEmulator over a
      pty — random-walk trades, then assert every FPGA signal was verified
      against the model with zero divergences.
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tick_protocol import (FrameParser, SMAMirror, EMAMirror, pack_tick,
                           parse_tick, pack_fpga_echo, pack_fpga_signal,
                           pack_symcfg, sym_wire,
                           TICK_SOF, TICK_EOF, TICK_LEN, FPGA_LEN,
                           TYPE_SIGNAL_EMA, TYPE_SYMCFG,
                           SIDE_BUY, SIDE_SELL, TYPE_TRADE)

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


# ---------------------------------------------------------------------------
print("\n[G1] golden vectors from tb_indicator_e2e.sv (silicon-anchored)")
m = SMAMirror(fast_n=4, slow_n=8)
for p in (2000, 1900, 1800, 1700, 1600, 1500, 1400, 1300):
    check("no signal during warm-up", m.ingest(p), None)
check("warmed up after slow_n", m.warmed_up, True)
check("sma_fast after warm-up", m.sma_fast, 1450)   # (1600+1500+1400+1300)>>2
check("sma_slow after warm-up", m.sma_slow, 1650)   # 13200>>3
sig = m.ingest(3000)
check("golden cross fired", sig is not None, True)
if sig:
    check("side BUY", sig.side, SIDE_BUY)
    check("trigger price", sig.price_e4, 3000)
    check("sma_fast == hardware", sig.sma_fast, 1800)
    check("sma_slow == hardware", sig.sma_slow, 1775)
check("no retrigger while above", m.ingest(3100), None)

# EMA mirror against the SAME silicon-anchored sequence (tb_ema.sv P1/P2:
# K_FAST=1, K_SLOW=3, WARMUP=8 -> 1399/1725 after warm-up, BUY @3000 with
# 2200/1884)
em = EMAMirror(k_fast=1, k_slow=3, warmup_n=8)
for pr in (2000, 1900, 1800, 1700, 1600, 1500, 1400, 1300):
    check("ema: no signal during warm-up", em.ingest(pr), None)
check("ema fast anchor", em.ema_fast, 1399)
check("ema slow anchor", em.ema_slow, 1725)
esig = em.ingest(3000)
check("ema golden cross fired", esig is not None, True)
if esig:
    check("ema side BUY", esig.side, SIDE_BUY)
    check("ema fast == hardware", esig.sma_fast, 2200)
    check("ema slow == hardware", esig.sma_slow, 1884)
check("ema no retrigger", em.ingest(3100), None)

# extended-precision convergence: constant price must be reached EXACTLY
em2 = EMAMirror(k_fast=1, k_slow=3, warmup_n=8)
for _ in range(200):
    em2.ingest(1_234_567)
check("ema converges exactly (no deadband)", (em2.ema_fast, em2.ema_slow),
      (1_234_567, 1_234_567))

# death cross direction too
m2 = SMAMirror(fast_n=4, slow_n=8)
for p in (1000,)*8:
    m2.ingest(p)
m2.ingest(5000)                       # force above
s_dn = None
for p in (100, 100, 100, 100):
    s = m2.ingest(p)
    if s:
        s_dn = s
        break
check("death cross fired", s_dn is not None, True)
if s_dn:
    check("side SELL", s_dn.side, SIDE_SELL)

# ---------------------------------------------------------------------------
print("[G2] codec roundtrips")
f = pack_tick(TYPE_TRADE, "AAPL", 1_823_400, 100, 1, 1_750_000_000_123_456)
check("tick frame length", len(f), TICK_LEN)
check("tick SOF/EOF", (f[0], f[-1]), (TICK_SOF, TICK_EOF))
d = parse_tick(f)
check("tick roundtrip", (d["symbol"], d["price_e4"], d["qty"], d["side"],
                         d["host_ts"]),
      ("AAPL  ", 1_823_400, 100, 1, 1_750_000_000_123_456))
check("qty clamps to uint16",
      parse_tick(pack_tick(TYPE_TRADE, "SPY", 1, 999_999, 0, 0))["qty"],
      0xFFFF)
check("3-char symbol pads to 6",
      parse_tick(pack_tick(1, "SPY", 1, 1, 0, 0))["symbol"], "SPY   ")
check("5-char S&P ticker fits",
      parse_tick(pack_tick(1, "GOOGL", 1, 1, 0, 0))["symbol"], "GOOGL ")
check("dotted class shares fit",
      parse_tick(pack_tick(1, "BRK.B", 1, 1, 0, 0))["symbol"], "BRK.B ")
try:
    sym_wire("TOOLONG1")
    check("7-char ticker rejected", "accepted", "rejected")
except ValueError:
    check("7-char ticker rejected", "rejected", "rejected")
# symcfg frame + its 0x90 ack decode
cf = pack_symcfg(3, "QQQ", True)
dc = parse_tick(cf)
check("symcfg frame fields", (dc["type"], dc["symbol"], dc["qty"],
                              dc["side"]), (TYPE_SYMCFG, "QQQ   ", 3, 1))
fpk = FrameParser()
ack = pack_fpga_echo(TYPE_SYMCFG, "QQQ   ", 0, 3, 1, 0, 42)
r = fpk.feed(ack)
check("0x90 ack parses", (r[0]["kind"], r[0]["slot"], r[0]["enabled"],
                          r[0]["symbol"].strip()),
      ("symcfg_ack", 3, True, "QQQ"))

fp = FrameParser()
e = pack_fpga_echo(TYPE_TRADE, "TSLA", 2_489_900, 250, 2,
                   111, 222)
r = fp.feed(e)
check("echo parses", len(r), 1)
check("echo fields", (r[0]["kind"], r[0]["type"], r[0]["symbol"],
                      r[0]["price_e4"], r[0]["host_ts"], r[0]["fpga_ts"]),
      ("echo", 0x81, "TSLA  ", 2_489_900, 111, 222))
s = pack_fpga_signal("SPY ", 3000, SIDE_BUY, 1800, 1775, 999)
r = fp.feed(s)
check("signal parses", len(r), 1)
check("signal tagged union", (r[0]["kind"], r[0]["sma_fast"],
                              r[0]["sma_slow"], r[0]["fpga_ts"]),
      ("signal", 1800, 1775, 999))
check("0x83 tagged sma", r[0]["strategy"], "sma")
r = fp.feed(pack_fpga_signal("SPY ", 3000, SIDE_BUY, 2200, 1884, 999,
                             ftype=TYPE_SIGNAL_EMA))
check("0x84 parses as ema signal", (r[0]["kind"], r[0]["strategy"],
                                    r[0]["sma_fast"], r[0]["sma_slow"]),
      ("signal", "ema", 2200, 1884))

# ---------------------------------------------------------------------------
print("[G3] FrameParser resync torture")
fp = FrameParser()
got = []
stream = (bytes([0x13, 0x37]) + e +          # garbage then a frame
          bytes([0xBB, 0x01, 0x02]) +        # fake SOF, truncated garbage
          s +                                 # real frame right after
          e[:10])                             # trailing partial frame
for b in stream:                              # worst case: one byte at a time
    got.extend(fp.feed(bytes([b])))
got.extend(fp.feed(e[10:]))                   # complete the partial
check("frames recovered from torture", len(got), 3)
check("torture order", [g["kind"] for g in got], ["echo", "signal", "echo"])
check("resyncs counted nonzero", fp.resync_count > 0, True)

# ---------------------------------------------------------------------------
print("[G4] closed loop: Bridge <-> FPGAEmulator over a pty")
from fpga_emulator import FPGAEmulator
from bridge import Bridge

emu = FPGAEmulator(symbol="SPY ", fast_n=4, slow_n=8, ema_kf=1, ema_ks=3)
path = emu.start()
br = Bridge(path, ["SPY"], fast_n=4, slow_n=8, ema_kf=1, ema_ks=3)

rng = random.Random(7)
price = 1_500_000
for _ in range(120):
    price = max(100_000, price + rng.randint(-60_000, 60_000))
    br.send_trade(price, rng.randint(1, 100))
    br.pump(timeout=0.004)
br.pump(timeout=0.5)
time.sleep(0.2)
br.pump(timeout=0.2)

check("all ticks echoed", br.echoes, br.sent)
check("stream needed no resync", br.parser.resync_count, 0)
for name in ("sma", "ema"):
    check(f"[{name}] fpga == model signals",
          br.fpga_by_key.get((name, "SPY"), 0),
          br.models[name]["SPY"].signals)
    check(f"[{name}] every signal verified",
          br.verifiers[(name, "SPY")].verified,
          br.fpga_by_key.get((name, "SPY"), 0))
    check(f"[{name}] zero divergences",
          br.verifiers[(name, "SPY")].divergences, 0)
    check(f"[{name}] signals occurred",
          br.fpga_by_key.get((name, "SPY"), 0) > 0, True)
print(f"       {br.sent} ticks; sma {br.fpga_by_strategy['sma']} / "
      f"ema {br.fpga_by_strategy['ema']} signals, all verified")

# ---- v2: runtime reconfiguration to TWO symbols over the wire ----------
check("configure_symbols acked", br.configure_symbols(["SPY", "QQQ"]), True)
walk2 = {"SPY": 1_500_000, "QQQ": 4_000_000}
for i in range(160):
    t = ("SPY", "QQQ")[i % 2]
    walk2[t] = max(100_000, walk2[t] + rng.randint(-60_000, 60_000))
    br.send_trade(walk2[t], 1, symbol=t)
    br.pump(timeout=0.004)
br.pump(timeout=0.5); time.sleep(0.2); br.pump(timeout=0.2)
for t in ("SPY", "QQQ"):
    for name in ("sma", "ema"):
        check(f"[{name} {t}] fpga == model",
              br.fpga_by_key.get((name, t), 0), br.models[name][t].signals)
        check(f"[{name} {t}] no divergence",
              br.verifiers[(name, t)].divergences, 0)
check("both symbols produced signals",
      all(br.models["sma"][t].signals + br.models["ema"][t].signals > 0
          for t in ("SPY", "QQQ")), True)
print(f"       reconfig phase: per-key counts {br.fpga_by_key}")

# scorecards score both strategies from the same verified stream
from compare import StrategyScorecard, ProfitGatedScorecard, comparison_report
cards = {"sma": StrategyScorecard("SMA 4/8"),
         "ema": StrategyScorecard("EMA")}
for name, v in br.verifiers.items():
    pass  # signals already consumed; replay from counters is not possible —
          # scorecard math is checked directly below instead
sc = StrategyScorecard("T")
sc.on_signal({"side": SIDE_BUY, "price_e4": 1_000_000, "strategy": "sma"})
sc.on_signal({"side": SIDE_BUY, "price_e4": 2_000_000, "strategy": "sma"})
check("scorecard ignores buy-while-open", sc.open_e4, 1_000_000)
sc.on_signal({"side": SIDE_SELL, "price_e4": 1_100_000, "strategy": "sma"})
check("scorecard trip pnl $10", sc.pnl_e4, 100_000)
check("scorecard win counted", (sc.trips, sc.wins), (1, 1))
check("scorecard fees charged", sc.fees_usd > 0, True)
check("scorecard flat after close", sc.open_e4, None)
check("report renders", "strategy comparison" in
      comparison_report({"t": sc}), True)

# ---- v2.1: gated replay through a RiskPolicy clone ---------------------
from order_manager import RiskPolicy, RiskLimits
tight = RiskLimits(order_qty=1, max_shares=10, max_notional_e4=10**12,
                   max_orders_per_day=99, cooldown_s=1000.0,
                   require_market_hours=False)
gated = StrategyScorecard("G", policy=RiskPolicy(tight))
gated.on_signal({"side": SIDE_BUY, "price_e4": 1_000_000, "symbol": "SPY",
                 "strategy": "ema"})
check("gated: first signal allowed", (gated.trips, gated.blocked), (0, 0))
check("gated: position opened", gated.positions.get("SPY"), 1)
gated.on_signal({"side": SIDE_SELL, "price_e4": 2_000_000, "symbol": "SPY",
                 "strategy": "ema"})
check("gated: cooldown blocks the very next signal",
      (gated.trips, gated.blocked), (0, 1))
check("gated: block reason is cooldown",
      "cooldown" in next(iter(gated.block_reasons)), True)
check("gated: position still open (sell was blocked)",
      gated.positions.get("SPY"), 1)

loose = RiskLimits(order_qty=1, max_shares=10, max_notional_e4=10**12,
                   max_orders_per_day=99, cooldown_s=0.0,
                   require_market_hours=False)
gated2 = StrategyScorecard("G2", policy=RiskPolicy(loose))
gated2.on_signal({"side": SIDE_BUY, "price_e4": 1_000_000, "symbol": "SPY",
                  "strategy": "ema"})
gated2.on_signal({"side": SIDE_SELL, "price_e4": 1_100_000, "symbol": "SPY",
                  "strategy": "ema"})
check("gated: zero-cooldown trip completes", gated2.trips, 1)
check("gated: fee applied", gated2.fees_usd > 0, True)
r = comparison_report({"live": StrategyScorecard("L", live=True, trips=3),
                       "gated": gated})
check("report tags LIVE row", "[LIVE]" in r, True)
check("report shows gated count", "1 gated" in r, True)
check("report explains block reasons", "gated-away signals" in r, True)

# ---- v3.3: ProfitGatedScorecard -- refuses to sell at a loss ------------
print("[G_pg] profit-gated shadow strategy: sells only above cost basis")
pg = ProfitGatedScorecard("SMA profit-gated")
check("buy proceeds exactly like the base class",
      pg.on_signal({"side": SIDE_BUY, "price_e4": 1_000_000,
                   "symbol": "SPY", "strategy": "sma"}),
      "FILLED (scored)")
check("position opened", pg.positions.get("SPY"), 1)

# a sell BELOW cost basis must be suppressed, not executed
out = pg.on_signal({"side": SIDE_SELL, "price_e4": 900_000,
                    "symbol": "SPY", "strategy": "sma"})
check("a losing sell is refused, not silently executed",
      out.startswith("gated: would realize a loss"), True)
check("position REMAINS OPEN after a suppressed sell",
      pg.positions.get("SPY"), 1)
check("no trip was recorded for the suppressed sell",
      pg.trips, 0)
check("the suppression is tracked in block_reasons (shows up "
     "automatically in the report's gated-away breakdown)",
     pg.block_reasons.get("would realize a loss"), 1)

# an EXACT breakeven sell (price == entry) must ALSO be refused --
# "higher than" means strictly above, not "at least"
out2 = pg.on_signal({"side": SIDE_SELL, "price_e4": 1_000_000,
                     "symbol": "SPY", "strategy": "sma"})
check("an exact breakeven sell is also refused (strictly ABOVE cost "
     "basis, not >=)", out2.startswith("gated: would realize a loss"),
     True)
check("still open, still zero trips", (pg.positions.get("SPY"), pg.trips),
      (1, 0))

# price recovers above cost basis: NOW the sell goes through
out3 = pg.on_signal({"side": SIDE_SELL, "price_e4": 1_100_000,
                     "symbol": "SPY", "strategy": "sma"})
check("a profitable sell executes normally once price recovers",
      out3, "FILLED (scored)")
check("position closes", pg.positions.get("SPY"), 0)
check("exactly one trip recorded, and it's a win (always true by "
     "construction, since a loss can never be realized here)",
     (pg.trips, pg.wins), (1, 1))
check("realized gain matches (exit - entry) x qty",
      pg.pnl_e4, 100_000)

# weighted-average cost basis across multiple buys works the same as
# the base class — the profit gate compares against the BLENDED entry.
# Accumulation only happens through the policy-gated path (matching
# the max_shares fix elsewhere in this project) — ungated mode
# deliberately preserves single-lot semantics, so this needs a policy.
pg2_limits = RiskLimits(order_qty=1, max_shares=5, max_notional_e4=10**13,
                       max_orders_per_day=99, cooldown_s=0.0,
                       require_market_hours=False)
pg2 = ProfitGatedScorecard("SMA profit-gated", policy=RiskPolicy(pg2_limits))
pg2.on_signal({"side": SIDE_BUY, "price_e4": 1_000_000, "symbol": "SPY",
              "strategy": "sma"})
pg2.on_signal({"side": SIDE_BUY, "price_e4": 1_200_000, "symbol": "SPY",
              "strategy": "sma"})
avg = (1_000_000 + 1_200_000) // 2
check("blended cost basis across both buys", pg2.opens.get("SPY"), avg)
out4 = pg2.on_signal({"side": SIDE_SELL, "price_e4": avg, "symbol": "SPY",
                      "strategy": "sma"})
check("sell at exactly the blended average is still refused "
     "(strictly above, not at)",
     out4.startswith("gated: would realize a loss"), True)
out5 = pg2.on_signal({"side": SIDE_SELL, "price_e4": avg + 1,
                      "symbol": "SPY", "strategy": "sma"})
check("one cent above the blended average finally clears the gate",
      out5, "FILLED (scored)")

# a suppressed sell must NOT consume the daily order count or the
# cooldown timer -- it never became a real order at all. Use zero
# cooldown here so the cooldown check itself can't confound the
# result; verify via RiskPolicy's own counters directly instead.
pg3_limits = RiskLimits(order_qty=1, max_shares=5, max_notional_e4=10**13,
                       max_orders_per_day=99, cooldown_s=0.0,
                       require_market_hours=False)
pg3_policy = RiskPolicy(pg3_limits)
pg3 = ProfitGatedScorecard("SMA profit-gated", policy=pg3_policy)
pg3.on_signal({"side": SIDE_BUY, "price_e4": 1_000_000, "symbol": "SPY",
              "strategy": "sma"})
check("one order recorded after the buy", pg3_policy.orders_today, 1)
out6 = pg3.on_signal({"side": SIDE_SELL, "price_e4": 900_000,
                      "symbol": "SPY", "strategy": "sma"})
check("suppressed sell (loss) reported as such",
      out6.startswith("gated: would realize a loss"), True)
check("the suppressed sell did NOT increment the daily order count "
     "-- it never became a real order, unlike an actual rejected one",
     pg3_policy.orders_today, 1)
out7 = pg3.on_signal({"side": SIDE_SELL, "price_e4": 1_100_000,
                      "symbol": "SPY", "strategy": "sma"})
check("a profitable sell right afterward executes normally -- "
     "confirms the suppressed loss-sell consumed nothing that would "
     "have blocked it", out7, "FILLED (scored)")
check("NOW the order count reflects both real orders (buy + sell)",
      pg3_policy.orders_today, 2)



# v2.4: block-reason breakdown now shows for ANY gated card, regardless
# of the [LIVE] label -- fixes a real backtest reporting gap (a
# backtest's "[LIVE]"-labeled row is still a gated replay, not real
# fills, so hiding its own breakdown hid the most useful diagnostic)
live_but_gated = StrategyScorecard("X", live=True, policy=RiskPolicy(tight))
live_but_gated.on_signal({"side": SIDE_BUY, "price_e4": 1_000_000,
                          "symbol": "SPY", "strategy": "sma"})
live_but_gated.on_signal({"side": SIDE_SELL, "price_e4": 1_100_000,
                          "symbol": "SPY", "strategy": "sma"})  # cooldown blocks
r2 = comparison_report({"x": live_but_gated})
check("breakdown now shows even for a [LIVE]-labeled gated card",
      "gated-away signals" in r2, True)
br.close()
emu.stop()

# ---------------------------------------------------------------------------
print("[G5] run_selftest passes against a dual-engine board")
import io, contextlib
from bridge import run_selftest
emu5 = FPGAEmulator(symbol="SPY ", fast_n=8, slow_n=32)
br5 = Bridge(emu5.start(), ["SPY"], fast_n=8, slow_n=32)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    run_selftest(br5)
out5 = buf.getvalue()
check("selftest PASS on healthy board", "[selftest] PASS" in out5, True)
check("both strategies signaled once",
      (br5.fpga_by_key.get(("sma", "SPY"), 0),
       br5.fpga_by_key.get(("ema", "SPY"), 0)), (1, 1))
check("all verified, no divergence",
      (sum(v.verified for v in br5.verifiers.values()),
       sum(v.divergences for v in br5.verifiers.values())), (3, 0))
# ^ was (2, 0) pre-v3.19: sma + ema. The selftest tape's dive-and-recover
# shape ALSO fires the new vwap_bounce mirror+emulator pair once, and
# they verify against each other -- 3 verified is the correct total now
br5.close(); emu5.stop()

# ---- v2.2: per-symbol grace window (fixes a real divergence bug) ----------
print("[G7] grace window is REAL ELAPSED TIME, not an echo count — "
     "a burst of many echoes with almost no real time passing must NOT "
     "expire a pending signal, and real time passing DOES expire one "
     "regardless of how few echoes happened to occur")
from bridge import SignalVerifier
from tick_protocol import SMASignal, SIDE_BUY

# A model signal goes pending at t=0. A BURST of 50 echoes, but with the
# clock barely moving (simulating many ticks arriving within
# milliseconds — exactly the reported real-world scenario: multiple
# symbols firing, the daily order cap already maxed out, signals piling
# up) must NOT expire it, because almost no REAL TIME has passed.
v = SignalVerifier(symbol="QQQ", strategy="sma", min_grace_s=2.0)
qqq_sig = SMASignal(side=SIDE_BUY, price_e4=4_000_000, sma_fast=1, sma_slow=2)
v.on_model_signal(qqq_sig, t=0.0, echo_n=0)
for i in range(1, 51):
    v.on_echo(t=0.05, echo_n=i)     # 50 echoes, clock barely moved (50ms)
check("a burst of 50 echoes in ~50ms does NOT expire the pending signal "
     "-- this is the actual reported bug: the old echo-COUNT grace "
     "window had no fixed real-time meaning and could be exhausted "
     "almost instantly during a burst", v.divergences, 0)
check("still pending, correctly", len(v.pending_model), 1)

# Now real time genuinely passes (2.5s > min_grace_s=2.0), even with NO
# further echoes at all -- it must still expire, because real elapsed
# time is what actually matters, not echo traffic
v.on_echo(t=2.5, echo_n=51)
check("once REAL TIME exceeds min_grace_s, it expires -- correctly, "
     "this time for a genuine reason (elapsed time), not an artifact "
     "of counting", v.divergences, 1)

# ---- the divergence info dict must carry full diagnostic detail ---------
print("[G7b] a divergence carries rich diagnostic detail, not just a "
     "one-line reason -- the actual second half of what was asked for")
captured = []
v2 = SignalVerifier(symbol="RKLB", strategy="ema", min_grace_s=1.0,
                    on_divergence=lambda info: captured.append(info))
v2.on_model_signal(qqq_sig, t=10.0, echo_n=5)
v2.on_echo(t=12.0, echo_n=9)        # 2s later, 4 echoes elapsed
check("exactly one divergence captured", len(captured), 1)
info = captured[0]
check("reason identifies which side was orphaned",
      info["reason"], "orphan model signal")
check("symbol is carried through", info["symbol"], "RKLB")
check("strategy is carried through", info["strategy"], "ema")
check("waited_s reflects the real elapsed time (~2.0s), not an echo count",
      abs(info["waited_s"] - 2.0) < 0.01, True)
check("echoes_elapsed is ALSO recorded, as useful extra context "
     "(not the basis for the decision, just diagnostic detail)",
      info["echoes_elapsed"], 4)
check("the actual signal contents are captured, not just that "
     "something diverged", (info["side"], info["price_e4"], info["sma_fast"],
                            info["sma_slow"]),
      (SIDE_BUY, 4_000_000, 1, 2))

# an SMA-mismatch divergence (not just an orphan) must ALSO carry rich,
# comparable detail from BOTH sides
captured2 = []
v3 = SignalVerifier(symbol="SPY", strategy="sma",
                    on_divergence=lambda info: captured2.append(info))
fpga_fr = {"side": SIDE_BUY, "price_e4": 1_000_000, "sma_fast": 10,
          "sma_slow": 20, "symbol": "SPY  "}
mismatched_sig = SMASignal(side=SIDE_BUY, price_e4=1_000_000,
                          sma_fast=99, sma_slow=20)   # fast disagrees
v3.on_fpga_signal(fpga_fr, t=0.0, echo_n=0)
v3.on_model_signal(mismatched_sig, t=0.0, echo_n=0)
check("mismatch detected immediately (both sides present, values differ)",
      len(captured2), 1)
info2 = captured2[0]
check("mismatch reason is distinct from an orphan", info2["reason"],
      "sma mismatch")
# ^ v3.19: the reason string now carries the verifier's own strategy name
# (f"{strategy} mismatch") so vwap divergences aren't mislabeled "SMA"
check("both FPGA's and the model's conflicting values are captured "
     "side by side, not just 'they disagreed'",
     (info2["fpga_sma_fast"], info2["model_sma_fast"]), (10, 99))

# ---- integration: bridge tracks echoes_by_symbol, not just the global ----
print("[G8] Bridge exposes a real per-symbol counter, reset on reconfigure")
emu2 = FPGAEmulator(symbol="SPY", fast_n=4, slow_n=8, ema_kf=1, ema_ks=3)
path2 = emu2.start()
br2 = Bridge(path2, ["SPY", "QQQ"], fast_n=4, slow_n=8, ema_kf=1, ema_ks=3)
check("echoes_by_symbol starts empty", br2.echoes_by_symbol, {})
w = {"SPY": 1_500_000, "QQQ": 5_000_000}
for i in range(40):
    t = "SPY" if i % 4 else "QQQ"           # SPY ticks 3x more often
    w[t] += rng.randint(-5_000, 5_000)
    br2.send_trade(w[t], 1, symbol=t)
    br2.pump(timeout=0.003)
br2.pump(timeout=0.3)
check("both symbols have their OWN counters",
      set(br2.echoes_by_symbol.keys()) >= {"SPY", "QQQ"}, True)
check("SPY's count reflects its own higher tick share",
      br2.echoes_by_symbol["SPY"] > br2.echoes_by_symbol["QQQ"], True)
check("reconfigure resets the per-symbol counters",
      br2.configure_symbols(["SPY", "QQQ"]) and br2.echoes_by_symbol, {})
br2.close(); emu2.stop()

# ---------------------------------------------------------------------------
print("[G9] v3.19 VWAP host side: protocol, mirror fidelity vs the RTL, "
     "verifier, session reset")
# ---------------------------------------------------------------------------
from tick_protocol import (VWAPMirror, VWAPSignal, pack_sessrst,
                          parse_tick, pack_fpga_signal,
                          _decode_fpga_frame as parse_fpga_frame,
                          TYPE_SESSRST, TYPE_SIGNAL_VWAP)

# -- protocol: the 0x11 frame round-trips through the tick parser
frb = pack_sessrst()                       # broadcast
t = parse_tick(frb)
check("sessrst broadcast: type 0x11", t["type"], TYPE_SESSRST)
check("sessrst broadcast: side 0xFF", t["side"], 0xFF)
frs = pack_sessrst(slot=5)
t = parse_tick(frs)
check("sessrst slot form: slot in qty[2:0]", t["qty"] & 7, 5)
check("sessrst slot form: side is not broadcast", t["side"], 0x01)

# -- protocol: 0x85 signal frames parse with vwap + eval_skips
sf = pack_fpga_signal("QQQ", 3_975_000, 1, 3_988_909, 7, 123456,
                      TYPE_SIGNAL_VWAP)
fr = parse_fpga_frame(sf)
check("0x85 parses as kind=signal", fr["kind"], "signal")
check("0x85 strategy name", fr["strategy"], "vwap_bounce")
check("0x85 vwap payload field", fr["vwap"], 3_988_909)
check("0x85 eval_skips payload field", fr["eval_skips"], 7)

# -- mirror fidelity: replay tb_vwap_integration.sv's EXACT tape and
# demand the EXACT events its DUT emitted in the bit-level RTL sim
# (BUY @3975000 vwap=3988909, SELL @4020000 vwap=3991500) — the
# three-implementations-agree check, now spanning fabric to Python
m = VWAPMirror(warmup_n=6)
g9_events = []
for i in range(8):
    s = m.ingest(4_000_000 + i * 1000, 10)
    if s: g9_events.append(s)
for p in (3_940_000, 3_935_000, 3_975_000, 4_020_000):
    s = m.ingest(p, 10)
    if s: g9_events.append(s)
check("mirror fires exactly the RTL sim's two events", len(g9_events), 2)
check("BUY at the bounce", g9_events[0].side, SIDE_BUY)
check("BUY vwap == the RTL sim's exact value",
      g9_events[0].vwap, 3_988_909)
check("SELL at the vwap cross", g9_events[1].side, SIDE_SELL)
check("SELL vwap == the RTL sim's exact value",
      g9_events[1].vwap, 3_991_500)

# -- sess_reset really clears (mirrors tb V6)
m.sess_reset()
check("sess_reset zeroes the tick count", m.ticks, 0)
g9_replay = [m.ingest(4_000_000 + i * 1000, 10) for i in range(8)]
check("re-warm-up fires nothing (matches the RTL's V6)",
      any(s is not None for s in g9_replay), False)

# -- SELL dominance on a gap through both edges (mirrors tb_vwap P7)
m2 = VWAPMirror(warmup_n=8)
for i in range(10):
    m2.ingest(4_004_000 if i % 2 == 0 else 3_996_000, 10)
m2.ingest(3_940_000, 10); m2.ingest(3_935_000, 10)
s = m2.ingest(4_050_000, 10)
check("gap through band AND vwap -> exactly the SELL",
      s is not None and s.side == SIDE_SELL, True)

# -- verifier: matches on (side, price, vwap); eval_skips EXCLUDED
from bridge import SignalVerifier
v = SignalVerifier(symbol="QQQ", strategy="vwap_bounce", min_grace_s=5.0)
good = dict(parse_fpga_frame(pack_fpga_signal(
    "QQQ", 3_975_000, SIDE_BUY, 3_988_909, 99, 1, TYPE_SIGNAL_VWAP)))
v.on_fpga_signal(good, 0.0, 0)
v.on_model_signal(VWAPSignal(side=SIDE_BUY, price_e4=3_975_000,
                             vwap=3_988_909), 0.0, 0)
check("vwap signal verifies on content", v.verified, 1)
check("no divergence on the match", v.divergences, 0)
check("eval_skips=99 did NOT block the match (telemetry, not math)",
      v.verified, 1)
bad = dict(good); bad["vwap"] = 3_988_910          # off by one
v.on_fpga_signal(bad, 0.0, 0)
v.on_model_signal(VWAPSignal(side=SIDE_BUY, price_e4=3_975_000,
                             vwap=3_988_909), 0.0, 0)
check("a one-count vwap mismatch IS a divergence", v.divergences, 1)

# -- emulator end to end: sessctl + 0x85 through a real pty bridge
emu3 = FPGAEmulator(symbol="QQQ", fast_n=4, slow_n=8)
port3 = emu3.start()
br3 = Bridge(port3, "QQQ", 4, 8, vwap_warmup=20)
time.sleep(0.2)
check("send_sessrst is acked end to end", br3.send_sessrst(), True)
# drive the integration tape at qty=10 through the emulator; the
# emulator's VWAPMirror (warmup 20) needs 20 ticks — use a 20-warm tape
for i in range(22):
    br3.send_trade(4_000_000 + i * 1000, 10, symbol="QQQ")
for p in (3_900_000, 3_890_000):
    br3.send_trade(p, 10, symbol="QQQ")
br3.send_trade(3_975_000, 10, symbol="QQQ")   # bounce: computed with the
                                              # mirror itself — the dives
                                              # inflate sigma to ~32.5k, so
                                              # 3.975M (diff ~25k) is back
                                              # INSIDE the band yet below
                                              # vwap (3999840) -> BUY;
                                              # 3.96M would still be BELOW
                                              # the band (diff ~40k) and
                                              # fire nothing
deadline = time.time() + 5
while br3.fpga_by_strategy["vwap_bounce"] < 1 and time.time() < deadline:
    br3.pump(timeout=0.05)
check("emulator emitted a vwap_bounce signal", 
      br3.fpga_by_strategy["vwap_bounce"] >= 1, True)
vv = br3.verifiers[("vwap_bounce", "QQQ")]
deadline = time.time() + 3
while vv.verified < 1 and time.time() < deadline:
    br3.pump(timeout=0.05)
check("and the bridge VERIFIED it against its own mirror",
      vv.verified >= 1, True)
check("zero divergences on the emulated path", vv.divergences, 0)
br3.close(); emu3.stop()

# ---------------------------------------------------------------------------
print("[G11] v3.30: non-standard-baud fallback for non-hardware serial "
     "connections (found on macOS, connecting order_manager.py to "
     "fpga_emulator.py — a pty has no real UART, so macOS correctly "
     "refuses the special ioctl a non-standard baud rate needs, "
     "ENOTTY/errno 25)")

import bridge as bridge_module

class _FakeSerialENOTTY:
    """Simulates pyserial's real macOS failure mode exactly: raises the
    identical bare OSError(errno=25) on a non-standard baud, succeeds
    immediately at 115200 — same as the real serialposix.py path this
    mirrors (see bridge.py's v3.30 comment for the full mechanism)."""
    calls = []
    def __init__(self, port, baud, timeout=0.05):
        _FakeSerialENOTTY.calls.append(baud)
        if baud != 115200:
            raise OSError(25, "Inappropriate ioctl for device")
        self.port = port
        self.baud = baud
    def write(self, data): return len(data)
    def read(self, n=1): return b""
    def close(self): pass

real_serial_Serial = bridge_module.serial.Serial
bridge_module.serial.Serial = _FakeSerialENOTTY
_FakeSerialENOTTY.calls.clear()
try:
    br_fallback = bridge_module.Bridge("/fake/pty", "SPY", 8, 32,
                                       baud=921_600)
    check("falls back and succeeds despite the non-standard baud "
         "failing first",
         isinstance(br_fallback.ser, _FakeSerialENOTTY), True)
    check("tried the REQUESTED baud first, not silently skipping it",
          _FakeSerialENOTTY.calls[0], 921_600)
    check("fell back to exactly 115200, not some other guessed value",
          _FakeSerialENOTTY.calls[1], 115200)
    check("exactly two attempts -- no retry storm",
          len(_FakeSerialENOTTY.calls), 2)
finally:
    bridge_module.serial.Serial = real_serial_Serial

# regression: an OSError with a DIFFERENT errno (a real hardware/driver
# problem, not the ENOTTY special-baud case) must NOT be silently
# swallowed by this fallback -- it has to propagate normally
class _FakeSerialOtherError:
    def __init__(self, port, baud, timeout=0.05):
        raise OSError(5, "Input/output error")   # a different errno

bridge_module.serial.Serial = _FakeSerialOtherError
try:
    threw = False
    try:
        bridge_module.Bridge("/fake/pty", "SPY", 8, 32, baud=921_600)
    except OSError as e:
        threw = (e.errno == 5)
    check("an unrelated OSError (different errno) is NOT swallowed -- "
         "propagates normally, exactly as before this fix existed",
         threw, True)
finally:
    bridge_module.serial.Serial = real_serial_Serial

# regression: requesting 115200 itself and hitting errno 25 anyway
# (a genuinely broken device, not the pty case) must not retry at the
# exact same value that just failed -- propagates immediately
class _FakeSerialAlways25:
    def __init__(self, port, baud, timeout=0.05):
        raise OSError(25, "Inappropriate ioctl for device")

bridge_module.serial.Serial = _FakeSerialAlways25
try:
    threw = False
    try:
        bridge_module.Bridge("/fake/pty", "SPY", 8, 32, baud=115_200)
    except OSError as e:
        threw = (e.errno == 25)
    check("baud=115200 hitting errno 25 doesn't retry at the same "
         "value (would just fail identically) -- propagates instead",
         threw, True)
finally:
    bridge_module.serial.Serial = real_serial_Serial

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
