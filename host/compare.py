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
from datetime import datetime, timedelta

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
    trip_log: list = field(default_factory=list)    # one entry per CLOSED
                                                    # trip, for the monthly
                                                    # breakdown report — see
                                                    # compare.py's
                                                    # monthly_breakdown_report()

    @property
    def open_e4(self) -> int | None:
        return next(iter(self.opens.values()), None)

    def on_signal(self, fr: dict, count: bool = True,
                 t: datetime | None = None) -> str:
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
        REPORTED as "today's" activity should only be today's own.

        t: the trade's own timestamp (backtest historical time, or
        None live) — recorded on trip_log when a sell actually closes,
        so monthly_breakdown_report() can bucket by CLOSE date. Only
        logged when count=True, matching every other reported total."""
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
                trip_win = trip > 0
                if trip_win:
                    self.wins = (self.wins or 0) + 1
                fee = self.fees.sell_fees(qty, qty * price / 10_000.0)["total"]
                self.fees_usd += fee
                self.trip_log.append({
                    "close_t": t, "symbol": sym, "entry_e4": entry,
                    "exit_e4": price, "qty": qty, "pnl_e4": trip,
                    "fees_usd": fee, "win": trip_win})
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

    MAX-HOLD FORCED EXIT (max_hold_days): the bound the two caveats
    above were begging for. When set, a position held longer than
    max_hold_days is force-closed at the next signal's price,
    REGARDLESS of profit — the one way a loss can now be realized.
    This directly fixes what a 3-year backtest made undeniable: the
    "100%" win rate was pure exit-rule artifact ("would realize a
    loss" was the single largest gated-away reason, 1.4M+ signals on
    VTI alone), and the perpetually-"1 open" position at report end
    was carrying unbounded unrealized loss that net $ structurally
    couldn't show. With the bound in place, win rate is a real number
    again (a forced exit below cost counts as a LOSS), and the worst
    case is capped at max_hold_days of adverse drift instead of
    forever.

    Honesty notes on the forced exit:
      * it fires at SIGNAL time, not continuously — this card only
        sees crossover signals, not raw ticks, so an expired hold
        closes at the next signal's price after expiry (SMA crossovers
        arrive thousands of times a month, so the granularity gap is
        minutes, not days).
      * it goes through the SAME RiskPolicy gate as any other sell
        (cooldown / daily cap / market hours) — if gated, it simply
        retries at the next signal rather than jumping the queue.
      * expiry is measured against the trade's own timestamp (t) when
        one is provided (backtests, startup replay); live signals
        carry no t, so it falls back to the policy clock when a policy
        is attached, and is inert otherwise.
      * None (the default) preserves the original never-realize-a-loss
        behavior exactly, so this stays an isolated, comparable
        variable — run with and without to measure what the bound
        costs or saves.
    """

    max_hold_days: float | None = None
    forced_exits: int = 0
    entry_t: dict = field(default_factory=dict)   # symbol -> first-entry
                                                  # time since last flat

    def _now(self, t: datetime | None) -> datetime | None:
        if t is not None:
            return t
        if self.policy is not None:
            return self.policy._now_fn()
        return None

    def _hold_expired(self, sym: str, now: datetime | None) -> bool:
        if self.max_hold_days is None or now is None:
            return False
        if self.positions.get(sym, 0) <= 0:
            return False
        entered = self.entry_t.get(sym)
        return (entered is not None
                and now - entered >= timedelta(days=self.max_hold_days))

    def _close(self, sym: str, price: int, count: bool,
              t: datetime | None, forced: bool = False):
        """Shared close bookkeeping for both the normal profitable sell
        and the max-hold forced exit — the ONLY difference is that a
        forced exit's trip can be (and usually is) a loss."""
        qty = self.positions.get(sym, 0)
        entry = self.opens.get(sym, price)
        if count:
            trip = (price - entry) * qty
            self.pnl_e4 += trip
            self.trips += 1
            win = trip > 0
            if win:
                self.wins = (self.wins or 0) + 1
            if forced:
                self.forced_exits += 1
            fee = self.fees.sell_fees(qty, qty * price / 10_000.0)["total"]
            self.fees_usd += fee
            self.trip_log.append({
                "close_t": t, "symbol": sym, "entry_e4": entry,
                "exit_e4": price, "qty": qty, "pnl_e4": trip,
                "fees_usd": fee, "win": win, "forced": forced})
        self.positions[sym] = 0
        self.opens.pop(sym, None)
        self.entry_t.pop(sym, None)

    def on_signal(self, fr: dict, count: bool = True,
                 t: datetime | None = None) -> str:
        if count:
            self.signals += 1
        side, price = fr["side"], fr["price_e4"]
        sym = fr.get("symbol", "").strip()

        # ---- max-hold forced exit: evaluated BEFORE the incoming
        # signal, through the same policy gate as any other sell ------
        now = self._now(t)
        if self._hold_expired(sym, now):
            pos = self.positions.get(sym, 0)
            allowed = True
            if self.policy is not None:
                allowed, _, _ = self.policy.evaluate(SIDE_SELL, pos, price)
            if allowed:
                self._close(sym, price, count, t, forced=True)
                if self.policy is not None and count:
                    self.policy.record_order()
                if side == SIDE_SELL:
                    return "FILLED (scored, forced exit: max hold)"
                # a BUY signal falls through and is processed normally
                # against the now-flat position (it may then gate on
                # the cooldown the forced exit just started — correct:
                # a real account couldn't fire both inside the gap
                # either)
            # not allowed (cooldown/cap/hours): retry at next signal

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
            if not old_qty and now is not None:
                # hold time is measured from the FIRST lot since flat —
                # adding to a position doesn't reset the clock (that
                # would let averaging-down extend a loser indefinitely,
                # the exact behavior the bound exists to prevent)
                self.entry_t[sym] = now
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
        self._close(sym, price, count, t)   # win by construction here:
        return "FILLED (scored)"            # the price > entry gate above
                                            # already passed


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


def monthly_breakdown_report(cards: dict[str, StrategyScorecard]) -> str:
    """Group each card's ALREADY-COMPLETED trips (trip_log) by the
    calendar month each one CLOSED in — matching how realized P&L is
    recognized everywhere else in this project (the OM's own day-scoped
    persistence fix used the same rule: a trade's gain belongs to the
    day it closed, priced against its real entry, not the day it
    opened).

    This is NOT the same as running independent monthly backtests and
    summing them — and deliberately so. A single continuous run (this
    function just re-buckets its ALREADY-CORRECT trip history for
    display) correctly carries state across month boundaries: an open
    position spanning month-end, a slow HTF warmup that only needs to
    happen once, a cooldown timer that shouldn't forget an order from
    11:59pm on the 31st. Splitting into independent monthly runs would
    silently lose all of that — see the README for the specifics."""
    lines = ["---- monthly P&L breakdown (from ONE continuous run — see "
             "monthly_breakdown_report()'s docstring for why this is NOT "
             "the same as summing independently-run monthly backtests) "
             "----"]
    any_trips = False
    for c in cards.values():
        if not c.trip_log:
            continue
        any_trips = True
        lines.append(f"\n  {c.name}:")
        lines.append(f"  {'month':<9} {'trips':>6} {'win':>5}  "
                     f"{'gross $':>10} {'fees $':>8} {'net $':>10}")
        by_month: dict[str, list] = {}
        for trip in c.trip_log:
            ym = (trip["close_t"].strftime("%Y-%m")
                 if trip["close_t"] is not None else "unknown")
            by_month.setdefault(ym, []).append(trip)
        for ym in sorted(by_month):
            month_trips = by_month[ym]
            n = len(month_trips)
            wins = sum(1 for tr in month_trips if tr["win"])
            gross = sum(tr["pnl_e4"] for tr in month_trips) / 10_000
            fees = sum(tr["fees_usd"] for tr in month_trips)
            wr = f"{100*wins/n:.0f}%" if n else "  —"
            lines.append(f"  {ym:<9} {n:>6} {wr:>5}  {gross:>+10.2f} "
                        f"{fees:>8.2f} {gross-fees:>+10.2f}")
        total_gross = sum(tr["pnl_e4"] for tr in c.trip_log) / 10_000
        total_fees = sum(tr["fees_usd"] for tr in c.trip_log)
        lines.append(f"  {'TOTAL':<9} {len(c.trip_log):>6} {'':>5}  "
                     f"{total_gross:>+10.2f} {total_fees:>8.2f} "
                     f"{total_gross-total_fees:>+10.2f}")
    if not any_trips:
        lines.append("\n  (no completed trips yet for any strategy)")
    return "\n".join(lines)
