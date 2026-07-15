#!/usr/bin/env python3
"""
vwap_bounce_strategy.py — session-VWAP mean-reversion "bounce" strategy.

VWAP = cumulative(price × volume) / cumulative(volume), reset at the
start of each trading session (here: each ET calendar day, matching
every other daily-scoped convention in this project — RiskPolicy's
order cap, the OM's day-scoped P&L persistence, etc.). It's a running,
volume-weighted "fair value" anchor for the day, not a fixed window.

The strategy: track a band around VWAP using the session's own
volume-weighted standard deviation (VWAP ± k·stdev — the same idea as
Bollinger Bands, but centered on VWAP instead of a simple moving
average). BUY when price dips below the lower band and then bounces
back above it (mean-reversion entry); SELL when price reverts back up
to VWAP itself (take-profit at fair value) OR when the session ends
(VWAP resets each day, so positions are forced flat at the day
boundary — see below for why).

Two deliberate scope decisions, stated plainly rather than left
implicit:

  * LONG-ONLY, bounces off the LOWER band only. Nothing in this
    project shorts (same reasoning as every other strategy here); the
    upper-band bounce (fade a spike back down) would be the natural
    short-side mirror and isn't built.
  * POSITIONS ARE FORCED FLAT AT EACH SESSION BOUNDARY. VWAP is only
    meaningful within the session it's computed over — carrying a
    position into a new session means the entry was measured against
    an anchor that no longer exists. This also happens to bound the
    worst case: without it, a position that never reverts could be
    held indefinitely (the same disposition-effect risk flagged for
    the profit-gated strategy) — forcing flat at day's end caps that
    at one trading day, by construction, not as an afterthought.

This is FPGA-suited in principle (VWAP reacts to every tick, the way
SMA/EMA do, unlike the bar-based HTF/LTF strategy) — but genuinely
harder to build in fabric than SMA/EMA were, for two real reasons
worth remembering if this ever gets built in RTL: the divisor
(cumulative volume) isn't a power of two, so it needs a real divider
core instead of a bit-shift; and VWAP needs a SESSION BOUNDARY concept
(daily reset) that nothing in the current RTL has — the symcfg
register-write-resets-engine-state mechanism built for multi-symbol
support would extend naturally to it, but it's new machinery, not a
free extension. This module is the host-side backtest version, built
first specifically to find out whether the strategy shows any real
signal before investing in that hardware effort.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from compare import StrategyScorecard
from tick_protocol import SIDE_BUY, SIDE_SELL


@dataclass
class VWAPBounceScorecard(StrategyScorecard):
    symbol: str = ""
    band_k: float = 1.0            # band width, in session stdevs
    min_session_ticks: int = 20    # warmup floor before trusting the band

    def __post_init__(self):
        self._session_date = None
        self._sum_pv = 0.0         # Σ price·qty  (for VWAP)
        self._sum_v = 0.0          # Σ qty
        self._sum_ppv = 0.0        # Σ price²·qty (for session variance)
        self._n = 0
        self._below_band_prev: bool | None = None
        self.vwap = None
        self.lower_band = None
        self.forced_flat_count = 0   # sessions ended with a position still
                                    # open, force-closed at the boundary

    @property
    def warmed_up(self) -> bool:
        return self._n >= self.min_session_ticks

    def _reset_session(self, new_date):
        self._session_date = new_date
        self._sum_pv = self._sum_v = self._sum_ppv = 0.0
        self._n = 0
        self._below_band_prev = None
        self.vwap = None
        self.lower_band = None

    def on_tick(self, t: datetime, price_e4: int, qty: int):
        """Feed every raw (timestamp, price, volume) tick here — NOT
        on_signal(), same convention as HTFLTFScorecard.on_tick(). Uses
        on_signal() internally once it decides to buy or sell, so cost
        basis / fees / reporting all match every other row for free."""
        if self.policy is not None and hasattr(self.policy._now_fn, "set"):
            self.policy._now_fn.set(t)

        day = t.date()
        if self._session_date is None:
            self._reset_session(day)
        elif day != self._session_date:
            # new session: VWAP resets, so any open position is forced
            # flat FIRST, against the OLD session's still-valid VWAP —
            # not carried into a new session where that anchor is
            # meaningless (see module docstring)
            if self.positions.get(self.symbol, 0) > 0 and self.vwap is not None:
                self.forced_flat_count += 1
                self.on_signal({"side": SIDE_SELL,
                               "price_e4": int(self.vwap),
                               "symbol": self.symbol,
                               "strategy": "vwap_bounce"})
            self._reset_session(day)

        qty = max(qty, 1)          # a zero/missing size must not zero out
                                   # the denominator and break VWAP
        price = float(price_e4)
        self._sum_pv += price * qty
        self._sum_v += qty
        self._sum_ppv += price * price * qty
        self._n += 1

        self.vwap = self._sum_pv / self._sum_v
        variance = max(0.0, self._sum_ppv / self._sum_v - self.vwap ** 2)
        stdev = math.sqrt(variance)
        self.lower_band = self.vwap - self.band_k * stdev

        if not self.warmed_up:
            return None

        below = price_e4 < self.lower_band
        prev = self._below_band_prev
        self._below_band_prev = below
        in_pos = self.positions.get(self.symbol, 0) > 0
        outcome = None

        if not in_pos:
            bounced_back_up = (prev is True and not below)
            if bounced_back_up:
                outcome = self.on_signal({
                    "side": SIDE_BUY, "price_e4": price_e4,
                    "symbol": self.symbol, "strategy": "vwap_bounce"})
        else:
            if price_e4 >= self.vwap:      # reverted to fair value: exit
                outcome = self.on_signal({
                    "side": SIDE_SELL, "price_e4": price_e4,
                    "symbol": self.symbol, "strategy": "vwap_bounce"})
        return outcome
