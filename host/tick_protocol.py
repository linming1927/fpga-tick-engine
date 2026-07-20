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

import json
import struct
from dataclasses import dataclass, field
from datetime import datetime


# ---------------------------------------------------------------------------
# Wire-format constants (must match tick_parser.sv / frame_tx.sv)
# ---------------------------------------------------------------------------
TICK_SOF, TICK_EOF, TICK_LEN = 0xAA, 0x55, 24     # host -> FPGA (v2)
FPGA_SOF, FPGA_EOF, FPGA_LEN = 0xBB, 0xCC, 32     # FPGA -> host (v2)
SYM_LEN = 6            # fits every S&P 500 ticker (GOOGL, BRK.B, ...)

TYPE_TRADE, TYPE_QUOTE = 0x01, 0x02
TYPE_SYMCFG, TYPE_SYMCFG_ACK = 0x10, 0x90   # runtime symbol slots (v2)
TYPE_SESSRST, TYPE_SESSRST_ACK = 0x11, 0x91  # VWAP session boundary (v3):
                                             # the HOST owns the market
                                             # calendar and commands the
                                             # session reset; the fabric's
                                             # 0x91 echo is the ack — see
                                             # rtl/sessctl.sv
TYPE_ECHO_TRADE, TYPE_ECHO_QUOTE = 0x81, 0x82
TYPE_SIGNAL_SMA, TYPE_SIGNAL_EMA = 0x83, 0x84
TYPE_SIGNAL_VWAP = 0x85                          # v3: vwap_engine events
TYPE_SIGNAL = TYPE_SIGNAL_SMA                    # back-compat alias
STRATEGY_NAME = {TYPE_SIGNAL_SMA: "sma", TYPE_SIGNAL_EMA: "ema",
                TYPE_SIGNAL_VWAP: "vwap_bounce"}

SIDE_NEUTRAL, SIDE_BUY, SIDE_SELL = 0x00, 0x01, 0x02
SIDE_NAME = {SIDE_BUY: "BUY", SIDE_SELL: "SELL", SIDE_NEUTRAL: "NEUTRAL"}


# ---------------------------------------------------------------------------
# host -> FPGA tick frame
# ---------------------------------------------------------------------------
def sym_wire(symbol: str) -> bytes:
    """Symbol -> 6-byte wire form: upper-cased, space padded. Rejects
    anything that can't round-trip (>6 chars, non-ticker characters)."""
    t = symbol.strip().upper()
    if not (1 <= len(t) <= SYM_LEN) or \
            not all(c.isalnum() or c == "." for c in t):
        raise ValueError(f"bad ticker {symbol!r}")
    return t.encode("ascii").ljust(SYM_LEN)


def pack_tick(msg_type: int, symbol: str, price_e4: int, qty: int,
              side: int, host_ts_us: int) -> bytes:
    """Build one 24-byte tick frame. price_e4 is price x 10 000 (int)."""
    sym = sym_wire(symbol)
    if not (0 <= price_e4 < 2**32):
        raise ValueError(f"price_e4 out of uint32 range: {price_e4}")
    qty = min(max(qty, 0), 0xFFFF)                 # clamp to uint16
    return (bytes([TICK_SOF, msg_type]) + sym +
            struct.pack(">IHBQ", price_e4, qty, side, host_ts_us) +
            bytes([TICK_EOF]))


def pack_symcfg(slot: int, symbol: str, enable: bool = True,
                host_ts_us: int = 0) -> bytes:
    """TYPE 0x10 symbol-configuration frame: QTY[2:0]=slot, SIDE=set/clear.
    The FPGA's echo of this frame (wire TYPE 0x90) is the write ACK."""
    if not 0 <= slot <= 7:
        raise ValueError(f"slot {slot} out of range 0-7")
    return pack_tick(TYPE_SYMCFG, symbol if enable else symbol or "X",
                     0, slot, 0x01 if enable else 0x00, host_ts_us)


def pack_sessrst(slot: int = None, host_ts_us: int = 0) -> bytes:
    """TYPE 0x11 session-reset frame for the fabric VWAP engines.

    slot=None (the normal session-open case) broadcasts to ALL slots
    (SIDE=0xFF); a specific slot 0-7 resets just that one — e.g. a
    trading-halt reopen whose auction print shouldn't anchor one
    symbol's band, without touching the other seven. The FPGA's echo
    of this frame (wire TYPE 0x91) is the acknowledgement, through
    the same data path as everything else — see rtl/sessctl.sv."""
    if slot is None:
        return pack_tick(TYPE_SESSRST, "ALL", 0, 0, 0xFF, host_ts_us)
    if not 0 <= slot <= 7:
        raise ValueError(f"slot {slot} out of range 0-7")
    return pack_tick(TYPE_SESSRST, "SLOT", 0, slot, 0x01, host_ts_us)


def parse_tick(frame: bytes) -> dict:
    """Decode one aligned 24-byte tick frame (used by the emulator)."""
    price_e4, qty, side, host_ts = struct.unpack(">IHBQ", frame[8:23])
    return {"type": frame[1], "symbol": frame[2:8].decode("ascii"),
            "price_e4": price_e4, "qty": qty, "side": side,
            "host_ts": host_ts}


# ---------------------------------------------------------------------------
# FPGA -> host frames (tagged union on TYPE — see frame_tx.sv header)
# ---------------------------------------------------------------------------
def pack_fpga_echo(msg_type: int, symbol: str, price_e4: int, qty: int,
                   side: int, host_ts_us: int, fpga_ts_us: int) -> bytes:
    """Build a 32-byte echo frame (used by the emulator)."""
    sym = symbol.encode("ascii").ljust(SYM_LEN)[:SYM_LEN]
    return (bytes([FPGA_SOF, 0x80 | msg_type]) + sym +
            struct.pack(">IHBQQ", price_e4, qty, side, host_ts_us,
                        fpga_ts_us) +
            bytes([FPGA_EOF]))


def pack_fpga_signal(symbol: str, price_e4: int, side: int,
                     sma_fast: int, sma_slow: int,
                     fpga_ts_us: int, ftype: int = TYPE_SIGNAL_SMA) -> bytes:
    """Build a 32-byte signal frame, 0x83 (SMA) or 0x84 (EMA) — used by the
    emulator. The TS_A slot (bytes 15-22) carries the {fast, slow} pair."""
    sym = symbol.encode("ascii").ljust(SYM_LEN)[:SYM_LEN]
    return (bytes([FPGA_SOF, ftype]) + sym +
            struct.pack(">IHBIIQ", price_e4, 0, side,
                        sma_fast & 0xFFFFFFFF, sma_slow & 0xFFFFFFFF,
                        fpga_ts_us) +
            bytes([FPGA_EOF]))


def _decode_fpga_frame(frame: bytes) -> dict:
    """Decode one aligned 32-byte FPGA frame into a dict keyed by kind."""
    ftype = frame[1]
    symbol = frame[2:8].decode("ascii", errors="replace")
    price_e4, qty = struct.unpack(">IH", frame[8:14])
    side = frame[14]
    fpga_ts = struct.unpack(">Q", frame[23:31])[0]
    if ftype in (TYPE_SIGNAL_SMA, TYPE_SIGNAL_EMA):
        sma_fast, sma_slow = struct.unpack(">II", frame[15:23])
        return {"kind": "signal", "type": ftype,
                "strategy": STRATEGY_NAME[ftype], "symbol": symbol,
                "price_e4": price_e4, "side": side,
                "sma_fast": sma_fast, "sma_slow": sma_slow,
                "fpga_ts": fpga_ts}
    if ftype == TYPE_SIGNAL_VWAP:
        # same 32-byte layout; the two indicator payload fields carry
        # the session vwap at the evaluated snapshot (the verifier's
        # cross-check value) and the engine's eval_skips counter
        # (coalesced-tick count — nonzero means the tick rate exceeded
        # the evaluation rate; see rtl/vwap_engine.sv)
        vwap, eval_skips = struct.unpack(">II", frame[15:23])
        return {"kind": "signal", "type": ftype,
                "strategy": STRATEGY_NAME[ftype], "symbol": symbol,
                "price_e4": price_e4, "side": side,
                "vwap": vwap, "eval_skips": eval_skips,
                "fpga_ts": fpga_ts}
    host_ts = struct.unpack(">Q", frame[15:23])[0]
    if ftype == TYPE_SYMCFG_ACK:
        return {"kind": "symcfg_ack", "type": ftype, "symbol": symbol,
                "slot": qty & 7, "enabled": bool(side & 1),
                "fpga_ts": fpga_ts}
    if ftype == TYPE_SESSRST_ACK:
        return {"kind": "sessrst_ack", "type": ftype,
                "slot": qty & 7, "broadcast": (side == 0xFF),
                "fpga_ts": fpga_ts}
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
# VWAP mirror model — the third implementation of the same specification
# (1: vwap_engine.sv, 2: tb_vwap.sv's bench mirror, 3: this)
# ---------------------------------------------------------------------------
@dataclass
class VWAPSignal:
    side: int
    price_e4: int
    vwap: int            # session vwap at the evaluated snapshot — the
                         # cross-check value the 0x85 frame carries

    @property
    def side_name(self) -> str:
        return SIDE_NAME.get(self.side, f"0x{self.side:02x}")


@dataclass
class VWAPMirror:
    """Mirror of vwap_engine.sv — session accumulators, truncating
    integer divides, squared-domain band test, position-independent
    edge events.

    Fidelity rules (see the RTL header, which is normative):
      * sums: Σv, Σp·v, Σp²·v over accepted trades since session start;
        Python ints are unbounded so the RTL's parameterized widths
        need no mirroring
      * vwap = Σpv // Σv and mean_sq = Σppv // Σv — floor division,
        exactly the restoring divider's truncation
      * variance = max(0, mean_sq - vwap²); band test entirely in the
        squared domain: below ⟺ price < vwap and (vwap-price)² >
        (K2_Q8·variance) >> 8 — no sqrt, same as the fabric
      * EVENTS, NOT POSITIONS: bounce-buy on below-band → not-below,
        revert-sell on the upward vwap cross, SELL dominant when one
        evaluation sees both edges. This is the ENGINE's convention —
        deliberately different from VWAPBounceScorecard.on_tick()'s
        position-gated stream (fabric cannot know host position; the
        host layer applies position logic downstream)
      * WARMUP_N accepted trades gate; the first warm evaluation primes
        the edge state without firing
      * sess_reset() = the host-commanded TYPE 0x11 boundary (and a
        slot rewrite behaves identically)

    This model assumes the per-tick evaluation regime — every accepted
    tick evaluated, which holds whenever tick spacing exceeds ~196
    fabric cycles (~2 µs). The current link's floor is ~2 ms/tick, three
    orders of magnitude above that; if a future link ever saturates the
    engine, the 0x85 frame's eval_skips field is the observable (the
    verifier should treat nonzero skips as "mirror comparison suspended",
    not as divergence — the engine is coalescing, exactly as documented).
    """
    warmup_n: int = 20
    k2_q8: int = 256          # k² in Q8: 256 = k of 1.0

    sum_v: int = 0
    sum_pv: int = 0
    sum_ppv: int = 0
    ticks: int = 0
    vwap: int = 0
    below_prev: bool = False
    above_prev: bool = False
    primed: bool = False
    signals: int = 0

    @property
    def warmed_up(self) -> bool:
        return self.ticks >= self.warmup_n

    def sess_reset(self):
        """The TYPE 0x11 boundary: clears sums, warm-up, edge state —
        exactly what sess_rst (or a slot rewrite) does in fabric."""
        self.sum_v = 0
        self.sum_pv = 0
        self.sum_ppv = 0
        self.ticks = 0
        self.vwap = 0
        self.below_prev = False
        self.above_prev = False
        self.primed = False

    def ingest(self, price_e4: int, qty: int) -> VWAPSignal | None:
        """Feed one accepted TRADE; returns the engine-convention event
        if this evaluation fires one.

        Caller owns the accept filter (target symbol, trades only),
        mirroring the RTL where the filter sits in front of the engine.
        """
        self.ticks += 1
        self.sum_v += qty
        self.sum_pv += price_e4 * qty
        self.sum_ppv += price_e4 * price_e4 * qty
        if self.sum_v == 0:
            return None            # qty=0-only session: no divide (RTL guard)

        self.vwap = self.sum_pv // self.sum_v
        mean_sq = self.sum_ppv // self.sum_v
        variance = mean_sq - self.vwap * self.vwap
        if variance < 0:
            variance = 0           # truncation clamp, same as the RTL
        diff = self.vwap - price_e4 if price_e4 < self.vwap else 0
        below = (price_e4 < self.vwap
                 and diff * diff > (self.k2_q8 * variance) >> 8)
        above = price_e4 >= self.vwap

        sig = None
        if self.warmed_up:
            if self.primed:
                if not self.above_prev and above:      # SELL dominates
                    self.signals += 1
                    sig = VWAPSignal(side=SIDE_SELL, price_e4=price_e4,
                                     vwap=self.vwap)
                elif self.below_prev and not below:
                    self.signals += 1
                    sig = VWAPSignal(side=SIDE_BUY, price_e4=price_e4,
                                     vwap=self.vwap)
            self.below_prev = below
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


# ---------------------------------------------------------------------------
# Historical trade file reading — moved here (from backtest.py, v3.22) so
# backtest.py's offline backtests and bridge.py's live hardware replay
# share ONE exact reader instead of two that could quietly diverge. This
# is the lowest-level project module (no local imports), the same reason
# every mirror model lives here rather than in bridge.py or backtest.py.
# ---------------------------------------------------------------------------
def iter_trades(path: str):
    """Stream one (datetime, price_e4, qty) triple per line — never
    loads the whole file, since these can be gigabytes for a multi-
    year pull. qty is Alpaca's own trade-size field ("s", the same
    field name bridge.py's live path already reads) — real historical
    downloads always have it since fetch_historical_trades.py writes
    Alpaca's trade record verbatim; defaults to 1 for synthetic test
    data that doesn't bother setting it, so every existing caller that
    only cares about price keeps working unmodified."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            t = datetime.fromisoformat(rec["t"].replace("Z", "+00:00"))
            qty = int(rec.get("s", 1))
            yield t, to_e4(float(rec["p"])), qty


def iter_trades_multi(paths: list[str]):
    """Stream several trade files IN THE ORDER GIVEN, as if they were
    one continuous file. Meant for combining separately-fetched,
    non-overlapping date ranges (fetch_historical_trades.py now scopes
    filenames to their exact range, so incrementally widening your
    history means MULTIPLE files rather than one growing file — this
    is how you replay them together without re-downloading anything).

    Does a cheap streaming sanity check, not a full sort: if a later
    file's first trade is timestamped BEFORE the previous file's last
    trade, that's very likely the files were passed out of
    chronological order (or the ranges overlap), which would corrupt
    the replay's notion of "historical time" — this raises rather
    than silently replaying history out of order."""
    prev_last_t = None
    for path in paths:
        first_in_file = True
        for t, price_e4, qty in iter_trades(path):
            if first_in_file and prev_last_t is not None and t < prev_last_t:
                raise ValueError(
                    f"{path} starts at {t}, which is BEFORE the previous "
                    f"file's last trade at {prev_last_t} — files must be "
                    f"passed in chronological order with non-overlapping "
                    f"ranges, or the replay's historical clock breaks")
            first_in_file = False
            prev_last_t = t
            yield t, price_e4, qty
