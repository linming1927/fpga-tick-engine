#!/usr/bin/env python3
"""
tick_protocol.py — wire-format codecs and the SMA mirror model.

This module is the single source of truth on the host for everything the
FPGA and host must agree on:

  * the 22-byte host -> FPGA tick frame        (MANUAL.md §2)
  * the 30-byte FPGA -> host echo/signal frame (frame_tx.sv header)
  * the SMA crossover semantics                (indicator_engine.sv header)

It is imported by bridge.py (the real application), fpga_emulator.py (a
virtual board for development without hardware), and test_host.py (golden
vectors). Keeping codec + model here, free of any I/O, means they can be
unit-tested in microseconds and reused everywhere.

MIRROR MODEL FIDELITY RULES
---------------------------
SMAMirror must match indicator_engine.sv bit-for-bit, which means matching
its *integer semantics*, not just its math:
  * averages use truncating right-shift:  sma = sum >> log2(N)
  * "above" is strict:                    above = (sma_fast > sma_slow)
  * warm-up: nothing is evaluated until SLOW_N samples have arrived
  * priming: the first post-warm-up evaluation sets state, never fires
Any deviation (float division, >=, off-by-one warm-up) produces false
divergence alarms against perfectly correct hardware.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Wire-format constants (must match tick_parser.sv / frame_tx.sv)
# ---------------------------------------------------------------------------
TICK_SOF, TICK_EOF, TICK_LEN = 0xAA, 0x55, 22     # host -> FPGA
FPGA_SOF, FPGA_EOF, FPGA_LEN = 0xBB, 0xCC, 30     # FPGA -> host

TYPE_TRADE, TYPE_QUOTE = 0x01, 0x02
TYPE_ECHO_TRADE, TYPE_ECHO_QUOTE = 0x81, 0x82
TYPE_SIGNAL_SMA, TYPE_SIGNAL_EMA = 0x83, 0x84
TYPE_SIGNAL = TYPE_SIGNAL_SMA                    # back-compat alias
STRATEGY_NAME = {TYPE_SIGNAL_SMA: "sma", TYPE_SIGNAL_EMA: "ema"}

SIDE_NEUTRAL, SIDE_BUY, SIDE_SELL = 0x00, 0x01, 0x02
SIDE_NAME = {SIDE_BUY: "BUY", SIDE_SELL: "SELL", SIDE_NEUTRAL: "NEUTRAL"}


# ---------------------------------------------------------------------------
# host -> FPGA tick frame
# ---------------------------------------------------------------------------
def pack_tick(msg_type: int, symbol: str, price_e4: int, qty: int,
              side: int, host_ts_us: int) -> bytes:
    """Build one 22-byte tick frame. price_e4 is price x 10 000 (int)."""
    sym = symbol.encode("ascii").ljust(4)[:4]
    if not (0 <= price_e4 < 2**32):
        raise ValueError(f"price_e4 out of uint32 range: {price_e4}")
    qty = min(max(qty, 0), 0xFFFF)                 # clamp to uint16
    return (bytes([TICK_SOF, msg_type]) + sym +
            struct.pack(">IHBQ", price_e4, qty, side, host_ts_us) +
            bytes([TICK_EOF]))


def parse_tick(frame: bytes) -> dict:
    """Decode one aligned 22-byte tick frame (used by the emulator)."""
    price_e4, qty, side, host_ts = struct.unpack(">IHBQ", frame[6:21])
    return {"type": frame[1], "symbol": frame[2:6].decode("ascii"),
            "price_e4": price_e4, "qty": qty, "side": side,
            "host_ts": host_ts}


# ---------------------------------------------------------------------------
# FPGA -> host frames (tagged union on TYPE — see frame_tx.sv header)
# ---------------------------------------------------------------------------
def pack_fpga_echo(msg_type: int, symbol: str, price_e4: int, qty: int,
                   side: int, host_ts_us: int, fpga_ts_us: int) -> bytes:
    """Build a 30-byte echo frame (used by the emulator)."""
    sym = symbol.encode("ascii").ljust(4)[:4]
    return (bytes([FPGA_SOF, 0x80 | msg_type]) + sym +
            struct.pack(">IHBQQ", price_e4, qty, side, host_ts_us,
                        fpga_ts_us) +
            bytes([FPGA_EOF]))


def pack_fpga_signal(symbol: str, price_e4: int, side: int,
                     sma_fast: int, sma_slow: int,
                     fpga_ts_us: int, ftype: int = TYPE_SIGNAL_SMA) -> bytes:
    """Build a 30-byte signal frame, 0x83 (SMA) or 0x84 (EMA) — used by the
    emulator. The TS_A slot (bytes 13-20) carries the {fast, slow} pair."""
    sym = symbol.encode("ascii").ljust(4)[:4]
    return (bytes([FPGA_SOF, ftype]) + sym +
            struct.pack(">IHBIIQ", price_e4, 0, side,
                        sma_fast & 0xFFFFFFFF, sma_slow & 0xFFFFFFFF,
                        fpga_ts_us) +
            bytes([FPGA_EOF]))


def _decode_fpga_frame(frame: bytes) -> dict:
    """Decode one aligned 30-byte FPGA frame into a dict keyed by kind."""
    ftype = frame[1]
    symbol = frame[2:6].decode("ascii", errors="replace")
    price_e4, qty = struct.unpack(">IH", frame[6:12])
    side = frame[12]
    fpga_ts = struct.unpack(">Q", frame[21:29])[0]
    if ftype in (TYPE_SIGNAL_SMA, TYPE_SIGNAL_EMA):
        sma_fast, sma_slow = struct.unpack(">II", frame[13:21])
        return {"kind": "signal", "type": ftype,
                "strategy": STRATEGY_NAME[ftype], "symbol": symbol,
                "price_e4": price_e4, "side": side,
                "sma_fast": sma_fast, "sma_slow": sma_slow,
                "fpga_ts": fpga_ts}
    host_ts = struct.unpack(">Q", frame[13:21])[0]
    return {"kind": "echo", "type": ftype, "symbol": symbol,
            "price_e4": price_e4, "qty": qty, "side": side,
            "host_ts": host_ts, "fpga_ts": fpga_ts}


class FrameParser:
    """Incremental, resyncing frame extractor for a byte stream.

    The software mirror of tick_parser.sv's recovery philosophy: hunt for
    SOF; if a candidate frame's EOF slot is wrong, that candidate is
    garbage — advance ONE byte and rehunt (the true SOF may be inside the
    bad candidate). Partial reads are handled by buffering; feed() may be
    called with any chunking the serial layer produces.
    """

    def __init__(self, sof: int = FPGA_SOF, eof: int = FPGA_EOF,
                 length: int = FPGA_LEN, decoder=_decode_fpga_frame):
        self._sof, self._eof, self._len = sof, eof, length
        self._decode = decoder
        self._buf = bytearray()
        self.resync_count = 0          # observability, like parse_error_count

    def feed(self, data: bytes) -> list[dict]:
        self._buf.extend(data)
        out = []
        while True:
            i = self._buf.find(self._sof)
            if i < 0:                          # no SOF anywhere: drop all
                if self._buf:
                    self.resync_count += 1
                self._buf.clear()
                return out
            if i > 0:                          # garbage before SOF: drop it
                del self._buf[:i]
                self.resync_count += 1
            if len(self._buf) < self._len:     # incomplete: wait for more
                return out
            if self._buf[self._len - 1] == self._eof:
                out.append(self._decode(bytes(self._buf[:self._len])))
                del self._buf[:self._len]
            else:                              # bad EOF: not a real frame
                del self._buf[:1]
                self.resync_count += 1


# ---------------------------------------------------------------------------
# SMA mirror model — the third implementation of the same specification
# (1: indicator_engine.sv, 2: the SV testbench model, 3: this)
# ---------------------------------------------------------------------------
@dataclass
class SMASignal:
    side: int
    price_e4: int
    sma_fast: int
    sma_slow: int

    @property
    def side_name(self) -> str:
        return SIDE_NAME.get(self.side, f"0x{self.side:02x}")


@dataclass
class SMAMirror:
    fast_n: int = 8
    slow_n: int = 32

    _fast: list = field(default_factory=list, repr=False)
    _slow: list = field(default_factory=list, repr=False)
    sum_fast: int = 0
    sum_slow: int = 0
    fill: int = 0
    sma_fast: int = 0
    sma_slow: int = 0
    above_prev: bool = False
    primed: bool = False
    ticks: int = 0
    signals: int = 0

    def __post_init__(self):
        for n in (self.fast_n, self.slow_n):
            if n & (n - 1):
                raise ValueError(f"window {n} must be a power of two "
                                 "(hardware divides by shift)")
        self._fast = [0] * self.fast_n          # zeros, like the FPGA buffers
        self._slow = [0] * self.slow_n
        self._log2f = self.fast_n.bit_length() - 1
        self._log2s = self.slow_n.bit_length() - 1

    @property
    def warmed_up(self) -> bool:
        return self.fill == self.slow_n

    def ingest(self, price_e4: int) -> SMASignal | None:
        """Feed one accepted TRADE price; returns a signal if one fires.

        Caller is responsible for the engine's accept filter (target
        symbol, trades only) — this mirrors indicator_engine.sv where the
        filter sits in front of the window logic.
        """
        self.ticks += 1
        # running sums: add newest, subtract the element about to fall off
        self.sum_fast += price_e4 - self._fast[-1]
        self.sum_slow += price_e4 - self._slow[-1]
        self._fast = [price_e4] + self._fast[:-1]      # shift register
        self._slow = [price_e4] + self._slow[:-1]
        if self.fill < self.slow_n:
            self.fill += 1

        # truncating shift, exactly like the hardware — NOT round(), NOT /
        self.sma_fast = self.sum_fast >> self._log2f
        self.sma_slow = self.sum_slow >> self._log2s

        if not self.warmed_up:
            return None
        above = self.sma_fast > self.sma_slow          # strict, like the RTL
        sig = None
        if self.primed and above != self.above_prev:
            self.signals += 1
            sig = SMASignal(side=SIDE_BUY if above else SIDE_SELL,
                            price_e4=price_e4,
                            sma_fast=self.sma_fast,
                            sma_slow=self.sma_slow)
        self.above_prev = above
        self.primed = True                             # first pass only primes
        return sig


@dataclass
class EMAMirror:
    """Mirror of ema_engine.sv — extended-precision leaky integrators.

    Fidelity rules (see the RTL header): A = ema << K accumulators, seeded
    with the first price; A' = A + p - (A >> K); read-out ema = A >> K;
    strict (fast > slow); WARMUP_N accepted trades gate; first evaluation
    primes without firing. Python ints are unbounded, so no width concerns;
    the >> truncation matches the hardware exactly.
    """
    k_fast: int = 3
    k_slow: int = 5
    warmup_n: int = 32

    acc_fast: int = 0
    acc_slow: int = 0
    seeded: bool = False
    fill: int = 0
    ema_fast: int = 0
    ema_slow: int = 0
    above_prev: bool = False
    primed: bool = False
    ticks: int = 0
    signals: int = 0

    @property
    def warmed_up(self) -> bool:
        return self.fill == self.warmup_n

    # kept name-compatible with SMAMirror so shared code can treat both
    # models uniformly (fast/slow read-outs under the sma_* names)
    @property
    def sma_fast(self) -> int:
        return self.ema_fast

    @property
    def sma_slow(self) -> int:
        return self.ema_slow

    @property
    def slow_n(self) -> int:
        return self.warmup_n

    def ingest(self, price_e4: int) -> SMASignal | None:
        self.ticks += 1
        if not self.seeded:
            self.acc_fast = price_e4 << self.k_fast
            self.acc_slow = price_e4 << self.k_slow
            self.seeded = True
        else:
            self.acc_fast += price_e4 - (self.acc_fast >> self.k_fast)
            self.acc_slow += price_e4 - (self.acc_slow >> self.k_slow)
        if self.fill < self.warmup_n:
            self.fill += 1
        self.ema_fast = self.acc_fast >> self.k_fast
        self.ema_slow = self.acc_slow >> self.k_slow
        if not self.warmed_up:
            return None
        above = self.ema_fast > self.ema_slow
        sig = None
        if self.primed and above != self.above_prev:
            self.signals += 1
            sig = SMASignal(side=SIDE_BUY if above else SIDE_SELL,
                            price_e4=price_e4,
                            sma_fast=self.ema_fast,
                            sma_slow=self.ema_slow)
        self.above_prev = above
        self.primed = True
        return sig


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------
def dollars(price_e4: int) -> float:
    """Fixed-point x10000 -> float dollars, for display only (never math)."""
    return price_e4 / 10_000.0


def to_e4(price: float) -> int:
    """Float dollars -> fixed-point x10000 with round-half-away banker-safe."""
    return int(round(price * 10_000))
