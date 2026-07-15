# Changelog

Each entry below was one development "drop". Going forward, every drop is
one git commit tagged `vX.Y` (see GITHUB_GUIDE.md). Drops 1–9 predate the
repository and are recorded here; their combined state is the initial
commit, tagged v1.0.

| Drop | Tag (retroactive) | Contents | Checks |
|------|------|----------|--------|
| 1 | — | RX layer: uart_rx, tick_parser, timestamp_us, top_arty, XDC, bit-level TB. Pin-name correction (uart_txd_in/A9). | 31 |
| 2 | — | TX/echo layer: uart_tx, sync_fifo, frame_tx, 30-byte echo frame, drop counting. Fixed a real 1-cycle arrival-timestamp race (pending_us). | 20 |
| 3 | — | SMA indicator engine + priority signal FIFO + 0x83 frames. Mirror-model TB incl. random walk (LCG low-bit lesson). | 850 + 14 |
| 4 | — | Host bridge: tick_protocol, SMAMirror, FrameParser, fpga_emulator (pty virtual board), sim/selftest/alpaca sources, SignalVerifier. | 37 |
| 5 | — | Order manager: RiskPolicy, latching kill switch, broker reconciliation, MockBroker + Alpaca paper REST (stdlib), audit JSONL. | 25 |
| 6 | — | Costs & tax: SEC §31 + FINRA TAF fee schedule, 2026 bracket engine, NIIT, CostTracker, after-tax summary. | 30 |
| 7 | — | Live-mode interlocks: six independent gates, separate live creds, mandatory daily-loss halt, two-key confirmation. | 21 |
| 8 | — | Web console: zero-dep dashboard, scope chart, LED strip, guarded kill, /api/state. | 23 |
| 9 | — | EMA engine (extended-precision leaky integrator) + collision arbiter + 0x84 frames + EMAMirror + StrategyScorecard comparison. | 1900 + 23 (+host updates) |

**v1.0** — initial repository commit: all of the above. 2990 checks / 0 failures.

**v1.1** — selftest fixed for dual-strategy boards (per-strategy model
comparison + DIAG failure fingerprints); G5 regression covers selftest
itself.

**v2.5.2** — fixed --years defaulting --end to "today", which
free-tier SIP access rejects with HTTP 403 ("subscription does not
permit querying recent SIP data") -- found via a real report.
SIP_RECENT_BUFFER_DAYS(=2) now backs the auto-computed default off
from today; an EXPLICIT --end within that buffer still gets a loud
warning before attempting, rather than a surprise 403. The 403 itself
now raises an actionable message (back --end off / use --feed iex)
instead of a raw JSON blob, and is correctly NOT retried (an
entitlement rejection isn't transient -- retrying it wastes the
backoff schedule for nothing). 6 new checks reproducing the exact
reported error and proving the fix, 320 total across the host suite,
0 failures.

**v2.5.1** — fixed a real data-loss-by-silence bug: checkpoint/data
files were keyed by SYMBOL ALONE, with no awareness of the requested
date range. Fetching a WIDER range after an earlier narrower fetch had
already completed silently returned the OLD checkpoint's "done" status
without fetching anything new for the wider range -- found via a real
report (asked for 2026-01-01..07-01 after an earlier 2026-06-01..07-01
smoke test; got only the narrow month back). Fixed at the root: output
is now <symbol>_<start>_<end>.trades.jsonl, so two different requested
ranges are structurally two different files and can never collide on
one "done" flag. Companion fix: backtest.py's --trades now accepts
multiple comma-separated files, replayed as one continuous chronological
history (with an out-of-order/overlap sanity check) -- so incrementally
fetched ranges can be COMBINED at backtest time instead of needing one
ever-widening download. 6 new checks (the exact reported scenario,
reproduced and proven fixed) + 4 for multi-file replay, 316 total
across the host suite, 0 failures. Old <symbol>.trades.jsonl /
<symbol>.checkpoint.json files from before this fix are orphaned under
the new naming -- delete and refetch.

**v2.5** — three real speedups for the historical downloader, one
architectural constraint made explicit rather than worked around:
  * HARD LIMIT (not a lever): Alpaca's trades pagination is cursor-based
    -- page N+1's URL isn't known until page N's response arrives with
    its next_page_token, so pages within ONE symbol cannot be
    parallelized. Stated plainly rather than pretending around it.
  * keep-alive: one persistent HTTP(S) connection per symbol, reused
    across every page, instead of a fresh TCP+TLS handshake per
    request. Verified with a test asserting only ONE distinct client
    connection is seen across a multi-page fetch.
  * concurrency ACROSS symbols: --symbols SPY,QQQ,... now fetches
    independently-paginated symbols in parallel worker threads sharing
    ONE thread-safe RateLimiter, so combined dispatch rate across every
    worker still respects a single account-wide cap. Verified faster
    than serial for the same work, with per-symbol output proven
    complete and uncorrupted despite concurrent execution.
  * retry-with-backoff on 429/5xx/dead-connection made it safe to raise
    the default rate limit from 150 to 180/min (closer to the 200/min
    ceiling) without risking an unhandled crash losing hours of
    progress to one transient blip.
12 new checks (30 total in the downloader suite), 306 total across the
host suite, 0 failures.

**v2.4.1** — fixed comparison_report() hiding a gated card's own
block-reason breakdown whenever it happened to be labeled [LIVE]. In a
real live session the traded row has no policy attached (its numbers
come from real fills) so this never fired there — but in a backtest
BOTH rows are gated replays, and hiding the [LIVE]-labeled one's
breakdown hid exactly the most useful diagnostic. Found immediately on
a real 5-year, 15.3M-trade SPY backtest. 1 new check, 294 total, 0
failures.

**v2.4** — historical backtesting. `fetch_historical_trades.py`:
resumable, checkpointed, rate-limited downloader for Alpaca's raw
historical trades REST endpoint (mechanics verified against a local
mock server — real network access to Alpaca isn't available in this
dev environment, so the live pull must be validated on real hardware
with real keys). `backtest.py`: replays downloaded trades through the
UNMODIFIED SMAMirror/EMAMirror/StrategyScorecard/RiskPolicy classes —
not a reimplementation, so results are guaranteed consistent with live
verification. RiskPolicy gained an injectable clock (now_fn, defaults
to real wall-clock, live behavior unchanged) so cooldown and daily-cap
gating evaluate against each trade's OWN historical timestamp instead
of real elapsed time during replay — without this, a multi-year replay
finishing in seconds would never trigger cooldown or day-rollover
correctly. 35 new checks (backtest engine + downloader mechanics), 293
total across the host suite, 0 failures.

**v2.3** — baud rate raised to 921600 (was 115200), both sides. RTL
default changed in top_arty.sv (100MHz/921600 rounds to 108 clocks/bit,
actual ~925.9kHz, +0.469% — same category of effect as the sim BAUD
speedup trick, well within normal UART tolerance). Host: --baud flag
added to order_manager.py and bridge.py's CLI (default 921600, override
to 115200 for any bitstream built before this change). Independent of
which Alpaca data plan is active — done now, at low free-tier IEX
volume, specifically to validate the higher rate on REAL hardware
before scaling up data volume, isolating that variable from any future
plan change. 258 checks still passing (unaffected — the emulator uses
a pty, which has no baud concept). BITSTREAM REBUILD REQUIRED;
re-run selftest with --baud 921600 to confirm on real hardware before
trusting it live.

**stress_test.py** (unversioned tooling addition) — synthetic 8-symbol
throughput benchmark against the emulator. Sweeps aggregate tick rate,
reports achieved rate / echo-loss / divergences / RTT at each level, and
finds the highest rate that stayed healthy. Also reproduces the real
kill-switch incident's failure SHAPE at 8-symbol scale (unpaced burst
across all 8 slots) to confirm v2.2.1's fix holds at larger scale.
Optional --dashboard flag adds real HTTP-polling load (matching the
frontend's exact 500ms cadence) for a more representative number. This
is a BENCHMARK, not a correctness test — the ceiling it finds is
specific to the machine running it; rerun on your actual hardware
before trusting the number. Explicitly does NOT validate wire-level
baud pacing (a pty doesn't enforce that) — only the host's real
processing throughput, independent of serial speed.

**v2.2.1** — fixed a real divergence bug found on live 2-symbol session
data: SignalVerifier's grace window was one echo counter SHARED across
every (strategy, symbol) verifier, so a burst of one symbol's activity
could expire a different symbol's pending signal before its own match
ever arrived. Now each verifier is stamped and expired against that
SYMBOL's OWN echo count. Reproduces the exact failure mode (a slow
symbol's signal surviving an unrelated fast symbol's burst) as a
regression test. 9 new checks, 258 total across the host suite, 0
failures. Host-only, no rebuild.

**v2.2** — third strategy: a weekly-anchored buy-the-dip ladder
(`ladder_strategy.py`), score-only (never trades, regardless of
--strategy). Buys in tranches as price falls below a baseline re-anchored
weekly (4 baseline algorithms: Friday close, week-average close,
week-VWAP, week-midpoint), sells the full position on a recovery above
baseline, tracking a weighted-average cost basis across levels. No
cooldown needed — the level index itself provides hysteresis. Slots into
the existing comparison_report() unmodified. Deliberately no stop-loss/
drawdown cap yet (explicit choice, noted in the module docstring as a
grid/martingale-style risk to be aware of). 46 new checks, 249 total
across the host suite, 0 failures. Host-only, no rebuild.

**v2.1** — fair strategy comparison: the untraded strategy's scorecard now
replays through its own RiskPolicy clone (same RiskLimits, same wall
clock) instead of turning every verified signal into an unthrottled
hypothetical trade. Traded strategy's row is now populated from the OM's
real CostTracker fills, not simulated at all. Fixes an ~80x trading-
frequency mismatch discovered on real session data (794 signals producing
397 naive hypothetical trips vs 10 real gated fills). 93 host checks in
test_host.py alone. No RTL or wire-format changes.

**v2.0** — runtime symbol configuration. Wire format v2 (6-byte symbols for
all S&P 500 tickers; 24/32-byte frames; TYPE 0x10 slot writes ACKed by 0x90
echoes). 8-slot register file + 8x both engines + priority encoders in
fabric; slot writes reset slot engine state. Per-symbol models, verifiers,
positions, costs, scorecards; multi-symbol sim + Alpaca sources; GUI slot
editor. 3030 checks / 0 failures. BITSTREAM REBUILD REQUIRED.
