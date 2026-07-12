#!/usr/bin/env python3
"""
costs.py — transaction fee accumulation and income-tax estimation.

*** ESTIMATES ONLY. Not tax advice; consult a tax professional before      ***
*** relying on any number here. Paper trading incurs no real fees or tax — ***
*** this module models what a LIVE account with identical fills would owe. ***

FEES (rates as verified July 2026 — they change; update the constants):
  * Commission: $0 (Alpaca).
  * SEC Section 31 fee: $20.60 per $1,000,000 of SALE proceeds, effective
    2026-04-04 (was $0.00 from 2025-05-14, $27.80/M before that).
    Source: sec.gov Fee Rate Advisory FY2026.
  * FINRA Trading Activity Fee (TAF): $0.000195 per share SOLD, max $9.79
    per trade, effective 2026-01-01 (was $0.000166 / $8.30).
  * Both apply to SELLS ONLY. Buys are free. Each fee is rounded UP to the
    next cent per trade, matching broker pass-through practice.
  * CAT fee: brokers also pass through a small Consolidated Audit Trail
    fee; volume-based and rate-varying — modeled as an optional flat
    per-trade amount, default $0.

TAX (tax year 2026, IRS Rev. Proc. 2025-32):
  * Every position here is held far under one year, so all gains are
    SHORT-TERM: taxed as ordinary income at your marginal rate, stacked on
    top of household income. (Long-term preferential rates never apply to
    this strategy.)
  * Bracket engine verified against published anchors: single $200,000
    taxable -> ~$40,600; MFJ $200,000 -> ~$33,400.
  * NIIT: +3.8% on net investment income to the extent MAGI exceeds
    $200,000 (single) / $250,000 (MFJ) — thresholds not inflation-indexed.
  * State: flat rate parameter, default 4.40% (Colorado's flat income tax;
    verify the current year's rate — TABOR triggers have temporarily
    lowered it in some years).
  * Net LOSSES: estimated tax is $0; up to $3,000/yr of net capital loss
    is deductible against ordinary income (noted, not modeled).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Fee schedule
# ---------------------------------------------------------------------------
@dataclass
class FeeSchedule:
    sec_per_million: float = 20.60      # $ per $1M sale proceeds (2026-04-04)
    taf_per_share: float = 0.000195     # $ per share sold        (2026-01-01)
    taf_cap: float = 9.79               # $ max TAF per trade
    cat_per_trade: float = 0.0          # optional flat CAT pass-through

    @staticmethod
    def _cent_up(x: float) -> float:
        return math.ceil(round(x * 100, 6)) / 100.0

    def sell_fees(self, qty: int, notional_usd: float) -> dict:
        """Regulatory fees for one SELL. Buys cost nothing."""
        sec = self._cent_up(notional_usd / 1_000_000 * self.sec_per_million)
        taf = self._cent_up(min(qty * self.taf_per_share, self.taf_cap))
        return {"sec": sec, "taf": taf, "cat": self.cat_per_trade,
                "total": sec + taf + self.cat_per_trade}


# ---------------------------------------------------------------------------
# 2026 federal brackets (taxable income thresholds, Rev. Proc. 2025-32)
# ---------------------------------------------------------------------------
BRACKETS_2026 = {
    "single": [(0.10,      0), (0.12,  12_400), (0.22,  50_400),
               (0.24, 105_700), (0.32, 201_775), (0.35, 256_225),
               (0.37, 640_600)],
    "mfj":    [(0.10,      0), (0.12,  24_800), (0.22, 100_800),
               (0.24, 211_400), (0.32, 403_550), (0.35, 512_450),
               (0.37, 768_700)],
}
STD_DEDUCTION_2026 = {"single": 16_100, "mfj": 32_200}
NIIT_RATE = 0.038
NIIT_THRESHOLD = {"single": 200_000, "mfj": 250_000}
TAX_YEAR = 2026


def federal_tax(taxable: float, status: str) -> float:
    """Total federal ordinary-income tax on `taxable` income (piecewise)."""
    if taxable <= 0:
        return 0.0
    tax = 0.0
    br = BRACKETS_2026[status]
    for i, (rate, lo) in enumerate(br):
        hi = br[i + 1][1] if i + 1 < len(br) else float("inf")
        if taxable > lo:
            tax += rate * (min(taxable, hi) - lo)
        else:
            break
    return tax


def marginal_rate(taxable: float, status: str) -> float:
    br = BRACKETS_2026[status]
    rate = br[0][0]
    for r, lo in br:
        if taxable > lo:
            rate = r
    return rate


def estimate_gains_tax(household_income: float, net_gain: float,
                       status: str = "mfj", state_rate_pct: float = 4.40,
                       income_is_gross: bool = False) -> dict:
    """Incremental tax attributable to short-term `net_gain` stacked on top
    of household income. household_income is TAXABLE income unless
    income_is_gross, in which case the 2026 standard deduction is applied."""
    base = household_income
    if income_is_gross:
        base = max(0.0, base - STD_DEDUCTION_2026[status])

    if net_gain <= 0:
        return {"federal": 0.0, "niit": 0.0, "state": 0.0, "total": 0.0,
                "marginal_pct": 0.0, "effective_pct": 0.0,
                "taxable_base": base,
                "note": ("net loss: no tax; up to $3,000/yr of net capital "
                         "loss is deductible against ordinary income")}

    fed = federal_tax(base + net_gain, status) - federal_tax(base, status)

    # NIIT applies to investment income above the MAGI threshold
    thr = NIIT_THRESHOLD[status]
    magi = base + net_gain                       # approximation: MAGI ~ taxable
    niit = NIIT_RATE * max(0.0, min(net_gain, magi - thr)) if magi > thr else 0.0

    state = net_gain * state_rate_pct / 100.0
    total = fed + niit + state
    return {"federal": fed, "niit": niit, "state": state, "total": total,
            "marginal_pct": 100 * marginal_rate(base + net_gain, status),
            "effective_pct": 100 * total / net_gain,
            "taxable_base": base, "note": ""}


# ---------------------------------------------------------------------------
# CostTracker — plugs into OrderManager fills
# ---------------------------------------------------------------------------
@dataclass
class CostTracker:
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    total_fees: float = 0.0             # $ accumulated (sells only)
    realized_pnl_e4: int = 0            # fixed-point x10000, gross of fees
    _entry_e4: int = 0                  # avg entry of the open long position
    _entry_qty: int = 0
    buys: int = 0
    sells: int = 0

    def on_fill(self, side: str, qty: int, fill_price_e4: int) -> dict | None:
        """Called by OrderManager after each fill. Returns the fee breakdown
        for a sell, None for a buy (long-only: buys open, sells close)."""
        if side == "buy":
            # weighted-average entry (order_qty can vary between fills)
            tot = self._entry_e4 * self._entry_qty + fill_price_e4 * qty
            self._entry_qty += qty
            self._entry_e4 = tot // self._entry_qty
            self.buys += 1
            return None
        # sell: realize P&L against the average entry, charge sell-side fees
        self.realized_pnl_e4 += (fill_price_e4 - self._entry_e4) * qty
        self._entry_qty = max(0, self._entry_qty - qty)
        if self._entry_qty == 0:
            self._entry_e4 = 0
        f = self.fees.sell_fees(qty, qty * fill_price_e4 / 10_000.0)
        self.total_fees += f["total"]
        self.sells += 1
        return f

    @property
    def realized_pnl_usd(self) -> float:
        return self.realized_pnl_e4 / 10_000.0

    @property
    def net_pnl_usd(self) -> float:
        return self.realized_pnl_usd - self.total_fees

    def report(self, household_income: float | None,
               status: str = "mfj", state_rate_pct: float = 4.40,
               income_is_gross: bool = False) -> str:
        lines = ["---- costs & tax estimate " + "-" * 34,
                 f"  fills                 {self.buys} buys / {self.sells} sells",
                 f"  realized P&L (gross)  ${self.realized_pnl_usd:+,.2f}",
                 f"  regulatory fees       ${self.total_fees:,.2f}"
                 f"   (SEC ${self.fees.sec_per_million}/M + TAF "
                 f"${self.fees.taf_per_share}/sh, sells only)",
                 f"  net realized P&L      ${self.net_pnl_usd:+,.2f}"]
        if household_income is None:
            lines.append("  (pass --household-income to estimate income tax)")
        else:
            t = estimate_gains_tax(household_income, self.net_pnl_usd,
                                   status, state_rate_pct, income_is_gross)
            lines += [f"  tax year {TAX_YEAR}, filing {status}, taxable base "
                      f"${t['taxable_base']:,.0f}",
                      f"    federal (short-term = ordinary)  ${t['federal']:,.2f}",
                      f"    NIIT (3.8% over "
                      f"${NIIT_THRESHOLD[status]:,})           ${t['niit']:,.2f}",
                      f"    state ({state_rate_pct}%)                     "
                      f"${t['state']:,.2f}",
                      f"    estimated total tax              ${t['total']:,.2f}"]
            if t["note"]:
                lines.append(f"    note: {t['note']}")
            else:
                lines += [f"    marginal bracket {t['marginal_pct']:.0f}%,  "
                          f"effective on gain {t['effective_pct']:.1f}%",
                          f"  after-tax P&L         "
                          f"${self.net_pnl_usd - t['total']:+,.2f}"]
        lines.append("  ESTIMATES ONLY — not tax advice; paper trades incur "
                     "no real fees/tax")
        return "\n".join(lines)
