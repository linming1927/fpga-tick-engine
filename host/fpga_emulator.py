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
It prints the pty slave path (e.g. /dev/pts/3 on Linux, /dev/ttys003
on macOS); point bridge.py's --port at that path.

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
import select
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tick_protocol import (FrameParser, SMAMirror, EMAMirror, VWAPMirror,
                           pack_fpga_echo, pack_fpga_signal, parse_tick,
                           TICK_SOF, TICK_EOF, TICK_LEN, TYPE_TRADE,
                           TYPE_SYMCFG, TYPE_SESSRST,
                           TYPE_SIGNAL_SMA, TYPE_SIGNAL_EMA,
                           TYPE_SIGNAL_VWAP, SYM_LEN, dollars)


class FPGAEmulator:
    def __init__(self, symbol: str = "SPY", fast_n: int = 8,
                 slow_n: int = 32, ema_kf: int = 3, ema_ks: int = 5,
                 vwap_warmup: int = 20, vwap_k2_q8: int = 256,
                 verbose: bool = False):
        self.params = (fast_n, slow_n, ema_kf, ema_ks)
        self.vwap_params = (vwap_warmup, vwap_k2_q8)
        # slot register file, mirroring symcfg.sv: slot 0 seeded
        self.slots = {0: symbol.strip().upper()}
        self.models = {"sma": {}, "ema": {}, "vwap_bounce": {}}
        self._ensure_models(self.slots[0])
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
        # v3.31: wait for _serve() to actually exit before returning,
        # so callers know teardown is genuinely complete (matters most
        # right after this fix -- confirms the select()-based loop
        # really does notice _stop promptly, rather than trusting it
        # silently). Bounded: this must never itself become a new way
        # to hang if something unexpected keeps the thread alive.
        self.thread.join(timeout=2.0)

    def _ensure_models(self, sym: str, fresh: bool = False):
        """fresh=True mirrors the v2 hardware rule: writing a slot RESETS
        that slot's engine state (symcfg.sv slot_wr -> engine state_rst),
        so host mirror models can rebuild in lockstep."""
        f, sl, kf, ks = self.params
        vw, vk2 = self.vwap_params
        if fresh or sym not in self.models["sma"]:
            self.models["sma"][sym] = SMAMirror(fast_n=f, slow_n=sl)
            self.models["ema"][sym] = EMAMirror(k_fast=kf, k_slow=ks,
                                                warmup_n=sl)
            self.models["vwap_bounce"][sym] = VWAPMirror(
                warmup_n=vw, k2_q8=vk2)

    # ---- the board ---------------------------------------------------------
    def _fpga_ts(self) -> int:
        return (time.monotonic_ns() - self.t0) // 1000

    def _serve(self):
        # v3.31: found on macOS, running order_manager.py's test suite
        # for the first time -- the ORIGINAL version here was a bare
        # blocking os.read(), relying entirely on stop()'s os.close()
        # (called from the MAIN thread) to interrupt this thread's read
        # and raise OSError. That race is platform-inconsistent: Linux
        # reliably wakes the blocked reader when the fd closes
        # elsewhere; macOS/BSD kernels are documented to sometimes
        # leave it blocked in an UNINTERRUPTIBLE state instead (ps
        # shows this thread's process as "U" — not even SIGKILL-able
        # cleanly, let alone a normal signal). The fix doesn't try to
        # make close() reliable across platforms — it sidesteps the
        # race entirely: select() with a bounded timeout means this
        # loop re-checks self._stop on its OWN schedule, never
        # depending on an external close() to wake it up at all.
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.2)
            except (OSError, ValueError):
                return          # fd already closed elsewhere -- done
            if not ready:
                continue        # timeout: nothing to read, recheck _stop
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
                             tick["host_ts"], ts)          # 0x10 -> 0x90 ack
        sym = tick["symbol"].strip()
        if tick["type"] == TYPE_SYMCFG:                    # slot write
            slot = tick["qty"] & 7
            if tick["side"] & 1:
                self.slots[slot] = sym
                self._ensure_models(sym, fresh=True)
            else:
                self.slots.pop(slot, None)
        elif tick["type"] == TYPE_SESSRST:                 # v3: sessctl.sv
            # 0x11 -> per-slot (or broadcast) VWAP session reset; the
            # echo built above (wire 0x91) is the ack, same as hardware
            if tick["side"] == 0xFF:
                for m in self.models["vwap_bounce"].values():
                    m.sess_reset()
            else:
                s = self.slots.get(tick["qty"] & 7)
                if s and s in self.models["vwap_bounce"]:
                    self.models["vwap_bounce"][s].sess_reset()
        elif tick["type"] == TYPE_TRADE and sym in self.slots.values():
            for name, ftype in (("sma", TYPE_SIGNAL_SMA),
                                ("ema", TYPE_SIGNAL_EMA)):
                sig = self.models[name][sym].ingest(tick["price_e4"])
                if sig:
                    out += pack_fpga_signal(sym, sig.price_e4, sig.side,
                                            sig.sma_fast, sig.sma_slow,
                                            ts, ftype)
                    if self.verbose:
                        print(f"[emu] {name.upper()} {sym} SIGNAL "
                              f"{sig.side_name} @ "
                              f"${dollars(sig.price_e4):.4f}")
            vsig = self.models["vwap_bounce"][sym].ingest(
                tick["price_e4"], tick["qty"])
            if vsig:
                # 0x85: the two indicator payload fields carry
                # {vwap, eval_skips} — the emulator never coalesces
                # (it evaluates every tick, like the fabric at any
                # realistic link rate), so skips is always 0
                out += pack_fpga_signal(sym, vsig.price_e4, vsig.side,
                                        vsig.vwap, 0,
                                        ts, TYPE_SIGNAL_VWAP)
                if self.verbose:
                    print(f"[emu] VWAP {sym} SIGNAL {vsig.side_name} @ "
                          f"${dollars(vsig.price_e4):.4f} "
                          f"vwap={vsig.vwap}")
        try:
            os.write(self.master_fd, out)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(
        description="Virtual Arty tick engine on a pty — a pure-Python "
                    "stand-in for the real board. Computes SMA/EMA/VWAP "
                    "signals through the exact same mirror models "
                    "(tick_protocol.SMAMirror/EMAMirror/VWAPMirror) "
                    "that every hardware signal in this project is "
                    "verified against — so a session run against this "
                    "emulator isn't an approximation of the board, it's "
                    "the same math, just without silicon underneath it.")
    ap.add_argument("--symbol", default="SPY",
                    help="initial symbol for slot 0 (order_manager.py's "
                        "own --symbols will configure the real slots "
                        "it needs once it connects — this is just a "
                        "placeholder before that happens)")
    ap.add_argument("--fast", type=int, default=8,
                    help="SMA fast window, in ticks — must match "
                        "order_manager.py's --fast")
    ap.add_argument("--slow", type=int, default=32,
                    help="SMA slow window, in ticks — must match "
                        "order_manager.py's --slow")
    ap.add_argument("--ema-kf", type=int, default=3,
                    help="fast EMA shift — must match --ema-kf")
    ap.add_argument("--ema-ks", type=int, default=5,
                    help="slow EMA shift — must match --ema-ks")
    ap.add_argument("--vwap-warmup", type=int, default=20,
                    help="VWAP ticks-before-events threshold — must "
                        "match --vwap-warmup")
    ap.add_argument("--vwap-k2-q8", type=int, default=256,
                    help="VWAP band width, k^2 in Q8 — must match "
                        "--vwap-k2-q8")
    ap.add_argument("--port-symlink", default="/tmp/fpga-tick-emulator",
                    help="maintain a stable symlink to the pty's real "
                        "(and otherwise different every run) path, so "
                        "your order_manager.py command never needs to "
                        "change between restarts. Pass '' to disable.")
    args = ap.parse_args()

    emu = FPGAEmulator(args.symbol, args.fast, args.slow,
                       ema_kf=args.ema_kf, ema_ks=args.ema_ks,
                       vwap_warmup=args.vwap_warmup,
                       vwap_k2_q8=args.vwap_k2_q8, verbose=True)
    path = emu.start()

    symlink = args.port_symlink.strip()
    if symlink:
        try:
            if os.path.islink(symlink) or os.path.exists(symlink):
                os.remove(symlink)
            os.symlink(path, symlink)
        except OSError as e:
            print(f"[emu] WARNING: couldn't create --port-symlink "
                 f"{symlink!r} ({e}) — use the real path below instead")
            symlink = None

    print(f"virtual FPGA listening on: {path}")
    print(f"  engines: SMA {args.fast}/{args.slow}, EMA k={args.ema_kf}/"
         f"{args.ema_ks}, VWAP warmup={args.vwap_warmup} "
         f"k2_q8={args.vwap_k2_q8}, 8 runtime slots "
         f"(slot 0 = '{emu.slots[0]}')")
    if symlink:
        print(f"  stable path (survives restarts): {symlink}")
        print(f"  point order_manager.py at it:")
        print(f"    python3 order_manager.py --port {symlink} "
             f"--symbol {args.symbol.strip()} --fast {args.fast} "
             f"--slow {args.slow} --source alpaca --broker mock ...")
    print(f"  Ctrl-C to stop")

    # cleanup (symlink removal, emulator.stop()) must run on ANY normal
    # termination, not just Ctrl-C/SIGINT -- `kill <pid>` (SIGTERM, the
    # default) doesn't raise KeyboardInterrupt on its own, and a process
    # manager or a closed terminal is more likely to send SIGTERM than
    # SIGINT. Route both through the same handler.
    def _handle_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        emu.stop()
        if symlink and os.path.islink(symlink):
            try:
                os.remove(symlink)
            except OSError:
                pass


if __name__ == "__main__":
    main()
