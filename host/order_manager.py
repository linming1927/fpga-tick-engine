#!/usr/bin/env python3
"""
order_manager.py — verified signals in, risk-checked paper orders out.

     verified 0x83 (via bridge callback)
              |
              v
      +---------------+     blocked? -> audit log with reason, no order
      |  RiskPolicy   |----------------------------------------------+
      +---------------+                                              |
              | allowed                                              |
              v                                                      v
      +---------------+  submit   +--------------------+      om_audit.jsonl
      | OrderManager  |---------->|  Broker            |      (every decision,
      +---------------+   fill    |  Mock / AlpacaPaper|       including the
              ^                   +--------------------+       refusals)
              |  divergence from the SignalVerifier
              +--> KILL SWITCH (latching)

DESIGN RULES
------------
* Consumes VERIFIED signals only. A signal whose SMAs failed the mirror-
  model check never reaches the policy layer; any divergence at all trips
  the kill switch. The order path inherits a continuous integrity check on
  the hardware math.
* The kill switch LATCHES. Once tripped (divergence, repeated broker
  rejections, or manual), no further orders this process — and a marker
  file (om.kill by default) is written so the next start REFUSES to run
  until a human deletes it. Kill switches that auto-recover aren't kill
  switches.
* The broker is the source of truth for position. On startup the manager
  reconciles from the broker's books rather than trusting local memory —
  the same discipline as the bridge's echo-driven model updates.
* Every decision is audited to JSONL, including the orders that DIDN'T
  happen and why. Refusals are the interesting records.
* Strategy is deliberately minimal: long-only, one symbol. BUY signal ->
  buy fixed qty if flat; SELL signal -> close the position if holding.
  Everything else (sizing, shorting, multi-symbol) is future work layered
  on the same policy scaffold.
* AlpacaPaperBroker is stdlib-only (urllib) — no new dependencies — and
  structurally refuses any base URL that isn't the paper endpoint.

USAGE (integrated: builds a Bridge internally)
    python3 order_manager.py --port /dev/pts/N --source sim --broker mock
    python3 order_manager.py --port /dev/ttyUSB1 --source alpaca \
            --broker alpaca --qty 1 --max-shares 5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tick_protocol import SIDE_BUY, SIDE_SELL, dollars, to_e4
from costs import CostTracker

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
LIVE_ACK_PHRASE = "I-UNDERSTAND-THIS-TRADES-REAL-MONEY"
ET = ZoneInfo("America/New_York")


class HistoricalClock:
    """Injected into RiskPolicy so cooldown/daily-cap gating is evaluated
    against a HISTORICAL timestamp, not real wall-clock time while the
    replay runs. Call .set(dt) before each evaluate()/record_order().
    Originally built for backtest.py (as BacktestClock); moved here and
    generalized because the same trick is needed for restoring TODAY's
    scored-strategy state across a restart — replaying this morning's
    signals in milliseconds at startup has the identical problem a
    multi-year backtest does: without a historical clock, cooldown and
    the daily cap would gate against "now" instead of when each signal
    actually happened.

    Starts at a sentinel far-past date (RiskPolicy reads the clock once
    at construction, before any real signal exists, purely to seed its
    day-rollover tracking) — the first real signal's date will always
    differ from the sentinel, so the day-rollover check corrects itself
    on the very first evaluate() call regardless."""

    _SENTINEL = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def __init__(self):
        self._t: datetime = self._SENTINEL

    def set(self, t: datetime):
        self._t = t

    def __call__(self) -> datetime:
        return self._t


def now_us() -> int:
    return time.time_ns() // 1000


def _load_scored_signals_split_by_today(audit_path: str
                                        ) -> tuple[list[dict], list[dict]]:
    """Same idea as _load_fills_split_by_today, for the SCORED (untraded
    comparison) strategies instead of the real OrderManager. Those
    cards have no external source of truth like a broker to reconcile
    from, so without this they silently reset to zero on every restart
    — a real reported bug: trips/wins/net $ for EMA (and any other
    scored row) showed stale/reset values after a restart, even though
    the live SMA row had already been fixed to persist correctly.

    Reads "scored_signal" events (logged by main()'s on_verified(), one
    per signal fed to any non-live-traded card) from the SAME audit
    file the real fills already use — no new log file needed."""
    if not os.path.exists(audit_path):
        return [], []
    today = datetime.now(ET).date()
    prior, today_sigs = [], []
    with open(audit_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") != "scored_signal" or "t" not in ev:
                continue
            ev_date = datetime.fromtimestamp(ev["t"] / 1_000_000,
                                             tz=ET).date()
            (today_sigs if ev_date == today else prior).append(ev)
    prior.sort(key=lambda e: e["t"])
    today_sigs.sort(key=lambda e: e["t"])
    return prior, today_sigs


def _load_fills_split_by_today(audit_path: str) -> tuple[list[dict], list[dict]]:
    """Read audit_path (if it exists) and return (prior_fills, todays_fills)
    — every "order_filled" event ever logged, split by whether it's from
    a calendar day before today (ET, matching RiskPolicy's own daily
    rollover convention) or from today itself, both in chronological
    order. Malformed lines (e.g. from a process killed mid-write) are
    skipped, not fatal.

    THE SPLIT MATTERS: cost basis and "today's reported totals" are NOT
    the same scope. A position bought yesterday and sold today needs
    its real yesterday-established cost basis to price today's sale
    correctly — but what gets REPORTED as "today's" fills/P&L/wins
    should still only be today's own activity. An earlier version of
    this function discarded prior-day fills entirely, which correctly
    scoped the daily order cap but WRONGLY discarded cost basis too: a
    position bought the day before and sold at today's open showed its
    ENTIRE sale price as profit, because nothing remembered what it had
    actually been bought for. See OrderManager.__init__ for how the two
    groups get replayed differently (prior_fills silently, for cost
    basis only; todays_fills normally, updating reported totals too)."""
    if not os.path.exists(audit_path):
        return [], []
    today = datetime.now(ET).date()
    prior, today_fills = [], []
    with open(audit_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue          # corrupted line: skip, don't crash startup
            if ev.get("event") != "order_filled" or "t" not in ev:
                continue
            ev_date = datetime.fromtimestamp(ev["t"] / 1_000_000,
                                             tz=ET).date()
            (today_fills if ev_date == today else prior).append(ev)
    prior.sort(key=lambda e: e["t"])
    today_fills.sort(key=lambda e: e["t"])
    return prior, today_fills


# ---------------------------------------------------------------------------
# Brokers
# ---------------------------------------------------------------------------
class BrokerError(Exception):
    pass


class MockBroker:
    """Instant fills at the signal price; injectable rejections for tests."""

    def __init__(self, reject_next: int = 0):
        self.positions: dict[str, int] = {}
        self.fills: list[dict] = []
        self.reject_next = reject_next     # tests: fail this many submissions

    def get_position_qty(self, symbol: str) -> int:
        return self.positions.get(symbol, 0)

    def submit_market_order(self, symbol: str, qty: int, side: str,
                            ref_price_e4: int) -> dict:
        if self.reject_next > 0:
            self.reject_next -= 1
            raise BrokerError("mock rejection (injected)")
        self.positions[symbol] = self.positions.get(symbol, 0) + \
            (qty if side == "buy" else -qty)
        fill = {"symbol": symbol, "qty": qty, "side": side,
                "fill_price_e4": ref_price_e4, "t": now_us()}
        self.fills.append(fill)
        return fill


class _AlpacaREST:
    """Shared Alpaca REST plumbing. Never instantiate directly — use
    AlpacaPaperBroker or AlpacaLiveBroker, each of which pins its URL."""

    def __init__(self, key: str, secret: str, base_url: str):
        self.base = base_url
        self.hdrs = {"APCA-API-KEY-ID": key,
                     "APCA-API-SECRET-KEY": secret,
                     "Content-Type": "application/json"}

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path, method=method, headers=self.hdrs,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read() or "{}")
        except urllib.error.HTTPError as e:
            raise BrokerError(f"{method} {path}: HTTP {e.code} "
                              f"{e.read().decode(errors='replace')[:200]}")
        except urllib.error.URLError as e:
            raise BrokerError(f"{method} {path}: {e.reason}")

    def get_position_qty(self, symbol: str) -> int:
        try:
            pos = self._req("GET", f"/v2/positions/{symbol}")
            return int(float(pos.get("qty", 0)))
        except BrokerError as e:
            if "HTTP 404" in str(e):       # no position = flat, not an error
                return 0
            raise

    def submit_market_order(self, symbol: str, qty: int, side: str,
                            ref_price_e4: int) -> dict:
        order = self._req("POST", "/v2/orders", {
            "symbol": symbol, "qty": str(qty), "side": side,
            "type": "market", "time_in_force": "day"})
        # poll briefly for the fill (near-instant in RTH)
        oid = order["id"]
        for _ in range(20):
            o = self._req("GET", f"/v2/orders/{oid}")
            if o.get("status") == "filled":
                px = float(o.get("filled_avg_price") or 0)
                return {"symbol": symbol, "qty": qty, "side": side,
                        "fill_price_e4": int(round(px * 10_000)),
                        "order_id": oid, "t": now_us()}
            if o.get("status") in ("rejected", "canceled", "expired"):
                raise BrokerError(f"order {oid} ended {o['status']}")
            time.sleep(0.25)
        return {"symbol": symbol, "qty": qty, "side": side,
                "fill_price_e4": ref_price_e4, "order_id": oid,
                "t": now_us(), "note": "fill not confirmed within poll window"}


class AlpacaPaperBroker(_AlpacaREST):
    """Paper endpoint, pinned. The default; safe to point anything at."""

    def __init__(self, key: str, secret: str):
        super().__init__(key, secret, PAPER_URL)


class AlpacaLiveBroker(_AlpacaREST):
    """LIVE endpoint — REAL MONEY. Constructing this class requires the
    acknowledgement environment variable in addition to live credentials;
    the CLI adds further interlocks on top (see arm_live_trading)."""

    def __init__(self, key: str, secret: str):
        if os.environ.get("ALPACA_LIVE_ACK") != LIVE_ACK_PHRASE:
            raise ValueError(
                "live broker refused: set ALPACA_LIVE_ACK="
                f"{LIVE_ACK_PHRASE} to acknowledge real-money trading")
        super().__init__(key, secret, LIVE_URL)


def arm_live_trading(symbol: str, limits: "RiskLimits",
                     env=os.environ, input_fn=input,
                     isatty=sys.stdin.isatty) -> AlpacaLiveBroker:
    """The live interlock chain. ALL of these must pass, independently:

      1. --live flag                      (caller reached this function)
      2. ALPACA_LIVE_KEY / _SECRET set    (separate from paper keys — a
                                           paper credential can never be
                                           silently reused for live)
      3. ALPACA_LIVE_ACK phrase set       (checked again by the broker)
      4. limits.max_daily_loss set > 0    (a live session without a loss
                                           bound is not allowed to exist)
      5. interactive terminal             (no accidental scripted/cron
                                           live starts)
      6. operator retypes 'LIVE <SYMBOL>' after reading the limits banner
         (confirmation restates parameters — two-key discipline)

    Testable: env/input_fn/isatty are injectable.
    """
    key = env.get("ALPACA_LIVE_KEY")
    secret = env.get("ALPACA_LIVE_SECRET")
    if not (key and secret):
        raise SystemExit("live refused: set ALPACA_LIVE_KEY and "
                         "ALPACA_LIVE_SECRET (deliberately distinct from "
                         "the paper ALPACA_KEY/ALPACA_SECRET)")
    if env.get("ALPACA_LIVE_ACK") != LIVE_ACK_PHRASE:
        raise SystemExit("live refused: set ALPACA_LIVE_ACK="
                         + LIVE_ACK_PHRASE)
    if not limits.max_daily_loss or limits.max_daily_loss <= 0:
        raise SystemExit("live refused: --max-daily-loss is mandatory and "
                         "must be > 0 in live mode")
    if not isatty():
        raise SystemExit("live refused: interactive terminal required "
                         "(no scripted live starts)")

    sym = symbol.strip().upper()
    print("\n" + "!" * 62)
    print("!!  LIVE TRADING — REAL MONEY — READ BEFORE CONFIRMING       !!")
    print("!" * 62)
    print(f"  symbol            {sym}")
    print(f"  shares per entry  {limits.order_qty}")
    print(f"  max position      {limits.max_shares} shares")
    print(f"  max notional      ${limits.max_notional_e4/10_000:,.2f} per order")
    print(f"  max orders/day    {limits.max_orders_per_day}")
    print(f"  cooldown          {limits.cooldown_s:.0f} s")
    print(f"  DAILY LOSS HALT   ${limits.max_daily_loss:,.2f} realized")
    print(f"  market hours      enforced (cannot be disabled in live)")
    expected = f"LIVE {sym}"
    if input_fn(f"  type '{expected}' to arm, anything else aborts: ")\
            .strip() != expected:
        raise SystemExit("live aborted by operator")
    class _Env:  # re-check phrase via the class gate too (defense in depth)
        pass
    return AlpacaLiveBroker(key, secret)


# ---------------------------------------------------------------------------
# Risk policy — pure decision function, trivially unit-testable
# ---------------------------------------------------------------------------
@dataclass
class RiskLimits:
    order_qty: int = 1                    # shares per entry
    max_shares: int = 10                  # position ceiling
    max_notional_e4: int = 2_000 * 10_000 # $2000 per order
    max_orders_per_day: int = 1000
    cooldown_s: float = 60.0              # anti-whipsaw gap between orders
    require_market_hours: bool = True     # RTH gate (no holiday calendar)
    max_daily_loss: float | None = None   # $ realized; halt when breached
                                          # (mandatory in live mode)


def market_is_open(t: datetime | None = None) -> bool:
    """Regular trading hours, 09:30–16:00 ET, Mon–Fri. No holiday calendar —
    a holiday order will simply be rejected/queued by the broker, which the
    rejection path already handles."""
    t = t or datetime.now(ET)
    if t.weekday() >= 5:
        return False
    mins = t.hour * 60 + t.minute
    return (9 * 60 + 30) <= mins < (16 * 60)


class RiskPolicy:
    def __init__(self, limits: RiskLimits, now_fn=None):
        """now_fn: optional callable returning the "current" aware datetime.
        Defaults to real wall-clock time (datetime.now(ET)) — LIVE behavior
        is completely unchanged. A backtest replaying historical trades
        injects a callable that returns each trade's OWN timestamp instead,
        so cooldown and daily-cap rollover are evaluated against historical
        time, not the seconds it takes this process to replay years of
        data. See backtest.py's BacktestClock."""
        self.lim = limits
        self._now_fn = now_fn or (lambda: datetime.now(ET))
        self.orders_today = 0
        self.day = self._now_fn().date()
        self.last_order_t = 0.0

    def evaluate(self, side: int, position_qty: int,
                 price_e4: int) -> tuple[bool, str, int]:
        """Return (allowed, reason, qty). Pure; no side effects."""
        lim = self.lim
        now = self._now_fn()
        today = now.date()
        if today != self.day:                        # daily counter rollover
            self.day, self.orders_today = today, 0

        if lim.require_market_hours and not market_is_open():
            return False, "market closed", 0
        if self.orders_today >= lim.max_orders_per_day:
            return False, f"daily order cap ({lim.max_orders_per_day}) reached", 0
        gap = now.timestamp() - self.last_order_t
        if self.last_order_t and gap < lim.cooldown_s:
            return False, f"cooldown ({gap:.1f}s < {lim.cooldown_s}s)", 0

        if side == SIDE_BUY:
            qty = lim.order_qty
            if position_qty + qty > lim.max_shares:
                return False, f"would exceed max_shares ({lim.max_shares})", 0
            if qty * price_e4 > lim.max_notional_e4:
                return False, (f"notional {dollars(qty*price_e4):.2f} > "
                               f"{dollars(lim.max_notional_e4):.2f}"), 0
            return True, "ok", qty

        if side == SIDE_SELL:
            if position_qty <= 0:
                return False, "flat (long-only: nothing to sell)", 0
            return True, "ok", position_qty          # close the whole position

        return False, f"unknown side {side}", 0

    def record_order(self):
        self.orders_today += 1
        self.last_order_t = self._now_fn().timestamp()


# ---------------------------------------------------------------------------
# Order manager
# ---------------------------------------------------------------------------
class OrderManager:
    MAX_CONSECUTIVE_REJECTS = 3

    def __init__(self, broker, symbols, limits: RiskLimits,
                 audit_path: str = "om_audit.jsonl",
                 killfile: str = "om.kill"):
        self.broker = broker
        if isinstance(symbols, str):
            symbols = [symbols]
        self.symbols = [t.strip().upper() for t in symbols]
        self.symbol = self.symbols[0]
        self.policy = RiskPolicy(limits)
        self.killfile = killfile
        self.halted = False
        self.halt_reason = ""
        self.consecutive_rejects = 0
        self.orders = 0
        self.blocked = 0
        self.costs = CostTracker()
        self._audit_f = open(audit_path, "a")

        # a previous kill must be acknowledged by a human before we run
        if os.path.exists(killfile):
            raise SystemExit(
                f"kill marker '{killfile}' exists — a previous session "
                "halted. Investigate, then delete the file to re-arm.")

        # broker is the source of truth: reconcile EVERY symbol, don't
        # remember (v2: per-symbol positions, long-only each)
        self.positions = {t: self.broker.get_position_qty(t)
                          for t in self.symbols}

        # Positions reconcile from the broker; realized P&L, cost basis,
        # and the daily order count do NOT have an external source of
        # truth like that, so without this they'd silently reset every
        # restart, even mid-day (a real reported issue — NET P&L showed
        # $0 immediately after a restart despite real trading earlier
        # the same day). Two-phase replay, because cost basis and
        # "today's reported totals" are NOT the same scope: a position
        # bought YESTERDAY and sold today needs its real prior cost
        # basis to price today's sale correctly, but what's REPORTED as
        # today's fills/P&L/wins should still only be today's own
        # activity. Getting this conflated the first time caused a real
        # bug: a position bought the day before and sold at today's
        # open showed its entire sale price as profit, because the
        # prior day's buy that established its true cost basis had been
        # discarded along with everything else from before today.
        prior_fills, todays_fills = _load_fills_split_by_today(audit_path)
        for ev in prior_fills:
            # silent: rebuilds cost basis for anything still open,
            # without polluting today's reported totals
            self.costs.on_fill(ev["side"], ev["qty"], ev["fill_price_e4"],
                               ev.get("symbol", "").strip(), count=False)
        for ev in todays_fills:
            self.costs.on_fill(ev["side"], ev["qty"], ev["fill_price_e4"],
                               ev.get("symbol", "").strip())
        if prior_fills:
            print(f"[om] carried forward cost basis from {len(prior_fills)} "
                 f"prior-day fill(s) in {audit_path} (for any position "
                 f"still open) — not counted toward today's totals")
        if todays_fills:
            self.policy.orders_today = len(todays_fills)
            self.policy.last_order_t = todays_fills[-1]["t"] / 1_000_000.0
            print(f"[om] restored {len(todays_fills)} fill(s) from earlier "
                 f"today ({audit_path}): net P&L so far "
                 f"${self.costs.net_pnl_usd:+.2f}, daily order count "
                 f"{self.policy.orders_today}/{limits.max_orders_per_day}")
        else:
            print(f"[om] no fills found for today in {audit_path} — "
                 f"starting the day's P&L and order count fresh")

        self._audit("startup", positions=self.positions,
                    limits={k: v for k, v in vars(limits).items()})
        print(f"[om] reconciled positions from broker: {self.positions}")

    # ---- audit ---------------------------------------------------------------
    def _audit(self, event: str, **kw):
        self._audit_f.write(json.dumps({"t": now_us(), "event": event, **kw})
                            + "\n")
        self._audit_f.flush()

    # ---- kill switch -----------------------------------------------------------
    def halt(self, reason: str, **extra):
        """extra: additional diagnostic fields persisted alongside the
        KILL event in the audit log — e.g. a divergence's symbol,
        strategy, how long it waited, and the actual signal contents
        (side/price/sma_fast/sma_slow) that didn't match. Previously
        on_divergence() only ever passed the short reason string through
        to halt(), so all of that richer detail existed in memory for
        one moment and was then gone — reconstructing what actually
        happened required re-deriving it from scratch after the fact.
        The killfile's own plaintext content is unchanged (still just
        the reason), so anything reading that file directly still works
        identically; the extra detail lives in the audit log only."""
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        self._audit("KILL", reason=reason, **extra)
        with open(self.killfile, "w") as f:
            f.write(f"{datetime.now(ET).isoformat()}  {reason}\n")
        print(f"[om] *** KILL SWITCH: {reason} — no further orders; "
              f"delete '{self.killfile}' to re-arm a future session ***")

    def on_divergence(self, info: dict):
        detail = {k: v for k, v in info.items() if k != "reason"}
        self.halt(f"model/hardware divergence: {info.get('reason')}",
                 **detail)

    @property
    def position_qty(self) -> int:            # back-compat: primary symbol
        return self.positions.get(self.symbol, 0)

    # ---- the signal path ---------------------------------------------------------
    def on_signal(self, fr: dict) -> str:
        """Callback for VERIFIED FPGA signals (bridge SignalVerifier).
        Returns a short status string describing what happened to this
        signal — "FILLED", "blocked: <reason>", or "rejected: <error>" —
        so callers (the dashboard's signals table, in particular) can
        show WHY a signal didn't trade instead of just that it fired."""
        side = fr["side"]
        price_e4 = fr["price_e4"]
        sym = fr.get("symbol", self.symbol).strip() or self.symbol
        if sym not in self.positions:          # symbol added at runtime
            self.positions[sym] = self.broker.get_position_qty(sym)
        if self.halted:
            self.blocked += 1
            self._audit("blocked", reason=f"halted: {self.halt_reason}",
                        symbol=sym, side=side, price_e4=price_e4)
            return f"blocked: halted: {self.halt_reason}"

        allowed, reason, qty = self.policy.evaluate(side,
                                                    self.positions[sym],
                                                    price_e4)
        if not allowed:
            self.blocked += 1
            self._audit("blocked", reason=reason, symbol=sym, side=side,
                        price_e4=price_e4,
                        position_qty=self.positions[sym])
            print(f"[om] blocked {sym} "
                  f"{('BUY' if side == SIDE_BUY else 'SELL')}: {reason}")
            return f"blocked: {reason}"

        verb = "buy" if side == SIDE_BUY else "sell"
        self._audit("order_submit", symbol=sym, side=verb, qty=qty,
                    price_e4=price_e4)
        try:
            fill = self.broker.submit_market_order(sym, qty, verb,
                                                   price_e4)
        except BrokerError as e:
            self.consecutive_rejects += 1
            self._audit("order_rejected", error=str(e),
                        consecutive=self.consecutive_rejects)
            print(f"[om] order rejected: {e}")
            if self.consecutive_rejects >= self.MAX_CONSECUTIVE_REJECTS:
                self.halt(f"{self.consecutive_rejects} consecutive broker "
                          "rejections")
            return f"rejected: {e}"

        self.consecutive_rejects = 0
        self.orders += 1
        self.policy.record_order()
        self.positions[sym] += qty if verb == "buy" else -qty
        fees = self.costs.on_fill(verb, qty, fill["fill_price_e4"], sym)
        self._audit("order_filled", **fill,
                    position_qty=self.positions[sym], fees=fees,
                    realized_pnl_e4=self.costs.realized_pnl_e4)
        fee_str = f"  fees ${fees['total']:.2f}" if fees else ""
        print(f"[om] FILLED {verb.upper()} {qty} {sym} @ "
              f"${dollars(fill['fill_price_e4']):.4f}  "
              f"-> position {self.positions[sym]}{fee_str}")
        # daily loss halt: realized net P&L breaching the bound stops the
        # session — losses can only be REALIZED on sells, so this check
        # after each fill is sufficient for a long-only strategy
        lim = self.policy.lim.max_daily_loss
        if lim and self.costs.net_pnl_usd <= -lim:
            self.halt(f"daily loss limit breached: net "
                      f"${self.costs.net_pnl_usd:+,.2f} <= -${lim:,.2f}")
        return "FILLED"

    # ---- teardown -----------------------------------------------------------------
    def summary(self, household_income: float | None = None,
                filing_status: str = "mfj", state_rate_pct: float = 4.40,
                income_is_gross: bool = False):
        print("\n---- order manager summary " + "-" * 33)
        print(f"  orders filled    {self.orders}")
        print(f"  signals blocked  {self.blocked}")
        openpos = {k: v for k, v in self.positions.items() if v}
        print(f"  final positions  "
              f"{openpos if openpos else 'flat'}"
              + ("  (open — P&L below is REALIZED only)" if openpos else ""))
        print(f"  kill switch      "
              f"{'TRIPPED: ' + self.halt_reason if self.halted else 'armed'}")
        print(self.costs.report(household_income, filing_status,
                                state_rate_pct, income_is_gross))
        self._audit("shutdown", orders=self.orders, blocked=self.blocked,
                    positions=self.positions, halted=self.halted,
                    total_fees=self.costs.total_fees,
                    realized_pnl_e4=self.costs.realized_pnl_e4)
        self._audit_f.close()


# ---------------------------------------------------------------------------
# Integrated CLI: bridge + order manager in one process
# ---------------------------------------------------------------------------
def sync_live_card(cards: dict, strategy: str, om: "OrderManager"):
    """Copy the traded strategy's REAL numbers from om.costs/om.positions
    into its scorecard. Must be called after EVERY verified signal for
    the live strategy, not just at shutdown — the dashboard polls this
    same `cards` dict every 500ms throughout a live session, so only
    syncing once at the end left it showing frozen zero defaults for the
    entire session's duration: real fills and real P&L were happening,
    but the strategy comparison panel showed 0 trips / 0 wins / net $0
    the whole time regardless (a real reported bug)."""
    live = cards[strategy]
    live.trips = om.costs.sells
    live.wins = om.costs.wins     # CostTracker now tracks per-trip win/loss
                                  # (added specifically so this stops
                                  # showing as a dash in the dashboard)
    live.pnl_e4 = om.costs.realized_pnl_e4
    live.fees_usd = om.costs.total_fees
    live.blocked = om.blocked
    live.positions = dict(om.positions)


def main():
    from bridge import Bridge, run_sim, run_alpaca   # reuse everything

    ap = argparse.ArgumentParser(
        description="FPGA signal -> risk-checked paper order")
    ap.add_argument("--port", required=True)
    ap.add_argument("--symbol", "--symbols", dest="symbols", default="SPY",
                    help="comma-separated, up to 8 (e.g. SPY,QQQ,AAPL)")
    ap.add_argument("--fast", type=int, default=8)
    ap.add_argument("--slow", type=int, default=32)
    ap.add_argument("--ema-kf", type=int, default=3,
                    help="fast EMA shift of the built bitstream (alpha 2^-k)")
    ap.add_argument("--ema-ks", type=int, default=5)
    ap.add_argument("--vwap-warmup", type=int, default=20,
                    help="VWAP_WARMUP of the built bitstream — ticks "
                         "before the fabric VWAP engine allows events "
                         "(default 20 matches top_arty.sv's parameter "
                         "default; only pass this if you rebuilt with "
                         "a different value)")
    ap.add_argument("--vwap-k2-q8", type=int, default=256,
                    help="VWAP_K2_Q8 of the built bitstream — band "
                         "width k² in Q8 fixed point (default 256 = "
                         "k of 1.0, matches top_arty.sv's default). "
                         "NOT the same thing as --vwap-band-k below: "
                         "that one tunes the independent HOST-side "
                         "--vwap-bounce scorecard's own band math; "
                         "this one must match the FABRIC bitstream's "
                         "build parameter or the hardware verifier "
                         "will report false divergences")
    ap.add_argument("--strategy", choices=["sma", "ema"], default="sma",
                    help="which engine's signals TRADE; the other is "
                         "scored hypothetically for comparison")
    ap.add_argument("--ladder", action="store_true",
                    help="also score a weekly-anchored buy-the-dip ladder "
                         "(see ladder_strategy.py) — SCORE ONLY, never "
                         "trades, regardless of --strategy")
    ap.add_argument("--ladder-step", type=float, default=0.03,
                    help="ladder trigger spacing, e.g. 0.03 = 3%%")
    ap.add_argument("--ladder-levels", type=int, default=3,
                    help="max buy levels before the ladder is 'full'")
    ap.add_argument("--ladder-qty", type=int, default=1,
                    help="shares bought at EACH level")
    ap.add_argument("--ladder-method", choices=list(__import__(
                    "ladder_strategy").BASELINE_METHODS),
                    default="week_vwap",
                    help="how to compute each symbol's weekly baseline")
    ap.add_argument("--vwap-bounce", action="store_true",
                    help="also score the session-VWAP mean-reversion "
                         "bounce strategy on live ticks (see "
                         "vwap_bounce_strategy.py) — SCORE ONLY, never "
                         "trades, regardless of --strategy. One scored "
                         "row per configured symbol. This is the "
                         "real-market evaluation step for the strategy "
                         "the multi-year QQQ/VTI backtests found "
                         "consistently profitable, ahead of any FPGA/RTL "
                         "investment in it. NOTE: like the ladder, this "
                         "consumes raw ticks (not verified signals), so "
                         "its session VWAP and scored totals start fresh "
                         "on every process start — a mid-day restart "
                         "resets this row (the scored-signal audit "
                         "replay that restores EMA/profit-gated cannot "
                         "rebuild tick-derived state)")
    ap.add_argument("--vwap-band-k", type=float, default=1.0,
                    help="VWAP bounce band width in session standard "
                         "deviations (default 1.0, matching backtest.py)")
    ap.add_argument("--profit-gate", action="store_true",
                    help="also score the SAME SMA crossover signals with "
                         "one added rule: a sell only executes if price "
                         "is above the average cost of shares held — "
                         "SCORE ONLY, never trades, regardless of "
                         "--strategy")
    ap.add_argument("--pg-max-hold-days", type=float, default=5.0,
                    help="--profit-gate only: force-close a position held "
                         "longer than this many days at the next signal, "
                         "even at a loss — bounds the never-realize-a-"
                         "loss rule's unbounded downside (see backtest.py's "
                         "flag of the same name, which found the case for "
                         "this: multi-year VTI/QQQ backtests showed "
                         "'would realize a loss' as the single largest "
                         "gated-away reason, and a perpetually-open "
                         "position carrying unbounded unrealized loss the "
                         "report couldn't show). <= 0 disables (restores "
                         "the original unbounded behavior). Default 5.0")
    ap.add_argument("--baud", type=int, default=921_600,
                    help="must match the bitstream's BAUD parameter — "
                         "921600 (default) for the current build, 115200 "
                         "for anything built before this change")
    ap.add_argument("--verify-grace-s", type=float, default=2.0,
                    help="real SECONDS an unmatched FPGA/model signal may "
                         "wait before the kill switch trips on 'model/"
                         "hardware divergence: orphan ... signal' — NOT "
                         "an echo count. Raise this if that divergence "
                         "recurs during genuinely high signal-volume "
                         "periods (multiple symbols firing, daily cap "
                         "maxed out) rather than a real hardware fault; "
                         "see SignalVerifier in bridge.py")
    ap.add_argument("--ladder-baseline", default=None,
                    help="manual override, e.g. 'SPY:500.00,QQQ:450.00' — "
                         "skips the Alpaca weekly-bars fetch (required "
                         "for --source sim, since there's no real feed "
                         "to compute a baseline from)")
    ap.add_argument("--source", choices=["sim", "alpaca"], default="sim")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--rate", type=float, default=10.0)
    ap.add_argument("--start-price", type=float, default=500.0)
    ap.add_argument("--broker", choices=["mock", "alpaca"], default="mock")
    ap.add_argument("--live", action="store_true",
                    help="REAL MONEY. Requires --broker alpaca plus the full "
                         "interlock chain (see arm_live_trading)")
    ap.add_argument("--max-daily-loss", type=float, default=None,
                    help="$ realized loss that halts the session "
                         "(MANDATORY in --live)")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--max-shares", type=int, default=10)
    ap.add_argument("--max-notional", type=float, default=2000.0)
    ap.add_argument("--max-orders-per-day", type=int, default=1000)
    ap.add_argument("--cooldown", type=float, default=60.0)
    ap.add_argument("--ignore-market-hours", action="store_true",
                    help="for mock/off-hours testing")
    ap.add_argument("--audit", default="om_audit.jsonl")
    ap.add_argument("--household-income", type=float, default=None,
                    help="taxable household income for the tax estimate "
                         "(use --gross if you're giving gross income)")
    ap.add_argument("--filing-status", choices=["single", "mfj"],
                    default="mfj")
    ap.add_argument("--state-rate", type=float, default=4.40,
                    help="flat state income tax %% (default: Colorado 4.40)")
    ap.add_argument("--gross", action="store_true",
                    help="treat --household-income as gross; subtract the "
                         "2026 standard deduction")
    ap.add_argument("--log", default=None, help="bridge tick JSONL")
    ap.add_argument("--dashboard", type=int, default=None, metavar="PORT",
                    help="serve the web console on this port (e.g. 8000)")
    ap.add_argument("--selftest", action="store_true",
                    help="hardware acceptance test: connect to --port, "
                         "run a deterministic warm-up + spike stimulus, "
                         "and verify the board's SMA/EMA/VWAP signals "
                         "against independent host models bit-for-bit. "
                         "Prints PASS or DIAG lines explaining what's "
                         "wrong, then exits — no trading, no dashboard. "
                         "Run this FIRST after any bitstream change, "
                         "before a live or historical-replay session.")
    args = ap.parse_args()

    if args.selftest:
        # hardware acceptance test — real board, no broker, no
        # trading, no dashboard, no OrderManager: none of that setup
        # below is needed, so this exits BEFORE any of it runs (a
        # --broker alpaca selftest shouldn't need ALPACA_KEY set, and
        # a --live selftest should never arm live trading at all).
        # run_selftest() prints its own PASS/DIAG/FAIL lines.
        symbols = [t for t in args.symbols.split(",") if t.strip()]
        br = Bridge(args.port, symbols, args.fast, args.slow,
                    ema_kf=args.ema_kf, ema_ks=args.ema_ks,
                    baud=args.baud, vwap_warmup=args.vwap_warmup,
                    vwap_k2_q8=args.vwap_k2_q8)
        from bridge import run_selftest
        run_selftest(br)
        br.close()
        return

    from compare import normalize_max_hold_days
    pg_max_hold = normalize_max_hold_days(args.pg_max_hold_days)

    limits = RiskLimits(order_qty=args.qty,
                        max_shares=args.max_shares,
                        max_notional_e4=int(args.max_notional * 10_000),
                        max_orders_per_day=args.max_orders_per_day,
                        cooldown_s=args.cooldown,
                        require_market_hours=(args.live or
                                              (args.broker == "alpaca"
                                               and not args.ignore_market_hours)),
                        max_daily_loss=args.max_daily_loss)

    if args.live:
        if args.broker != "alpaca":
            sys.exit("--live requires --broker alpaca")
        broker = arm_live_trading(args.symbols.split(",")[0].strip().upper(),
                                  limits)
        print(f"[om] broker: Alpaca *** LIVE *** ({LIVE_URL})")
    elif args.broker == "alpaca":
        key = os.environ.get("ALPACA_KEY")
        secret = os.environ.get("ALPACA_SECRET")
        if not (key and secret):
            sys.exit("set ALPACA_KEY and ALPACA_SECRET")
        broker = AlpacaPaperBroker(key, secret)
        print(f"[om] broker: Alpaca PAPER ({PAPER_URL})")
    else:
        broker = MockBroker()
        print("[om] broker: mock (no orders leave this machine)")
    symbols = [t for t in args.symbols.split(",") if t.strip()]
    om = OrderManager(broker, symbols, limits, audit_path=args.audit)

    from compare import StrategyScorecard, comparison_report
    br = Bridge(args.port, symbols, args.fast, args.slow,
                ema_kf=args.ema_kf, ema_ks=args.ema_ks, baud=args.baud,
                log_path=args.log, verify_grace_s=args.verify_grace_s,
                vwap_warmup=args.vwap_warmup, vwap_k2_q8=args.vwap_k2_q8)

    # v3.19: command the fabric VWAP session boundary at startup. Every
    # process start IS a session start from the fabric's point of view —
    # the board may hold yesterday's accumulators (it has no calendar; see
    # rtl/sessctl.sv for why the HOST owns that), and the host mirrors
    # start empty, so the two sides must be zeroed together before the
    # first tick or the verifier would flag divergences that are really
    # just mismatched session baselines. Safe against every board
    # revision: a pre-v3.18 bitstream has no sessctl but still ECHOES the
    # 0x11 frame as 0x91 (the echo path echoes every decoded frame, and
    # tick_parser accepts any type byte), so the ack arrives either way —
    # a timeout here means the link itself is in trouble.
    if not br.send_sessrst():
        print("[om] WARNING: VWAP session reset not acknowledged — the "
              "link may be down; fabric VWAP state may span sessions "
              "until a reset is acked")

    labels = {"sma": f"SMA {args.fast}/{args.slow}",
              "ema": f"EMA 1/{1 << args.ema_kf}:1/{1 << args.ema_ks}"}
    cards = {}
    for name, label in labels.items():
        live = (name == args.strategy)
        cards[name] = StrategyScorecard(
            name=label, live=live,
            # the UNTRADED strategy is gated through its OWN RiskPolicy
            # clone, built from the SAME RiskLimits the real OM enforces
            # and ticking on the same wall clock — so this row answers
            # "how would this strategy have fared under IDENTICAL
            # constraints" rather than "if every signal became a trade".
            # The live row is overwritten from om.costs at session end.
            policy=None if live else RiskPolicy(limits))

    ladder = None
    if args.ladder:
        from ladder_strategy import (LadderScorecard, compute_weekly_baseline,
                                     fetch_prior_week_bars)
        ladder = LadderScorecard(
            f"Ladder {args.ladder_step*100:.0f}%/{args.ladder_levels}lvl",
            step_pct=args.ladder_step, max_levels=args.ladder_levels,
            qty_per_level=args.ladder_qty, live=False)
        cards["ladder"] = ladder

        manual = {}
        if args.ladder_baseline:
            for pair in args.ladder_baseline.split(","):
                sym, price = pair.split(":")
                manual[sym.strip().upper()] = float(price)
        for sym in symbols:
            sym = sym.strip().upper()
            if sym in manual:
                ladder.set_baseline(sym, to_e4(manual[sym]))
            elif args.source == "alpaca":
                key = os.environ.get("ALPACA_KEY")
                secret = os.environ.get("ALPACA_SECRET")
                bars = fetch_prior_week_bars(sym, key, secret)
                base = compute_weekly_baseline(bars, args.ladder_method)
                ladder.set_baseline(sym, base)
                print(f"[ladder] {sym} baseline ({args.ladder_method}): "
                      f"${base/10_000:.2f}")
            else:
                print(f"[ladder] WARNING: no baseline for {sym} — pass "
                      f"--ladder-baseline {sym}:<price> for --source sim")

    profit_gated = None
    if args.profit_gate:
        from compare import ProfitGatedScorecard
        # its OWN RiskPolicy clone (same limits as every other shadow
        # row) — this isolates the sell-side profit rule as the ONLY
        # difference from the plain SMA row, not a difference in
        # cooldown/daily-cap/position-sizing too
        profit_gated = ProfitGatedScorecard(
            "SMA profit-gated", policy=RiskPolicy(limits),
            max_hold_days=pg_max_hold)
        cards["sma_pg"] = profit_gated

    vwap_cards = {}
    if args.vwap_bounce:
        from vwap_bounce_strategy import VWAPBounceScorecard
        # VWAPBounceScorecard is single-symbol by design (its session
        # state — Σpv, Σv, Σp²v, band edge tracking — is per symbol),
        # so a multi-symbol session gets one card per symbol, each with
        # its OWN RiskPolicy clone (same limits), exactly like every
        # other shadow row. Wall-clock policies: on_tick's historical-
        # clock hook (hasattr _now_fn.set) is a no-op live, as intended.
        for _sym in om.symbols:
            _name = (f"VWAP bounce {_sym}" if len(om.symbols) > 1
                     else "VWAP bounce")
            _card = VWAPBounceScorecard(
                _name, symbol=_sym, live=False,
                policy=RiskPolicy(limits), band_k=args.vwap_band_k)
            vwap_cards[_sym] = _card
            cards[f"vwap_{_sym.lower()}"] = _card
        print(f"[vwap] scoring session-VWAP bounce "
             f"(k={args.vwap_band_k}) on: {', '.join(om.symbols)} — "
             f"score-only; note: this row starts fresh each process "
             f"start (tick-derived state can't replay from the audit "
             f"log — see --help)")

    def _vwap_fpga_card(sym: str):
        """The scored row for VERIFIED FABRIC VWAP signals (wire 0x85),
        one per symbol, created on first use. Distinct from the
        --vwap-bounce row on purpose: that one is the HOST-computed,
        position-gated tick stream; this one is the engine-convention
        event stream (position-independent edges, SELL dominant — see
        rtl/vwap_engine.sv), gated through its own RiskPolicy clone,
        which IS the "host layer applies position logic" half of the
        hardware/host split. The two rows measuring the same strategy
        through two different paths is the point, not duplication.
        Lazy creation keeps the comparison table clean when the
        board's bitstream predates the VWAP engines (no zero rows for
        signals that can never arrive)."""
        key = f"vwapfpga_{sym.lower()}"
        if key not in cards:
            from compare import StrategyScorecard
            cards[key] = StrategyScorecard(
                name=(f"VWAP-FPGA {sym}" if len(om.symbols) > 1
                      else "VWAP-FPGA"),
                live=False, policy=RiskPolicy(limits))
        return cards[key]

    def route_to_shadow_cards(fr: dict, count: bool = True):
        """Feed a signal to whichever SCORED (non-live) cards should see
        it — the single routing rule used both for live signals
        arriving now (via on_verified, below) and for startup replay of
        today's earlier history (right below this). Keeping this in
        one place means replay can never reach a different set of
        cards than live signals do."""
        strat = fr["strategy"]
        if strat == "vwap_bounce":
            # verified fabric VWAP signals: per-symbol scored row (the
            # cards dict has no "vwap_bounce" key — this indexing gap
            # would have been a KeyError crash on the first hardware
            # VWAP signal, caught while wiring the routing, not live)
            _vwap_fpga_card(fr["symbol"].strip()).on_signal(fr, count=count)
        elif strat != args.strategy:
            cards[strat].on_signal(fr, count=count)
        if profit_gated is not None and strat == "sma":
            profit_gated.on_signal(fr, count=count)

    # ---- restore SCORED strategies' state from earlier today ---------------
    # Positions reconcile from the broker and the LIVE row restores from
    # om.costs (both fixed earlier) — but the scored/shadow cards (EMA
    # when it isn't the live strategy, and profit_gated) have no
    # external source of truth like a broker, so without this their
    # trips/wins/net$ silently reset to zero on every restart, even
    # mid-day — a real reported bug, found right after the live row's
    # own equivalent bug had already been fixed.
    prior_scored, todays_scored = _load_scored_signals_split_by_today(
        args.audit)
    if prior_scored or todays_scored:
        # precisely the cards route_to_shadow_cards can reach — NOT a
        # blanket "every non-live card" filter, which would incorrectly
        # sweep in the ladder (it consumes raw ticks via br.on_echo, not
        # verified signals, so this replay mechanism never touches it)
        other_strat = "ema" if args.strategy == "sma" else "sma"
        shadow_cards = [cards[other_strat]] if other_strat in cards else []
        if profit_gated is not None:
            shadow_cards.append(profit_gated)
        # historical clock per gated card, so cooldown/daily-cap replay
        # correctly against each signal's OWN timestamp — the exact same
        # problem (and the exact same fix) as backtest.py's BacktestClock
        clocks = {}
        for c in shadow_cards:
            if c.policy is not None:
                clk = HistoricalClock()
                c.policy._now_fn = clk
                clocks[id(c)] = clk

        def _set_clocks(t_us):
            t = datetime.fromtimestamp(t_us / 1_000_000, tz=ET)
            for clk in clocks.values():
                clk.set(t)

        for ev in prior_scored:      # silent: cost basis only
            _set_clocks(ev["t"])
            route_to_shadow_cards(ev, count=False)
        for ev in todays_scored:     # counted: today's real totals
            _set_clocks(ev["t"])
            route_to_shadow_cards(ev, count=True)

        for c in shadow_cards:       # back to real wall-clock time for
            if c.policy is not None:  # every live signal from here on
                c.policy._now_fn = lambda: datetime.now(ET)

        print(f"[compare] restored {len(todays_scored)} scored signal(s) "
             f"from earlier today ({args.audit}) for: "
             + ", ".join(c.name for c in shadow_cards)
             + (f" [{len(prior_scored)} prior-day signal(s) also replayed "
                f"silently, for cost basis only]" if prior_scored else ""))

    dash = None
    if args.dashboard:
        from dashboard import DashboardServer
        dash = DashboardServer(br, om, args.dashboard, scorecards=cards)
        dash.start()

    if ladder:
        # chain onto whatever's already listening for echoes (the
        # dashboard, if running) rather than replace it — the ladder
        # needs EVERY accepted trade, not just verified crossover
        # signals, since it compares raw price against static levels.
        # v3.19: now filters to TRADE echoes, same as the VWAP hook
        # below. This was a latent inconsistency (quote echoes carry
        # two-sided prices the ladder was never meant to compare), and
        # the fabric VWAP path made it concrete: any frame type the
        # parser files under "echo" would have fed the ladder's level
        # comparison as if it were a trade print.
        from tick_protocol import TYPE_ECHO_TRADE as _TET
        _prev_echo = br.on_echo
        def _on_echo_with_ladder(fr):
            if _prev_echo:
                _prev_echo(fr)
            if fr["type"] != _TET:
                return
            sym = fr["symbol"].strip()
            ev = ladder.on_tick(sym, fr["price_e4"])
            if ev:
                ladder.on_signal(ev)
        br.on_echo = _on_echo_with_ladder

    if vwap_cards:
        from tick_protocol import TYPE_ECHO_TRADE
        # same chaining pattern as the ladder above — VWAP also consumes
        # raw ticks, in parallel with whoever's already listening. Two
        # deliberate differences from the ladder's hook:
        #   * TRADE echoes only. on_echo fires for every echo kind,
        #     including QUOTE echoes (0x82) — quotes carry two-sided
        #     prices with different semantics, and folding them into
        #     Σ(p·v)/Σ(v) would corrupt the session VWAP. This is the
        #     same accept filter the RTL applies (TYPE_TRADE only) and
        #     the same reason indicator_engine.sv documents for it.
        #   * timestamps are ET wall-clock, because the card's session
        #     boundary is "the ET calendar day changed" — the semantics
        #     the strategy is defined in (backtests feed it the trade's
        #     own exchange timestamp for the same reason).
        _prev_echo_v = br.on_echo
        def _on_echo_with_vwap(fr):
            if _prev_echo_v:
                _prev_echo_v(fr)
            if fr["type"] != TYPE_ECHO_TRADE:
                return
            _card = vwap_cards.get(fr["symbol"].strip())
            if _card is not None:
                _card.on_tick(datetime.now(ET), fr["price_e4"],
                              fr["qty"])
        br.on_echo = _on_echo_with_vwap

    def on_verified(fr):
        strat = fr["strategy"]
        if strat == args.strategy:
            cards[strat].signals += 1     # real routing/gating/fills below
            outcome = om.on_signal(fr)
            sync_live_card(cards, args.strategy, om)  # keep dashboard fresh
        elif strat == "vwap_bounce":
            # verified FABRIC vwap signal — same per-symbol scored row
            # route_to_shadow_cards uses (cards has no "vwap_bounce"
            # key; the old cards[strat] here would have crashed)
            outcome = _vwap_fpga_card(fr["symbol"].strip()).on_signal(fr)
        else:
            outcome = cards[strat].on_signal(fr)  # hypothetical, gated
        # profit_gated is ALWAYS score-only, regardless of --strategy —
        # same as the ladder — so it gets fed in parallel, not instead
        # of the normal routing above. It watches the SAME "sma"
        # crossover signal stream (SMA is what the sell-above-cost rule
        # was built against), whether or not "sma" happens to be the
        # strategy actually trading.
        if profit_gated is not None and strat == "sma":
            profit_gated.on_signal(fr)
        # log once per signal that touched any SCORED card, so a
        # restart can restore trips/wins/net$ instead of resetting them
        # to zero — a real reported bug (EMA's numbers, and profit-
        # gated's, went stale/zero on every restart even though the
        # live SMA row had already been fixed to persist correctly)
        if strat != args.strategy or (profit_gated is not None
                                      and strat == "sma"):
            om._audit("scored_signal", **fr)
        if dash:
            dash.on_signal(fr, outcome)

    def on_divergence(info):
        if dash:
            dash.on_event("DIVERGENCE: " + info.get("reason", "?"), True)
        om.on_divergence(info)

    br.on_verified = on_verified          # survives slot reconfiguration:
    br.on_divergence = on_divergence      # _build_models re-attaches these
    print(f"[om] trading strategy: {args.strategy.upper()} "
          f"(the other is scored, gated identically, not traded)")

    try:
        if args.source == "sim":
            run_sim(br, args.n, args.rate, args.start_price)
        else:
            run_alpaca(br)
    except KeyboardInterrupt:
        pass
    finally:
        ok = br.summary()
        sync_live_card(cards, args.strategy, om)  # final guarantee, even if
                                                  # nothing arrived between
                                                  # the last signal and Ctrl+C
        print(comparison_report(cards))
        om.summary(args.household_income, args.filing_status,
                   args.state_rate, args.gross)
        br.close()
        sys.exit(0 if ok and not om.halted else 1)


if __name__ == "__main__":
    main()
