#!/usr/bin/env python3
"""
bridge.py — host bridge between a tick source and the FPGA tick engine.

                 +--------------------------------------------+
   tick source   |                 bridge.py                   |     FPGA
  (sim / alpaca) | send: pack 22-byte frames  ---------------> | uart_rx
                 |                                             | tick_parser
                 | recv thread: FrameParser  <---------------- | frame_tx
                 |   0x81/0x82 echo -> mirror model ingest     |
                 |   0x83 signal   -> SignalVerifier           |
                 |   latency stats, JSONL log                  |
                 +--------------------------------------------+

Three sources:
  --source sim       synthetic random-walk trades (no credentials, works
                     against the real board or fpga_emulator.py)
  --source selftest  scripted warm-up + spike that must produce exactly one
                     BUY signal whose SMAs match the local model — a
                     hardware acceptance test for a freshly built bitstream
  --source alpaca    live trades from Alpaca's IEX websocket feed
                     (needs ALPACA_KEY / ALPACA_SECRET env vars and the
                     `websocket-client` package)

VERIFICATION DESIGN (the mirror-model contract)
-----------------------------------------------
The local SMAMirror ingests a trade only when its ECHO frame returns —
i.e., only ticks the FPGA provably decoded. This keeps host and hardware
models in lockstep even if a frame were lost on the wire (a lost frame
produces no echo and updates neither model).

Signals are matched by CONTENT, not arrival order, because the wire order
is legitimately ambiguous: the signal FIFO has priority at the serializer,
so an 0x83 can overtake queued echoes — but when the TX path is idle, the
echo of the triggering tick goes out first. The SignalVerifier therefore
keeps two pending queues (FPGA-emitted, model-predicted) and matches
heads whenever both are non-empty; a signal stranded unmatched for more
than GRACE echoes raises a divergence — the host-side analogue of a TMR
voter disagreeing.

Caveat (documented, not hidden): under sustained input saturation the
FPGA drops ECHOES (tx_drop_count) while its engine still processes the
tick — the echo-driven model would then fall behind. At realistic tick
rates this cannot occur (echo path saturates ~520 ticks/s); the bridge
warns if send/echo accounting drifts, which is the observable symptom.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tick_protocol import (FrameParser, SMAMirror, EMAMirror, SMASignal,
                           pack_tick, pack_symcfg, dollars, to_e4,
                           TYPE_TRADE, TYPE_ECHO_TRADE,
                           SIDE_NEUTRAL, SIDE_NAME)

try:
    import serial
except ImportError:
    sys.exit("pyserial is required:  pip3 install pyserial --break-system-packages")


def now_us() -> int:
    return time.time_ns() // 1000


# ---------------------------------------------------------------------------
# Signal verifier — order-agnostic content matching with a grace window
# ---------------------------------------------------------------------------
@dataclass
class SignalVerifier:
    grace: int = 3                       # echoes a pending signal may wait
    pending_fpga: list = field(default_factory=list)
    pending_model: list = field(default_factory=list)
    verified: int = 0
    divergences: int = 0
    on_verified: object = None           # callback(fr) — a VERIFIED FPGA signal
    on_divergence: object = None         # callback(info) — any mismatch/orphan

    def on_fpga_signal(self, fr: dict, echo_seq: int):
        self.pending_fpga.append((echo_seq, fr))
        self._match()

    def on_model_signal(self, sig: SMASignal, echo_seq: int):
        self.pending_model.append((echo_seq, sig))
        self._match()

    def on_echo(self, echo_seq: int):
        """Called per echo: expire anything waiting longer than grace."""
        for name, pend in (("FPGA", self.pending_fpga),
                           ("model", self.pending_model)):
            while pend and echo_seq - pend[0][0] > self.grace:
                _, item = pend.pop(0)
                self.divergences += 1
                other = "model" if name == "FPGA" else "FPGA"
                print(f"!! DIVERGENCE: {name} signal never matched by "
                      f"{other}: {item}")
                if self.on_divergence:
                    self.on_divergence({"reason": f"orphan {name} signal",
                                        "detail": str(item)})

    def _match(self):
        while self.pending_fpga and self.pending_model:
            _, fr = self.pending_fpga.pop(0)
            _, sig = self.pending_model.pop(0)
            ok = (fr["side"] == sig.side
                  and fr["price_e4"] == sig.price_e4
                  and fr["sma_fast"] == sig.sma_fast
                  and fr["sma_slow"] == sig.sma_slow)
            if ok:
                self.verified += 1
                print(f"   verified: FPGA SMAs {fr['sma_fast']}/"
                      f"{fr['sma_slow']} == model — hardware math confirmed")
                if self.on_verified:
                    self.on_verified(fr)
            else:
                self.divergences += 1
                print(f"!! DIVERGENCE: FPGA {fr} vs model {sig}")
                if self.on_divergence:
                    self.on_divergence({"reason": "SMA mismatch",
                                        "fpga": fr, "model": str(sig)})


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------
class Bridge:
    def __init__(self, port: str, symbols, fast_n: int, slow_n: int,
                 ema_kf: int = 3, ema_ks: int = 5,
                 baud: int = 115_200, log_path: str | None = None):
        if isinstance(symbols, str):
            symbols = [symbols]
        self.symbols = [t.strip().upper() for t in symbols][:8]
        self.symbol = self.symbols[0]            # primary (OM default)
        self.params = (fast_n, slow_n, ema_kf, ema_ks)
        self.ser = serial.Serial(port, baud, timeout=0.05)
        # one mirror model + one verifier PER (strategy, symbol) — models
        # must match the bitstream's engine parameters, symbols must match
        # the slot register file (configure_symbols keeps them in lockstep)
        self._build_models()
        self.symcfg_acks: dict[int, dict] = {}
        self.on_symbols_changed = None            # alpaca resubscribe hook
        self.parser = FrameParser()
        self.frames: queue.Queue = queue.Queue()
        self.log = open(log_path, "a") if log_path else None

        self.sent = 0
        self.echoes = 0
        self.fpga_signals = 0                    # total across strategies
        self.fpga_by_strategy = {"sma": 0, "ema": 0}
        self.rtt_us: list[int] = []
        self.min_offset: int | None = None   # min(host_ts - fpga_ts) seen

        self.on_echo = None                  # optional hook (dashboard)
        self.on_verified = None              # fan-out hooks set by the app
        self.on_divergence = None

        self._stop = threading.Event()
        self._rx = threading.Thread(target=self._reader, daemon=True)
        self._rx.start()

    def _build_models(self):
        # per-key signal counters share the models' lifetime: a slot
        # reconfiguration starts models, verifiers, AND counts fresh, so
        # the summary's fpga-vs-model comparison is always like-for-like
        # (session totals live in fpga_signals / fpga_by_strategy)
        self.fpga_by_key = {}
        f, sl, kf, ks = self.params
        self.models = {
            "sma": {t: SMAMirror(fast_n=f, slow_n=sl) for t in self.symbols},
            "ema": {t: EMAMirror(k_fast=kf, k_slow=ks, warmup_n=sl)
                    for t in self.symbols}}
        self.verifiers = {(st, t): SignalVerifier()
                          for st in ("sma", "ema") for t in self.symbols}
        for v in self.verifiers.values():
            v.on_verified = lambda fr: (self.on_verified and
                                        self.on_verified(fr))
            v.on_divergence = lambda i: (self.on_divergence and
                                         self.on_divergence(i))

    def configure_symbols(self, symbols: list[str],
                          timeout: float = 3.0) -> bool:
        """Write the FPGA's 8 slots over UART and wait for all 8 ACKs
        (the 0x90 echoes). Also rebuilds the mirror models — hardware and
        host change symbol sets atomically, or the call reports failure.
        NOTE: reconfiguring resets model warm-up; that's inherent (a new
        symbol has no window history on either side)."""
        symbols = list(dict.fromkeys(t.strip().upper()
                                     for t in symbols if t.strip()))[:8]
        if not symbols:
            return False
        self.symcfg_acks = {}
        for slot in range(8):
            if slot < len(symbols):
                self.ser.write(pack_symcfg(slot, symbols[slot], True,
                                           now_us()))
            else:
                self.ser.write(pack_symcfg(slot, "X", False, now_us()))
        deadline = time.monotonic() + timeout
        while len(self.symcfg_acks) < 8 and time.monotonic() < deadline:
            self.pump(timeout=0.05)
        ok = (len(self.symcfg_acks) == 8 and
              all(self.symcfg_acks[i]["symbol"].strip() == symbols[i]
                  and self.symcfg_acks[i]["enabled"]
                  for i in range(len(symbols))) and
              all(not self.symcfg_acks[i]["enabled"]
                  for i in range(len(symbols), 8)))
        if ok:
            self.symbols = symbols
            self.symbol = symbols[0]
            self._build_models()
            print(f"[bridge] FPGA slots configured + acked: {symbols}")
            if self.on_symbols_changed:
                self.on_symbols_changed(symbols)
        else:
            print(f"[bridge] symbol configuration FAILED "
                  f"({len(self.symcfg_acks)}/8 acks)")
        return ok

    # ---- serial RX thread: bytes -> frames -> queue --------------------------
    def _reader(self):
        while not self._stop.is_set():
            try:
                data = self.ser.read(256)
            except (serial.SerialException, OSError):
                return
            if data:
                for fr in self.parser.feed(data):
                    self.frames.put(fr)

    # ---- TX ------------------------------------------------------------------
    def send_trade(self, price_e4: int, qty: int,
                   side: int = SIDE_NEUTRAL, symbol: str | None = None) -> int:
        ts = now_us()
        self.ser.write(pack_tick(TYPE_TRADE, symbol or self.symbol,
                                 price_e4, qty, side, ts))
        self.sent += 1
        return ts

    # ---- frame processing (call from the main thread) --------------------------
    def pump(self, timeout: float = 0.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                remaining = max(0.0, deadline - time.monotonic())
                fr = self.frames.get(timeout=remaining) if timeout else \
                     self.frames.get_nowait()
            except queue.Empty:
                return
            self._handle(fr)
            if not timeout:
                continue

    def _handle(self, fr: dict):
        if fr["kind"] == "echo":
            self.echoes += 1
            t_recv = now_us()
            rtt = t_recv - fr["host_ts"]
            self.rtt_us.append(rtt)
            off = fr["host_ts"] - fr["fpga_ts"]
            if self.min_offset is None or off < self.min_offset:
                self.min_offset = off
            if self.log:
                self.log.write(json.dumps(
                    {"t": t_recv, **{k: fr[k] for k in
                     ("type", "symbol", "price_e4", "qty", "side",
                      "host_ts", "fpga_ts")}, "rtt_us": rtt}) + "\n")

            # echo-driven model updates, both strategies, keyed by the
            # tick's own symbol — the same accept filter the hardware
            # slot compare applies
            sym = fr["symbol"].strip()
            if fr["type"] == TYPE_ECHO_TRADE and sym in self.models["sma"]:
                for name in ("sma", "ema"):
                    sig = self.models[name][sym].ingest(fr["price_e4"])
                    if sig:
                        print(f">> model[{name}] {sym}: {sig.side_name} @ "
                              f"${dollars(sig.price_e4):.4f}  "
                              f"fast={sig.sma_fast} slow={sig.sma_slow}")
                        self.verifiers[(name, sym)].on_model_signal(
                            sig, self.echoes)
            for v in self.verifiers.values():
                v.on_echo(self.echoes)
            if self.on_echo:
                self.on_echo(fr)

        elif fr["kind"] == "signal":
            strat = fr.get("strategy", "sma")
            sym = fr["symbol"].strip()
            key = (strat, sym)
            self.fpga_signals += 1
            self.fpga_by_strategy[strat] += 1
            self.fpga_by_key[key] = self.fpga_by_key.get(key, 0) + 1
            print(f">> FPGA[{strat}] {sym}: "
                  f"{SIDE_NAME.get(fr['side'], '?')} @ "
                  f"${dollars(fr['price_e4']):.4f}  "
                  f"fast={fr['sma_fast']} slow={fr['sma_slow']}")
            if self.log:
                self.log.write(json.dumps(
                    {"t": now_us(), "signal": True, **fr}) + "\n")
            if key in self.verifiers:
                self.verifiers[key].on_fpga_signal(fr, self.echoes)
            else:
                print(f"!! signal for unconfigured symbol {sym} — "
                      "host/FPGA slot mismatch?")

        elif fr["kind"] == "symcfg_ack":
            self.symcfg_acks[fr["slot"]] = fr

    # ---- teardown / report ------------------------------------------------------
    def close(self):
        self._stop.set()
        time.sleep(0.1)
        self.ser.close()
        if self.log:
            self.log.close()

    def summary(self) -> bool:
        print("\n---- session summary " + "-" * 40)
        print(f"  ticks sent            {self.sent}")
        print(f"  echoes received       {self.echoes}"
              + ("   << MISMATCH — frames lost?"
                 if self.echoes != self.sent else ""))
        print(f"  resyncs on RX stream  {self.parser.resync_count}")
        for (name, sym), v in sorted(self.verifiers.items()):
            m = self.models[name][sym]
            print(f"  [{name} {sym:<6}] fpga/model/verified/diverged   "
                  f"{self.fpga_by_key.get((name, sym), 0)} / {m.signals} / "
                  f"{v.verified} / {v.divergences}")
        if self.rtt_us:
            r = sorted(self.rtt_us)
            print(f"  round-trip us         min {r[0]}  "
                  f"median {r[len(r)//2]}  max {r[-1]}")
        if self.min_offset is not None:
            print(f"  host-fpga clock offset (min, us)  {self.min_offset}")
            print( "    (transit jitter = per-tick (host_ts - fpga_ts) minus this)")
        ok = all(v.divergences == 0 for v in self.verifiers.values()) \
             and all(self.fpga_by_key.get(k, 0) == self.models[k[0]][k[1]].signals
                     for k in self.verifiers)
        print(f"  RESULT: {'OK' if ok else '** CHECK FAILED **'}")
        return ok


# ---------------------------------------------------------------------------
# Tick sources
# ---------------------------------------------------------------------------
def run_sim(br: Bridge, n: int, rate: float, start_price: float):
    """Deterministic-seed random walks, one per configured symbol,
    round-robin at `rate` ticks/second total. Configures the FPGA's
    slots first — every session starts by syncing hardware to host."""
    import random
    if not br.configure_symbols(br.symbols):
        print("[sim] aborting: slot configuration failed")
        return
    rng = random.Random(42)
    walks = {t: to_e4(start_price) for t in br.symbols}
    period = 1.0 / rate
    print(f"[sim] {n} trades across {br.symbols} @ {rate}/s")
    for i in range(n):
        sym = br.symbols[i % len(br.symbols)]
        walks[sym] = max(100_000, walks[sym] + rng.randint(-40_000, 40_000))
        br.send_trade(walks[sym], rng.randint(1, 500), symbol=sym)
        br.pump(timeout=period)
    br.pump(timeout=1.0)


def run_selftest(br: Bridge):
    """Hardware acceptance test: descending warm-up then a spike.

    The spike crosses BOTH strategies (deliberately — it also exercises the
    same-cycle collision arbiter in fabric), so the expectation is one BUY
    per strategy, each matching its local mirror model. Expected values are
    computed by the models themselves, so any FAST/SLOW/EMA-K build passes
    as long as the CLI params match the bitstream.

    On failure, the diagnosis lines map each fingerprint to its usual
    cause: no echoes = link/programming; echoes but no signals = bitstream
    predates the indicator drops; SMA fine but EMA orphaned = bitstream
    predates the EMA drop (rebuild with ema_engine.sv); counts equal but
    divergent = CLI params don't match the bitstream's.
    """
    sym = br.symbol
    m0 = br.models["sma"][sym]
    print(f"[selftest] SMA {m0.fast_n}/{m0.slow_n}, EMA "
          f"k={br.models['ema'][sym].k_fast}/{br.models['ema'][sym].k_slow} "
          f"on '{sym}'")
    # first: exercise the runtime slot write + ACK path
    if not br.configure_symbols([sym]):
        print("[selftest] FAIL — slot configuration not acked "
              "(bitstream may predate v2 / wire-format mismatch)")
        return
    m0 = br.models["sma"][sym]                        # rebuilt by configure
    p0 = to_e4(200.0)
    for k in range(m0.slow_n):                        # descending warm-up
        br.send_trade(p0 - k * to_e4(1.0), 1)
        br.pump(timeout=0.05)
    br.send_trade(to_e4(500.0), 1)                   # spike: both cross
    br.pump(timeout=0.05)
    br.send_trade(to_e4(510.0), 1)                   # hold above: no retrigger
    br.pump(timeout=1.5)

    ok = True
    if br.echoes != br.sent:
        ok = False
        print(f"[selftest] DIAG: {br.echoes}/{br.sent} echoes — "
              + ("no link: check --port and that the board is programmed "
                 "(LD7 heartbeat?)" if br.echoes == 0 else
                 "frames lost: check cabling/baud"))
    for name in ("sma", "ema"):
        m, v = br.models[name][sym], br.verifiers[(name, sym)]
        f = br.fpga_by_key.get((name, sym), 0)
        if f == m.signals == v.verified and v.divergences == 0:
            continue
        ok = False
        print(f"[selftest] DIAG [{name}]: fpga={f} model={m.signals} "
              f"verified={v.verified} diverged={v.divergences}")
        if f == 0 and m.signals > 0:
            print(f"[selftest]   -> board never sent a {name.upper()} "
                  "signal: bitstream likely predates this engine — "
                  "rebuild with all rtl/*.sv files")
        elif v.divergences:
            print(f"[selftest]   -> values disagree: CLI params "
                  "(--fast/--slow/--ema-kf/--ema-ks) don't match the "
                  "bitstream's build parameters")
    if br.models["sma"][sym].signals != 1:
        ok = False
        print("[selftest] DIAG: stimulus should produce exactly one SMA "
              "BUY — check --symbol matches the bitstream's TARGET_SYMBOL")

    if ok:
        print("[selftest] PASS — board decodes, computes, and signals both "
              "strategies in agreement with the models")
    else:
        print("[selftest] FAIL — see DIAG lines above and the summary")


def run_alpaca(br: Bridge, feed: str = "iex"):
    """Live trades via Alpaca's v2 websocket. Lazy import + clear errors."""
    try:
        import websocket                              # websocket-client
    except ImportError:
        sys.exit("alpaca source needs:  pip3 install websocket-client "
                 "--break-system-packages")
    key = os.environ.get("ALPACA_KEY")
    secret = os.environ.get("ALPACA_SECRET")
    if not (key and secret):
        sys.exit("set ALPACA_KEY and ALPACA_SECRET environment variables")

    url = f"wss://stream.data.alpaca.markets/v2/{feed}"
    if not br.configure_symbols(br.symbols):
        sys.exit("[alpaca] aborting: FPGA slot configuration failed")

    def on_open(ws):
        ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
        ws.send(json.dumps({"action": "subscribe",
                            "trades": list(br.symbols)}))
        print(f"[alpaca] subscribed to trades: {br.symbols}")

    def on_message(ws, message):
        for m in json.loads(message):
            if m.get("T") == "t" and m.get("S") in br.symbols:
                br.send_trade(to_e4(float(m["p"])), int(m.get("s", 0)),
                              symbol=m["S"])

    def on_error(ws, err):
        print(f"[alpaca] websocket error: {err}")

    def resub(new_syms):
        # dashboard reconfigured the slots mid-session: follow on the feed
        try:
            ws.send(json.dumps({"action": "subscribe",
                                "trades": list(new_syms)}))
        except Exception as e:
            print(f"[alpaca] resubscribe failed: {e}")
    br.on_symbols_changed = resub

    ws = websocket.WebSocketApp(url, on_open=on_open,
                                on_message=on_message, on_error=on_error)
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    print("[alpaca] running — Ctrl-C to stop")
    try:
        while True:
            br.pump(timeout=0.2)
    except KeyboardInterrupt:
        ws.close()


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="FPGA tick-engine host bridge")
    ap.add_argument("--port", required=True,
                    help="serial port (/dev/ttyUSB1) or emulator pty path")
    ap.add_argument("--symbol", "--symbols", dest="symbols", default="SPY",
                    help="comma-separated, up to 8 (e.g. SPY,QQQ,AAPL)")
    ap.add_argument("--fast", type=int, default=8,
                    help="FAST_N of the built bitstream")
    ap.add_argument("--slow", type=int, default=32,
                    help="SLOW_N of the built bitstream")
    ap.add_argument("--source", choices=["sim", "selftest", "alpaca"],
                    default="sim")
    ap.add_argument("--n", type=int, default=100, help="sim: tick count")
    ap.add_argument("--rate", type=float, default=10.0, help="sim: ticks/s")
    ap.add_argument("--start-price", type=float, default=500.0)
    ap.add_argument("--log", default=None, help="JSONL tick log path")
    args = ap.parse_args()

    br = Bridge(args.port, args.symbol, args.fast, args.slow,
                log_path=args.log)
    try:
        if args.source == "sim":
            run_sim(br, args.n, args.rate, args.start_price)
        elif args.source == "selftest":
            run_selftest(br)
        else:
            run_alpaca(br)
    except KeyboardInterrupt:
        pass
    finally:
        ok = br.summary()
        br.close()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
