#!/usr/bin/env python3
"""
backtest.py — replay historical trades through the SAME engines that run
live and in verification. Not a reimplementation: SMAMirror, EMAMirror,
StrategyScorecard, and RiskPolicy are imported unchanged from the exact
modules the FPGA's signals are verified against. A backtest is only
meaningful if "SMA crossover" means the identical arithmetic in both
places — this guarantees that by construction rather than by care.

    python3 backtest.py --symbol SPY --strategy sma \\
        --trades ./historical_trades/SPY.trades.jsonl \\
        --fast 8 --slow 32 --cooldown 60 --max-orders-per-day 10

Feed it JSONL from fetch_historical_trades.py (one Alpaca trade record
per line, fields "t" ISO timestamp and "p" price — matches Alpaca's
documented trade schema). Streams the file rather than loading it whole
(these files can be gigabytes), replaying each trade through:

  1. BOTH SMAMirror and EMAMirror (matching the live bridge exactly)
  2. one StrategyScorecard per strategy, gated through a RiskPolicy whose
     clock is the trade's OWN historical timestamp — NOT wall-clock time
     as this script runs. Without that, replaying years of history in
     seconds would mean cooldown never expires and the daily cap never
     rolls over against the real calendar; see BacktestClock below and
     RiskPolicy's now_fn parameter in order_manager.py.

Output is the same comparison_report() table you already read from live
sessions — same columns, same "few round trips" caveat, same honesty
about hypothetical signal-price fills with no slippage. A multi-year
backtest answers "does this crossover show any edge over real history",
not "will it be profitable live" — spread, slippage, and partial fills
are absent here exactly as they're absent from the live scorecard's
untraded-strategy row.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from backtest_results import save_backtest_result, RESULTS_DIR_DEFAULT
from compare import (StrategyScorecard, comparison_report,
                     monthly_breakdown_report)
from order_manager import (RiskPolicy, RiskLimits, HistoricalClock,
                          OrderManager, MockBroker, sync_live_card, ET)
from position_risk import PositionRiskOverlay
from tick_protocol import (SMAMirror, EMAMirror, VWAPMirror, SIDE_SELL,
                          to_e4, iter_trades, iter_trades_multi)
BacktestClock = HistoricalClock   # backward-compatible alias — this class
                                 # moved to order_manager.py so the same
                                 # replay mechanism could be reused for
                                 # restoring scored-strategy state across
                                 # a live restart, not just backtests



def _route_live_signal(om, broker, cards, strategy_key: str,
                       symbol: str, side: int, price_e4: int, t
                       ) -> str:
    """v3.44: routes one signal to the REAL, unmodified OrderManager
    for the live strategy, then backfills the StrategyScorecard-
    internal bookkeeping (.opens, .block_reasons, .trip_log) that
    on_signal() has no concept of and sync_live_card() (reused as-is
    from order_manager.py's live dashboard, which needs none of this)
    never populates. Returns on_signal()'s own outcome string.

    Without this, the live row's report would be missing entry prices,
    the specific reasons behind blocked signals, and every completed
    trip's month for --monthly -- all real backtest.py reporting
    features the old, StrategyScorecard-only implementation always
    had for every row for free.
    """
    pre_qty = om.positions.get(symbol, 0)
    pre_avg_e4 = om.costs._entries.get(symbol, [0, 0])[1]
    pre_fees = om.costs.total_fees
    outcome = om.on_signal({"side": side, "price_e4": price_e4,
                            "symbol": symbol, "strategy": strategy_key})
    sync_live_card(cards, strategy_key, om)
    card = cards[strategy_key]

    if outcome.startswith("blocked:"):
        reason = outcome[len("blocked: "):]
        card.block_reasons[reason.split(" (")[0]] += 1
    elif outcome == "FILLED":
        if pre_qty == 0 and om.positions.get(symbol, 0) > 0:
            card.opens[symbol] = price_e4    # a fresh entry just opened
        if pre_qty > 0 and om.positions.get(symbol, 0) == 0 and broker.fills:
            exit_e4 = broker.fills[-1]["fill_price_e4"]
            trip_pnl_e4 = (exit_e4 - pre_avg_e4) * pre_qty
            fee_usd = om.costs.total_fees - pre_fees
            card.trip_log.append({
                "close_t": t, "symbol": symbol, "entry_e4": pre_avg_e4,
                "exit_e4": exit_e4, "qty": pre_qty, "pnl_e4": trip_pnl_e4,
                "fees_usd": fee_usd, "win": trip_pnl_e4 > 0})
    return outcome


def run_backtest(trades_paths, symbol: str, fast_n: int, slow_n: int,
                 ema_kf: int, ema_ks: int, limits: RiskLimits,
                 traded_strategy: str, progress_every: int = 500_000,
                 audit_path: str = "backtest_audit.jsonl",
                 killfile: str = "backtest.kill",
                 stop_sigma_mult: float = 0.0,
                 anchor_gate_tolerance: float = 0.0,
                 risk_dollars_per_trade: float = 500.0,
                 vwap_warmup: int = 20, vwap_k2_q8: int = 256,
                 profit_gate: bool = False, htf_ltf: bool = False,
                 htf_interval_s: int = 3600, ltf_interval_s: int = 300,
                 vwap_bounce: bool = False, vwap_band_k: float = 1.0,
                 pg_max_hold_days: float | None = None,
                 blended: bool = False, blend_vwap_shares: int = 6,
                 blend_vwap_notional: float = 1_300.0,
                 blend_pg_shares: int = 4,
                 blend_pg_notional: float = 700.0,
                 blend_account_notional: float = 2_000.0
                 ) -> tuple[dict[str, StrategyScorecard], dict]:
    """v3.44: traded_strategy (sma/ema/vwap_bounce) now runs through the
    REAL, unmodified OrderManager from order_manager.py -- not a
    reimplementation. This is the actual point of this rewrite: any
    future change to on_signal()'s trading logic (risk gating, the
    position-risk overlay, cost tracking, anything) applies here too,
    automatically, with zero duplicated code to keep in sync.

    The two OTHER strategies are still scored via StrategyScorecard,
    exactly as before -- hypothetical, gated identically, never
    trading -- matching how order_manager.py itself only ever applies
    real fills (and the risk overlay) to the ONE live strategy.

    No Bridge, no FPGAEmulator, no wire protocol anywhere: SMAMirror/
    EMAMirror/VWAPMirror are fed directly, in-process, at full CPU
    speed. That's the whole reason this is dramatically faster than
    order_manager.py --source historical, which has to round-trip
    every tick through the emulator for hardware verification -- a
    guarantee that matters enormously for live trading and is pure
    overhead for evaluating strategy performance against history you
    already trust.
    """
    if isinstance(trades_paths, str):
        trades_paths = [trades_paths]
    sma_model = SMAMirror(fast_n=fast_n, slow_n=slow_n)
    ema_model = EMAMirror(k_fast=ema_kf, k_slow=ema_ks, warmup_n=slow_n)

    clocks = {"sma": HistoricalClock(), "ema": HistoricalClock()}
    cards = {
        name: StrategyScorecard(
            f"{name.upper()} backtest", live=(name == traded_strategy),
            policy=RiskPolicy(limits, now_fn=clocks[name]))
        for name in ("sma", "ema")
    }

    # v3.44: the traded strategy runs through the REAL OrderManager.
    # MockBroker fills instantly at the signal price, same as a live
    # --broker mock session -- no slippage, no partial fills, exactly
    # as honest (or not) as the old scored rows always were.
    broker = MockBroker()
    # v3.55: restore_state=False -- a backtest starts clean every time.
    # Sharing the default audit file across runs made each run inherit
    # the previous one's P&L, trip count and daily order count, and a
    # restored wall-clock last_order_t against this historical clock
    # locked the cooldown on permanently.
    om = OrderManager(broker, [symbol], limits, audit_path=audit_path,
                      killfile=killfile, restore_state=False,
                      # v3.56: skip the write-only blocked records and the
                      # per-record flush. A full-year replay writes hundreds
                      # of thousands of blocked events that nothing ever
                      # reads back, each with its own fsync -- all of it
                      # pure I/O cost. Fills, halts and the rest are still
                      # audited, and close() flushes the tail.
                      audit_blocked=False, audit_flush=False)
    om.policy._now_fn = HistoricalClock()   # historical time, not wall
                                            # clock -- same trick every
                                            # other row here already uses
    live_vwap_model = None
    if traded_strategy == "vwap_bounce":
        live_vwap_model = VWAPMirror(warmup_n=vwap_warmup,
                                     k2_q8=vwap_k2_q8)
        cards["vwap_bounce"] = StrategyScorecard(
            "VWAP_BOUNCE backtest", live=True,
            policy=RiskPolicy(limits, now_fn=HistoricalClock()))
        if stop_sigma_mult > 0:
            om.risk_overlay = PositionRiskOverlay(
                stop_sigma_mult=stop_sigma_mult,
                anchor_gate_tolerance=anchor_gate_tolerance,
                risk_dollars_per_trade=risk_dollars_per_trade)
            om.vwap_models = {symbol: live_vwap_model}
    elif traded_strategy in ("sma", "ema"):
        om.risk_overlay = None   # the overlay is inherently VWAP-based
                                # (stop/anchor need a sigma the way
                                # only vwap_model has); sma/ema live
                                # sessions never get it either

    live_vwap_session_date = None   # ET calendar day, for the SAME
                                    # once-per-day reset order_manager.
                                    # py uses live -- NOT force-flat at
                                    # the boundary, unlike the scored
                                    # VWAPBounceScorecard row below;
                                    # allowing a position to survive
                                    # is the entire point of the
                                    # anchored-VWAP gate this backtests
    profit_gated = None
    if profit_gate:
        # SAME SMA crossover stream as cards["sma"], one added rule on
        # sells (see compare.py's ProfitGatedScorecard) — its own
        # RiskPolicy clone and its own historical clock, exactly
        # mirroring how the live order_manager.py wires this in
        clocks["sma_pg"] = HistoricalClock()
        from compare import ProfitGatedScorecard
        profit_gated = ProfitGatedScorecard(
            "SMA profit-gated", live=False,
            policy=RiskPolicy(limits, now_fn=clocks["sma_pg"]),
            max_hold_days=pg_max_hold_days)
        cards["sma_pg"] = profit_gated

    htf_ltf_card = None
    if htf_ltf:
        from htf_ltf_strategy import HTFLTFScorecard
        # its own RiskPolicy clone too, same limits as every other row,
        # so the comparison isolates the STRATEGY LOGIC (multi-timeframe
        # trend alignment) as the variable, not a difference in risk
        # gating — same principle as every other shadow row
        htf_ltf_card = HTFLTFScorecard(
            "HTF/LTF trend", symbol=symbol, live=False,
            policy=RiskPolicy(limits, now_fn=HistoricalClock()),
            htf_interval_s=htf_interval_s, ltf_interval_s=ltf_interval_s)
        cards["htf_ltf"] = htf_ltf_card

    vwap_card = None
    if vwap_bounce and traded_strategy != "vwap_bounce":
        # only meaningful as a SEPARATE, scored-only comparison row when
        # vwap_bounce ISN'T already the live strategy above -- if it is,
        # cards["vwap_bounce"] already holds the real, live-traded row
        from vwap_bounce_strategy import VWAPBounceScorecard
        vwap_card = VWAPBounceScorecard(
            "VWAP bounce", symbol=symbol, live=False,
            policy=RiskPolicy(limits, now_fn=HistoricalClock()),
            band_k=vwap_band_k)
        cards["vwap_bounce"] = vwap_card

    blend_card = None
    if blended:
        from blended_strategy import BlendedScorecard
        # two sleeves, each its own RiskPolicy clone + HistoricalClock
        # (same per-row isolation as every other shadow card), plus one
        # account-level exposure cap across both — see
        # blended_strategy.py for why a portfolio blend and not a
        # signal filter
        blend_card = BlendedScorecard.build(
            symbol=symbol, base_limits=limits,
            vwap_shares=blend_vwap_shares,
            vwap_notional_e4=to_e4(blend_vwap_notional),
            pg_shares=blend_pg_shares,
            pg_notional_e4=to_e4(blend_pg_notional),
            account_cap_e4=to_e4(blend_account_notional),
            band_k=vwap_band_k,
            max_hold_days=pg_max_hold_days,
            now_fn_factory=HistoricalClock)
        cards["blend"] = blend_card

    n = 0
    first_t = last_t = None
    interrupted = False
    try:
        for t, price_e4, qty in iter_trades_multi(trades_paths):
            n += 1
            if first_t is None:
                first_t = t
            last_t = t
            if n % progress_every == 0:
                print(f"  ...{n:,} trades replayed ({t.date()})",
                     file=sys.stderr)

            if traded_strategy == "vwap_bounce":
                # v3.44: once-per-day session reset, matching
                # order_manager.py's own live behavior -- deliberately
                # NOT forcing the position flat here (unlike the
                # scored VWAPBounceScorecard row below), since letting
                # a position survive the boundary is the entire
                # scenario the anchored-VWAP gate exists to judge
                day = t.astimezone(ET).date()
                if live_vwap_session_date is None:
                    live_vwap_session_date = day
                elif day != live_vwap_session_date:
                    live_vwap_model.sess_reset()
                    live_vwap_session_date = day

                if om.risk_overlay is not None:
                    om.risk_overlay.on_tick(symbol, price_e4, qty)
                    if (om.positions.get(symbol, 0) > 0
                            and om.risk_overlay.stop_triggered(
                                symbol, price_e4)):
                        _route_live_signal(om, broker, cards, "vwap_bounce",
                                          symbol, SIDE_SELL, price_e4, t)

                sig = live_vwap_model.ingest(price_e4, qty)
                if sig:
                    cards["vwap_bounce"].signals += 1   # matches main()'s
                                                        # on_verified()
                                                        # exactly -- done
                                                        # separately from
                                                        # om.on_signal()
                    om.policy._now_fn.set(t)
                    _route_live_signal(om, broker, cards, "vwap_bounce",
                                      symbol, sig.side, sig.price_e4, t)
            elif vwap_card is not None:
                vwap_card.on_tick(t, price_e4, qty)   # scored-only, as before

            sig = sma_model.ingest(price_e4)
            if sig:
                if traded_strategy == "sma":
                    cards["sma"].signals += 1
                    om.policy._now_fn.set(t)
                    _route_live_signal(om, broker, cards, "sma",
                                      symbol, sig.side, sig.price_e4, t)
                else:
                    clocks["sma"].set(t)
                    cards["sma"].on_signal({"side": sig.side,
                                           "price_e4": sig.price_e4,
                                           "symbol": symbol,
                                           "strategy": "sma"}, t=t)
                if profit_gated is not None:
                    clocks["sma_pg"].set(t)
                    profit_gated.on_signal({"side": sig.side,
                                           "price_e4": sig.price_e4,
                                           "symbol": symbol,
                                           "strategy": "sma"}, t=t)
                if blend_card is not None:
                    blend_card.on_sma_signal({"side": sig.side,
                                             "price_e4": sig.price_e4,
                                             "symbol": symbol,
                                             "strategy": "sma"}, t=t)

            sig = ema_model.ingest(price_e4)
            if sig:
                if traded_strategy == "ema":
                    cards["ema"].signals += 1
                    om.policy._now_fn.set(t)
                    _route_live_signal(om, broker, cards, "ema",
                                      symbol, sig.side, sig.price_e4, t)
                else:
                    clocks["ema"].set(t)
                    cards["ema"].on_signal({"side": sig.side,
                                           "price_e4": sig.price_e4,
                                           "symbol": symbol,
                                           "strategy": "ema"}, t=t)

            if htf_ltf_card is not None:
                htf_ltf_card.on_tick(t, price_e4)

            if blend_card is not None:
                blend_card.on_tick(t, price_e4, qty)
    except KeyboardInterrupt:
        # A partial result, honestly labeled, beats no result at all —
        # this is exactly the reported gap: Ctrl+C used to propagate
        # straight past the report/save steps below, so a long backtest
        # interrupted partway through produced NOTHING. Now it returns
        # normally with everything accumulated so far, with `interrupted`
        # set in meta so nothing downstream can mistake this for a
        # complete run.
        interrupted = True
        print(f"\n[backtest] INTERRUPTED after {n:,} trades "
             f"(last: {last_t}) — reporting PARTIAL results below, "
             f"NOT a complete backtest", file=sys.stderr)

    print(f"[backtest] {n:,} trades replayed for {symbol}"
         + (" [INCOMPLETE -- interrupted]" if interrupted else ""),
         file=sys.stderr)
    # v3.44: "live" is set at construction time now (cards["sma"]/
    # cards["ema"]/cards["vwap_bounce"] as appropriate) — the traded
    # strategy's row holds REAL fills from the real OrderManager,
    # synced throughout the loop via sync_live_card(), not just
    # identically-gated hypothetical numbers the way every row used
    # to work before this rewrite

    # date range is derived from the ACTUAL DATA replayed, not trusted
    # from a filename — correct regardless of naming convention, and
    # what save_backtest_result() uses to name/label a saved run
    meta = {"n_trades": n, "first_t": first_t, "last_t": last_t,
           "trades_paths": trades_paths, "interrupted": interrupted}
    return cards, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True,
                    help="one JSONL file from fetch_historical_trades.py, "
                         "or several comma-separated files to replay as "
                         "one continuous history (e.g. if you fetched "
                         "Jan-Mar and Apr-Jun separately: "
                         "SPY_2026-01-01_2026-04-01.trades.jsonl,"
                         "SPY_2026-04-01_2026-07-01.trades.jsonl) — "
                         "must be given in chronological, non-"
                         "overlapping order")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--strategy", choices=["sma", "ema", "vwap_bounce"],
                    default="sma",
                    help="v3.44: which strategy actually TRADES, through "
                        "the real OrderManager -- real fills, real cost "
                        "tracking, and (for vwap_bounce) the real risk "
                        "overlay if --stop-sigma-mult is set. The other "
                        "two strategies are still scored alongside for "
                        "comparison, gated identically, never trading -- "
                        "exactly matching order_manager.py's own live "
                        "behavior")
    ap.add_argument("--fast", type=int, default=8)
    ap.add_argument("--slow", type=int, default=32)
    ap.add_argument("--ema-kf", type=int, default=3)
    ap.add_argument("--ema-ks", type=int, default=5)
    ap.add_argument("--qty", type=int, default=5,
                    help="v3.44: renamed from --order-qty and default "
                        "corrected 1 -> 5 to match order_manager.py's "
                        "actual current live default -- the old default "
                        "silently drifted out of sync back at v3.27 and "
                        "this rebuild is exactly the moment to fix it. "
                        "Ignored for vwap_bounce as the live strategy "
                        "when --stop-sigma-mult is set: risk-based "
                        "sizing overrides it then, same as live")
    ap.add_argument("--max-notional", type=float, default=3_000.0,
                    help="v3.44: corrected 2000 -> 3000 to match "
                        "order_manager.py's actual current live default "
                        "(changed at v3.27; backtest.py's default had "
                        "silently drifted out of sync since)")
    ap.add_argument("--max-position-notional", type=float, default=10_000.0,
                    help="v3.44: was MISSING entirely before this rebuild "
                        "-- order_manager.py's live sessions have used "
                        "this total-position dollar cap since v3.27, but "
                        "backtest.py had no way to apply it at all. "
                        "Matches the live default; <= 0 disables it")
    ap.add_argument("--max-orders-per-day", type=int, default=1000)
    ap.add_argument("--cooldown", type=float, default=60.0)
    ap.add_argument("--audit", default="backtest_audit.jsonl",
                    help="v3.44: cost-basis/fill audit log for the LIVE "
                        "strategy's real OrderManager -- same meaning as "
                        "order_manager.py's --audit, just defaulting to "
                        "a backtest-specific filename so it never collides "
                        "with a real trading session's own audit log")
    ap.add_argument("--killfile", default="backtest.kill",
                    help="v3.44: same meaning as order_manager.py's "
                        "--killfile, defaulting to a backtest-specific "
                        "path for the same reason as --audit above")
    ap.add_argument("--stop-sigma-mult", type=float, default=0.0,
                    help="v3.44: --strategy vwap_bounce only. Same "
                        "meaning and same default (0.0 = disabled) as "
                        "order_manager.py's flag of the same name -- "
                        "the actual point of this rebuild is that this "
                        "flag does the SAME thing here as it does live, "
                        "using the same position_risk.py module")
    ap.add_argument("--anchor-gate-tolerance", type=float, default=0.0,
                    help="v3.44: --strategy vwap_bounce only, same "
                        "meaning as order_manager.py's flag of the same "
                        "name")
    ap.add_argument("--risk-per-trade", type=float, default=500.0,
                    help="v3.44: --strategy vwap_bounce only, same "
                        "meaning as order_manager.py's flag of the same "
                        "name")
    ap.add_argument("--vwap-warmup", type=int, default=20,
                    help="v3.44: --strategy vwap_bounce only, ticks "
                        "before the live VWAP band is trusted -- same "
                        "meaning as order_manager.py's flag")
    ap.add_argument("--vwap-k2-q8", type=int, default=256,
                    help="v3.44: --strategy vwap_bounce only, live VWAP "
                        "band width (k^2, Q8 fixed point; 256 = k=1.0) "
                        "-- same meaning as order_manager.py's flag. "
                        "NOT the same as --vwap-band-k below, which "
                        "tunes the separate, SCORED-only VWAPBounceScorecard "
                        "comparison row when vwap_bounce isn't the live "
                        "strategy")
    ap.add_argument("--profit-gate", action="store_true",
                    help="also backtest the SAME SMA crossover signals "
                         "with one added rule: a sell only executes if "
                         "price is above the average cost of shares "
                         "held (see compare.py's ProfitGatedScorecard) "
                         "— always score-only, regardless of --strategy")
    ap.add_argument("--htf-ltf", action="store_true",
                    help="also backtest a multi-timeframe trend-"
                         "alignment strategy: a higher-timeframe 20/50/"
                         "200 EMA stack sets a long-only bullish/bearish/"
                         "none bias, a lower-timeframe fast/slow EMA "
                         "cross times entries (only with the bias), and "
                         "the position trails until price closes back "
                         "below the LTF fast EMA (see htf_ltf_strategy.py) "
                         "— always score-only, regardless of --strategy")
    ap.add_argument("--htf-interval", type=int, default=3600,
                    help="higher-timeframe bar size in seconds "
                         "(default 3600 = 1 hour)")
    ap.add_argument("--ltf-interval", type=int, default=300,
                    help="lower-timeframe bar size in seconds "
                         "(default 300 = 5 minutes)")
    ap.add_argument("--vwap-bounce", action="store_true",
                    help="also backtest a session-VWAP mean-reversion "
                         "strategy: buy when price dips below a "
                         "volume-weighted-stdev band under VWAP and "
                         "bounces back above it, sell when price reverts "
                         "to VWAP (positions are forced flat at each "
                         "day's session boundary — see "
                         "vwap_bounce_strategy.py) — always score-only, "
                         "regardless of --strategy")
    ap.add_argument("--vwap-band-k", type=float, default=1.0,
                    help="band width in session standard deviations "
                         "(default 1.0)")
    ap.add_argument("--pg-max-hold-days", type=float, default=5.0,
                    help="profit-gated max hold: force-close a position "
                         "held longer than this many days at the next "
                         "signal, even at a loss — bounds the never-"
                         "realize-a-loss rule's unbounded downside and "
                         "makes its win rate a real number instead of a "
                         "definitional 100%%. Applies to the standalone "
                         "profit-gated row AND the blend's SMA-PG "
                         "sleeve. <= 0 disables (restores the original "
                         "unbounded behavior, for comparison runs). "
                         "Default 5.0")
    ap.add_argument("--blended", action="store_true",
                    help="also backtest the two-sleeve portfolio blend: "
                         "VWAP bounce + SMA profit-gated trading "
                         "independently, each with its own carved-down "
                         "RiskPolicy budget, under one account-level "
                         "open-notional cap across both (see "
                         "blended_strategy.py) — always score-only, "
                         "regardless of --strategy. Implies the SMA and "
                         "VWAP machinery it feeds from; run with "
                         "--profit-gate --vwap-bounce to also get the "
                         "standalone rows for comparison")
    ap.add_argument("--blend-vwap-shares", type=int, default=6,
                    help="VWAP sleeve max_shares (default 6 — the "
                         "larger budget goes to the sleeve carrying "
                         "the larger share of the historical edge)")
    ap.add_argument("--blend-vwap-notional", type=float, default=1_300.0,
                    help="VWAP sleeve per-order notional cap $ "
                         "(default 1300)")
    ap.add_argument("--blend-pg-shares", type=int, default=4,
                    help="SMA-PG sleeve max_shares (default 4)")
    ap.add_argument("--blend-pg-notional", type=float, default=700.0,
                    help="SMA-PG sleeve per-order notional cap $ "
                         "(default 700)")
    ap.add_argument("--blend-account-notional", type=float,
                    default=2_000.0,
                    help="account-level cap: total open cost-basis "
                         "notional across BOTH sleeves (default 2000 — "
                         "matches the single-strategy --max-notional "
                         "default, so the blend re-divides the same "
                         "budget rather than adding capital)")
    ap.add_argument("--results-dir", default=RESULTS_DIR_DEFAULT,
                    help="where saved runs go — browse them with "
                         "list_backtest_results.py")
    ap.add_argument("--no-save", action="store_true",
                    help="skip saving this run (default: always saved, "
                         "so you can go back and review it later)")
    ap.add_argument("--monthly", action="store_true",
                    help="also print (and save) a month-by-month P&L "
                         "breakdown, bucketed by each trip's CLOSE date "
                         "from this ONE continuous run — NOT the same "
                         "as independently re-running the backtest per "
                         "month, which would silently disagree due to "
                         "state (open positions, warmup, cooldown) that "
                         "wouldn't carry across artificial boundaries")
    args = ap.parse_args()

    limits = RiskLimits(
        order_qty=args.qty,
        # v3.45: max_shares itself still exists on RiskLimits (the
        # blend strategy's own per-sleeve caps below depend on it for
        # their own, separate purpose) but this main CLI path no
        # longer exposes it at all -- matching order_manager.py's own
        # RiskLimits construction exactly, which sets this to 10**9
        # for the identical reason: this project's real sessions are
        # sized by dollar exposure now, not share count, so this is
        # set high enough to never be the binding constraint here
        # either; the real limits are max_notional_e4/
        # max_position_notional_e4 below
        max_shares=10**9,
        max_notional_e4=to_e4(args.max_notional),
        max_position_notional_e4=(to_e4(args.max_position_notional)
                                  if args.max_position_notional > 0
                                  else None),
        max_orders_per_day=args.max_orders_per_day,
        cooldown_s=args.cooldown, require_market_hours=False)
        # require_market_hours=False: historical trade timestamps ARE
        # market-hours by construction (that's when trades print), so
        # this gate would just be redundant work against real data

    trades_paths = [p.strip() for p in args.trades.split(",") if p.strip()]
    from compare import normalize_max_hold_days
    pg_max_hold = normalize_max_hold_days(args.pg_max_hold_days)
    cards, meta = run_backtest(trades_paths, args.symbol, args.fast,
                              args.slow, args.ema_kf, args.ema_ks, limits,
                              args.strategy,
                              audit_path=args.audit, killfile=args.killfile,
                              stop_sigma_mult=args.stop_sigma_mult,
                              anchor_gate_tolerance=args.anchor_gate_tolerance,
                              risk_dollars_per_trade=args.risk_per_trade,
                              vwap_warmup=args.vwap_warmup,
                              vwap_k2_q8=args.vwap_k2_q8,
                              profit_gate=args.profit_gate,
                              htf_ltf=args.htf_ltf,
                              htf_interval_s=args.htf_interval,
                              ltf_interval_s=args.ltf_interval,
                              vwap_bounce=args.vwap_bounce,
                              vwap_band_k=args.vwap_band_k,
                              pg_max_hold_days=pg_max_hold,
                              blended=args.blended,
                              blend_vwap_shares=args.blend_vwap_shares,
                              blend_vwap_notional=args.blend_vwap_notional,
                              blend_pg_shares=args.blend_pg_shares,
                              blend_pg_notional=args.blend_pg_notional,
                              blend_account_notional=
                                  args.blend_account_notional)
    print()
    if meta.get("interrupted"):
        print("=" * 60)
        print(f"*** INTERRUPTED after {meta['n_trades']:,} trades — "
             f"PARTIAL RESULTS, NOT A COMPLETE BACKTEST ***")
        print("=" * 60)
    print(comparison_report(cards))
    if args.monthly:
        print()
        print(monthly_breakdown_report(cards))

    if not args.no_save:
        params = {
            "traded_strategy": args.strategy,
            "fast": args.fast, "slow": args.slow,
            "ema_kf": args.ema_kf, "ema_ks": args.ema_ks,
            "qty": args.qty,
            "max_notional": args.max_notional,
            "max_position_notional": args.max_position_notional,
            "stop_sigma_mult": args.stop_sigma_mult,
            "anchor_gate_tolerance": args.anchor_gate_tolerance,
            "risk_per_trade": args.risk_per_trade,
            "vwap_warmup": args.vwap_warmup, "vwap_k2_q8": args.vwap_k2_q8,
            "max_orders_per_day": args.max_orders_per_day,
            "cooldown": args.cooldown,
            "profit_gate": args.profit_gate,
            "htf_ltf": args.htf_ltf,
            "htf_interval_s": args.htf_interval,
            "ltf_interval_s": args.ltf_interval,
            "vwap_bounce": args.vwap_bounce,
            "vwap_band_k": args.vwap_band_k,
            "pg_max_hold_days": pg_max_hold,
            "blended": args.blended,
            "blend_vwap_shares": args.blend_vwap_shares,
            "blend_vwap_notional": args.blend_vwap_notional,
            "blend_pg_shares": args.blend_pg_shares,
            "blend_pg_notional": args.blend_pg_notional,
            "blend_account_notional": args.blend_account_notional,
        }
        run_dir = save_backtest_result(cards, args.symbol, meta, params,
                                       results_dir=args.results_dir,
                                       include_monthly=args.monthly)
        print(f"\n[backtest] saved to {run_dir}/ "
             f"(summary.json + report.txt) — browse past runs with "
             f"list_backtest_results.py")


if __name__ == "__main__":
    main()
