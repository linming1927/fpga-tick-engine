#!/usr/bin/env python3
"""
compare.py — score both strategies from the same session, fairly.

Only ONE strategy actually trades (the order manager's --strategy). But
every VERIFIED signal from both engines flows through a StrategyScorecard,
which simulates the identical minimal policy hypothetically: long-only,
1 share, BUY opens if flat, SELL closes if holding, sell-side regulatory
fees applied. Because both engines consumed the same ticks and both
scorecards apply the same policy, the comparison isolates the one variable
that differs — the indicator math.

What this is honest about:
  * hypothetical fills are AT THE SIGNAL PRICE — no slippage, no spread.
    Both strategies get the same optimistic treatment, so the COMPARISON
    is fair even though the absolute numbers flatter both.
  * an open position at session end is not scored (unrealized) — the
    open_e4 field says so in the report.
  * a handful of round trips is NOISE, not evidence. The report prints the
    trip count next to the P&L for exactly that reason; treat any
    conclusion from n < dozens of trips as anecdote.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from costs import FeeSchedule
from tick_protocol import SIDE_BUY, SIDE_SELL, dollars


@dataclass
class StrategyScorecard:
    name: str
    qty: int = 1
    fees: FeeSchedule = field(default_factory=FeeSchedule)

    signals: int = 0
    trips: int = 0                  # completed round trips (buy -> sell)
    wins: int = 0
    pnl_e4: int = 0                 # gross, fixed-point x10000
    fees_usd: float = 0.0
    opens: dict = field(default_factory=dict)   # symbol -> entry price

    @property
    def open_e4(self):              # back-compat for single-symbol tests
        return next(iter(self.opens.values()), None)

    def on_signal(self, fr: dict):
        self.signals += 1
        side, price = fr["side"], fr["price_e4"]
        sym = fr.get("symbol", "").strip()
        if side == SIDE_BUY and sym not in self.opens:
            self.opens[sym] = price
        elif side == SIDE_SELL and sym in self.opens:
            trip = (price - self.opens.pop(sym)) * self.qty
            self.pnl_e4 += trip
            self.trips += 1
            if trip > 0:
                self.wins += 1
            self.fees_usd += self.fees.sell_fees(
                self.qty, self.qty * price / 10_000.0)["total"]

    @property
    def net_usd(self) -> float:
        return self.pnl_e4 / 10_000.0 - self.fees_usd

    def row(self) -> str:
        wr = f"{100*self.wins/self.trips:.0f}%" if self.trips else "  —"
        open_s = (f"{len(self.opens)} open" if self.opens else "flat")
        return (f"  {self.name:<14} {self.signals:>7} {self.trips:>6} "
                f"{wr:>5}  {self.pnl_e4/10_000:>+10.2f} {self.fees_usd:>7.2f} "
                f"{self.net_usd:>+10.2f}  {open_s}")


def comparison_report(cards: dict[str, StrategyScorecard]) -> str:
    lines = ["---- strategy comparison (hypothetical, 1 share, signal-price "
             "fills) ----",
             "  strategy       signals  trips  win     gross $  fees $"
             "      net $  position"]
    lines += [c.row() for c in cards.values()]
    trips = [c.trips for c in cards.values()]
    if trips and max(trips) < 20:
        lines.append("  note: few round trips — treat as anecdote, "
                     "not evidence")
    return "\n".join(lines)
