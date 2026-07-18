#!/usr/bin/env python3
"""
blended_strategy.py — two-sleeve portfolio blend: VWAP bounce +
SMA profit-gated, each with its own capital budget, under one
account-level exposure cap.

WHY A BLEND, AND WHY THIS SHAPE
-------------------------------
The two consistently-positive rows from the multi-year VTI/QQQ
backtests were VWAP bounce and SMA profit-gated. Their monthly nets
are essentially uncorrelated (~-0.15 on both symbols), and profit-
gated sits out entirely in roughly a third of months where VWAP
bounce is still trading — a genuine diversification case, NOT the
same edge twice. So this is a PORTFOLIO BLEND (both sleeves trade
independently, capital split between them), deliberately not a
signal filter (one strategy vetoing the other's entries) — a filter
changes trade selection and would need its own backtest from
scratch; a blend only changes capital allocation, so the existing
per-sleeve results still mean what they meant.

Three layers, mirroring how the real OrderManager already thinks:

  * Each sleeve is the UNCHANGED existing scorecard class
    (VWAPBounceScorecard / ProfitGatedScorecard) with its OWN
    RiskPolicy clone built from its own carved-down RiskLimits —
    own cooldown, own daily cap, own max_shares/max_notional. One
    sleeve's cooldown never blocks the other's signal, exactly as
    two separate accounts wouldn't share a cooldown.
  * ONE AccountExposureCap sits above both: the sum of open cost-
    basis notional across BOTH sleeves can never exceed the account
    ceiling, no matter what each sleeve's own per-order limits would
    allow. This is the piece per-sleeve limits can't express — the
    same reason the live OM reconciles positions per symbol but
    enforces one kill switch.
  * The BlendedScorecard aggregates the two into one comparison-
    report row (with per-sleeve sub-rows), one merged monthly
    breakdown, one combined realized-equity max-drawdown figure, and
    an UNREALIZED mark for anything still open — the number whose
    absence made the standalone profit-gated row's "+$1,245 net,
    100% win" claim so misleading in the first place.

The profit-gated sleeve should always be run with a max_hold_days
bound (see ProfitGatedScorecard) — an unbounded never-realize-a-loss
sleeve next to a bounded one isn't diversification, it's an
unbounded liability wearing a 100% win rate.

WHAT THIS DELIBERATELY DOES NOT DO YET
--------------------------------------
Backtest/score-only, same as every other shadow row was when it was
born. Live wiring is a separate step with one real open problem: the
scored-state restore mechanism replays "scored_signal" audit events,
but the VWAP sleeve consumes RAW TICKS (like the ladder), which
aren't in the audit log — so a mid-day restart would silently reset
its session VWAP. Solve that before this row runs live, not after.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from compare import ProfitGatedScorecard
from order_manager import RiskLimits, RiskPolicy
from tick_protocol import SIDE_BUY, dollars
from vwap_bounce_strategy import VWAPBounceScorecard


class AccountExposureCap:
    """The account-level check no per-sleeve RiskLimits can express:
    total open cost-basis notional across ALL attached sleeves must
    stay under one ceiling. Stateless by design — open notional is
    recomputed from the sleeves' own positions/opens on every check,
    so there is no mutable ledger that can drift out of sync with
    the cards (the same no-local-memory discipline as the OM
    reconciling positions from the broker instead of remembering)."""

    def __init__(self, cap_e4: int):
        self.cap_e4 = cap_e4
        self._sleeves: list = []

    def attach(self, card):
        self._sleeves.append(card)

    def open_notional_e4(self) -> int:
        total = 0
        for c in self._sleeves:
            for sym, qty in c.positions.items():
                if qty > 0:
                    total += qty * c.opens.get(sym, 0)
        return total


class SleevePolicy:
    """A RiskPolicy wrapper: the inner policy enforces the sleeve's
    OWN limits (cooldown, daily cap, per-order notional, max_shares);
    this adds exactly one further gate on BUYS — the account-level
    exposure cap across all sleeves. Sells are never blocked by the
    cap (reducing exposure is always allowed).

    Duck-types the parts of RiskPolicy the scorecards actually touch:
    evaluate(), record_order(), and _now_fn (VWAPBounceScorecard sets
    the historical clock through it)."""

    def __init__(self, inner: RiskPolicy, account: AccountExposureCap):
        self.inner = inner
        self.account = account

    @property
    def _now_fn(self):
        return self.inner._now_fn

    @_now_fn.setter
    def _now_fn(self, fn):
        self.inner._now_fn = fn

    def evaluate(self, side: int, position_qty: int,
                 price_e4: int) -> tuple[bool, str, int]:
        allowed, reason, qty = self.inner.evaluate(side, position_qty,
                                                    price_e4)
        if not allowed or side != SIDE_BUY:
            return allowed, reason, qty
        projected = self.account.open_notional_e4() + qty * price_e4
        if projected > self.account.cap_e4:
            return False, (
                f"account exposure cap (open "
                f"{dollars(self.account.open_notional_e4()):.2f} + "
                f"{dollars(qty * price_e4):.2f} > "
                f"{dollars(self.account.cap_e4):.2f} across sleeves)"), 0
        return True, "ok", qty

    def record_order(self):
        self.inner.record_order()


class BlendedScorecard:
    """Aggregates two sleeves into one reportable card. Duck-types the
    slice of StrategyScorecard that comparison_report() and
    monthly_breakdown_report() actually use (name / policy /
    block_reasons / trips / trip_log / row()), so the existing report
    functions render it with zero changes."""

    def __init__(self, name: str, symbol: str,
                 vwap_card: VWAPBounceScorecard,
                 pg_card: ProfitGatedScorecard,
                 account: AccountExposureCap):
        self.name = name
        self.symbol = symbol
        self.vwap = vwap_card
        self.pg = pg_card
        self.account = account
        self.live = False
        self.last_price_e4: dict[str, int] = {}   # for unrealized mark

    # ---- construction ------------------------------------------------------
    @classmethod
    def build(cls, symbol: str, base_limits: RiskLimits,
             vwap_shares: int, vwap_notional_e4: int,
             pg_shares: int, pg_notional_e4: int,
             account_cap_e4: int, band_k: float = 1.0,
             max_hold_days: float | None = 5.0,
             now_fn_factory=None) -> "BlendedScorecard":
        """now_fn_factory: () -> clock, one FRESH clock per sleeve —
        pass HistoricalClock in a backtest (each sleeve's policy must
        tick on the trade's own timestamp, independently, exactly as
        every existing shadow row does), or None live (wall clock)."""
        mk = now_fn_factory if now_fn_factory is not None else (lambda: None)
        account = AccountExposureCap(account_cap_e4)

        vwap_lim = replace(base_limits, max_shares=vwap_shares,
                          max_notional_e4=vwap_notional_e4)
        pg_lim = replace(base_limits, max_shares=pg_shares,
                        max_notional_e4=pg_notional_e4)

        vwap_card = VWAPBounceScorecard(
            "  - VWAP sleeve", symbol=symbol, live=False,
            policy=SleevePolicy(RiskPolicy(vwap_lim, now_fn=mk()), account),
            band_k=band_k)
        pg_card = ProfitGatedScorecard(
            "  - SMA-PG sleeve", live=False,
            policy=SleevePolicy(RiskPolicy(pg_lim, now_fn=mk()), account),
            max_hold_days=max_hold_days)

        account.attach(vwap_card)
        account.attach(pg_card)
        return cls("Blend (VWAP+SMA-PG)", symbol, vwap_card, pg_card,
                  account)

    # ---- feeding -----------------------------------------------------------
    def on_tick(self, t: datetime, price_e4: int, qty: int):
        """Every raw trade tick — drives the VWAP sleeve, and keeps the
        last-seen price fresh for the unrealized mark."""
        self.last_price_e4[self.symbol] = price_e4
        return self.vwap.on_tick(t, price_e4, qty)

    def on_sma_signal(self, fr: dict, count: bool = True,
                     t: datetime | None = None):
        """Every verified SMA crossover signal — drives the profit-
        gated sleeve (same stream the standalone sma_pg row watches)."""
        if (self.pg.policy is not None
                and hasattr(self.pg.policy._now_fn, "set")
                and t is not None):
            self.pg.policy._now_fn.set(t)
        return self.pg.on_signal(fr, count=count, t=t)

    # ---- aggregation (duck-typed StrategyScorecard surface) ----------------
    @property
    def _sleeves(self):
        return (self.vwap, self.pg)

    @property
    def signals(self) -> int:
        return sum(c.signals for c in self._sleeves)

    @property
    def trips(self) -> int:
        return sum(c.trips for c in self._sleeves)

    @property
    def wins(self):
        return sum(c.wins or 0 for c in self._sleeves)

    @property
    def blocked(self) -> int:
        return sum(c.blocked for c in self._sleeves)

    @property
    def block_reasons(self):
        """Merged, sleeve-prefixed, so the existing 'gated-away
        signals' report line shows which sleeve gated what."""
        from collections import Counter
        merged = Counter()
        for label, c in (("vwap", self.vwap), ("sma-pg", self.pg)):
            for reason, n in c.block_reasons.items():
                merged[f"{label}: {reason}"] += n
        return merged

    def gate_summary(self, top_n: int = 3) -> str:
        """comparison_report()'s default 'gated-away signals' line takes
        the top N reasons from block_reasons GLOBALLY. For a blend
        that's actively misleading: SMA-PG fires on nearly every tick
        (millions of signals) while VWAP fires only on rare band-touch
        events (a small fraction of that) -- every one of SMA-PG's
        reason-buckets outnumbers EVERY one of VWAP's, so a global
        top-3 can NEVER show a single VWAP reason, no matter how much
        of VWAP's own blocked count it explains. Confirmed on a real
        report: VWAP's sleeve showed 1.3M blocked signals of its own,
        and zero of its reasons made the merged top-3.

        This takes the top N reasons PER SLEEVE instead, so each
        sleeve's own gating story stays visible regardless of how the
        other sleeve's volume compares."""
        parts = []
        for label, c in (("vwap", self.vwap), ("sma-pg", self.pg)):
            if c.block_reasons:
                top = ", ".join(f"{r} x{n}"
                               for r, n in c.block_reasons.most_common(top_n))
                parts.append(f"{label}: {top}")
        return "; ".join(parts)

    @property
    def policy(self):
        # non-None so comparison_report() prints the merged gated-away
        # breakdown; the object itself is the honest answer to "what
        # gates this card as a whole"
        return self.account

    @property
    def pnl_e4(self) -> int:
        return sum(c.pnl_e4 for c in self._sleeves)

    @property
    def fees_usd(self) -> float:
        return sum(c.fees_usd for c in self._sleeves)

    @property
    def net_usd(self) -> float:
        return sum(c.net_usd for c in self._sleeves)

    @property
    def positions(self) -> dict:
        merged: dict[str, int] = {}
        for c in self._sleeves:
            for sym, qty in c.positions.items():
                merged[sym] = merged.get(sym, 0) + qty
        return merged

    @property
    def trip_log(self) -> list:
        """Both sleeves' closed trips, merged in close order — feeds
        monthly_breakdown_report() (combined monthly blend) and the
        drawdown calculation below. None close_t (live signals carry
        no timestamp) sorts last, matching the 'unknown' bucket."""
        merged = list(self.vwap.trip_log) + list(self.pg.trip_log)
        merged.sort(key=lambda tr: (tr["close_t"] is None, tr["close_t"]
                                    or datetime.min))
        return merged

    # ---- the two numbers the separate reports couldn't show ---------------
    def unrealized_usd(self) -> float:
        """Mark-to-last-tick of everything still open, per sleeve cost
        basis. THE number whose absence let the standalone profit-
        gated row report '+net, 100% win, 1 open' while the open lot
        quietly carried the strategy's entire downside."""
        total = 0.0
        for c in self._sleeves:
            for sym, qty in c.positions.items():
                if qty > 0 and sym in self.last_price_e4:
                    total += qty * (self.last_price_e4[sym]
                                    - c.opens.get(sym,
                                                  self.last_price_e4[sym])
                                    ) / 10_000.0
        return total

    def max_drawdown_usd(self) -> float:
        """Max peak-to-trough dip of the COMBINED realized equity curve
        (net of fees), in trip-close order. Monthly nets can't show
        this — two sleeves each fine on the month can still have
        overlapped inside a bad week; this is the number that catches
        that."""
        equity = peak = max_dd = 0.0
        for tr in self.trip_log:
            equity += tr["pnl_e4"] / 10_000.0 - tr["fees_usd"]
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return max_dd

    # ---- report row --------------------------------------------------------
    def row(self) -> str:
        trips = self.trips
        wr = f"{100 * self.wins / trips:.0f}%" if trips else "  —"
        open_n = sum(1 for v in self.positions.values() if v)
        open_s = f"{open_n} open" if open_n else "flat"
        blk = f"  ({self.blocked} gated)" if self.blocked else ""
        lines = [(f"  {self.name:<24} {self.signals:>7} {trips:>6} "
                 f"{wr:>5}  {self.pnl_e4 / 10_000:>+10.2f} "
                 f"{self.fees_usd:>7.2f} {self.net_usd:>+10.2f}  "
                 f"{open_s}{blk}")]
        lines += [c.row() for c in self._sleeves]
        unreal = self.unrealized_usd()
        forced = getattr(self.pg, "forced_exits", 0)
        hold_label = (f"{self.pg.max_hold_days}d" if self.pg.max_hold_days
                     is not None else "unbounded")
        lines.append(
            f"      blend: unrealized {unreal:+.2f} on open positions, "
            f"combined realized max drawdown "
            f"{self.max_drawdown_usd():.2f}, "
            f"{forced} forced exit(s) [SMA-PG max-hold {hold_label}]")
        return "\n".join(lines)
