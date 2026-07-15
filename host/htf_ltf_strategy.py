#!/usr/bin/env python3
"""
htf_ltf_strategy.py — multi-timeframe trend-alignment strategy.

Models a real, commonly-described discretionary style (related to what
Alexander Elder called "triple screen" trading): a HIGHER timeframe's
20/50/200 EMA stack sets the trend BIAS (only trade with it, never
against it); a LOWER timeframe's fast/slow EMA cross times the actual
ENTRY once that bias is set; the position is then TRAILED — held open
as long as price keeps closing on the right side of the LTF fast EMA,
and closed the moment that structure breaks, independent of whether the
HTF bias has technically reversed yet.

Built by explicit request, with two scope decisions worth restating:

  * LONG-ONLY. Nothing in this project ever shorts — every strategy
    here (SMA, EMA, the ladder, profit-gated) only buys-to-open and
    sells-to-close. A bearish HTF bias just means "stay flat," not
    "go short." Real short-selling would need position-side semantics
    this codebase doesn't have anywhere; out of scope here.
  * EXACT EMA math (alpha = 2/(N+1)), NOT the power-of-two alpha used
    by every other engine in this project. Those use 2^-K specifically
    so the update is a hardware-friendly bit-shift — a real constraint
    for something meant to run on the FPGA. This strategy is backtest-
    only and never needs to run in fabric, so there's no reason to
    force that same approximation; "the 20 EMA" here means exactly
    what a trader means by it, not the nearest power-of-two stand-in
    (which would land closer to a ~15- or ~31-period EMA, matching
    neither 20 nor 50 nor 200 exactly).

Architecturally, this is the first strategy in the project that needs
BARS, not raw ticks — everything else operates tick-by-tick. So this
module also provides simple OHLC bar aggregation from a tick stream,
something nothing else here has needed until now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from compare import StrategyScorecard
from tick_protocol import SIDE_BUY, SIDE_SELL


# ---------------------------------------------------------------------------
# Bar aggregation — the one genuinely new primitive this strategy needs
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    start: datetime
    open_e4: int
    high_e4: int
    low_e4: int
    close_e4: int


def _bucket_start(t: datetime, interval_s: int) -> datetime:
    """Floor t to the start of its interval_s-second bucket, epoch-
    aligned (so bucket boundaries are stable regardless of where the
    data stream happens to start)."""
    epoch = t.timestamp()
    bucket = int(epoch // interval_s) * interval_s
    return datetime.fromtimestamp(bucket, tz=timezone.utc)


class BarAggregator:
    """Feed ticks one at a time via on_tick(); get back a COMPLETED bar
    exactly when a tick from a new bucket arrives (the just-finished
    bar), or None while still accumulating the current one. Call
    flush() at the end of a stream for the final, possibly-partial bar."""

    def __init__(self, interval_s: int):
        self.interval_s = interval_s
        self._start: datetime | None = None
        self._o = self._h = self._l = self._c = None

    def on_tick(self, t: datetime, price_e4: int) -> Bar | None:
        bstart = _bucket_start(t, self.interval_s)
        if self._start is None:
            self._start = bstart
            self._o = self._h = self._l = self._c = price_e4
            return None
        if bstart != self._start:
            completed = Bar(self._start, self._o, self._h, self._l, self._c)
            self._start = bstart
            self._o = self._h = self._l = self._c = price_e4
            return completed
        self._h = max(self._h, price_e4)
        self._l = min(self._l, price_e4)
        self._c = price_e4
        return None

    def flush(self) -> Bar | None:
        if self._start is not None:
            return Bar(self._start, self._o, self._h, self._l, self._c)
        return None


class SingleEMA:
    """Textbook single EMA, alpha = 2/(N+1) — see module docstring for
    why this deliberately does NOT reuse this project's usual power-
    of-two-alpha EMA math."""

    def __init__(self, period: int):
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: float | None = None
        self.n = 0

    def update(self, price_e4: int) -> float:
        if self.value is None:
            self.value = float(price_e4)
        else:
            self.value = self.alpha * price_e4 + (1 - self.alpha) * self.value
        self.n += 1
        return self.value

    @property
    def warmed_up(self) -> bool:
        return self.n >= self.period


# ---------------------------------------------------------------------------
# The strategy
# ---------------------------------------------------------------------------

@dataclass
class HTFLTFScorecard(StrategyScorecard):
    """HTF 20/50/200 EMA stack sets a long-only bias; LTF fast/slow EMA
    cross times entries (only in the bias direction); the position
    trails until an LTF bar closes back below the LTF fast EMA.

    Feed raw ticks via on_tick(t, price_e4) — NOT on_signal(), which
    this class still supports (inherited) purely as the internal
    bookkeeping mechanism once on_tick() decides to buy or sell; that's
    what gives this strategy the exact same weighted-average cost
    basis, fee accounting, and reporting shape as every other row in
    the comparison table, for free.
    """

    symbol: str = ""
    htf_interval_s: int = 3600     # 1 hour
    ltf_interval_s: int = 300      # 5 minutes
    htf_periods: tuple = (20, 50, 200)
    ltf_periods: tuple = (20, 50)

    def __post_init__(self):
        self._htf_agg = BarAggregator(self.htf_interval_s)
        self._ltf_agg = BarAggregator(self.ltf_interval_s)
        self._htf_emas = {p: SingleEMA(p) for p in self.htf_periods}
        self._ltf_emas = {p: SingleEMA(p) for p in self.ltf_periods}
        self._ltf_fast_p = min(self.ltf_periods)
        self._ltf_slow_p = max(self.ltf_periods)
        self._htf_fast_p, self._htf_mid_p, self._htf_slow_p = \
            sorted(self.htf_periods)
        self.bias = "none"                  # "bullish" / "bearish" / "none"
        self._ltf_fast_above_prev: bool | None = None
        self.htf_bars_seen = 0
        self.ltf_bars_seen = 0

    @property
    def htf_warmed_up(self) -> bool:
        return all(e.warmed_up for e in self._htf_emas.values())

    @property
    def ltf_warmed_up(self) -> bool:
        return all(e.warmed_up for e in self._ltf_emas.values())

    def _update_bias(self, htf_bar: Bar):
        for ema in self._htf_emas.values():
            ema.update(htf_bar.close_e4)
        if not self.htf_warmed_up:
            self.bias = "none"
            return
        f = self._htf_emas[self._htf_fast_p].value
        m = self._htf_emas[self._htf_mid_p].value
        s = self._htf_emas[self._htf_slow_p].value
        if f > m > s:
            self.bias = "bullish"
        elif f < m < s:
            self.bias = "bearish"
        else:
            self.bias = "none"

    def on_tick(self, t: datetime, price_e4: int):
        """The main entry point — feed every raw tick here, not
        on_signal(). Returns the same outcome string on_signal() would
        (for GUI/logging consistency) if a buy or sell actually fired
        this tick, else None."""
        # If this card's policy is replaying historical data (backtest),
        # its clock is a HistoricalClock — advance it to THIS tick's own
        # timestamp before any signal decision, exactly like the SMA/EMA
        # backtest clocks. In live mode the policy's clock is real
        # wall-time with no .set() method, so this is a harmless no-op.
        if self.policy is not None and hasattr(self.policy._now_fn, "set"):
            self.policy._now_fn.set(t)

        htf_bar = self._htf_agg.on_tick(t, price_e4)
        if htf_bar:
            self.htf_bars_seen += 1
            self._update_bias(htf_bar)

        ltf_bar = self._ltf_agg.on_tick(t, price_e4)
        if ltf_bar is None:
            return None
        self.ltf_bars_seen += 1
        for ema in self._ltf_emas.values():
            ema.update(ltf_bar.close_e4)
        if not self.ltf_warmed_up:
            return None

        fast_v = self._ltf_emas[self._ltf_fast_p].value
        slow_v = self._ltf_emas[self._ltf_slow_p].value
        above = fast_v > slow_v
        prev = self._ltf_fast_above_prev
        self._ltf_fast_above_prev = above

        in_pos = self.positions.get(self.symbol, 0) > 0
        outcome = None

        if not in_pos:
            fresh_cross_up = (prev is not None and above and not prev)
            if self.bias == "bullish" and fresh_cross_up:
                outcome = self.on_signal({
                    "side": SIDE_BUY, "price_e4": ltf_bar.close_e4,
                    "symbol": self.symbol, "strategy": "htf_ltf"})
        else:
            # trail: exit the moment an LTF bar CLOSES back below the
            # LTF fast EMA — independent of whether the HTF bias has
            # reversed yet, matching "trail as long as LTF EMAs are
            # respected" (the HTF bias gates ENTRIES only, per the
            # strategy as described)
            if ltf_bar.close_e4 < fast_v:
                outcome = self.on_signal({
                    "side": SIDE_SELL, "price_e4": ltf_bar.close_e4,
                    "symbol": self.symbol, "strategy": "htf_ltf"})
        return outcome
