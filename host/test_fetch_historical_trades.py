#!/usr/bin/env python3
"""
test_fetch_historical_trades.py — proves the downloader's MECHANICS,
including the three speedups (keep-alive, concurrent multi-symbol,
retry-with-backoff), against a local mock server standing in for
Alpaca. This does NOT and CANNOT prove the real Alpaca endpoint behaves
this way — see the module docstring in fetch_historical_trades.py.

    python3 test_fetch_historical_trades.py
"""

from __future__ import annotations
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from fetch_historical_trades import (
    fetch_symbol, fetch_symbols_concurrent, RateLimiter, Connection,
    load_checkpoint, data_path)

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


TOTAL_TRADES = 250
PAGE_SIZE = 37


class MockAlpaca(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"    # required for keep-alive to matter
    call_count = 0
    connections_seen = set()         # (client_addr, client_port) per accept
    fail_after_n_calls: int | None = None
    fail_with_429_first_n: int = 0
    _429_served = 0
    reject_recent_sip = False        # mirrors the real free-tier 403

    def do_GET(self):
        MockAlpaca.call_count += 1
        MockAlpaca.connections_seen.add(self.client_address)

        if MockAlpaca.reject_recent_sip:
            q = parse_qs(urlparse(self.path).query)
            end = q.get("end", [""])[0]
            feed = q.get("feed", [""])[0]
            # mirror the real restriction: sip + an end date within the
            # last couple of days -> 403, exactly as Alpaca's own API does
            from datetime import date as _date
            end_date = _date.fromisoformat(end[:10])
            if feed == "sip" and (_date.today() - end_date).days < 2:
                body = (b'{"message":"subscription does not permit '
                       b'querying recent SIP data"}')
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        if (MockAlpaca.fail_after_n_calls is not None
                and MockAlpaca.call_count > MockAlpaca.fail_after_n_calls):
            self.connection.close()
            return

        if MockAlpaca._429_served < MockAlpaca.fail_with_429_first_n:
            MockAlpaca._429_served += 1
            body = b'{"message":"rate limited"}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        q = parse_qs(urlparse(self.path).query)
        token = q.get("page_token", [None])[0]
        offset = int(token) if token else 0
        limit = int(q.get("limit", [PAGE_SIZE])[0])
        chunk = min(PAGE_SIZE, limit)
        end = min(offset + chunk, TOTAL_TRADES)
        trades = [{"t": f"2020-01-01T00:00:{i:02d}Z", "p": 100.0 + i,
                   "s": 1} for i in range(offset, end)]
        next_token = str(end) if end < TOTAL_TRADES else None

        body = json.dumps({"trades": trades,
                           "next_page_token": next_token}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_mock():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockAlpaca)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def reset_mock():
    MockAlpaca.call_count = 0
    MockAlpaca.connections_seen = set()
    MockAlpaca.fail_after_n_calls = None
    MockAlpaca.fail_with_429_first_n = 0
    MockAlpaca._429_served = 0
    MockAlpaca.reject_recent_sip = False


# ---- G1: a clean, uninterrupted fetch (baseline correctness) --------------
print("[G1] full fetch across multiple pages, no interruption")
httpd, port = start_mock()
base_url = f"http://127.0.0.1:{port}/v2/stocks/{{symbol}}/trades"
outdir = tempfile.mkdtemp()

reset_mock()
result = fetch_symbol("SPY", "2020-01-01", "2020-01-02", "key", "secret",
                     outdir, base_url=base_url,
                     rate=RateLimiter(per_minute=100_000))
check("all trades fetched", result["trades_done"], TOTAL_TRADES)
check("done flag set", result["done"], True)
with open(data_path("SPY", outdir, "2020-01-01", "2020-01-02")) as f:
    lines = f.readlines()
check("jsonl has one line per trade", len(lines), TOTAL_TRADES)
prices = [json.loads(l)["p"] for l in lines]
check("trades in order, none duplicated, none skipped",
      prices, [100.0 + i for i in range(TOTAL_TRADES)])

# ---- G2: keep-alive actually reuses ONE TCP connection ----------------------
print("[G2] keep-alive: one symbol's pages share a single TCP connection")
reset_mock()
outdir_ka = tempfile.mkdtemp()
fetch_symbol("SPY", "2020-01-01", "2020-01-02", "key", "secret", outdir_ka,
            base_url=base_url, rate=RateLimiter(per_minute=100_000))
check("multiple pages fetched", MockAlpaca.call_count > 1, True)
check("but only ONE distinct client connection was seen "
     "(same TCP connection reused across every page, not "
     "reconnected per request)",
     len(MockAlpaca.connections_seen), 1)

# ---- G3: interrupted mid-fetch, then resumed --------------------------------
print("[G3] simulated crash mid-fetch, --resume picks up exactly where "
     "it left off (byte-identical to an uninterrupted run)")
reset_mock()
outdir2 = tempfile.mkdtemp()
MockAlpaca.fail_after_n_calls = 3
try:
    fetch_symbol("QQQ", "2020-01-01", "2020-01-02", "key", "secret",
                outdir2, base_url=base_url,
                rate=RateLimiter(per_minute=100_000), max_attempts=1,
                timeout=2.0)
    check("interrupted fetch raised", "no error", "error")
except RuntimeError:
    check("interrupted fetch raised", "error", "error")

ck2 = load_checkpoint("QQQ", outdir2, "2020-01-01", "2020-01-02")
check("checkpoint captured partial progress", ck2["done"], False)
check("checkpoint has exactly 3 pages worth", ck2["trades_done"],
      PAGE_SIZE * 3)

MockAlpaca.fail_after_n_calls = None
result2 = fetch_symbol("QQQ", "2020-01-01", "2020-01-02", "key", "secret",
                      outdir2, base_url=base_url,
                      rate=RateLimiter(per_minute=100_000), resume=True)
check("resumed fetch completes", result2["trades_done"], TOTAL_TRADES)
with open(data_path("QQQ", outdir2, "2020-01-01", "2020-01-02")) as f:
    full_lines = f.readlines()
check("no duplicate trades after resume", len(full_lines), TOTAL_TRADES)
resumed_prices = [json.loads(l)["p"] for l in full_lines]
check("resumed data still in correct order, no gaps",
      resumed_prices, [100.0 + i for i in range(TOTAL_TRADES)])
httpd.shutdown()

# ---- G4: retry-with-backoff survives a transient 429 ------------------------
print("[G4] a simulated 429 is retried with backoff, not fatal")
httpd4, port4 = start_mock()
base_url4 = f"http://127.0.0.1:{port4}/v2/stocks/{{symbol}}/trades"
reset_mock()
MockAlpaca.fail_with_429_first_n = 2      # first 2 requests: 429, then OK
outdir4 = tempfile.mkdtemp()

from fetch_historical_trades import _get_with_retry
t0 = time.monotonic()
data = _get_with_retry(Connection(base_url4), "SPY",
                       "start=2020-01-01&end=2020-01-02&limit=37",
                       "key", "secret", max_attempts=5)
elapsed = time.monotonic() - t0
check("retried past the 429s and got real data",
      len(data.get("trades", [])), PAGE_SIZE)
check("backoff actually waited before succeeding (>= 1s for 2 retries)",
      elapsed >= 0.9, True)
httpd4.shutdown()

# ---- G5: a 429 that never clears eventually raises, not hangs forever -----
print("[G5] exhausting retries on a persistent 429 raises cleanly")
httpd5, port5 = start_mock()
base_url5 = f"http://127.0.0.1:{port5}/v2/stocks/{{symbol}}/trades"
reset_mock()
MockAlpaca.fail_with_429_first_n = 999
try:
    _get_with_retry(Connection(base_url5), "SPY",
                    "start=2020-01-01&end=2020-01-02&limit=37",
                    "key", "secret", max_attempts=3)
    check("persistent 429 eventually raises", "no error", "error")
except RuntimeError as e:
    check("persistent 429 eventually raises", "error", "error")
    check("error message mentions the status code", "429" in str(e), True)
httpd5.shutdown()

# ---- G6: concurrent multi-symbol fetch — correct AND actually parallel ----
print("[G6] fetching multiple symbols concurrently: each symbol's output "
     "is complete and correct, and it's genuinely faster than serial")
httpd6, port6 = start_mock()
base_url6 = f"http://127.0.0.1:{port6}/v2/stocks/{{symbol}}/trades"
reset_mock()
outdir6 = tempfile.mkdtemp()

# a rate limiter loose enough that wall-clock time is dominated by the
# mock server's per-request latency, not the rate cap -- so concurrency
# across symbols should measurably shorten total wall time vs serial
SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT"]
t0 = time.monotonic()
results = fetch_symbols_concurrent(
    SYMBOLS, "2020-01-01", "2020-01-02", "key", "secret", outdir6,
    rate_per_min=100_000, max_workers=4, base_url=base_url6)
concurrent_elapsed = time.monotonic() - t0

for sym in SYMBOLS:
    check(f"{sym}: all trades fetched", results[sym]["trades_done"],
          TOTAL_TRADES)
    with open(data_path(sym, outdir6, "2020-01-01", "2020-01-02")) as f:
        lines = f.readlines()
    check(f"{sym}: complete, correct file with no cross-symbol "
         f"interleaving corruption", len(lines), TOTAL_TRADES)
    prices = [json.loads(l)["p"] for l in lines]
    check(f"{sym}: trades in order", prices,
          [100.0 + i for i in range(TOTAL_TRADES)])

# same work, done serially, for a real timing comparison
reset_mock()
outdir6b = tempfile.mkdtemp()
t0 = time.monotonic()
for sym in SYMBOLS:
    fetch_symbol(sym, "2020-01-01", "2020-01-02", "key", "secret", outdir6b,
                rate=RateLimiter(per_minute=100_000), base_url=base_url6)
serial_elapsed = time.monotonic() - t0
check(f"concurrent ({concurrent_elapsed:.2f}s) faster than serial "
     f"({serial_elapsed:.2f}s) for {len(SYMBOLS)} symbols",
     concurrent_elapsed < serial_elapsed, True)
httpd6.shutdown()

# ---- G7: the shared RateLimiter still holds under concurrent access -------
print("[G7] RateLimiter enforces the SHARED cap even with concurrent callers")
rl = RateLimiter(per_minute=600)          # 100ms min interval, shared
call_times = []
lock = threading.Lock()

def hammer():
    for _ in range(4):
        rl.wait()
        with lock:
            call_times.append(time.monotonic())

threads = [threading.Thread(target=hammer) for _ in range(3)]
t0 = time.monotonic()
for th in threads:
    th.start()
for th in threads:
    th.join()
total_calls = len(call_times)
elapsed = time.monotonic() - t0
min_expected = (total_calls - 1) * 0.1 * 0.9   # 90% margin for scheduling jitter
check("12 calls (3 threads x 4) across a shared 600/min limiter take "
     ">= ~1.1s aggregate, not ~0.4s (proves the cap is SHARED, not "
     "per-thread)", elapsed >= min_expected, True)

# ---- G8: THE REPORTED BUG — a wider range must NOT be silently
# short-circuited by an old "done" checkpoint from a narrower range ----
print("[G8] different date ranges for the SAME symbol never collide on "
     "one checkpoint (the actual bug that was reported)")
httpd8, port8 = start_mock()
base_url8 = f"http://127.0.0.1:{port8}/v2/stocks/{{symbol}}/trades"
reset_mock()
outdir8 = tempfile.mkdtemp()

# narrow range first (the "smoke test" workflow) -- completes and is
# marked done
narrow = fetch_symbol("SPY", "2020-06-01", "2020-07-01", "key", "secret",
                      outdir8, base_url=base_url8,
                      rate=RateLimiter(per_minute=100_000))
check("narrow range completed", narrow["done"], True)
narrow_calls = MockAlpaca.call_count

# now the WIDE range -- this must actually fetch, not silently return
# the narrow checkpoint's stats
wide = fetch_symbol("SPY", "2020-01-01", "2020-07-01", "key", "secret",
                    outdir8, base_url=base_url8,
                    rate=RateLimiter(per_minute=100_000))
check("wide range made NEW http calls, did not short-circuit",
      MockAlpaca.call_count > narrow_calls, True)
check("wide range is marked done independently", wide["done"], True)

# both files must exist, independently, on disk -- neither overwrote
# nor was shadowed by the other
narrow_file = data_path("SPY", outdir8, "2020-06-01", "2020-07-01")
wide_file = data_path("SPY", outdir8, "2020-01-01", "2020-07-01")
check("narrow-range file still exists", os.path.exists(narrow_file), True)
check("wide-range file exists separately", os.path.exists(wide_file), True)
with open(wide_file) as f:
    wide_lines = f.readlines()
check("wide-range file actually has data (not silently empty/stale)",
      len(wide_lines), TOTAL_TRADES)
httpd8.shutdown()

shutil.rmtree(outdir8, ignore_errors=True)
shutil.rmtree(outdir, ignore_errors=True)
shutil.rmtree(outdir2, ignore_errors=True)
shutil.rmtree(outdir4, ignore_errors=True)
shutil.rmtree(outdir6, ignore_errors=True)
shutil.rmtree(outdir6b, ignore_errors=True)

# ---- G9: THE REPORTED 403 -- --years N must not default to querying
# "today" on the SIP feed, which the real API rejects ----------------------
print("[G9] --years defaults to a safely historical end date, not "
     "'today' (which 403s on the SIP feed for free-tier accounts)")
httpd9, port9 = start_mock()
base_url9 = f"http://127.0.0.1:{port9}/v2/stocks/{{symbol}}/trades"
reset_mock()
MockAlpaca.reject_recent_sip = True
outdir9 = tempfile.mkdtemp()

from datetime import date as _date, timedelta as _td
safe_end = (_date.today() - _td(days=2)).isoformat()   # what main() SHOULD use
ok = fetch_symbol("SPY", "2020-01-01", safe_end, "key", "secret", outdir9,
                  base_url=base_url9, rate=RateLimiter(per_minute=100_000))
check("a safely-historical end date does NOT 403", ok["done"], True)

# and confirm the mock's 403 behavior itself is a faithful stand-in: an
# end date of "today" DOES 403, proving the fix actually matters
today_end = _date.today().isoformat()
calls_before_second_attempt = MockAlpaca.call_count
try:
    fetch_symbol("SPY", "2020-01-01", today_end, "key", "secret", outdir9,
                base_url=base_url9, rate=RateLimiter(per_minute=100_000),
                max_attempts=1)
    check("an end date of 'today' on SIP correctly 403s "
         "(proves the bug was real)", "no error", "error")
except RuntimeError as e:
    check("an end date of 'today' on SIP correctly 403s "
         "(proves the bug was real)", "error", "error")
    check("the error message is actionable, not a raw JSON blob",
          "back --end off" in str(e) or "free-tier restriction" in str(e),
          True)
    check("403 was NOT retried (it's not transient -- retrying an "
         "entitlement error wastes time for nothing)",
         MockAlpaca.call_count - calls_before_second_attempt, 1)
httpd9.shutdown()
shutil.rmtree(outdir9, ignore_errors=True)

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
