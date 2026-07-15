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

    def on_signal(self, fr: dict, count: bool = True) -> str:
        """Returns a short status string, same spirit as
        OrderManager.on_signal — "FILLED (scored)", "gated: <reason>",
        or "ignored: <reason>" for the ungated single-lot mode's silent
        skips — so the GUI can show why a scored (untraded) signal
        didn't count as a trip, not just that it arrived.

        count=False replays a signal to rebuild cost basis (opens/
        positions) WITHOUT touching signals/trips/wins/pnl_e4/blocked —
        same purpose as CostTracker.on_fill's count flag: restoring a
        scored strategy's state across a restart needs prior-day
        history to get an open position's cost basis right, but what's
        REPORTED as "today's" activity should only be today's own."""
        if count:
            self.signals += 1
        side, price = fr["side"], fr["price_e4"]
        sym = fr.get("symbol", "").strip()
        pos_qty = self.positions.get(sym, 0)

        if self.policy is not None:
            allowed, reason, qty = self.policy.evaluate(side, pos_qty,
                                                         price)
            if not allowed:
                if count:
                    self.blocked += 1
                    self.block_reasons[reason.split(" (")[0]] += 1
                return f"gated: {reason}"
            if count:
                self.policy.record_order()
        else:
            if side == SIDE_BUY and pos_qty > 0:
                return "ignored: already open"
            if side == SIDE_SELL and pos_qty <= 0:
                return "ignored: flat"
            qty = self.qty if side == SIDE_BUY else pos_qty

        if side == SIDE_BUY:
            self.positions[sym] = qty
            self.opens[sym] = price
        elif side == SIDE_SELL:
            entry = self.opens.pop(sym, price)
            self.positions[sym] = 0
            if count:
                trip = (price - entry) * qty
                self.pnl_e4 += trip
                self.trips += 1
                if trip > 0:
                    self.wins = (self.wins or 0) + 1
                self.fees_usd += self.fees.sell_fees(
                    qty, qty * price / 10_000.0)["total"]
        return "FILLED (scored)"

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


@dataclass
class ProfitGatedScorecard(StrategyScorecard):
    """The SAME SMA (or EMA) crossover signals as any other scored row,
    with one extra rule on the SELL side: a sell only executes if the
    price is strictly ABOVE the weighted-average cost basis of the
    shares held — i.e., this variant refuses to realize a loss. If the
    crossover says sell but the position would come out underwater, the
    position just stays open, waiting for a future price recovery (or a
    later crossover) before it will ever close.

    BUYS are untouched — identical to the base class, gated through the
    same RiskPolicy clone as any other shadow row — so the comparison
    isolates ONE variable: does refusing to sell at a loss help or hurt,
    holding entry logic and risk limits constant.

    Two things worth knowing about what this measures, not just what it
    does:
      * win rate here is trivially 100% by construction — a loss is
        never realized, so every trip that DOES close is a win. That's
        not a sign of a good strategy; it's definitionally true given
        the rule. The number that actually matters is net P&L and how
        many symbols end the session still stuck open underwater.
      * this is the textbook "disposition effect" (behavioral finance's
        name for holding losers too long, selling winners quickly) —
        the published literature on it generally finds it hurts
        returns, since a position that never recovers just ties up
        capital indefinitely instead of realizing a small, bounded
        loss. Built as asked; let the comparison numbers speak for
        themselves rather than assuming either outcome.
    """

    def on_signal(self, fr: dict, count: bool = True) -> str:
        if count:
            self.signals += 1
        side, price = fr["side"], fr["price_e4"]
        sym = fr.get("symbol", "").strip()
        pos_qty = self.positions.get(sym, 0)

        if self.policy is not None:
            allowed, reason, qty = self.policy.evaluate(side, pos_qty,
                                                         price)
            if not allowed:
                if count:
                    self.blocked += 1
                    self.block_reasons[reason.split(" (")[0]] += 1
                return f"gated: {reason}"
        else:
            if side == SIDE_BUY and pos_qty > 0:
                return "ignored: already open"
            if side == SIDE_SELL and pos_qty <= 0:
                return "ignored: flat"
            qty = self.qty if side == SIDE_BUY else pos_qty

        if side == SIDE_BUY:
            old_qty = self.positions.get(sym, 0)
            old_avg = self.opens.get(sym, price)
            new_qty = old_qty + qty
            self.opens[sym] = ((old_avg * old_qty + price * qty)
                               // new_qty) if old_qty else price
            self.positions[sym] = new_qty
            if self.policy is not None and count:
                self.policy.record_order()
            return "FILLED (scored)"

        # SIDE_SELL: the one rule this whole class exists to add
        entry = self.opens.get(sym, price)
        if price <= entry:
            if count:
                self.blocked += 1
                self.block_reasons["would realize a loss"] += 1
            # NOTE: record_order() is deliberately NOT called here — a
            # suppressed sell never became an order at all, so it
            # shouldn't consume cooldown or the daily cap the way a
            # real rejected order would; only actual fills should.
            return "gated: would realize a loss (price <= avg cost)"

        if self.policy is not None and count:
            self.policy.record_order()
        qty_to_sell = self.positions.get(sym, 0)
        if count:
            trip = (price - entry) * qty_to_sell
            self.pnl_e4 += trip
            self.trips += 1
            self.wins = (self.wins or 0) + 1    # always true here, by rule
            self.fees_usd += self.fees.sell_fees(
                qty_to_sell, qty_to_sell * price / 10_000.0)["total"]
        self.positions[sym] = 0
        self.opens.pop(sym, None)
        return "FILLED (scored)"


def comparison_report(cards: dict[str, StrategyScorecard]) -> str:
    lines = ["---- strategy comparison ([LIVE] = real fills; the other is "
             "replayed through an identical RiskPolicy clone) ----",
             "  strategy                 signals  trips  win     gross $ "
             " fees $      net $  position"]
    lines += [c.row() for c in cards.values()]
    for c in cards.values():
        if c.policy is not None and c.block_reasons:
            # NOTE: shown regardless of c.live. The [LIVE] tag means
            # "these numbers are real broker fills" (true in a live
            # session, never true in a backtest — see backtest.py's
            # run_backtest(), which labels one row [LIVE] purely for
            # cosmetic consistency with live reports even though BOTH
            # rows are gated replays there). Hiding the reason breakdown
            # for whichever row happens to be labeled [LIVE] was a real
            # gap: in a backtest it hid exactly the diagnostic you'd
            # want most, for no real benefit even in a true live session.
            top = ", ".join(f"{r} x{n}"
                            for r, n in c.block_reasons.most_common(3))
            lines.append(f"    {c.name} gated-away signals: {top}")
    trips = [c.trips for c in cards.values()]
    if trips and max(trips) < 20:
        lines.append("  note: few round trips — treat as anecdote, "
                     "not evidence")
    return "\n".join(lines)
