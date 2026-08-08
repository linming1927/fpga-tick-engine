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
    python3 order_manager.py --port /tmp/fpga-tick-emulator --source sim \
            --broker mock
    python3 order_manager.py --port /dev/ttyUSB1 --source alpaca \
            --broker alpaca --qty 1 --max-position-notional 500
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


def _load_last_sessrst_day(audit_path: str):
    """v3.38: the date (ET) of the most recent successful VWAP session
    reset, or None if none is on record. Used at startup to decide
    whether THIS restart is the first one of a new trading day (reset
    needed) or a same-day restart (skip resetting, so a continuously-
    running board/emulator keeps its already-accumulated state instead
    of losing it to a redundant reset)."""
    if not os.path.exists(audit_path):
        return None
    last = None
    with open(audit_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "sessrst_sent" and "t" in ev:
                d = datetime.fromtimestamp(ev["t"] / 1_000_000, tz=ET).date()
                if last is None or d > last:
                    last = d
    return last


def _replay_vwap_from_log(br, log_path: str, today) -> int:
    """v3.38: rebuild the HOST's own (otherwise empty on every process
    restart, regardless of what the board does) VWAPMirror instances by
    replaying today's raw trade ticks from --log. Returns the number
    of ticks replayed. --log records EVERY echo type, not just trades
    (quote echoes included), and both raw-tick lines and signal lines
    (marked "signal": True) — filtered here to TYPE_ECHO_TRADE, no
    "signal" key, and today's ET calendar day only, so a stale or
    multi-day log doesn't feed anything but what actually belongs in
    the CURRENT session."""
    from tick_protocol import TYPE_ECHO_TRADE
    if not os.path.exists(log_path):
        return 0
    n = 0
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (ev.get("type") != TYPE_ECHO_TRADE or ev.get("signal")
                    or "t" not in ev):
                continue
            d = datetime.fromtimestamp(ev["t"] / 1_000_000, tz=ET).date()
            if d != today:
                continue
            sym = ev.get("symbol", "").strip()
            model = br.models.get("vwap_bounce", {}).get(sym)
            if model is not None:
                model.ingest(ev["price_e4"], ev.get("qty", 0))
                n += 1
    return n


def _position_open_dates(all_fills: list[dict]) -> dict[str, object]:
    """v3.51: for every symbol still holding shares at the end of the
    audit history, the ET date on which that position last went from
    flat to non-flat.

    Needed because the risk overlay's same-day-vs-older sell gate keys
    off when a position OPENED, and a position reconciled from the
    broker at startup has no such record in memory. Reading it back from
    the fills is the only way to tell an overnight holding from a fresh
    scalp -- getting this wrong in the permissive direction would let
    the gate treat a week-old position as a same-day one.
    """
    running: dict[str, int] = {}
    opened: dict[str, object] = {}
    for ev in all_fills:
        sym = ev.get("symbol", "").strip()
        if not sym or "t" not in ev:
            continue
        before = running.get(sym, 0)
        qty = int(ev.get("qty", 0))
        after = before + (qty if ev.get("side") == "buy" else -qty)
        running[sym] = after
        if before == 0 and after != 0:
            opened[sym] = datetime.fromtimestamp(ev["t"] / 1_000_000,
                                                 tz=ET).date()
        elif after == 0:
            opened.pop(sym, None)
    return {k: v for k, v in opened.items() if running.get(k, 0) != 0}


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


class OrderPending(Exception):
    """v3.50: a submitted order did NOT confirm as filled inside the
    poll window. It is not a rejection -- the order is live at the
    broker and may still fill, partially or fully, at a price nobody
    knows yet.

    Deliberately NOT a subclass of BrokerError: every existing
    `except BrokerError` treats its catch as a failed order and counts
    it toward the kill switch, which is the opposite of what should
    happen here.

    This replaces submit_market_order()'s old behaviour of INVENTING a
    fill when the poll expired -- returning the signal price as the
    fill price with a "note". The caller took that at face value, so
    the position and the cost basis both moved for shares that did not
    exist yet. Real consequence, from a real session: two NVDA buys
    "filled" that way, our books said 25 shares, the broker actually
    had 7, and the next SELL was rejected as a wash trade against the
    still-open buy -- three times in one second, tripping the kill
    switch. 31 of 1,125 fills in that audit log were invented this way,
    each also recording a fabricated price.
    """

    def __init__(self, order_id: str, symbol: str, qty: int, side: str):
        self.order_id = order_id
        self.symbol = symbol
        self.qty = qty
        self.side = side
        super().__init__(f"order {order_id} ({side} {qty} {symbol}) still "
                         f"open after the poll window -- NOT counted as a "
                         f"fill")


class MockBroker:
    """Instant fills at the signal price; injectable rejections for tests.

    v3.50: also injectable PENDING outcomes (pending_next) plus
    get_order/cancel_order, so the pending-order path can be tested
    without a real broker. A pending order is tracked here exactly as
    Alpaca would: live, unfilled, and in the way of an opposite-side
    order on the same symbol until it is resolved or cancelled.
    """

    def __init__(self, reject_next: int = 0, pending_next: int = 0,
                 wash_on_opposite: bool = False):
        self.positions: dict[str, int] = {}
        self.fills: list[dict] = []
        self.reject_next = reject_next     # tests: fail this many submissions
        self.pending_next = pending_next   # tests: leave this many unfilled
        self.wash_on_opposite = wash_on_opposite   # tests: reject an
                                                   # opposite-side order
                                                   # while one is open,
                                                   # the way Alpaca does
        self.open_orders: dict[str, dict] = {}     # order_id -> order
        self._next_id = 1

    def get_position_qty(self, symbol: str) -> int:
        return self.positions.get(symbol, 0)

    def get_order(self, order_id: str) -> dict:
        o = self.open_orders.get(order_id)
        return o if o else {"id": order_id, "status": "canceled"}

    def cancel_order(self, order_id: str) -> bool:
        self.open_orders.pop(order_id, None)
        return True

    def list_open_orders(self) -> list[dict]:
        return list(self.open_orders.values())

    def cancel_all_open_orders(self) -> list[dict]:
        out = [{"id": i, "status": "canceled"} for i in self.open_orders]
        self.open_orders.clear()
        return out

    def submit_market_order(self, symbol: str, qty: int, side: str,
                            ref_price_e4: int) -> dict:
        if self.reject_next > 0:
            self.reject_next -= 1
            raise BrokerError("mock rejection (injected)")
        if self.wash_on_opposite:
            for o in self.open_orders.values():
                if o["symbol"] == symbol and o["side"] != side:
                    raise BrokerError(
                        'POST /v2/orders: HTTP 403 {"code":40310000,'
                        f'"existing_order_id":"{o["id"]}",'
                        '"message":"potential wash trade detected. use '
                        'complex orders","reject_reason":"opposite side '
                        'market/stop order exists"}')
        oid = f"mock-{self._next_id}"
        self._next_id += 1
        if self.pending_next > 0:
            self.pending_next -= 1
            self.open_orders[oid] = {"id": oid, "symbol": symbol, "qty": qty,
                                     "side": side, "status": "new"}
            raise OrderPending(oid, symbol, qty, side)
        self.positions[symbol] = self.positions.get(symbol, 0) + \
            (qty if side == "buy" else -qty)
        fill = {"symbol": symbol, "qty": qty, "side": side,
                "fill_price_e4": ref_price_e4, "order_id": oid,
                "t": now_us()}
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

    def list_open_orders(self) -> list[dict]:
        """v3.41: GET /v2/orders?status=open -- raw Alpaca order
        objects. Used at startup to catch orders left open from a
        PRIOR session (e.g. a fill that never confirmed before a
        disconnect) before they cause "wash trade" rejections against
        a NEW order on the same symbol."""
        result = self._req("GET", "/v2/orders?status=open")
        return result if isinstance(result, list) else []

    def cancel_all_open_orders(self) -> list[dict]:
        """v3.41: DELETE /v2/orders -- cancels EVERY open order on the
        account, regardless of what placed it (this tool, a prior
        crashed session, a manual trade, anything else). Returns
        Alpaca's own per-order result list ([{id, status}, ...])."""
        result = self._req("DELETE", "/v2/orders")
        return result if isinstance(result, list) else []

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
        # v3.50: the poll expired with the order still live. It used to
        # return a synthetic fill here (ref_price_e4 + a "note"), which
        # the caller then applied to the position and the cost basis --
        # see OrderPending's docstring for what that cost in practice.
        # Raise instead, and let the caller track it as pending.
        raise OrderPending(oid, symbol, qty, side)

    def get_order(self, order_id: str) -> dict:
        """v3.50: current state of one order, for resolving a pending
        submission before touching that symbol again."""
        return self._req("GET", f"/v2/orders/{order_id}")

    def cancel_order(self, order_id: str) -> bool:
        """v3.50: cancel ONE order. cancel_all_open_orders() already
        existed but is far too blunt mid-session -- it would also kill
        orders on unrelated symbols, and any the user placed by hand."""
        try:
            self._req("DELETE", f"/v2/orders/{order_id}")
            return True
        except BrokerError as e:
            # 404 (already gone) and 422 (already filled/cancelled) both
            # mean "it is no longer in the way", which is what the
            # caller actually cares about
            return "HTTP 404" in str(e) or "HTTP 422" in str(e)


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


def arm_live_trading(symbol: str, limits: "RiskLimits", strategy: str,
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
      6. operator retypes 'LIVE <SYMBOL> <STRATEGY>' after reading the
         limits banner (confirmation restates parameters — two-key
         discipline)

    strategy is REQUIRED, not defaulted — v3.24: with three tradeable
    strategies now possible (sma/ema/vwap_bounce), the confirmation
    phrase names which one is about to place real orders, not just
    which symbol. A --strategy flag typo or a stale saved command
    line arming the wrong strategy live is exactly the class of
    mistake this banner exists to catch before it costs money; the
    old phrase ("LIVE SPY" alone) couldn't catch it at all.

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
    strat = strategy.strip().upper()
    print("\n" + "!" * 62)
    print("!!  LIVE TRADING — REAL MONEY — READ BEFORE CONFIRMING       !!")
    print("!" * 62)
    print(f"  symbol            {sym}")
    print(f"  strategy          {strat}   <-- this one TRADES; the "
         f"others are scored only")
    print(f"  shares per entry  {limits.order_qty}")
    print(f"  max per order     ${limits.max_notional_e4/10_000:,.2f}")
    if limits.max_position_notional_e4 is not None:
        print(f"  max position      ${limits.max_position_notional_e4/10_000:,.2f} "
             f"total exposure")
    print(f"  max orders/day    {limits.max_orders_per_day}")
    print(f"  cooldown          {limits.cooldown_s:.0f} s")
    print(f"  DAILY LOSS HALT   ${limits.max_daily_loss:,.2f} realized")
    print(f"  market hours      enforced (cannot be disabled in live)")
    expected = f"LIVE {sym} {strat}"
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
    max_shares: int = 10                  # position ceiling (share count)
    max_notional_e4: int = 2_000 * 10_000 # $2000 per order
    max_position_notional_e4: int | None = None
                                          # v3.27: total position dollar-value
                                          # cap, an ALTERNATIVE to max_shares
                                          # for callers who want to size a
                                          # position by $ exposure rather than
                                          # share count. None (default) means
                                          # disabled — every EXISTING consumer
                                          # of this shared dataclass (backtest.py,
                                          # blended_strategy.py's per-sleeve
                                          # share caps) is unaffected unless it
                                          # explicitly opts in. order_manager.py
                                          # is the one caller that does (see its
                                          # --max-position-notional flag)
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
                 price_e4: int, qty_override: int | None = None
                 ) -> tuple[bool, str, int]:
        """Return (allowed, reason, qty). Pure; no side effects.

        qty_override: v3.38 — use this quantity instead of
        lim.order_qty for a BUY (e.g. risk-based position sizing from
        position_risk.py). Deliberately NOT a bypass: every existing
        safety check below (max_shares, max_position_notional,
        max_notional) still runs against whatever quantity is actually
        intended, override or not — the point of threading it through
        evaluate() rather than substituting qty after the fact is that
        a risk-sized quantity larger than the default order_qty must
        still be caught by these same caps, not silently exceed them.
        """
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
        # v3.55: `0 <= gap` matters. A NEGATIVE gap means last_order_t sits
        # in the future relative to this policy's clock, and the old test
        # blocked on it forever -- every signal, for the entire run, with
        # no way out. It happens whenever the two clocks disagree: the
        # audit log stamps fills with WALL-CLOCK time, while a backtest
        # runs on a historical clock at the tick timestamps. Restoring
        # "today's" fills into a replay of last year therefore set
        # last_order_t about a year AHEAD of now, and the cooldown never
        # expired. Observed on real runs: 1,247,326 signals, 1,247,326
        # blocked, 100% cooldown, zero fills -- while the reported P&L
        # and trip count were just the restored numbers from the previous
        # run, unchanged. A future timestamp is nonsense in any context,
        # so treat the cooldown as satisfied rather than trusting it.
        if self.last_order_t and 0 <= gap < lim.cooldown_s:
            return False, f"cooldown ({gap:.1f}s < {lim.cooldown_s}s)", 0

        if side == SIDE_BUY:
            qty = qty_override if qty_override is not None else lim.order_qty
            trims = []

            # v3.47: the caps now TRIM the order rather than reject it.
            # A risk-sized quantity that overshoots a cap is a reason to
            # buy LESS, not a reason to skip the trade -- rejecting was
            # why a full year of real SPY replay produced ZERO trades at
            # every strategy, and why five live symbols sat blocked all
            # morning. Only a cap with no room left for even one share
            # still blocks.
            if qty * price_e4 > lim.max_notional_e4:
                qty = lim.max_notional_e4 // price_e4
                if qty <= 0:
                    return False, (f"one share at "
                                   f"{dollars(price_e4):.2f} already "
                                   f"exceeds the "
                                   f"{dollars(lim.max_notional_e4):.2f} "
                                   f"order cap"), 0
                trims.append(f"order cap "
                             f"{dollars(lim.max_notional_e4):.0f}")

            if lim.max_position_notional_e4 is not None:
                room_e4 = (lim.max_position_notional_e4
                           - position_qty * price_e4)
                if room_e4 < price_e4:
                    return False, (f"position already at its "
                                   f"{dollars(lim.max_position_notional_e4):.2f}"
                                   f" cap -- no room for another share"), 0
                if qty * price_e4 > room_e4:
                    qty = room_e4 // price_e4
                    trims.append(f"position cap "
                                 f"{dollars(lim.max_position_notional_e4):.0f}")

            if position_qty + qty > lim.max_shares:
                qty = lim.max_shares - position_qty
                if qty <= 0:
                    return False, (f"already at max_shares "
                                   f"({lim.max_shares})"), 0
                trims.append(f"max_shares {lim.max_shares}")

            return True, ("ok" if not trims
                          else "ok (trimmed to fit " + ", ".join(trims)
                               + ")"), qty

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
                 killfile: str = "om.kill",
                 risk_overlay=None, vwap_models: dict | None = None,
                 restore_state: bool = True,
                 audit_blocked: bool = True, audit_flush: bool = True):
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
        # v3.53: realized P&L of the most recent SELL fill,
        # in e4. None after a buy. Read by the dashboard so a
        # signal row can show what the trip actually made.
        self.last_trade_pnl_e4 = None
        # v3.54: shares in the most recent fill. The FILLED quantity,
        # not the requested one -- with the caps trimming rather than
        # rejecting, those two routinely differ and only the filled
        # number reflects what actually happened.
        self.last_fill_qty = None
        # v3.59: set by main() to the dashboard's on_signal. _apply_fill
        # is reached ONLY from _resolve_pending -- the late/partial
        # reconciliation of an order that did not confirm inside the
        # poll window -- and that path books the fill directly, so it
        # never touched the GUI. A real session: an RKLB sell for
        # +$15.99 filled 3 minutes after submission, was reconciled on
        # the next signal, and appeared nowhere on screen. Worse than a
        # missing row: the session-P&L column includes it, so the
        # visible rows stopped reconciling against the running total.
        self.on_late_fill = None
        # v3.50: symbol -> {order_id, qty, side, price_e4, t}
        # for orders that were submitted but never confirmed
        # filled. Kept OUT of self.positions on purpose: the
        # shares do not exist until the broker says so.
        self.pending_orders: dict[str, dict] = {}
        self.orders = 0
        self.blocked = 0
        self.costs = CostTracker()
        self._audit_f = open(audit_path, "a")
        # v3.56: two audit costs a BACKTEST has no use for.
        #
        # audit_blocked -- every blocked signal writes a record, and
        # blocked counts run to millions on a full-year replay (one real
        # run logged 5.1M). Scaled from a 1M-tick slice, a year of GOOGL
        # writes ~730,000 records of which ~99% are "cooldown" -- noise
        # nobody will ever read. Nothing reads blocked events back:
        # _load_fills_split_by_today() only looks at order_filled, the
        # scored-signal restore only at scored_signal, and every block
        # counter in the report is in-memory. So this is pure write-only
        # I/O.
        #
        # audit_flush -- flushing after EVERY record is a syscall per
        # event. That is the right trade live, where the audit log is the
        # cost-basis record of real money and a crash must not lose it.
        # A backtest's audit file is disposable, regenerated by the next
        # run, so paying for durability buys nothing.
        #
        # Both stay True by default: live sessions are unchanged.
        self._audit_blocked = audit_blocked
        self._audit_flush = audit_flush
        # v3.38: opt-in host-side risk overlay (position_risk.py) --
        # None (the default) means every existing caller/test is
        # completely unaffected; main() wires this up explicitly only
        # for --strategy vwap_bounce sessions. vwap_models is
        # Bridge.models["vwap_bounce"] (set after Bridge exists, since
        # OrderManager is constructed before it) -- needed to read the
        # live, already-verified VWAPMirror's sigma at position-open
        # time for stop placement and risk sizing.
        self.risk_overlay = risk_overlay
        self.vwap_models = vwap_models or {}

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
        if not restore_state:
            # v3.55: a BACKTEST must be reproducible and independent of
            # whatever ran before it. Inheriting a previous run's P&L,
            # trip count and daily order count made consecutive runs
            # report the earlier run's numbers unchanged -- three real
            # runs across two different symbols all reported an
            # identical 815 trips / $12,586.52, because none of them
            # traded at all. Live sessions still restore: resuming
            # today's totals across a restart is the whole point there.
            prior_fills, todays_fills = [], []
        # v3.51: when each still-open position actually opened, for the
        # risk overlay to adopt once main() has wired one up
        self.position_open_dates = _position_open_dates(prior_fills
                                                        + todays_fills)
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

        # v3.37: found the hard way -- a position moved between machines
        # (or an audit file lost/replaced) leaves the broker correctly
        # reporting shares held while the cost tracker has NO record of
        # how they were acquired. That's not a crash and not even a
        # wrong number by itself -- self.costs._entries just starts at
        # [0, 0] for that symbol, same as a symbol that's never traded
        # at all -- so a sell against it silently prices the ENTIRE
        # held quantity against whatever price happens to be recorded
        # for a LATER, unrelated buy, understating (or overstating) the
        # real P&L with no error or warning anywhere. Catch it loudly,
        # before any trading happens, rather than let it surface only
        # as a P&L number that's quietly wrong.
        for sym, qty in self.positions.items():
            if qty > 0 and self.costs._entries.get(sym, [0, 0])[0] == 0:
                print(f"[om] ⚠️  WARNING: broker reports {qty} share(s) of "
                     f"{sym} held, but {audit_path} has NO cost-basis "
                     f"history for it — P&L on this position will be "
                     f"WRONG until it's fully closed once, or you merge "
                     f"in the audit log that actually recorded how these "
                     f"shares were acquired (e.g. from another machine)")

    # ---- audit ---------------------------------------------------------------
    def _audit(self, event: str, **kw):
        if event == "blocked" and not self._audit_blocked:
            return
        self._audit_f.write(json.dumps({"t": now_us(), "event": event, **kw})
                            + "\n")
        if self._audit_flush:
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
    def adopt_open_positions(self) -> None:
        """v3.51: hand every position we are already holding to the risk
        overlay. Call once, after main() has wired self.risk_overlay up.

        on_position_opened() only ever runs on a fresh flat->non-flat
        entry inside a live session, so until now a position carried
        across a restart got NO overlay state: stop_price_e4 stayed
        None and stop_triggered() returned False on every tick, forever.
        A real 32-share SOFI holding sat that way across several
        sessions with no downside protection, and every other carried
        position was in the same state.

        The stop cannot be computed yet -- the session VWAP mirror is
        empty at startup and would give a stop of exactly 0, which can
        never trigger. It is deferred to the first warmed-up tick; see
        _commit_pending_stops().
        """
        ov = self.risk_overlay
        if ov is None:
            return
        for sym, qty in self.positions.items():
            if not qty:
                continue
            when = self.position_open_dates.get(sym)
            if when is None:
                # held, but no fill history explains it -- the same
                # situation the v3.37 cost-basis warning already flags.
                # Treat it as OLDER (today's date would tell the sell
                # gate it is a fresh scalp, which is the permissive
                # guess and the wrong one to make blind).
                when = (self.policy._now_fn().date()
                        - timedelta(days=1))
                print(f"[om] {sym}: {qty} share(s) held with no opening "
                      f"fill in the audit log — adopting as an OLDER "
                      f"position (the cautious reading; the sell gate "
                      f"will require its anchored VWAP)")
            ov.adopt_existing_position(sym, when)
            self._audit("position_adopted", symbol=sym, qty=qty,
                        opened=str(when))
            print(f"[om] adopted {qty} {sym} opened {when} into the risk "
                  f"overlay — stop pending until the session VWAP warms up")

    def _commit_pending_stops(self, sym: str) -> None:
        """v3.51: place an adopted position's deferred stop the moment
        that symbol's session VWAP has real data behind it."""
        ov = self.risk_overlay
        if ov is None or not ov.stop_is_pending(sym):
            return
        stop = ov.commit_pending_stop(sym, self.vwap_models.get(sym))
        if stop is None:
            return
        self._audit("stop_committed", symbol=sym, stop_price_e4=stop,
                    adopted=True)
        print(f"[om] {sym}: stop now armed at ${dollars(stop):.4f} "
              f"(adopted position — it had none until the session VWAP "
              f"warmed up)")

    def _resolve_pending(self, sym: str,
                         incoming_verb: str = None) -> str | None:
        """v3.50: settle a previously-unconfirmed order on `sym` before
        doing anything else with that symbol.

        Three outcomes, all of which end with nothing outstanding:
          * it filled after all -> apply it now, at the price the broker
            actually got, not the signal price we would have guessed
          * still working -> cancel it, so it cannot collide with what we
            are about to send (this is the wash-trade trigger)
          * already gone (cancelled/expired/rejected) -> just forget it
        """
        p = self.pending_orders.get(sym)
        if not p:
            return
        get_order = getattr(self.broker, "get_order", None)
        if get_order is None:                 # broker too simple to ask
            self.pending_orders.pop(sym, None)
            return
        try:
            o = get_order(p["order_id"])
        except Exception as e:                # never let reconciliation
            print(f"[om] could not check pending {sym} order "   # itself
                  f"{p['order_id']}: {e}")                       # halt us
            return
        status = (o or {}).get("status", "")
        filled_qty = int(float((o or {}).get("filled_qty") or 0))

        if status == "filled" or filled_qty >= p["qty"]:
            px = float((o or {}).get("filled_avg_price") or 0)
            fill_e4 = int(round(px * 10_000)) or p["price_e4"]
            self._apply_fill(sym, p["side"], p["qty"], fill_e4,
                             p["order_id"], late=True)
            self.pending_orders.pop(sym, None)
            return

        if status in ("canceled", "expired", "rejected"):
            self._audit("pending_resolved", symbol=sym,
                        order_id=p["order_id"], outcome=status,
                        filled_qty=filled_qty)
            print(f"[om] pending {sym} order {p['order_id']} ended "
                  f"{status} — nothing to apply")
            self.pending_orders.pop(sym, None)
            return

        # still live. Partial fills count for what actually filled.
        if filled_qty > 0:
            px = float((o or {}).get("filled_avg_price") or 0)
            fill_e4 = int(round(px * 10_000)) or p["price_e4"]
            self._apply_fill(sym, p["side"], filled_qty, fill_e4,
                             p["order_id"], late=True, partial=True)
        # v3.59: only an OPPOSITE-side order is in the way. The reason
        # for cancelling is the wash-trade rejection, and Alpaca only
        # raises that against an opposite-side working order -- so
        # cancelling a working SELL just to submit an identical SELL
        # buys nothing and actively costs. Observed live: three RKLB
        # sells for the same 6 shares, cancelled at 07:31 and 07:32
        # before the third finally filled at 07:36, because Alpaca paper
        # was taking ~30s to fill right after the open while our poll
        # window is 5s. The exit was delayed five minutes by our own
        # churn. Leave it working and decline to duplicate it instead.
        if incoming_verb is not None and incoming_verb == p["side"]:
            self._audit("pending_kept", symbol=sym,
                        order_id=p["order_id"], side=p["side"],
                        filled_qty=filled_qty, qty=p["qty"])
            print(f"[om] {sym}: a {p['side'].upper()} for {p['qty']} is "
                  f"already working ({p['order_id']}) — leaving it alone "
                  f"rather than cancelling and resubmitting the same "
                  f"order")
            return "working"

        cancel = getattr(self.broker, "cancel_order", None)
        ok = bool(cancel and cancel(p["order_id"]))
        self._audit("pending_resolved", symbol=sym, order_id=p["order_id"],
                    outcome="cancelled" if ok else "cancel_failed",
                    filled_qty=filled_qty)
        print(f"[om] pending {sym} order {p['order_id']} was still open "
              f"({filled_qty}/{p['qty']} filled) — "
              f"{'cancelled' if ok else 'COULD NOT CANCEL'} before "
              f"sending the next order")
        self.pending_orders.pop(sym, None)

    def _apply_fill(self, sym: str, verb: str, qty: int, fill_e4: int,
                    order_id: str, late: bool = False,
                    partial: bool = False) -> None:
        """Book a fill that really happened: position, cost basis, audit."""
        self.positions[sym] = self.positions.get(sym, 0) + \
            (qty if verb == "buy" else -qty)
        _pnl_before = self.costs.realized_pnl_e4
        fees = self.costs.on_fill(verb, qty, fill_e4, sym)
        trade_pnl_e4 = (self.costs.realized_pnl_e4 - _pnl_before
                        if verb == "sell" else None)
        self.last_trade_pnl_e4 = trade_pnl_e4
        self.last_fill_qty = qty
        self._audit("order_filled", symbol=sym, side=verb, qty=qty,
                    trade_pnl_e4=trade_pnl_e4,
                    fill_price_e4=fill_e4, order_id=order_id,
                    position_qty=self.positions[sym], fees=fees,
                    realized_pnl_e4=self.costs.realized_pnl_e4,
                    late=late, partial=partial)
        fee_str = f"  fees ${fees['total']:.2f}" if fees else ""
        tag = " (late" + (", partial)" if partial else ")") if late else ""
        pnl_str = ""
        if trade_pnl_e4 is not None:
            pnl_str = (f"  {'PROFIT' if trade_pnl_e4 >= 0 else 'LOSS'} "
                       f"${dollars(trade_pnl_e4):+,.2f}")
        print(f"[om] FILLED{tag} {verb.upper()} {qty} {sym} @ "
              f"${dollars(fill_e4):.4f}  -> position "
              f"{self.positions[sym]}{fee_str}{pnl_str}")
        if self.on_late_fill:
            self.on_late_fill(
                {"side": SIDE_BUY if verb == "buy" else SIDE_SELL,
                 "price_e4": fill_e4, "symbol": sym,
                 "strategy": "vwap_bounce"},
                "FILLED", trade_pnl_e4=trade_pnl_e4, fill_qty=qty,
                reason="late fill" + (" (partial)" if partial else ""))

    _WASH_MARKERS = ("wash trade", "opposite side")

    def _submit_with_wash_recovery(self, sym: str, qty: int, verb: str,
                                   price_e4: int) -> dict:
        """v3.50: a wash-trade 403 is not "the broker is broken" -- it is
        "you already have a conflicting order live", which is
        self-inflicted and recoverable. Alpaca even hands back the
        offending existing_order_id. Cancel that order and retry ONCE;
        only if the retry also fails does it count as a real rejection
        toward the kill switch.

        Without this, three identical wash-trade rejections in one second
        looked like a broker meltdown and halted the session -- three
        times in this project's history, always minutes after the open.
        """
        try:
            return self.broker.submit_market_order(sym, qty, verb, price_e4)
        except BrokerError as e:
            msg = str(e)
            if not any(m in msg.lower() for m in self._WASH_MARKERS):
                raise
            oid = None
            m = re.search(r'"existing_order_id"\s*:\s*"([^"]+)"', msg)
            if m:
                oid = m.group(1)
            cancel = getattr(self.broker, "cancel_order", None)
            cancelled = False
            if oid and cancel:
                try:
                    cancelled = bool(cancel(oid))
                except Exception:
                    cancelled = False
            self._audit("wash_recovery", symbol=sym, side=verb, qty=qty,
                        existing_order_id=oid, cancelled=cancelled)
            _outcome = ("cancelled it, retrying once" if cancelled
                        else "could not cancel it")
            print(f"[om] {sym} {verb.upper()} hit a wash-trade rejection "
                  f"against order {oid or '(id not reported)'} — "
                  f"{_outcome}")
            if not cancelled:
                raise
            return self.broker.submit_market_order(sym, qty, verb, price_e4)

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

        # v3.50: settle any unconfirmed order on this symbol BEFORE the
        # risk gate runs, not just before submitting. If it filled late,
        # self.positions is wrong until we book it -- and a stale zero
        # would get a perfectly legitimate SELL blocked as "flat
        # (long-only: nothing to sell)", leaving a real position with no
        # way out. Gating has to see the true position.
        if self.pending_orders.get(sym):
            _pend = self._resolve_pending(
                sym, incoming_verb=("buy" if side == SIDE_BUY else "sell"))
            if _pend == "working":
                # v3.59: an identical-side order is already live at the
                # broker. Submitting a second one would double the
                # intended size, and cancelling the first to resubmit the
                # same thing is pure churn -- which is exactly what
                # delayed a real RKLB exit by five minutes.
                self.blocked += 1
                reason = ("an order on this symbol is already working at "
                          "the broker (same side) -- not duplicating it")
                self._audit("blocked", reason=reason, symbol=sym,
                            side=side, price_e4=price_e4,
                            position_qty=self.positions[sym])
                print(f"[om] blocked {sym} "
                      f"{'BUY' if side == SIDE_BUY else 'SELL'}: {reason}")
                return f"blocked: {reason}"

        # v3.38: the risk overlay only applies to the vwap_bounce
        # strategy (stop/anchor concepts are inherently VWAP-based;
        # SMA/EMA have no natural sigma the same way) and only when
        # main() has actually wired one up -- self.risk_overlay is
        # None for every existing caller/test, making all of this a
        # pure no-op unless explicitly enabled.
        overlay = (self.risk_overlay
                  if self.risk_overlay is not None
                  and fr.get("strategy") == "vwap_bounce" else None)
        is_fresh_entry = self.positions[sym] == 0
        qty_override = None

        if overlay is not None and side == SIDE_SELL:
            stopped = overlay.stop_triggered(sym, price_e4)
            if not stopped and not overlay.sell_allowed(
                    sym, price_e4, datetime.now(ET).date()):
                self.blocked += 1
                av = overlay.anchored_vwap_e4(sym)
                reason = (f"held for an older position: price "
                         f"{dollars(price_e4):.2f} still below its own "
                         f"anchored VWAP {dollars(av):.2f}"
                         if av is not None else
                         "held for an older position (anchored VWAP "
                         "not yet established)")
                self._audit("blocked", reason=reason, symbol=sym,
                            side=side, price_e4=price_e4,
                            position_qty=self.positions[sym])
                print(f"[om] blocked {sym} SELL: {reason}")
                return f"blocked: {reason}"

        if overlay is not None and side == SIDE_BUY:
            if is_fresh_entry:
                stop_e4 = overlay.peek_stop_price_e4(self.vwap_models.get(sym))
                qty_override = overlay.risk_sized_qty(price_e4, stop_e4)
            else:
                # A pyramiding add onto an ALREADY-open position. Still
                # sized against the ORIGINAL committed stop (v3.43's
                # reasoning holds and is unchanged: never
                # peek_stop_price_e4 here, which would recompute from
                # the CURRENT session VWAP and let the stop drift down
                # with a declining market -- exactly what a fixed stop
                # exists to prevent).
                #
                # v3.47: what IS gone is v3.43's total-risk budget,
                # which compared the blended cost of shares already
                # held against that stop and BLOCKED the add outright
                # once the budget was spent. Two reasons it had to go.
                # It made pyramiding almost unreachable by design (a
                # fresh entry consumed most of the budget), and in
                # practice it was the single most common block in a
                # real session -- every mid-session symbol this
                # afternoon died on "position already at its risk
                # budget". Position exposure is now bounded by
                # max_position_notional_e4 in evaluate() instead, which
                # TRIMS to fit rather than rejecting, so an add still
                # happens, just smaller as the position fills up. Each
                # add is independently sized to risk_dollars_per_trade
                # against the real stop that will actually execute;
                # total risk at a full position is bounded by
                # 2 * position_cap * sigma%, not by a separate budget.
                existing_stop_e4 = overlay.stop_price_e4(sym)
                if existing_stop_e4 is not None:
                    qty_override = overlay.risk_sized_qty(
                        price_e4, existing_stop_e4)
                # else: no committed stop on record for this symbol
                # (e.g. a position reconciled from the broker at
                # startup rather than opened through this session) --
                # qty_override stays None, falling back to the fixed
                # --qty default rather than risk-sizing against an
                # unknown stop

        allowed, reason, qty = self.policy.evaluate(
            side, self.positions[sym], price_e4, qty_override=qty_override)
        if allowed and reason != "ok" and qty_override is not None:
            # v3.47: a cap reduced the risk-sized quantity. Say so --
            # silently filling a smaller order than the risk model
            # asked for is exactly the kind of thing that should be
            # visible in the log and the audit trail, not inferred
            # later from a share count that looks surprising.
            self._audit("trimmed", symbol=sym, side=side, price_e4=price_e4,
                        wanted_qty=qty_override, filled_qty=qty,
                        reason=reason)
            print(f"[om] {sym} BUY sized {qty_override} -> {qty} "
                  f"({reason[4:-1] if reason.startswith('ok (') else reason})")
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
            fill = self._submit_with_wash_recovery(sym, qty, verb, price_e4)
        except OrderPending as p:
            # NOT a fill and NOT a rejection. Record it, leave the
            # position and the cost basis alone, and start the cooldown
            # so the strategy does not immediately fire again into an
            # order that is still working.
            self.pending_orders[sym] = {"order_id": p.order_id, "qty": p.qty,
                                        "side": p.side,
                                        "price_e4": price_e4,
                                        "t": now_us()}
            self.policy.record_order()
            self._audit("order_pending", symbol=sym, side=verb, qty=qty,
                        price_e4=price_e4, order_id=p.order_id)
            print(f"[om] {sym} {verb.upper()} {qty} PENDING (order "
                  f"{p.order_id} still open) — position unchanged; it "
                  f"will be reconciled before the next order on {sym}")
            return f"pending: {p.order_id}"
        except BrokerError as e:
            self.consecutive_rejects += 1
            # v3.50: a rejection now starts the cooldown too. record_order()
            # used to run ONLY on the success path, so a rejected order left
            # last_order_t untouched and the very next tick retried
            # immediately -- three identical rejections inside one second,
            # the entire kill-switch budget spent on one conflict while a
            # 60s cooldown was configured and doing nothing.
            self.policy.record_order()
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
        # v3.53: realized P&L for THIS fill, as the delta in the running
        # total across it. A sell is the only thing that realizes
        # anything, and the delta is the trip's own result rather than
        # the session's cumulative figure -- which is what you actually
        # want to see the moment a position closes.
        _pnl_before = self.costs.realized_pnl_e4
        fees = self.costs.on_fill(verb, qty, fill["fill_price_e4"], sym)
        trade_pnl_e4 = (self.costs.realized_pnl_e4 - _pnl_before
                        if verb == "sell" else None)
        self.last_trade_pnl_e4 = trade_pnl_e4
        self.last_fill_qty = qty
        self._audit("order_filled", **fill,
                    position_qty=self.positions[sym], fees=fees,
                    trade_pnl_e4=trade_pnl_e4,
                    realized_pnl_e4=self.costs.realized_pnl_e4)
        fee_str = f"  fees ${fees['total']:.2f}" if fees else ""
        pnl_str = ""
        if trade_pnl_e4 is not None:
            pnl_str = (f"  {'PROFIT' if trade_pnl_e4 >= 0 else 'LOSS'} "
                       f"${dollars(trade_pnl_e4):+,.2f}")
        print(f"[om] FILLED {verb.upper()} {qty} {sym} @ "
              f"${dollars(fill['fill_price_e4']):.4f}  "
              f"-> position {self.positions[sym]}{fee_str}{pnl_str}")
        if overlay is not None:
            if verb == "buy" and is_fresh_entry:
                overlay.on_position_opened(sym, datetime.now(ET).date(),
                                           self.vwap_models.get(sym))
            elif verb == "sell" and self.positions[sym] == 0:
                overlay.on_position_closed(sym)
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
        self._audit_f.flush()     # v3.56: with audit_flush=False the tail
        self._audit_f.close()     # is still buffered -- never lose it


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


def check_stale_open_orders(broker, cancel: bool):
    """v3.41: see the call site's comment for the full incident this
    fixes (a stale open order surviving between sessions caused three
    straight 'wash trade' rejections, which tripped the kill switch).

    MockBroker has no open-orders concept at all -- everything fills
    instantly -- so this checks for the method rather than the broker
    TYPE, making it a clean no-op for MockBroker (and any future
    broker that doesn't carry the concept) without needing a broker-
    type check that would need updating every time a new broker class
    is added.
    """
    if not hasattr(broker, "list_open_orders"):
        return
    try:
        open_orders = broker.list_open_orders()
    except BrokerError as e:
        print(f"[om] WARNING: couldn't check for stale open orders: {e}")
        return
    if not open_orders:
        return

    by_symbol: dict[str, int] = {}
    for o in open_orders:
        sym = o.get("symbol", "?")
        by_symbol[sym] = by_symbol.get(sym, 0) + 1
    summary = ", ".join(f"{sym} x{n}" for sym, n in sorted(by_symbol.items()))

    if cancel:
        print(f"[om] {len(open_orders)} stale open order(s) found "
             f"({summary}) — cancelling before trading begins "
             f"(--cancel-stale-orders)")
        try:
            results = broker.cancel_all_open_orders()
            print(f"[om] cancelled {len(results)} order(s)")
        except BrokerError as e:
            print(f"[om] WARNING: failed to cancel stale open orders: "
                 f"{e} — they may still cause 'wash trade' rejections")
    else:
        print(f"[om] WARNING: {len(open_orders)} stale open order(s) "
             f"found on the account ({summary}) — possibly left over "
             f"from a prior session (e.g. a disconnect before a fill "
             f"confirmed). They may cause 'wash trade' rejections the "
             f"moment this session trades the same symbol. Pass "
             f"--cancel-stale-orders to clear them automatically at "
             f"startup, or cancel manually via Alpaca's dashboard/API "
             f"first")


def main():
    # v3.46: feeds come from feeds.py now, and Bridge is imported
    # LAZILY, only if --port is actually passed. bridge.py hard-exits
    # at import time when pyserial is missing, so deferring it means
    # the default (no-hardware) path has no serial dependency at all.
    from feeds import run_sim, run_alpaca, run_historical
    from tick_protocol import install_local_timestamps

    ap = argparse.ArgumentParser(
        description="FPGA signal -> risk-checked paper order")
    ap.add_argument("--port", default=None,
                    help="v3.46: NO LONGER REQUIRED. Omit it (the new "
                        "default) to run the direct in-process engine "
                        "-- ticks go straight into the same SMA/EMA/"
                        "VWAP mirror models, no serial port, no pty, "
                        "no wire protocol, no emulator process, one "
                        "terminal. Pass a port to drive real hardware "
                        "(or fpga_emulator.py) over UART through "
                        "bridge.py exactly as before, including the "
                        "fabric-vs-mirror signal verification, which "
                        "is only meaningful against real silicon")
    ap.add_argument("--no-timestamps", action="store_true",
                    help="v3.41: disable the local HH:MM:SS timestamp "
                        "prefix normally added to every printed line "
                        "-- e.g. if you're piping output somewhere "
                        "that already timestamps for you")
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
    ap.add_argument("--force-vwap-reset", action="store_true",
                    help="v3.38: reset the VWAP session even if one's "
                         "already been recorded today — use this if "
                         "the board/emulator itself was ALSO restarted "
                         "since the day's earlier reset (not just "
                         "order_manager.py), since in that case its "
                         "real accumulated state is already gone and "
                         "resetting both sides together again is "
                         "correct, the same way every restart used to "
                         "behave before this flag existed")
    ap.add_argument("--stop-sigma-mult", type=float, default=0.0,
                    help="v3.38: --strategy vwap_bounce only. Stop-loss "
                         "placed at this many standard deviations below "
                         "session VWAP, fixed at the moment a position "
                         "opens (does not move afterward). Default 0 "
                         "DISABLES the stop-loss and anchored-VWAP gate "
                         "entirely -- this is new, real-money-affecting "
                         "behavior, so it's explicit opt-in rather than "
                         "silently active for every existing vwap_bounce "
                         "session; pass e.g. 3.0 to enable it")
    ap.add_argument("--anchor-gate-tolerance", type=float, default=0.0,
                    help="v3.38: --strategy vwap_bounce only. How far "
                         "BELOW a position's own anchored VWAP still "
                         "counts as close enough to allow an OLDER "
                         "(opened on an earlier day) position to exit "
                         "— e.g. 0.01 allows exiting up to 1%% below "
                         "it. Default 0.0 requires price at or above "
                         "the anchored VWAP exactly. Same-day positions "
                         "are never gated by this at all")
    ap.add_argument("--risk-per-trade", type=float, default=500.0,
                    help="v3.38: --strategy vwap_bounce only. Dollars "
                         "at risk per NEW position (if the stop-loss "
                         "is hit, this is roughly what that trade "
                         "costs) — position size is computed from "
                         "this divided by the distance to the stop, "
                         "not a fixed share count. Still capped by "
                         "--max-shares/--max-notional/"
                         "--max-position-notional same as any buy")
    ap.add_argument("--strategy", choices=["sma", "ema", "vwap_bounce"],
                    default="sma",
                    help="which engine's signals TRADE; the others are "
                         "scored hypothetically for comparison. "
                         "'vwap_bounce' here means the FABRIC-verified "
                         "VWAP engine (wire 0x85, requires a v3.18+ "
                         "bitstream) -- a DIFFERENT thing from the "
                         "separate --vwap-bounce flag below, which adds "
                         "an always-score-only HOST-computed VWAP row "
                         "for comparison and can be used alongside "
                         "--strategy vwap_bounce or any other choice")
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
    ap.add_argument("--source", choices=["sim", "alpaca", "historical"],
                    default="sim",
                    help="'historical' replays REAL market trades from "
                         "--trades against the board — the bring-up step "
                         "after --selftest passes, before any live "
                         "session trusts a fabric signal")
    ap.add_argument("--relay-url", default=None,
                    help="--source alpaca: connect to a local "
                        "alpaca_relay.py instance instead of Alpaca "
                        "directly, e.g. ws://localhost:8765 — use this "
                        "when running alongside another project that "
                        "also wants live prices at the same time (only "
                        "one direct connection allowed per Alpaca login)")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--rate", type=float, default=10.0)
    ap.add_argument("--start-price", type=float, default=500.0)
    ap.add_argument("--trades", default=None,
                    help="one or more JSONL files from "
                         "fetch_historical_trades.py, comma-separated in "
                         "chronological order (e.g. widening date ranges: "
                         "SPY_2026-01-01_2026-04-01.trades.jsonl,"
                         "SPY_2026-04-01_2026-07-01.trades.jsonl) — "
                         "required for --source historical, single "
                         "symbol only (one file = one symbol)")
    ap.add_argument("--replay-rate", type=float, default=200.0,
                    help="--source historical: ticks/sec cap (real "
                         "recorded gaps between ticks are NOT replayed — "
                         "this paces by rate alone, same as --source "
                         "sim; see run_historical()'s docstring)")
    ap.add_argument("--replay-max", type=int, default=20_000,
                    help="--source historical: stop after this many "
                         "trades (files can be hundreds of millions of "
                         "trades; 0 or negative means no cap — use with "
                         "real caution, could run for many hours)")
    ap.add_argument("--broker", choices=["mock", "alpaca"], default="mock")
    ap.add_argument("--cancel-stale-orders", action="store_true",
                    help="v3.41: --broker alpaca only. At startup, if "
                        "any open orders are already sitting on the "
                        "account (e.g. left over from a prior session "
                        "that disconnected before a fill confirmed), "
                        "cancel all of them automatically before "
                        "trading begins. Without this flag, stale open "
                        "orders are only WARNED about, not touched -- "
                        "they'll likely cause 'wash trade' rejections "
                        "the moment this session tries to trade the "
                        "same symbol, since Alpaca refuses an order "
                        "when an opposite-side order already exists. "
                        "Off by default since cancelling is a "
                        "destructive action on orders this specific "
                        "session didn't necessarily place itself")
    ap.add_argument("--live", action="store_true",
                    help="REAL MONEY. Requires --broker alpaca plus the full "
                         "interlock chain (see arm_live_trading)")
    ap.add_argument("--max-daily-loss", type=float, default=None,
                    help="$ realized loss that halts the session "
                         "(MANDATORY in --live)")
    ap.add_argument("--qty", type=int, default=5,
                    help="shares bought per entry signal (default 5)")
    ap.add_argument("--max-position-notional", type=float, default=10_000.0,
                    help="$ cap on the TOTAL value of an open position "
                         "(existing shares + this buy, at the current "
                         "price) — default $10,000. v3.27: replaces the "
                         "old share-count position cap with a dollar-"
                         "EXPOSURE cap instead; sizing by dollar risk "
                         "rather than share count. The underlying "
                         "max_shares mechanism still exists in RiskLimits "
                         "(backtest.py and the blend strategy's per-sleeve "
                         "caps still use it for their own purposes) but "
                         "order_manager.py no longer exposes it as a CLI "
                         "flag or lets it meaningfully constrain a live/"
                         "paper session — this dollar cap is what governs "
                         "here now")
    ap.add_argument("--max-notional", type=float, default=3000.0,
                    help="$ cap on any SINGLE buy order (qty x price) — "
                         "independent of --max-position-notional above, "
                         "which caps the total position, not one order")
    ap.add_argument("--max-orders-per-day", type=int, default=1000)
    ap.add_argument("--cooldown", type=float, default=60.0)
    ap.add_argument("--ignore-market-hours", action="store_true",
                    help="for mock/off-hours testing")
    ap.add_argument("--audit", default="om_audit.jsonl")
    ap.add_argument("--killfile", default="om.kill",
                    help="v3.42: path to the kill-switch marker file. "
                        "Was previously hardcoded to 'om.kill' in the "
                        "current directory with NO way to override it "
                        "from the CLI at all -- meaning every session "
                        "run from the same directory (a real trading "
                        "session, a test run, a second strategy) shared "
                        "the exact same kill-switch state, with no way "
                        "to isolate them. Same default as before, for "
                        "backward compatibility -- just now overridable")
    ap.add_argument("--household-income", type=float, default=None,
                    help="taxable household income for the tax estimate "
                         "(use --gross if you're giving gross income)")
    ap.add_argument("--filing-status", choices=["single", "mfj"],
                    default="mfj")
    ap.add_argument("--state-rate", type=float, default=4.40,
                    help="flat state income tax %% (default: Colorado 4.40)")
    ap.add_argument("--report-all-strategies", action="store_true",
                    help="v3.52: restore the full multi-strategy console "
                        "output. By default a live session reports only "
                        "the strategy it actually trades -- the others "
                        "are still scored, audited and restored across "
                        "restarts, they just don't narrate signals the "
                        "session will never act on. Pass this to see how "
                        "the untraded strategies would have done")
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
    if not args.no_timestamps:
        install_local_timestamps()

    # v3.47: fail fast on a missing feed dependency. run_alpaca does its
    # own import check, but that only runs AFTER cost-basis replay, VWAP
    # mirror rebuild, scored-signal restore and dashboard startup -- on a
    # real session that is a couple of seconds of work and a screen of
    # output before a one-line "you need websocket-client". Worse, until
    # v3.46 order_manager.py imported bridge.py unconditionally, and
    # bridge.py hard-exits at import time without pyserial, so an
    # un-activated venv used to be caught instantly by that accidental
    # tripwire. Making the bridge import lazy (correctly -- the direct
    # engine needs no serial library) removed it, so check explicitly
    # here instead.
    if args.source == "alpaca":
        try:
            import websocket            # noqa: F401  (websocket-client)
        except ImportError:
            sys.exit("[om] --source alpaca needs websocket-client:\n"
                     "       pip3 install websocket-client "
                     "--break-system-packages\n"
                     "     (if you use a virtualenv, check it's active "
                     "first -- e.g. source ~/fpga-venv/bin/activate)")

    if args.selftest:
        # hardware acceptance test — real board, no broker, no
        # trading, no dashboard, no OrderManager: none of that setup
        # below is needed, so this exits BEFORE any of it runs (a
        # --broker alpaca selftest shouldn't need ALPACA_KEY set, and
        # a --live selftest should never arm live trading at all).
        # run_selftest() prints its own PASS/DIAG/FAIL lines.
        symbols = [t for t in args.symbols.split(",") if t.strip()]
        if not args.port:
            print("[om] --selftest needs --port: it exists to verify a "
                  "real device's signals against the host mirror models, "
                  "which is only meaningful when something independent is "
                  "actually computing them. The default direct engine IS "
                  "the mirror models, so there is nothing to compare it "
                  "against.")
            sys.exit(2)
        from bridge import Bridge, run_selftest   # hardware-only path
        br = Bridge(args.port, symbols, args.fast, args.slow,
                    ema_kf=args.ema_kf, ema_ks=args.ema_ks,
                    baud=args.baud, vwap_warmup=args.vwap_warmup,
                    vwap_k2_q8=args.vwap_k2_q8)
        run_selftest(br)
        br.close()
        return

    from compare import normalize_max_hold_days
    pg_max_hold = normalize_max_hold_days(args.pg_max_hold_days)

    limits = RiskLimits(order_qty=args.qty,
                        # max_shares itself still exists on RiskLimits and
                        # is still enforced by RiskPolicy.evaluate() (see
                        # the dataclass's own comment — backtest.py and the
                        # blend strategy's per-sleeve caps depend on it for
                        # their own purposes) — but order_manager.py's live/
                        # paper sessions are sized by dollar exposure now,
                        # not share count, so this is set high enough to
                        # never be the binding constraint here; the real
                        # limit is max_position_notional_e4 below
                        max_shares=10**9,
                        max_notional_e4=int(args.max_notional * 10_000),
                        max_position_notional_e4=int(
                            args.max_position_notional * 10_000),
                        max_orders_per_day=args.max_orders_per_day,
                        cooldown_s=args.cooldown,
                        require_market_hours=(args.live or
                                              (args.broker == "alpaca"
                                               and not args.ignore_market_hours)),
                        max_daily_loss=args.max_daily_loss)

    if args.source == "historical" and args.live:
        sys.exit("--source historical replays ALREADY-HAPPENED market "
                 "data — combining it with --live would place REAL "
                 "trades on stale prices. Use --broker mock (the "
                 "default) or --broker alpaca without --live for "
                 "historical replay; --live is for --source alpaca only.")

    if args.live:
        if args.broker != "alpaca":
            sys.exit("--live requires --broker alpaca")
        broker = arm_live_trading(args.symbols.split(",")[0].strip().upper(),
                                  limits, strategy=args.strategy)
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

    # v3.41: catch orders left open from a PRIOR session (e.g. a fill
    # that never confirmed before a disconnect, or a real crash) before
    # they cause "wash trade" rejections -- found from a real incident:
    # a stale open order sat on the account, and three straight
    # rejections against it tripped the kill switch on an otherwise-
    # healthy session. MockBroker has no concept of open orders at all
    # (fills instantly), so this only applies to a real Alpaca broker.
    check_stale_open_orders(broker, args.cancel_stale_orders)

    symbols = [t for t in args.symbols.split(",") if t.strip()]
    om = OrderManager(broker, symbols, limits, audit_path=args.audit,
                      killfile=args.killfile)

    from compare import StrategyScorecard, comparison_report
    # v3.46: two interchangeable engines behind one duck-typed
    # interface. Without --port, the direct in-process TickEngine --
    # no serial, no pty, no framing, no emulator, no second terminal.
    # With --port, the original Bridge over UART, unchanged, for real
    # hardware. Everything downstream (callbacks, feeds, dashboard)
    # treats them identically.
    if args.port:
        from bridge import Bridge
        br = Bridge(args.port, symbols, args.fast, args.slow,
                    ema_kf=args.ema_kf, ema_ks=args.ema_ks, baud=args.baud,
                    log_path=args.log, verify_grace_s=args.verify_grace_s,
                    vwap_warmup=args.vwap_warmup, vwap_k2_q8=args.vwap_k2_q8)
    else:
        from tick_engine import TickEngine
        _eng_log = open(args.log, "a") if args.log else None
        br = TickEngine(symbols, args.fast, args.slow,
                        k_fast=args.ema_kf, k_slow=args.ema_ks,
                        vwap_warmup=args.vwap_warmup,
                        vwap_k2_q8=args.vwap_k2_q8, log=_eng_log)
        print("[om] direct engine (no --port): ticks go straight into "
              "the mirror models")
    # v3.52: only the strategy that actually TRADES reports its signals
    # to the console. The others are still scored, still audited and
    # still restored across restarts -- they just stop narrating
    # crossings the session will never act on. Same knob on both
    # engines, so --port and the direct path print the same thing.
    if not args.report_all_strategies:
        br.report_strategies = {args.strategy}

    # v3.19: command the fabric VWAP session boundary at startup.
    # v3.38: NOT unconditionally anymore. The original reasoning was
    # sound for what it was solving (host mirrors start empty every
    # restart, so both sides must be zeroed together or the verifier
    # would flag divergences that are really just mismatched session
    # baselines) but it had a real cost: restarting order_manager.py
    # mid-day for any reason (a crash, routine troubleshooting) wiped
    # out the WHOLE day's VWAP and restarted it from that moment,
    # rather than truly reflecting "since market open" the way a
    # session VWAP is supposed to. Found from a real overnight session
    # that needed several restarts before market open the next
    # morning, unintentionally treating the eventual market-open
    # restart as a brand new (very late) session start.
    #
    # Now: reset only on the FIRST startup of a new trading day (ET),
    # tracked via the audit log, same persistence pattern already used
    # for cost basis and order counts. A same-day restart skips the
    # reset — trusting that the board/emulator (left running
    # continuously, per the established two-terminal workflow) still
    # has this morning's correctly-accumulating state — and instead
    # rebuilds the HOST's own mirror (a fresh Python object every
    # restart regardless) by replaying today's ticks from --log.
    today_et = datetime.now(ET).date()
    last_sessrst_day = _load_last_sessrst_day(args.audit)
    if args.force_vwap_reset or last_sessrst_day != today_et:
        if not br.send_sessrst():
            print("[om] WARNING: VWAP session reset not acknowledged — "
                 "the link may be down; fabric VWAP state may span "
                 "sessions until a reset is acked")
        else:
            with open(args.audit, "a") as _af:
                _af.write(json.dumps({"t": now_us(),
                                      "event": "sessrst_sent"}) + "\n")
    else:
        print(f"[om] VWAP already reset today ({last_sessrst_day}) — "
             f"skipping another reset so the board/emulator's "
             f"already-accumulating state survives this restart "
             f"(pass --force-vwap-reset to reset both sides anyway, "
             f"e.g. if the board/emulator itself was also restarted)")
        if args.log:
            n_replayed = _replay_vwap_from_log(br, args.log, today_et)
            print(f"[om] rebuilt host VWAP mirror(s) from {n_replayed} "
                 f"tick(s) in {args.log} (today only)")
        else:
            print("[om] WARNING: no --log to replay from — host VWAP "
                 "mirrors start empty this restart even though the "
                 "board/emulator's own state should still be "
                 "accumulating since this morning; expect DIVERGENCE "
                 "until this naturally resolves, or pass "
                 "--force-vwap-reset to reset both sides together")

    labels = {"sma": f"SMA {args.fast}/{args.slow}",
              "ema": f"EMA 1/{1 << args.ema_kf}:1/{1 << args.ema_ks}",
              # v3.23: VWAP-FPGA is now a full peer of SMA/EMA, not a
              # special case — one shared card (positions keyed by
              # symbol internally, same as SMA/EMA already do), always
              # scored, live iff --strategy vwap_bounce. This REPLACES
              # the old per-symbol _vwap_fpga_card mechanism (below,
              # removed) that existed only because VWAP could never be
              # live before this drop.
              "vwap_bounce": "VWAP-FPGA"}
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
                      f"--ladder-baseline {sym}:<price> for --source "
                      "sim/historical")

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

    def route_to_shadow_cards(fr: dict, count: bool = True):
        """Feed a signal to whichever SCORED (non-live) cards should see
        it — the single routing rule used both for live signals
        arriving now (via on_verified, below) and for startup replay of
        today's earlier history (right below this). Keeping this in
        one place means replay can never reach a different set of
        cards than live signals do."""
        strat = fr["strategy"]
        if strat != args.strategy:
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
        # verified signals, so this replay mechanism never touches it).
        # v3.23: generalized from a hardcoded sma<->ema binary swap —
        # with vwap_bounce now a full peer, there are always TWO other
        # scored strategies to restore, not one, whichever is live.
        shadow_cards = [cards[s] for s in labels if s != args.strategy]
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
        # v3.59: late/partial fills reconciled from a pending order are
        # booked inside _apply_fill, which is nowhere near on_verified()
        # -- so they never reached the GUI. Wire them straight through.
        om.on_late_fill = dash.on_signal
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

    # v3.38: host-side risk overlay -- stop-loss, position-anchored
    # VWAP, and the same-day-vs-older sell gate. Only meaningful for
    # --strategy vwap_bounce (stop/anchor concepts are inherently
    # VWAP-based; SMA/EMA have no natural sigma the same way).
    # --stop-sigma-mult 0 disables it entirely, leaving om.risk_overlay
    # as None -- the exact same safe default as if this feature didn't
    # exist at all.
    if args.strategy == "vwap_bounce" and args.stop_sigma_mult > 0:
        from position_risk import PositionRiskOverlay
        om.risk_overlay = PositionRiskOverlay(
            stop_sigma_mult=args.stop_sigma_mult,
            anchor_gate_tolerance=args.anchor_gate_tolerance,
            risk_dollars_per_trade=args.risk_per_trade)
        om.vwap_models = br.models["vwap_bounce"]
        # v3.51: hand any position we are ALREADY holding to the overlay.
        # Must happen after vwap_models is wired, and it is what gives a
        # carried-over position a stop at all -- without it,
        # on_position_opened() never runs for those and they trade
        # unprotected for as long as they are held.
        om.adopt_open_positions()

        # v3.44: a REAL bug, found via a direct comparison against a
        # from-scratch backtest.py rewrite that was supposed to match
        # this exactly and didn't. configure_symbols()'s own docstring
        # says it plainly: "Also rebuilds the mirror models" -- it
        # replaces self.models["vwap_bounce"] with brand-new VWAPMirror
        # objects, every time it's called. And EVERY run_* function
        # (run_sim/run_historical/run_alpaca) calls configure_symbols()
        # AGAIN, internally, right here, AFTER the assignment above --
        # orphaning the dict this just captured. From that point on,
        # every stop-loss and every risk-sized entry was silently
        # computed against a permanently empty mirror (sigma reads as
        # 0), which degenerates the stop to exactly session VWAP --
        # for a bounce-buy (entry below VWAP by definition) that's a
        # negative risk-per-share, hitting the fallback that always
        # sizes to exactly 1 share. --risk-per-trade was never actually
        # being honored in ANY real session (live, sim, or historical)
        # since this feature shipped at v3.38 -- always silently 1
        # share regardless of the configured budget.
        #
        # Fixed by re-syncing through the same on_symbols_changed hook
        # configure_symbols() already calls whenever it rebuilds the
        # models -- this also covers any FUTURE reconfiguration (e.g.
        # a dynamic symbol change mid-session), not just startup.
        _prev_on_symbols_changed_risk = br.on_symbols_changed
        def _resync_vwap_models(new_symbols):
            if _prev_on_symbols_changed_risk:
                _prev_on_symbols_changed_risk(new_symbols)
            om.vwap_models = br.models["vwap_bounce"]
        br.on_symbols_changed = _resync_vwap_models

        # an independent stop-loss check on EVERY raw trade tick -- not
        # gated on a verified fabric signal arriving at all, since a
        # stop has to fire the moment it's breached, not wait for the
        # next bounce/cross event. Also feeds the anchored VWAP
        # accumulator (a no-op for any symbol without an open
        # position). Same on_echo chaining pattern as the ladder/
        # vwap_cards hooks above: adds to whatever's already
        # listening, never replaces it.
        from tick_protocol import TYPE_ECHO_TRADE as _TET_RISK
        _prev_echo_risk = br.on_echo
        def _on_echo_with_risk(fr):
            if _prev_echo_risk:
                _prev_echo_risk(fr)
            if fr["type"] != _TET_RISK:
                return
            sym = fr["symbol"].strip()
            price_e4 = fr["price_e4"]
            om.risk_overlay.on_tick(sym, price_e4, fr["qty"])
            # v3.51: an adopted position's stop is deferred until this
            # symbol's session VWAP actually has data behind it -- at
            # startup the mirror is empty and would yield a stop of 0,
            # which can never fire. Cheap: a no-op once committed.
            om._commit_pending_stops(sym)
            if (om.positions.get(sym, 0) > 0
                    and om.risk_overlay.stop_triggered(sym, price_e4)):
                _stop_fr = {"side": SIDE_SELL, "price_e4": price_e4,
                            "symbol": sym, "strategy": "vwap_bounce"}
                _stop_out = om.on_signal(_stop_fr)
                # v3.58: tell the dashboard. This path deliberately does
                # NOT go through on_verified() -- a breached stop has to
                # act on the tick that breached it, not wait for the next
                # bounce signal -- but that meant it also skipped the one
                # place dash.on_signal() is called. So a stop-triggered
                # sell filled, booked its P&L, moved the position and
                # wrote to the audit log while staying completely
                # invisible in the GUI. Found from a real session: two
                # sells (CIFR -$4.32, MARA -$8.75) were missing from the
                # FILLS box with no trace anywhere on screen. They were
                # never in SIGNALS either; the v3.57 fills log just made
                # the hole obvious by giving fills somewhere durable to
                # live.
                if dash:
                    dash.on_signal(
                        _stop_fr, _stop_out,
                        trade_pnl_e4=(om.last_trade_pnl_e4
                                      if _stop_out == "FILLED" else None),
                        fill_qty=(om.last_fill_qty
                                  if _stop_out == "FILLED" else None),
                        reason="stop-loss")
        br.on_echo = _on_echo_with_risk

    def on_verified(fr):
        strat = fr["strategy"]
        if strat == args.strategy:
            cards[strat].signals += 1     # real routing/gating/fills below
            outcome = om.on_signal(fr)
            sync_live_card(cards, args.strategy, om)  # keep dashboard fresh
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
            # v3.53: hand the dashboard the P&L this fill realized, so a
            # SELL row can show what the round trip actually made. None
            # for buys and for anything that did not fill.
            dash.on_signal(fr, outcome,
                           trade_pnl_e4=(om.last_trade_pnl_e4
                                         if outcome == "FILLED" else None),
                           fill_qty=(om.last_fill_qty
                                     if outcome == "FILLED" else None))

    def on_divergence(info):
        if dash:
            dash.on_event("DIVERGENCE: " + info.get("reason", "?"), True)
        om.on_divergence(info)

    br.on_verified = on_verified          # survives slot reconfiguration:
    br.on_divergence = on_divergence      # _build_models re-attaches these
    _others = ", ".join(s.upper() for s in labels if s != args.strategy)
    print(f"[om] trading strategy: {args.strategy.upper()} "
          f"({_others} scored in parallel, gated identically, not traded)")

    early_abort = False
    try:
        if args.source == "sim":
            run_sim(br, args.n, args.rate, args.start_price)
        elif args.source == "historical":
            if not args.trades:
                print("[om] --source historical requires --trades "
                      "PATH[,PATH...]")
                sys.exit(2)
            paths = [p.strip() for p in args.trades.split(",") if p.strip()]
            cap = args.replay_max if args.replay_max > 0 else None
            run_historical(br, paths, rate=args.replay_rate, max_trades=cap)
        else:
            run_alpaca(br, relay_url=args.relay_url)
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        # v3.35: without this, a sys.exit() from deep inside run_sim/
        # run_historical/run_alpaca (missing credentials, missing
        # dependency, a failed slot-configuration handshake, etc.) was
        # SILENTLY SWALLOWED by this function's own finally-block
        # sys.exit() below -- the real reason never printed anywhere,
        # and the resulting all-zero summary (nothing ever ran) looked
        # exactly like a normal, empty, successful session. Surface
        # the real reason, and remember an early abort happened so the
        # final exit code below reflects it honestly -- silently
        # reporting success for a session that never actually started
        # would be worse than the missing message alone.
        if e.code and not isinstance(e.code, int):
            print(f"[om] session aborted early: {e.code}")
        early_abort = True
    finally:
        ok = br.summary()
        sync_live_card(cards, args.strategy, om)  # final guarantee, even if
                                                  # nothing arrived between
                                                  # the last signal and Ctrl+C
        print(comparison_report(
            cards, live_only=not args.report_all_strategies))
        om.summary(args.household_income, args.filing_status,
                   args.state_rate, args.gross)
        br.close()
        sys.exit(0 if (ok and not om.halted and not early_abort) else 1)


if __name__ == "__main__":
    main()
