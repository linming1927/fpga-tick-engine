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
                           pack_tick, dollars, to_e4,
                           TYPE_TRADE, TYPE_ECHO_TRADE, TYPE_SIGNAL,
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
    def __init__(self, port: str, symbol: str, fast_n: int, slow_n: int,
                 ema_kf: int = 3, ema_ks: int = 5,
                 baud: int = 115_200, log_path: str | None = None):
        self.symbol = symbol.ljust(4)[:4]
        self.ser = serial.Serial(port, baud, timeout=0.05)
        # one mirror model + one content-matching verifier PER STRATEGY —
        # both must match the parameters of the built bitstream
        self.models = {"sma": SMAMirror(fast_n=fast_n, slow_n=slow_n),
                       "ema": EMAMirror(k_fast=ema_kf, k_slow=ema_ks,
                                        warmup_n=slow_n)}
        self.verifiers = {"sma": SignalVerifier(), "ema": SignalVerifier()}
        self.model = self.models["sma"]          # back-compat aliases
        self.verifier = self.verifiers["sma"]
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

        self._stop = threading.Event()
        self._rx = threading.Thread(target=self._reader, daemon=True)
        self._rx.start()

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
                   side: int = SIDE_NEUTRAL) -> int:
        ts = now_us()
        self.ser.write(pack_tick(TYPE_TRADE, self.symbol, price_e4, qty,
                                 side, ts))
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

            # echo-driven model updates, BOTH strategies from the same
            # decoded tick — the same accept filter the hardware applies
            if fr["type"] == TYPE_ECHO_TRADE and fr["symbol"] == self.symbol:
                for name, model in self.models.items():
                    sig = model.ingest(fr["price_e4"])
                    if sig:
                        print(f">> model[{name}]: {sig.side_name} @ "
                              f"${dollars(sig.price_e4):.4f}  "
                              f"fast={sig.sma_fast} slow={sig.sma_slow}")
                        self.verifiers[name].on_model_signal(sig,
                                                             self.echoes)
            for v in self.verifiers.values():
                v.on_echo(self.echoes)
            if self.on_echo:
                self.on_echo(fr)

        elif fr["kind"] == "signal":
            strat = fr.get("strategy", "sma")
            self.fpga_signals += 1
            self.fpga_by_strategy[strat] += 1
            print(f">> FPGA[{strat}]: {SIDE_NAME.get(fr['side'], '?')} @ "
                  f"${dollars(fr['price_e4']):.4f}  "
                  f"fast={fr['sma_fast']} slow={fr['sma_slow']}  "
                  f"[fpga_ts={fr['fpga_ts']} us]")
            if self.log:
                self.log.write(json.dumps(
                    {"t": now_us(), "signal": True, **fr}) + "\n")
            self.verifiers[strat].on_fpga_signal(fr, self.echoes)

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
        for name in ("sma", "ema"):
            v, m = self.verifiers[name], self.models[name]
            print(f"  [{name}] fpga/model/verified/diverged   "
                  f"{self.fpga_by_strategy[name]} / {m.signals} / "
                  f"{v.verified} / {v.divergences}")
        if self.rtt_us:
            r = sorted(self.rtt_us)
            print(f"  round-trip us         min {r[0]}  "
                  f"median {r[len(r)//2]}  max {r[-1]}")
        if self.min_offset is not None:
            print(f"  host-fpga clock offset (min, us)  {self.min_offset}")
            print( "    (transit jitter = per-tick (host_ts - fpga_ts) minus this)")
        ok = all(v.divergences == 0 for v in self.verifiers.values()) \
             and all(self.fpga_by_strategy[n] == self.models[n].signals
                     for n in self.models)
        print(f"  RESULT: {'OK' if ok else '** CHECK FAILED **'}")
        return ok


# ---------------------------------------------------------------------------
# Tick sources
# ---------------------------------------------------------------------------
def run_sim(br: Bridge, n: int, rate: float, start_price: float):
    """Deterministic-seed random-walk trades at `rate` per second."""
    import random
    rng = random.Random(42)
    price = to_e4(start_price)
    period = 1.0 / rate
    print(f"[sim] {n} trades of '{br.symbol}' @ {rate}/s from "
          f"${start_price:.2f}")
    for _ in range(n):
        step = rng.randint(-40_000, 40_000)          # up to +/- $4
        price = max(100_000, price + step)           # floor at $10
        br.send_trade(price, rng.randint(1, 500))
        br.pump(timeout=period)
    br.pump(timeout=1.0)                             # drain


def run_selftest(br: Bridge):
    """Hardware acceptance test: descending warm-up then a spike.

    Expected: exactly one BUY whose SMAs match the local model — computed
    by the model itself, so this works for any FAST_N/SLOW_N build.
    """
    print(f"[selftest] engine {br.model.fast_n}/{br.model.slow_n} on "
          f"'{br.symbol}'")
    p0 = to_e4(200.0)
    for k in range(br.model.slow_n):                 # descending warm-up
        br.send_trade(p0 - k * to_e4(1.0), 1)
        br.pump(timeout=0.05)
    br.send_trade(to_e4(500.0), 1)                   # spike -> golden cross
    br.pump(timeout=0.05)
    br.send_trade(to_e4(510.0), 1)                   # hold above: no retrigger
    br.pump(timeout=1.0)
    if br.fpga_signals == 1 and br.verifier.verified == 1 \
            and br.verifier.divergences == 0:
        print("[selftest] PASS — board decodes, computes, and signals "
              "in agreement with the model")
    else:
        print("[selftest] FAIL — see summary")


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
    sym = br.symbol.strip()

    def on_open(ws):
        ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
        ws.send(json.dumps({"action": "subscribe", "trades": [sym]}))
        print(f"[alpaca] subscribed to trades: {sym}")

    def on_message(ws, message):
        for m in json.loads(message):
            if m.get("T") == "t" and m.get("S") == sym:
                br.send_trade(to_e4(float(m["p"])), int(m.get("s", 0)))

    def on_error(ws, err):
        print(f"[alpaca] websocket error: {err}")

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
    ap.add_argument("--symbol", default="SPY")
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
