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

**v3.11** — signal verification grace window changed from a fixed echo
count to real elapsed seconds (--verify-grace-s, default 2.0). The old
count-based design had no fixed real-time meaning; during a burst
(multiple symbols firing, daily cap maxed out, signals piling up), it
was consumed almost instantly -- found after "orphan FPGA signal"
recurred three times in three days, always during high signal volume.
Divergences now also persist full diagnostic detail (symbol, strategy,
wait time, actual signal contents) to the audit log's KILL event,
instead of just a one-line reason -- previously that detail existed in
memory for one moment and was then gone. 19 new checks, 568 total
across the host suite, 0 failures.

**v3.11.1** — fixed a real regression in test_order_manager.py and
test_backtest_results.py: both had a bare relative subprocess path
("order_manager.py" / "backtest.py") that only resolves when invoked
from inside host/ -- already fixed once before (v3.4.1 and v3.0.1
respectively), but reintroduced when a sandbox reset caused these
files to be rebuilt from copies that predated those fixes. Found via
a real report. Also fixed a diagnostic gap: run_session() was
silently discarding subprocess stderr/returncode on failure, turning
a clear root cause into a bare IndexError with no explanation.
Verified from both the repo root and host/ this time. 0 new failures,
same 568 total.
**v3.12** — blended two-sleeve portfolio (VWAP bounce + SMA
profit-gated), plus the fix the 3-year reports demanded: a max-hold
bound on the profit-gated rule. The VTI/QQQ multi-year runs showed
"would realize a loss" as the single largest gated-away reason (1.4M+
on VTI, 4.2M+ on QQQ) — the 100% win rate was pure exit-rule artifact,
and the perpetual "1 open" position carried unbounded unrealized loss
the report structurally couldn't show. ProfitGatedScorecard now takes
max_hold_days (backtest default 5.0; <=0 restores the old unbounded
behavior for comparison): an expired position force-closes at the next
signal's price, even at a loss, through the SAME RiskPolicy gate as
any other sell — win rate is a real number again, forced exits are
counted and flagged in trip_log. Hold time is measured from the first
lot since flat, so averaging down cannot extend a loser. New
blended_strategy.py: each sleeve is the unchanged existing scorecard
with its own RiskPolicy clone from its own carved-down RiskLimits
(VWAP 6sh/$1300, SMA-PG 4sh/$700 by default — re-dividing the same
$2,000 budget, not adding capital), under one AccountExposureCap on
total open cost-basis notional across both — recomputed from the
sleeves' own positions on every check, no mutable ledger to drift.
Chosen as a portfolio blend, not a signal filter, on the evidence:
monthly nets correlate ~-0.15 on both symbols and profit-gated sits
out ~1/3 of months where VWAP still trades. The blend row reports
per-sleeve sub-rows, merged sleeve-prefixed gate reasons, combined
realized max drawdown, and an UNREALIZED mark on open lots — the two
numbers the separate per-strategy tables couldn't show. Score-only in
backtest.py (--blended); live wiring deliberately deferred: the
scored-state restore replays audit signals, but the VWAP sleeve
consumes raw ticks (like the ladder), which aren't in the audit log —
solve that before this row runs live. 39 new checks, 611 total across
the host suite, 0 failures.

**v3.13** — the live `--profit-gate` row now gets v3.12's max-hold
fix too. A reported gap: v3.12 added `max_hold_days` to
`ProfitGatedScorecard` and wired it into backtest.py's standalone row
and the blend, but `order_manager.py`'s live/paper session still
constructed the profit-gated card with no bound at all — a live
session run today would still hold a loser open indefinitely with a
trivially-100%-by-construction win rate, the exact thing the VTI/QQQ
backtests exposed. New `--pg-max-hold-days` CLI flag on
`order_manager.py` (same default 5.0, same `<=0` disables convention
as backtest.py), threaded into the live profit-gated construction.
The `<=0`-disables normalization itself moved to one shared
`compare.normalize_max_hold_days()`, called by both CLIs, so a live
session and a backtest can't silently disagree about what the flag
means — and so the logic is unit-testable without spinning up either
argparse + main(). New coverage exercises the one path the v3.12 test
suite hadn't touched: the forced exit firing against `RiskPolicy`'s
real wall-clock fallback (`datetime.now(ET)`, no `HistoricalClock`
injected) — the actual configuration `order_manager.py`'s live row
uses, as opposed to every existing test's backtest-style historical
clock. 8 new checks, 623 total across the host suite, 0 failures.

**v3.14** — fixed the reported bug: both tick-graph canvases' right
axis wasn't showing real prices. Root cause was two compounding
issues in the embedded chart JS, found by extracting drawChart() into
an offline node-canvas harness and rendering it against realistic
tick data rather than guessing from reading the code: (1) the axis
label used a bare `(v/1e4).toFixed(2)` instead of the page's own
`usd()` helper, so it rendered "436.72" instead of "$436.72" like
every other price on the page, and (2) even fixed, the 46px gutter
reserved for the label was too narrow — measuring actual glyph widths
at the page's 10px monospace font, "$436.72" alone is ~42px (already
flush against the edge) and any 4-digit price ("$1234.56", ~48px)
would visibly clip. Gutter widened to 60px (confirmed against three
scenarios: a normal 3-digit price, a 4-digit price, and a narrow
mobile-width panel — all fit with real margin now, not just barely).
New structural regression test in test_dashboard.py parses the
served PAGE source directly (this suite has zero JS/node dependency
elsewhere, so the test stays pure Python rather than introducing one)
and confirms: the label calls usd(), the gutter is wide enough, and
X()/the gridline agree on one consistent constant. Verified the new
test actually catches the original bug by reverting the fix and
confirming it fails, then restoring it. 9 new checks, 632 total
across the host suite, 0 failures.

**v3.15** — fixed a real diagnostic gap in the blend's report: the
"gated-away signals" line took the top 3 reasons GLOBALLY over the
merged, sleeve-prefixed block_reasons dict. Since SMA-PG fires on
nearly every tick (millions of signals) and VWAP only on rare
band-touch events, every one of SMA-PG's reason-buckets outnumbers
every one of VWAP's — a global top-3 can never show a single VWAP
reason, no matter how much of VWAP's own blocked count it explains.
Confirmed on a real QQQ report: VWAP's sleeve showed 1.3M blocked
signals of its own and zero of its reasons made the merged top-3.
New BlendedScorecard.gate_summary() takes the top N reasons PER
SLEEVE instead; comparison_report() calls it when a card exposes one
(hasattr check), falling back to the original global top-3 for every
other card type — zero behavior change for non-blend rows. Also
fixed a small cosmetic bug found while testing this: a blend with
max_hold_days=None (unbounded) rendered its row as "max-hold Noned"
(an f-string pasting None directly against "d") instead of
"max-hold unbounded". 15 new checks, 644 total across the host
suite, 0 failures.

**v3.16** — VWAP bounce wired into the LIVE session (score-only): the
strategy the multi-year QQQ/VTI backtests found consistently
profitable (+$7,639 QQQ, +$1,780 VTI standalone, real 55-77% win
rates) gets its real-market evaluation path ahead of any FPGA/RTL
investment in it — the same score-first discipline every other
strategy here went through. New `--vwap-bounce` / `--vwap-band-k`
flags on order_manager.py; one scored row per configured symbol,
each card with its own RiskPolicy clone, fed raw ticks by chaining
onto br.on_echo exactly as the ladder does. Two wiring details that
matter: (1) TRADE echoes only — on_echo fires for every echo kind
including QUOTE echoes (0x82), and folding a quote's two-sided price
into Σ(p·v)/Σ(v) would corrupt the session VWAP; this is the same
accept filter the RTL applies and documents. (2) Timestamps are ET
wall-clock, because the card's session boundary is "the ET calendar
day changed" — the semantics the strategy is defined in. Honest
restart limitation, stated in --help rather than discovered later: a
mid-day restart resets this row's session VWAP and scored totals,
because the scored-signal audit replay that restores EMA/
profit-gated cannot rebuild tick-derived state (same true-but-
undocumented property the ladder has). Verified end-to-end with real
sim sessions through emulator -> bridge -> order manager, single-
and multi-symbol. 10 new checks, 654 total across the host suite,
0 failures.

**v3.17** — first RTL of the VWAP bounce engine: rtl/vwap_engine.sv +
rtl/sessctl.sv, verified standalone in simulation (1236 + 22 checks,
0 failures; all five existing benches still green including rtl/*.sv
glob compiles proving no conflicts). Built explicitly for the
higher-volume data feed future: the ACCUMULATION plane (3-stage
multiply/add pipeline; Σv, Σp·v, Σp²·v updated together for snapshot
consistency) accepts a tick EVERY cycle indefinitely, and the
EVALUATION plane (two serial restoring divides from a snapshot, ~196
cycles ≈ 500k evaluations/sec at 100 MHz) COALESCES under burst with
an eval_skips counter instead of corrupting — the current link tops
out near 480 ticks/sec (24-byte frames at 115 200 baud), so there's
three orders of magnitude of headroom before coalescing even begins,
and past that the failure mode is documented decimation of the
signal-check rate with exact sums, never wrong numbers. Two
algorithmic choices worth remembering: the band test runs entirely in
the SQUARED domain ((vwap-price)² vs k²·variance, K2_Q8 fixed-point
parameter) so no sqrt core is needed, and the engine emits POSITION-
INDEPENDENT edge events (bounce-buy 0x01 / revert-sell 0x02, SELL
dominant on simultaneity) because fabric cannot know host position —
the host mirror model must adopt this convention when the FPGA path
goes live; the event stream deliberately differs from the host-only
scorecard's position-gated stream. Session boundaries are HOST-
COMMANDED (sessctl.sv, TYPE 0x11, slot-indexed or 0xFF broadcast,
echo-is-the-ack — symcfg's exact pattern) rather than computed in
fabric: the host knows the market calendar; hardware calendar logic
would be new bug surface replicating what Python already does right.
The bench's directed-bounce phase also recorded a real strategy
subtlety the first run caught: a dive below the band drags vwap down
AND inflates sigma, so a "recovery" price can land above the new vwap
and correctly fire the SELL edge instead of the expected BUY —
computed exactly against the integer model before fixing the test's
choreography, not the DUT. NOT YET INTEGRATED: top_arty generate loop
(8 instances), 3-way signal arbiter extension, host-side integer
mirror model — that is the next drop, deliberately separate so the
engine's standalone verification stands on its own.

**v3.18** — VWAP engine integrated into top_arty: 8 per-slot instances
in the generate loop (VWAP_WARMUP / VWAP_K2_Q8 top-level parameters),
sessctl wired into the frame bus (TYPE 0x11 -> per-slot sess_rst
pulses, echoed as 0x91 = the host's ack), and the same-cycle signal
arbiter extended from 2-way/1-deep-pend to 3-way/2-deep-pend-queue
(priority SMA > EMA > VWAP, matching the existing SMA-first
convention). VWAP record type 0x05 (wire 0x85) reuses the two 32-bit
indicator payload fields for the session vwap at the evaluated
snapshot (the host verifier's cross-check value) and the engine's
eval_skips counter — saturation observability riding along in a field
that would otherwise be zero. The new pend queue also closes a latent
gap in the old 2-way arbiter (a pended record could in principle be
silently overwritten by a new collision on the very next cycle —
impossible at real tick spacing, but now impossible by construction
AND counted if it ever happens: pend_drop_count). New
tb_vwap_integration.sv drives the ACTUAL UART waveform through the
real parser/sessctl/engine/arbiter/frame_tx/uart_tx path: slot config
ack, 0x91 session-reset ack, warm-up with zero events, a directed
dive-bounce firing exactly one 0x85 BUY whose vwap field matches an
independent integer mirror, the SELL edge, a second reset proving
state really clears, and SMA crossovers still arriving as 0x83
through the 3-way arbiter — 22 checks. Two bench-authoring lessons
recorded in comments: the original drain-wait returned mid-frame (40
clock cycles of idle line is SHORTER than one stop bit at this baud),
and an alternating warm-up tape legitimately fires a SELL on the
first post-priming up-tick because it crosses vwap every tick — both
found because the DUT was right and the bench was wrong, verified
tick-by-tick against the integer model before touching either. All
eight RTL benches green (4117 checks total); host suite untouched at
654. BITSTREAM NOTE: this drop is synthesizable and IS the point
where a rebuild becomes meaningful — but hold the flash until the
host-side drop lands (0x85 parsing + bridge.py integer mirror +
verifier), because the current host misfiles unknown 0x85 frames as
echoes, where an unfiltered ladder hook would ingest their prices.

**v3.19** — the host side of the fabric VWAP path; with this pushed,
building the v3.18 bitstream is safe. tick_protocol.py: TYPE_SESSRST/
TYPE_SESSRST_ACK (0x11/0x91), pack_sessrst() (broadcast or per-slot),
0x85 signal parsing ({vwap, eval_skips} in the payload fields), and
VWAPMirror — the THIRD implementation of the engine spec (RTL, SV
bench mirror, Python), cross-checked against the exact values the RTL
simulation emitted on the same tape (BUY @3975000 vwap=3988909, SELL
@4020000 vwap=3991500: all three agree bit-for-bit). bridge.py:
vwap_bounce models + verifiers per symbol; the verifier's matcher
generalized off its hard-coded sma_fast/sma_slow keys (a vwap frame
was a KeyError) with eval_skips deliberately EXCLUDED from matching —
it is telemetry about engine load, not math, and including it would
turn a saturation report into a false divergence; send_sessrst() with
ACK-DRIVEN mirror clearing (the ack is the trigger, not the send, so
host state tracks what the fabric actually did; a timeout means
neither side reset — safe to retry); a loud warning when eval_skips
goes nonzero (mirror comparison is not tick-for-tick under
coalescing). fpga_emulator.py now emulates sessctl and the VWAP
engine, so the whole path tests without hardware. order_manager.py:
session-reset broadcast at startup (every process start IS a session
start from the fabric's view — it has no calendar; even pre-v3.18
bitstreams ack via the universal echo path, so a timeout means link
trouble); verified fabric VWAP signals route to a lazily-created
per-symbol "VWAP-FPGA" scored row — the old cards[strat] indexing had
no "vwap_bounce" key and the FIRST hardware VWAP signal would have
been a KeyError crash in the live session, caught while wiring, not
live; the same routing serves startup replay, so restored history
cannot reach different cards than live signals. Deliberate design:
the VWAP-FPGA row is DISTINCT from the --vwap-bounce row — host-
computed position-gated stream vs engine-convention event stream;
two rows measuring one strategy through two paths is the point.
Also fixed while here: the ladder's echo hook now filters to TRADE
echoes (previously latent — quote echoes carry two-sided prices its
level comparison was never meant to see; the 0x85 wire type made the
gap concrete), and dashboard.on_signal no longer KeyErrors on vwap
frames (vwap maps into the "fast" column as the cross-check value;
null fields render as a dash, not $NaN). 30 new checks; 684 total
across the host suite, 0 failures.

**v3.20** — timing closure for the VWAP engine. First synthesis of the
v3.18 bitstream failed at 100 MHz: WNS -7.437 ns, ~13.4k failing
endpoints (wide arithmetic cones replicated 8x, one per slot). Two
causes, both invisible in simulation: (1) EV_DECIDE chained vwap²
(32x32), the variance subtract, diff² (a second 32x32), the threshold
scale, and two 64-bit compares in ONE combinational cycle — two
cascaded 32x32 multiplies alone exceed a 10 ns budget on Artix-7;
(2) the accumulation multiplies had no registers around them, so
synthesis could not use the DSP48's internal MREG/PREG pipeline
registers. Fix is structural and free — the evaluation has a
~200,000-cycle budget per tick at any realistic link rate: the
decision now does ONE operation per FSM state (MSQ -> VSQ -> VAR ->
THR -> EMIT, +4 cycles), and the accumulation pipeline gained an
operand-register stage (A0 operands -> A1 products -> A2 second
product -> A3 accumulate) so every multiply has registered inputs
AND outputs — still one tick per cycle throughput, it is a straight
pipeline with no stalls. Total cost ~5 cycles out of ~200,000. Zero
behavioral change, proven: all 1236 tb_vwap checks, the 22-check
bit-level integration bench, and every other bench pass unchanged
(4117 RTL checks total); the three mirror implementations (RTL, SV
bench model, tick_protocol.VWAPMirror) agree exactly as before. The
lesson is recorded in the engine header's new "Timing closure"
section. Only rtl/vwap_engine.sv changed; host suite untouched at
684.

**v3.21** — the hardware acceptance test you can actually run.
run_selftest() existed in bridge.py but was reachable only from the
test suite; new --selftest flag on order_manager.py wires it to the
CLI (no broker, no dashboard, no OrderManager spun up — exits right
after printing PASS/DIAG). Extended run_selftest() itself to cover
what it never did before: the fabric VWAP engine and the session-
reset control path (sessctl.sv's TYPE 0x11/0x91), tested against real
hardware for the first time (simulation had already proven it in
tb_sessctl.sv / tb_vwap_integration.sv). The existing warm-up-then-
spike stimulus needed no changes — traced it exactly through
VWAPMirror before relying on it: a strictly descending price stream
keeps price at/below its own running vwap the whole warm-up (vwap
lags above the latest price by construction), so the spike is a big
upward gap that deterministically fires the SELL edge. One board
program now exercises all three engines plus session reset. New
--vwap-warmup / --vwap-k2-q8 CLI flags so the host mirror can be told
if a rebuild ever changes VWAP_WARMUP/VWAP_K2_Q8 from their top_arty.sv
defaults — the same role --fast/--slow/--ema-kf/--ema-ks already play
for SMA/EMA, closing a latent gap where VWAP had no equivalent. Help
text explicitly distinguishes --vwap-k2-q8 (must match the fabric
bitstream) from the pre-existing --vwap-band-k (tunes the independent
host-side --vwap-bounce scorecard) — easy to confuse, worth being
explicit. New [G19] in test_order_manager.py: CLI wiring, end-to-end
PASS against the emulator through the real entry point, and a
PreVWAPEmulator regression proving the diagnostic correctly isolates
"engine missing" from "link broken" — verified against tick_parser.sv
first (no type whitelist; an older board really would still ack a
0x11 frame, so the simulated failure needed to be the engine's silence,
not a suppressed ack, to be a realistic test). 15 new checks; 699
total across the host suite, 0 failures.
