#!/usr/bin/env python3
"""
feeds.py — v3.46

Market-data sources: simulated random walks, historical JSONL replay,
and the live Alpaca websocket. Moved here verbatim from bridge.py.

These were never really "bridge" code -- they are where ticks come
FROM, and they only ever touched four things on whatever they were
handed: .symbols, .configure_symbols(), .send_trade(), .pump(). That
makes them duck-typed, so the same implementation drives both the
direct TickEngine (tick_engine.py, the default) and the hardware
Bridge (bridge.py, via --port).

Keeping ONE copy is the point. Maintaining a second, parallel version
for the no-hardware path is exactly the drift that took a full rewrite
to undo in backtest.py at v3.44, and the reconnection/heartbeat work
in run_alpaca (v3.36's supervisor, v3.40's ping/pong) is far too
hard-won to duplicate.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

from tick_protocol import to_e4, iter_trades_multi


def run_sim(br, n: int, rate: float, start_price: float):
    """Deterministic-seed random walks, one per configured symbol,
    round-robin at `rate` ticks/second total. Configures the FPGA's
    slots first — every session starts by syncing hardware to host."""
    import random
    if not br.configure_symbols(br.symbols):
        print("[sim] aborting: slot configuration failed")
        return
    rng = random.Random(42)
    walks = {t: to_e4(start_price) for t in br.symbols}
    period = 1.0 / rate
    print(f"[sim] {n} trades across {br.symbols} @ {rate}/s")
    for i in range(n):
        sym = br.symbols[i % len(br.symbols)]
        walks[sym] = max(100_000, walks[sym] + rng.randint(-40_000, 40_000))
        br.send_trade(walks[sym], rng.randint(1, 500), symbol=sym)
        br.pump(timeout=period)
    br.pump(timeout=1.0)


def run_historical(br, paths: list[str], rate: float = 200.0,
                   max_trades: int | None = 20_000):
    """The bring-up step promised after --selftest passes: REAL market
    ticks (from fetch_historical_trades.py's JSONL, the exact same
    files backtest.py scores) driving the REAL board, verified the
    same way every other signal here is — against an independent host
    model, bit-for-bit, zero divergence required. Where --source sim's
    random walk can only ever prove the math works on synthetic data,
    this proves it on the actual price/volume PATTERNS the strategy
    will trade on.

    Single-symbol only, deliberately: a trades file has one symbol's
    data (backtest.py's own convention — one file per symbol), and
    interleaving multiple symbols' real files chronologically is real
    added complexity this bring-up step doesn't need. Configure the
    board with exactly one --symbol to use this.

    rate caps replay speed — historical files span months and can be
    hundreds of millions of trades (a real one seen in this project:
    218M for a multi-year QQQ pull), and even the current link's
    ceiling (~480 ticks/sec, see vwap_engine.sv's header) would take
    a real trading day's worth of ticks HOURS to replay tick-for-tick.
    This does NOT replay at the recorded real-world gaps between
    ticks — it streams REAL prices/volumes, in REAL order, paced by
    `rate` alone, same as --source sim's pacing model. That is
    correct for what a bring-up run needs (does the math hold on real
    market patterns?) and wrong for anything claiming to reproduce
    actual session timing.

    max_trades bounds the run so a bring-up session finishes in a
    reasonable time rather than accidentally kicking off a many-hour
    replay of an entire multi-year file; pass None for no cap.
    """
    from tick_protocol import iter_trades_multi
    if len(br.symbols) != 1:
        print(f"[historical] aborting: {len(br.symbols)} symbols "
             f"configured ({br.symbols}) — historical replay is "
             "single-symbol only (one trades file = one symbol, "
             "matching backtest.py's convention); pass exactly one "
             "--symbol")
        return
    sym = br.symbols[0]
    if not br.configure_symbols(br.symbols):
        print("[historical] aborting: slot configuration failed")
        return
    if not br.send_sessrst():
        print("[historical] aborting: session reset not acked — link "
              "trouble, or a bitstream predating v3.18 (no sessctl.sv). "
              "Run --selftest first.")
        return
    period = 1.0 / rate
    print(f"[historical] replaying {paths} for {sym} @ up to {rate}/s"
         + (f", capped at {max_trades} trades" if max_trades else ""))
    n = 0
    for t, price_e4, qty in iter_trades_multi(paths):
        br.send_trade(price_e4, qty, symbol=sym)
        br.pump(timeout=period)
        n += 1
        if n % 1000 == 0:
            print(f"[historical] {n} trades replayed "
                 f"(real timestamp reached: {t})")
        if max_trades is not None and n >= max_trades:
            print(f"[historical] stopping at --replay-max ({max_trades})")
            break
    br.pump(timeout=1.0)
    print(f"[historical] done: {n} real trades replayed for {sym}")



def run_alpaca(br, feed: str = "iex", relay_url: str | None = None,
               reconnect_backoff_s: float = 5.0,
               reconnect_backoff_max_s: float = 60.0,
               reconnect_healthy_threshold_s: float = 60.0,
               ping_interval_s: float = 20.0,
               ping_timeout_s: float = 10.0):
    """Live trades via Alpaca's v2 websocket. Lazy import + clear errors.

    relay_url: if set, connect here instead of to Alpaca directly —
    point this at a running alpaca_relay.py (see the ladder-trader
    project) when you want this AND another project both consuming
    live prices at the same time. Alpaca only allows one direct
    connection per login, even on paid data tiers, so running two
    projects' direct connections concurrently isn't possible without
    a relay in front of one real connection. Auth is still sent (the
    relay ignores its contents) so nothing else here needs to change.

    v3.36: automatically reconnects on ANY disconnect (idle overnight
    gaps, network blips, Alpaca-side resets) — found the hard way when
    a session started the evening before market open never resumed
    trading once it actually opened, with no error anywhere, because
    the many-hour overnight idle gap had dropped the connection at
    some point and nothing brought it back. reconnect_backoff_s/
    reconnect_backoff_max_s/reconnect_healthy_threshold_s are exposed
    mainly for fast, deterministic testing of the backoff logic itself
    (see test_host.py) — the defaults (5s / 60s / 60s) are what any
    real session should use.

    v3.40: ping_interval_s/ping_timeout_s enable a websocket heartbeat
    — found from a real VPN-toggle incident where the reconnection
    logic above never engaged at all. The v3.36 supervisor only
    reconnects once run_forever() RETURNS, which requires the
    underlying socket to actually notice something went wrong (an
    error, a clean close). A VPN changing network routes can silently
    black-hole a connection instead — no FIN, no RST, packets just
    stop arriving — and with no heartbeat, the socket has nothing
    telling it the far end is gone; it just blocks in recv() forever,
    run_forever() never returns, and the reconnection supervisor never
    gets a chance to run. websocket-client's own ping_interval defaults
    to 0 (disabled) — nothing here was asking for a heartbeat at all.
    Now sends a ping every ping_interval_s and requires a pong within
    ping_timeout_s or the library closes the connection itself,
    which DOES make run_forever() return — handing control back to
    the exact same reconnection supervisor, unchanged.
    """
    try:
        import websocket                              # websocket-client
    except ImportError:
        sys.exit("alpaca source needs:  pip3 install websocket-client "
                 "--break-system-packages")
    key = os.environ.get("ALPACA_KEY")
    secret = os.environ.get("ALPACA_SECRET")
    if not (key and secret):
        sys.exit("set ALPACA_KEY and ALPACA_SECRET environment variables")

    url = relay_url or f"wss://stream.data.alpaca.markets/v2/{feed}"
    if not br.configure_symbols(br.symbols):
        sys.exit("[alpaca] aborting: FPGA slot configuration failed")

    # v3.36: the websocket connection can drop for any number of mundane
    # reasons -- an idle overnight gap with no trades to keep it alive,
    # a network blip, Alpaca-side maintenance -- and the ORIGINAL code
    # here started run_forever() ONCE with no supervision at all: if it
    # ever returned (any disconnect, for any reason), the feed just
    # silently died and nothing brought it back. Found exactly this way:
    # a session started the evening before market open never resumed
    # trading once the market actually opened, with no error anywhere,
    # because the overnight idle gap (many hours with zero trades) had
    # dropped the connection at some point and nothing reconnected.
    #
    # ws_holder/connected_at are single-element lists (not bare
    # variables) so the nested closures below always reach the CURRENT
    # live connection's state, even after a reconnect replaces the
    # WebSocketApp object entirely.
    ws_holder = [None]
    connected_at = [None]
    stop_requested = threading.Event()

    def on_open(ws):
        connected_at[0] = time.monotonic()
        ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
        ws.send(json.dumps({"action": "subscribe",
                            "trades": list(br.symbols)}))
        print(f"[alpaca] subscribed to trades: {br.symbols}")

    def on_message(ws, message):
        for m in json.loads(message):
            if m.get("T") == "t" and m.get("S") in br.symbols:
                br.send_trade(to_e4(float(m["p"])), int(m.get("s", 0)),
                              symbol=m["S"])

    def on_error(ws, err):
        print(f"[alpaca] websocket error: {err}")

    def on_close(ws, *args):
        # *args absorbs whatever (close_status_code, close_msg) shape
        # this websocket-client version passes -- not load-bearing,
        # just avoids a version-specific signature mismatch
        if not stop_requested.is_set():
            print(f"[alpaca] connection closed unexpectedly {args} -- "
                 f"the supervisor below will reconnect")

    def resub(new_syms):
        # dashboard reconfigured the slots mid-session: follow on the feed
        try:
            if ws_holder[0] is not None:
                ws_holder[0].send(json.dumps({"action": "subscribe",
                                              "trades": list(new_syms)}))
        except Exception as e:
            print(f"[alpaca] resubscribe failed: {e}")

    # v3.46 -- A REAL, SEVERE BUG, found in a live paper-trading audit
    # log. This used to be a plain `br.on_symbols_changed = resub`,
    # which silently DISCARDED whatever handler was already installed.
    # order_manager.py installs its own here (v3.44) to re-point the
    # risk overlay at the rebuilt mirror models.
    #
    # Ordering hid it at startup: run_alpaca calls configure_symbols()
    # ABOVE this line, so the first reconfiguration still re-synced and
    # the session's initial symbols behaved correctly. Every LATER
    # reconfiguration -- i.e. adding or swapping symbols mid-session
    # from the dashboard -- fired only resub. om.vwap_models was left
    # pointing at the startup dict, so vwap_models.get(<new symbol>)
    # returned None, peek_stop_price_e4(None) returned 0, and:
    #   * risk sizing degenerated to floor($500 / price), i.e. a flat
    #     notional cap rather than any risk calculation at all, and
    #   * on_position_opened committed stop_price_e4 = 0, so
    #     stop_triggered (price <= stop) could NEVER fire -- those
    #     positions were opened with NO working stop-loss.
    # Confirmed against a real session: five symbols added mid-session
    # all filled at exactly floor($500/price), while a symbol present
    # since startup risk-sized correctly to 807 shares.
    #
    # Chaining instead of overwriting fixes it for every engine. Note
    # that TickEngine additionally makes this class of bug structurally
    # impossible -- it never rebuilds its models dict, so there is
    # nothing to re-sync in the first place -- but the Bridge still
    # rebuilds, so the chain matters for --port sessions.
    _prev_on_symbols_changed = getattr(br, "on_symbols_changed", None)

    def _on_symbols_changed(new_syms):
        resub(new_syms)
        if _prev_on_symbols_changed:
            _prev_on_symbols_changed(new_syms)

    br.on_symbols_changed = _on_symbols_changed

    def supervisor():
        """Runs in the background thread for the entire session. Each
        pass through the loop is one connection's whole lifetime:
        connect, run until it disconnects (run_forever() blocks the
        whole time and only returns once that happens), then either
        stop (if the user asked to) or reconnect after a backoff.
        Backoff grows on REPEATED failures to connect at all (so a
        persistent problem -- bad credentials, Alpaca down -- doesn't
        hammer the endpoint for hours unattended) but resets after any
        connection that stayed up a reasonable while, so a single
        random disconnect during an otherwise-healthy session recovers
        quickly rather than inheriting a stale, escalated delay.
        """
        backoff = reconnect_backoff_s
        while not stop_requested.is_set():
            connected_at[0] = None
            ws = websocket.WebSocketApp(url, on_open=on_open,
                                        on_message=on_message,
                                        on_error=on_error,
                                        on_close=on_close)
            ws_holder[0] = ws
            ws.run_forever(ping_interval=ping_interval_s,
                          ping_timeout=ping_timeout_s)
                                          # blocks for the connection's
                                          # entire lifetime; the ping/
                                          # pong heartbeat is what lets
                                          # this actually RETURN if the
                                          # connection goes silently
                                          # dead rather than cleanly
                                          # closed (see docstring)
            if stop_requested.is_set():
                break
            if (connected_at[0] and
                    (time.monotonic() - connected_at[0])
                    > reconnect_healthy_threshold_s):
                backoff = reconnect_backoff_s   # was healthy a while --
                                          # the next attempt shouldn't
                                          # pay for an old run of failures
            else:
                backoff = min(backoff * 2, reconnect_backoff_max_s)
            print(f"[alpaca] reconnecting in {backoff:.0f}s...")
            stop_requested.wait(backoff)  # interruptible: Ctrl-C during
                                          # the wait exits immediately
                                          # rather than blocking it out

    t = threading.Thread(target=supervisor, daemon=True)
    t.start()
    print("[alpaca] running — Ctrl-C to stop")
    try:
        while True:
            br.pump(timeout=0.2)
    except KeyboardInterrupt:
        stop_requested.set()              # tells the supervisor this
                                          # close is intentional, not a
                                          # disconnect to recover from
        if ws_holder[0] is not None:
            ws_holder[0].close()


# ---------------------------------------------------------------------------
