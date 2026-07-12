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
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tick_protocol import SIDE_BUY, SIDE_SELL, dollars
from costs import CostTracker

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
LIVE_ACK_PHRASE = "I-UNDERSTAND-THIS-TRADES-REAL-MONEY"
ET = ZoneInfo("America/New_York")


def now_us() -> int:
    return time.time_ns() // 1000


# ---------------------------------------------------------------------------
# Brokers
# ---------------------------------------------------------------------------
class BrokerError(Exception):
    pass


class MockBroker:
    """Instant fills at the signal price; injectable rejections for tests."""

    def __init__(self, reject_next: int = 0):
        self.position_qty = 0
        self.fills: list[dict] = []
        self.reject_next = reject_next     # tests: fail this many submissions

    def get_position_qty(self, symbol: str) -> int:
        return self.position_qty

    def submit_market_order(self, symbol: str, qty: int, side: str,
                            ref_price_e4: int) -> dict:
        if self.reject_next > 0:
            self.reject_next -= 1
            raise BrokerError("mock rejection (injected)")
        self.position_qty += qty if side == "buy" else -qty
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
    max_orders_per_day: int = 10
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
    def __init__(self, limits: RiskLimits):
        self.lim = limits
        self.orders_today = 0
        self.day = datetime.now(ET).date()
        self.last_order_t = 0.0

    def evaluate(self, side: int, position_qty: int,
                 price_e4: int) -> tuple[bool, str, int]:
        """Return (allowed, reason, qty). Pure; no side effects."""
        lim = self.lim
        today = datetime.now(ET).date()
        if today != self.day:                        # daily counter rollover
            self.day, self.orders_today = today, 0

        if lim.require_market_hours and not market_is_open():
            return False, "market closed", 0
        if self.orders_today >= lim.max_orders_per_day:
            return False, f"daily order cap ({lim.max_orders_per_day}) reached", 0
        gap = time.monotonic() - self.last_order_t
        if self.last_order_t and gap < lim.cooldown_s:
            return False, f"cooldown ({gap:.1f}s < {lim.cooldown_s}s)", 0

        if side == SIDE_BUY:
            if position_qty > 0:
                return False, "already long (no pyramiding)", 0
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
        self.last_order_t = time.monotonic()


# ---------------------------------------------------------------------------
# Order manager
# ---------------------------------------------------------------------------
class OrderManager:
    MAX_CONSECUTIVE_REJECTS = 3

    def __init__(self, broker, symbol: str, limits: RiskLimits,
                 audit_path: str = "om_audit.jsonl",
                 killfile: str = "om.kill"):
        self.broker = broker
        self.symbol = symbol.strip()
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

        # broker is the source of truth: reconcile, don't remember
        self.position_qty = self.broker.get_position_qty(self.symbol)
        self._audit("startup", position_qty=self.position_qty,
                    limits=vars(limits))
        print(f"[om] reconciled position from broker: "
              f"{self.position_qty} {self.symbol}")

    # ---- audit ---------------------------------------------------------------
    def _audit(self, event: str, **kw):
        self._audit_f.write(json.dumps({"t": now_us(), "event": event, **kw})
                            + "\n")
        self._audit_f.flush()

    # ---- kill switch -----------------------------------------------------------
    def halt(self, reason: str):
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        self._audit("KILL", reason=reason)
        with open(self.killfile, "w") as f:
            f.write(f"{datetime.now(ET).isoformat()}  {reason}\n")
        print(f"[om] *** KILL SWITCH: {reason} — no further orders; "
              f"delete '{self.killfile}' to re-arm a future session ***")

    def on_divergence(self, info: dict):
        self.halt(f"model/hardware divergence: {info.get('reason')}")

    # ---- the signal path ---------------------------------------------------------
    def on_signal(self, fr: dict):
        """Callback for VERIFIED FPGA signals (bridge SignalVerifier)."""
        side = fr["side"]
        price_e4 = fr["price_e4"]
        if self.halted:
            self.blocked += 1
            self._audit("blocked", reason=f"halted: {self.halt_reason}",
                        side=side, price_e4=price_e4)
            return

        allowed, reason, qty = self.policy.evaluate(side, self.position_qty,
                                                    price_e4)
        if not allowed:
            self.blocked += 1
            self._audit("blocked", reason=reason, side=side,
                        price_e4=price_e4, position_qty=self.position_qty)
            print(f"[om] blocked {('BUY' if side == SIDE_BUY else 'SELL')}: "
                  f"{reason}")
            return

        verb = "buy" if side == SIDE_BUY else "sell"
        self._audit("order_submit", side=verb, qty=qty, price_e4=price_e4)
        try:
            fill = self.broker.submit_market_order(self.symbol, qty, verb,
                                                   price_e4)
        except BrokerError as e:
            self.consecutive_rejects += 1
            self._audit("order_rejected", error=str(e),
                        consecutive=self.consecutive_rejects)
            print(f"[om] order rejected: {e}")
            if self.consecutive_rejects >= self.MAX_CONSECUTIVE_REJECTS:
                self.halt(f"{self.consecutive_rejects} consecutive broker "
                          "rejections")
            return

        self.consecutive_rejects = 0
        self.orders += 1
        self.policy.record_order()
        self.position_qty += qty if verb == "buy" else -qty
        fees = self.costs.on_fill(verb, qty, fill["fill_price_e4"])
        self._audit("order_filled", **fill, position_qty=self.position_qty,
                    fees=fees, realized_pnl_e4=self.costs.realized_pnl_e4)
        fee_str = f"  fees ${fees['total']:.2f}" if fees else ""
        print(f"[om] FILLED {verb.upper()} {qty} {self.symbol} @ "
              f"${dollars(fill['fill_price_e4']):.4f}  "
              f"-> position {self.position_qty}{fee_str}")
        # daily loss halt: realized net P&L breaching the bound stops the
        # session — losses can only be REALIZED on sells, so this check
        # after each fill is sufficient for a long-only strategy
        lim = self.policy.lim.max_daily_loss
        if lim and self.costs.net_pnl_usd <= -lim:
            self.halt(f"daily loss limit breached: net "
                      f"${self.costs.net_pnl_usd:+,.2f} <= -${lim:,.2f}")

    # ---- teardown -----------------------------------------------------------------
    def summary(self, household_income: float | None = None,
                filing_status: str = "mfj", state_rate_pct: float = 4.40,
                income_is_gross: bool = False):
        print("\n---- order manager summary " + "-" * 33)
        print(f"  orders filled    {self.orders}")
        print(f"  signals blocked  {self.blocked}")
        print(f"  final position   {self.position_qty} {self.symbol}"
              + ("  (open — P&L below is REALIZED only)"
                 if self.position_qty else ""))
        print(f"  kill switch      "
              f"{'TRIPPED: ' + self.halt_reason if self.halted else 'armed'}")
        print(self.costs.report(household_income, filing_status,
                                state_rate_pct, income_is_gross))
        self._audit("shutdown", orders=self.orders, blocked=self.blocked,
                    position_qty=self.position_qty, halted=self.halted,
                    total_fees=self.costs.total_fees,
                    realized_pnl_e4=self.costs.realized_pnl_e4)
        self._audit_f.close()


# ---------------------------------------------------------------------------
# Integrated CLI: bridge + order manager in one process
# ---------------------------------------------------------------------------
def main():
    from bridge import Bridge, run_sim, run_alpaca   # reuse everything

    ap = argparse.ArgumentParser(
        description="FPGA signal -> risk-checked paper order")
    ap.add_argument("--port", required=True)
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--fast", type=int, default=8)
    ap.add_argument("--slow", type=int, default=32)
    ap.add_argument("--ema-kf", type=int, default=3,
                    help="fast EMA shift of the built bitstream (alpha 2^-k)")
    ap.add_argument("--ema-ks", type=int, default=5)
    ap.add_argument("--strategy", choices=["sma", "ema"], default="sma",
                    help="which engine's signals TRADE; the other is "
                         "scored hypothetically for comparison")
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
    ap.add_argument("--max-orders-per-day", type=int, default=10)
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
    args = ap.parse_args()

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
        broker = arm_live_trading(args.symbol, limits)
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
    om = OrderManager(broker, args.symbol, limits, audit_path=args.audit)

    from compare import StrategyScorecard, comparison_report
    br = Bridge(args.port, args.symbol, args.fast, args.slow,
                ema_kf=args.ema_kf, ema_ks=args.ema_ks, log_path=args.log)
    cards = {"sma": StrategyScorecard(f"SMA {args.fast}/{args.slow}"),
             "ema": StrategyScorecard(
                 f"EMA 1/{1 << args.ema_kf}:1/{1 << args.ema_ks}")}
    dash = None
    if args.dashboard:
        from dashboard import DashboardServer
        dash = DashboardServer(br, om, args.dashboard, scorecards=cards)
        dash.start()

    def on_verified(fr):
        cards[fr["strategy"]].on_signal(fr)      # both strategies scored
        if dash:
            dash.on_signal(fr)
        if fr["strategy"] == args.strategy:      # only one trades
            om.on_signal(fr)

    def on_divergence(info):
        if dash:
            dash.on_event("DIVERGENCE: " + info.get("reason", "?"), True)
        om.on_divergence(info)

    for v in br.verifiers.values():
        v.on_verified = on_verified
        v.on_divergence = on_divergence
    print(f"[om] trading strategy: {args.strategy.upper()} "
          f"(the other is scored, not traded)")

    try:
        if args.source == "sim":
            run_sim(br, args.n, args.rate, args.start_price)
        else:
            run_alpaca(br)
    except KeyboardInterrupt:
        pass
    finally:
        ok = br.summary()
        print(comparison_report(cards))
        om.summary(args.household_income, args.filing_status,
                   args.state_rate, args.gross)
        br.close()
        sys.exit(0 if ok and not om.halted else 1)


if __name__ == "__main__":
    main()
