"""
position_risk.py — host-side risk overlay. v3.38.

Sits between a verified fabric signal and the actual trade decision.
The fabric's own bounce/cross signal generation is completely
unchanged and still independently verified exactly as it always has
been — nothing here touches, requires, or second-guesses that. This
is a NEW, purely host-side layer answering three questions the fabric
was never asked and can't answer (it has no concept of position, cost
basis, or days):

  1. STOP-LOSS — has price fallen far enough that this position should
     be cut regardless of what the fabric's own signals are doing?
  2. ANCHORED VWAP — for a position that's been open across more than
     one day, what's a price reference that actually reflects its
     whole holding period, not just "since 9:30am today"?
  3. THE GATE — should an older position's exit be held back until
     it's actually near a reasonable price, rather than closing the
     instant TODAY's short-term session VWAP happens to be touched by
     an unrelated same-day scalp signal?

Built after a real incident: a 25-share position held across several
days got swept out by an unrelated, same-day 5-share entry's own
exit signal, the moment that fresh entry's session-VWAP-cross fired —
without regard to whether the older shares were anywhere near a
reasonable exit level themselves.
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field


@dataclass
class _SymbolRiskState:
    anchor_date: object = None            # date position FIRST opened;
                                          # None means flat
    anchor_sum_pv: int = 0                # Σ(p·v) since the anchor
    anchor_sum_v: int = 0                 # Σv since the anchor
    stop_price_e4: int | None = None      # fixed at position-open time;
                                          # None means flat
    stop_pending: bool = False            # v3.51: adopted at startup,
                                          # stop deferred until the
                                          # session VWAP warms up


class PositionRiskOverlay:
    """One instance per OrderManager session. Tracks every symbol it's
    told about. Entirely host-side: reads a live VWAPMirror's own
    already-verified public sums (sum_v, sum_pv, sum_ppv, vwap) to
    derive sigma at the moment a position opens, then keeps its own
    completely separate running state from there — it never mutates
    or depends on the fabric-verified VWAPMirror after that point.
    """

    def __init__(self, stop_sigma_mult: float = 3.0,
                anchor_gate_tolerance: float = 0.0,
                risk_dollars_per_trade: float = 500.0):
        self.stop_sigma_mult = stop_sigma_mult
        # tolerance: how far BELOW the anchored VWAP still counts as
        # "close enough" to let an older position exit (0.0 = must be
        # at or above it exactly; e.g. 0.002 = allow exiting up to 0.2%
        # below it)
        self.anchor_gate_tolerance = anchor_gate_tolerance
        self.risk_dollars_per_trade = risk_dollars_per_trade
        self._state: dict[str, _SymbolRiskState] = {}

    def _get(self, sym: str) -> _SymbolRiskState:
        return self._state.setdefault(sym, _SymbolRiskState())

    @staticmethod
    def _sigma_e4(vwap_mirror) -> float:
        """Sigma, in e4 units (price*10000 scale), from a live
        VWAPMirror's own accumulated sums. The exact same variance
        computation vwap_engine.sv's own band test already does
        (mean_sq - vwap^2) — just in floating point here, since this
        is host-only decision support, not something that needs
        bit-exact hardware verification the way the fabric signal
        itself does."""
        if vwap_mirror is None or vwap_mirror.sum_v == 0:
            return 0.0
        mean_sq = vwap_mirror.sum_ppv / vwap_mirror.sum_v
        variance = max(0.0, mean_sq - vwap_mirror.vwap ** 2)
        return math.sqrt(variance)

    def on_position_opened(self, sym: str, when, vwap_mirror) -> None:
        """Call exactly once: the moment a symbol's position goes from
        flat to non-flat (a fresh entry, not an add to an already-open
        position). Anchors the position-VWAP starting here, and FIXES
        the stop price using the CURRENT session VWAP/sigma at this
        moment — the stop does not move afterward, even if more
        shares get added to the position later. `when` is a date
        (the ET calendar day), used later to tell a same-day position
        from an older one."""
        st = self._get(sym)
        st.anchor_date = when
        st.anchor_sum_pv = 0
        st.anchor_sum_v = 0
        sigma = self._sigma_e4(vwap_mirror)
        vwap = vwap_mirror.vwap if vwap_mirror is not None else 0
        st.stop_price_e4 = int(vwap - self.stop_sigma_mult * sigma)
        st.stop_pending = False

    def adopt_existing_position(self, sym: str, when) -> None:
        """v3.51: take responsibility for a position this overlay did
        NOT open -- one reconciled from the broker at startup.

        on_position_opened() only ever runs on a fresh flat->non-flat
        entry inside a live session, so a position carried across a
        restart previously got no overlay state at all: no anchor, and
        stop_price_e4 None, which makes stop_triggered() return False on
        every tick forever. A real holding (32 SOFI shares) sat that way
        across several sessions with no downside protection whatsoever,
        and it was not a quirk of that symbol -- EVERY carried position
        was in the same state.

        `when` is the date the position actually opened, recovered from
        the audit log, so the same-day-vs-older sell gate judges it
        correctly rather than treating an overnight holding as a fresh
        scalp.

        The stop is deliberately NOT computed here. At startup the
        session VWAP mirror is empty, and a stop computed from an empty
        mirror is exactly 0 -- unable to ever trigger, which is the very
        failure this is meant to fix. commit_pending_stop() sets it once
        the mirror has real data.
        """
        st = self._get(sym)
        st.anchor_date = when
        st.anchor_sum_pv = 0
        st.anchor_sum_v = 0
        st.stop_price_e4 = None
        st.stop_pending = True

    def stop_is_pending(self, sym: str) -> bool:
        st = self._state.get(sym)
        return bool(st is not None and st.stop_pending)

    def commit_pending_stop(self, sym: str, vwap_mirror) -> int | None:
        """v3.51: place the deferred stop for an adopted position, once
        the session VWAP has actually warmed up. A no-op unless this
        symbol is waiting for one. Returns the committed stop, or None
        if it is not ready yet (so the caller can report it exactly
        once, when it lands)."""
        st = self._state.get(sym)
        if st is None or not st.stop_pending or st.anchor_date is None:
            return None
        if vwap_mirror is None or not getattr(vwap_mirror, "warmed_up", False):
            return None
        sigma = self._sigma_e4(vwap_mirror)
        if sigma <= 0:
            return None          # warmed but still no dispersion -- wait,
                                 # rather than commit another stop of 0
        st.stop_price_e4 = int(vwap_mirror.vwap
                               - self.stop_sigma_mult * sigma)
        st.stop_pending = False
        return st.stop_price_e4

    def on_position_closed(self, sym: str) -> None:
        """Call when a symbol's position returns to flat (a full
        exit) — clears the anchor so the NEXT entry starts fresh."""
        st = self._get(sym)
        st.anchor_date = None
        st.anchor_sum_pv = 0
        st.anchor_sum_v = 0
        st.stop_price_e4 = None
        st.stop_pending = False

    def on_tick(self, sym: str, price_e4: int, qty: int) -> None:
        """Feed EVERY raw trade tick for a symbol — a no-op unless
        that symbol currently has an open (anchored) position, since
        there's nothing to accumulate for a flat symbol."""
        st = self._state.get(sym)
        if st is None or st.anchor_date is None:
            return
        st.anchor_sum_v += qty
        st.anchor_sum_pv += price_e4 * qty

    def anchored_vwap_e4(self, sym: str) -> int | None:
        """None if flat or no ticks accumulated yet since the anchor
        (can happen right at the instant a position opens, before the
        next tick arrives)."""
        st = self._state.get(sym)
        if st is None or st.anchor_date is None or st.anchor_sum_v == 0:
            return None
        return st.anchor_sum_pv // st.anchor_sum_v

    def stop_price_e4(self, sym: str) -> int | None:
        st = self._state.get(sym)
        return st.stop_price_e4 if st is not None else None

    def stop_triggered(self, sym: str, price_e4: int) -> bool:
        """True the instant price is at or below the fixed stop for an
        open position. Meant to be checked on every tick, independent
        of whether the fabric's own bounce/cross conditions happen to
        align at that exact moment — a stop has to fire the moment
        it's breached, not wait for the next signal."""
        sp = self.stop_price_e4(sym)
        return sp is not None and price_e4 <= sp

    def is_same_day(self, sym: str, today) -> bool:
        st = self._state.get(sym)
        return st is not None and st.anchor_date == today

    def sell_allowed(self, sym: str, price_e4: int, today) -> bool:
        """The gate. A same-day position (opened today) sells on the
        normal signal unconditionally — this is exactly what the
        strategy is designed to do for a fresh scalp. An OLDER
        position (anchor_date before today) needs price at/above its
        OWN anchored VWAP too, within the configured tolerance.

        Callers MUST check stop_triggered() first and treat a
        triggered stop as an unconditional exit regardless of what
        this method returns — the stop-loss is the downside safety
        net and always overrides "wait for a better price"."""
        st = self._state.get(sym)
        if st is None or st.anchor_date is None:
            return True     # nothing tracked here -- don't block
                            # anything this overlay has no opinion on
        if st.anchor_date == today:
            return True     # same-day position: the normal signal
                            # governs, exactly as before this existed
        av = self.anchored_vwap_e4(sym)
        if av is None:
            return True     # no anchored reference yet -- don't
                            # block on an undefined comparison
        return price_e4 >= av * (1.0 - self.anchor_gate_tolerance)

    def risk_sized_qty(self, entry_price_e4: int, stop_price_e4: int,
                       held_qty: int = 0, held_avg_cost_e4: int = 0
                       ) -> int:
        """v3.43: two genuinely different situations share this method,
        distinguished by held_qty.

        held_qty == 0 (a FRESH entry, position was flat): the original
        behavior, unchanged — shares = risk_dollars_per_trade /
        (entry - stop), floored, minimum 1. A signal that fires but
        risk-sizes to zero shares would be a silent no-op
        indistinguishable from a bug, so a fresh entry always sizes at
        least 1 share.

        held_qty > 0 (a PYRAMIDING ADD onto an already-open position):
        found and fixed a real gap here -- naively applying the SAME
        formula to every add would let each one independently target
        a full NEW $500 of risk, so total risk-if-stopped could grow
        without bound across repeated adds (exactly the scenario in a
        sustained decline where this strategy keeps buying). Instead,
        this caps the risk of the WHOLE resulting position — existing
        shares' blended average cost (held_avg_cost_e4, from
        CostTracker's own running average) against the SAME fixed
        stop, plus the new shares — at risk_dollars_per_trade total,
        not per add. As the position accumulates, less budget remains
        for further adds; minimum 0 in this case (not 1) — "no room
        left in the budget" is a legitimate, expected outcome for an
        add, not a bug to paper over the way a fresh entry sizing to
        zero would be. The caller should treat 0 as "block this add,"
        the same way an unmet anchored-VWAP gate blocks a sell.

        held_avg_cost_e4 is read directly from CostTracker's own
        _entries, not recomputed here — this method stays a pure
        function of its inputs, with no dependency on CostTracker
        itself.
        """
        risk_per_share_e4 = entry_price_e4 - stop_price_e4
        if risk_per_share_e4 <= 0:
            return 1 if held_qty == 0 else 0
                            # entry already at/through the stop --
                            # degenerate case; a fresh entry still
                            # sizes minimally rather than dividing by
                            # zero, but an add has nothing sensible
                            # left to size into and should just block

        if held_qty == 0:
            shares = int((self.risk_dollars_per_trade * 10_000)
                        // risk_per_share_e4)
            return max(1, shares)

        already_used_e4 = held_qty * (held_avg_cost_e4 - stop_price_e4)
        remaining_budget_e4 = (int(self.risk_dollars_per_trade * 10_000)
                              - already_used_e4)
        if remaining_budget_e4 <= 0:
            return 0        # already at or over the risk budget --
                            # block the add entirely, don't shrink to
                            # some arbitrary small leftover size
        shares = remaining_budget_e4 // risk_per_share_e4
        return max(0, int(shares))

    def peek_stop_price_e4(self, vwap_mirror) -> int:
        """Like on_position_opened's stop computation, but WITHOUT
        committing any state — used to size a BUY before deciding to
        place it, since the actual anchor only gets committed once the
        order is known to have filled."""
        sigma = self._sigma_e4(vwap_mirror)
        vwap = vwap_mirror.vwap if vwap_mirror is not None else 0
        return int(vwap - self.stop_sigma_mult * sigma)
