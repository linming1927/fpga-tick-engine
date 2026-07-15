#!/usr/bin/env python3
"""
ladder_strategy.py — weekly-anchored mean-reversion ladder (score-only).

A genuinely different animal from the SMA/EMA crossovers: instead of
reacting to a moving-average cross on every tick, this strategy compares
the current price against a fixed BASELINE re-anchored once a week, and
buys in tranches as price falls below it:

    level 1 buy   price <= baseline * (1 - step)
    level 2 buy   price <= baseline * (1 - 2*step)
    level 3 buy   price <= baseline * (1 - 3*step)      ... up to max_levels
    sell (all)    price >= baseline * (1 + step), while holding

Because the trigger is a STATIC level rather than a moving indicator, the
strategy needs no time-based cooldown the way SMA/EMA do — the level
index itself provides hysteresis: having already bought level N, only a
further price drop (crossing level N+1) or a full recovery (the sell
threshold) can fire again. This is a structural property, not a
workaround; it's worth noting because every other strategy in this
project (and the RiskPolicy that gates them) leans on a wall-clock
cooldown for exactly this purpose.

DELIBERATELY SCORE-ONLY, not wired to trade. It never touches
OrderManager or a broker. Feed it ticks via on_tick()/on_signal(), same
event shape as a verified FPGA signal ({"side", "price_e4", "symbol"}),
and read its numbers the same way as any other StrategyScorecard row in
the comparison report.

This is functionally a grid / martingale-style averaging-down ladder:
exposure GROWS as price falls further, which is exactly backwards from
"cut losses, let winners run." It performs fine in a genuinely
range-bound week and can compound badly in a real, sustained trend,
since there is (deliberately, per current settings) no stop-loss and no
cap beyond max_levels * qty_per_level. Treat any comparison-table numbers
from this strategy with that in mind — a few good weeks prove nothing
about the one week a real trend runs through it.

Re-anchoring semantics: calling set_baseline() with a new value does NOT
force-close an open position. Levels bought and the weighted-average
cost basis carry over; only the buy/sell trigger PRICES move to the new
baseline from that point on. This is a design choice, not the only
possible one — an alternative would force-flatten every Monday before
re-anchoring. Change on_new_baseline() below if you'd rather do that.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from compare import StrategyScorecard
from tick_protocol import SIDE_BUY, SIDE_SELL, to_e4


# ---------------------------------------------------------------------------
# Baseline calculation — pure functions, no network, easy to test in isolation
# ---------------------------------------------------------------------------

BASELINE_METHODS = ("friday_close", "week_avg_close", "week_vwap",
                    "week_midpoint")


def compute_weekly_baseline(daily_bars: list[dict],
                            method: str = "week_vwap") -> int:
    """daily_bars: list of {"o","h","l","c","v"} dicts (Alpaca bar shape,
    prices in dollars), oldest first, covering ONE prior week (<=5 bars).
    Returns the baseline as price_e4 (dollars x 10000, matching the wire
    format). Raises ValueError on an empty list or unknown method.

    Method choices (no one "correct" answer — pick what you want to test):
      friday_close    last bar's close — simplest, most noise-sensitive
      week_avg_close  simple average of the week's daily closes
      week_vwap       volume-weighted average close across the week —
                      weights toward where the week's volume actually
                      traded; the closest thing to a "fair value" anchor
      week_midpoint   (week high + week low) / 2 — a range-center measure
    """
    if not daily_bars:
        raise ValueError("no daily bars supplied")
    if method not in BASELINE_METHODS:
        raise ValueError(f"unknown method {method!r}, choose from "
                         f"{BASELINE_METHODS}")

    if method == "friday_close":
        return to_e4(daily_bars[-1]["c"])

    if method == "week_avg_close":
        closes = [b["c"] for b in daily_bars]
        return to_e4(sum(closes) / len(closes))

    if method == "week_vwap":
        num = sum(b["c"] * b["v"] for b in daily_bars)
        den = sum(b["v"] for b in daily_bars)
        if den == 0:
            return to_e4(daily_bars[-1]["c"])       # no volume: fall back
        return to_e4(num / den)

    if method == "week_midpoint":
        hi = max(b["h"] for b in daily_bars)
        lo = min(b["l"] for b in daily_bars)
        return to_e4((hi + lo) / 2)


def fetch_prior_week_bars(symbol: str, key: str, secret: str,
                          feed: str = "iex") -> list[dict]:
    """Pull the prior Mon-Fri's daily bars from Alpaca's market-data REST
    endpoint (one call — well within the free tier's 200/min limit).
    Uses the same stdlib-urllib pattern as order_manager.py's broker
    classes; no extra dependency. Call this once, before Monday's open,
    per symbol you're scoring."""
    today = datetime.now(timezone.utc).date()
    # most recent Monday strictly before today, and the Friday before that
    last_monday = today - timedelta(days=today.weekday() or 7)
    prior_monday = last_monday - timedelta(days=7)
    prior_friday = last_monday - timedelta(days=3)
    url = (f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
          f"?timeframe=1Day&start={prior_monday}&end={prior_friday}"
          f"&feed={feed}&limit=10")
    req = urllib.request.Request(
        url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"bar fetch failed: HTTP {e.code} "
                          f"{e.read().decode(errors='replace')[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"bar fetch failed: {e.reason}")
    bars = data.get("bars") or []
    if not bars:
        raise RuntimeError(f"no bars returned for {symbol} "
                          f"{prior_monday}..{prior_friday}")
    return [{"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
             "v": b["v"]} for b in bars]


# ---------------------------------------------------------------------------
# The strategy itself — reuses StrategyScorecard's reporting shape, but
# overrides on_signal() to track a WEIGHTED-AVERAGE cost basis across
# multiple buy levels rather than the parent's single-entry overwrite,
# since a ladder can hold several buys before one sell closes them all.
# ---------------------------------------------------------------------------

@dataclass
class LadderScorecard(StrategyScorecard):
    step_pct: float = 0.03
    max_levels: int = 3
    qty_per_level: int = 1
    baseline_e4: dict = field(default_factory=dict)     # symbol -> anchor
    levels_bought: dict = field(default_factory=dict)   # symbol -> int

    def set_baseline(self, symbol: str, baseline_e4: int):
        """Call once per symbol at the start of each week (before Monday's
        open). Does NOT force-close an open position — see module
        docstring "Re-anchoring semantics"."""
        self.baseline_e4[symbol] = baseline_e4

    def on_tick(self, symbol: str, price_e4: int) -> dict | None:
        """Evaluate one price against this symbol's ladder. Returns an
        event dict shaped like a verified FPGA signal — pass it straight
        to on_signal() — or None if nothing fired this tick."""
        base = self.baseline_e4.get(symbol)
        if base is None:
            return None                       # no baseline set yet: inert
        lvl = self.levels_bought.get(symbol, 0)
        qty = self.positions.get(symbol, 0)

        if qty > 0 and price_e4 >= base * (1 + self.step_pct):
            return {"side": SIDE_SELL, "price_e4": price_e4,
                    "symbol": symbol, "strategy": "ladder"}
        if lvl < self.max_levels:
            trigger = base * (1 - self.step_pct * (lvl + 1))
            if price_e4 <= trigger:
                return {"side": SIDE_BUY, "price_e4": price_e4,
                        "symbol": symbol, "strategy": "ladder"}
        return None

    def on_signal(self, fr: dict) -> str:
        self.signals += 1
        side, price = fr["side"], fr["price_e4"]
        sym = fr.get("symbol", "").strip()

        if side == SIDE_BUY:
            lvl = self.levels_bought.get(sym, 0)
            if lvl >= self.max_levels:
                return "ignored: ladder full"
            old_qty = self.positions.get(sym, 0)
            old_avg = self.opens.get(sym, price)
            new_qty = old_qty + self.qty_per_level
            # weighted-average entry across levels (same arithmetic as
            # costs.py's CostTracker — see its header for why)
            self.opens[sym] = ((old_avg * old_qty + price * self.qty_per_level)
                               // new_qty) if old_qty else price
            self.positions[sym] = new_qty
            self.levels_bought[sym] = lvl + 1
            return f"FILLED (scored): level {lvl + 1}/{self.max_levels}"

        elif side == SIDE_SELL:
            qty = self.positions.get(sym, 0)
            if qty <= 0:
                return "ignored: flat"
            entry = self.opens.pop(sym, price)
            trip = (price - entry) * qty
            self.pnl_e4 += trip
            self.trips += 1
            if trip > 0:
                self.wins = (self.wins or 0) + 1
            self.fees_usd += self.fees.sell_fees(
                qty, qty * price / 10_000.0)["total"]
            self.positions[sym] = 0
            self.levels_bought[sym] = 0
            return "FILLED (scored): ladder closed"
