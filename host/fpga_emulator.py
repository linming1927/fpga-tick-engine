#!/usr/bin/env python3
"""
fpga_emulator.py — a virtual Arty on a pseudo-terminal.

Behaves like the real board's serial interface: parses 22-byte tick frames,
echoes each decoded tick as a 30-byte 0x81/0x82 frame with an emulated
FPGA arrival timestamp, runs the SMA engine (via SMAMirror — same spec,
same integer semantics), and emits 0x83 signal frames on crossovers.

Why this exists:
  * develop/test bridge.py with no hardware attached (or from this machine)
  * closed-loop test in test_host.py: bridge + emulator through a real
    serial byte stream, exercising the FrameParser's chunking/resync paths
  * a known-good conversation partner when debugging the real board — if
    the bridge works against the emulator but not the board, the problem
    is on the board side, and vice versa

Usage (standalone):
    python3 fpga_emulator.py --symbol "SPY " --fast 8 --slow 32
It prints the pty slave path (e.g. /dev/pts/3); point bridge.py's --port
at that path.

Fidelity notes / deliberate simplifications:
  * fpga_ts is microseconds since emulator start (the real counter is
    since-reset — same semantics)
  * echo precedes any signal from the same tick; the real board can emit
    either order depending on TX-queue state, and the bridge's verifier is
    order-agnostic, so both are covered between emulator and hardware
  * no FIFO saturation / drop modeling — the emulator is infinitely fast
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tick_protocol import (FrameParser, SMAMirror, EMAMirror,
                           pack_fpga_echo, pack_fpga_signal, parse_tick,
                           TICK_SOF, TICK_EOF, TICK_LEN, TYPE_TRADE,
                           TYPE_SIGNAL_SMA, TYPE_SIGNAL_EMA, dollars)


class FPGAEmulator:
    def __init__(self, symbol: str = "SPY ", fast_n: int = 8,
                 slow_n: int = 32, ema_kf: int = 3, ema_ks: int = 5,
                 verbose: bool = False):
        self.symbol = symbol.ljust(4)[:4]
        self.model = SMAMirror(fast_n=fast_n, slow_n=slow_n)
        self.model_ema = EMAMirror(k_fast=ema_kf, k_slow=ema_ks,
                                   warmup_n=slow_n)
        self.verbose = verbose
        self.t0 = time.monotonic_ns()
        self.parser = FrameParser(sof=TICK_SOF, eof=TICK_EOF,
                                  length=TICK_LEN, decoder=parse_tick)
        self.master_fd, self.slave_fd = os.openpty()
        self.slave_path = os.ttyname(self.slave_fd)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    # ---- lifecycle ---------------------------------------------------------
    def start(self):
        self.thread.start()
        return self.slave_path

    def stop(self):
        self._stop.set()
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    # ---- the board ---------------------------------------------------------
    def _fpga_ts(self) -> int:
        return (time.monotonic_ns() - self.t0) // 1000

    def _serve(self):
        while not self._stop.is_set():
            try:
                data = os.read(self.master_fd, 256)
            except OSError:
                return
            if not data:
                return
            for tick in self.parser.feed(data):
                self._handle(tick)

    def _handle(self, tick: dict):
        ts = self._fpga_ts()
        out = pack_fpga_echo(tick["type"], tick["symbol"],
                             tick["price_e4"], tick["qty"], tick["side"],
                             tick["host_ts"], ts)
        # engine accept filter — same rule as indicator_engine.sv
        if tick["type"] == TYPE_TRADE and tick["symbol"] == self.symbol:
            for model, ftype, name in ((self.model, TYPE_SIGNAL_SMA, "SMA"),
                                       (self.model_ema, TYPE_SIGNAL_EMA,
                                        "EMA")):
                sig = model.ingest(tick["price_e4"])
                if sig:
                    out += pack_fpga_signal(self.symbol, sig.price_e4,
                                            sig.side, sig.sma_fast,
                                            sig.sma_slow, ts, ftype)
                    if self.verbose:
                        print(f"[emu] {name} SIGNAL {sig.side_name} @ "
                              f"${dollars(sig.price_e4):.4f}")
        try:
            os.write(self.master_fd, out)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="Virtual Arty tick engine on a pty")
    ap.add_argument("--symbol", default="SPY ")
    ap.add_argument("--fast", type=int, default=8)
    ap.add_argument("--slow", type=int, default=32)
    args = ap.parse_args()

    emu = FPGAEmulator(args.symbol, args.fast, args.slow, verbose=True)
    path = emu.start()
    print(f"virtual FPGA listening on: {path}")
    print(f"  engine: {args.fast}/{args.slow} SMA crossover on "
          f"'{emu.symbol}' trades")
    print(f"  point bridge.py at it:  python3 bridge.py --port {path} "
          f"--source sim --symbol {args.symbol.strip()}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        emu.stop()


if __name__ == "__main__":
    main()
