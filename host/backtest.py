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
from compare import StrategyScorecard, comparison_report
from order_manager import RiskPolicy, RiskLimits, HistoricalClock
from tick_protocol import SMAMirror, EMAMirror, to_e4
BacktestClock = HistoricalClock   # backward-compatible alias — this class
                                 # moved to order_manager.py so the same
                                 # replay mechanism could be reused for
                                 # restoring scored-strategy state across
                                 # a live restart, not just backtests



def iter_trades(path: str):
    """Stream one (datetime, price_e4, qty) triple per line — never
    loads the whole file, since these can be gigabytes for a multi-
    year pull. qty is Alpaca's own trade-size field ("s", the same
    field name bridge.py's live path already reads) — real historical
    downloads always have it since fetch_historical_trades.py writes
    Alpaca's trade record verbatim; defaults to 1 for synthetic test
    data that doesn't bother setting it, so every existing caller that
    only cares about price keeps working unmodified."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            t = datetime.fromisoformat(rec["t"].replace("Z", "+00:00"))
            qty = int(rec.get("s", 1))
            yield t, to_e4(float(rec["p"])), qty


def iter_trades_multi(paths: list[str]):
    """Stream several trade files IN THE ORDER GIVEN, as if they were
    one continuous file. Meant for combining separately-fetched,
    non-overlapping date ranges (fetch_historical_trades.py now scopes
    filenames to their exact range, so incrementally widening your
    history means MULTIPLE files rather than one growing file — this
    is how you replay them together without re-downloading anything).

    Does a cheap streaming sanity check, not a full sort: if a later
    file's first trade is timestamped BEFORE the previous file's last
    trade, that's very likely the files were passed out of
    chronological order (or the ranges overlap), which would corrupt
    the backtest's notion of "historical time" — this raises rather
    than silently replaying history out of order."""
    prev_last_t = None
    for path in paths:
        first_in_file = True
        for t, price_e4, qty in iter_trades(path):
            if first_in_file and prev_last_t is not None and t < prev_last_t:
                raise ValueError(
                    f"{path} starts at {t}, which is BEFORE the previous "
                    f"file's last trade at {prev_last_t} — files must be "
                    f"passed in chronological order with non-overlapping "
                    f"ranges, or the backtest's historical clock breaks")
            first_in_file = False
            prev_last_t = t
            yield t, price_e4, qty


def run_backtest(trades_paths, symbol: str, fast_n: int, slow_n: int,
                 ema_kf: int, ema_ks: int, limits: RiskLimits,
                 traded_strategy: str, progress_every: int = 500_000,
                 profit_gate: bool = False, htf_ltf: bool = False,
                 htf_interval_s: int = 3600, ltf_interval_s: int = 300,
                 vwap_bounce: bool = False, vwap_band_k: float = 1.0
                 ) -> dict[str, StrategyScorecard]:
    if isinstance(trades_paths, str):
        trades_paths = [trades_paths]
    sma_model = SMAMirror(fast_n=fast_n, slow_n=slow_n)
    ema_model = EMAMirror(k_fast=ema_kf, k_slow=ema_ks, warmup_n=slow_n)

    clocks = {"sma": HistoricalClock(), "ema": HistoricalClock()}
    cards = {
        name: StrategyScorecard(
            f"{name.upper()} backtest", live=False,
            policy=RiskPolicy(limits, now_fn=clocks[name]))
        for name in ("sma", "ema")
    }
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
            policy=RiskPolicy(limits, now_fn=clocks["sma_pg"]))
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
    if vwap_bounce:
        from vwap_bounce_strategy import VWAPBounceScorecard
        vwap_card = VWAPBounceScorecard(
            "VWAP bounce", symbol=symbol, live=False,
            policy=RiskPolicy(limits, now_fn=HistoricalClock()),
            band_k=vwap_band_k)
        cards["vwap_bounce"] = vwap_card

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

            sig = sma_model.ingest(price_e4)
            if sig:
                clocks["sma"].set(t)
                cards["sma"].on_signal({"side": sig.side,
                                       "price_e4": sig.price_e4,
                                       "symbol": symbol, "strategy": "sma"})
                if profit_gated is not None:
                    clocks["sma_pg"].set(t)
                    profit_gated.on_signal({"side": sig.side,
                                           "price_e4": sig.price_e4,
                                           "symbol": symbol,
                                           "strategy": "sma"})

            sig = ema_model.ingest(price_e4)
            if sig:
                clocks["ema"].set(t)
                cards["ema"].on_signal({"side": sig.side,
                                       "price_e4": sig.price_e4,
                                       "symbol": symbol, "strategy": "ema"})

            if htf_ltf_card is not None:
                htf_ltf_card.on_tick(t, price_e4)

            if vwap_card is not None:
                vwap_card.on_tick(t, price_e4, qty)
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
    cards["sma"].live = (traded_strategy == "sma")
    cards["ema"].live = (traded_strategy == "ema")
    # the "live" flag here only controls report LABELING (both rows were
    # actually gated identically) — a backtest has no real broker fills
    # to prefer over the gated replay, unlike a live session's true row

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
    ap.add_argument("--strategy", choices=["sma", "ema"], default="sma",
                    help="which row is labeled [LIVE] in the report — "
                         "cosmetic only, both are gated identically")
    ap.add_argument("--fast", type=int, default=8)
    ap.add_argument("--slow", type=int, default=32)
    ap.add_argument("--ema-kf", type=int, default=3)
    ap.add_argument("--ema-ks", type=int, default=5)
    ap.add_argument("--order-qty", type=int, default=1)
    ap.add_argument("--max-shares", type=int, default=10)   # matches
                    # order_manager.py's live default -- was 1, an
                    # oversight that made a bare backtest.py run NOT
                    # faithfully reproduce default live behavior
    ap.add_argument("--max-notional", type=float, default=2_000.0)   # was
                    # 1_000_000.0 (effectively a no-op cap); now matches
                    # order_manager.py's live default of $2,000/order
    ap.add_argument("--max-orders-per-day", type=int, default=1000)
    ap.add_argument("--cooldown", type=float, default=60.0)
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
    ap.add_argument("--results-dir", default=RESULTS_DIR_DEFAULT,
                    help="where saved runs go — browse them with "
                         "list_backtest_results.py")
    ap.add_argument("--no-save", action="store_true",
                    help="skip saving this run (default: always saved, "
                         "so you can go back and review it later)")
    args = ap.parse_args()

    limits = RiskLimits(
        order_qty=args.order_qty, max_shares=args.max_shares,
        max_notional_e4=to_e4(args.max_notional),
        max_orders_per_day=args.max_orders_per_day,
        cooldown_s=args.cooldown, require_market_hours=False)
        # require_market_hours=False: historical trade timestamps ARE
        # market-hours by construction (that's when trades print), so
        # this gate would just be redundant work against real data

    trades_paths = [p.strip() for p in args.trades.split(",") if p.strip()]
    cards, meta = run_backtest(trades_paths, args.symbol, args.fast,
                              args.slow, args.ema_kf, args.ema_ks, limits,
                              args.strategy, profit_gate=args.profit_gate,
                              htf_ltf=args.htf_ltf,
                              htf_interval_s=args.htf_interval,
                              ltf_interval_s=args.ltf_interval,
                              vwap_bounce=args.vwap_bounce,
                              vwap_band_k=args.vwap_band_k)
    print()
    if meta.get("interrupted"):
        print("=" * 60)
        print(f"*** INTERRUPTED after {meta['n_trades']:,} trades — "
             f"PARTIAL RESULTS, NOT A COMPLETE BACKTEST ***")
        print("=" * 60)
    print(comparison_report(cards))

    if not args.no_save:
        params = {
            "traded_strategy": args.strategy,
            "fast": args.fast, "slow": args.slow,
            "ema_kf": args.ema_kf, "ema_ks": args.ema_ks,
            "order_qty": args.order_qty, "max_shares": args.max_shares,
            "max_notional": args.max_notional,
            "max_orders_per_day": args.max_orders_per_day,
            "cooldown": args.cooldown,
            "profit_gate": args.profit_gate,
            "htf_ltf": args.htf_ltf,
            "htf_interval_s": args.htf_interval,
            "ltf_interval_s": args.ltf_interval,
            "vwap_bounce": args.vwap_bounce,
            "vwap_band_k": args.vwap_band_k,
        }
        run_dir = save_backtest_result(cards, args.symbol, meta, params,
                                       results_dir=args.results_dir)
        print(f"\n[backtest] saved to {run_dir}/ "
             f"(summary.json + report.txt) — browse past runs with "
             f"list_backtest_results.py")


if __name__ == "__main__":
    main()
