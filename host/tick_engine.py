#!/usr/bin/env python3
"""
tick_engine.py — v3.46

The direct, in-process tick engine: ticks go straight into the same
SMAMirror/EMAMirror/VWAPMirror models that every strategy in this
project already runs on, and signals come straight back out. No serial
port, no pty, no wire protocol, no emulator process, no second
terminal.

WHY THIS REPLACED THE BRIDGE
----------------------------
The Bridge existed to talk to a real Arty A7-100T over UART and to
verify the fabric's own signals against independently-computed host
mirror models. Against real silicon that verification was genuine and
valuable: the RTL and the Python mirrors are truly independent
implementations, so a disagreement means a real bug in one of them.

Once the board left the picture, it stopped being verification at all.
fpga_emulator.py imports the SAME SMAMirror/EMAMirror/VWAPMirror
classes from tick_protocol that the host verifies against — its own
docstring says so plainly ("the same math, just without silicon
underneath it"). Comparing VWAPMirror against VWAPMirror is
structurally incapable of catching a math error; it can only catch
framing or transport corruption in a transport that exists solely to
emulate a cable that is no longer plugged into anything.

Meanwhile the architecture kept costing real bugs: the macOS baud/
ENOTTY failure (v3.30), pty zombie processes (v3.31), a hardcoded
baud in a test (v3.32), a /dev/pts naming assumption (v3.33), the
vwap_models orphaning that silently disabled risk-based sizing in
every session since v3.38 (v3.44), and the on_symbols_changed
overwrite that silently disabled STOP-LOSSES on any symbol added
mid-session (v3.46, found in the same live audit log that prompted
this rewrite). The last two were the most consequential bugs in the
project's history and both were artifacts of transport bookkeeping,
not of the trading logic.

The hardware path is NOT deleted. bridge.py, fpga_emulator.py, and
the RTL all remain in the tree, and order_manager.py still selects
them with --port. This module is simply what runs when you don't pass
one.

THE ONE STRUCTURAL FIX THAT MATTERS
-----------------------------------
Bridge._build_models() did `self.models = {...}` — it REASSIGNED the
whole models dict on every slot reconfiguration, orphaning any
reference anyone else had captured. That single line is the root of
both v3.44's and v3.46's bugs.

_build_models() here MUTATES the existing per-strategy dicts in place
and never reassigns them, so a reference captured once (om.vwap_models
= engine.models["vwap_bounce"]) stays correct forever, through any
number of symbol changes. There is no hook to forget to fire and no
hook for anything else to overwrite.

A deliberate consequence, and an improvement: adding a symbol
mid-session no longer resets the OTHER symbols' models. Under the
Bridge every reconfiguration rebuilt every model from scratch, which
threw away the running session VWAP for symbols you didn't touch —
silently moving their stops and their risk sizing mid-day. Models are
now created for new symbols, dropped for removed ones, and left alone
otherwise.
"""
from __future__ import annotations

import json
import queue
import time

from tick_protocol import (SMAMirror, EMAMirror, VWAPMirror,
                           dollars,
                           TYPE_ECHO_TRADE, TYPE_SIGNAL_SMA,
                           TYPE_SIGNAL_EMA, TYPE_SIGNAL_VWAP,
                           SIDE_NEUTRAL)


def now_us() -> int:
    return int(time.time() * 1_000_000)


class _NullParser:
    """Stand-in for Bridge.parser. There is no framing to resync, so
    the count is permanently zero — dashboard.py reads
    `.resync_count` off whatever engine it was handed."""
    resync_count = 0


class TickEngine:
    """Duck-type compatible with Bridge for everything order_manager.py
    and dashboard.py actually touch: symbols/symbol/models/sent/echoes/
    fpga_signals/rtt_us/parser/verifiers, configure_symbols/send_trade/
    pump/send_sessrst/summary/close, and the on_verified/on_echo/
    on_divergence/on_symbols_changed hooks.

    That compatibility is deliberate: run_sim/run_historical/run_alpaca
    in feeds.py drive this and the Bridge through the exact same calls,
    so there is one data-feed implementation rather than two that can
    drift apart.
    """

    def __init__(self, symbols, fast_n: int, slow_n: int,
                 k_fast: int, k_slow: int,
                 vwap_warmup: int = 20, vwap_k2_q8: int = 256,
                 log=None, quiet: bool = False,
                 report_strategies=None):
        self.symbols = [s.strip().upper() for s in symbols if s.strip()]
        if not self.symbols:
            raise ValueError("TickEngine needs at least one symbol")
        self.symbol = self.symbols[0]
        self.params = (fast_n, slow_n, k_fast, k_slow)
        self.vwap_params = (vwap_warmup, vwap_k2_q8)

        # NEVER reassigned after this line -- see the module docstring.
        # _build_models() mutates these three dicts in place.
        self.models = {"sma": {}, "ema": {}, "vwap_bounce": {}}
        self._build_models(self.symbols)

        self._q: queue.Queue = queue.Queue()
        self.log = log
        self.quiet = quiet
        # v3.52: which strategies print their signals to the terminal.
        # None means all of them. order_manager.py narrows this to the
        # ONE strategy that actually trades -- the others are still
        # scored, and still appear in saved results, but a live session
        # trading vwap_bounce has no use for a running commentary of
        # sma/ema crossings it will never act on.
        self.report_strategies = (set(report_strategies)
                                  if report_strategies is not None else None)

        # hooks (same names/semantics as Bridge)
        self.on_verified = None        # callback(fr) -- a strategy signal
        self.on_echo = None            # callback(fr) -- every accepted tick
        self.on_divergence = None      # never fires: one computation, so
                                       # there is nothing to disagree with.
                                       # Kept so callers can wire it
                                       # unconditionally.
        self.on_symbols_changed = None

        # counters dashboard.py reads
        self.sent = 0
        self.echoes = 0
        self.fpga_signals = 0          # signals produced, keeping the
                                       # dashboard's existing field name
        self.signals_by_strategy = {"sma": 0, "ema": 0, "vwap_bounce": 0}
        self.rtt_us: list[int] = []    # stays empty: no round trip exists
        self.parser = _NullParser()
        self.verifiers: dict = {}      # stays empty: nothing to verify

    # ---- models ------------------------------------------------------
    def _build_models(self, symbols) -> None:
        """Create models for newly-added symbols, drop models for
        removed ones, and leave every other symbol's model UNTOUCHED.
        Mutates in place; never rebinds self.models or its inner dicts."""
        f, sl, kf, ks = self.params
        vw, vk2 = self.vwap_params
        factories = {
            "sma": lambda: SMAMirror(fast_n=f, slow_n=sl),
            "ema": lambda: EMAMirror(k_fast=kf, k_slow=ks, warmup_n=sl),
            "vwap_bounce": lambda: VWAPMirror(warmup_n=vw, k2_q8=vk2),
        }
        keep = set(symbols)
        for name, make in factories.items():
            d = self.models[name]
            for sym in symbols:
                if sym not in d:
                    d[sym] = make()
            for sym in [s for s in d if s not in keep]:
                del d[sym]

    def configure_symbols(self, symbols, **_kw) -> bool:
        """Always succeeds -- there are no hardware slots to negotiate.
        Kept as a method (and kept returning bool) so feeds.py drives
        this and the Bridge identically."""
        symbols = [s.strip().upper() for s in symbols if s.strip()]
        if not symbols:
            return False
        added = [s for s in symbols if s not in self.models["sma"]]
        dropped = [s for s in self.models["sma"] if s not in set(symbols)]
        self.symbols = symbols
        self.symbol = symbols[0]
        self._build_models(symbols)
        if not self.quiet:
            note = []
            if added:
                note.append(f"+{','.join(added)}")
            if dropped:
                note.append(f"-{','.join(dropped)}")
            print(f"[engine] symbols: {symbols}"
                  + (f"  ({' '.join(note)}; existing symbols' models "
                     f"left running)" if note else ""))
        if self.on_symbols_changed:
            self.on_symbols_changed(symbols)
        return True

    def send_sessrst(self, slot: int = None, timeout: float = 3.0) -> bool:
        """Session reset: clears the session-VWAP accumulation, the same
        thing the sessrst_ack path did on the Bridge. `slot` and
        `timeout` are accepted and ignored -- there is no device to
        acknowledge anything."""
        for m in self.models["vwap_bounce"].values():
            m.sess_reset()
        if not self.quiet:
            print("[engine] session VWAP reset")
        return True

    # ---- tick ingress -------------------------------------------------
    def send_trade(self, price_e4: int, qty: int,
                   side: int = SIDE_NEUTRAL,
                   symbol: str | None = None) -> int:
        """Enqueue a tick. Deliberately does NOT process it inline: the
        Bridge decoupled the reader thread from processing via a queue
        so that every callback (and therefore every real order) ran on
        the main thread, and run_alpaca still calls this from the
        websocket thread. Keeping the queue keeps that guarantee."""
        ts = now_us()
        self._q.put((symbol or self.symbol, price_e4, qty, side, ts))
        self.sent += 1
        return ts

    def pump(self, timeout: float = 0.0) -> None:
        """Drain and process queued ticks on the CALLING thread."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                remaining = max(0.0, deadline - time.monotonic())
                item = self._q.get(timeout=remaining) if timeout else \
                       self._q.get_nowait()
            except queue.Empty:
                return
            self._process(*item)
            if not timeout:
                continue

    # ---- the actual work ---------------------------------------------
    def _process(self, sym: str, price_e4: int, qty: int,
                 side: int, ts: int) -> None:
        if sym not in self.models["sma"]:
            return                      # not a configured symbol; the
                                        # Bridge's slot compare dropped
                                        # these too
        self.echoes += 1

        # The "echo" frame. There is no echo in the literal sense any
        # more, but the shape and the TYPE_ECHO_TRADE tag are what
        # order_manager.py's per-tick risk hook and the --log format
        # already expect, so both keep working unchanged.
        echo = {"kind": "echo", "type": TYPE_ECHO_TRADE, "symbol": sym,
                "price_e4": price_e4, "qty": qty, "side": side,
                "host_ts": ts, "fpga_ts": ts}
        if self.log:
            self.log.write(json.dumps({"t": ts, **{k: echo[k] for k in
                           ("type", "symbol", "price_e4", "qty", "side",
                            "host_ts", "fpga_ts")}, "rtt_us": 0}) + "\n")
        if self.on_echo:
            self.on_echo(echo)
            # fired BEFORE any strategy signal below, matching the
            # Bridge's own ordering (echo frame first, fabric signal
            # after) -- it is also the safer order, since that hook is
            # where the stop-loss check lives and a breached stop
            # should fire before a fresh entry signal is considered.

        for name, ftype in (("sma", TYPE_SIGNAL_SMA),
                            ("ema", TYPE_SIGNAL_EMA)):
            sig = self.models[name][sym].ingest(price_e4)
            if sig:
                self._emit({"kind": "signal", "signal": True, "type": ftype,
                            "strategy": name, "symbol": sym,
                            "price_e4": sig.price_e4, "side": sig.side,
                            "sma_fast": sig.sma_fast,
                            "sma_slow": sig.sma_slow, "fpga_ts": ts},
                           name, sym, sig.side_name, sig.price_e4)

        vsig = self.models["vwap_bounce"][sym].ingest(price_e4, qty)
        if vsig:
            self._emit({"kind": "signal", "signal": True,
                        "type": TYPE_SIGNAL_VWAP, "strategy": "vwap_bounce",
                        "symbol": sym, "price_e4": vsig.price_e4,
                        "side": vsig.side, "vwap": vsig.vwap,
                        "eval_skips": 0, "fpga_ts": ts},
                       "vwap_bounce", sym, vsig.side_name, vsig.price_e4)

    def _emit(self, fr: dict, strategy: str, sym: str,
              side_name: str, price_e4: int) -> None:
        self.fpga_signals += 1
        self.signals_by_strategy[strategy] += 1
        if not self.quiet and (self.report_strategies is None
                               or strategy in self.report_strategies):
            print(f">> [{strategy}] {sym}: {side_name} @ "
                  f"${dollars(price_e4):.4f}")
        if self.on_verified:
            self.on_verified(fr)

    # ---- lifecycle ----------------------------------------------------
    def summary(self) -> bool:
        """Returns True (the Bridge returned False on divergence; a
        single computation cannot diverge from itself)."""
        by = ", ".join(f"{k}={v}" for k, v in self.signals_by_strategy.items())
        print(f"\n[engine] {self.sent} ticks sent, {self.echoes} processed, "
              f"{self.fpga_signals} signals ({by})")
        print(f"[engine] symbols: {self.symbols}")
        return True

    def close(self) -> None:
        try:
            self.pump()          # drain anything still queued
        except Exception:
            pass
        if self.log:
            try:
                self.log.flush()
            except Exception:
                pass
