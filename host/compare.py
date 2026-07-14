#!/usr/bin/env python3
"""
compare.py — score both strategies from the same session, fairly.

Only ONE strategy actually trades (the order manager's --strategy). The
TRADED strategy's row is the real thing: filled straight from the OM's own
CostTracker at session end, so it matches Alpaca's fills to the cent.

The UNTRADED strategy has no real fills to report, so it's replayed
through its own RiskPolicy CLONE — built from the exact same RiskLimits
the live OM enforces, ticking on the same wall clock as verified signals
arrive. That means the untraded row answers "how would this strategy have
fared under IDENTICAL constraints" — same cooldown, same daily order cap,
same market-hours gate — not "if every signal had become a trade".

This replaces an earlier, naive version that turned every verified signal
into a hypothetical trade with no throttling at all. On a real session
that produced ~800 verified crossovers, the earlier scorecard opened and
closed ~400 hypothetical round trips while the real, risk-gated system
made 10 fills — an ~80x mismatch in trading frequency that made the two
rows incomparable. Gating both strategies identically fixes that.

What this is still honest about:
  * the untraded row's fills are AT THE SIGNAL PRICE — no slippage, no
    spread — so it's mildly optimistic versus the traded row's real fills.
  * an open position at session end is not scored (unrealized).
  * a handful of round trips is NOISE, not evidence. The report prints the
    trip count next to the P&L for exactly that reason; treat any
    conclusion from n < dozens of trips as anecdote.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from costs import FeeSchedule
from tick_protocol import SIDE_BUY, SIDE_SELL, dollars


@dataclass
class StrategyScorecard:
    name: str
    qty: int = 1                    # ungated (policy=None) fill size only
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    # RiskPolicy clone: when set, on_signal() gates through it exactly as
    # the real OrderManager gates the live strategy — same evaluate() /
    # record_order() calls, own independent cooldown/day-counter state.
    # None means ungated (every signal fills) — used for the live row,
    # which is overwritten from the OM's real numbers anyway, and for
    # standalone/test use.
    policy: object | None = None
    live: bool = False              # True: numbers come from real OM fills

    signals: int = 0
    blocked: int = 0                # gated-away (policy) or real (live)
    block_reasons: Counter = field(default_factory=Counter)
    trips: int = 0                  # completed round trips (buy -> sell)
    wins: int | None = 0            # None = unknown (live row: CostTracker
                                    # doesn't track per-trip win/loss)
    pnl_e4: int = 0                 # gross, fixed-point x10000
    fees_usd: float = 0.0
    positions: dict = field(default_factory=dict)  # symbol -> qty (hyp.)
    opens: dict = field(default_factory=dict)       # symbol -> entry price

    @property
    def open_e4(self) -> int | None:
        return next(iter(self.opens.values()), None)

    def on_signal(self, fr: dict):
        self.signals += 1
        side, price = fr["side"], fr["price_e4"]
        sym = fr.get("symbol", "").strip()
        pos_qty = self.positions.get(sym, 0)

        if self.policy is not None:
            allowed, reason, qty = self.policy.evaluate(side, pos_qty,
                                                         price)
            if not allowed:
                self.blocked += 1
                self.block_reasons[reason.split(" (")[0]] += 1
                return
            self.policy.record_order()
        else:
            if side == SIDE_BUY and pos_qty > 0:
                return                       # ignore buy-while-open
            if side == SIDE_SELL and pos_qty <= 0:
                return                       # nothing to sell
            qty = self.qty if side == SIDE_BUY else pos_qty

        if side == SIDE_BUY:
            self.positions[sym] = qty
            self.opens[sym] = price
        elif side == SIDE_SELL:
            entry = self.opens.pop(sym, price)
            trip = (price - entry) * qty
            self.pnl_e4 += trip
            self.trips += 1
            if trip > 0:
                self.wins = (self.wins or 0) + 1
            self.fees_usd += self.fees.sell_fees(
                qty, qty * price / 10_000.0)["total"]
            self.positions[sym] = 0

    @property
    def net_usd(self) -> float:
        return self.pnl_e4 / 10_000.0 - self.fees_usd

    def row(self) -> str:
        wr = (f"{100*self.wins/self.trips:.0f}%"
              if (self.wins is not None and self.trips) else "  —")
        open_n = sum(1 for v in self.positions.values() if v)
        open_s = f"{open_n} open" if open_n else "flat"
        tag = " [LIVE]" if self.live else ""
        blk = ""
        if self.blocked:
            kind = "blocked" if self.live else "gated"
            blk = f"  ({self.blocked} {kind})"
        return (f"  {self.name + tag:<24} {self.signals:>7} {self.trips:>6} "
                f"{wr:>5}  {self.pnl_e4/10_000:>+10.2f} {self.fees_usd:>7.2f} "
                f"{self.net_usd:>+10.2f}  {open_s}{blk}")


def comparison_report(cards: dict[str, StrategyScorecard]) -> str:
    lines = ["---- strategy comparison ([LIVE] = real fills; the other is "
             "replayed through an identical RiskPolicy clone) ----",
             "  strategy                 signals  trips  win     gross $ "
             " fees $      net $  position"]
    lines += [c.row() for c in cards.values()]
    for c in cards.values():
        if not c.live and c.policy is not None and c.block_reasons:
            top = ", ".join(f"{r} x{n}"
                            for r, n in c.block_reasons.most_common(3))
            lines.append(f"    {c.name} gated-away signals: {top}")
    trips = [c.trips for c in cards.values()]
    if trips and max(trips) < 20:
        lines.append("  note: few round trips — treat as anecdote, "
                     "not evidence")
    return "\n".join(lines)
