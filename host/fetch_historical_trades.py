#!/usr/bin/env python3
"""
fetch_historical_trades.py — resumable downloader for raw historical
trades, via Alpaca's /v2/stocks/{symbol}/trades REST endpoint.

    python3 fetch_historical_trades.py --symbols SPY,QQQ --years 5

WHY THIS IS RESUMABLE, NOT A ONE-SHOT SCRIPT: raw tick-level history for
a liquid symbol over years is enormous — potentially hundreds of
millions of individual prints, paginated at up to 10,000 records per
call. Even fast, this can be a long-running job. A script that holds
everything in memory and fails partway wastes all of it. This one:

  * writes each page's trades to disk immediately (append-only JSONL,
    one file per symbol) — a killed process loses at most one page
  * checkpoints (symbol, page_token, counts) after every page, so
    rerunning the same command resumes instead of re-downloading
  * paces itself under the account-wide rate limit with a safety
    margin, shared correctly across concurrent workers

FILE NAMING IS SCOPED TO THE ACTUAL RANGE REQUESTED: output is
<symbol>_<start>_<end>.trades.jsonl, not just <symbol>.trades.jsonl.
This matters — an earlier version keyed checkpoints by symbol alone,
so fetching SPY for a wide range after already having a "done"
checkpoint from a narrower SPY range silently returned the OLD,
narrower data without fetching anything new: the checkpoint's own
`done: True` from the first run short-circuited the second run before
it ever looked at what range was actually being asked for. Range-
scoped filenames make that class of bug structurally impossible: two
different (symbol, start, end) requests are now just two different
files, never colliding on one "done" flag. If you have old
<symbol>.trades.jsonl / <symbol>.checkpoint.json files from before
this fix, they're orphaned under the new scheme — delete them and
refetch under the range you actually want.

THREE SPEED LEVERS, and one hard limit that ISN'T a lever:
  * HARD LIMIT: pagination is cursor-based — page N+1's URL literally
    isn't known until page N's response arrives with its
    next_page_token. Pages within ONE symbol cannot be parallelized;
    that sequence has a hard floor set by (network latency + the
    shared rate limit), full stop.
  * LEVER 1 — keep-alive: a persistent HTTP(S) connection per symbol
    reused across every page removes a full TCP+TLS handshake from
    every single request instead of paying it every time.
  * LEVER 2 — concurrency ACROSS symbols: different symbols' page
    sequences are fully independent, so --symbols SPY,QQQ,... runs one
    worker thread per symbol (bounded by --max-workers), all sharing
    ONE thread-safe RateLimiter so the combined dispatch rate across
    every worker still never exceeds the account-wide cap.
  * LEVER 3 — retry-with-backoff on 429/5xx/connection failures makes
    it safe to raise the rate limiter closer to the true ceiling
    (default raised to 180/min, from 150) instead of leaving a large
    margin purely to avoid an unhandled crash — a transient rate-limit
    hit now backs off and retries instead of losing the whole run.

WHAT THIS DOES NOT DO: talk to the real Alpaca API from an environment
without network access to it. All of the above is verified here against
a local mock server (see test_fetch_historical_trades.py), including
keep-alive actually reusing one TCP connection, concurrent workers
producing correct non-corrupted per-symbol output, and retry-then-
succeed on a simulated 429. That proves the logic is correct, not that
it has been run against the real endpoint. Run this for real on your
own machine, with your own keys, and watch the first few pages before
walking away from a long pull.

Historical data note (verified against Alpaca's own docs): the
free/paid split is about REAL-TIME recency, not historical depth — data
older than 15 minutes is accessible on all feeds regardless of plan.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


DATA_HOST = "data.alpaca.markets"
DATA_PATH = "/v2/stocks/{symbol}/trades"

# Alpaca's docs describe the free-tier SIP restriction as "data older
# than 15 minutes is accessible on all feeds" -- but the ACTUAL 403
# ("subscription does not permit querying recent SIP data") observed in
# practice suggests the real enforced boundary is coarser than a strict
# 15-minute wall (settlement lag, end-of-day batching, or simply a more
# conservative server-side check than the docs describe precisely). A
# multi-day buffer is safe, simple, and costs nothing for a backtest --
# missing the most recent couple of days is irrelevant when the point
# is years of statistical history, not up-to-the-minute data (that's
# what the live bridge is for).
SIP_RECENT_BUFFER_DAYS = 2


class RateLimiter:
    """Thread-safe pacer: sleeps as needed to stay under `per_minute`
    calls/min IN AGGREGATE across every caller, so multiple concurrent
    symbol-workers sharing one instance never collectively exceed the
    account-wide limit, no matter how they're interleaved."""

    def __init__(self, per_minute: int = 180):
        self.min_interval = 60.0 / per_minute
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()


class Connection:
    """A reused HTTP(S) keep-alive connection, one per symbol-worker
    (http.client connections are NOT thread-safe to share). Auto-
    reconnects if the server closes the connection (idle timeout, a
    retried request after a dropped connection, etc.) — the whole point
    is durability across a long-running fetch, not a fragile fast path."""

    def __init__(self, base_url: str, timeout: float = 20.0):
        parts = urlsplit(base_url)
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port
        self.path_prefix = parts.path
        self.timeout = timeout
        self._conn: http.client.HTTPConnection | None = None

    def _connect(self):
        cls = (http.client.HTTPSConnection if self.scheme == "https"
              else http.client.HTTPConnection)
        self._conn = cls(self.host, self.port, timeout=self.timeout)

    def get(self, symbol: str, query: str, headers: dict
           ) -> tuple[int, bytes]:
        path = self.path_prefix.format(symbol=symbol) + "?" + query
        for attempt in (1, 2):        # one silent reconnect-and-retry
            if self._conn is None:
                self._connect()
            try:
                self._conn.request("GET", path, headers=headers)
                resp = self._conn.getresponse()
                body = resp.read()
                return resp.status, body
            except (http.client.HTTPException, OSError):
                self._conn = None     # connection was stale/dead: retry once
                if attempt == 2:
                    raise

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


def _get_with_retry(conn: Connection, symbol: str, query: str, key: str,
                    secret: str, max_attempts: int = 5) -> dict:
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            status, body = conn.get(symbol, query, headers)
        except (http.client.HTTPException, OSError) as e:
            # a connection that's genuinely dead (not just idle-stale --
            # Connection.get() already silently reconnects once for
            # that case), e.g. the server crashed or the network
            # dropped. Same backoff-and-retry treatment as a 429: a
            # multi-hour run shouldn't die to one transient blip.
            if attempt == max_attempts:
                raise RuntimeError(
                    f"connection failed after {max_attempts} attempts: {e}")
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue

        if status == 200:
            return json.loads(body)
        if status == 429 or 500 <= status < 600:
            # transient: rate-limited or a server-side hiccup. Back off
            # and retry rather than losing an hours-long run to one blip.
            if attempt == max_attempts:
                raise RuntimeError(
                    f"HTTP {status} after {max_attempts} attempts: "
                    f"{body[:300]!r}")
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
        if status == 403 and b"recent SIP data" in body:
            raise RuntimeError(
                f"HTTP 403: your account's subscription doesn't permit "
                f"querying RECENT data on the SIP feed (this is a "
                f"free-tier restriction, not a bug) — back --end off by "
                f"a few more days from today, or pass --feed iex for "
                f"the recent slice specifically. Raw response: "
                f"{body[:200]!r}")
        raise RuntimeError(f"HTTP {status}: {body[:300]!r}")   # not retryable
    raise RuntimeError("unreachable")


def checkpoint_path(symbol: str, outdir: str, start: str, end: str) -> str:
    return os.path.join(outdir, f"{symbol}_{start}_{end}.checkpoint.json")


def data_path(symbol: str, outdir: str, start: str, end: str) -> str:
    return os.path.join(outdir, f"{symbol}_{start}_{end}.trades.jsonl")


def load_checkpoint(symbol: str, outdir: str, start: str, end: str
                    ) -> dict | None:
    p = checkpoint_path(symbol, outdir, start, end)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def save_checkpoint(symbol: str, outdir: str, start: str, end: str,
                    state: dict):
    p = checkpoint_path(symbol, outdir, start, end)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, p)          # write-then-rename: a crash never corrupts it


def fetch_symbol(symbol: str, start: str, end: str, key: str, secret: str,
                 outdir: str, feed: str = "sip", limit: int = 10_000,
                 rate: RateLimiter | None = None, resume: bool = True,
                 base_url: str = f"https://{DATA_HOST}{DATA_PATH}",
                 max_pages: int | None = None,
                 max_attempts: int = 5, timeout: float = 20.0) -> dict:
    """Fetch all trades for `symbol` in [start, end] over ONE reused
    keep-alive connection, appending to
    <outdir>/<symbol>_<start>_<end>.trades.jsonl, checkpointing after every page. `base_url` is overridable for
    testing against a local mock server (note the scheme in base_url
    picked HTTP/HTTPS above — swap it for a real https:// Alpaca URL)."""
    os.makedirs(outdir, exist_ok=True)
    rate = rate or RateLimiter()

    ck = load_checkpoint(symbol, outdir, start, end) if resume else None
    page_token = ck.get("page_token") if ck else None
    pages_done = ck.get("pages_done", 0) if ck else 0
    trades_done = ck.get("trades_done", 0) if ck else 0
    if ck and ck.get("done"):
        print(f"[{symbol}] checkpoint says already complete "
             f"({trades_done} trades, {pages_done} pages) — "
             f"delete the .checkpoint.json to refetch")
        return ck

    mode = "a" if (ck and page_token) else "w"
    out = open(data_path(symbol, outdir, start, end), mode)
    conn = Connection(base_url, timeout=timeout)
    print(f"[{symbol}] {'resuming' if page_token else 'starting'}: "
         f"{trades_done} trades / {pages_done} pages so far")

    try:
        while True:
            if max_pages is not None and pages_done >= max_pages:
                break
            query = f"start={start}&end={end}&limit={limit}&feed={feed}"
            if page_token:
                query += f"&page_token={page_token}"

            rate.wait()
            data = _get_with_retry(conn, symbol, query, key, secret,
                                   max_attempts=max_attempts)
            trades = data.get("trades", [])
            for t in trades:
                out.write(json.dumps(t) + "\n")
            out.flush()

            trades_done += len(trades)
            pages_done += 1
            page_token = data.get("next_page_token")
            save_checkpoint(symbol, outdir, start, end, {
                "symbol": symbol, "start": start, "end": end,
                "page_token": page_token, "pages_done": pages_done,
                "trades_done": trades_done, "done": page_token is None})

            if pages_done % 20 == 0 or page_token is None:
                print(f"[{symbol}] {pages_done} pages, "
                     f"{trades_done} trades so far...")
            if page_token is None:
                break
    finally:
        out.close()
        conn.close()

    print(f"[{symbol}] done: {trades_done} trades across {pages_done} pages "
         f"-> {data_path(symbol, outdir, start, end)}")
    return {"symbol": symbol, "trades_done": trades_done,
            "pages_done": pages_done, "done": page_token is None}


def fetch_symbols_concurrent(symbols: list[str], start: str, end: str,
                            key: str, secret: str, outdir: str,
                            feed: str = "sip", rate_per_min: int = 180,
                            max_workers: int | None = None,
                            resume: bool = True,
                            base_url: str = f"https://{DATA_HOST}{DATA_PATH}"
                            ) -> dict[str, dict]:
    """Fetch several symbols concurrently — safe because each symbol's
    page sequence is independent, and every worker shares ONE
    RateLimiter, so the combined dispatch rate across all of them still
    respects the single account-wide cap. This is the lever that
    actually helps a multi-symbol pull; it does nothing for a single
    symbol, since that sequence is inherently serial (see module
    docstring)."""
    rate = RateLimiter(rate_per_min)         # ONE shared limiter
    max_workers = max_workers or min(len(symbols), 8)
    results: dict[str, dict] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers) as pool:
        futures = {
            pool.submit(fetch_symbol, sym, start, end, key, secret, outdir,
                       feed=feed, rate=rate, resume=resume,
                       base_url=base_url): sym
            for sym in symbols
        }
        for fut in concurrent.futures.as_completed(futures):
            sym = futures[fut]
            results[sym] = fut.result()     # re-raises on failure
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None, help="single symbol")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated, fetched CONCURRENTLY "
                         "(e.g. SPY,QQQ) — has no effect on how fast a "
                         "single symbol downloads, only helps when you "
                         "have more than one")
    ap.add_argument("--years", type=float, default=None)
    ap.add_argument("--start", default=None, help="ISO date, overrides --years")
    ap.add_argument("--end", default=None, help="ISO date, default: today")
    ap.add_argument("--outdir", default="./historical_trades")
    ap.add_argument("--feed", default="sip", choices=["sip", "iex"])
    ap.add_argument("--rate-per-min", type=int, default=180,
                    help="raised from 150: retry-with-backoff on "
                         "429/5xx now makes it safe to run closer to "
                         "the real 200/min ceiling")
    ap.add_argument("--max-workers", type=int, default=None)
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("ALPACA_KEY")
    secret = os.environ.get("ALPACA_SECRET")
    if not key or not secret:
        raise SystemExit("Set ALPACA_KEY / ALPACA_SECRET first (see README).")

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
              if args.symbols else ([args.symbol.upper()] if args.symbol
                                    else None))
    if not symbols:
        raise SystemExit("Pass --symbol SPY or --symbols SPY,QQQ")

    if args.end:
        end = args.end
        # user gave an explicit end -- respect it, but warn loudly if it
        # looks likely to hit the same recency restriction, so the 403
        # (if it happens) isn't a surprise
        end_dt = datetime.fromisoformat(end).date()
        today = datetime.now(timezone.utc).date()
        if args.feed == "sip" and (today - end_dt).days < SIP_RECENT_BUFFER_DAYS:
            print(f"[fetch] WARNING: --end {end} is only "
                 f"{(today - end_dt).days} day(s) before today, and "
                 f"--feed sip does not permit querying recent data. "
                 f"This will very likely 403. Back --end off by a few "
                 f"more days, or pass --feed iex for the recent slice.")
    else:
        # no explicit --end: default to a SAFELY historical date rather
        # than "today", which reliably 403s on the SIP feed (see
        # SIP_RECENT_BUFFER_DAYS above)
        end = (datetime.now(timezone.utc)
              - timedelta(days=SIP_RECENT_BUFFER_DAYS)).date().isoformat()
    if args.start:
        start = args.start
    elif args.years:
        start = (datetime.now(timezone.utc)
                - timedelta(days=int(args.years * 365.25))).date().isoformat()
    else:
        raise SystemExit("Pass --years or --start")

    real_url = f"https://{DATA_HOST}{DATA_PATH}"
    print(f"[fetch] {symbols}  {start} .. {end}  feed={args.feed}  "
         f"rate<={args.rate_per_min}/min"
         + (f"  workers={args.max_workers or min(len(symbols), 8)}"
            if len(symbols) > 1 else ""))
    print(f"[fetch] resumable: Ctrl+C any time, rerun the same command "
         f"to continue from the checkpoint")

    if len(symbols) == 1:
        fetch_symbol(symbols[0], start, end, key, secret, args.outdir,
                    feed=args.feed, rate=RateLimiter(args.rate_per_min),
                    resume=not args.no_resume, base_url=real_url)
    else:
        fetch_symbols_concurrent(symbols, start, end, key, secret,
                                args.outdir, feed=args.feed,
                                rate_per_min=args.rate_per_min,
                                max_workers=args.max_workers,
                                resume=not args.no_resume, base_url=real_url)


if __name__ == "__main__":
    main()
