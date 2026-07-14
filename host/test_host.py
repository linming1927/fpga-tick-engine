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
from compare import StrategyScorecard, comparison_report
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
       sum(v.divergences for v in br5.verifiers.values())), (2, 0))
br5.close(); emu5.stop()

# ---------------------------------------------------------------------------
print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
